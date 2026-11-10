# Changelog

## 2.0.0 (Unreleased)

The 5-hour blocks are gone. burnrate now describes the 7-day rate-limit window
alone.

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

### Upgrading

- Logs written in the old format keep working with no migration. The
  `five_hour` and `resets5` keys on existing lines are ignored.
- Re-copying the logger wrapper is optional. An installed copy keeps writing
  those keys, which is harmless.
