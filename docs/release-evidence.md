# Release preparation evidence

Date: 2026-10-02 UTC. Status: **offline validated; one fresh isolated real-session B trial passed**.

The real-session trial tested commit `8b87315f65b45dc3624c97114c19d270e9d131e1`.
The later evidence/documentation commit preserves all tested runtime hashes.
The runtime SHA-256 hashes below identify the exact tested code; release
metadata/documentation commits leave them unchanged.

## Local checks

| Check | Result | Scope |
| --- | --- | --- |
| Filesystem helper suite | 48 tests passed | Disposable roots; configuration, ownership, crash recovery, fault injection, bounded records |
| Session report suite | 16 tests passed | No Claude process; every init checked, later drift retained, private text excluded |
| Sender examples | 3 tests passed | Actual file helper with a scripted receiver; Python and shell correlation, timeout |
| Plugin manifest/hooks validation | Passed | Static Claude Code inspection |
| Plugin hook suite | 50 passed, zero failed | Claude Code's mocked engine; no real Claude session or model turn |
| Shell syntax | Passed | Sender example |
| Candidate and existing Git history audit | No matches in the inspected patterns | Private paths/project names, credential literals, upload IDs; all intended files and prior reachable blobs |
| Architecture SVG | Valid XML, no external references | Neutral diagram; no screenshot or embedded private data |

Python target: 3.14.7 on macOS. Claude Code target: **2.1.287**.
The mocked hook suite ran with an existing account configuration where mods are
enabled. The default configuration reported the hooks rollout disabled; availability
must be checked for the account used in an actual trial. No new login, credentials,
persistent setting change, or install was performed. The approved live receiver
was a fresh disposable session with only the package reply tool.

## Runtime hashes

| File | SHA-256 |
| --- | --- |
| `bridge.py` | `4df5f85b88b10d13888065598e03f6a3020d208057898567facebdaef475f984` |
| `mod/hooks/register.ts` | `713bf539bcc4aa1185c59d26a30a93c4319c19713880d783f1efe51b28829723` |
| `validation/session_report.py` | `fdc49f475a5823d252db9469eb5ea0ea0322c9c9a01908a0c5098cc4d30359a5` |

## Fresh generic real-session proof

The approved trial ran on 2026-10-02 UTC with an existing first-party Claude Max
login and no API-key source. Its separate temporary mailbox used generic names
`worker` and `bot`, limits of six addressed sequences per rolling hour and forty
per rolling day, and an empty temporary workspace. The unrelated user plugin was
disabled only for that child; no persistent setting or existing session changed.

- Message `m-20261002t192951z-181ecb9a662a` progressed queued → claimed → submitted
  → acknowledged → completed. No forced or stray start occurred.
- Exactly one acknowledgement and one completion file addressed `bot`, from
  `worker`, with matching `in_reply_to` and `correlation_id`.
- Two init reports passed the same route/tool/plugin/MCP-provenance checks. One
  non-query initialization result reported zero turns, API duration, tokens, cost,
  and model usage; one actual mail sequence followed. No other tool or foreign ID.
- The child exited with status 0 after stdin closed, in 13.4 seconds. Stream ended
  at EOF, signals were empty, and no child-group process remained.
- Final mailbox: queued 0, in-flight 0, presence offline, one charged sequence.

The machine-readable [proof summary](real-session-proof.json) contains selected
non-secret facts only, not prompts, reply bodies, account identifiers, private
paths, secret tool URLs, runtime mailbox files, or raw stream. An initial controller
adaptation retained the predecessor's expected plugin path; it stopped before
sending mail with zero model work and clean exit. The comparison was corrected,
then this trial passed with the package unchanged.

## Limits of this evidence

- The observed proof covers one fresh isolated headless session on the tested runtime. It does not prove the normal interactive quickstart or attaching an already-open conversation.
- A private predecessor observed idle delivery and correlated acknowledgement and
  completion. Its isolation observer missed rechecking later init reports; no drift
  was observed, but that gap limits its evidence. The public offline validator now
  checks every report, with regression tests for later scope changes.
- The generic package's no-allow permission trial, real pause/expiry withdrawal,
  and conversation changes with queued mail remain unverified.
- Sender example tests use a scripted receiver; they do not prove Claude delivery.
- CI covers the Python suites on macOS/Linux and Python 3.10/3.13. Hosted
  results are separate from this local evidence: see the repository's tests
  workflow. The real Claude trial was macOS-only; native Windows is unsupported,
  and WSL and newer CLI versions are unverified.
- Pattern scanning reduces accidental disclosure risk; it is not a guarantee of
  arbitrary secret detection. No runtime mailbox, transcript, raw log, credentials,
  game data, private review history, or generated CLI declarations are exported.

Public creation, push, live-demo recording, and announcement were not performed
by this test. Publication must use a fresh privacy/history audit of the exact
committed archive and verified destination, and accurately state this proof's
scope. No tweet was sent.
