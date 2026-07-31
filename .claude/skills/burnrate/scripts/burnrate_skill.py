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
import subprocess
import sys

TOOL_NAME = "burnrate.py"

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
    guards main() behind `if __name__ == '__main__'`."""
    spec = importlib.util.spec_from_file_location("burnrate_tool", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
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
    return ap


def main():
    a = build_parser().parse_args()
    raise SystemExit(a.func(a))


if __name__ == "__main__":
    main()
