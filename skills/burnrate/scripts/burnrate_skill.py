#!/usr/bin/env python3
"""
burnrate skill helper -- the /burnrate skill's only entry point into the tool.

Subcommands:
    run [words...]   build the dashboard and (unless --no-open) open it
    ask              summarize the last run's daily rows as JSON  (task 4)
    blocks           summarize the last run's 5h blocks as JSON   (task 5)

The helper never copies, edits or vendors burnrate.py: it walks up from its own
location until it finds the single tracked copy, so `python3 burnrate.py` keeps
working with this skill absent, and the copy the test suite covers is the copy
the skill runs. Reports are written under the platform cache directory that
burnrate.py itself computes, never into the user's working tree.

Stdlib only, Python 3.8+. No network calls; the only writes are the ones
burnrate.py makes inside the resolved output directory.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

TOOL_NAME = "burnrate.py"
PAYLOAD = "dashboard_data.json"
BLOCK_SECONDS = 5 * 3600        # a rate-limit window, same constant as the tool

# The daily row's twelve unlabeled positions, mirroring burnrate.py's own
# legend (`const D = {...}` beside the viewer's payload). Every read of a daily
# row goes through this map: a wrong literal index returns a plausible wrong
# number with no failure signal, and this is the one place to get it right.
# cache_write is the TOTAL cache_creation; cache_write_1h is the 1h-TTL half
# already counted inside it. They nest, and summing the two double-counts.
COLS = {"day": 0, "project": 1, "command": 2, "model": 3, "effort": 4,
        "kind": 5, "billed_equiv": 6, "input": 7, "cache_write": 8,
        "cache_write_1h": 9, "cache_read": 10, "output": 11, "messages": 12}
# Day strings are compared as strings, so an unvalidated one filters silently
# instead of failing: '2026-3-1' sorts below every payload day and empties the
# selection. Every date-shaped flag goes through this.
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
GROUP_KEYS = ("day", "project", "command", "model", "effort", "kind")
METRICS = ("billed_equiv", "input", "cache_write", "cache_write_1h",
           "cache_read", "output")

# The words the skill accepts, mapped onto burnrate.py's own flags. Matching is
# case-insensitive and ignores surrounding punctuation, so "7d," from a typed
# line still lands. Anything not in here is a question, not a flag, and `run`
# refuses it rather than silently rebuilding the whole dashboard.
WORDS = {
    "7": ["--range", "7"],
    "7d": ["--range", "7"],
    "14": ["--range", "14"],
    "14d": ["--range", "14"],
    "30": ["--range", "30"],
    "30d": ["--range", "30"],
    "90": ["--range", "90"],
    "90d": ["--range", "90"],
    "all": ["--range", "all"],
    "rebuild": ["--rebuild"],
    "no-archive": ["--no-archive"],
    "noarchive": ["--no-archive"],
    "no_archive": ["--no-archive"],
}

PUNCT = "\"'`.,;:!?()[]{}<>"


def die(msg, code=2):
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def find_tool():
    """burnrate.py, found by walking up from this file. Never an absolute path
    baked in (it would carry whoever built it), never a second copy."""
    here = os.path.dirname(os.path.abspath(__file__))
    d = here
    while True:
        cand = os.path.join(d, TOOL_NAME)
        if os.path.isfile(cand):
            return cand
        parent = os.path.dirname(d)
        if parent == d:
            die(f"{TOOL_NAME} not found in any directory above {here}")
        d = parent


def load_tool(path):
    """burnrate.py as a module, for its own platform rules. Safe to import: it
    guards main() behind `if __name__ == '__main__'`.

    Bytecode is suppressed across the exec: the source loader caches a .pyc
    beside the file it loaded, and under a plugin install that file sits in a
    directory Claude Code owns and re-copies on update -- so every /burnrate
    would leave a __pycache__ there for an update to clobber. The flag is
    restored afterwards rather than set once, so this does not change
    interpreter-wide behavior for anything imported later."""
    spec = importlib.util.spec_from_file_location("burnrate_tool", path)
    mod = importlib.util.module_from_spec(spec)
    prior = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = prior
    return mod


def out_dir(tool_path, create=True):
    """<platform cache dir>/burnrate/report -- asked of the tool rather than
    reimplemented, and never derived from the working directory. --dry-run
    passes create=False so a rehearsal writes nothing at all."""
    tool = load_tool(tool_path)
    d = os.path.join(tool.cache_dir(), "report")
    if create:
        os.makedirs(d, exist_ok=True)
    return d


def flags_for(words):
    """Words to argv fragments, keyed by flag name so two range words collapse
    to the last one asked for instead of emitting a stray bare value."""
    seen = {}
    for raw in words:
        w = raw.strip(PUNCT).lower()
        if w not in WORDS:
            die(f"unrecognized word: {raw!r}\n"
                f"accepted: {', '.join(sorted(WORDS))}\n"
                "anything else is a question -- use the ask subcommand")
        frag = WORDS[w]
        seen[frag[0]] = frag
    out = []
    for frag in seen.values():
        out += frag
    return out


# ---------------------------------------------------------------- payload

def payload_age_min(data):
    """Minutes since the payload was generated. `generated` is the only
    freshness signal burnrate writes, and it is always an aware timestamp."""
    ts = datetime.fromisoformat(data["generated"])
    if ts.tzinfo is None:                       # defensive: never seen in v1
        ts = ts.astimezone()
    return (datetime.now(timezone.utc) - ts).total_seconds() / 60.0


def ensure_payload(max_age_min):
    """(data, reused). Reuse dashboard_data.json while it is younger than
    max_age_min; otherwise rebuild it with --no-open, which is what "answer
    without opening the report" means -- dashboard.html is rewritten either
    way, only the browser launch is suppressed."""
    tool = find_tool()
    out = out_dir(tool)
    path = os.path.join(out, PAYLOAD)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        age = payload_age_min(data)
        # a stamp far in the future is a broken clock, not a fresh payload
        if -max_age_min <= age < max_age_min:
            return data, True
    except (OSError, ValueError, KeyError):
        pass

    argv = [sys.executable, tool, "--json", "--no-open", "--quiet",
            "--out", out]
    # the tool's summary goes to stderr here: stdout belongs to this command's
    # single JSON object, and a caller parsing it must not have to strip
    # four lines of report chatter off the front.
    proc = subprocess.run(argv, stdout=subprocess.PIPE, text=True)
    if proc.stdout:
        sys.stderr.write(proc.stdout)
    if proc.returncode != 0:
        die(f"{TOOL_NAME} exited {proc.returncode}", proc.returncode)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), False
    except (OSError, ValueError) as exc:
        die(f"no usable {PAYLOAD} in {out}: {exc}")


def check_day_word(word):
    """Shape-check a --day word BEFORE the payload is touched. Resolving one to
    a date needs the payload's tz_offset, so a malformed word validated after
    ensure_payload pays for a full transcript reparse before its usage error."""
    w = word.strip().lower()
    if not DAY_RE.match(w) and w not in ("today", "yesterday"):
        die(f"--day wants YYYY-MM-DD, today or yesterday: {word!r}")


def check_date_flag(name, value):
    if value is not None and not DAY_RE.match(value.strip()):
        die(f"{name} wants YYYY-MM-DD: {value!r}")


def resolve_day(word, tz_offset):
    """A day string in the same bucketing the payload used. 'today' and
    'yesterday' follow the system's local date when the payload was built at
    the same UTC offset, and otherwise UTC shifted by that offset -- which is
    the offset the payload's day strings were bucketed under."""
    w = word.strip().lower()
    if DAY_RE.match(w):
        return w
    if w not in ("today", "yesterday"):
        die(f"--day wants YYYY-MM-DD, today or yesterday: {word!r}")
    local = datetime.now().astimezone()
    local_off = (local.utcoffset() or timedelta()).total_seconds() / 3600.0
    if abs(local_off - float(tz_offset)) < 1e-6:
        base = local.date()
    else:
        base = (datetime.now(timezone.utc)
                + timedelta(hours=float(tz_offset))).date()
    if w == "yesterday":
        base -= timedelta(days=1)
    return base.isoformat()


