import { describe, expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const SESSION = 'sess-1'
const NOW = Date.parse('2030-01-01T00:00:00Z')
const DAY = 86400e3
const MEASURE = { context: {}, rateLimits: [{ kind: 'seven_day', percentUsed: 40, resetsAt: '2030-01-03T00:00:00Z' }], changed: ['rateLimits'] } as never
const TURN = { answer: 'done', durationMs: 1, isAborted: false, turnId: 't1', reason: 'end_turn', usage: { model: 'm', input_tokens: 1, output_tokens: 2, cache_read_input_tokens: 3, cache_creation_input_tokens: 4 } } as never

// answers every op the mod calls and records what ui.status was asked to draw
const world = (on: On) => {
  const drawn: (string | undefined)[] = []
  mock.store(on)
  const clock = mock.clock(on, { now: NOW })
  on('session.id', () => ({ value: SESSION }) as never)
  on('session.usage', () => ({ value: { rateLimits: [] } }) as never)
  on('ui.status', (_$, e) => { drawn.push(e.text); return { value: undefined } as never })
  on('session.measure', (_$, e) => ({ changed: e.changed }) as never)
  on('turn.complete', (_$, e) => ({ text: e.answer }) as never)
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
