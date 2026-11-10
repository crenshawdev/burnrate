import { describe, expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const SESSION = 'sess-1'
const NOW = Date.parse('2030-01-01T00:00:00Z')
const DAY = 86400e3
const MEASURE = { context: {}, rateLimits: [{ kind: 'seven_day', percentUsed: 40, resetsAt: '2030-01-03T00:00:00Z' }], changed: ['rateLimits'] } as never
const TURN = { answer: 'done', durationMs: 1, isAborted: false, turnId: 't1', reason: 'end_turn', usage: { model: 'm', input_tokens: 1, output_tokens: 2, cache_read_input_tokens: 3, cache_creation_input_tokens: 4 } } as never

// answers every op the mod calls and records what ui.status was asked to draw
const world = (on: On, entries?: Record<string, unknown> | null) => {
  const drawn: (string | undefined)[] = []
  if (entries !== null) mock.store(on, entries)
  const clock = mock.clock(on, { now: NOW })
  on('session.id', () => ({ value: SESSION }) as never)
  on('session.usage', () => ({ value: { rateLimits: [] } }) as never)
  on('ui.status', (_$, e) => { drawn.push(e.text); return { value: undefined } as never })
  on('session.measure', (_$, e) => ({ changed: e.changed }) as never)
  on('turn.complete', (_$, e) => ({ text: e.answer }) as never)
  on('session.start', (_$, e) => ({ cwd: e.cwd }) as never)
  return Object.assign(drawn, { clock })
}

describe('the switch', () => {
  test('off, no ui.status call carries text and the line is taken away', { options: { showStatusLine: false } }, async ($, on) => {
    const drawn = world(on)
    await $.session.measure(MEASURE)
    await $.turn.complete(TURN)
    expect(drawn.filter(t => t !== undefined)).toEqual([])
    expect(drawn).toEqual([undefined])
  })

  test('unset, the line shows', async ($, on) => {
    const drawn = world(on)
    await $.session.measure(MEASURE)
    await $.turn.complete(TURN)
    expect(drawn.some(t => typeof t === 'string' && t.length > 0)).toBe(true)
  })
})

// a session.measure carrying one seven_day entry, its window resetting `ms` after NOW
const week = (percentUsed: number, ms?: number) => ({
  context: {},
  rateLimits: [{ kind: 'seven_day', percentUsed, ...(ms === undefined ? {} : { resetsAt: new Date(NOW + ms).toISOString() }) }],
  changed: ['rateLimits'],
}) as never
const last = (drawn: (string | undefined)[]) => drawn[drawn.length - 1] ?? ''

describe('the week', () => {
  test('40% with 2 days left is on pace for 56%', async ($, on) => {
    const drawn = world(on)
    await $.session.measure(week(40, 2 * DAY))
    expect(last(drawn)).toContain('Week: 40% used, on pace for 56% at reset')
  })

  test('90% with 3 days left runs out, and the tick redraws it while idle', async ($, on) => {
    const drawn = world(on)
    await $.session.measure(week(90, 3 * DAY))
    expect(last(drawn)).toContain('Week: 90% used, out in 10h 40m, 2d 13h before reset')
    await drawn.clock.advance(3600e3)
    expect(last(drawn)).toContain('Week: 90% used, out in 10h 47m, 2d 12h before reset')
  })

  test('0% has no burn yet', async ($, on) => {
    const drawn = world(on)
    await $.session.measure(week(0, 3 * DAY))
    expect(last(drawn)).toContain('Week: 0% used, no burn yet')
  })

  test('a seven_day entry with no resetsAt draws no Week text', async ($, on) => {
    const drawn = world(on)
    await $.session.measure(week(40))
    expect(drawn.some(t => t?.includes('Week:'))).toBe(false)
  })
})

const turn = (input: number, cc: number, cr: number, agentId?: string) => ({
  answer: 'done', durationMs: 1, isAborted: false, turnId: 't', reason: 'end_turn', ...(agentId ? { agentId } : {}),
  usage: { model: 'm', input_tokens: input, output_tokens: 5, cache_read_input_tokens: cr, cache_creation_input_tokens: cc },
}) as never
const START = { cwd: '/x', surface: null, isInteractive: false } as never
const NO_WEEK = { context: {}, rateLimits: [], changed: ['rateLimits'] } as never

describe('the session hit rate', () => {
  test('a main turn and a subagent turn add up to 79.9%', async ($, on) => {
    const drawn = world(on)
    await $.turn.complete(turn(60, 50, 400))
    await $.turn.complete(turn(40, 50, 399, 'agent-1'))
    expect(last(drawn).endsWith('session cache hit 79.9%')).toBe(true)
  })

  test('turns ending at the same moment lose no increment', async ($, on) => {
    const drawn = world(on)
    await Promise.all([$.turn.complete(turn(60, 50, 400)), $.turn.complete(turn(40, 50, 399, 'agent-1'))])
    expect(last(drawn)).toBe('session cache hit 79.9%')
  })

  test('sums stored for this session id are carried on, other ids left out', async ($, on) => {
    const drawn = world(on, {
      [SESSION]: { input: 100, output: 9, read: 700, write: 100 },
      'sess-other': { input: 1e6, output: 0, read: 0, write: 1e6 },
    })
    await $.turn.complete(turn(0, 0, 99))
    expect(last(drawn)).toBe('session cache hit 79.9%')
  })

  test('a second session start carries on from what the first wrote', async ($, on) => {
    const drawn = world(on)
    await $.turn.complete(turn(60, 50, 400))
    await $.turn.complete(turn(40, 50, 300))
    await $.session.start(START)
    await $.turn.complete(turn(0, 0, 99))
    expect(last(drawn)).toBe('session cache hit 79.9%')
  })

  test('with no week reading the hit rate stands alone', async ($, on) => {
    const drawn = world(on)
    await $.turn.complete(turn(100, 100, 799))
    await $.session.measure(NO_WEEK)
    expect(last(drawn)).toBe('session cache hit 79.9%')
    expect(drawn.some(t => t?.includes('Week:'))).toBe(false)
  })

  test('a miss never rounds up to 100%', async ($, on) => {
    const drawn = world(on)
    await $.turn.complete(turn(1, 0, 9999))
    expect(last(drawn)).toBe('session cache hit 99.9%')
  })

  test('with no turns and no week the line is cleared', async ($, on) => {
    const drawn = world(on)
    await $.session.measure(NO_WEEK)
    expect(drawn.filter(t => t !== undefined)).toEqual([])
    expect(drawn.length).toBeGreaterThan(0)
    expect(drawn[drawn.length - 1]).toBeUndefined()
  })

  test('each turn writes the running sums to the store under the session id', async ($, on) => {
    const sets: [string, unknown][] = []
    world(on, null)
    on('store.get', () => ({ value: undefined }) as never)
    on('store.set', (_$, e) => { sets.push([(e as unknown as { key: string }).key, (e as unknown as { value: unknown }).value]); return { value: undefined } as never })
    await $.turn.complete(turn(60, 50, 400))
    await $.turn.complete(turn(40, 50, 399, 'agent-1'))
    expect(sets.length).toBe(2)
    expect(sets[1]).toEqual([SESSION, { input: 100, output: 10, read: 799, write: 100 }])
  })
})
