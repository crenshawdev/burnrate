#!/usr/bin/env python3
"""Materialize a synthetic "stranger" Claude Code environment for burnrate's
tests.

    python3 tools/mkfixture.py <dir>

Nothing in here is user-specific: every path, project and session is invented.
The generator also writes <dir>/expected.json describing what a correct run
must produce, so the selftest asserts against hand-derived expectations rather
than against a second copy of the implementation.

Layout under <dir>:
    home/.claude/projects/            primary live tree (the ~ default)
    home/.claude/usage-logger/usage-log.jsonl
    alt/.claude/projects/             second live tree ($CLAUDE_PROJECTS)
    cfg/projects/                     third live tree ($CLAUDE_CONFIG_DIR)
    backup/                           live-shaped archive (<proj>/<sid>.jsonl)
    hindsight/archive/                zst-shaped archive (<proj>/<sid>/*.zst)
    hindsight/config.toml             base_dir pointing at <dir>/hindsight
    expected.json
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

try:
    import zstandard as zstd
except ImportError:
    zstd = None

CW5, CW1H, CR = 1.25, 2.00, 0.10

# Every assistant message uses the same cache split so billed-equiv stays an
# exact integer: be = input + 1.25*300 + 2*100 + 0.10*2000 = input + 775.
CC, CC1H, CRD, OUT = 400, 100, 2000, 300
BE_EXTRA = CW5 * (CC - CC1H) + CW1H * CC1H + CR * CRD     # 775.0

# Three probe instants, each chosen for a specific timezone disagreement.
PROBES = {
    # a different local calendar day in Tokyo than in Los Angeles
    "probe_tokyo_vs_la": "2026-03-09T02:00:00Z",
    # after the US spring-forward: local LA and a fixed -8 offset disagree
    "probe_dst": "2026-03-09T07:30:00Z",
    # UTC hour >= 15, so Tokyo (+9) rolls to the next calendar day
    "probe_tokyo_rollover": "2026-03-09T16:00:00Z",
}
# Hand-derived, verified against the OS tz database; NOT computed by the same
# code path burnrate uses.
PROBE_DAYS = {
    "probe_tokyo_vs_la": {"Asia/Tokyo": "2026-03-09",
                          "America/Los_Angeles": "2026-03-08",
                          "UTC": "2026-03-09"},
    "probe_dst": {"Asia/Tokyo": "2026-03-09",
                  "America/Los_Angeles": "2026-03-09",
                  "UTC": "2026-03-09",
                  "fixed-8": "2026-03-08"},
    "probe_tokyo_rollover": {"Asia/Tokyo": "2026-03-10",
                             "America/Los_Angeles": "2026-03-09",
                             "UTC": "2026-03-09"},
}
PROBE_EFFORT = {
    "probe_tokyo_vs_la": "probe-tvl",
    "probe_dst": "probe-dst",
    "probe_tokyo_rollover": "probe-roll",
}

# Rate-limit logger samples: (ts, five_hour, seven_day), written literally as
# the shipped wrapper writes them -- one JSON object per line whose percentages
# are SCALARS. Spread over TWO days so the fixture exercises both the viewer's
# rl.length > 1 card gate and lineChart's refusal below two points.
#
# Every sample sits in a midday-UTC band on purpose. The viewer buckets rl
# epochs into days with ONE generation-time tz_offset while Python buckets the
# daily rows per timestamp, so the two disagree by an hour across a DST
# boundary: a March sample near local midnight would land on different calendar
# days in the two bucketings, collapsing the day set to one on some machines and
# not others. 12:00Z-15:00Z is outside that hazard for every real offset.
RL_SAMPLES = [
    ("2026-03-01T12:00:00Z", 10, 30),
    ("2026-03-01T13:00:00Z", 15.5, 31),
    ("2026-03-02T12:00:00Z", 20, 32.25),
    ("2026-03-02T13:00:00Z", 25, 33),
]

# Carries all three script-data breakout vectors in one string: "</script>"
# closes the element directly, while an unclosed "<!--" followed by "<script"
# drives the tokenizer into script-data-double-escaped state, where the page's
# own </script> stops closing it. A defense that only rewrites "</" passes the
# first and fails the other two.
TITLE = "alpha </script> <!-- <script defer> <b>x</b> beta"

# (cwd, explicit dirname or None to encode the cwd, expected label, base input)
PRIMARY = [
    ("/home/alice/work/api", None, "home/alice/work/api", 1000),
    ("/home/alice/work/web", None, "web", 2000),
    ("/home/alice/personal/api", None, "personal/api", 3000),
    # folds into its parent project, contributing to home/alice/work/api
    ("/home/alice/work/api/.claude/worktrees/agent-abc", None,
     "home/alice/work/api", 4000),
    # the outlier that collapses any longest-common-prefix rule
    ("/tmp/scratch", None, "scratch", 5000),
    ("C:\\Users\\bob\\src\\tool", None, "tool", 6000),
    # a proper suffix collision with the first path: lengthening runs out of
    # segments, so the exhaustion tiebreak has to fire
    ("/mnt/old/home/alice/work/api", None, "home/alice/work/api#2", 7000),
    # no cwd anywhere in this project's lines: the dirname is the only label
    (None, "-var-tmp-nocwd-proj", "var-tmp-nocwd-proj", 8000),
]


def encode(path):
    """How Claude Code encodes a project path into a directory name: both '/'
    (and '\\') and '.' collapse to '-', which is why the dirname alone is
    structurally ambiguous."""
    out = []
    for ch in path:
        out.append("-" if ch in "/\\.:" else ch)
    return "".join(out)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def usage(inp):
    return {"input_tokens": inp,
            "cache_creation_input_tokens": CC,
            "cache_creation": {"ephemeral_5m_input_tokens": CC - CC1H,
                               "ephemeral_1h_input_tokens": CC1H},
            "cache_read_input_tokens": CRD,
            "output_tokens": OUT}


def asst(ts, mid, inp, cwd=None, effort="high", model="claude-opus-4-6"):
    d = {"type": "assistant", "timestamp": iso(ts), "uuid": "u-" + mid,
         "effort": effort,
         "message": {"id": mid, "type": "message", "role": "assistant",
                     "model": model, "usage": usage(inp)}}
    if cwd is not None:
        d["cwd"] = cwd
    return d


def write_lines(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def write_zst(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    blob = "".join(json.dumps(r) + "\n" for r in rows).encode("utf-8")
    with open(path, "wb") as fh:
        if zstd is not None:
            fh.write(zstd.ZstdCompressor().compress(blob))
        else:
            # magic bytes only: the layout sniff still classifies the tree
            fh.write(b"\x28\xb5\x2f\xfd" + blob)


def live_project(root, cwd, dirname, slug, base, day, n_sess=2, n_msg=3,
                 extras=None):
    """Write one project directory of live-shaped sessions. Returns
    (billed_equiv, message_count)."""
    pdir = os.path.join(root, dirname or encode(cwd))
    be, n = 0.0, 0
    for si in range(n_sess):
        sid = f"{slug}-sess-{si}"
        rows = []
        t0 = epoch(f"2026-03-{day:02d}T12:00:00Z") + si * 600
        if si == 0 and extras:
            rows.extend(extras(t0))
        for mi in range(n_msg):
            mid = f"msg-{slug}-{si}-{mi}"
            rows.append(asst(t0 + mi * 60, mid, base + mi, cwd))
            be += base + mi + BE_EXTRA
            n += 1
        write_lines(os.path.join(pdir, sid + ".jsonl"), rows)
    return be, n


def build(root):
    os.makedirs(root, exist_ok=True)
    home = os.path.join(root, "home")
    primary = os.path.join(home, ".claude", "projects")
    alt = os.path.join(root, "alt", ".claude", "projects")
    cfg = os.path.join(root, "cfg", "projects")
    backup = os.path.join(root, "backup")
    arch = os.path.join(root, "hindsight", "archive")

    label_be, msgs, slugs = {}, 0, []

    for idx, (cwd, dirname, label, base) in enumerate(PRIMARY):
        slug = f"p{idx}"
        slugs.append(slug)
        extras = None
        if idx == 0:
            def extras(t0, _slug=slug):
                return [
                    {"type": "user", "timestamp": iso(t0 - 30),
                     "uuid": f"u-cmd-{_slug}",
                     "message": {"role": "user",
                                 "content": "<command-name>/plan"
                                            "</command-name> go"}},
                    {"type": "ai-title", "timestamp": iso(t0 - 20),
                     "uuid": f"u-title-{_slug}", "aiTitle": TITLE},
                ]
        be, n = live_project(primary, cwd, dirname, slug, base, 1 + idx % 5,
                             extras=extras)
        label_be[label] = label_be.get(label, 0.0) + be
        msgs += n

    # a subagent under the first project's first session
    p0dir = os.path.join(primary, encode(PRIMARY[0][0]))
    sub = os.path.join(p0dir, f"{slugs[0]}-sess-0", "subagents")
    sub_ts = epoch("2026-03-01T12:05:00Z")
    write_lines(os.path.join(sub, "agent-1.jsonl"),
                [asst(sub_ts, "msg-agent-1", 900, effort="medium")])
    with open(os.path.join(sub, "agent-1.meta.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"agentType": "cad-executor"}, fh)
    label_be[PRIMARY[0][2]] += 900 + BE_EXTRA
    msgs += 1

    # the three timezone probes, carried by the 'web' project so each probe row
    # is findable by its unique effort marker
    web_dir = os.path.join(primary, encode(PRIMARY[1][0]))
    rows = []
    for name, when in PROBES.items():
        mid = f"msg-{PROBE_EFFORT[name]}"
        rows.append(asst(epoch(when), mid, 1000, PRIMARY[1][0],
                         effort=PROBE_EFFORT[name]))
        label_be[PRIMARY[1][2]] += 1000 + BE_EXTRA
        msgs += 1
    write_lines(os.path.join(web_dir, "probes-sess.jsonl"), rows)

    # rate-limit logger samples, beside the primary tree, in exactly the record
    # extras/usage_logger.sh writes: SCALAR percentages, not nested objects.
    rl = os.path.join(home, ".claude", "usage-logger", "usage-log.jsonl")
    os.makedirs(os.path.dirname(rl), exist_ok=True)
    with open(rl, "w", encoding="utf-8") as fh:
        for i, (when, five, seven) in enumerate(RL_SAMPLES):
            fh.write(json.dumps({
                "ts": when,
                "five_hour": five,
                "seven_day": seven,
                "resets5": 1772380800 + i,
                "resets7": 1772985600 + i,
                "session_id": f"rl-sess-{i}",
                "model": "Sonnet 4.6"}) + "\n")

    # the other trees
    alt_be, _ = live_project(alt, "/home/carol/labs/altonly", None, "alt",
                             1100, 2, n_sess=1, n_msg=2)
    cfg_be, _ = live_project(cfg, "/home/dave/space/cfgonly", None, "cfg",
                             1200, 2, n_sess=1, n_msg=2)
    bk_be, _ = live_project(backup, "/home/erin/backup/proj", None, "bk",
                            1300, 3, n_sess=1, n_msg=2)

    # hindsight-shaped archive: <proj>/<sid>/*.zst
    acwd = "/home/frank/arch/thing"
    arows, arch_be = [], 0.0
    t0 = epoch("2026-03-04T12:00:00Z")
    for mi in range(2):
        arows.append(asst(t0 + mi * 60, f"msg-arch-{mi}", 1400 + mi, acwd))
        arch_be += 1400 + mi + BE_EXTRA
    write_zst(os.path.join(arch, encode(acwd), "arch-sess-0", "000.zst"), arows)
    with open(os.path.join(root, "hindsight", "config.toml"), "w",
              encoding="utf-8") as fh:
        fh.write("[storage]\n")
        fh.write('base_dir = "%s"\n' % os.path.join(root, "hindsight"))

    expected = {
        "labels": sorted(label_be),
        "label_be": {k: round(v) for k, v in label_be.items()},
        "total_be": round(sum(label_be.values())),
        "messages": msgs,
        "title": TITLE,
        "worktree_label": PRIMARY[3][2],
        "nocwd_label": PRIMARY[7][2],
        "windows_label": PRIMARY[5][2],
        "suffix_pair": ["/home/alice/work/api", "/mnt/old/home/alice/work/api"],
        "alt": {"label": "altonly", "be": round(alt_be)},
        "cfg": {"label": "cfgonly", "be": round(cfg_be)},
        "backup": {"label": "proj", "be": round(bk_be)},
        "archive": {"label": "thing", "be": round(arch_be)},
        "rl_samples": len(RL_SAMPLES),
        # what read_rl_log must produce from the file above: [epoch, five,
        # seven], ascending. Written from the literals rather than through the
        # reader, so a reader that drops or reshapes a field fails here.
        "rl_rows": sorted([int(epoch(w)), f, s] for w, f, s in RL_SAMPLES),
        "zstd": zstd is not None,
        "probe_efforts": PROBE_EFFORT,
        "probe_days": PROBE_DAYS,
        "paths": {
            "home": home,
            "primary": primary,
            "alt": alt,
            "cfg_dir": os.path.join(root, "cfg"),
            "cfg": cfg,
            "backup": backup,
            "archive": arch,
            "config_home": root,
            "rl_log": rl,
        },
    }
    for name, when in PROBES.items():
        expected[name] = int(epoch(when))
    with open(os.path.join(root, "expected.json"), "w",
              encoding="utf-8") as fh:
        json.dump(expected, fh, indent=1, sort_keys=True)
    return expected


def main():
    if len(sys.argv) != 2:
        print("usage: mkfixture.py <dir>", file=sys.stderr)
        return 2
    exp = build(os.path.abspath(sys.argv[1]))
    print(f"fixture written: {len(exp['labels'])} labels, "
          f"{exp['messages']} billing messages, zstd={exp['zstd']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