def project_labels(data):
    return [p["id"] for p in data.get("projects", [])]


def label_of(labels, idx):
    """A daily row carries a project INDEX, never a label; the label lives in
    the payload's `projects` array at that position."""
    return labels[idx] if 0 <= idx < len(labels) else str(idx)


def positive_int(v):
    """A --last of 0 or below is a usage error, not an empty slice: rows[-0:]
    is the WHOLE list and rows[1:] silently drops the current window, which is
    the one a 'how much is left' question is about."""
    try:
        n = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f"wants an integer: {v!r}")
    if n < 1:
        raise argparse.ArgumentTypeError(f"wants N >= 1: {v!r}")
    return n


def emit(obj):
    json.dump(obj, sys.stdout, indent=1)
    sys.stdout.write("\n")


# ---------------------------------------------------------------- commands

def cmd_ask(a):
    by = [w.strip().lower() for w in a.by.split(",") if w.strip()]
    for b in by:
        if b not in GROUP_KEYS:
            die(f"unknown --by field: {b!r}; accepted: "
                + ", ".join(GROUP_KEYS))
    if not by:
        die("--by needs at least one field")

    if a.day:
        check_day_word(a.day)
    check_date_flag("--since", a.since)
    check_date_flag("--until", a.until)

    data, reused = ensure_payload(a.max_age)
    labels = project_labels(data)
    day = resolve_day(a.day, data["tz_offset"]) if a.day else None
    want = a.project.lower() if a.project else None
    # An exact label wins outright: matching exact OR substring in one pass
    # sums every label the wanted one is a prefix of, which is the collision
    # burnrate.py's distinct project labels exist to prevent.
    exact = want is not None and want in {lab.lower() for lab in labels}

    sel = []
    for r in data.get("daily", []):
        d = r[COLS["day"]]
        if day is not None and d != day:
            continue
        if a.since and d < a.since:
            continue
        if a.until and d > a.until:
            continue
        if want is not None:
            lab = label_of(labels, r[COLS["project"]]).lower()
            hit = lab == want if exact else want in lab
            if not hit:
                continue
        sel.append(r)
    if a.last is not None:
        keep = set(sorted({r[COLS["day"]] for r in sel})[-a.last:])
        sel = [r for r in sel if r[COLS["day"]] in keep]

    groups = {}
    total = dict.fromkeys(METRICS, 0)
    total["messages"] = 0
    for r in sel:
        key = tuple(label_of(labels, r[COLS[b]]) if b == "project"
                    else r[COLS[b]] for b in by)
        g = groups.get(key)
        if g is None:
            g = groups[key] = dict(zip(by, key))
            for m in METRICS:
                g[m] = 0
            g["messages"] = 0
        for m in METRICS + ("messages",):
            g[m] += r[COLS[m]]
            total[m] += r[COLS[m]]

    rows = list(groups.values())
    if by[0] == "day":
        rows.sort(key=lambda g: tuple(str(g[b]) for b in by))
    else:
        rows.sort(key=lambda g: -g["billed_equiv"])

    emit({"generated": data.get("generated"), "reused": reused,
          "tz_offset": data.get("tz_offset"),
          "filters": {"by": by, "day": day, "since": a.since,
                      "until": a.until, "project": a.project,
                      "last": a.last},
          "rows": rows, "total": total})
    return 0


