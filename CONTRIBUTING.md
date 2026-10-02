# Development

There is nothing to install: the helper is one standard-library Python file and the mod is one TypeScript hooks module that Claude Code loads as it is.

## Tests

```sh
python3 test_bridge.py        # helper on throwaway roots, including fault injection
python3 test_session_report.py # later-turn scope regressions; no Claude process
python3 test_sender_examples.py # both sender examples with a disposable scripted receiver
claude plugin validate mod    # static: what Claude Code reads from the mod, and what it would refuse
claude plugin test mod        # the mod's hooks with files, helper, clock and prompts replaced by stand-ins
```

The last two need the Claude Code CLI with function-hooks mods enabled. This release targets 2.1.287. They are static or mocked: neither starts a real Claude session or model turn. CI runs only the Python tests. If hooks are unavailable for the CLI's current account rollout, report that exact blocker; do not change global settings or install another CLI to hide it.

`claude plugin test` writes `mod/.claude-plugin/types/` and may write `mod/tsconfig.json`; both are ignored by git. To type-check by hand, point a `tsconfig.json` at `mod/hooks`, `mod/tests` and those generated declarations and run `npx -p typescript@5.6 tsc -p <that tsconfig>`.

## Rules for changes

- `bridge.py` is the only writer of mailbox files. The mod reads two directories and runs the helper; keep it that way.
- Keep sender handoff in local files. Add no standalone daemon, public listener, credential access, or persistent settings change. Claude Code provides its normal model connection and temporary localhost transport for custom tools.
- Validate every `system/init` report in an isolated headless trial. A later report must pass the same checks as the pre-mail report; an earlier violation must stay recorded.
- The state record is written first and is the commit point. Any new persistent step needs a `checkpoint("...")` label and a row in the crash matrix in `test_bridge.py` (`BOUNDARIES`).
- Anything that is not proven in a real session is described as unverified in `README.md`. The mocked tests prove the mod's logic, not the engine's behaviour.
- Hooks-module rules that `claude plugin validate` enforces: spell every `$.noun.method` call in full, pass `$` only to top-level functions in the same file, use string-literal event names, and give `$.env.get` a string-literal name.
- In `*.test.ts`, stand-ins for `$` operations (`fs.stat`, `fs.list`, `process.run`, `session.id`, `tool.register`, `command.register`, `ui.log`, `ui.status`, `turn.abort`) return `{ value: ... }`; engine events (`session.start`, `turn.start`, `turn.complete`, `tool.check`, `prompt.submit`, `tool.call`) return their plain result; register every `on(...)` stand-in before the first `$` call.

## Do not commit

A mailbox root (`config.json`, `mail/`, `state/`, `presence/`, `control/`), session transcripts, logs, or the generated `mod/.claude-plugin/types/` folder.
