# Real-session release checklist — prepared, not run

A scripted receiver and a mocked engine cannot prove delivery to Claude. Complete
this checklist on the exact release commit before claiming a working public demo.
Record the commit, CLI version, message IDs, final state, reply correlations, and
owned-session cleanup. Keep transcripts, credentials, account details, and secret
localhost tool URLs out of evidence and out of Git.

This checklist authorizes nothing by itself. The session owner must approve the
recipient, existing account, disposable workspace, polling/turn limits, reply tool,
and temporary localhost tool transport. Do not convert or resume an existing
worker for this test. Never type into an occupied terminal pane.

## Prepare a disposable receiver

Use a spare terminal and the installed Claude binary directly. Replace the path
below; shell wrappers may run unrelated helpers or change trust settings. Use the
owner's existing login through its secure flow. Do not add credentials, bypass
permissions, install plugins globally, or change persistent settings.

From the release checkout:

```sh
REPO=$(pwd -P)
B="$REPO/bridge.py"
CLAUDE_BIN=/absolute/path/to/claude
TOOL=mcp__session-mailbox__mailbox_reply
export CLAUDE_MAILBOX_ROOT=$(mktemp -d)
WORK=$(mktemp -d)
python3 "$B" init --session trial --reader bot
python3 "$B" status --agent trial
```

Record the private root and workspace paths locally; do not show them in the public
demo. Initial queued and in-flight counts must be zero. Freeze the plugin files:
`--plugin-dir` can hot-reload edits. Stop if the owner cannot verify the intended
account, if an authentication/trust prompt appears, or if another receiver holds
this mailbox.

## A: observe the reply permission without an allow rule

In the spare receiver terminal, enter the empty workspace and launch:

```sh
cd "$WORK"
CLAUDE_MAILBOX_AGENT=trial "$CLAUDE_BIN" \
  --plugin-dir "$REPO/mod" \
  --setting-sources project,local \
  --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
  --tools "$TOOL"
```

Wait for the plugin to report active for `trial`. If mods are unavailable for this
account's rollout, stop and report that exact blocker. Do not substitute an API key
or change global settings. The isolated test loads no user settings; it is not a
launch recipe for a real asset worker, whose existing tools and rules must stay.

From a separate sender terminal with the same root:

```sh
python3 "$B" send --from bot --to trial --subject "permission trial A" \
  --ttl-minutes 5 \
  --body "Disposable mailbox test. Do no other work. Acknowledge, then complete with body pong. If the reply tool refuses permission, stop and report that refusal; do not write by another route."
python3 "$B" status --agent trial
python3 "$B" inbox --agent bot
```

Observe only the returned ID for at most 120 seconds. Expected: a refused reply
attempt and no reply files. This is an observation of the permission decision,
not proof of every possible permission dialog. Once this owned session is idle,
exit normally. Confirm offline presence, then close only this test message with
`resolve --id <returned-id> --state blocked --note "permission trial A finished"`.
Never resend to get around a refusal.

## B: verify acknowledgement and completion with a session allow rule

Start another disposable receiver with the same command plus:

```sh
--allowedTools mcp__session-mailbox__mailbox_reply
```

Send one instruction as above, with subject `delivery trial B`. Observe only its
returned ID for at most 120 seconds. Require all of the following:

- Final state `completed`; exactly one `acknowledged` and one `completed` reply.
- Replies have `from: trial`, `to: bot`, and both `in_reply_to` and `correlation_id`
  equal to the returned message ID.
- No forced or stray start; queue and in-flight counts return to zero.
- No extra work, foreign message, duplicate model sequence, or alternate writer.
- Pause this recipient, then exit this owned session normally and verify offline
  presence. Do not stop another process or listener by name or port.

If the deadline expires, pause new claims and inspect status. A running turn is
not stopped by pause. Do not inject text, retry the instruction, or claim success.
Let the harmless owned sequence finish or exit normally at an idle point. Resolve
only the owned test message once no session can still reply to it.

## Headless startup and every-turn validation

An open but empty `stream-json` stdin does **not** necessarily emit `system/init`.
An `initialize` control acknowledgement also does not prove plugin readiness. A
bounded headless controller must keep stdin open, send an initialize request and
then this non-query user frame before waiting for the session report:

```json
{"type":"user","message":{"role":"user","content":"Initialization marker only. Wait for an addressed mailbox delivery."},"parent_tool_use_id":null,"shouldQuery":false,"isSynthetic":true,"client_composed":true}
```

Require exactly one zero-work result (zero API duration, turns, token use, cost,
and empty model usage), then verify the session report before sending mail. The
marker is retained as context for the first querying turn. Do not use `--tools ""`:
it can suppress the custom reply tool itself. Use the exact reply-tool selector.

**Recheck every later `system/init`, including the mail turn's own report.** The
pre-mail report alone cannot prove later scope. The offline reducer below checks
all reports, retains any violation, and outputs selected facts without prompt,
reply body, account details, model text, plugin paths, or raw stream:

```sh
# Feed the live stream directly from an explicitly authorized, bounded controller.
# This validator does not launch a receiver, send mail, or verify reply files.
python3 validation/session_report.py --plugin-path "$REPO/mod" \
  --message-id <returned-id> --trial B
```

For the isolated login-route test, it requires only the reply tool (plus native
ToolSearch), exactly the inline package plugin and recognized built-ins with
built-in provenance, and exactly the connected plugin-owned localhost server.
Configured lookalikes and external servers fail. The validator is stricter than
normal worker operation and must not be used to remove a worker's normal tools.

The controller must independently verify the existing login route, read only
allowlisted auth-status fields, enforce a finite child deadline, close stdin,
drain the reducer, and clean up only its own process group. Do not save the raw
stream or count `stream-validated` as filesystem proof: inspect the correlated
reply files separately. This package provides the reducer, not an automated live
session controller.

## Remaining engine behavior

C: pause/expiry between claim and turn start. D: `/clear`, `/resume`, or `/branch`
with a queued prompt. Both remain untested in a real session. Test them only in
separate approved disposable sessions. Abort may fail and a forced/stray start
may run; never promise immediate cancellation. Do not test these races in a live
project or use a public animation as evidence of real behavior.

After an actual trial, record its limitations in `release-evidence.md`. A public
demo should follow [demo-shotlist.md](demo-shotlist.md) using only disposable data.