def cmd_blocks(a):
    data, reused = ensure_payload(a.max_age)
    labels = project_labels(data)
    off = float(data.get("tz_offset") or 0.0)
    tz = timezone(timedelta(hours=off))

    def iso(ts):
        return datetime.fromtimestamp(ts, tz).isoformat(timespec="seconds")

    now = datetime.now(timezone.utc).timestamp()
    rows = data.get("blocks", [])
    if a.last is not None:
        rows = rows[-a.last:]
    out = []
    for t0, first, last, be, output, msgs, pp in rows:
        end = t0 + BLOCK_SECONDS
        out.append({
            "start": iso(t0), "start_epoch": t0,
            "first_activity": iso(first), "first_activity_epoch": first,
            "last_activity": iso(last), "last_activity_epoch": last,
            "window_end": iso(end), "window_end_epoch": end,
            "billed_equiv": be, "output": output, "messages": msgs,
            "active": now < end,
            # the per-project map is keyed by the STRING form of the project's
            # index into `projects`, never by its label
            "top_projects": {label_of(labels, int(i)): v
                             for i, v in pp.items()},
        })
    emit({"generated": data.get("generated"), "reused": reused,
          "tz_offset": data.get("tz_offset"), "blocks": out})
    return 0


def cmd_run(a):
    tool = find_tool()
    argv = [sys.executable, tool, "--json", "--out",
            out_dir(tool, create=not a.dry_run)]
    argv += flags_for(a.words)
    if a.no_open:
        argv.append("--no-open")
    if a.dry_run:
        print(json.dumps(argv))
        return 0
    return subprocess.run(argv).returncode


