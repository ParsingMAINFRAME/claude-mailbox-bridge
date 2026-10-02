// Mocked-world tests: the engine's own test kit loads the mod, and everything beneath it
// (files, the helper process, the clock, the prompt queue) is a stand-in defined here.
// They prove the mod's own logic, not how a real session behaves.
import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const TOOL = 'mcp__session-mailbox__mailbox_reply'

type World = {
  mail: Record<string, string[]>
  runs: string[][]
  stdin: string[]
  lists: string[]
  prompts: string[]
  origins: unknown[]
  aborts: string[]
  sessionId: string
  isPaused: boolean
  isDropping: boolean
  isConflict: boolean
  canAbort: boolean
  gate: Record<string, unknown>
  decision: 'allow' | 'ask' | 'deny' | 'throw'
  claimDelayMs: number
  hold: boolean
  release: () => void
  checks: string[][]
  argv: string[][]
  check: Record<string, unknown> | undefined
  stats: Record<string, Record<string, unknown> | null>
  statted: string[]
  tools: string[]
  logs: string[]
}

const ROOT = '/box'

function world(on: On, agent: string | undefined, clock?: { sleep: (ms: number) => Promise<void> }, root: string | null = ROOT): World {
  const w: World = {
    mail: {}, runs: [], stdin: [], lists: [], prompts: [], origins: [], aborts: [], sessionId: 'session-1',
    isPaused: false, isDropping: false, isConflict: false, canAbort: true, gate: { ok: true, start: true },
    decision: 'allow', claimDelayMs: 0, hold: false, release: () => undefined,
    checks: [], argv: [], check: undefined, stats: {}, statted: [], tools: [], logs: [],
  }
  mock.env(on, { ...(agent === undefined ? {} : { CLAUDE_MAILBOX_AGENT: agent }), ...(root === null ? {} : { CLAUDE_MAILBOX_ROOT: root }) })
  on('session.id', () => ({ value: w.sessionId }) as never)
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('session.end', ($, e) => ({ sessionId: e.sessionId }) as never)
  on('turn.start', ($, e) => ({ turnId: e.turnId }))
  on('turn.complete', ($, e) => ({ text: e.answer }))
  on('turn.abort', ($, e) => {
    if (!w.canAbort) throw new Error('not the running turn')
    w.aborts.push(e.turnId)
    return { value: undefined } as never
  })
  on('tool.register', ($, e) => {
    w.tools.push(e.name)
    return { value: { tool: `mcp__session-mailbox__${e.name}` } } as never
  })
  on('tool.check', () => {
    if (w.decision === 'throw') throw new Error('check unavailable')
    return { decision: w.decision }
  })
  on('tool.call', () => ({ result: 'untouched by the mod' }) as never)
  on('command.register', ($, e) => ({ value: { command: e.name } }) as never)
  on('ui.log', ($, e) => {
    w.logs.push(JSON.stringify(e))
    return { value: undefined } as never
  })
  on('ui.status', () => ({ value: undefined }) as never)
  on('fs.stat', ($, e) => {
    const path = String(e.path)
    w.statted.push(path)
    const override = w.stats[path]
    if (override === null) throw new Error('no such path')
    const kind = path.endsWith('/bridge.py') ? 'file' : 'dir'
    return { value: { kind, size: 0, mtimeMs: 0, isLink: false, realPath: path, ...override } } as never
  })
  on('fs.list', ($, e) => {
    w.lists.push(String(e.path))
    const who = /\/mail\/([^/]+)\/new$/.exec(String(e.path))?.[1]
    const names = who === undefined ? (w.isPaused ? ['pause-all'] : []) : (w.mail[who] ?? [])
    return { value: names.map(name => ({ name, kind: 'file', size: 1, mtimeMs: 0, isLink: false })) } as never
  })
  on('process.run', async ($, e) => {
    // Every helper call is `python3 <the bridge.py beside the mod> --root <root> <verb> ...`.
    w.argv.push([...e.argv])
    const args = e.argv.slice(4)
    const who = args[args.indexOf('--agent') + 1] ?? ''
    const reply = (answer: Record<string, unknown>) =>
      ({ value: { exitCode: answer.ok === true ? 0 : 2, stdout: JSON.stringify(answer), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }) as never
    if (args[0] === 'check') {
      w.checks.push([...args])
      return reply(w.check ?? { ok: true, agent: who })
    }
    w.runs.push([...args])
    w.stdin.push(e.init?.stdin ?? '')
    let answer: Record<string, unknown> = { ok: true }
    if (args[0] === 'heartbeat') answer = { ok: true, conflict: w.isConflict }
    if (args[0] === 'begin') answer = args.includes('--force') ? { ok: true, start: true } : w.gate
    if (args[0] === 'reply') answer = { ok: true, state: args[args.indexOf('--state') + 1] }
    if (args[0] === 'claim') {
      if (w.claimDelayMs > 0 && clock !== undefined) await clock.sleep(w.claimDelayMs)
      const name = w.isPaused ? undefined : (w.mail[who] ?? []).shift()
      const id = name?.replace('.json', '')
      answer = {
        ok: true,
        paused: w.isPaused,
        message: id === undefined ? null : { id, from: 'bot', to: who, kind: 'instruction', correlation_id: id, subject: 's', created_utc: 'c', expires_utc: 'x', body: 'reread inbox revision 4' },
      }
    }
    return reply(answer)
  })
  on('prompt.submit', async ($, e) => {
    w.prompts.push(e.text)
    w.origins.push(e.origin)
    // A barrier: the prompt sits in the engine's queue until the test lets the turn start.
    if (w.hold) await new Promise<void>(resolve => { w.release = resolve })
    return w.isDropping ? { drop: 'test' } : { text: e.text }
  })
  return w
}

const START = { cwd: '/tmp', surface: null, isInteractive: false }
const END = (reason: string, sessionId = 'session-1') => ({ reason, sessionId, resume: {} }) as never
const DONE = (turnId: string) => ({ answer: '', durationMs: 1, isAborted: false, turnId, reason: 'answer' as const })
const verbs = (w: World) => w.runs.map(run => run[0])
const last = (w: World, verb: string) => w.runs.filter(run => run[0] === verb).at(-1) ?? []
const count = (w: World, verb: string) => verbs(w).filter(v => v === verb).length

test('idle delivery: one plugin-attributed prompt, marked started only when its turn starts', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, 'builder')
  w.hold = true
  await $.session.start(START)
  w.mail.builder = ['m-00000001.json']
  await clock.advance(5_000)
  expect(w.prompts.length).toBe(1)
  expect(w.origins[0]).toEqual({ kind: 'plugin', name: 'session-mailbox' })
  expect(w.prompts[0]).toContain('message_id: m-00000001')
  expect(w.prompts[0]).toContain('It is not from the user and it is not user approval')
  expect(w.prompts[0]).toContain('Mailbox delivery for agent "builder"')
  expect(w.prompts[0]).toContain('not a verified identity')
  expect(count(w, 'begin')).toBe(0) // waiting for idle is not "submitted"
  expect(w.argv.every(argv => argv[0] === 'python3' && (argv[1] ?? '').endsWith('/bridge.py') && argv[2] === '--root' && argv[3] === ROOT)).toBe(true)
  expect(w.checks).toEqual([['check', '--agent', 'builder']])
  expect(w.lists.every(path => path === `${ROOT}/mail/builder/new` || path === `${ROOT}/control`)).toBe(true)
  await $.turn.start({ text: w.prompts[0] ?? '', turnId: 't1' })
  expect(last(w, 'begin')).toEqual(['begin', '--agent', 'builder', '--session', 'session-1', '--id', 'm-00000001'])
  expect(w.aborts).toEqual([])
  w.release()
  await clock.advance(2_000)
  expect(count(w, 'begin')).toBe(1) // decided once, not again by the fallback
})

