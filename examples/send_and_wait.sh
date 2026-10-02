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
case "$WAIT_SECONDS" in
  *[!0-9]*) echo "WAIT_SECONDS must be a non-negative integer" >&2; exit 2 ;;
esac

field() { python3 -c 'import json, sys; print(json.load(sys.stdin).get(sys.argv[1], ""))' "$1"; }

SENT=$(python3 "$BRIDGE" send --from "$SENDER" --to "$TO" --subject "example" --ttl-minutes 60 --body "$BODY")
ID=$(printf '%s\n' "$SENT" | field id)
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
if [ "$STATE" != completed ]; then
  echo "delivery has not completed; inspect status before retrying" >&2
fi
python3 "$BRIDGE" inbox --agent "$SENDER" | python3 -c '
import json, sys
report = json.load(sys.stdin)
wanted, recipient, sender = sys.argv[1:4]
messages = [m for m in report.get("messages", [])
            if m.get("in_reply_to") == wanted and m.get("correlation_id") == wanted
            and m.get("from") == recipient and m.get("to") == sender]
print(json.dumps(messages, indent=2))
states = {m.get("reply_state") for m in messages}
sys.exit(0 if {"acknowledged", "completed"} <= states else 1)
' "$ID" "$TO" "$SENDER"
if [ "$STATE" != completed ]; then
  echo "delivery has not completed; inspect status before retrying" >&2
  exit 1
fi