def build_parser():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True

    r = sub.add_parser("run", help="build the dashboard and open it")
    r.add_argument("words", nargs="*",
                   help="any of: " + ", ".join(sorted(WORDS)))
    r.add_argument("--no-open", action="store_true",
                   help="write the report without opening a browser")
    r.add_argument("--dry-run", action="store_true",
                   help="print the argv that would run, as JSON, and exit")
    r.set_defaults(func=cmd_run)

    k = sub.add_parser(
        "ask", help="summarize the daily rows as one JSON object on stdout",
        description="Group and filter the last run's daily rows. Rebuilds the "
                    "payload with --no-open when it is missing or stale, and "
                    "never opens a browser. `kind` is 'm' (main thread) or "
                    "'a' (subagent); an empty `command` means no command "
                    "segment was open. `cache_write` is the whole "
                    "cache-creation total and ALREADY INCLUDES "
                    "`cache_write_1h`, which is the 1-hour-TTL half of it -- "
                    "the two are nested, not disjoint, so adding them "
                    "double-counts.")
    k.add_argument("--by", default="day",
                   help="comma list of " + ", ".join(GROUP_KEYS)
                        + " (default: day)")
    k.add_argument("--day", metavar="D",
                   help="one day: YYYY-MM-DD, today or yesterday")
    k.add_argument("--since", metavar="YYYY-MM-DD")
    k.add_argument("--until", metavar="YYYY-MM-DD")
    k.add_argument("--project", metavar="LABEL",
                   help="exact project label, else a substring of one")
    k.add_argument("--last", type=positive_int, metavar="N",
                   help="keep only the N most recent days that survive the "
                        "other filters")
    k.add_argument("--max-age", type=float, default=15.0, metavar="MIN",
                   help="reuse an existing payload younger than this many "
                        "minutes (default: 15)")
    k.set_defaults(func=cmd_ask)

    b = sub.add_parser(
        "blocks", help="summarize the recent 5h rate-limit windows",
        description="The most recent 5h rate-limit windows, oldest first, as "
                    "one JSON object on stdout. Blocks are ACCOUNT-WIDE: they "
                    "cover every project at once, so no project filter applies "
                    "to them and this command takes none. Same freshness rule "
                    "as ask; never opens a browser.")
    b.add_argument("--last", type=positive_int, default=3, metavar="N",
                   help="how many of the most recent windows (default: 3)")
    b.add_argument("--max-age", type=float, default=15.0, metavar="MIN",
                   help="reuse an existing payload younger than this many "
                        "minutes (default: 15)")
    b.set_defaults(func=cmd_blocks)
    return ap


def main():
    a = build_parser().parse_args()
    raise SystemExit(a.func(a))


if __name__ == "__main__":
    main()