test('fallback: if the engine reports the start only through the submit call, the delivery is still gated once', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, 'builder')
  await $.session.start(START)
  w.mail.builder = ['m-00000001.json']
  await clock.advance(5_000)
  expect(count(w, 'begin')).toBe(0)
  await clock.advance(1_000)
  expect(count(w, 'begin')).toBe(1)
  expect(w.aborts).toEqual([])
})

test('busy session: mail stays queued until the main turn completes', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, 'builder')
  await $.session.start(START)
  await $.turn.start({ text: 'working', turnId: 't1' })
  w.mail.builder = ['m-00000001.json']
  await clock.advance(20_000)
  expect(w.prompts.length).toBe(0)
  expect(verbs(w)).not.toContain('claim')
  await $.turn.complete({ ...DONE('t2'), agentId: 'a1' }) // a subagent finishing is not idle
  await clock.advance(5_000)
  expect(w.prompts.length).toBe(0)
  await $.turn.complete(DONE('t1'))
  await clock.advance(5_000)
  expect(w.prompts.length).toBe(1)
})

test('recipient isolation: builder never lists, claims or receives reviewer mail', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, 'builder')
  await $.session.start(START)
  w.mail['reviewer'] = ['m-00000002.json']
  await clock.advance(30_000)
  expect(w.prompts.length).toBe(0)
  expect(w.lists.some(path => path.includes('reviewer'))).toBe(false)
  expect(w.runs.every(run => !run.includes('reviewer'))).toBe(true)
  expect(w.mail['reviewer']).toEqual(['m-00000002.json'])
})

