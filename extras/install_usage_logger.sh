#!/usr/bin/env bash
# Install the rate_limits tee in front of the existing statusline command.
#
# Defaults to a DRY RUN. Nothing is written until you pass --apply.
#
# Guarantees:
#   - your current statusline command is copied to inner-command and NEVER inlined
#   - an existing inner-command is never overwritten (guarded), so re-running
#     cannot lose the original after the wrapper is already installed
#   - settings.json is backed up with a timestamp before any edit
#   - double-wrapping is detected and refused
#   - the JSON is rewritten with json.dump, so unrelated keys keep their values
#   - a settings.json that cannot be parsed, or whose statusLine is not an
#     object carrying a string command, stops the run before anything is
#     written rather than being read as "no statusline configured"
#   - a run that fails partway removes what it created, so a retry is never
#     blocked by the leftovers of the attempt before it
#
# The wrapper is COPIED into the state directory and settings.json points at
# that copy, not at wherever this repository happens to sit. A path into a
# directory that can move or be version-pinned (a plugin cache, a checkout you
# later delete) dies on the next update and takes your whole statusline with it.
#
# bash only. Not supported on native Windows.
set -uo pipefail

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

SETTINGS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
DIR="${CLAUDE_USAGE_LOGGER_DIR:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}/usage-logger}"
INNER="$DIR/inner-command"
WRAPPER="$(cd "$(dirname "$0")" && pwd)/usage_logger.sh"
INSTALLED="$DIR/usage_logger.sh"

if [ ! -r "$SETTINGS" ]; then
    echo "no settings.json at $SETTINGS" >&2
    echo >&2
    echo "Claude Code writes that file the first time any setting changes, so a" >&2
    echo "fresh install may not have one yet. An empty object is enough to" >&2
    echo "install against:" >&2
    echo >&2
    echo "    mkdir -p \"$(dirname "$SETTINGS")\" && printf '{}\\n' > \"$SETTINGS\"" >&2
    echo >&2
    echo "Then re-run this script." >&2
    exit 1
fi
[ -r "$WRAPPER" ] || { echo "wrapper not found at $WRAPPER" >&2; exit 1; }

# Read the current command. An unreadable, unparseable or unexpectedly shaped
# settings.json has to be a hard stop HERE, before anything is written: an
# unchecked read fails to an empty CUR, which is indistinguishable from "no
# statusline configured" and would wrap a working status bar into nothing.
CUR=$(python3 - "$SETTINGS" <<'PY'
import json, sys
path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
except Exception as exc:
    sys.stderr.write("cannot parse %s as JSON:\n  %s\n" % (path, exc))
    sys.exit(2)
if not isinstance(d, dict):
    sys.stderr.write("%s is a %s at the top level, not a JSON object.\n"
                     % (path, type(d).__name__))
    sys.exit(3)
sl = d.get("statusLine")
if sl is None:
    sys.exit(0)
if not isinstance(sl, dict):
    sys.stderr.write("statusLine in %s is a %s, not an object.\n"
                     % (path, type(sl).__name__))
    sys.exit(4)
cmd = sl.get("command")
if cmd is None:
    sys.exit(0)
if not isinstance(cmd, str):
    sys.stderr.write("statusLine.command in %s is a %s, not a string.\n"
                     % (path, type(cmd).__name__))
    sys.exit(5)
sys.stdout.write(cmd)
PY
)
RC=$?
if [ "$RC" -ne 0 ]; then
    echo >&2
    if [ "$RC" -eq 2 ] || [ "$RC" -eq 3 ]; then
        echo "That file draws your status bar and this installer will not guess at" >&2
        echo "what is in it. Fix the JSON by hand (a trailing comma or a stray" >&2
        echo "character is the usual cause), then re-run this script." >&2
    else
        echo "This installer only wraps a statusLine object whose command is a" >&2
        echo "string, because that string is what gets saved and replayed. Put your" >&2
        echo "statusline in that shape by hand, then re-run this script." >&2
    fi
    echo >&2
    echo "Nothing was written." >&2
    exit 1
fi

echo "settings file : $SETTINGS"
echo "wrapper       : $WRAPPER"
echo "state dir     : $DIR"
echo
echo "--- current statusLine.command ---"
if [ -z "$CUR" ]; then echo "(none configured)"; else echo "$CUR"; fi
echo

case "$CUR" in
  *usage_logger.sh*)
      echo "Already wrapped. Nothing to do."
      [ -r "$INNER" ] && { echo; echo "--- inner-command currently saved ---"; cat "$INNER"; }
      exit 0 ;;
esac

