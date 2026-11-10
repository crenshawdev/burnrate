import type { EngineInterface, Register } from 'claude-code'

type Counts = { input: number; output: number; read: number; write: number }
const zero = (): Counts => ({ input: 0, output: 0, read: 0, write: 0 })
const KEYS = ['input', 'output', 'read', 'write'] as const

// The engine only follows $ into top-level functions, so the state lives at module level too.
let show = true
let cleared = false
let week: { percentUsed: number; resetsAt?: string } | undefined
let seen = false
let tick: { cancel: () => void } | undefined
// per session id, each a promise of the live sums: the store is read once, then every turn adds in memory
const sums = new Map<string, Promise<Counts>>()

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

// store values and usage figures are outside data: anything but a finite count reads as 0
const num = (x: unknown) => (typeof x === 'number' && Number.isFinite(x) && x > 0 ? x : 0)

async function load($: EngineInterface, id: string): Promise<Counts> {
  const c = zero()
  const v = await $.store.get(id).catch(() => undefined)
  if (v && typeof v === 'object') for (const k of KEYS) c[k] = num((v as Record<string, unknown>)[k])
  return c
}

const sumsFor = ($: EngineInterface, id: string) => {
  let p = sums.get(id)
  if (!p) sums.set(id, p = load($, id))
  return p
}

// no await between the add and its write, so parallel subagents ending together lose nothing
async function count($: EngineInterface, usage: Record<string, unknown>) {
  const id = await $.session.id()
  const c = await sumsFor($, id)
  c.input += num(usage.input_tokens)
  c.output += num(usage.output_tokens)
  c.read += num(usage.cache_read_input_tokens)
  c.write += num(usage.cache_creation_input_tokens)
  await $.store.set(id, { ...c })
}

// same formula as the copies in burnrate.py (CLI `cache   :` line, viewer hitPct), floored in integers
const hitPart = (c: Counts) => {
  const total = c.input + c.write + c.read
  if (total <= 0) return undefined
  const t = Math.floor(c.read * 1000 / total)
  return `session cache hit ${Math.floor(t / 10)}.${t % 10}%`
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
  const parts = [weekPart(now), hitPart(await sumsFor($, await $.session.id()))].filter(Boolean)
  $.ui.status(parts.length ? parts.join(' | ') : undefined)
}

export const register: Register = (on, options) => {
  show = options.showStatusLine !== false
  // a reload may hand this module over with the old tick gone, so start clean
  tick?.cancel(); tick = undefined; seen = false; cleared = false; sums.clear()

  // a plugin reload fires neither of the hooks below, so this is what clears or draws the line at once
  on('session.start', async ($, e, next) => {
    await draw($).catch(() => {})
    return next(e)
  })

  on('session.measure', async ($, e, next) => {
    seen = true
    week = e.rateLimits.find(r => r.kind === 'seven_day')
    await draw($).catch(() => {})
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    // subagent turns carry agentId and count too
    if (e.usage) await count($, e.usage as Record<string, unknown>).catch(() => {})
    await draw($).catch(() => {})
    return next(e)
  })
}