test('nothing changes: only heartbeats run, and no prompt', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, 'builder')
  await $.session.start(START)
  const before = w.runs.length
  await clock.advance(120_000)
  expect(w.prompts.length).toBe(0)
  expect(new Set(verbs(w).slice(before))).toEqual(new Set(['heartbeat']))
  expect(w.runs.length - before).toBe(4)
})

test('pause before claim: no prompt, and claim attempts back off', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, 'builder')
  await $.session.start(START)
  w.isPaused = true
  w.mail.builder = ['m-00000001.json']
  await clock.advance(300_000)
  expect(w.prompts.length).toBe(0)
  expect(count(w, 'claim')).toBeGreaterThan(0)
  expect(count(w, 'claim')).toBeLessThan(10)
  w.isPaused = false
  await clock.advance(5_000)
  expect(w.prompts.length).toBe(1)
})

test('pause while the prompt waits for idle: the turn is withdrawn as it starts, then redelivered once after resume', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, 'builder')
  w.hold = true
  await $.session.start(START)
  w.mail.builder = ['m-00000001.json']
  await clock.advance(5_000) // claimed; the prompt now waits behind the barrier
  await $.turn.start({ text: 'the user typed first', turnId: 'user-turn' })
  expect(count(w, 'begin')).toBe(0) // somebody else's turn is never gated or withdrawn
  expect(w.aborts).toEqual([])
  await $.turn.complete(DONE('user-turn'))
  w.gate = { ok: true, start: false, revocable: true, reason: 'paused' } // pause lands while it waits
  await $.turn.start({ text: w.prompts[0] ?? '', turnId: 'mail-turn' })
  expect(w.aborts).toEqual(['mail-turn'])
  expect(last(w, 'release')).toEqual(['release', '--agent', 'builder', '--session', 'session-1', '--id', 'm-00000001', '--reason', 'paused'])
  expect(verbs(w).filter(v => v === 'begin').length).toBe(1)
  expect(w.runs.some(run => run.includes('--force'))).toBe(false)
  w.release()
  w.hold = false
  w.gate = { ok: true, start: true } // resumed
  w.mail.builder = ['m-00000001.json'] // the helper put it back in the queue
  await clock.advance(10_000)
  expect(w.prompts.length).toBe(2)
  await clock.advance(60_000)
  expect(w.prompts.length).toBe(2) // no duplicate beyond the one redelivery
})

test('when the engine will not withdraw the turn, it is recorded as started, not hidden', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, 'builder')
  w.hold = true
  w.canAbort = false
  await $.session.start(START)
  w.mail.builder = ['m-00000001.json']
  await clock.advance(5_000)
  w.gate = { ok: true, start: false, revocable: true, reason: 'paused' }
  await $.turn.start({ text: w.prompts[0] ?? '', turnId: 't1' })
  expect(w.aborts).toEqual([])
  expect(last(w, 'begin')).toContain('--force')
  expect(verbs(w)).not.toContain('release')
})