if [ -z "$CUR" ]; then
    echo "warning: no statusline is configured, so there is nothing to wrap."
    echo "         The logger will record rate limits, but your status bar will"
    echo "         stay empty until you configure a statusline command. Set one"
    echo "         first if you want one, then re-run this script."
    echo
fi

echo "--- after install, statusLine.command becomes ---"
echo "$INSTALLED"
echo
echo "--- which is a copy of ---"
echo "$WRAPPER"
echo
echo "--- and the command above is preserved verbatim at ---"
echo "$INNER"
echo

if [ "$APPLY" -ne 1 ]; then
    echo "DRY RUN. Re-run with --apply to make these changes."
    exit 0
fi

mkdir -p "$DIR" || exit 1

# Guard: never clobber an already-saved original.
if [ -f "$INNER" ]; then
    echo "refusing to overwrite existing $INNER" >&2
    echo >&2
    echo "its current contents:" >&2
    cat "$INNER" >&2
    echo >&2
    echo "That file is a statusline command saved by an earlier install of this" >&2
    echo "wrapper, and overwriting it would lose it for good. Put it back into" >&2
    echo "settings.json if you still want that statusline, then remove the file" >&2
    echo "and re-run this script:" >&2
    echo >&2
    echo "    rm -f \"$INNER\"" >&2
    exit 1
fi

# The saved original is staged under a name the guard above ignores and only
# takes its real name once every other step has succeeded. A run that dies
# partway therefore leaves no inner-command to trip that guard on the retry,
# and this script never has to delete anything to clean up after itself. The
# staging file is rewritten by the next run, and nothing reads it.
STAGED="$DIR/.inner-command.new"
printf '%s' "$CUR" > "$STAGED" || exit 1

cp "$WRAPPER" "$INSTALLED" || exit 1
chmod +x "$INSTALLED" || exit 1
echo "installed wrapper -> $INSTALLED"

BK="$SETTINGS.bak.$(date -u +%Y%m%dT%H%M%SZ)"
cp -p "$SETTINGS" "$BK" || exit 1
echo "backed up settings.json -> $BK"

python3 - "$SETTINGS" "$INSTALLED" <<'PY' || exit 1
import json, os, shlex, sys
p, w = sys.argv[1], sys.argv[2]
with open(p, encoding="utf-8") as fh:
    d = json.load(fh)
sl = d.get("statusLine")
if sl is None:
    sl = {"type": "command"}
elif not isinstance(sl, dict):
    # unreachable via this script (the read above refuses it), so reaching it
    # means the file changed underneath us: stop rather than discard it
    sys.exit("statusLine is no longer an object; refusing to replace it")
# Claude Code runs statusLine.command through a shell, so a bare path with a
# space in it word-splits: /mnt/c/Users/First Last/... is a normal WSL home and
# a normal config dir, and the unquoted form dies with exit 127 while this
# script reports success. shlex.quote leaves an ordinary path untouched.
sl["command"] = shlex.quote(w)
sl.setdefault("type", "command")
d["statusLine"] = sl
# Edit the file IN PLACE. Never write a temp file and rename over the target:
# a rename replaces the name, so it silently changes what settings.json IS.
# Aimed at a stow/yadm symlink it deletes the link and leaves a regular file
# the dotfiles repo never sees; it breaks hardlinks; and it resets owner,
# group and xattrs to whatever the temp file had. Writing through the existing
# handle keeps the inode and every one of those properties, so this installer
# changes the file's CONTENT and nothing else about how the system manages it.
#
# The cost is a truncate-then-write window: serialize completely first so the
# window is one write() call, and the timestamped backup taken moments ago is
# the recovery path if it is ever interrupted.
blob = json.dumps(d, indent=2) + "\n"
with open(p, "w", encoding="utf-8") as fh:
    fh.write(blob)
    fh.flush()
    os.fsync(fh.fileno())
print("statusLine.command updated")
PY

# Commit point: settings.json now names the wrapper, so the saved original
# takes the name the wrapper reads. A rename within one directory, over a path
# the guard above proved empty.
if ! mv "$STAGED" "$INNER"; then
    echo "could not move $STAGED to $INNER" >&2
    echo "settings.json already points at the wrapper, so move that file into" >&2
    echo "place by hand -- it holds your original statusline command." >&2
    exit 1
fi

echo
echo "Done. Restart Claude Code (or start a new session) for the statusline to reload."
echo "Samples will appear at $DIR/usage-log.jsonl once a session gets its first API response."
echo
echo "To undo:"
echo "  cp '$BK' '$SETTINGS'"
echo "  rm -f '$INSTALLED' '$INNER' '$DIR/.last'"
echo
echo "usage-log.jsonl is deliberately left behind: it is the only record of your"
echo "cap percentages, and nothing can reconstruct it. Delete it separately if you"
echo "want it gone."
