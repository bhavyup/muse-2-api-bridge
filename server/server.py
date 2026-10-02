#!/usr/bin/env python3
"""jarvis-serve: OpenAI-compatible HTTP front-end for a Muse agent.

Architecture
------------
An OpenAI-compatible client (e.g. OpenCode) POSTs to /v1/chat/completions.
The server routes the request to a persistent *session* (keyed by the
client's session id, or one fresh session per unknown request), writes a
dispatch file, and long-polls responses/<id>.json, which a hook-woken
*worker* agent writes after reasoning over the dispatch file directly.
The server owns ALL protocol correctness: the worker writes a minimal,
easy-to-produce contract (see agent/SKILL.md); the server validates it
and translates it into a spec-compliant OpenAI response (streaming or
not).

Separation of concerns (deliberate):
  - The worker does inference. It never touches HTTP, SSE, or auth.
    It reads the dispatch file (delta + history_tail) plus
    data/sessions/<sid>/history.json for continuity, then writes the
    response file atomically. It NEVER talks to side chats — per-turn
    agents do not have the chat.* tool namespace.
  - The server does protocol + session routing. It never does inference.

Stdlib only. Python >= 3.9. No third-party dependencies, ever.

Wire protocol (all files under <data_dir>/)
------------------------------------------
queue/<id>.json        written by server: {"id","received_at","stream","payload"}
dispatch/<id>.json     written by server: {"id","session_id","session_kind",
                       "chat_id","delta","tools_block","history_tail",...}
                       read by the worker
processing/<id>.json   claimed by the hook script (atomic mv from queue/)
responses/<id>.json    written by the worker (tmp file + rename = atomic)
failed/<id>.json       stale requests moved here by the sweeper
dead_letter/<id>.*.json  turns the worker never answered (watchdog);
                       parked per-stage for diagnosis
sessions/<sid>/        per-session state: meta.json (timestamps, hashes) +
                       history.json (full message history)
state/turns.log        append-only turn records (written by the worker)

Agent response contract (responses/<id>.json)
---------------------------------------------
{
  "content": "<assistant text>" | null,
  "tool_calls": [
    {"id": "call_abc", "name": "tool_name", "arguments": "<JSON string>"}
  ],                                   // optional, may be []
  "finish_reason": "stop" | "tool_calls" | "length",
  "usage": {"prompt_tokens": 0, "completion_tokens": 0},   // optional
}
... or {"finish_reason": "error", "error": "<message>"} to fail the request.

Security notes
--------------
- Bearer-token auth on every /v1/* route. Only the SHA-256 of the token is
  stored in config.json; the raw token is shown once at install time.
- Binds to 127.0.0.1 by default. For LAN/Tailscale access set "bind"
  explicitly (install.sh detects the Tailscale IPv4 address).
- Request bodies are size-capped. Tokens are never logged.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import select
import shutil
import signal
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler

VERSION = "2.0.0"
DEFAULT_PORT = 8765
DEFAULT_TIMEOUT_SECS = 300
MAX_BODY_BYTES = 8 * 1024 * 1024
REQ_ID_RE = re.compile(r"^req_[0-9a-f]{12}$")
CHUNK_CHARS = 120          # fake-streaming chunk size
CHUNK_DELAY_SECS = 0.01    # fake-streaming pacing
HEARTBEAT_SECS = 2.0       # SSE keep-alive cadence while waiting for a turn
HISTORY_TAIL_TOOL_MAX = 250  # tool outputs in history_tail are capped here

# --- output hygiene ---------------------------------------------------- #
# Defense in depth for the identity boundary: even if the worker's prompt
# contract (IDENTITY block) ever slips, the server scrubs machinery
# references before anything reaches the client. Patterns are deliberately
# tight (only our IPC dirs) so a user's own data/ directory is untouched.
SCRUB_PATTERNS = [
    re.compile(r"jarvis-serve", re.IGNORECASE),
    re.compile(r"\bdata/(queue|dispatch|processing|responses|failed|sessions)/"
               r"[A-Za-z0-9_\-]+", re.IGNORECASE),
    re.compile(r"hooks?/queue-watch\.sh", re.IGNORECASE),
]


def scrub_output(text: str) -> str:
    """Remove bridge-machinery references from agent text. Logs when it
    fires so over-scrubs are visible."""
    if not isinstance(text, str):
        return text
    out = text
    for pat in SCRUB_PATTERNS:
        out = pat.sub("[redacted]", out)
    if out != text:
        log.info("scrubbed %d machinery reference(s) from agent output",
                 len(SCRUB_PATTERNS))
    return out


def truncate_history_tail(messages: list) -> list:
    """Last 10 messages with tool outputs capped at HISTORY_TAIL_TOOL_MAX
    chars. Deep tool payloads (compiler logs, file dumps) would otherwise
    blow up prefill on every turn; the worker can read the full
    sessions/<sid>/history.json on demand (see worker contract)."""
    tail = []
    for m in messages[-10:]:
        if (isinstance(m, dict) and m.get("role") == "tool"
                and isinstance(m.get("content"), str)
                and len(m["content"]) > HISTORY_TAIL_TOOL_MAX):
            m = dict(m)
            cut = len(m["content"]) - HISTORY_TAIL_TOOL_MAX
            m["content"] = (m["content"][:HISTORY_TAIL_TOOL_MAX] +
                            "\u2026 [truncated %d chars; full output in "
                            "sessions/<sid>/history.json]" % cut)
        tail.append(m)
    return tail


def session_digest(messages: list) -> dict:
    """Compact session orientation for the dispatch file: message count
    plus the opening user message (truncated to 100 chars). Lets the
    worker bound context on deep sessions without re-reading history.
    Deterministic, zero inference cost."""
    opened_with = ""
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            opened_with = c[:100] if isinstance(c, str) else ""
            break
    return {"turns": len(messages), "opened_with": opened_with}


log = logging.getLogger("jarvis-serve")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
class Config:
    def __init__(self, path: str):
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("config must be a JSON object")
        self.bind: str = raw.get("bind", "127.0.0.1")
        self.port: int = int(raw.get("port", DEFAULT_PORT))
        self.token_sha256: str = raw.get("token_sha256", "")
        if not self.token_sha256 or len(self.token_sha256) != 64:
            raise ValueError("config.token_sha256 must be a 64-char hex digest")
        self.model: str = raw.get("model", "muse-agent")
        self.data_dir: str = raw.get("data_dir", os.path.join(os.getcwd(), "data"))
        self.request_timeout_secs: float = float(
            raw.get("request_timeout_secs", DEFAULT_TIMEOUT_SECS)
        )
        self.log_file: str = raw.get(
            "log_file", os.path.join(os.getcwd(), "logs", "server.log")
        )
        # --- persistent sessions (all optional; sane defaults) ---
        # Display/identity strings. Nothing here is user-specific: this
        # project is meant to be deployed by anyone, for any operator.
        self.project_name: str = str(raw.get("project_name", "jarvis-serve"))
        self.agent_name: str = str(raw.get("agent_name", "assistant"))
        # Explicit session identity, when the client provides one. Checked
        # first, before the content-derived matching below. Either or both
        # may be set; null/"" disables.
        self.session_id_header: str | None = (
            raw.get("session_id_header", "X-Session-Id") or None)
        self.session_id_field: str | None = (
            raw.get("session_id_field") or None)
        # /compact + message-edit fallback: a request that matches no known
        # session prefix still joins the most recent session if it was
        # active within this window and the system prompt is unchanged.
        self.recent_window_secs: float = float(
            raw.get("recent_window_secs", 120))
        # Idle sessions (no turn within this long) are treated as fresh.
        self.session_idle_secs: float = float(
            raw.get("session_idle_secs", 86400))
        # Tool descriptions are truncated to this when forwarded to the
        # worker (names + parameter schemas always go in full).
        self.tool_desc_max_chars: int = int(
            raw.get("tool_desc_max_chars", 200))
        # Standing-contract template for new sessions. Defaults to
        # <repo>/agent/seed.md next to data_dir.
        self.seed_template: str | None = raw.get("seed_template") or None
        # Log wire header names + top-level body keys per request (no
        # values, no message content). Privacy-safe; helps diagnose what
        # a client actually sends (e.g. whether it emits a session id).
        self.log_wire_keys: bool = bool(raw.get("log_wire_keys", True))
        # Active watchdog: a turn with no worker response after this long
        # is dead-lettered and answered 504 instead of hanging until
        # request_timeout_secs. Must be comfortably above the slowest
        # legitimate turn (measured worst ~17s at xhigh).
        self.watchdog_secs: float = float(raw.get("watchdog_secs", 120))

    @property
    def queue_dir(self): return os.path.join(self.data_dir, "queue")
    @property
    def processing_dir(self): return os.path.join(self.data_dir, "processing")
    @property
    def responses_dir(self): return os.path.join(self.data_dir, "responses")
    @property
    def failed_dir(self): return os.path.join(self.data_dir, "failed")
    @property
    def dispatch_dir(self): return os.path.join(self.data_dir, "dispatch")
    @property
    def dead_letter_dir(self): return os.path.join(self.data_dir, "dead_letter")
    @property
    def sessions_dir(self): return os.path.join(self.data_dir, "sessions")


def ensure_dirs(cfg: Config) -> None:
    for d in (cfg.queue_dir, cfg.processing_dir, cfg.responses_dir,
              cfg.failed_dir, cfg.dispatch_dir, cfg.dead_letter_dir,
              cfg.sessions_dir, os.path.dirname(cfg.log_file)):
        os.makedirs(d, exist_ok=True)


# --------------------------------------------------------------------------- #
# OpenAI protocol translation
# --------------------------------------------------------------------------- #
def openai_error(message: str, err_type: str = "server_error", code=None) -> dict:
    err: dict = {"message": message, "type": err_type}
    if code is not None:
        err["code"] = code
    return {"error": err}


def validate_agent_response(agent: object) -> tuple[int, dict]:
    """Validate the agent's minimal contract.

    Returns (http_status, openai_body_without_envelope). The caller wraps it
    into a streaming or non-streaming response.
    """
    if not isinstance(agent, dict):
        return 500, openai_error("agent response is not a JSON object",
                                 "agent_response_invalid")
    if agent.get("finish_reason") == "error" or "error" in agent:
        msg = str(agent.get("error") or "agent reported an error")[:500]
        return 500, openai_error("agent error: " + scrub_output(msg),
                                 "agent_error")

    content = agent.get("content")
    if content is not None and not isinstance(content, str):
        return 500, openai_error("agent 'content' must be a string or null",
                                 "agent_response_invalid")
    content = scrub_output(content)  # identity boundary, defense in depth

    raw_tcs = agent.get("tool_calls") or []
    if not isinstance(raw_tcs, list):
        return 500, openai_error("agent 'tool_calls' must be a list",
                                 "agent_response_invalid")
    tool_calls = []
    for i, tc in enumerate(raw_tcs):
        if not isinstance(tc, dict):
            return 500, openai_error(f"tool_calls[{i}] is not an object",
                                     "agent_response_invalid")
        tc_id, name, args = tc.get("id"), tc.get("name"), tc.get("arguments")
        if not tc_id or not isinstance(tc_id, str):
            return 500, openai_error(f"tool_calls[{i}].id missing/invalid",
                                     "agent_response_invalid")
        if not name or not isinstance(name, str):
            return 500, openai_error(f"tool_calls[{i}].name missing/invalid",
                                     "agent_response_invalid")
        if not isinstance(args, str):
            return 500, openai_error(
                f"tool_calls[{i}].arguments must be a JSON string",
                "agent_response_invalid")
        try:
            json.loads(args)
        except (json.JSONDecodeError, ValueError):
            return 500, openai_error(
                f"tool_calls[{i}].arguments is not valid JSON",
                "agent_response_invalid")
        tool_calls.append({
            "id": tc_id,
            "type": "function",
            "function": {"name": name, "arguments": args},
        })

    fr = agent.get("finish_reason")
    if fr is None:
        fr = "tool_calls" if tool_calls else "stop"
    if fr not in ("stop", "tool_calls", "length"):
        log.warning("agent sent unknown finish_reason %r; coercing to stop", fr)
        fr = "stop"

    usage = agent.get("usage") or {}
    try:
        pt = int(usage.get("prompt_tokens", 0))
        ct = int(usage.get("completion_tokens", 0))
    except (TypeError, ValueError):
        pt, ct = 0, 0

    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    body = {
        "message": message,
        "finish_reason": fr,
        "usage": {"prompt_tokens": pt, "completion_tokens": ct,
                  "total_tokens": pt + ct},
    }
    return 200, body


def build_chat_completion(req_id: str, model: str, inner: dict) -> dict:
    return {
        "id": "chatcmpl-" + req_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": inner["message"],
            "finish_reason": inner["finish_reason"],
        }],
        "usage": inner["usage"],
    }


def stream_chunks(req_id: str, model: str, inner: dict):
    """Yield SSE 'data:' lines (without the trailing blank line handling)."""
    created = int(time.time())
    base = {"id": "chatcmpl-" + req_id, "object": "chat.completion.chunk",
            "created": created, "model": model}

    def chunk(delta: dict, finish_reason=None) -> str:
        c = dict(base)
        c["choices"] = [{"index": 0, "delta": delta,
                         "finish_reason": finish_reason}]
        return "data: " + json.dumps(c, separators=(",", ":"))

    yield chunk({"role": "assistant"})
    content = inner["message"].get("content") or ""
    for i in range(0, len(content), CHUNK_CHARS):
        yield chunk({"content": content[i:i + CHUNK_CHARS]})
    for i, tc in enumerate(inner["message"].get("tool_calls", [])):
        fn = tc["function"]
        yield chunk({"tool_calls": [{
            "index": i, "id": tc["id"], "type": "function",
            "function": {"name": fn["name"], "arguments": ""},
        }]})
        args = fn["arguments"]
        for j in range(0, len(args), CHUNK_CHARS):
            yield chunk({"tool_calls": [{
                "index": i, "function": {"arguments": args[j:j + CHUNK_CHARS]},
            }]})
    yield chunk({}, inner["finish_reason"])
    yield "data: [DONE]"


# --------------------------------------------------------------------------- #
# Queue plumbing
# --------------------------------------------------------------------------- #
def write_json_atomic(path: str, obj: dict) -> None:
    tmp = path + ".tmp-" + uuid.uuid4().hex[:8]
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, path)  # atomic on POSIX: readers never see a partial file


# Module-level server stats for the deep /healthz endpoint.
_START_TIME = time.time()
_LAST_LATENCY_MS: int | None = None


def _set_last_latency(ms: int) -> None:
    global _LAST_LATENCY_MS
    _LAST_LATENCY_MS = ms


def sweeper_loop(cfg: Config, stop: threading.Event) -> None:
    """Reap stale queue/processing files so nothing wedges the hook or HTTP."""
    while not stop.wait(30):
        cutoff = time.time() - (cfg.request_timeout_secs + 60)
        for dirname, write_error_response in (
                (cfg.queue_dir, False), (cfg.processing_dir, True)):
            try:
                names = os.listdir(dirname)
            except OSError:
                continue
            for name in names:
                if not name.endswith(".json"):
                    continue
                req_id = name[:-5]
                if not REQ_ID_RE.match(req_id):
                    continue
                src = os.path.join(dirname, name)
                try:
                    if os.path.getmtime(src) > cutoff:
                        continue
                except OSError:
                    continue
                dst = os.path.join(cfg.failed_dir, name)
                try:
                    shutil.move(src, dst)
                    log.warning("reaped stale %s -> failed/", name)
                    if write_error_response:
                        # Unblock the waiting HTTP request instead of letting
                        # it hang until the client times out.
                        write_json_atomic(
                            os.path.join(cfg.responses_dir, name),
                            {"finish_reason": "error",
                             "error": "agent worker did not respond in time"})
                except OSError as e:
                    log.error("sweeper failed on %s: %s", name, e)


# --------------------------------------------------------------------------- #
# Persistent sessions: route each request to a long-lived session id
# --------------------------------------------------------------------------- #
# A request belongs to a *session* (one client conversation). The server
# identifies the session deterministically — no agent judgment involved:
#
#   1. Explicit id: if config.session_id_header names a request header (or
#      config.session_id_field names a top-level body field) and it is
#      present, it wins outright.
#   2. Prefix match: a known session's stored per-message hashes equal the
#      leading hashes of the incoming messages[] -> continuation. Sessions
#      are checked most-recently-active first.
#   3. Recency fallback: no prefix match, but the most recent session was
#      active within recent_window_secs and the system prompt is unchanged
#      -> same session, resync (covers /compact and manual message edits:
#      only the latest user message is forwarded; history.json already
#      holds the richer pre-compaction history).
#   4. Otherwise: a brand-new session.
#
# The hook-woken worker agent does the only thing code cannot: inference.
# It reads the dispatch file, reasons, and writes the response file. All
# matching and diffing already happened here.
#
# Session store: <sessions_dir>/<sid>/meta.json + history.json
# Dispatch file: <dispatch_dir>/<req_id>.json (read by the worker)

SESSION_ID_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_-]")

_FALLBACK_SEED = """You are {agent_name}, a model backend serving an \
OpenAI-compatible API for {project_name}. You never speak to a human \
directly: you reason over each turn's dispatch file and write your reply \
as a JSON response file; the client's harness executes any tools.

