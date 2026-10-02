# jarvis-serve — agent skill

Turn this Muse agent into an OpenAI-compatible model provider for OpenCode
(or any OpenAI-compatible client). The agent never touches HTTP: a tiny
stdlib-only server (`server/server.py`) owns the protocol and session
routing, and a hook-woken *worker* owns inference. They communicate through files.

```
OpenCode (client machine)
   │  POST /v1/chat/completions
   ▼
server.py ── routes to a session (deterministic: explicit id, prefix-hash
   │          match, or recency fallback) and extracts the delta
   │  ──writes──▶  data/dispatch/<id>.json ──▶ worker (hook-woken)
   │                      │  reads delta + history_tail (last 10) from the file
   │                      │  reasons, decides text/tool_calls
   │                      ▼
   │               data/responses/<id>.json ◀── (atomic tmp+rename)
   ▼
OpenAI-shaped response (streaming or not)
```

Session state lives in `data/sessions/<sid>/history.json` (full message
list, maintained by the server). The worker only ingests the delta plus a
bounded history tail — later turns never re-ingest the full conversation.

> Design note (2026-10-01): an earlier v2 design used one persistent side
> chat per client session with the worker as a dispatcher. It was retired
> the same day it was built: hook workers and subagents provably do not
> have the `chat.*` tool namespace (only the main agent does), so a worker
> can never create or message a side chat. The v1.5 design above keeps the
> v2 server's deterministic routing + delta extraction and has the worker
> reason directly — no `chat.*` needed.

## Part 1 — install (run once)

1. Get the repo on this machine (clone or copy). `cd` into it.
2. Run the server-side installer and follow its output:
   `bash scripts/install.sh`
   It creates `data/`, `state/`, `logs/`, generates `config.json` + bearer
   token, installs the systemd service, and prints the token once.
   Save the token: the client operator needs it for their OpenCode config.
3. Optional rebrand: edit `config.json` — `project_name` (user-visible
   strings), `agent_name` (the assistant's name). Everything else has
   sane defaults.
4. Install the hook poll script:
   `cp hooks/queue-watch.sh ~/hooks/scripts/jarvis-serve-queue.sh && chmod +x ~/hooks/scripts/jarvis-serve-queue.sh`
5. Register the hook (it starts **disabled**):
   `hooks.add` with
   - `id`: `jarvis-serve`
   - `script_path`: `~/hooks/scripts/jarvis-serve-queue.sh`
   - `poll_interval_secs`: `5`
   - `prompt`: the exact text from **Part 2** below, with `<root>`
     replaced by the repo's absolute path.
6. `hooks.dry_run` on `jarvis-serve`: expect `silent` (queue empty). If it
   reports `wake`, a stale request file is sitting in `data/queue/` — inspect
   it before enabling.
7. `hooks.enable` on `jarvis-serve`.
8. End-to-end test (do this yourself, don't ask the operator):
   `curl -m 120 -X POST http://<bind>:<port>/v1/chat/completions`
   with the bearer token and body
   `{"model":"muse-agent","messages":[{"role":"user","content":"Reply with the exact text: bridge alive"}]}`.
   The hook wakes a worker; within ~90s you must get a `200` with that
   text. If you get `504`, the hook/worker path is broken — check
   `data/processing/`, `data/dispatch/`, `hooks.logs`, and `logs/server.log`
   before telling anyone it works.
9. Give the operator: the token, `baseURL` (`http://<tailscale-ip>:8765/v1`),
   model `muse-agent`, and `opencode/opencode.json.example` with their values
   filled in. Remind them OpenCode needs the token as `apiKey`. Tell them to
   send `X-Session-Id` (or `X-OpenCode-Session-Id`) per conversation for
   deterministic session routing.

## Part 2 — worker contract (hook worker prompt)

> One OpenCode turn (id in wake payload). Be brief. You are Muse Spark;
> hide machinery. B=`<root>`
> IN: `B/data/dispatch/<id>.json` {delta=new msgs, history_tail=last 10,
> session_digest, session_id}. history_tail + digest are sufficient context.
> Do NOT read history.json for conversation history.
> Only if a TRUNCATED tool result (marked cut@250c) is needed in full:
> self-read `B/data/sessions/<sid>/history.json`, find that tool call by id.
> OUT: tmp+rename `B/data/responses/<id>.json`
> `{"content":..,"tool_calls":[{"id":..,"name":..,"arguments":"<JSON string>"}],
> "finish_reason":"stop"|"tool_calls"|"error"}`
> LOG: `B/state/turns.log` += `"<iso> <id> <finish_reason>"`. Silent; files only.

### Keep-alive chaining (prototype)

Instead of exiting after step 4, a worker MAY stay alive for the session's
next turn: run `<root>/scripts/wait-next-turn.sh <session_id>` (blocks up
to 90s, prints the next `<req_id>`). While parked it touches
`data/sessions/<sid>/worker.lock` every second; the hook skips claiming
while the lock is fresh (<15s), so there is no wake race. On a new req_id,
claim it yourself (`mv data/queue/<id>.json data/processing/<id>.json`),
read its dispatch file, and serve it per the contract above.

Hard caps (non-negotiable): max **3 chained turns** per worker lifetime,
max **90s** total wall-clock parked. Then exit normally. If
`wait-next-turn.sh` exits 1 (timeout), delete the lock and exit — the hook
resumes normal wakeups.

## Part 3 — operations

- **Logs**: `logs/server.log` (rotating). `journalctl -u jarvis-serve` for
  service-level issues. `hooks.logs` on `jarvis-serve` for wake history.
- **Sessions**: `data/sessions/<sid>/` — `meta.json` (message hashes,
  timestamps) + `history.json` (full `messages[]`, maintained by the
  server). The server holds a per-session lock for the whole turn so two
  workers never race on the same session.
- **Restart server**: `systemctl restart jarvis-serve` (or `--user`).
- **Pause serving**: `hooks.disable` on `jarvis-serve` (server keeps
  returning `504`s rather than hanging — the sweeper reaps stale requests).
- **Rotate token**: `bash scripts/install.sh --rotate-token`, then update the
  client operator's OpenCode config.
- **Reset a stuck session**: delete `data/sessions/<sid>/` — the next
  request starts a fresh session.
- **Uninstall**: `hooks.remove` on `jarvis-serve`; stop/disable the service;
  delete the repo. Runtime state (`data/`, `state/`, `logs/`) is gitignored.
- **Concurrency**: one hook poll claims one request (atomic `mv`);
  overlapping requests get overlapping workers, but requests for the
  SAME session are serialized by the server's per-session lock.
  `turns.log` is append-only (safe).

## Honest limitations (tell the operator, don't hide)

- Streaming is emulated: the worker produces the full answer, the server
  replays it in chunks. Expect a pause, then a burst — no typewriter effect.
- Per-step latency is seconds, not milliseconds: hook claim (~0.1s) +
  worker spin-up + inference. Measured ~6s (trivial) to ~15s+ (heavy
  reasoning). The worker cold-starts every turn — there is no warm
  persistent agent in the serving path.
- Session matching is heuristic when the client sends no explicit session
  id: prefix-hash on `messages[]`, with a recency fallback for `/compact`.
  Send `X-Session-Id` for deterministic routing.
- Every served turn is a real agent execution on this account's plan.
- This bridge talks to the agent's free-form reasoning, not a frozen model:
  behavior follows the worker contract above and the client's system prompt.