test('a helper that cannot answer fails closed: the turn is withdrawn and nothing is marked started', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, 'builder')
  w.hold = true
  await $.session.start(START)
  w.mail.builder = ['m-00000001.json']
  await clock.advance(5_000)
  w.gate = { ok: false, error: 'ENOSPC' }
  await $.turn.start({ text: w.prompts[0] ?? '', turnId: 't1' })
  expect(w.aborts).toEqual(['t1'])
  expect(w.runs.some(run => run.includes('--force'))).toBe(false)
})

test('a prompt the session did not take is released with a refund, never marked started', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, 'builder')
  await $.session.start(START)
  w.isDropping = true
  w.mail.builder = ['m-00000001.json']
  await clock.advance(10_000)
  expect(last(w, 'release')).toContain('--refund')
  expect(verbs(w)).not.toContain('begin')
})

test('one delivery per turn: a second message waits for the first turn to end', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, 'builder')
  await $.session.start(START)
  w.mail.builder = ['m-00000001.json', 'm-00000002.json']
  await clock.advance(5_000)
  await $.turn.start({ text: w.prompts[0] ?? '', turnId: 't1' })
  await clock.advance(60_000)
  expect(w.prompts.length).toBe(1)
  await $.turn.complete(DONE('t1'))
  await clock.advance(5_000)
  expect(w.prompts.length).toBe(2)
})

test('a slow helper cannot overlap claims or deliveries across ticks', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, 'builder', clock)
  w.claimDelayMs = 22_000
  w.hold = true
  await $.session.start(START)
  w.mail.builder = ['m-00000001.json', 'm-00000002.json']
  await clock.advance(20_000)
  expect(count(w, 'claim')).toBe(1)
  await clock.advance(60_000)
  expect(count(w, 'claim')).toBe(1) // the first delivery is still waiting for idle
  expect(w.prompts.length).toBe(1)
})

test('heartbeats name the delivery still waiting, and the startup heartbeat names none', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, 'builder')
  w.hold = true
  await $.session.start(START)
  expect(w.runs[0]).toEqual(['heartbeat', '--agent', 'builder', '--session', 'session-1', '--busy', '0'])
  w.mail.builder = ['m-00000001.json']
  await clock.advance(35_000)
  expect(last(w, 'heartbeat').slice(-2)).toEqual(['--pending', 'm-00000001'])
})

for (const reason of ['clear', 'resume']) {
  test(`/${reason}: the listener survives under the new session id and lets go of the old one`, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on, 'builder')
    w.hold = true
    await $.session.start(START)
    w.mail.builder = ['m-00000001.json']
    await clock.advance(5_000) // a delivery is waiting when the conversation changes
    await $.turn.start({ text: 'user work', turnId: 't0' }) // and a turn was running
    await $.session.end(END(reason))
    expect(last(w, 'offline')).toEqual(['offline', '--agent', 'builder', '--session', 'session-1'])
    w.sessionId = 'session-2' // no session.start follows
    w.hold = false
    w.mail.builder = ['m-00000001.json'] // the helper re-queued what the old id had not started
    await clock.advance(5_000)
    expect(last(w, 'heartbeat').slice(0, 5)).toEqual(['heartbeat', '--agent', 'builder', '--session', 'session-2'])
    expect(last(w, 'claim')).toEqual(['claim', '--agent', 'builder', '--session', 'session-2'])
    expect(w.prompts.length).toBe(2)
    await $.turn.start({ text: w.prompts[1] ?? '', turnId: 't1' })
    expect(last(w, 'begin')).toContain('session-2')
    // The old prompt firing late under the new identity is not gated as a current delivery.
    expect(count(w, 'begin')).toBe(1)
    const ok = await $.tool.call({ tool: TOOL, message_id: 'm-00000001', state: 'acknowledged', body: 'a' } as never)
    expect(String((ok as { result?: unknown }).result)).toContain('recorded')
    expect(last(w, 'reply')).toContain('session-2')
  })
}

