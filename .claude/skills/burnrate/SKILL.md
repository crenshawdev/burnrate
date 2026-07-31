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

Call `ask`, then answer from the JSON it prints. Never hand-index
`dashboard_data.json`, and never write inline Python to compute a total: the
daily rows are twelve unlabeled positions, and a wrong index returns a
plausible wrong number with no failure signal.

```
python3 <helper> ask --by day
python3 <helper> ask --day yesterday
python3 <helper> ask --by project --last 7
python3 <helper> ask --by command,model --project web
```

Pick the `--by` / `--day` / `--since` / `--until` / `--project` combination the
question implies; `python3 <helper> ask --help` lists them all. `ask` reuses a
payload under 15 minutes old, rebuilds it otherwise, and never opens a browser.

For a question about 5h rate-limit windows ("how much is left in this block",
"when does it reset"), use `blocks` instead: `python3 <helper> blocks --last 1`.
Blocks are account-wide, so no project filter applies to them.

Then answer in one or two sentences: the figure with thousands separators, the
unit ("billed-equivalent tokens"), and which day or range it covers. Say
`reused: false` runs took a rebuild only if the user is waiting on why it was
slow. If `rows` is empty, say there was no activity in that window rather than
reporting zero as a measurement.
