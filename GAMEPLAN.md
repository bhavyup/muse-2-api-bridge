# jarvis-serve — GAMEPLAN.md

The complete build history: every decision, every problem, every fix, from
the first green light to the shippable product. Nothing shortened. File
pointers everywhere so you can read the primary sources yourself.

---

## Table of contents

1. [Origin & motivation](#1-origin--motivation)
2. [User constraints (the non-negotiables)](#2-user-constraints-the-non-negotiables)
3. [v1 — the naive bridge](#3-v1--the-naive-bridge)
4. [v2 — persistent sessions + the side-chat design](#4-v2--persistent-sessions--the-side-chat-design)
5. [The invalidation — workers can't touch side chats](#5-the-invalidation--workers-cant-touch-side-chats)
6. [v1.5 — the worker reasons directly](#6-v15--the-worker-reasons-directly)
7. [The hook corruption incident](#7-the-hook-corruption-incident)
8. [Live deployment — the reverse tunnel](#8-live-deployment--the-reverse-tunnel)
9. [First real user test from OpenCode](#9-first-real-user-test-from-opencode)
10. [The latency saga](#10-the-latency-saga)
11. [The identity leak](#11-the-identity-leak)
12. [Reasoning effort — the per-session wish](#12-reasoning-effort--the-per-session-wish)
13. [GitHub readiness pass](#13-github-readiness-pass)
14. [Final architecture (v1.5 as shipped)](#14-final-architecture-v15-as-shipped)
15. [Deploy runbook for the second account](#15-deploy-runbook-for-the-second-account)
16. [File index — where everything lives](#16-file-index--where-everything-lives)

---

## 1. Origin & motivation

**Date:** 2026-09-30. **Where:** main chat with Bhavya Upreti.

The user had spent days on the pc-mcp saga (typed Windows-PC toolset over
Tailscale SSH — see `~/AGENTS.md` for that whole epic). Separately, there
was a parked experiment: a reverse-engineered muse.ai bridge that tapped
decrypted frames (`docs/BRIDGE.md` in some earlier workspace — since
parked per the user's call on 2026-09-28 as unproven).

The user's empirical verdict, after testing Muse Spark 1/2/1.3 inside
OpenCode themselves: **raw model + OpenCode's harness wins for agentic
coding** ("long context, no decay"). The Jarvis bridge concept was
repositioned: **whole-task delegation** (PC ops, research, memory-rich
personal tasks), NOT coding loops.

jarvis-serve was green-lit 2026-09-30 as: *"a real GitHub-hosted project on
my second Muse account, generalized for any agent."* The pitch: point
OpenCode at your Muse agent as if it were an OpenAI model — the agent's
judgment and memory, OpenCode's local tools, no per-token API bill.

> Source: `~/MEMORY.md` → "jarvis-serve" section; alignment synthesis
> `~/dreams/alignment/derived/ALIGNMENT_SYNTHESIS.md`.

## 2. User constraints (the non-negotiables)

Set 2026-09-30, before a line of code:

1. **Deploys on the second Muse account, NOT the main one.** Nothing
   installed or registered on the main account. Everything done on the
   main account since is temporary verification only.
2. **Generalized real-world project, not hardcoded to Jarvis/me.** No
   "Jarvis" branding in user-visible strings; configurable
   `project_name` / `agent_name`.
3. **Honest limitations documented.** Fake streaming (burst not typewriter),
   per-turn hook wakeup latency (seconds), serial-ish, every turn burns the
   account's plan. Never quote only the best-case number.

Later additions (2026-10-01):

4. **"Always tell before doing something" extended to removals/retirements.**
   Never retire designs, hooks, or files silently. (Set after the v2
   side-chat design was retired and the old hook id removed without
   telling the user.)
5. **The worker must never mention jarvis-serve.** It identifies as a Muse
   agent; the machinery is invisible. (Set after the identity leak, §11.)

> Source: `~/MEMORY.md` → Preferences (2026-09-30, 2026-10-01 entries).

## 3. v1 — the naive bridge

**What was built first:** `server/server.py` (stdlib-only HTTP server),
`agent/SKILL.md`, `hooks/queue-watch.sh`, `scripts/install.sh`, systemd
unit, `README.md`, `docs/ARCHITECTURE.md`, `opencode/opencode.json.example`.

**How v1 worked:** OpenCode → server → queue → hook wakes a fresh worker
→ worker gets the request → worker reasons → writes response file →
server returns it.

**The problem:** every turn woke a *fresh* worker and re-fed it the
*entire conversation*. Latency grew with session length. Measured: **a
4-step turn took 1m41s, each step slower than the last.** Unusable for
real sessions.

**The lesson:** per-turn cost must be flat, not linear in history length.
This drove the v2 session design.

> Source: `docs/ARCHITECTURE.md` → "Bounded context per step" section.

## 4. v2 — persistent sessions + the side-chat design

**The v2 server** (`server/server.py` v2.0.0) added deterministic session
routing, all in pure Python with 38 unit tests (`server/test_router.py`):

1. **Explicit id** — `X-Session-Id` / `X-OpenCode-Session-Id` header (or a
   body field). Wins outright.
2. **Prefix-hash match** — the session's stored per-message SHA-256 hashes
   equal the leading hashes of incoming `messages[]`.
3. **Recency fallback** — no prefix match, but the most recent session was
   active within 120s with the same system prompt → same session, `resync`
   (covers `/compact` and manual edits).
4. **New session** — content-derived id.

The server extracts only the **delta** (what's new since the last turn)
and persists full `messages[]` to `data/sessions/<sid>/history.json`.

**The v2 agent design** (built 2026-10-01 morning): one persistent **side
chat** per OpenCode session. The hook-woken worker was a *dispatcher* —
it read the dispatch file, forwarded the delta to the session's side chat
(creating and seeding it on first turn via `agent/seed.md`), polled the
reply, parsed the JSON envelope leniently, and wrote the response file.
The side chat's transcript was its memory: warm, runtime-managed, never
re-sent. This was the "2-4 second" architecture — a warm chat with full
context answers fast.

**Why it was attractive:** the user had seen a spike (a screenshot of a
"156"/"200" thread) where an agent answering in a side chat replied in
2-4s. The v2 design tried to put that speed in the serving path.

> Source: `agent/seed.md` (marked RETIRED at the top);
> `docs/ARCHITECTURE.md` (v2 design note, preserved).

## 5. The invalidation — workers can't touch side chats

**Date:** 2026-10-01, the same day v2 was built. **This is the single most
important event in the project's history.**

A subagent was tasked with verifying the v2 design's core assumption: that
a hook-woken worker could use `chat.*` tools to create/message side chats.
Result: **definitive failure.** `tool_search.load_tool_namespace(["chat"])`
fails inside hook worker and subagent runtimes ("no deferred namespaces
matched"). The `chat.*` namespace exists **only in the main agent's
runtime.**

**What this meant:** no worker could ever create or message a side chat.
The v2 dispatcher design was impossible — not slow, not flaky, *impossible*.

**The twist:** the user's quoted spike (their screenshot) was **me doing
it manually as the main agent** — that's why it worked then. The main agent
has `chat.*`; workers never will.

**What was done:** the v2 side-chat design was retired the same day it was
built. `agent/seed.md` marked RETIRED at the top. `agent/SKILL.md` and
`docs/ARCHITECTURE.md` rewritten for the replacement design. (This
retirement happened without notifying the user first — see constraint #4
in §2, which was created because of this.)

> Source: `agent/seed.md` (retirement banner); `agent/SKILL.md` design
> note; `~/MEMORY.md` → jarvis-serve v1.5 entry.

## 6. v1.5 — the worker reasons directly

**The replacement:** keep everything the v2 *server* got right
(deterministic routing, delta extraction, session persistence) and change
only the worker's job. Instead of dispatching to a side chat, **the worker
reasons directly**: read the dispatch file → delta + bounded history tail
→ decide text/tool_calls → write the response file atomically → stay
silent.

**No `chat.*` needed.** The worker never touches side chats. Session
memory lives in `data/sessions/<sid>/history.json` on disk, not in a
chat transcript.

**First E2E (curl, 2026-10-01 ~11:25–11:26 UTC):** turn 1 "banana" in 33s,
turn 2 recalled "banana" from history in 27s, `stream:false`. Context
worked. Latency didn't (see §10).

> Source: `agent/SKILL.md` Part 2 (worker contract);
> `~/workspace/jarvis-serve/state/turns.log` (turn records).

## 7. The hook corruption incident

**Date:** 2026-10-01. **Hook id:** the original `jarvis-serve`.

**Symptom:** workers woke but wrote nothing for 2+ minutes. Verified by a
120-second filesystem watch: the claimed file sat in `data/processing/`,
the dispatch file existed, no response file appeared.

**Diagnosis:** a trivial diagnostic hook (`diag-trivial`, writes "alive"
to /tmp) ran fine in the same runtime — proving the runtime was healthy
and the hook *definition* was the problem. The original hook id had
stuck/corrupt worker state.

**Fix:** recreated the hook fresh as **`jarvis-serve2`** → worked
immediately. The old id was removed (archived to
`/home/hatch/hooks/archive/`).

**Cost:** this removal happened without notifying the user — the incident
that created constraint #4 ("always tell before removing or retiring
anything"). Apologized, committed.

**Live hook as of now:** `jarvis-serve2` → script
`~/hooks/scripts/jarvis-serve-queue.sh`, poll 5s, enabled, delivery to the
jarvis-serve side chat (`f9aa3abf-27f5-47a6-b496-b52043ba07c7`).

> Source: `~/hooks/logs/jarvis-serve2.jsonl`; `~/MEMORY.md` → 2026-10-01
> preference entry.

## 8. Live deployment — the reverse tunnel

**The problem:** the VM's Tailscale is client-only (accepts no inbound
connections), so OpenCode on the user's PC couldn't dial the server
directly.

**The solution:** `scripts/reverse-tunnel.py` (stdlib + paramiko, CONNECT
via the runtime HTTP proxy on port 3130, host key pinned, auto-reconnect
with backoff). It opens:

```
PC 127.0.0.1:18765 ──SSH──▶ VM 127.0.0.1:8765
```

OpenCode on the PC uses `baseURL: http://127.0.0.1:18765/v1` (its own
loopback).

**Timeline 2026-10-01:**
- ~16:16 IST: server v2.0.0 + tunnel redeployed as background processes.
  Healthz ok.
- 10:20 UTC: tunnel went down.
- 10:39 UTC: tunnel reconnected (auto-reconnect worked).
- 10:53–10:55 UTC: forwarded the user's OpenCode connections.

**Caveat:** the tunnel is a plain background process. It dies on VM
restart and needs a manual restart. (Documented in README.)

**Token:** `~/workspace/jarvis-serve/.token` (mode 600, gitignored —
location recorded, value never recorded).

> Source: `scripts/reverse-tunnel.py`; `~/workspace/jarvis-serve/logs/reverse-tunnel.log`.

## 9. First real user test from OpenCode

**Date:** 2026-10-01 ~16:57–17:05 IST.

The user sent "hiii" from OpenCode. The worker replied: "Hey! How's it
going — what are you working on today?" Then the user asked "what did we
do before? do you remember?" — and the worker **correctly recalled the
earlier nim-playground-provider session** (payload.json, payloads.json,
the killed PowerShell HAR step) from `history.json`.

**What this proved:** session history + delta extraction + deterministic
session-id matching all work through the real PC → tunnel → server path.
The user's earlier complaint ("your curl tests created a new chat and
asked two things there") was resolved — this was their real OpenCode
session.

> Source: `~/workspace/jarvis-serve/state/turns.log`;
> `~/workspace/jarvis-serve/data/sessions/ext_ses_f1d57ef2effewoAPobvidCI0G1/history.json`.

## 10. The latency saga

### 10a. The complaint

User's words: *"side chat fix was for speed, agent really answering in a
chat is nearly 2-4 s while this takes 15s minimum."*

The honest bind: the 2-4s came from a **warm** chat with full context
loaded. Workers **cold-start every turn** (spin-up + file reads +
reasoning). The fast path needs `chat.*`, which workers don't have (§5).
There is no architecture that puts a warm chat in the serving path — the
main agent is the only runtime with `chat.*`, and it's conversational, not
a real-time server. (Alternatives considered and rejected: main-agent
polling, hook-to-main handoffs, dedicated account, workflows — all hit the
same wall.)

### 10b. The profiling

Measured breakdown per turn:
- Server accept → hook claim: **~1s** (the queue-watch script's 0.1s stat
  loop + 5s poll; claim is atomic `mv`)
- Hook claim → worker done: **30-40s** (the bottleneck — all inside the
  worker)
- Worker done → server 200: **~1s**

Inside the worker: spin-up + prompt parsing + file reads + reasoning +
file writes. The reasoning is the bulk and can't be sped up directly —
but everything around it could be trimmed.

### 10c. The optimization (2026-10-01)

Two changes:

1. **Server embeds `history_tail` in the dispatch file.**
   `server/server.py` → the dispatch dict gained
   `"history_tail": messages[-10:]`. The worker reads **one file instead
   of two** (no separate `history.json` read + parse).
2. **Worker prompt trimmed ~1200 → ~700 chars.** Removed the verbose
   explanations; kept only the contract.

Also added a `LATENCY-SENSITIVE` instruction block ("think briefly,
answer directly, do not over-deliberate") after the user noted subagent
reasoning is xhigh and can't be lowered per-session (§12).

### 10d. The results

| Turn | Before | After |
|---|---|---|
| Turn 1 (trivial) | 33s | **6s** |
| Turn 2 (with context) | 27s | **9s** |

Context intact (turn 2 correctly recalled turn 1). Real-world turns with
heavier reasoning measured ~13s worker + ~4s overhead ≈ 17s total —
the remaining time is the model actually thinking at xhigh effort, which
is the floor for this architecture.

> Source: `server/server.py` (dispatch dict);
> `~/hooks/definitions/jarvis-serve2.json` (live prompt);
> `~/workspace/jarvis-serve/state/turns.log`.

## 11. The identity leak

**Date:** 2026-10-01 ~17:30 IST. The user sent a screenshot of their
OpenCode session. They'd asked "ok which model are you? nd can you call
tools?" The worker replied: *"I'm running on the model
jarvis-serve/muse-agent (a Muse Spark backend)."*

**The problem:** the worker parroted the model string from the user's
OpenCode config (`opencode.json` → `models: { "muse-agent": ... }` under
provider `jarvis-serve`). The user: *"whyd it say jarvis-serve now? it
should not."*

**The fix:** an IDENTITY block in the worker prompt (and in
`agent/SKILL.md` Part 2 for the second-account deploy):

> IDENTITY: you are a Muse agent (Muse Spark). If asked what model you
> are, say so. NEVER mention jarvis-serve, the bridge, dispatch files,
> hooks, workers, or any implementation details. The user must never see
> the machinery.

Also scrubbed `opencode/opencode.json.example` display names ("Muse
Agent", not "Muse Agent (jarvis-serve)").

> Source: `~/hooks/definitions/jarvis-serve2.json`; `agent/SKILL.md`.

## 12. Reasoning effort — the per-session wish

**The exchange:** after the latency work, the user noted their subagents
run at `xhigh` reasoning effort (`~/config/home.yaml`:
`root_agent: max`, `subagent: xhigh`) and said lowering it globally would
"make all subagents go loose on reasoning." Their wish: *"Only if i can
alter session based reasoning for agents then it would be good."*

**The answer:** the config only supports global `root_agent` / `subagent`
effort — no per-hook, per-session, or per-worker override exists. The
global setting was left untouched. The mitigation is the
`LATENCY-SENSITIVE` prompt block (§10c), which guides the model to spend
fewer reasoning tokens on simple turns without changing the effort
parameter.

> Source: `~/config/home.yaml`.

## 13. GitHub readiness pass

**Date:** 2026-10-01 ~17:35 IST. Goal: the repo must be pushable as-is,
and the second account's agent must be able to clone → install → serve
with no tribal knowledge.

**What was fixed:**

1. **`scripts/reverse-tunnel.py` generalized.** Was hardcoded with the
   user's SSH user (`91952`), key path (`~/.ssh/id_bhavya_win`), and PC
   host. Now: `--ssh-user`, `--ssh-key`, `--remote-host` flags with
   `JARVIS_SSH_USER` / `JARVIS_SSH_KEY` / `JARVIS_PC_HOST` / 
...[truncated 4001 chars]