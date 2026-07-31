---
name: burnrate
description: Build and open a local dashboard of the user's own Claude Code token usage, or answer a quick usage question from the last run. Triggered by /burnrate, optionally with a date-window word or a question.
argument-hint: '[7d|14d|30d|90d|all] [rebuild] [no-archive] | <question>'
disable-model-invocation: true
---

# burnrate

A bare run parses the whole transcript tree and launches a browser, so only act
on an explicit `/burnrate` invocation.

## The helper

Every action goes through one script. Never write inline Python against
`dashboard_data.json`, and never edit, copy or vendor `burnrate.py` -- it stays
a standalone tool that runs on its own as `python3 burnrate.py`.

```
python3 "${CLAUDE_PROJECT_DIR:-.}/.claude/skills/burnrate/scripts/burnrate_skill.py" ...
```

If that path does not exist, resolve the repository root once with
`git rev-parse --show-toplevel` and use
`<root>/.claude/skills/burnrate/scripts/burnrate_skill.py` for every call in the
turn.

## Routing

Look at the words the user passed:

- No words, or every word is one of `7d`, `14d`, `30d`, `90d`, `all`,
  `rebuild`, `no-archive` (also accepted: `7`, `14`, `30`, `90`) -- call
  `run` with those words verbatim:
  `python3 <helper> run 7d`. The helper maps them onto the tool's flags and
  writes the report under the platform cache directory.
- Anything else is a question. Use the answer flow below; do not guess a flag
  from it, and do not fall back to a bare `run` -- the helper rejects an
  unrecognized word on purpose.

## Reporting a run

`run` streams the tool's own output. Report back the `wrote:` path and the
one-line totals (range, billed-equiv, blocks, projects). Do not paste the whole
output.

## Answering a question

<!-- filled in with the ask flow -->
