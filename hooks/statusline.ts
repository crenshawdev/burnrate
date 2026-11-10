import type { EngineInterface, Register } from 'claude-code'

type Counts = { input: number; output: number; read: number; write: number }
const zero = (): Counts => ({ input: 0, output: 0, read: 0, write: 0 })

// Task 1 readout: the raw inputs on screen so a live session can show what the engine hands a mod.
// The engine only follows $ into top-level functions, so the state lives at module level too.
let show = true
let cleared = false
let week: { percentUsed?: number; resetsAt?: string } | undefined
const main = zero()
const agent = zero()

async function draw($: EngineInterface) {
  if (!show) {
    // a reload does not drop the old line, so an off switch takes it away once
    if (!cleared) { cleared = true; $.ui.status(undefined) }
    return
  }
  const id = await $.session.id()
  const w = week ? `${week.percentUsed} ${week.resetsAt}` : 'none'
  const c = (n: Counts) => `in ${n.input} out ${n.output} read ${n.read} write ${n.write}`
  $.ui.status(`burnrate probe | seven_day ${w} | id ${id} | main ${c(main)} | agent ${c(agent)}`)
}

export const register: Register = (on, options) => {
  show = options.showStatusLine !== false

  on('session.measure', async ($, e, next) => {
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
