#!/usr/bin/env bash
# jarvis-serve hook poll script.
# Wakes a worker agent when an OpenCode request is waiting in the queue.
#
# Latency design: the runtime invokes this script at most every
# poll_interval_secs (5s minimum), but instead of exiting when the queue is
# empty, the script BLOCKS on the queue directory (0.1s stat loop, up to
# BLOCK_SECS). A request arriving mid-wait is claimed in ~0.1s instead of
# waiting for the next invocation (~2.5s average). The 5s floor then only
# bites in the 5s window right after a previous block ends.
#
# BLOCK_SECS is deliberately short (55s): the runtime serializes a hook's
# invocations, so while one invocation blocks, dry_run / hooks.run queue
# behind it. 55s keeps those diagnostics under their tool timeouts.
# Dry runs never block (single immediate check).
#
# Install: copy to ~/hooks/scripts/jarvis-serve-queue.sh, chmod +x, then
# register with the hooks tool (see agent/SKILL.md).
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

BLOCK_SECS=55

BASE="${JARVIS_SERVE_DATA:-$HOME/workspace/jarvis-serve/data}"
Q="$BASE/queue"
P="$BASE/processing"
R="$BASE/responses"
mkdir -p "$Q" "$P" "$R"
DRY="${HATCH_HOOK_DRY_RUN:-0}"

# Try to claim the oldest answerable request. On success it wakes and exits
# the script; on no-claim it returns 1. Fully race-safe: a concurrent
# invocation may claim a file between our glob and our stat/mv, so every
# filesystem touch tolerates disappearance (never trust [ -e ] under race).
claim_oldest() {
  for f in "$Q"/req_*.json; do
    id="$(basename "$f" .json)"
    mtime="$(stat -c %Y "$f" 2>/dev/null)" || continue
    [ -e "$R/$id.json" ] && continue
    age=$(( $(date +%s) - mtime ))
    [ "$age" -gt 600 ] && continue
    if [ "$DRY" = "1" ]; then
      wake "pending OpenCode request (dry-run: not claiming)" "{\"id\":\"$id\"}"
      exit 0
    fi
    if mv "$f" "$P/$id.json" 2>/dev/null; then
      wake "new OpenCode request" "{\"id\":\"$id\"}"
      exit 0
    fi
    # Lost the atomic-claim race to a concurrent invocation; try the next.
  done
  return 1
}

claim_oldest || true

if [ "$DRY" = "1" ]; then
  silent "no pending OpenCode requests" '{}'
  exit 0
fi

# Event-driven wait: block until a request appears (BLOCK_SECS < timeout).
deadline=$(( $(date +%s) + BLOCK_SECS ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  sleep 0.1
  claim_oldest || true
done

silent "no pending OpenCode requests" '{}'
exit 0
