# burnrate

A single-file, on-demand dashboard of your own Claude Code token usage.

Run it and one self-contained `dashboard.html` opens in your browser: daily burn
by project, per-model and per-command breakdowns, 5-hour rate-limit blocks, and
per-session summaries. Date-range and project filters run client-side, so the
report works offline and can be reopened without re-running anything.

It reads local files and writes local files. No network calls, no telemetry, no
account required.

## Install

Copy `burnrate.py` anywhere and run it:

```sh
python3 burnrate.py
```

That's the whole install. Python 3.8 or newer, standard library only.

`zstandard` is optional and needed solely to read `.zst` archives. Without it
the tool still runs against your live transcripts and says so in the report
subtitle instead of failing.

## What it reads

Your Claude Code transcript tree, resolved in this order:

1. `--root DIR`
2. `$CLAUDE_PROJECTS`
3. `$CLAUDE_CONFIG_DIR/projects`
4. `~/.claude/projects`

`$CLAUDE_PROJECTS` is burnrate's own variable. `$CLAUDE_CONFIG_DIR` is a Claude
Code convention that burnrate honors.

Inside that tree it parses `<project-dir>/<session>.jsonl`, including subagent
transcripts, and optionally any archive you point `--archive` at.

## What it computes

Per assistant message, deduped globally by `message.id`:

- input, cache-write (5m and 1h), cache-read and output tokens
- **billed-equiv** = `input + 1.25*cache_creation + 0.10*cache_read`
- context footprint = `input + cache_creation + cache_read`, the live window size

From those it reconstructs daily burn at day x project x command x model x
effort x main/agent grain, account-wide 5-hour rate-limit blocks, and
per-session summaries (peak context, compactions, interrupts, subagents).

### One term, three spellings

`cache_creation` is the field name in the transcript JSONL
(`cache_creation_input_tokens`). `burnrate.py --help` calls the same quantity
`cache_write`, and the chart note in the report calls it "cache write". They are
the same number with the same 1.25 weight, not three different things.

### billed-equiv is a comparison unit

Quoting the report's own footer:

> Billed-equivalent weights every token to a common unit; it is not a bill.

It exists so a cache-heavy day and a fresh-context day can be compared on one
axis. It is not a price, it is not tied to any plan or rate card, and burnrate
never converts it to currency.

## Why your history is shallow

Claude Code prunes `~/.claude/projects` on its own schedule, governed by the
`cleanupPeriodDays` setting in `~/.claude/settings.json` and defaulting to 30
days. Transcripts older than that are simply gone, so a first run typically
shows about a month no matter how long you've been using Claude Code.

You can raise it, and if you care about long-range usage history you should:

```json
{ "cleanupPeriodDays": 365 }
```

The sweep runs at startup and the minimum is `1`. Do not set `0` expecting it to
mean "never clean up", as it is rejected outright: it used to silently disable
transcript writes, which is the opposite of what anyone setting it wanted. For an
effectively permanent history use a large number such as `3650`.

That only helps going forward. Raising the limit does not bring back anything
already pruned, so the value of setting it is entirely in how early you set it.

To recover history that is already gone you need your own backup. Point
`--archive DIR` at any tree with the same layout (`<project>/*.jsonl`, or
`<project>/<session>/*.zst`) and burnrate merges it with the live tree.

If a hindsight archive is configured on the machine, burnrate detects it
automatically and labels that run "hindsight archive" in the report. `--no-archive` ignores every archive source, auto-detected or not.

## What a generated report embeds

**The HTML is as shareable as this list is.** `dashboard.html` is built from
your transcripts and contains:

- project labels derived from your directory paths
- session titles and slugs
- the first 8 characters of each session id
- slash-command and skill names you invoked
- model ids and effort levels
- per-message timestamps
- the basename of any `--archive` directory

It does **not** contain message content, code, or prompts. But project names and
session titles alone routinely describe what you were working on and for whom.
Treat a generated report as private unless you have read it and decided
otherwise.

The report is written next to `burnrate.py` by default. `.gitignore` it if you
run the tool inside a repository.

## Flags

| Flag | Effect |
|---|---|
| `--tz-offset HOURS` | Bucket days at a fixed UTC offset. Default is system local time, DST-aware. |
| `--range {7,14,30,90,all}` | Date window the opened report starts on, in days or `all`. Default `30`; the report's own preset buttons still change it. |
| `--root DIR` | Transcript tree to read. See resolution order above. |
| `--out DIR` | Where to write the report. Defaults to the directory holding `burnrate.py`. |
| `--archive DIR` | Extra transcript source: a `<project>/*.jsonl` tree or a `<project>/<session>/*.zst` archive. |
| `--no-archive` | Ignore every archive source, auto-detected or not. |
| `--rebuild` | Ignore the cache and reparse everything. |
| `--json` | Also write `dashboard_data.json` beside the report. |
| `--no-open` | Write the report without launching a browser. |
| `--quiet` | Suppress progress output. |

## Cache

Each session is parsed once into a compact cache, so re-runs only touch files
that changed. One gzipped file per source set, under the platform cache
directory:

| Platform | Location |
|---|---|
| Linux | `$XDG_CACHE_HOME/burnrate`, else `~/.cache/burnrate` |
| macOS | `~/Library/Caches/burnrate` |
| Windows | `%LOCALAPPDATA%\burnrate` |

Delete that directory or pass `--rebuild` to force a full reparse.

## The /burnrate skill

This repository ships a Claude Code project skill at `.claude/skills/burnrate/`,
so `/burnrate` is available when Claude Code is working inside this repository,
and nowhere else today.

A bare `/burnrate` builds the dashboard and opens it, writing it under the
platform cache directory above rather than into your working tree. The words
`7d`, `14d`, `30d`, `90d`, `all`, `rebuild` and `no-archive` map onto the flags
of the same name, so `/burnrate 7d rebuild` is `--range 7 --rebuild`. Anything
else is treated as a question: `/burnrate what did yesterday cost` is answered
in chat from `dashboard_data.json`, without opening the report. The same privacy
note applies to a report the skill builds; see "What a generated report embeds".

The skill is a wrapper, never a fork: it runs this repository's own
`burnrate.py`, and `python3 burnrate.py` keeps working with the skill absent or
`.claude/` deleted.

Installing burnrate as a Claude Code plugin from a git URL, so `/burnrate` works
in every project, is planned and not shipped yet.

## The 5h/7d cap card

The report can show rate-limit cap percentages, but only if a statusline logger
has been recording them, because Claude Code does not write them to the
transcripts.

That logger is opt-in and ships separately in [`extras/`](extras/README.md),
which covers installing, upgrading and removing it. Nothing installs it for
you. Without it, every other panel works and the cap card simply does not
render.

## Verifying a copy

```sh
python3 tools/selftest.py
```

## License

MIT. See [LICENSE](LICENSE).
