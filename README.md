# burnrate

A single-file, on-demand dashboard of your own Claude Code token usage.

Run it and one self-contained `dashboard.html` opens in your browser: daily burn
by project, per-model and per-command breakdowns, 5-hour rate-limit blocks, and
per-session summaries. Date-range and project filters run client-side, so the
report works offline and can be reopened without re-running anything.

It reads local files and writes local files. No network calls, no telemetry, no
account required.

## Contents

- [Install](#install)
- [Running it](#running-it)
- [What it reads](#what-it-reads)
- [What it computes](#what-it-computes)
- [What the dashboard shows](#what-the-dashboard-shows)
- [Why your history is shallow](#why-your-history-is-shallow)
- [What a generated report embeds](#what-a-generated-report-embeds)
- [Flags](#flags)
- [Cache](#cache)
- [Updating, disabling and removing](#updating-disabling-and-removing)
- [The 5h/7d cap card](#the-5h7d-cap-card)
- [Troubleshooting](#troubleshooting)
- [Verifying a copy](#verifying-a-copy)
- [License](#license)

## Install

burnrate installs as a Claude Code plugin. Two commands:

```sh
claude plugin marketplace add https://git.jcrenshaw.dev/crenshawdev/burnrate.git
claude plugin install burnrate@burnrate
```

The same two steps work inside a running session:

```
/plugin marketplace add https://git.jcrenshaw.dev/crenshawdev/burnrate.git
/plugin install burnrate@burnrate
```

A bare `/plugin` opens the plugin manager if you would rather browse. The GitHub
mirror works as an alternative source for the first step either way:

```sh
claude plugin marketplace add crenshawdev/burnrate
```

Restart Claude Code, or start a new session, and `/burnrate` works in every
project. The plugin carries the tool with it: nothing to copy, nothing to clone,
no path to configure.

Python 3.8 or newer, standard library only. `zstandard` is optional and needed
solely to read `.zst` archives. Without it burnrate still runs against your live
transcripts and says so in the report subtitle instead of failing.

## Running it

```
/burnrate                              build the dashboard and open it
/burnrate 7d                           open on the 7-day preset
/burnrate 30d rebuild                  reparse everything, 30-day preset
/burnrate all no-archive               live transcripts only, full history
/burnrate what did yesterday cost      answered in chat, no browser
/burnrate which project burned most this week
/burnrate how much is left in this block
```

The words `7d`, `14d`, `30d`, `90d`, `all`, `rebuild` and `no-archive` select
the behavior described under [Flags](#flags). Anything else is treated as a
question and answered in chat from the last run's data, without opening the
report. The same privacy note applies to a report the skill builds; see
[What a generated report embeds](#what-a-generated-report-embeds).

A bare `/burnrate` writes the report under the platform
[cache directory](#cache) rather than into your working tree.

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

## What the dashboard shows

Across the top: a date-range picker and a project filter. Both run client-side,
so narrowing the range or picking projects re-renders every panel below without
re-running the tool. Six summary tiles follow the filters:

| Tile | Reads |
|---|---|
| Billed-equivalent tokens | Range total, with the percentage change against the prior window of the same length |
| Daily average | Total over active days, not calendar days |
| Peak day | Heaviest single day and its date |
| Output tokens | Output total and the message count behind it |
| 5h blocks | How many rate-limit blocks the range covers, and their median burn |
| Peak context | Largest live window reached, and the session count |

Then the panels:

- **Daily burn by project**, stacked per day. The main chart.
- **Five-hour blocks**, the rate-limit windows. A block opens on the hour of
  first activity and runs five hours. Account-wide by design, so the project
  filter does not apply to it.
- **Rolling 7-day burn**, the trailing weekly total, which is the pacing signal
  against a weekly window.
- **Token composition**, the billed-weighted share of input, cache write and
  cache read per day. This is where a cache-heavy pattern becomes visible.
- **By command**, **by model**, **by effort**, and **main vs subagents**, four
  breakdown bars over the filtered range.
- **Rate-limit windows**, logged used-percentage from the statusline payload.
  Renders only when the [cap card](#the-5h7d-cap-card) logger has been running.
- **Top sessions**, the heaviest sessions in range, with peak context,
  compactions, interrupts and subagent counts.
- **Daily totals table**, collapsed by default, for reading exact numbers.

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

The report is written under the platform [cache directory](#cache), outside any
project, so it never lands in a repository by accident.

## Flags

These are the tool's own flags. `/burnrate`'s window words select among them;
they are listed so the words have a precise meaning.

| Flag | Effect |
|---|---|
| `--tz-offset HOURS` | Bucket days at a fixed UTC offset. Default is system local time, DST-aware. |
| `--range {7,14,30,90,all}` | Date window the opened report starts on, in days or `all`. Default `30`; the report's own preset buttons still change it. |
| `--root DIR` | Transcript tree to read. See resolution order above. |
| `--out DIR` | Where to write the report. `/burnrate` always points this at the cache directory. |
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

## Updating, disabling and removing

| Command | Effect |
|---|---|
| `claude plugin update burnrate` | Pull a newer commit. Restart Claude Code to apply it. |
| `claude plugin disable burnrate` | Turn it off. `/burnrate` goes away with it after a restart. |
| `claude plugin enable burnrate` | Turn it back on, again after a restart. |
| `claude plugin uninstall burnrate` | Remove it entirely. |

Each of these has a `/plugin` equivalent inside a session.

The skill is a wrapper, never a fork. It runs the `burnrate.py` the plugin
shipped, so an update moves both at once and there is no second copy to drift.

## The 5h/7d cap card

The report can show rate-limit cap percentages, but only if a statusline logger
has been recording them, because Claude Code does not write them to the
transcripts.

That logger is opt-in and ships separately in [`extras/`](extras/README.md),
which covers installing, upgrading and removing it. Nothing installs it for
you. Without it, every other panel works and the cap card simply does not
render.

The logger is the one part that does need a clone, because you run its installer
yourself:

```sh
git clone https://git.jcrenshaw.dev/crenshawdev/burnrate.git
cd burnrate
bash extras/install_usage_logger.sh
```

## Troubleshooting

**"no data", or a report with nothing in it.** burnrate found no transcripts at
the path it resolved. Check the resolution order under
[What it reads](#what-it-reads); setting `$CLAUDE_PROJECTS` or
`$CLAUDE_CONFIG_DIR` in your environment redirects it. An empty run means no
files matched, which is a different thing from a real zero.

**Only about a month of history.** Expected, and not a bug in burnrate. Claude
Code prunes its own transcripts. See
[Why your history is shallow](#why-your-history-is-shallow).

**`/burnrate 7d` reports a range wider than seven days.** The run line reports
the whole parsed dataset. The window word sets the preset the opened report
starts on and nothing else, so that line and the chart disagree by design.

**Archives are being skipped.** `.zst` archives need the optional `zstandard`
package. Without it the tool runs on your live transcripts and says so in the
report subtitle. Plain `<project>/*.jsonl` archive trees need nothing extra.

**Stale numbers after a session you know happened.** The cache keys on files
that changed. Force a full reparse with `/burnrate rebuild`, or delete the cache
directory listed under [Cache](#cache).

**No browser opened.** The run still reports the path it wrote. Open that file
yourself; the report is self-contained and works offline.

**`/burnrate` does not appear after installing the plugin.** Restart Claude Code
or start a new session, then confirm it is installed and enabled with
`claude plugin list`. A plugin skill can register under a qualified id, so check
that `/burnrate` works rather than looking for a literal `/burnrate` row.

**The 5h/7d cap card does not render.** Its logger is opt-in and installed
separately. See [The 5h/7d cap card](#the-5h7d-cap-card). Every other panel works
without it.

## Verifying a copy

```sh
python3 tools/selftest.py
```

## License

MIT. See [LICENSE](LICENSE).
