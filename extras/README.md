# The rate-limit logger

An opt-in statusline logger that records your 5-hour and 7-day cap
percentages, so burnrate's cap card has something to draw.

Claude Code hands its statusline command a JSON payload on stdin, and for
Claude.ai subscribers that payload carries `rate_limits.five_hour` and
`rate_limits.seven_day` as percentages of your window. That is the only place
those numbers appear: Claude Code never writes them to the transcripts under
`~/.claude/projects`, so a tool that only reads transcripts, burnrate included,
cannot reconstruct them. If nobody records a sample while a window is open, the
number for that moment is gone.

This logger records them. It is opt-in and does nothing until you install it
yourself: neither `burnrate.py` nor the `/burnrate` skill installs it, mentions
the installer, or writes to `settings.json`. Every other panel in the report
works without it.

`usage_logger.sh` wraps the statusline command you already have as a
transparent tee. It reads the payload once, appends a line when a percentage
actually changes, and replays the payload verbatim into your original command,
propagating that command's exit status. Your status bar renders exactly as it
did before, including when every part of the logging fails.

**bash only.** The logger is not supported on native Windows, because a Windows
statusline is not a bash context. `burnrate.py` itself runs fine there; only
this extra does not. WSL is a bash context and works.

## Install

Preview first. Without `--apply` the installer writes nothing at all: no
directory, no backup, no edit.

```sh
bash extras/install_usage_logger.sh
```

It prints the settings file it found, your current `statusLine.command`, and
what that command would become. When it looks right:

```sh
bash extras/install_usage_logger.sh --apply
```

That writes three things, all under `<config>/usage-logger/` where `<config>`
is `$CLAUDE_CONFIG_DIR` when set and `~/.claude` otherwise:

| Path | What it is |
|---|---|
| `<config>/usage-logger/usage_logger.sh` | a copy of the wrapper, and what `statusLine.command` now points at |
| `<config>/usage-logger/inner-command` | your original statusline command, saved verbatim |
| `<config>/settings.json.bak.<timestamp>` | a copy of your settings file as it was |

Then restart Claude Code, or start a new session, for the statusline to
reload.

Samples start landing in `<config>/usage-logger/usage-log.jsonl` once a session
gets its first API response. The `rate_limits` field is absent before that and
absent for accounts it does not apply to, so an empty log right after a restart
is normal.

The wrapper is **copied** into that directory rather than run from wherever
this repository sits, and `settings.json` points at the copy. A path into a
tree that can move, be version-pinned or be deleted (a plugin cache, a checkout
you later clean up) stops resolving on the next update, and a statusline
command that does not resolve takes your whole status bar with it. The copy
lives beside your own config and outlives this checkout.

### No settings.json yet

Claude Code writes `settings.json` the first time any setting changes, so a
fresh install may not have one. The installer says so and stops. An empty
object is enough to install against:

```sh
mkdir -p ~/.claude && printf '{}\n' > ~/.claude/settings.json
```

### No statusline configured

Installing with no `statusLine.command` set is allowed and the installer warns
about it: the logger will record cap percentages, but your status bar stays
empty until you configure a statusline of your own. Nothing breaks; there is
simply nothing to tee.

## Upgrading an installed copy

The installer answers a re-run with "Already wrapped. Nothing to do.", which is
the guard that stops it from wrapping the wrapper. It also means a later fix to
`usage_logger.sh` never reaches you on its own. Copy the new version over the
installed one:

```sh
cp extras/usage_logger.sh ~/.claude/usage-logger/usage_logger.sh
```

No settings change is needed: `statusLine.command` already points there. Use
`$CLAUDE_CONFIG_DIR/usage-logger/` instead if you set that variable.

## Uninstall

Restore the backup the install printed, then remove the installed files:

```sh
cp ~/.claude/settings.json.bak.<timestamp> ~/.claude/settings.json
rm -f ~/.claude/usage-logger/usage_logger.sh \
      ~/.claude/usage-logger/inner-command \
      ~/.claude/usage-logger/.last
```

`usage-log.jsonl` is deliberately left behind. That directory is both the
install directory and the data directory, and the log is the only record of
your cap history: nothing can reconstruct it, not the transcripts and not
Claude Code. Deleting the directory wholesale throws that away. Delete the log
separately if you actually want it gone.

## What guards this

- The installer is a dry run unless you pass `--apply`. Nothing is created,
  copied, backed up or edited without it.
- Your original statusline command is saved to `inner-command`, never inlined
  into another command string, so no quoting from `settings.json` has to
  survive a round trip.
- An existing `inner-command` is never overwritten. The installer prints its
  contents and stops, because that file may be the last copy of a statusline
  you have, and names the file to remove once you no longer need it.
- A `statusLine.command` already pointing at the wrapper is refused rather than
  wrapped again.
- A `settings.json` the installer cannot parse, or whose `statusLine` is not an
  object carrying a string command, stops the run before anything is written.
  An unreadable settings file is never treated as "no statusline configured".
- A run that fails partway leaves no `inner-command` behind, so the guard above
  cannot trap you on the retry: the saved original takes that name only once
  every step that can fail has succeeded.
- `settings.json` is copied to a timestamped backup before any edit, and
  rewritten through a JSON parser, so every other key keeps its value.
- `settings.json` is edited **in place**. The installer changes what is inside
  the file and nothing about the file itself: a symlink into a dotfiles repo
  stays a symlink and the repo's own copy receives the change, a hardlink keeps
  its other names, and the inode, owner, mode and extended attributes are the
  ones you had. Nothing here writes a temp file and renames it over yours.
- The wrapper propagates the inner command's exit status. Claude Code uses a
  statusline child's output only when it exited 0, so a swallowed status is a
  blank status bar.
- Every logging step is best-effort and separately guarded. An unwritable log
  directory, a full disk or a missing `date` costs you samples, never your
  statusline.

## Where the log lives, and where burnrate looks

`$CLAUDE_CONFIG_DIR` moves both the settings file and the state directory: set
it and everything above lives under `$CLAUDE_CONFIG_DIR/` instead of
`~/.claude/`.

burnrate resolves its own log path differently. It looks for
`usage-logger/usage-log.jsonl` beside whatever transcript root it resolved, so
a `$CLAUDE_PROJECTS` or `--root` tree whose parent is not your config directory
puts the two in different places, and the cap card never appears. Set
`$CLAUDE_USAGE_LOGGER_DIR` for both the installer and the statusline in that
case, pointing it at `<that parent>/usage-logger`. It overrides the state
directory outright, for the installer and the wrapper alike.

## Known limits

The wrapper extracts the two percentages with `grep`, not a JSON parser,
because the statusline fires many times a second and a Python startup on each
firing is not worth it. That assumes the payload stays single-line JSON with no
nested braces inside the two window objects, which is what Claude Code emits
today. If a future payload pretty-prints or nests another field in there, the
extraction quietly matches nothing and the log stops growing. No error appears
anywhere, so the symptom is a cap card that stops advancing.
