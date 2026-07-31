#!/usr/bin/env bash
# Tee Claude Code's rate_limits telemetry to a log, then hand the payload to the
# real statusline command untouched.
#
# Claude Code passes a JSON blob on stdin to whatever statusline command is
# configured. For Pro/Max subscribers that blob carries the only ground-truth
# signal available for subscription burn:
#
#   "rate_limits": {
#     "five_hour": { "used_percentage": 23.5, "resets_at": 1738425600 },
#     "seven_day": { "used_percentage": 41.2, "resets_at": 1738857600 }
#   }
#
# (code.claude.com/docs/en/statusline -- the field appears only for Claude.ai
# subscribers, only after the first API response in a session, and each window
# may be independently absent.)
#
# This wrapper is deliberately dumb: it reads stdin once, appends one compact
# record when the percentages CHANGE, and replays stdin verbatim into the
# original command. It never edits, reorders, or reformats the payload, so the
# wrapped statusline behaves exactly as it did before.
#
# The original command lives in a sibling file rather than being inlined here,
# so no quoting from your settings.json ever has to survive a round trip.
#
# State lives under <config>/usage-logger, where <config> is $CLAUDE_CONFIG_DIR
# when set and ~/.claude otherwise:
#
#   <config>/usage-logger/inner-command    the original statusline command
#   <config>/usage-logger/usage-log.jsonl  the append-only sample log
#   <config>/usage-logger/.last            dedupe memo (last seen pair)
#
# $CLAUDE_USAGE_LOGGER_DIR overrides that directory outright. Use it when
# burnrate reads the log from somewhere else: burnrate looks for the log beside
# the transcript root it resolved, so a $CLAUDE_PROJECTS (or --root) tree whose
# parent is not the config directory needs this variable pointed at
# <that parent>/usage-logger for the cap card to appear.
#
# bash only. This is not supported on native Windows (a Windows statusline is
# not a bash context), even though burnrate.py itself runs there.
#
# Failure policy: every logging step is best-effort. If anything here breaks,
# the statusline must still render, so all logging is guarded and the inner
# command's exit status is what propagates.

set -uo pipefail

DIR="${CLAUDE_USAGE_LOGGER_DIR:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}/usage-logger}"
LOG="$DIR/usage-log.jsonl"
LAST="$DIR/.last"
INNER="$DIR/inner-command"
MAX_BYTES=$((32 * 1024 * 1024))

input=$(cat)

log_sample() {
    # Cheap presence check first: most invocations before the first API response
    # carry no rate_limits at all, and those are not worth a subshell.
    case "$input" in
        *'"rate_limits"'*) ;;
        *) return 0 ;;
    esac

    local five seven r5 r7 pair now
    five=$(printf '%s' "$input" | grep -o '"five_hour":[[:space:]]*{[^}]*}' |
           grep -o '"used_percentage":[[:space:]]*[0-9.]*' | head -1 |
           grep -o '[0-9.]*$')
    seven=$(printf '%s' "$input" | grep -o '"seven_day":[[:space:]]*{[^}]*}' |
            grep -o '"used_percentage":[[:space:]]*[0-9.]*' | head -1 |
            grep -o '[0-9.]*$')
    [ -z "$five" ] && [ -z "$seven" ] && return 0

    # One row per actual change. A statusline can fire many times a second; the
    # regression only learns from intervals where the number actually moved.
    pair="${five:-_}:${seven:-_}"
    if [ -r "$LAST" ] && [ "$(cat "$LAST" 2>/dev/null)" = "$pair" ]; then
        return 0
    fi
    printf '%s' "$pair" > "$LAST" 2>/dev/null

    r5=$(printf '%s' "$input" | grep -o '"five_hour":[[:space:]]*{[^}]*}' |
         grep -o '"resets_at":[[:space:]]*[0-9]*' | head -1 | grep -o '[0-9]*$')
    r7=$(printf '%s' "$input" | grep -o '"seven_day":[[:space:]]*{[^}]*}' |
         grep -o '"resets_at":[[:space:]]*[0-9]*' | head -1 | grep -o '[0-9]*$')

    # session id and model are carried so the join can attribute an interval
    sid=$(printf '%s' "$input" | grep -o '"session_id":[[:space:]]*"[^"]*"' |
          head -1 | sed 's/.*"\([^"]*\)"$/\1/')
    mdl=$(printf '%s' "$input" | grep -o '"display_name":[[:space:]]*"[^"]*"' |
          head -1 | sed 's/.*"\([^"]*\)"$/\1/')

    now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    if [ -f "$LOG" ]; then
        sz=$(wc -c < "$LOG" 2>/dev/null || echo 0)
        [ "$sz" -gt "$MAX_BYTES" ] && mv -f "$LOG" "$LOG.1" 2>/dev/null
    fi
    printf '{"ts":"%s","five_hour":%s,"seven_day":%s,"resets5":%s,"resets7":%s,"session_id":"%s","model":"%s"}\n' \
        "$now" "${five:-null}" "${seven:-null}" "${r5:-null}" "${r7:-null}" \
        "${sid:-}" "${mdl:-}" >> "$LOG" 2>/dev/null
}

log_sample 2>/dev/null || true

# Replay stdin verbatim into the original statusline command.
if [ -r "$INNER" ]; then
    cmd=$(cat "$INNER")
    if [ -n "$cmd" ]; then
        printf '%s' "$input" | bash -c "$cmd"
        exit $?
    fi
fi

# No inner command configured: emit nothing rather than clobbering the status bar.
exit 0
