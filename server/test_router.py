#!/usr/bin/env python3
"""Unit tests for the persistent-session router in server.py. Stdlib only.

Tests the deterministic part of the design: session identification
(explicit id / prefix-hash match / recency fallback), delta extraction,
tool stripping, and seed-contract rendering. No HTTP, no agents, no side
chats — route_request() is pure logic over a temp session store.

Usage:  python3 test_router.py
Exit code 0 = all green.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import server as S

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def make_cfg(tmp: str, **overrides) -> S.Config:
    data = os.path.join(tmp, "data")
    os.makedirs(data, exist_ok=True)
    raw = {
        "bind": "127.0.0.1", "port": 18765,
        "token_sha256": hashlib.sha256(b"test-token").hexdigest(),
        "model": "muse-agent", "data_dir": data,
        "request_timeout_secs": 300,
        "log_file": os.path.join(tmp, "server.log"),
        "project_name": "test-proj", "agent_name": "testagent",
        "session_id_header": "X-Session-Id", "session_id_field": None,
        "recent_window_secs": 120, "session_idle_secs": 86400,
        "tool_desc_max_chars": 200, "seed_template": None,
        "log_wire_keys": False,
    }
    raw.update(overrides)
    path = os.path.join(tmp, "config.json")
    with open(path, "w") as f:
        json.dump(raw, f)
    cfg = S.Config(path)
    # Exercise the real seed template, not the fallback.
    cfg.seed_template = os.path.join(HERE, "..", "agent", "seed.md")
    return cfg


SYS = {"role": "system", "content": "You are a test system prompt."}
U1 = {"role": "user", "content": "first question"}
A1 = {"role": "assistant", "content": "first answer"}
T1 = {"role": "assistant", "content": None,
      "tool_calls": [{"id": "call_1", "type": "function",
                      "function": {"name": "read", "arguments": "{}"}}]}
TR1 = {"role": "tool", "tool_call_id": "call_1", "content": "file contents"}
U2 = {"role": "user", "content": "second question"}

TOOLS_V1 = [{"type": "function",
             "function": {"name": "read",
                          "description": "Read a file. " * 50,
                          "parameters": {"type": "object",
                                         "properties": {"p": {"type": "string"}}}}}]
TOOLS_V2 = [{"type": "function",
             "function": {"name": "write",
                          "description": "Write a file.",
                          "parameters": {"type": "object"}}}]


def payload(messages, tools=None):
    p = {"model": "muse-agent", "messages": messages}
    if tools is not None:
        p["tools"] = tools
    return p


# --- 1. hash stability -----------------------------------------------------
check("msg_hash ignores key order",
      S.msg_hash({"role": "user", "content": "hi"})
      == S.msg_hash({"content": "hi", "role": "user"}))
check("msg_hash differs on content",
      S.msg_hash({"role": "user", "content": "hi"})
      != S.msg_hash({"role": "user", "content": "ho"}))

# --- 2. new session ---------------------------------------------------------
tmp = tempfile.mkdtemp()
cfg = make_cfg(tmp)
d1 = S.route_request(cfg, "req_aaa", payload([SYS, U1], TOOLS_V1), {}, now=1000.0)
check("new session kind", d1["session_kind"] == "new", d1["session_kind"])
check("new session chat_id null", d1["chat_id"] is None)
check("new session delta is everything", d1["delta"] == [SYS, U1])
check("new session forwards tools_block", isinstance(d1["tools_block"], list)
      and d1["tools_block"][0]["name"] == "read")
check("tool description truncated",
      d1["tools_block"][0]["description"].endswith("\u2026")
      and len(d1["tools_block"][0]["description"]) <= 201)
check("tool params schema intact",
      d1["tools_block"][0]["parameters"]["properties"]["p"]["type"] == "string")
check("seed names the agent", "testagent" in d1["seed_contract"])
check("seed carries the tool", "`read`" in d1["seed_contract"])
check("seed has no leftover placeholders",
      "{agent_name}" not in d1["seed_contract"]
      and "{tools_block}" not in d1["seed_contract"])
check("seed carries system prompt",
      "You are a test system prompt." in d1["seed_contract"])
sid_a = d1["session_id"]
check("session id shape", sid_a.startswith("ses_") and len(sid_a) == 20, sid_a)

# --- 3. continuation (prefix match) ------------------------------------------
d2 = S.route_request(cfg, "req_bbb",
                     payload([SYS, U1, A1, T1, TR1, U2], TOOLS_V1), {},
                     now=1010.0)
check("continuation kind", d2["session_kind"] == "continue")
check("continuation same session", d2["session_id"] == sid_a)
check("continuation delta is the suffix", d2["delta"] == [A1, T1, TR1, U2])
check("unchanged tools not re-sent", d2["tools_block"] is None)

# --- 4. tools change re-sends the block --------------------------------------
d3 = S.route_request(cfg, "req_ccc",
                     payload([SYS, U1, A1, T1, TR1, U2, A1, U2], TOOLS_V2), {},
                     now=1020.0)
check("tools change still continues", d3["session_kind"] == "continue")
check("changed tools re-sent",
      isinstance(d3["tools_block"], list)
      and d3["tools_block"][0]["name"] == "write")

# --- 5. duplicate request -> empty delta -------------------------------------
d4 = S.route_request(cfg, "req_ddd",
                     payload([SYS, U1, A1, T1, TR1, U2, A1, U2], TOOLS_V2), {},
                     now=1030.0)
check("duplicate is a continuation", d4["session_kind"] == "continue")
check("duplicate delta empty", d4["delta"] == [])

# --- 6. /compact resync (recency fallback) -----------------------------------
COMPACT = [SYS, {"role": "user",
                 "content": "Summary so far: we discussed files. Continue."}]
d5 = S.route_request(cfg, "req_eee", payload(COMPACT, TOOLS_V2), {}, now=1040.0)
check("compact resyncs to recent session", d5["session_kind"] == "resync",
      d5["session_kind"])
check("resync same session", d5["session_id"] == sid_a)
check("resync delta is last message only", d5["delta"] == [COMPACT[-1]])

# --- 7. reopen an old session after a different one --------------------------
tmp2 = tempfile.mkdtemp()
cfg2 = make_cfg(tmp2)
a1 = S.route_request(cfg2, "req_1", payload([SYS, {"role": "user",
                                                  "content": "topic A"}]), {},
                     now=1000.0)
b1 = S.route_request(cfg2, "req_2", payload([SYS, {"role": "user",
                                                  "content": "topic B"}]), {},
                     now=2000.0)
check("two sessions split", a1["session_id"] != b1["session_id"])
# Back to A's full history long after the recency window: prefix match wins.
a2 = S.route_request(
    cfg2, "req_3",
    payload([SYS, {"role": "user", "content": "topic A"},
             {"role": "assistant", "content": "answer A"},
             {"role": "user", "content": "follow-up A"}]), {}, now=5000.0)
check("old session reopens by prefix", a2["session_kind"] == "continue",
      a2["session_kind"])
check("old session id restored", a2["session_id"] == a1["session_id"])
check("reopen delta is the suffix",
      a2["delta"] == [{"role": "assistant", "content": "answer A"},
                      {"role": "user", "content": "follow-up A"}])

# --- 8. explicit session id via header ----------------------------------------
tmp3 = tempfile.mkdtemp()
cfg3 = make_cfg(tmp3)
h1 = S.route_request(cfg3, "req_h1", payload([SYS, U1]), {"X-Session-Id": "abc 123"})
check("header session id used",
      h1["session_id"] == "ext_abc_123", h1["session_id"])
check("header session kind new", h1["session_kind"] == "new")
h2 = S.route_request(cfg3, "req_h2", payload([SYS, U1, A1]),
                     {"X-Session-Id": "abc 123"})
check("header session continues", h2["session_kind"] == "continue"
      and h2["session_id"] == "ext_abc_123")

# --- 9. explicit session id via body field ------------------------------------
tmp4 = tempfile.mkdtemp()
cfg4 = make_cfg(tmp4, session_id_header=None, session_id_field="sid")
f1 = S.route_request(cfg4, "req_f1", {"model": "m", "sid": "my-sess",
                                     "messages": [SYS, U1]}, {})
check("body field session id used", f1["session_id"] == "ext_my-sess",
      f1["session_id"])

# --- 10. identical first messages merge (documented heuristic limit) ----------
tmp5 = tempfile.mkdtemp()
cfg5 = make_cfg(tmp5)
m1 = S.route_request(cfg5, "req_m1", payload([SYS, U1]), {}, now=1000.0)
m2 = S.route_request(cfg5, "req_m2", payload([SYS, U1]), {}, now=1005.0)
check("identical histories share the session (known limit)",
      m2["session_id"] == m1["session_id"]
      and m2["session_kind"] == "continue")

# --- 11. system prompt extraction ----------------------------------------------
check("system from messages[]",
      S.extract_system_prompt(payload([SYS, U1])) == "You are a test system prompt.")
check("system from top-level field",
      S.extract_system_prompt({"messages": [U1], "system": "top-level sys"})
      == "top-level sys")
check("no system -> empty",
      S.extract_system_prompt(payload([U1])) == "")

# --- 12. adopt_chat_id ----------------------------------------------------------
tmp6 = tempfile.mkdtemp()
cfg6 = make_cfg(tmp6)
dd = S.route_request(cfg6, "req_z1", payload([SYS, U1]), {}, now=1000.0)
# Simulate the dispatcher: it fills chat_id into the dispatch file.
os.makedirs(cfg6.dispatch_dir, exist_ok=True)
dpath = os.path.join(cfg6.dispatch_dir, "req_z1.json")
with open(dpath, "w") as f:
    json.dump({**dd, "chat_id": "chat_side_123"}, f)
S.adopt_chat_id(cfg6, dd)
meta = S.read_session_meta(cfg6.sessions_dir, dd["session_id"])
check("adopt_chat_id records the side chat",
      meta and meta.get("chat_id") == "chat_side_123")
# A follow-up request now carries the chat_id to the dispatcher.
dd2 = S.route_request(cfg6, "req_z2", payload([SYS, U1, A1]), {}, now=1010.0)
check("follow-up dispatch carries chat_id", dd2["chat_id"] == "chat_side_123")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
