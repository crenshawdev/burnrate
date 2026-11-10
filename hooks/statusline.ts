import type { EngineInterface, Register } from 'claude-code'

type Counts = { input: number; output: number; read: number; write: number }
const zero = (): Counts => ({ input: 0, output: 0, read: 0, write: 0 })

// Task 1 readout, kept for the session part until Task 3 replaces it.
// The engine only follows $ into top-level functions, so the state lives at module level too.
let show = true
let cleared = false
let week: { percentUsed: number; resetsAt?: string } | undefined
let seen = false
let tick: { cancel: () => void } | undefined
const main = zero()
const agent = zero()

const WEEK_MS = 7 * 86400e3
const TICK_MS = 60_000

// minutes to "2d 13h" / "10h 40m" / "40m", the same cut as the old statusline-burndown.mjs
const fmt = (ms: number) => {
  const m = Math.round(ms / 60000), d = Math.floor(m / 1440), h = Math.floor(m % 1440 / 60), mm = m % 60
  return d ? `${d}d ${h}h` : h ? `${h}h ${mm}m` : `${mm}m`
}

// the week avg branch of that script: usage so far against time since the window opened
function weekPart(now: number): string | undefined {
  if (!week?.resetsAt) return undefined
  const used = week.percentUsed, reset = Date.parse(week.resetsAt)
  if (!Number.isFinite(reset)) return undefined
  const head = `Week: ${used.toFixed(0)}% used`
  const elapsed = now - (reset - WEEK_MS)
  if (elapsed <= 0 || used <= 0) return `${head}, no burn yet`
  const rate = used / elapsed, left = reset - now
  const toFull = (100 - used) / rate
  if (toFull < left) return `${head}, out in ${fmt(toFull)}, ${fmt(left - toFull)} before reset`
  return `${head}, on pace for ${Math.min(999, used + rate * left).toFixed(0)}% at reset`
}

async function draw($: EngineInterface) {
  if (!show) {
    // a reload does not drop the old line, so an off switch takes it away once
    if (!cleared) { cleared = true; $.ui.status(undefined) }
    return
  }
  // a reload resets this module, and session.measure may not fire again for a while
  if (!seen) { seen = true; week = (await $.session.usage()).rateLimits.find(r => r.kind === 'seven_day') }
  // a reload cancels the old environment's waits, so the first draw of a new one starts the tick again
  if (!tick) tick = $.clock.every(TICK_MS, () => { draw($).catch(() => {}) })
  const now = await $.clock.now()
  const id = await $.session.id()
  const c = (n: Counts) => `in ${n.input} out ${n.output} read ${n.read} write ${n.write}`
  const parts = [weekPart(now), `burnrate probe id ${id} | main ${c(main)} | agent ${c(agent)}`]
  $.ui.status(parts.filter(Boolean).join(' | '))
}

export const register: Register = (on, options) => {
  show = options.showStatusLine !== false
  // a reload may hand this module over with the old tick gone, so start clean
  tick?.cancel(); tick = undefined; seen = false

  on('session.measure', async ($, e, next) => {
    seen = true
    week = e.rateLimits.find(r => r.kind === 'seven_day')
    await draw($).catch(() => {})
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const u = e.usage
    if (u) {
      const into = e.agentId ? agent : main
      into.input += u.input_tokens
      into.output += u.output_tokens
      into.read += u.cache_read_input_tokens
      into.write += u.cache_creation_input_tokens
    }
    await draw($).catch(() => {})
    return next(e)
  })
}
