# jarvis-serve — architecture

## The idea in one paragraph

OpenAI's chat-completions format is the lingua franca of coding harnesses.
Instead of reimplementing a model, jarvis-serve *rents the protocol*: a tiny
server accepts OpenAI-shaped requests, routes each one to a persistent
session, and a hook-woken worker reasons over the new delta plus a bounded
history tail and returns an OpenAI-shaped answer. OpenCode thinks it's
talking to a model; it's actually talking to an agent that remembers the
session — without ever re-ingesting the full conversation.

## Why this shape (decisions and rejections)

**Bounded context per step, not a cold re-ingest.** The first design
woke a fresh worker per request and re-fed it the entire conversation
every turn — latency grew with session length (a 4-step turn measured
1m41s, each step slower than the last). Now the server matches each request
to a session *deterministically in code* (explicit id → prefix-hash on
`messages[]` → recency fallback), extracts only the delta, embeds the last
10 history messages in the dispatch file, and persists the full `messages[]`
to `data/sessions/<sid>/history.json`. The worker reads one file and ingests
the delta plus the tail — per-step cost stays flat.

> A v2 variant (2026-10-01) tried one persistent side chat per session with
> the worker as a dumb dispatcher. Retired the same day: hook workers and
> subagents provably lack the `chat.*` tool namespace (it exists only in the
> main agent's runtime), so a worker can never create or message a side chat.
> v1.5 keeps v2's deterministic routing + delta extraction with the worker
> reasoning directly.

**Deterministic routing, not agent judgment.** Session matching, delta
extraction, and tool-diffing are pure functions in `server.py` with 38 unit
tests (`server/test_router.py`). No agent is ever asked "which session is
this" — that would be slow, flaky, and untestable.

**The worker is the thinker; the server is the router.** The worker reads
the dispatch file (delta + history tail + tools block already inside),
decides text and/or tool_calls (the client executes tools — the worker only
decides), and writes the response file atomically. All routing decisions
were already made deterministically by the server (which session, what's new).

**The server owns the protocol; the worker owns inference.** The server
validates every response envelope and translates it to spec-compliant
OpenAI (streaming or not). Per-session locks in the server serialize turns
so two workers never race on the same session.

**Fake streaming.** Workers produce whole answers; the server replays them
as SSE chunks. Real token streaming would require the worker to stream,
which the runtime doesn't offer. The burst is honest and documented.

**File-backed session memory, not in-context memory.** `data/sessions/<sid>/`
holds `meta.json` (per-message hashes, timestamps) and `history.json` (full
`messages[]`, appended every turn). Reopening a days-old client session
prefix-matches its stored history and resumes. `/compact` doesn't rewrite
anything: the recency fallback forwards only the latest message as a
`resync` delta.

**Identity.** The worker never mentions jarvis-serve, the bridge, dispatch
files, hooks, or workers. It identifies as a Muse agent. The machinery is
invisible by contract (see the IDENTITY block in `agent/SKILL.md` Part 2).

## Wire protocol

All under `<root>/data/`:

| File | Writer | Contents |
|---|---|---|
| `queue/<id>.json` | server | `{"id","received_at","stream","payload"}` — the hook claims this |
| `dispatch/<id>.json` | server | `{"id","session_id","session_kind","delta","history_tail","tools_block","agent_name","poll_timeout_secs","sessions_dir"}` — everything the worker needs in one read |
| `processing/<id>.json` | hook script | atomically `mv`'d from `queue/` (the claim) |
| `responses/<id>.json` | worker | agent contract, via temp-file + rename (atomic) |
| `failed/<id>.json` | sweeper | stale requests the worker never answered |
| `sessions/<sid>/` | server | `meta.json` + `history.json` (session index) |

`<id>` is `req_` + 12 hex chars, server-generated. `<sid>` is
`ses_<16 hex>` (content-derived) or `ext_<sanitized>` (client-supplied).

## Session routing (deterministic)

1. **Explicit id** — `session_id_header` (default `X-Session-Id`) or
   `session_id_field` (a top-level body field, unset by default). Also
   honors `X-OpenCode-Session-Id`. Wins outright when present.
2. **Prefix match** — a known session's stored per-message SHA-256 hashes
   equal the leading hashes of the incoming `messages[]`. Checked
   most-recently-active first.
3. **Recency fallback** — no prefix match, but the most recent session was
   active within `recent_window_secs` (default 120s) with the same system
   prompt → same session, `resync`: only the latest user message is
   forwarded (covers `/compact` and manual message edits).
4. **New session** — content-derived id from the full opening messages.

The delta is everything after the matched prefix (`continue`), the last
message (`resync`), or the whole body (`new`). Tool definitions ride in the
dispatch file only when new or changed (descriptions truncated to
`tool_desc_max_chars`, parameter schemas always whole).

## Failure modes

| Failure | Detection | Behavior |
|---|---|---|
| Worker never answers | server long-poll timeout (300s default) | `504` + OpenAI error, `code: agent_timeout` |
| Stale queue/processing file | sweeper thread (30s cadence) | moved to `failed/`; orphans in `processing/` get a synthetic error response so the HTTP wait ends |
| Worker writes malformed envelope | `validate_agent_response` | `500` + OpenAI error object; never a hang, never a crash |
| Worker reports error | `finish_reason: error` contract | `500` + the worker's message |
| Dispatch file missing | worker checks on read | worker writes error envelope; request never hangs |
| Bad bearer token | constant-time compare | `401` |
| Bad client body | size cap, JSON parse, `messages` check | `400` |
| Server SIGTERM/SIGINT | signal handler | graceful: sweeper stops, `serve_forever` exits via a shutdown thread (calling `shutdown()` on the serving thread deadlocks — it's done off-thread) |
| Client disconnects mid-stream | `BrokenPipeError` guard | logged, no crash |
| Two hook polls race | atomic `mv` claim | second poll sees nothing to claim |
| Hook fires with no server | worker writes response file nobody reads | harmless; files sit until cleanup |
| Session dir deleted mid-use | `read_session_meta` → None | next request starts a fresh session |

## Concurrency

`ThreadingHTTPServer`: concurrent HTTP requests are fine. A per-session
lock is held for the whole serving turn, so requests for the SAME session
are serialized (matches how clients actually behave — one step at a time).
Different sessions proceed in parallel. `turns.log` is append-only (safe).

## Latency

Per turn: hook claim (~0.1s via the blocking queue-watch script) + worker
spin-up + inference. Measured on loopback: ~6s trivial, ~9s with context,
~15s+ for heavy reasoning. The worker cold-starts every turn — there is no
warm persistent agent in the serving path, so this is the floor. Optimizations
applied: `history_tail` embedded in the dispatch file (one file read, not
two), worker prompt trimmed (~1200 → ~700 chars), and a LATENCY-SENSITIVE
instruction block in the prompt.

## Security model

- Bearer token on `/v1/*`; only SHA-256 stored, 0600 perms, constant-time
  compare. Raw token shown once at install, also in `.token` (0600,
  gitignored).
- Default bind is loopback; installer detects the Tailscale IPv4 and binds
  that. Never expose the port publicly — the token is the only auth.
- Bodies capped at 8 MB; tokens, bodies, and message content are never
  logged (request ids, session ids, kinds, and status only).
- The worker never executes client tools — it only *decides* `tool_calls`;
  execution stays on the client machine.
- The wire-keys diagnostic log records header NAMES and body KEYS only —
  never values or content.
