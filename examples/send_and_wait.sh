#!/bin/sh
# Example external sender that uses the bridge.py command line (no network, no keys).
#
#   export CLAUDE_MAILBOX_ROOT="$HOME/mailbox"
#   sh examples/send_and_wait.sh bot worker "Reply with the word pong."
#
# Sends one instruction from <sender> to <session agent>, waits for it to reach a
# final state, then prints the replies waiting in the sender's own mailbox.
set -eu

if [ "$#" -ne 3 ]; then
  echo "usage: $0 <sender> <session-agent> <body>" >&2
  exit 2
fi
: "${CLAUDE_MAILBOX_ROOT:?set CLAUDE_MAILBOX_ROOT to the mailbox root}"

HERE=$(cd "$(dirname "$0")/.." && pwd)
BRIDGE="$HERE/bridge.py"
SENDER=$1
TO=$2
BODY=$3
WAIT_SECONDS=${WAIT_SECONDS:-300}

field() { python3 -c 'import json, sys; print(json.load(sys.stdin).get(sys.argv[1], ""))' "$1"; }

ID=$(python3 "$BRIDGE" send --from "$SENDER" --to "$TO" --subject "example" --ttl-minutes 60 --body "$BODY" | field id)
echo "sent $ID; queued is not delivered"

STATE=queued
LAST=
END=$(( $(date +%s) + WAIT_SECONDS ))
while [ "$(date +%s)" -lt "$END" ]; do
  # `status` is read-only: it creates and moves nothing.
  STATE=$(python3 "$BRIDGE" status --agent "$TO" --limit 200 | python3 -c '
import json, sys
wanted = sys.argv[1]
print(next((m["state"] for m in json.load(sys.stdin)["messages"] if m["id"] == wanted), "unknown"))' "$ID")
  if [ "$STATE" != "$LAST" ]; then echo "state: $STATE"; LAST=$STATE; fi
  case "$STATE" in completed|blocked|expired|rejected|failed) break ;; esac
  sleep 2
done

# Replies addressed to the sender. Add --archive to file them under closed/ once read.
python3 "$BRIDGE" inbox --agent "$SENDER"
[ "$STATE" = completed ]
