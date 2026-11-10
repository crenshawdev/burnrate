import { describe, expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const SESSION = 'sess-1'
const MEASURE = { context: {}, rateLimits: [{ kind: 'seven_day', percentUsed: 40, resetsAt: '2030-01-03T00:00:00Z' }], changed: ['rateLimits'] } as never
const TURN = { answer: 'done', durationMs: 1, isAborted: false, turnId: 't1', reason: 'end_turn', usage: { model: 'm', input_tokens: 1, output_tokens: 2, cache_read_input_tokens: 3, cache_creation_input_tokens: 4 } } as never

// answers every op the mod calls and records what ui.status was asked to draw
const world = (on: On) => {
  const drawn: (string | undefined)[] = []
  mock.store(on)
  mock.clock(on, { now: Date.parse('2030-01-01T00:00:00Z') })
  on('session.id', () => ({ value: SESSION }) as never)
  on('session.usage', () => ({ value: { rateLimits: [] } }) as never)
  on('ui.status', (_$, e) => { drawn.push(e.text); return { value: undefined } as never })
  on('session.measure', (_$, e) => ({ changed: e.changed }) as never)
  on('turn.complete', (_$, e) => ({ text: e.answer }) as never)
  return drawn
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