## Standing contract (every reply, no exceptions)
- Reply with EXACTLY ONE JSON object and nothing else:
  {"content": "<text>" | null, "tool_calls": [...],
   "finish_reason": "stop" | "tool_calls" | "length"}
- "content": your message text. Use null or "" when you only make tool calls.
- "tool_calls": [{"id": "call_<unique>", "name": "<tool>",
  "arguments": "<JSON string>"}]. Flat keys, NOT nested under "function".
  "arguments" MUST be a JSON-encoded string, never an object.
  Only include tools when you need the client to execute something; the
  client runs them and returns the results as your next turn.
- "finish_reason": "stop" for a final answer, "tool_calls" when emitting calls.
- Never add prose, explanations, or markdown outside the JSON object. The
  server parses your reply mechanically; anything outside it is lost.

## Client system prompt
{system_prompt}

## Tools available (the client executes these; you only decide)
{tools_block}
"""


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def msg_hash(msg: dict) -> str:
    """Stable hash of one chat message (key order independent)."""
    return hashlib.sha256(_canonical(msg).encode("utf-8")).hexdigest()


def tools_hash(tools) -> str:
    return hashlib.sha256(_canonical(tools or []).encode("utf-8")).hexdigest()


def extract_system_prompt(payload: dict) -> str:
    """System prompt from a top-level 'system' field or messages[]."""
    sys_top = payload.get("system")
    if isinstance(sys_top, str) and sys_top.strip():
        return sys_top
    parts = []
    for m in payload.get("messages", []):
        if not isinstance(m, dict) or m.get("role") != "system":
            continue
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            parts.append(" ".join(
                p.get("text", "") for p in c
                if isinstance(p, dict) and p.get("type") == "text"))
    return "\n\n".join(p for p in parts if p)


def strip_tools(tools, max_desc_chars: int) -> list:
    """Tools reduced to what the worker needs: name, short description,
    full parameter schema. Descriptions are truncated; schemas never are."""
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function")
        fn = fn if isinstance(fn, dict) else {}
        name = fn.get("name") or t.get("name") or "unknown"
        params = fn.get("parameters") or {}
        desc = fn.get("description") or t.get("description") or ""
        if not isinstance(desc, str):
            desc = str(desc)
        if len(desc) > max_desc_chars:
            desc = desc[:max_desc_chars] + "\u2026"
        out.append({"name": name, "description": desc,
                    "parameters": params})
    return out


def _session_path(sessions_dir: str, sid: str) -> str:
    return os.path.join(sessions_dir, sid)


def read_session_meta(sessions_dir: str, sid: str) -> dict | None:
    try:
        with open(os.path.join(_session_path(sessions_dir, sid),
                               "meta.json"), "r", encoding="utf-8") as f:
            m = json.load(f)
            return m if isinstance(m, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def write_session_meta(sessions_dir: str, sid: str, meta: dict) -> None:
    os.makedirs(_session_path(sessions_dir, sid), exist_ok=True)
    write_json_atomic(os.path.join(_session_path(sessions_dir, sid),
                                   "meta.json"), meta)


def explicit_session_id(cfg: Config, headers, payload: dict) -> str | None:
    """A client-supplied session id, sanitized. None when not configured
    or not present."""
    if cfg.session_id_header:
        try:
            v = headers.get(cfg.session_id_header)
        except (AttributeError, TypeError):
            v = None
        if v and str(v).strip():
            return "ext_" + SESSION_ID_SANITIZE_RE.sub(
                "_", str(v).strip())[:48]
    if cfg.session_id_field:
        v = payload.get(cfg.session_id_field)
        if isinstance(v, str) and v.strip():
            return "ext_" + SESSION_ID_SANITIZE_RE.sub(
                "_", v.strip())[:48]
    return None


def new_session_id(system_hash: str, incoming_hashes: list) -> str:
    """Content-derived session id. Two sessions that start with byte-identical
    histories intentionally share an id (they are indistinguishable anyway);
    any difference in the opening content splits them."""
    return "ses_" + hashlib.sha256(
        (system_hash + "|" + "|".join(incoming_hashes)).encode("utf-8")
    ).hexdigest()[:16]


def find_session(sessions_dir: str, incoming_hashes: list,
                 system_hash: str, now: float,
                 recent_window_secs: float) -> tuple:
    """Match incoming messages against known sessions.

    Returns (sid, kind, prefix_len); kind is one of
    "continue" (prefix matched), "resync" (recency fallback), "new".
    sid is None for "new".
    """
    sessions = []
    try:
        names = os.listdir(sessions_dir)
    except OSError:
        names = []
    for sid in names:
        meta = read_session_meta(sessions_dir, sid)
        if meta:
            sessions.append((meta.get("last_active", 0), sid, meta))
    sessions.sort(reverse=True)  # most-recently-active first
    for _, sid, meta in sessions:
        stored = meta.get("msg_hashes") or []
        if stored and incoming_hashes[:len(stored)] == stored:
            return sid, "continue", len(stored)
    if sessions:
        _, sid, meta = sessions[0]
        if (now - meta.get("last_active", 0) <= recent_window_secs
                and meta.get("system_hash") == system_hash):
            return sid, "resync", 0
    return None, "new", 0


def seed_template_text(cfg: Config) -> str:
    """Standing-contract template for new sessions."""
    path = cfg.seed_template or os.path.join(
        os.path.dirname(os.path.abspath(cfg.data_dir)), "agent", "seed.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        log.warning("seed template %s unreadable; using fallback", path)
        return _FALLBACK_SEED


def render_seed_contract(template: str, *, agent_name: str,
                         project_name: str, system_prompt: str,
                         tools_block: list) -> str:
    if tools_block:
        tools_text = "\n".join(
            "- `%s`: %s\n  parameters: %s" % (
                t["name"], t.get("description") or "(no description)",
                _canonical(t.get("parameters") or {})[:4000])
            for t in tools_block)
    else:
        tools_text = "(no tools)"
    return (template
            .replace("{agent_name}", agent_name)
            .replace("{project_name}", project_name)
            .replace("{system_prompt}", system_prompt or "(none)")
            .replace("{tools_block}", tools_text))


# Per-session locks: the request handler holds one for the whole serving
# turn, so two workers never serve the same session concurrently.
_SESSION_LOCKS: dict = {}
_SESSION_LOCKS_GUARD = threading.Lock()


def session_lock(sid: str) -> threading.Lock:
    with _SESSION_LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(sid)
        if lock is None:
            lock = threading.Lock()
            _SESSION_LOCKS[sid] = lock
        return lock


def route_request(cfg: Config, req_id: str, payload: dict,
                  headers, now: float | None = None) -> dict:
    """Identify the session, extract the delta, build the dispatch file
    content. Returns the dispatch dict (chat_id null for new sessions;
    the worker records it when serving the first turn)."""
    now = time.time() if now is None else now
    messages = [m for m in payload["messages"] if isinstance(m, dict)]
    if not messages:  # pathological; the 400-check guarantees a non-empty list
        messages = payload["messages"]
    incoming = [msg_hash(m) for m in messages]
    system_prompt = extract_system_prompt(payload)
    system_h = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
    tools = payload.get("tools") or []
    t_hash = tools_hash(tools)

    sessions_dir = cfg.sessions_dir
    os.makedirs(sessions_dir, exist_ok=True)

    # --- identify (reads only; the lock below guards the update) ---
    explicit = explicit_session_id(cfg, headers, payload)
    if explicit:
        sid = explicit
        meta_probe = read_session_meta(sessions_dir, sid)
        if meta_probe:
            stored = meta_probe.get("msg_hashes") or []
            if stored and incoming[:len(stored)] == stored:
                kind, prefix = "continue", len(stored)
            else:
                kind, prefix = "resync", 0
        else:
            kind, prefix = "new", 0
    else:
        sid, kind, prefix = find_session(
            sessions_dir, incoming, system_h, now, cfg.recent_window_secs)
        if kind == "new":
            sid = new_session_id(system_h, incoming)

    with session_lock(sid):
        meta = read_session_meta(sessions_dir, sid) or {}
        prev_tools_hash = meta.get("tools_hash")

        if kind == "continue":
            delta = messages[prefix:]
        elif kind == "resync":
            # /compact or manual edits: history.json already holds the full
            # history, so only the latest message is new information.
            delta = [messages[-1]] if messages else []
        else:  # new
            delta = list(messages)

        # Forward tool definitions only when they are new or changed.
        tools_block = None
        if kind == "new" or prev_tools_hash != t_hash:
            tools_block = strip_tools(tools, cfg.tool_desc_max_chars)

        seed = render_seed_contract(
            seed_template_text(cfg),
            agent_name=cfg.agent_name, project_name=cfg.project_name,
            system_prompt=system_prompt,
            tools_block=tools_block if tools_block is not None
            else strip_tools(tools, cfg.tool_desc_max_chars))

        # Seed contract is 30KB (19KB system prompt + tools); only send
        # when new or changed, like tools_block. Workers are stateless
        # but the hook prompt + history_tail carry the operational context;
        # the full contract is only needed on session start or change.
        seed_hash = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        prev_seed_hash = meta.get("seed_hash")
        seed_to_send = (seed if (kind == "new" or prev_seed_hash != seed_hash)
                        else None)

        meta.update({
            "session_id": sid,
            "chat_id": meta.get("chat_id"),  # worker records for new sessions
            "created_at": meta.get("created_at", now),
            "last_active": now,
            "system_hash": system_h,
            "tools_hash": t_hash,
            "seed_hash": seed_hash,
            "msg_hashes": incoming,
            "kind": kind,
        })
        write_session_meta(sessions_dir, sid, meta)
        write_json_atomic(
            os.path.join(_session_path(sessions_dir, sid), "history.json"),
            messages)

    dispatch = {
        "id": req_id,
        "session_id": sid,
        "session_kind": kind,   # new | continue | resync
        "chat_id": meta.get("chat_id"),  # null for brand-new sessions
        "delta": delta,
        "history_tail": truncate_history_tail(messages),  # last 10; tool outputs capped
        "session_digest": session_digest(messages),  # {turns, opened_with}
        "tools_block": tools_block,     # null when unchanged
        "seed_contract": seed_to_send,  # null when unchanged (30KB saved)
        "agent_name": cfg.agent_name,
        "session_idle_secs": cfg.session_idle_secs,
        "poll_timeout_secs": max(30.0, cfg.request_timeout_secs - 60),
        "sessions_dir": sessions_dir,
    }
    return dispatch


def adopt_chat_id(cfg: Config, dispatch: dict) -> None:
    """After a turn, copy the chat_id the worker recorded (new sessions)
    into the session meta. Best-effort; never raises."""
    try:
        with open(os.path.join(cfg.dispatch_dir, dispatch["id"] + ".json"),
                   "r", encoding="utf-8") as f:
            recorded = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    chat_id = recorded.get("chat_id") if isinstance(recorded, dict) else None
    sid = dispatch.get("session_id")
    if not chat_id or not sid:
        return
    try:
        with session_lock(sid):
            meta = read_session_meta(cfg.sessions_dir, sid)
            if meta and not meta.get("chat_id"):
                meta["chat_id"] = chat_id
                write_session_meta(cfg.sessions_dir, sid, meta)
                log.info("%s session %s adopted worker chat %s",
                         dispatch["id"], sid, chat_id)
    except OSError as e:
        log.warning("%s could not adopt chat_id: %s", dispatch["id"], e)


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def version_string(self):
        try:
            proj = self.server.cfg.project_name  # type: ignore[attr-defined]
        except AttributeError:
            proj = "jarvis-serve"
        return "%s/%s %s" % (proj, VERSION, self.sys_version)
    # HTTP/1.1 so keep-alive works for requests with a known Content-Length
    # (non-streaming + GETs). Streaming responses close the connection
    # explicitly after [DONE] since their length is unknowable upfront.
    protocol_version = "HTTP/1.1"

    # -- helpers ---------------------------------------------------------- #
    def log_message(self, fmt, *args):  # route stdlib noise into logging
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _auth_ok(self) -> bool:
        cfg: Config = self.server.cfg  # type: ignore[attr-defined]
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        presented = auth[len("Bearer "):].strip()
        if not presented:
            return False
        digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()
        return hmac.compare_digest(digest, cfg.token_sha256)

    def _require_auth(self) -> bool:
        if self._auth_ok():
            return True
        self._send_json(401, openai_error(
            "invalid bearer token", "invalid_request_error", "invalid_api_key"))
        return False

    def _client_gone(self) -> bool:
        """True if the HTTP client already disconnected. Non-destructive:
        MSG_PEEK never consumes bytes."""
        try:
            r, _, _ = select.select([self.connection], [], [], 0)
            if r:
                return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True
        return False

    def _prune_request(self, cfg: Config, req_id: str) -> None:
        """Client went away: pull the request back out of queue/dispatch so
        no worker wakes for it (saves a plan execution). If the hook already
        claimed it, the sweeper reaps the orphan later."""
        for d in (cfg.queue_dir, cfg.dispatch_dir):
            try:
                os.remove(os.path.join(d, req_id + ".json"))
            except OSError:
                pass

    def _dead_letter(self, cfg: Config, req_id: str, reason: str) -> None:
        """A turn the worker never answered: park the pipeline files in
        dead_letter/ (per-stage names, no clobbering) so the next turn for
        the session is not blocked behind a corpse."""
        for d, suffix in ((cfg.queue_dir, "queue"),
                          (cfg.dispatch_dir, "dispatch"),
                          (cfg.processing_dir, "processing")):
            src = os.path.join(d, req_id + ".json")
            if os.path.exists(src):
                try:
                    shutil.move(src, os.path.join(
                        cfg.dead_letter_dir, "%s.%s.json" % (req_id, suffix)))
                except OSError as e:
                    log.error("%s dead-letter move failed: %s", req_id, e)
        log.warning("%s dead-lettered: %s", req_id, reason)

    def _wait_for_agent(self, cfg: Config, req_id: str, t0: float,
                        stream: bool):
        """Poll responses/<req_id>.json until the worker delivers.

        In stream mode the caller has already sent the SSE headers, so this
        flushes `: keep-alive` heartbeats while waiting (prevents client
        timeouts on slow networks). Returns the agent dict, None on
        watchdog/timeout (caller answers 504), or the string "gone" when
        the client disconnected mid-wait (caller just returns).
        """
        path = os.path.join(cfg.responses_dir, req_id + ".json")
        deadline = t0 + cfg.request_timeout_secs
        watchdog_at = t0 + cfg.watchdog_secs
        last_beat = 0.0
        while time.time() < deadline:
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        return json.load(f)
                except (OSError, json.JSONDecodeError) as e:
                    log.error("could not read agent response for %s: %s",
                              req_id, e)
                    return {"finish_reason": "error",
                            "error": "unreadable agent response file"}
            now = time.time()
            if now >= watchdog_at:
                self._dead_letter(cfg, req_id,
                                  "no worker response after %.0fs" %
                                  cfg.watchdog_secs)
                return None
            if self._client_gone():
                self._prune_request(cfg, req_id)
                log.info("%s client disconnected; pruned unclaimed request",
                         req_id)
                return "gone"
            if stream and now - last_beat >= HEARTBEAT_SECS:
                try:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    last_beat = now
                except (BrokenPipeError, ConnectionResetError):
                    self._prune_request(cfg, req_id)
                    log.info("%s client disconnected mid-stream wait", req_id)
                    return "gone"
            time.sleep(0.05)
        self._dead_letter(cfg, req_id, "request timed out")
        return None

    # -- routes ----------------------------------------------------------- #
    def do_GET(self):
        cfg: Config = self.server.cfg  # type: ignore[attr-defined]
        if self.path == "/healthz":
            now = time.time()
            hb = os.path.join(cfg.data_dir, "tunnel.heartbeat")
            try:
                tunnel_ok = (now - os.path.getmtime(hb)) < 90
            except OSError:
                tunnel_ok = False

            def count_json(d):
                try:
                    return sum(1 for n in os.listdir(d)
                               if n.endswith(".json"))
                except OSError:
                    return 0

            self._send_json(200, {
                "status": "ok",
                "version": VERSION,
                "uptime_seconds": int(now - _START_TIME),
                "queue_depth": count_json(cfg.queue_dir),
                "processing": count_json(cfg.processing_dir),
                "active_sessions": (len(os.listdir(cfg.sessions_dir))
                                    if os.path.isdir(cfg.sessions_dir)
                                    else 0),
                "last_turn_latency_ms": _LAST_LATENCY_MS,
                "tunnel_connected": tunnel_ok,
            })
        elif self.path == "/v1/models":
            if not self._require_auth():
                return
            self._send_json(200, {
                "object": "list",
                "data": [{"id": cfg.model, "object": "model",
                          "created": int(time.time()),
                          "owned_by": cfg.project_name}],
            })
        else:
            self._send_json(404, openai_error("not found",
                                             "invalid_request_error", 404))

    def do_POST(self):
        cfg: Config = self.server.cfg  # type: ignore[attr-defined]
        if self.path != "/v1/chat/completions":
            self._send_json(404, openai_error("not found",
                                             "invalid_request_error", 404))
            return
        if not self._require_auth():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json(400, openai_error(
                "body must be 1..%d bytes" % MAX_BODY_BYTES,
                "invalid_request_error"))
            return
        try:
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, openai_error("body is not valid JSON",
                                             "invalid_request_error"))
            return
        if not isinstance(payload, dict) or not isinstance(
                payload.get("messages"), list) or not payload["messages"]:
            self._send_json(400, openai_error(
                "body.messages must be a non-empty list",
                "invalid_request_error"))
            return

        req_id = "req_" + uuid.uuid4().hex[:12]
        stream = bool(payload.get("stream"))
        if cfg.log_wire_keys:
            # Privacy-safe diagnostics: header NAMES and top-level body
            # KEYS only — never values or message content. Shows what the
            # client actually sends (e.g. whether it emits a session id).
            log.info("%s wire keys: headers=%s body_keys=%s", req_id,
                     sorted(self.headers.keys()), sorted(payload.keys()))
        write_json_atomic(os.path.join(cfg.queue_dir, req_id + ".json"), {
            "id": req_id,
            "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "stream": stream,
            "payload": payload,
        })
        # Route to a persistent session and hand the worker everything it
        # needs. The per-session lock is held for the whole turn so two
        # workers never serve the same session concurrently.
        dispatch = route_request(cfg, req_id, payload, self.headers)
        write_json_atomic(
            os.path.join(cfg.dispatch_dir, req_id + ".json"), dispatch)
        log.info("accepted %s stream=%s session=%s kind=%s", req_id, stream,
                 dispatch["session_id"], dispatch["session_kind"])

        t0 = time.time()

        def serve_turn():
            """Wait for the worker, validate its contract, clean up.
            Returns (status, inner); None = answer 504; "gone" = the
            client disconnected mid-wait, just return silently."""
            with session_lock(dispatch["session_id"]):
                agent = self._wait_for_agent(cfg, req_id, t0, stream)
                # Pick up the side-chat id the worker recorded (new sessions).
                adopt_chat_id(cfg, dispatch)
                # Best-effort cleanup; files stay for debugging if removal fails.
                for d in (cfg.queue_dir, cfg.processing_dir,
                          cfg.responses_dir, cfg.dispatch_dir):
                    try:
                        os.remove(os.path.join(d, req_id + ".json"))
                    except OSError:
                        pass
                if agent is None:
                    log.warning("%s no agent response (watchdog/timeout)",
                                req_id)
                    return None
                if agent == "gone":
                    return "gone"
                status, inner = validate_agent_response(agent)
                if status != 200:
                    log.warning("%s agent contract invalid", req_id)
                return status, inner

        def sse_error_frame(obj: dict) -> None:
            # Headers already sent: surface the failure as an SSE error
            # frame instead of hanging or bare-closing the stream.
            try:
                self.wfile.write(("data: " + json.dumps(obj) + "\n\n"
                                  "data: [DONE]\n\n").encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                self.close_connection = True

        if not stream:
            result = serve_turn()
            if result is None:
                self._send_json(504, openai_error(
                    "agent did not respond in time", "timeout",
                    "agent_timeout"))
                return
            if result == "gone":
                return
            status, inner = result
            if status != 200:
                self._send_json(status, inner)
                return
            self._send_json(200, build_chat_completion(
                req_id, cfg.model, inner))
            _set_last_latency(int((time.time() - t0) * 1000))
            log.info("%s served stream=%s", req_id, stream)
            return

        # --- streaming: headers FIRST so SSE heartbeats can flow while the
        # --- worker reasons. Length is unknowable upfront, so termination
        # --- = connection close after [DONE].
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        result = serve_turn()
        if result is None:
            sse_error_frame(openai_error("agent did not respond in time",
                                         "timeout", "agent_timeout"))
            return
        if result == "gone":
            self.close_connection = True
            return
        status, inner = result
        if status != 200:
            sse_error_frame(inner)
            return
        try:
            for line in stream_chunks(req_id, cfg.model, inner):
                self.wfile.write((line + "\n\n").encode("utf-8"))
                self.wfile.flush()
                time.sleep(CHUNK_DELAY_SECS)
        except (BrokenPipeError, ConnectionResetError):
            log.info("%s client disconnected mid-stream", req_id)
        finally:
            self.close_connection = True
        _set_last_latency(int((time.time() - t0) * 1000))
        log.info("%s served stream=%s", req_id, stream)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="jarvis-serve agent bridge")
    ap.add_argument("--config", default=os.environ.get(
        "JARVIS_SERVE_CONFIG", "config.json"))
    ap.add_argument("--version", action="version", version=VERSION)
    args = ap.parse_args(argv)

    try:
        cfg = Config(args.config)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"bad config {args.config}: {e}", file=sys.stderr)
        return 2
    ensure_dirs(cfg)

    logging.basicConfig(level=logging.INFO)
    fh = RotatingFileHandler(cfg.log_file, maxBytes=2 * 1024 * 1024,
                             backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(fh)
    log.setLevel(logging.INFO)

    stop = threading.Event()
    sweeper = threading.Thread(target=sweeper_loop, args=(cfg, stop),
                               name="sweeper", daemon=True)
    sweeper.start()

    server = ThreadingHTTPServer((cfg.bind, cfg.port), Handler)
    server.cfg = cfg  # type: ignore[attr-defined]
    server.daemon_threads = True

    def on_term(signum, frame):
        log.info("shutting down")
        stop.set()
        # shutdown() blocks until serve_forever() notices; calling it on the
        # serve_forever thread itself (where signal handlers run) deadlocks.
        threading.Thread(target=server.shutdown, name="shutdown",
                         daemon=True).start()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    log.info("%s %s listening on %s:%d model=%s",
             cfg.project_name, VERSION, cfg.bind, cfg.port, cfg.model)
    try:
        server.serve_forever()
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
