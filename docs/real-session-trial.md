# Real-session trial plan (prepared, NOT run)

The automated tests cannot show how the Claude Code engine itself behaves. This plan is how to find out. Nothing in it has been run against this version; the README's "not yet verified" statements stand until it has.

Run it only if you accept what loading the mod means (README, "What activating the mod authorizes"). Use a throwaway mailbox root, a dedicated `trial` agent, fixed harmless message bodies and a spare working directory with nothing valuable in it. Each trial starts real model turns, which use your Claude usage, and counts against the `trial` agent's hourly limit (6 by default). Do not edit `mod/` while a trial session is open: `--plugin-dir` hot-reloads.

Keep as evidence only `bridge.py status` output, message ids and state histories. The `.jsonl` files hold a session's stream; read the mailbox tool lines from them and delete them afterwards.

## Setup (once)

```sh
REPO=/path/to/claude-mailbox-bridge
B=$REPO/bridge.py
export CLAUDE_MAILBOX_ROOT="${TMPDIR:-/tmp}/mailbox-trial"
OUT="${TMPDIR:-/tmp}/mailbox-trial-out"; mkdir -p "$OUT"
python3 $B init --session trial --reader bot
python3 $B status --agent trial    # expect queued 0, in_flight 0
TOOL=mcp__session-mailbox__mailbox_reply
show() { grep -o '"name":"mcp__session-mailbox__mailbox_reply"[^}]*}\|recorded \\"[a-z]*\\" for [a-z0-9-]*\|refused: [^"]*' "$1" | sort | uniq -c; }
```

## Trial A: what the permission check returns WITHOUT the allow flag

```sh
python3 $B send --from bot --to trial --subject "trial A" --ttl-minutes 10 \
  --body "Mailbox self-test. Do no other work. Acknowledge, then complete with the body 'trial A'."
(sleep 90) | CLAUDE_MAILBOX_AGENT=trial \
  claude -p --input-format stream-json --output-format stream-json --verbose \
  --plugin-dir $REPO/mod > "$OUT/A.jsonl"
show "$OUT/A.jsonl"; python3 $B status --agent trial
```

Expected: the message reaches `submitted` and stays there; the tool result says "nothing was written" (or the engine denies the call before the hook); the `bot` mailbox gains no reply. Then close it: `python3 $B resolve --id <id> --state blocked --note "trial A: reply correctly refused"`.

## Trial B: the same WITH the allow flag

```sh
python3 $B send --from bot --to trial --subject "trial B" --ttl-minutes 10 \
  --body "Mailbox self-test. Do no other work. Acknowledge, then complete with the body 'trial B'."
(sleep 90) | CLAUDE_MAILBOX_AGENT=trial \
  claude -p --input-format stream-json --output-format stream-json --verbose \
  --plugin-dir $REPO/mod --allowedTools $TOOL > "$OUT/B.jsonl"
show "$OUT/B.jsonl"; python3 $B status --agent trial; python3 $B inbox --agent bot
```

Expected: `completed`, with one `acknowledged` and one `completed` reply carrying the message id as `in_reply_to` and `correlation_id`.

## Trial C: is a withdrawal at turn start honoured?

The pause has to land between the claim and the turn's start gate, a window of a few tens of milliseconds, so a watcher drops the pause file the instant the body appears in `claimed/`. This is a test-harness shortcut for `bridge.py pause --agent trial`.

```sh
R=$CLAUDE_MAILBOX_ROOT
ls $R/mail/trial/claimed/*.json 2>/dev/null && echo "STOP: claimed/ must be empty first"
python3 $B send --from bot --to trial --subject "trial C" --ttl-minutes 10 \
  --body "Mailbox self-test. Do no other work. Acknowledge, then complete with the body 'trial C'."
( while ! ls $R/mail/trial/claimed/*.json >/dev/null 2>&1; do sleep 0.01; done; : > $R/control/pause-trial ) &
(sleep 60) | CLAUDE_MAILBOX_AGENT=trial \
  claude -p --input-format stream-json --output-format stream-json --verbose \
  --plugin-dir $REPO/mod --allowedTools $TOOL > "$OUT/C.jsonl"
wait; show "$OUT/C.jsonl"; python3 $B status --agent trial
python3 - "$R" <<'EOF'
import glob, json, sys
for p in sorted(glob.glob(sys.argv[1] + "/state/m-*.json"))[-1:]:
    r = json.load(open(p)); print(r["id"], r["state"], [(h["state"], h.get("note")) for h in r["history"]])
EOF
python3 $B resume --agent trial
```

Reading the result:

- history `claimed -> queued` with note `released: paused`, no model turn: **withdrawal honoured**.
- `submitted` with note `turn started; it could not be withdrawn`: **the engine refuses an abort at turn start**; the README's fallback wording is the real behaviour.
- `submitted` with note `a turn started with this message`: the watcher lost the race; nothing learned, run it again.
- `in_flight 1` with state `claimed` and no turn: the gate never ran (the `turn.start` hook did not see the prompt text). That is a defect to fix.

Afterwards the message is either `queued` (it will deliver on the next trial session; let trial D consume it or `resolve` it) or already `submitted`/`completed`.

## Trial D: delivery after /clear (interactive)

Terminal 1:

```sh
CLAUDE_MAILBOX_ROOT="${TMPDIR:-/tmp}/mailbox-trial" CLAUDE_MAILBOX_AGENT=trial \
  claude --plugin-dir /path/to/claude-mailbox-bridge/mod --allowedTools mcp__session-mailbox__mailbox_reply
```

Confirm `/plugin` lists `session-mailbox` and the transcript shows `active for "trial"`. Type `/clear`. Leave it idle.

Terminal 2 (with the same `CLAUDE_MAILBOX_ROOT`):

```sh
python3 $B send --from bot --to trial --subject "trial D" --ttl-minutes 10 \
  --body "Mailbox self-test. Do no other work. Acknowledge, then complete with the body 'trial D'."
sleep 45; python3 $B status --agent trial
```

Expected: within about 10 seconds terminal 1 starts a turn by itself and the message ends `completed`. `presence/trial.json` shows a session id different from the one before `/clear`. If nothing wakes, the listener did not survive `/clear`. Repeat for `/resume`. Then quit terminal 1 with `/exit` and check `status` shows `presence: offline`.

Not forced by this plan: a prompt that is already queued in the engine at the instant of `/clear`. That needs the same millisecond window as trial C and cannot be staged by hand. The helper refuses to start a delivery the session no longer owns, and that path is covered only by the mocked tests.

## Further checks worth making

- Busy wait: send while a long turn runs; expect `queued` until it ends.
- Restart recovery: kill a session that holds a `claimed` message; start another; expect a re-queue once the old heartbeat lapses (90 s).
- Rate bound: send 7 in an hour; expect 6 turns.
- Inert without the variable: start a session with `--plugin-dir` but no `CLAUDE_MAILBOX_AGENT`; expect no `active`/`inactive` line, no `/mailbox` output beyond "inactive", and no `presence/` file.

## After the trial

```sh
python3 $B fsck; python3 $B status --agent trial
pgrep -fl "plugin-dir" || echo "no session has a plugin dir loaded"
```

Record ids, final states and the trial C history line, then update the README's Status section with what was actually observed. Delete the trial root and `$OUT`.
