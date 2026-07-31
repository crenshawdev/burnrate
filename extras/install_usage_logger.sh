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

CUR=$(python3 - "$SETTINGS" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
sl=d.get("statusLine") or {}
print(sl.get("command","") if isinstance(sl,dict) else "")
PY
)

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
    echo "its current contents:" >&2
    cat "$INNER" >&2
    exit 1
fi
printf '%s' "$CUR" > "$INNER" || exit 1

cp "$WRAPPER" "$INSTALLED" || exit 1
chmod +x "$INSTALLED" || exit 1
echo "installed wrapper -> $INSTALLED"

BK="$SETTINGS.bak.$(date -u +%Y%m%dT%H%M%SZ)"
cp -p "$SETTINGS" "$BK" || exit 1
echo "backed up settings.json -> $BK"

python3 - "$SETTINGS" "$INSTALLED" <<'PY' || exit 1
import json,sys
p,w=sys.argv[1],sys.argv[2]
d=json.load(open(p))
sl=d.get("statusLine")
if not isinstance(sl,dict):
    sl={"type":"command"}
sl["command"]=w
sl.setdefault("type","command")
d["statusLine"]=sl
json.dump(d,open(p,"w"),indent=2)
open(p,"a").write("\n")
print("statusLine.command updated")
PY

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