test('a session id that changes with no end event is picked up on the next tick', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, 'builder')
  await $.session.start(START)
  w.sessionId = 'session-9'
  await clock.advance(5_000)
  expect(last(w, 'offline')).toEqual(['offline', '--agent', 'builder', '--session', 'session-1'])
  expect(last(w, 'heartbeat')).toContain('session-9')
})

for (const reason of ['logout', 'prompt_input_exit', 'other']) {
  test(`real shutdown (${reason}): presence is dropped and no timer or callback runs afterwards`, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on, 'builder')
    await $.session.start(START)
    await $.session.end(END(reason))
    expect(w.runs.at(-1)).toEqual(['offline', '--agent', 'builder', '--session', 'session-1'])
    const after = w.runs.length
    w.mail.builder = ['m-00000001.json']
    await clock.advance(120_000)
    expect(w.runs.length).toBe(after)
    expect(w.prompts.length).toBe(0)
    const refused = await $.tool.call({ tool: TOOL, message_id: 'm-00000001', state: 'completed', body: 'x' } as never)
    expect(String((refused as { result?: unknown }).result)).toContain('inactive')
  })
}

test('a reload starts one listener, not two', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, 'builder')
  await $.session.start(START)
  await $.session.start(START) // what a hot reload raises
  const before = w.runs.length
  await clock.advance(60_000)
  expect(w.runs.length - before).toBe(2) // one heartbeat per 30 s, not doubled
})

test('replies are written only on an explicit allow, under this session and agent', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, 'builder')
  await $.session.start(START)
  const call = { tool: TOOL, message_id: 'm-00000001', state: 'acknowledged', body: 'ack rev 4' } as never
  for (const decision of ['ask', 'deny', 'throw'] as const) {
    w.decision = decision
    const refused = await $.tool.call(call)
    expect(String((refused as { result?: unknown }).result)).toContain('nothing was written')
    expect(verbs(w)).not.toContain('reply')
  }
  w.decision = 'allow'
  const answer = await $.tool.call(call)
  expect(String((answer as { result?: unknown }).result)).toContain('recorded "acknowledged"')
  expect(last(w, 'reply')).toEqual(['reply', '--agent', 'builder', '--session', 'session-1', '--id', 'm-00000001', '--state', 'acknowledged'])
  expect(w.stdin.at(-1)).toBe('ack rev 4')
  const bad = await $.tool.call({ tool: TOOL, message_id: '../../etc/passwd', state: 'completed', body: 'x' } as never)
  expect(String((bad as { result?: unknown }).result)).toContain('refused')
  expect(count(w, 'reply')).toBe(1)
  await clock.advance(30_000)
  expect(w.prompts.length).toBe(0) // a reply never starts a turn
})

test('only the exact reply tool is handled; other tools pass through untouched', async ($, on) => {
  mock.clock(on)
  const w = world(on, 'builder')
  await $.session.start(START)
  const before = w.runs.length
  for (const tool of ['Read', 'mcp__session-mailbox__mailbox_reply_all', 'mcp__other__mailbox_reply']) {
    const answer = await $.tool.call({ tool, file_path: 'a.md', message_id: 'm-00000001', state: 'completed', body: 'x' } as never)
    expect((answer as { result?: unknown }).result).toBe('untouched by the mod')
  }
  expect(w.runs.length).toBe(before)
})

test('another live session holding the mailbox keeps this one inactive', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, 'builder')
  w.isConflict = true
  await $.session.start(START)
  w.mail.builder = ['m-00000001.json']
  await clock.advance(60_000)
  expect(w.prompts.length).toBe(0)
  expect(verbs(w)).not.toContain('claim')
})

test('no agent variable: completely inert, even with a root set', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, undefined)
  await $.session.start(START)
  w.mail.builder = ['m-00000001.json']
  await clock.advance(120_000)
  await $.turn.start({ text: 'message_id: m-00000001', turnId: 't1' })
  expect([w.prompts.length, w.runs.length, w.checks.length, w.argv.length, w.statted.length, w.lists.length, w.tools.length, w.aborts.length]).toEqual([0, 0, 0, 0, 0, 0, 0, 0])
  const refused = await $.tool.call({ tool: TOOL, message_id: 'm-00000001', state: 'completed', body: 'x' } as never)
  expect(String((refused as { result?: unknown }).result)).toContain('inactive')
  expect(w.argv.length).toBe(0)
})

