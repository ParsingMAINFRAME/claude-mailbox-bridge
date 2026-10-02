# Release preparation evidence

Date: 2026-10-02 UTC. Status: **offline validated; live release proof pending**.

This file applies to the candidate commit that contains it. The local handoff
reports that commit ID. The tested runtime SHA-256 hashes are listed below.

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
persistent setting change, install, or live session activation was performed.

## Runtime hashes

| File | SHA-256 |
| --- | --- |
| `bridge.py` | `4df5f85b88b10d13888065598e03f6a3020d208057898567facebdaef475f984` |
| `mod/hooks/register.ts` | `713bf539bcc4aa1185c59d26a30a93c4319c19713880d783f1efe51b28829723` |
| `validation/session_report.py` | `fdc49f475a5823d252db9469eb5ea0ea0322c9c9a01908a0c5098cc4d30359a5` |

## Limits of this evidence

- This exact generic package has not been tested in a real Claude session.
- A private predecessor observed idle delivery and correlated acknowledgement and
  completion. Its isolation observer missed rechecking later init reports; no drift
  was observed, but that gap limits its evidence. The public offline validator now
  checks every report, with regression tests for later scope changes.
- The generic package's actual quickstart, permission behavior, real pause/expiry
  withdrawal, and conversation changes with queued mail remain unverified.
- Sender example tests use a scripted receiver; they do not prove Claude delivery.
- CI is prepared for macOS/Linux and Python 3.10/3.13; no hosted CI result is claimed.
  Native Windows is unsupported; WSL and newer CLI versions are unverified.
- Pattern scanning reduces accidental disclosure risk; it is not a guarantee of
  arbitrary secret detection. No runtime mailbox, transcript, raw log, credentials,
  game data, private review history, or generated CLI declarations are exported.

Public creation, push, live-demo recording, and announcement remain pending the
real-session release checklist. Publication must use a fresh privacy/history audit
of the exact committed archive and verified destination. No tweet was sent.
