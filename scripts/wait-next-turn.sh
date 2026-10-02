#!/usr/bin/env bash
# wait-next-turn.sh — keep-alive helper (prototype).
#
# Parks the calling worker waiting for the next turn of <session_id>
# instead of exiting. Prints the next <req_id> and exits 0 when one
# arrives; exits 1 on timeout (caller deletes the lock and exits).
#
# While parked, touches data/sessions/<sid>/worker.lock every second so the
# hook's queue-watch.sh skips claiming (fresh lock = parked worker will
# serve it). The worker claims the request itself (mv queue -> processing)
# and serves it per the contract in agent/SKILL.md.
#
# Hard caps live in the contract: max 3 chained turns, 90s total parked.
set -euo pipefail

SID="${1:?usage: wait-next-turn.sh <session_id> [timeout_secs]}"
TIMEOUT="${2:-90}"
BASE="${JARVIS_SERVE_DATA:-$HOME/workspace/jarvis-serve/data}"
LOCK="$BASE/sessions/$SID/worker.lock"
mkdir -p "$(dirname "$LOCK")"

deadline=$(( $(date +%s) + TIMEOUT ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  touch "$LOCK"
  for f in "$BASE"/queue/req_*.json; do
    [ -e "$f" ] || continue
    id="$(basename "$f" .json)"
    df="$BASE/dispatch/$id.json"
    [ -f "$df" ] || continue
    sid="$(python3 -c 'import json,sys
d = json.load(open(sys.argv[1]))
print(d.get("session_id") or "")' "$df" 2>/dev/null)" || continue
    if [ "$sid" = "$SID" ]; then
      echo "$id"
      exit 0
    fi
  done
  sleep 1
done

rm -f "$LOCK"
exit 1
