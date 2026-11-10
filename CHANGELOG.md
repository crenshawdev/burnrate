# Changelog

## 2.0.0 (Unreleased)

The 5-hour blocks are gone. burnrate now describes the 7-day rate-limit window
alone. It also reads the fields Claude Code now records in its transcripts, and
transcripts written before those fields existed still parse.

### Added

- New transcript fields are read: `attributionSkill`, `attributionPlugin`,
  `attributionAgent`, `attributionMcpServer` and `attributionMcpTool`,
  `usage.output_tokens_details.thinking_tokens`, `promptCacheRequested`,
  `system/turn_duration` lines, `sessionKind`, the web search and fetch counts
  in `usage.server_tool_use`, and `turnOrigin`.
- Dashboard surfaces:
  - By agent, By plugin and By MCP tool cards in the breakdown grid. They count
    stamped messages only.
  - Thinking tokens as a share of output on the Output tokens tile. Thinking is
    already part of output, so the tile's value does not change.
  - An Active column in Top sessions, summed from `turn_duration`, and a
    background mark on sessions with background messages. A session with no
    `turn_duration` lines shows a dash, not zero.
  - A Web search and fetch card and a Foreground vs background card. Each
    shows only when the selected range has a nonzero value for it.
- `--json` payload positions. A `daily` row is now
  `[day, project, command, model, effort, kind, billed-equiv, input, cache write,
  1h cache write, cache read, output, messages, agent, plugin, mcp server,
  mcp tool, background, requested cache ttl, thinking tokens, web searches,
  web fetches]`, so positions 13 to 21 are new:
  - 13 agent, 14 plugin, 15 MCP server, 16 MCP tool: the stamps as written, an
    empty string when absent.
  - 17 background: 1 when the message carries `sessionKind: "bg"`, else 0.
    `kind` stays `m` or `a`.
  - 18 requested cache TTL from `promptCacheRequested`, an empty string when
    unrecorded. It is information only and changes no number.
  - 19 thinking tokens, a part of output (position 11), never added to it.
  - 20 web search requests and 21 web fetch requests.
- A `sessions` row gains two positions: 12 is the count of its background
  messages, and 13 is its active time in milliseconds, summed over unique
  `turn_duration` uuids and null when the session has none.
- A status line mod in the plugin, drawn under the prompt in Claude Code. It
  shows the week's burndown, meaning 7-day usage against the time since the
  window opened, and this session's cache hit rate. It is on by default, and
  the `showStatusLine` switch in `/config` turns it off. The hit rate counts
  only turns since the plugin was on.
- History from Terminus. When `terminus` 2.0.0 or later is on the `PATH`,
  burnrate reads its archive through read-only commands. The live copy of a
  file wins, an evicted Terminus file keeps the usage the cache already holds
  for it, and a session is exported again when its Terminus watermarks or its
  live or archive files changed. `--no-archive` turns it off. The first run with Terminus starts a new cache
  file and is slow.

### Removed

- The `blocks` key is gone from `--json` (`dashboard_data.json`), and the
  skill's `blocks` subcommand is gone with it. `burnrate_skill.py blocks` now
  exits 2 with an argparse error.
- The viewer's blocks card, chart and `5h blocks` tile. The summary row is six
  tiles, and the CLI `billed` line no longer carries a block count.

### Changed

- `rl` rows are now `[ts, seven_day]` instead of `[ts, five_hour, seven_day]`,
  so `seven_day` moved from index 2 to index 1. The cap card plots the 7-day
  series alone, and log lines whose `seven_day` is null are dropped.
- The logger wrapper (`extras/usage_logger.sh`) no longer writes `five_hour` or
  `resets5`, and it writes a line only when the 7-day percentage moves.
- Usage now comes from the most complete streamed line of each message, the one
  with the largest `output_tokens`, not the first line. Output totals rise for
  messages Claude Code wrote in parts, and billed-equiv moves where a later
  line's cache numbers differ.
- A message stamped with `attributionSkill` is filed under that skill. Messages
  without the stamp follow the segment rule as before.
- A prompt with `turnOrigin: "human"` ends a command's segment, even when the
  line has no `origin` key.

### Upgrading

- Logs written in the old format keep working with no migration. The
  `five_hour` and `resets5` keys on existing lines are ignored.
- Re-copying the logger wrapper is optional. An installed copy keeps writing
  those keys, which is harmless.
- The parse cache version moved, so the first run after upgrading reparses
  every session.
- Transcripts without the new fields parse as before. An unstamped message
  falls back to the segment rule and reads as foreground.