for (const name of ['', '../builder', 'Builder', 'builder/x', 'a b']) {
  test(`agent name ${JSON.stringify(name)}: refused before anything is read or run`, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on, name)
    await $.session.start(START)
    await clock.advance(60_000)
    expect([w.prompts.length, w.argv.length, w.statted.length, w.lists.length, w.tools.length]).toEqual([0, 0, 0, 0, 0])
  })
}

for (const root of [null, '', 'relative/box', '~/box']) {
  test(`root ${JSON.stringify(root)}: inactive, the helper is never run`, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on, 'builder', undefined, root)
    await $.session.start(START)
    w.mail.builder = ['m-00000001.json']
    await clock.advance(60_000)
    expect([w.prompts.length, w.argv.length, w.lists.length, w.tools.length]).toEqual([0, 0, 0, 0])
    expect(w.logs.some(line => line.includes('inactive'))).toBe(true)
  })
}

test('a root that is missing or not a directory leaves the session inactive', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, 'builder')
  w.stats[ROOT] = null
  await $.session.start(START)
  w.stats[ROOT] = { kind: 'file' }
  await $.session.start(START)
  w.mail.builder = ['m-00000001.json']
  await clock.advance(60_000)
  expect([w.prompts.length, w.argv.length, w.lists.length, w.tools.length]).toEqual([0, 0, 0, 0])
})

test('a helper that is missing, or is a link, leaves the session inactive and is never run', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, 'builder')
  await $.session.start(START)
  const script = w.argv[0]?.[1] ?? ''
  expect(script.endsWith('/bridge.py')).toBe(true)
  for (const stat of [null, { isLink: true }, { kind: 'dir' }, { realPath: '/elsewhere/bridge.py' }]) {
    w.stats[script] = stat
    await $.session.start(START) // also stops the listener the first start began
    const before = w.argv.length
    w.mail.builder = ['m-00000001.json']
    await clock.advance(60_000)
    expect(w.argv.length).toBe(before)
    expect(w.prompts.length).toBe(0)
  }
})

for (const [label, answer] of [
  ['a missing or invalid config', { ok: false, error: 'no config.json in the mailbox root; run `init` first' }],
  ['an agent the config does not list as a session', { ok: false, error: 'not a session agent in config.json' }],
  ['a helper that confirms some other agent', { ok: true, agent: 'reviewer' }],
  ['a helper that answers nothing useful', {}],
] as const) {
  test(`fail closed: ${label} leaves the session inactive`, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on, 'builder')
    w.check = answer
    await $.session.start(START)
    w.mail.builder = ['m-00000001.json']
    await clock.advance(60_000)
    expect(w.checks.length).toBe(1)
    expect([w.prompts.length, w.runs.length, w.lists.length, w.tools.length]).toEqual([0, 0, 0, 0])
    const refused = await $.tool.call({ tool: TOOL, message_id: 'm-00000001', state: 'completed', body: 'x' } as never)
    expect(String((refused as { result?: unknown }).result)).toContain('inactive')
    expect(w.runs.length).toBe(0)
  })
}

test('a root reached through a link is used by its real path, and the mailbox must sit really under it', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, 'builder')
  w.stats[ROOT] = { isLink: true, realPath: '/real/box' }
  await $.session.start(START)
  expect(w.argv.every(argv => argv[3] === '/real/box')).toBe(true)
  w.mail.builder = ['m-00000001.json']
  await clock.advance(5_000)
  expect(w.prompts.length).toBe(1)
  expect(w.lists.every(path => path.startsWith('/real/box/'))).toBe(true)
})

test('a mailbox directory that is a link is not listed or claimed from', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on, 'builder')
  w.stats[`${ROOT}/mail/builder/new`] = { realPath: '/elsewhere/new' }
  await $.session.start(START)
  w.mail.builder = ['m-00000001.json']
  await clock.advance(60_000)
  expect(w.prompts.length).toBe(0)
  expect(verbs(w)).not.toContain('claim')
  expect(w.lists.length).toBe(0)
})
