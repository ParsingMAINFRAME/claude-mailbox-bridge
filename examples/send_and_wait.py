#!/usr/bin/env python3
"""Example external sender that uses the file protocol directly (no bridge.py, no network, no keys).

It writes one instruction envelope into a session agent's mailbox, then polls,
read-only, for that message's state and for the replies addressed back to the
sender. Standard library only.

    export CLAUDE_MAILBOX_ROOT="$HOME/mailbox"
    python3 examples/send_and_wait.py --from bot --to worker --body "Reply with the word pong."

The only thing this script writes is the one envelope file. It never moves or
deletes mailbox files: that is the helper's job (`bridge.py inbox --archive`).
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import sys
import time
import uuid

DONE = ("completed", "blocked", "expired", "rejected", "failed")


def utc(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def read_json(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def send(root, sender, to, subject, body, ttl_minutes):
    """Publish one envelope: complete it in tmp/, then link it into new/ so it is never seen half-written."""
    now = datetime.datetime.now(datetime.timezone.utc)
    message_id = f"m-{now.strftime('%Y%m%dt%H%M%Sz')}-{uuid.uuid4().hex[:12]}"
    envelope = {
        "v": 1,
        "id": message_id,                # lower-case letters, digits and dashes, 8 to 64 characters
        "from": sender,                  # a label, not authentication
        "to": to,
        "kind": "instruction",           # the only kind that starts a turn
        "correlation_id": message_id,    # the thread; replies carry the same value
        "subject": subject,
        "created_utc": utc(now),
        "expires_utc": utc(now + datetime.timedelta(minutes=ttl_minutes)),  # at most 7 days later
        "body": body,
    }
    box = root / "mail" / to
    tmp, new = box / "tmp" / f".tmp-{message_id}.json", box / "new" / f"{message_id}.json"
    tmp.write_text(json.dumps(envelope, indent=2) + "\n")
    try:
        os.link(tmp, new)  # fails rather than overwrite an existing message
    finally:
        tmp.unlink()
    return message_id


def replies_to(root, sender, recipient, message_id):
    found = {}
    for place in ("new", "closed"):
        for path in sorted((root / "mail" / sender / place).glob("r-*.json")):
            reply = read_json(path)
            if (reply and reply.get("in_reply_to") == message_id and
                    reply.get("correlation_id") == message_id and
                    reply.get("from") == recipient and reply.get("to") == sender):
                found[reply["id"]] = reply
    return sorted(found.values(), key=lambda reply: reply.get("created_utc", ""))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="sender", required=True, help="this sender's agent name (a reader in config.json)")
    parser.add_argument("--to", required=True, help="the session agent to instruct")
    parser.add_argument("--subject", default="")
    parser.add_argument("--body", required=True)
    parser.add_argument("--ttl-minutes", type=int, default=60)
    parser.add_argument("--wait-seconds", type=int, default=300)
    args = parser.parse_args()

    where = os.environ.get("CLAUDE_MAILBOX_ROOT")
    if not where:
        sys.exit("set CLAUDE_MAILBOX_ROOT to the mailbox root")
    root = Path(where).expanduser().resolve()
    agents = (read_json(root / "config.json") or {}).get("agents") or {}
    if agents.get(args.to) != "session":
        sys.exit(f"{args.to!r} is not a session agent in {root / 'config.json'}")
    if args.sender not in agents:
        sys.exit(f"{args.sender!r} is not an agent in {root / 'config.json'}")

    message_id = send(root, args.sender, args.to, args.subject, args.body, args.ttl_minutes)
    print(f"sent {message_id}; queued is not delivered")

    last, deadline = None, time.monotonic() + args.wait_seconds
    while time.monotonic() < deadline:
        # The state record appears when the receiving session (or any helper command) first sees the file.
        record = read_json(root / "state" / f"{message_id}.json")
        state = record["state"] if record else "queued (not yet seen by the recipient)"
        if state != last:
            print(f"state: {state}")
            last = state
        if state in DONE:
            break
        time.sleep(2)

    replies = replies_to(root, args.sender, args.to, message_id)
    for reply in replies:
        print(f"--- {reply.get('reply_state')} ({reply['id']}) ---\n{reply.get('body', '')}")
    states = {reply.get("reply_state") for reply in replies}
    if last == "completed" and {"acknowledged", "completed"} <= states:
        return 0
    if last not in DONE:
        print("wait deadline reached; delivery is still pending. Inspect status before retrying.", file=sys.stderr)
    elif last == "completed":
        print("completed state lacks both correlated reply files", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
