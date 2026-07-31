#!/usr/bin/env python3
"""burnrate's selftest: every case runs against the synthetic fixture only.

    python3 tools/selftest.py

Each subprocess run gets HOME, XDG_CACHE_HOME, XDG_CONFIG_HOME and
CLAUDE_CONFIG_DIR pointed inside a throwaway temp tree, so the suite never
reads a real ~/.claude tree and never writes a real cache.
"""
from __future__ import annotations

import ast
import atexit
import contextlib
import getpass
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
import warnings

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
BR = os.path.join(REPO, "burnrate.py")
EXTRAS = os.path.join(REPO, "extras")
WRAPPER = os.path.join(EXTRAS, "usage_logger.sh")
INSTALLER = os.path.join(EXTRAS, "install_usage_logger.sh")

TMP = tempfile.mkdtemp(prefix="burnrate-selftest-")
atexit.register(shutil.rmtree, TMP, ignore_errors=True)
FX = os.path.join(TMP, "fx")
subprocess.run([sys.executable, os.path.join(HERE, "mkfixture.py"), FX],
               check=True, stdout=subprocess.DEVNULL)
with open(os.path.join(FX, "expected.json"), encoding="utf-8") as _fh:
    EXP = json.load(_fh)
P = EXP["paths"]

# dirs the fixture deliberately does not ship: an empty config home (so the
# hindsight auto-detect finds nothing), an existing-but-empty transcript root
# and an existing-but-unrecognizable archive dir
EMPTY_CONF = os.path.join(TMP, "emptyconf")
EMPTY_ROOT = os.path.join(TMP, "empty-root")
EMPTY_DIR = os.path.join(TMP, "empty-dir")
for _d in (EMPTY_CONF, EMPTY_ROOT, EMPTY_DIR):
    os.makedirs(_d, exist_ok=True)

# The same trees under a directory whose own name carries glob metacharacters.
# '[', ']' and '?' are legal in a path on every OS burnrate targets, and a
# project cwd like /home/alice/[work]/api encodes to a dirname holding them.
ODD = os.path.join(TMP, "od[d]?dir")
ODD_ROOT = os.path.join(ODD, "projects")
ODD_ARCH = os.path.join(ODD, "archive")
shutil.copytree(P["primary"], ODD_ROOT)
shutil.copytree(P["archive"], ODD_ARCH)

# one directory carrying BOTH shapes: <proj>/*.jsonl beside <proj>/<sid>/*.zst,
# which is what dropping a backup tree next to an archive (or archiving a tree
# in place) leaves behind
MIXED = os.path.join(TMP, "mixed")
shutil.copytree(P["backup"], MIXED)
shutil.copytree(P["archive"], MIXED, dirs_exist_ok=True)

# Layout-shaped stand-ins whose "zst" chunks are four magic bytes and nothing
# more. classify_tree and both discovery walks read names, never contents, so
# these exercise the layout and zstandard-absent branches identically on a
# machine that has zstandard and one that does not -- which is the point: the
# cases below fire exactly where the real fixture has to skipTest.
FAKE_ARCH = os.path.join(TMP, "fake-arch")
FAKE_ARCH2 = os.path.join(TMP, "fake-arch-2")
FAKE_MIXED = os.path.join(TMP, "fake-mixed")
ZMAGIC = b"\x28\xb5\x2f\xfd"


def _write(path, body=b""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(body)


for _d in (FAKE_ARCH, FAKE_ARCH2):
    _write(os.path.join(_d, "-home-alice-work-api", "sess-1", "000.zst"),
           ZMAGIC)
_write(os.path.join(FAKE_MIXED, "-home-alice-work-api", "sess-1.jsonl"))
_write(os.path.join(FAKE_MIXED, "-home-alice-work-api", "sess-2", "000.zst"),
       ZMAGIC)
# a stray subdirectory holding no chunk at all, in both shapes of tree, and a
# session whose only chunks belong to its subagents
for _d in (FAKE_ARCH, FAKE_MIXED):
    _write(os.path.join(_d, "-home-alice-work-api", "notes-dir", "notes.txt"),
           b"not a session\n")
_write(os.path.join(FAKE_ARCH, "-home-alice-work-api", "sess-2", "subagents",
                    "agent-1", "000.zst"), ZMAGIC)
# an archive whose EVERY session is subagent-only: no <proj>/<sid>/*.zst
# anywhere in the tree. FAKE_ARCH cannot stand in for this -- it passes the
# layout sniff on sess-1's own chunk -- so the subagent-only guard was proved
# against a tree shape the resolver itself refused.
FAKE_SUBONLY = os.path.join(TMP, "fake-subonly")
_write(os.path.join(FAKE_SUBONLY, "-home-alice-work-api", "sess-2",
                    "subagents", "agent-1", "000.zst"), ZMAGIC)

# Stand-in browsers for the launch cases, so nothing here can open a real
# window. Both exit 0: webbrowser stops at the first registered entry that
# succeeds, so exit 0 is what keeps it from falling through to a real browser.
# Deliberately NOT redirecting their inherited stdout/stderr: a real browser
# does not, and a stand-in that does hides the defect these cases exist to
# catch -- a child holding the caller's captured pipe open for its whole
# lifetime. The launch has to detach the child itself.
# Each stand-in appends the URI it was handed to $MARKER, so a test can prove a
# browser was actually reached. Without that, every case here passes against an
# open_report() that returns True having launched nothing.
FAST_BROWSER = os.path.join(TMP, "fast-browser")
SLOW_BROWSER = os.path.join(TMP, "slow-browser")
DEAD_BROWSER = os.path.join(TMP, "no-such-browser")  # deliberately never created
for _p, _body in ((FAST_BROWSER, "exit 0\n"), (SLOW_BROWSER, "sleep 8\nexit 0\n")):
    with open(_p, "w", encoding="utf-8") as _fh:
        _fh.write('#!/bin/sh\n[ -n "$MARKER" ] && printf \'%s\\n\' "$1" >> "$MARKER"\n'
                  + _body)
    os.chmod(_p, 0o755)

_spec = importlib.util.spec_from_file_location("burnrate_under_test", BR)
br = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(br)


def base_env(**over):
    env = dict(os.environ)
    for k in ("CLAUDE_PROJECTS", "CLAUDE_CONFIG_DIR", "TZ", "BROWSER"):
        env.pop(k, None)
    env["HOME"] = P["home"]
    env["XDG_CACHE_HOME"] = os.path.join(TMP, "cache")
    env["XDG_CONFIG_HOME"] = EMPTY_CONF
    for k, v in over.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return env


def run(args=(), env=None, out=None, flags=("--json", "--no-open", "--quiet")):
    out = out or tempfile.mkdtemp(dir=TMP)
    proc = subprocess.run(
        [sys.executable, BR, "--out", out, *flags, *args],
        env=env if env is not None else base_env(),
        capture_output=True, text=True)
    return proc, out


@contextlib.contextmanager
def zstd_as(value):
    """Pin burnrate's optional zstandard import for the duration of a case, so
    the branches that turn on its presence are exercised the same way whether
    or not this machine has it installed."""
    old = br.zstd
    br.zstd = value
    try:
        yield
    finally:
        br.zstd = old


def resolve(**kw):
    """resolve_sources in-process: (result, stderr text). The environment is
    supplied whole so the resolver never reaches the real ~/.claude tree."""
    kw.setdefault("archive", None)
    kw.setdefault("no_archive", False)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        src = br.resolve_sources(types.SimpleNamespace(**kw),
                                 env={"HOME": P["home"],
                                      "XDG_CONFIG_HOME": EMPTY_CONF})
    return src, err.getvalue()


def payload(out):
    with open(os.path.join(out, "dashboard_data.json"), encoding="utf-8") as fh:
        return json.load(fh)


def total_be(data):
    return sum(p["be"] for p in data["projects"])


class TestRootResolution(unittest.TestCase):
    """D-04/D-16: --root > $CLAUDE_PROJECTS > $CLAUDE_CONFIG_DIR/projects >
    ~/.claude/projects, and the rate-limit log follows the resolved root."""

    def test_home_default(self):
        proc, out = run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(total_be(payload(out)), EXP["total_be"])

    def test_claude_projects_env(self):
        proc, out = run(env=base_env(CLAUDE_PROJECTS=P["alt"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(total_be(payload(out)), EXP["alt"]["be"])

    def test_claude_config_dir_env(self):
        proc, out = run(env=base_env(CLAUDE_CONFIG_DIR=P["cfg_dir"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(total_be(payload(out)), EXP["cfg"]["be"])

    def test_root_flag_beats_env(self):
        proc, out = run(["--root", P["cfg"]],
                        env=base_env(CLAUDE_PROJECTS=P["alt"],
                                     CLAUDE_CONFIG_DIR=P["cfg_dir"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(total_be(payload(out)), EXP["cfg"]["be"])

    def test_claude_projects_beats_config_dir(self):
        proc, out = run(env=base_env(CLAUDE_PROJECTS=P["alt"],
                                     CLAUDE_CONFIG_DIR=P["cfg_dir"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(total_be(payload(out)), EXP["alt"]["be"])

    def test_rl_log_follows_the_root(self):
        _, out = run()
        self.assertEqual(len(payload(out)["rl"]), EXP["rl_samples"])
        self.assertTrue(payload(out)["rl_installed"])
        _, out2 = run(["--root", P["alt"]])
        self.assertEqual(payload(out2)["rl"], [])
        self.assertFalse(payload(out2)["rl_installed"])

    def test_rl_rows_carry_the_shape_the_logger_writes(self):
        """A row count says nothing about the row. The logger writes SCALAR
        percentages; against nested {"used_percentage": n} objects the payload
        still carries four rows, and the chart's `v > 0` test then fails on
        every one of them and renders 'Not enough data in range'."""
        _, out = run()
        rl = payload(out)["rl"]
        self.assertEqual(rl, EXP["rl_rows"])
        for ts, five, seven in rl:
            self.assertIsInstance(ts, int)
            for v in (five, seven):
                self.assertIsInstance(v, (int, float))
                self.assertNotIsInstance(v, bool)

    def test_missing_root_exits_two_with_its_own_message(self):
        # match the MESSAGE: argparse's own usage error is also exit 2, so an
        # exit-code-only assertion would pass with --root never wired up
        proc, _ = run(["--root", os.path.join(FX, "nope")])
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("transcript root not found", proc.stderr)

    def test_an_archive_shaped_root_is_read_not_silently_empty(self):
        # --root pointed at a zst archive rendered 0 projects, exit 0 and no
        # stderr at all: the layout sniff was applied to --archive but never to
        # the primary root
        if not EXP["zstd"]:
            self.skipTest("zstandard not installed")
        proc, out = run(["--root", P["archive"]])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("no project directories under", proc.stderr)
        self.assertEqual(total_be(payload(out)), EXP["archive"]["be"])

    def test_a_root_holding_no_transcripts_warns(self):
        # the emptiness guard asked only whether subdirectories exist, so a
        # root full of directories that hold no sessions passed it in silence
        d = os.path.join(TMP, "junk-root")
        os.makedirs(os.path.join(d, "a-project"), exist_ok=True)
        with open(os.path.join(d, "a-project", "notes.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("not a transcript\n")
        proc, out = run(["--root", d])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("no project directories under", proc.stderr)
        self.assertEqual(payload(out)["projects"], [])

    def test_empty_root_warns_and_continues(self):
        proc, out = run(["--root", EMPTY_ROOT])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("no project directories under", proc.stderr)
        self.assertEqual(payload(out)["projects"], [])


class TestArchiveSources(unittest.TestCase):
    """D-11: --archive sniffs the layout, so a plain backup of a transcript
    tree contributes rows instead of silently contributing none."""

    def test_classify_tree(self):
        self.assertEqual(br.classify_tree(P["primary"]), ("live",))
        self.assertEqual(br.classify_tree(P["backup"]), ("live",))
        self.assertEqual(br.classify_tree(P["archive"]), ("arch",))
        self.assertEqual(br.classify_tree(EMPTY_DIR), ())
        self.assertEqual(br.classify_tree(os.path.join(TMP, "nope")), ())

    def test_classify_tree_accepts_a_subagent_only_archive(self):
        # The sniff and the discovery walk have to agree on what a tree holds.
        # _discover_arch reads a session whose only chunks are its subagents',
        # but the sniff looked for <proj>/<sid>/*.zst alone, so a tree of
        # nothing but those classified as (): passed as --root it warned "no
        # project directories" and was read live-shaped for zero sessions, and
        # passed as --archive it took the "matches no known layout" branch.
        self.assertEqual(br.classify_tree(FAKE_SUBONLY), ("arch",))

    def test_a_subagent_only_archive_is_not_skipped(self):
        with zstd_as(object()):
            src, err = resolve(root=P["primary"], archive=FAKE_SUBONLY)
        self.assertIn(("arch", FAKE_SUBONLY), src["sources"])
        self.assertTrue(src["archive_used"])
        self.assertNotIn("matches no known layout", err)

    def test_classify_tree_reports_both_shapes(self):
        # stopping at the first match dropped the zst half of a mixed tree
        self.assertEqual(br.classify_tree(MIXED), ("live", "arch"))

    def test_a_mixed_archive_contributes_both_halves(self):
        if not EXP["zstd"]:
            self.skipTest("zstandard not installed")
        _, base = run()
        proc, out = run(["--archive", MIXED])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = payload(out)
        self.assertTrue(data["archive_used"])
        # both halves, not just the jsonl one the old sniff stopped at
        self.assertEqual(total_be(data), total_be(payload(base))
                         + EXP["backup"]["be"] + EXP["archive"]["be"])
        ids = {p["id"] for p in data["projects"]}
        self.assertIn(EXP["backup"]["label"], ids)
        self.assertIn(EXP["archive"]["label"], ids)

    def test_a_mixed_root_is_not_an_archive(self):
        # archive_used follows the source PATHS: a root holding both shapes
        # contributes two sources and no archive at all
        if not EXP["zstd"]:
            self.skipTest("zstandard not installed")
        proc, out = run(["--root", MIXED])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = payload(out)
        self.assertFalse(data["archive_used"])
        self.assertIsNone(data["archive_label"])
        self.assertEqual(total_be(data),
                         EXP["backup"]["be"] + EXP["archive"]["be"])

    def test_live_shaped_archive_adds_rows(self):
        _, base = run()
        proc, out = run(["--archive", P["backup"]])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = payload(out)
        self.assertTrue(data["archive_used"])
        self.assertEqual(data["archive_label"], "archive: backup")
        self.assertEqual(total_be(data),
                         total_be(payload(base)) + EXP["backup"]["be"])

    def test_zst_shaped_archive_is_accepted(self):
        if not EXP["zstd"]:
            self.skipTest("zstandard not installed")
        _, base = run()
        proc, out = run(["--archive", P["archive"]])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = payload(out)
        self.assertTrue(data["archive_used"])
        self.assertEqual(total_be(data),
                         total_be(payload(base)) + EXP["archive"]["be"])

    def test_hindsight_autodetect(self):
        if not EXP["zstd"]:
            self.skipTest("zstandard not installed")
        _, base = run()
        proc, out = run(env=base_env(XDG_CONFIG_HOME=P["config_home"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = payload(out)
        self.assertTrue(data["archive_used"])
        self.assertEqual(data["archive_label"], "hindsight archive")
        self.assertEqual(total_be(data),
                         total_be(payload(base)) + EXP["archive"]["be"])

    def test_no_archive_suppresses_autodetect(self):
        proc, out = run(["--no-archive"],
                        env=base_env(XDG_CONFIG_HOME=P["config_home"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = payload(out)
        self.assertFalse(data["archive_used"])
        self.assertIsNone(data["archive_label"])

    def test_no_archive_drops_the_roots_own_arch_half(self):
        # the flag promises "every archive source, auto-detected or not", but
        # the sniff that classifies the ROOT ran ahead of it: a tree archived
        # in place had its zst frames decompressed and counted anyway, under a
        # header still reading "live transcripts only"
        with zstd_as(object()):
            src, err = resolve(root=FAKE_MIXED, no_archive=True)
        self.assertEqual(src["sources"], [("live", FAKE_MIXED)])
        self.assertIn("--no-archive skips the archived transcripts", err)

    def test_without_the_flag_a_mixed_root_keeps_both_halves(self):
        # the control: suppression must come from --no-archive, not from the
        # root sniff quietly forgetting the arch half for everyone
        with zstd_as(object()):
            src, _ = resolve(root=FAKE_MIXED)
        self.assertEqual(src["sources"],
                         [("live", FAKE_MIXED), ("arch", FAKE_MIXED)])

    def test_no_archive_on_a_mixed_root_reports_the_live_half_only(self):
        if not EXP["zstd"]:
            self.skipTest("zstandard not installed")
        proc, out = run(["--root", MIXED, "--no-archive"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = payload(out)
        self.assertFalse(data["archive_used"])
        self.assertEqual(total_be(data), EXP["backup"]["be"])

    def test_absent_config_completes_without_archive(self):
        proc, out = run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(payload(out)["archive_used"])

    def test_unknown_layout_warns_and_continues(self):
        proc, out = run(["--archive", EMPTY_DIR])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("matches no known layout", proc.stderr)
        self.assertFalse(payload(out)["archive_used"])

    def test_missing_archive_dir_exits_two(self):
        proc, _ = run(["--archive", os.path.join(TMP, "no-such-archive")])
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("archive directory not found", proc.stderr)


class TestDiscovery(unittest.TestCase):
    """A session exists because a file feeding it exists, not because a
    directory does. The arch walk instantiated sessions[proj][sid] on the
    defaultdict before checking for a chunk, and now that the walk also runs
    over live-shaped roots, every stray subdirectory under a project became a
    session -- one whose empty signature matches forever, so the cache kept
    it."""

    PROJ = "-home-alice-work-api"

    def test_a_stray_directory_is_not_a_session(self):
        found = br.discover([("arch", FAKE_ARCH)])
        self.assertEqual(sorted(found[self.PROJ]), ["sess-1", "sess-2"])

    def test_a_stray_directory_under_a_mixed_root_is_not_a_session(self):
        # the reported shape: a root carrying both layouts plus one scratch dir
        found = br.discover([("live", FAKE_MIXED), ("arch", FAKE_MIXED)])
        self.assertEqual(sorted(found[self.PROJ]), ["sess-1", "sess-2"])

    def test_a_subagent_only_session_still_counts(self):
        # The other side of the guard: no top-level chunk, but real subagent
        # transcripts, so the session is real. Run against a tree the resolver
        # actually hands to the walk -- discovering a session inside a tree the
        # tool refuses to accept proves nothing a user can reach.
        with zstd_as(object()):
            src, err = resolve(root=FAKE_SUBONLY)
        self.assertEqual(src["sources"], [("arch", FAKE_SUBONLY)])
        self.assertNotIn("no project directories", err)
        s = br.discover(src["sources"])[self.PROJ]["sess-2"]
        self.assertEqual(s["main"], [])
        self.assertIn("agent-1", s["subs"])


class TestZstandardAbsent(unittest.TestCase):
    """The optional dependency is missing on most machines, so every branch
    that drops an arch source has to hold there -- which is precisely where the
    fixture-driven cases skipTest. These pin zstd themselves instead."""

    CENV = {"XDG_CACHE_HOME": os.path.join(TMP, "cache")}

    def test_an_arch_root_keeps_its_own_source(self):
        with zstd_as(None):
            src, err = resolve(root=FAKE_ARCH)
        # the root is never dropped out of the list: cache_path keys off it
        self.assertEqual(src["sources"], [("live", FAKE_ARCH)])
        self.assertFalse(src["archive_used"])
        # ... and the message calls it a root, not "the archive": the user
        # passed --root and no archive flag at all
        self.assertIn(FAKE_ARCH, err)
        self.assertNotIn("-- archive skipped", err)

    def test_two_arch_roots_do_not_collide_on_one_cache_file(self):
        with zstd_as(None):
            a, _ = resolve(root=FAKE_ARCH)
            b, _ = resolve(root=FAKE_ARCH2)
        pa = br.cache_path(a["sources"], platform="linux", env=self.CENV)
        pb = br.cache_path(b["sources"], platform="linux", env=self.CENV)
        empty = br.cache_path([], platform="linux", env=self.CENV)
        self.assertNotEqual(pa, pb)
        # an emptied source list hashed the empty string, so every such run
        # shared sha256("")[:12] and overwrote the last one's sessions
        self.assertNotIn(empty, (pa, pb))

    def test_a_dropped_archive_still_says_archive(self):
        with zstd_as(None):
            src, err = resolve(root=P["primary"], archive=FAKE_ARCH)
        self.assertEqual(src["sources"], [("live", P["primary"])])
        self.assertFalse(src["archive_used"])
        self.assertIn("zstandard not installed -- archive skipped", err)


class TestGlobMetacharacters(unittest.TestCase):
    """Paths are data, not patterns: a root, an archive or a project dirname
    holding '[', ']', '?' or '*' must walk exactly like any other path. Passed
    to glob.glob() unescaped it becomes a character class matching nothing, so
    the tree reports zero sessions -- with a "no project directories" warning
    that misstates the cause."""

    def test_a_root_whose_path_holds_metacharacters_is_read(self):
        proc, out = run(["--root", ODD_ROOT])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("no project directories under", proc.stderr)
        self.assertEqual(total_be(payload(out)), EXP["total_be"])
        self.assertEqual(sorted(p["id"] for p in payload(out)["projects"]),
                         EXP["labels"])

    def test_an_archive_whose_path_holds_metacharacters_is_read(self):
        if not EXP["zstd"]:
            self.skipTest("zstandard not installed")
        _, base = run()
        proc, out = run(["--archive", ODD_ARCH])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("matches no known layout", proc.stderr)
        self.assertEqual(total_be(payload(out)),
                         total_be(payload(base)) + EXP["archive"]["be"])

    def test_classify_tree_survives_metacharacters(self):
        self.assertEqual(br.classify_tree(ODD_ROOT), ("live",))
        self.assertEqual(br.classify_tree(ODD_ARCH), ("arch",))

    def test_dglob_escapes_only_the_directory(self):
        d = os.path.join(TMP, "dg[1]")
        os.makedirs(d, exist_ok=True)
        for name in ("a.jsonl", "b.jsonl", "c.txt"):
            with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                fh.write("x")
        self.assertEqual(sorted(os.path.basename(p)
                                for p in br.dglob(d, "*.jsonl")),
                         ["a.jsonl", "b.jsonl"])


class TestLabels(unittest.TestCase):
    """D-01/D-02/D-12/D-13: labels come from the real cwd in the transcripts,
    worktrees fold into their parent, and only colliding labels lengthen."""

    @classmethod
    def setUpClass(cls):
        _, out = run()
        cls.data = payload(out)

    def test_label_set_matches_the_fixture(self):
        # sorted on both sides: burnrate ranks projects by descending
        # billed-equiv, expected.json stores them sorted
        self.assertEqual(sorted(p["id"] for p in self.data["projects"]),
                         EXP["labels"])

    def test_each_label_carries_its_own_numbers(self):
        # sorting proves the label SET; this pins the mapping sorting discards
        got = {p["id"]: p["be"] for p in self.data["projects"]}
        self.assertEqual(got, EXP["label_be"])

    def test_worktree_folds_into_its_parent(self):
        ids = [p["id"] for p in self.data["projects"]]
        self.assertIn(EXP["worktree_label"], ids)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertFalse([i for i in ids if "worktrees" in i])

    def test_backslash_path_and_cwdless_dir(self):
        ids = {p["id"] for p in self.data["projects"]}
        self.assertIn(EXP["windows_label"], ids)      # C:\Users\bob\src\tool
        self.assertIn(EXP["nocwd_label"], ids)        # verbatim dirname

    def test_only_colliding_labels_lengthen(self):
        got = br.label_paths(["/a/b/api", "/c/d/api", "/x/y/web"])
        self.assertEqual(got, {"/a/b/api": "b/api", "/c/d/api": "d/api",
                               "/x/y/web": "web"})

    def test_suffix_exhaustion_still_yields_distinct_labels(self):
        pair = EXP["suffix_pair"]
        got = br.label_paths(pair)
        self.assertEqual(len(set(got.values())), 2, got)
        self.assertEqual(got[pair[0]], "home/alice/work/api")
        self.assertEqual(got[pair[1]], "home/alice/work/api#2")

    def test_the_tiebreak_cannot_land_on_a_label_already_taken(self):
        # '#' is a legal path character, so a project's own last segment can be
        # spelled exactly like the exhaustion tiebreak's output. Appending '#2'
        # without checking it against the other labels produced two entities
        # sharing one label -- two projects' totals fused into a single row.
        got = br.label_paths(["api", "/a/api", "/x/api#2"])
        self.assertEqual(len(set(got.values())), 3, got)
        self.assertEqual(got["/x/api#2"], "api#2")   # its own label, unmoved
        self.assertEqual(got["api"], "api")          # the shortest path keeps
        self.assertEqual(got["/a/api"], "api#3")     # the tiebreak steps past

    def test_a_colliding_group_cannot_eat_another_groups_base_label(self):
        # the previous guard pre-claimed only the SINGLETON labels, so a
        # colliding group processed earlier still consumed the base label a
        # later colliding group owned: the path literally named 'api#2' was
        # pushed to 'api#2#2' while a path named 'api' took its spelling.
        # Distinctness held, so no totals fused -- but the promise that a path
        # keeps its own label and the #N moves instead did not.
        got = br.label_paths(["api", "/a/api", "api#2", "/z/api#2"])
        self.assertEqual(len(set(got.values())), 4, got)
        self.assertEqual(got["api"], "api")          # shortest path keeps
        self.assertEqual(got["api#2"], "api#2")      # its own label, unmoved
        self.assertEqual(got["/a/api"], "api#3")     # the tiebreak steps past
        self.assertEqual(got["/z/api#2"], "api#2#2")

    def test_labels_are_always_distinct(self):
        cases = [
            ["/a/api", "/b/api", "/c/api"],
            ["/home/alice/work/api", "/mnt/old/home/alice/work/api",
             "/srv/home/alice/work/api"],
            ["C:\\Users\\bob\\src\\tool", "/home/bob/src/tool"],
            ["/tmp/scratch"],
            ["opaque-dirname", "/x/opaque-dirname"],
            ["/a/b/c", "/a/b/c"],
            # the exhaustion tiebreak's own output as somebody else's label
            ["api", "/a/api", "/x/api#2"],
            ["api", "/a/api", "/x/api#2", "/y/api#3"],
            # the tiebreak's output owned by a path in another COLLIDING group
            ["api", "/a/api", "api#2", "/z/api#2"],
            ["api", "/a/api", "api#2", "/z/api#2", "api#3"],
        ]
        for paths in cases:
            got = br.label_paths(paths)
            self.assertEqual(len(set(got.values())), len(set(paths)), got)

    def test_worktree_cwd_canonicalizes_to_the_parent(self):
        self.assertEqual(
            br.canon_path("/home/alice/work/api/.claude/worktrees/agent-abc"),
            "/home/alice/work/api")
        self.assertEqual(br.canon_path("/home/alice/work/api"),
                         "/home/alice/work/api")
        self.assertEqual(br.canon_path("C:\\Users\\bob\\src\\tool"),
                         "C:/Users/bob/src/tool")


class TestTimezone(unittest.TestCase):
    """D-03/D-14/D-15: days bucket in the OS's local time by default and follow
    DST; --tz-offset gives a fixed-offset bucketing instead."""

    EFF = EXP["probe_efforts"]

    def days_by_effort(self, data):
        """{effort marker: day string} for the probe rows only."""
        out = {}
        for row in data["daily"]:
            day, effort = row[0], row[4]
            if effort in self.EFF.values():
                out.setdefault(effort, set()).add(day)
        return {k: sorted(v) for k, v in out.items()}

    def placement(self, args=(), tz=None):
        env = base_env(TZ=tz) if tz else base_env()
        proc, out = run(args, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = payload(out)
        got = self.days_by_effort(data)
        self.assertEqual(len(got), 3, got)
        return {probe: got[eff][0] for probe, eff in self.EFF.items()}, data

    def test_probe_rows_land_where_the_zone_says(self):
        for zone, key in (("Asia/Tokyo", "Asia/Tokyo"),
                          ("America/Los_Angeles", "America/Los_Angeles")):
            got, _ = self.placement(tz=zone)
            for probe, days in EXP["probe_days"].items():
                self.assertEqual(got[probe], days[key],
                                 f"{probe} under {zone}")

    def test_fixed_offset_zero_matches_utc(self):
        got, data = self.placement(["--tz-offset", "0"], tz="Asia/Tokyo")
        for probe, days in EXP["probe_days"].items():
            self.assertEqual(got[probe], days["UTC"], probe)
        self.assertEqual(data["tz_offset"], 0.0)

    def test_tokyo_rolls_a_day_past_a_fixed_zero_offset(self):
        tokyo, _ = self.placement(tz="Asia/Tokyo")
        utc, _ = self.placement(["--tz-offset", "0"], tz="Asia/Tokyo")
        self.assertEqual(tokyo["probe_tokyo_rollover"], "2026-03-10")
        self.assertEqual(utc["probe_tokyo_rollover"], "2026-03-09")

    def test_tokyo_and_los_angeles_disagree(self):
        tokyo, _ = self.placement(tz="Asia/Tokyo")
        la, _ = self.placement(tz="America/Los_Angeles")
        self.assertNotEqual(tokyo["probe_tokyo_vs_la"], la["probe_tokyo_vs_la"])

    def test_local_time_follows_dst_a_fixed_offset_cannot(self):
        # 2026-03-09T07:30Z is after the US spring-forward, so PDT (-7) and a
        # fixed -8 land on different calendar days
        local, _ = self.placement(tz="America/Los_Angeles")
        fixed, data = self.placement(["--tz-offset", "-8"],
                                     tz="America/Los_Angeles")
        self.assertEqual(local["probe_dst"], "2026-03-09")
        self.assertEqual(fixed["probe_dst"], EXP["probe_days"]["probe_dst"]
                         ["fixed-8"])
        self.assertEqual(data["tz_offset"], -8.0)

    def test_an_impossible_offset_fails_like_any_other_bad_argument(self):
        # unvalidated, the value survived argparse and raised a ValueError out
        # of aggregate() -- a traceback and exit 1 AFTER parsing the whole tree
        for bad in ("24", "-24", "99", "nan", "inf", "abc"):
            proc, _ = run(["--tz-offset", bad])
            self.assertEqual(proc.returncode, 2, f"{bad}: {proc.stderr}")
            self.assertIn("--tz-offset", proc.stderr)
            self.assertIn("usage:", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)

    def test_a_legal_extreme_offset_still_runs(self):
        proc, out = run(["--tz-offset", "13.75"])   # Chatham, in DST
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(payload(out)["tz_offset"], 13.75)

    def test_default_payload_offset_is_the_local_offset(self):
        _, data = self.placement(tz="Asia/Tokyo")
        self.assertEqual(data["tz_offset"], 9.0)


class TestCache(unittest.TestCase):
    """D-05/D-06: the cache lives in the platform cache dir, one file per
    source set, keyed on realpath'd source paths."""

    def _cachedir(self):
        c = tempfile.mkdtemp(dir=TMP)
        return c, os.path.join(c, "burnrate")

    def _files(self, d):
        return sorted(f for f in os.listdir(d)
                      if f.startswith("sessions-") and f.endswith(".json.gz"))

    def test_xdg_cache_home_is_honored(self):
        c, bd = self._cachedir()
        proc, _ = run(env=base_env(XDG_CACHE_HOME=c))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self._files(bd)), 1, self._files(bd))

    def test_two_roots_two_files_neither_evicts_the_other(self):
        c, bd = self._cachedir()
        env = base_env(XDG_CACHE_HOME=c)
        run(["--root", P["primary"]], env=env)
        run(["--root", P["alt"]], env=env)
        self.assertEqual(len(self._files(bd)), 2, self._files(bd))
        # back to the first root: still entirely warm
        proc, _ = run(["--root", P["primary"]], env=env,
                      flags=("--json", "--no-open"))
        self.assertIsNotNone(re.search(r"\b0 parsed\b", proc.stderr),
                             proc.stderr)

    def test_trailing_separator_shares_one_cache_file(self):
        c, bd = self._cachedir()
        env = base_env(XDG_CACHE_HOME=c)
        run(["--root", P["primary"]], env=env)
        proc, _ = run(["--root", P["primary"] + os.sep], env=env,
                      flags=("--json", "--no-open"))
        self.assertEqual(len(self._files(bd)), 1, self._files(bd))
        self.assertIsNotNone(re.search(r"\b0 parsed\b", proc.stderr),
                             proc.stderr)

    @unittest.skipIf(not hasattr(os, "symlink"), "needs symlinks")
    def test_a_symlinked_root_reuses_the_targets_cache_entries(self):
        # ~/.claude symlinked into a dotfiles checkout is the realistic case.
        # cache_path already realpaths, so both runs share one file -- which is
        # exactly why session_sig has to realpath too: recording the unresolved
        # paths makes each run rewrite the other's entries and reparse the whole
        # tree, forever. A trailing separator alone cannot catch that; abspath
        # normalizes it, and only a symlink separates abspath from realpath.
        c, bd = self._cachedir()
        env = base_env(XDG_CACHE_HOME=c)
        link = os.path.join(tempfile.mkdtemp(dir=TMP), "projects-link")
        os.symlink(P["primary"], link)
        cold, _ = run(["--root", P["primary"]], env=env)
        self.assertEqual(cold.returncode, 0, cold.stderr)
        warm, out = run(["--root", link], env=env,
                        flags=("--json", "--no-open"))
        self.assertEqual(warm.returncode, 0, warm.stderr)
        self.assertEqual(len(self._files(bd)), 1, self._files(bd))
        self.assertIsNotNone(re.search(r"\b0 parsed\b", warm.stderr),
                             warm.stderr)
        self.assertEqual(total_be(payload(out)), EXP["total_be"])

    def test_cold_then_warm_is_byte_identical(self):
        c, _ = self._cachedir()
        env = base_env(XDG_CACHE_HOME=c)
        cold, out1 = run(env=env, flags=("--json", "--no-open"))
        warm, out2 = run(env=env, flags=("--json", "--no-open"))
        self.assertEqual(cold.returncode, 0, cold.stderr)
        self.assertIsNone(re.search(r"\b0 parsed\b", cold.stderr), cold.stderr)
        self.assertIsNotNone(re.search(r"\b0 parsed\b", warm.stderr),
                             warm.stderr)
        a, b = payload(out1), payload(out2)
        a.pop("generated")
        b.pop("generated")
        self.assertEqual(a, b)

    @unittest.skipIf(os.name != "posix" or os.geteuid() == 0,
                     "needs POSIX modes and a non-root user")
    def test_an_unwritable_cache_does_not_cost_the_report(self):
        # the cache is written from inside collect(), before the report exists,
        # so an unwritable cache directory (read-only $HOME, full disk, a
        # root-owned ~/.cache/burnrate from an earlier sudo run) used to end a
        # run whose sessions all parsed fine with a traceback and no output
        c = tempfile.mkdtemp(dir=TMP)
        os.chmod(c, 0o500)
        try:
            proc, out = run(env=base_env(XDG_CACHE_HOME=c))
        finally:
            os.chmod(c, 0o700)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("cache not written", proc.stderr)
        self.assertTrue(os.path.exists(os.path.join(out, "dashboard.html")))
        self.assertEqual(total_be(payload(out)), EXP["total_be"])

    def test_no_archive_run_owns_its_own_cache_file(self):
        if not EXP["zstd"]:
            self.skipTest("zstandard not installed")
        c, bd = self._cachedir()
        env = base_env(XDG_CACHE_HOME=c, XDG_CONFIG_HOME=P["config_home"])
        run(env=env)
        run(["--no-archive"], env=env)
        self.assertEqual(len(self._files(bd)), 2, self._files(bd))

    def test_the_cache_key_distinguishes_a_paths_layout(self):
        # Keying on paths alone aliased two source lists that discover
        # different things. An exhaustive resolve_sources matrix turns up
        # exactly two aliased keys and both are this shape: the layout at the
        # ROOT differs and nothing else does.
        env = {"XDG_CACHE_HOME": os.path.join(TMP, "cache")}

        def key(sources):
            return br.cache_path(sources, platform="linux", env=env)

        self.assertNotEqual(key([("arch", FAKE_ARCH)]),
                            key([("live", FAKE_ARCH)]))
        self.assertNotEqual(key([("arch", FAKE_ARCH), ("live", FAKE_ARCH2)]),
                            key([("live", FAKE_ARCH), ("live", FAKE_ARCH2)]))
        # ...and at a NON-first position too: keying the layout onto the first
        # entry alone would leave this pair aliased. Reachable without an odd
        # flag -- `--root L --archive X` where the user later archives X in
        # place and deletes the .jsonl originals.
        self.assertNotEqual(key([("live", FAKE_ARCH), ("live", FAKE_ARCH2)]),
                            key([("live", FAKE_ARCH), ("arch", FAKE_ARCH2)]))

    def test_a_path_cannot_forge_a_cache_key_boundary(self):
        # A tab or a newline is legal in a POSIX path, so joining the key on
        # those delimiters lets one source impersonate two. Components are
        # hashed individually instead.
        env = {"XDG_CACHE_HOME": os.path.join(TMP, "cache")}

        def key(sources):
            return br.cache_path(sources, platform="linux", env=env)

        self.assertNotEqual(key([("live", "/a\narch\t/b")]),
                            key([("live", "/a"), ("arch", "/b")]))

    def test_an_arch_only_root_is_not_evicted_by_its_no_archive_run(self):
        # An arch-only root resolves to [("arch", R)]; --no-archive strips that
        # half and the fallback re-inserts ("live", R), which discovers nothing
        # under an arch-shaped tree. Under a path-only cache key those two
        # lists named the SAME file, so the empty run wrote an empty session
        # map over the full run's cache and the next real run went cold again.
        # The same alias fires with no flag at all whenever zstandard stops
        # being importable between two runs.
        if not EXP["zstd"]:
            self.skipTest("zstandard not installed")
        c, bd = self._cachedir()
        env = base_env(XDG_CACHE_HOME=c)  # EMPTY_CONF: no hindsight autodetect
        flags = ("--json", "--no-open")
        cold, _ = run(["--root", P["archive"]], env=env, flags=flags)
        self.assertEqual(cold.returncode, 0, cold.stderr)
        self.assertIsNone(re.search(r"\b0 parsed\b", cold.stderr), cold.stderr)
        empty, _ = run(["--root", P["archive"], "--no-archive"], env=env,
                       flags=flags)
        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertEqual(len(self._files(bd)), 2, self._files(bd))
        warm, _ = run(["--root", P["archive"]], env=env, flags=flags)
        self.assertEqual(warm.returncode, 0, warm.stderr)
        self.assertIsNotNone(re.search(r"\b0 parsed\b", warm.stderr),
                             warm.stderr)


def encodingless_opens(path):
    """Line numbers of every text-mode open()/gzip.open() with no encoding=.
    On Windows open() defaults to the locale codepage, so a session title with
    an emoji would raise UnicodeDecodeError on a stranger's machine."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        is_open = (isinstance(f, ast.Name) and f.id == "open") or (
            isinstance(f, ast.Attribute) and f.attr == "open"
            and isinstance(f.value, ast.Name) and f.value.id == "gzip")
        if not is_open:
            continue
        mode = "r"
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            mode = node.args[1].value
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                mode = kw.value.value
        if "b" in (mode or ""):
            continue
        if not any(kw.arg == "encoding" for kw in node.keywords):
            bad.append(node.lineno)
    return bad


LAUNCHERS = ("run", "Popen", "call", "check_call", "check_output", "system",
             "popen", "startfile", "execv", "execvp", "execl", "execlp",
             "execve", "spawnv", "spawnl", "spawnvp", "spawnlp")


def launch_call_strings(path):
    """(lineno, text) for every string constant inside a call that starts a
    process: what a file could EXECUTE, as opposed to what it merely names in
    a docstring or a message."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute):
            base = f.value.id if isinstance(f.value, ast.Name) else ""
            hit = f.attr in LAUNCHERS or base in ("subprocess", "webbrowser")
        else:
            hit = isinstance(f, ast.Name) and f.id in LAUNCHERS
        if not hit:
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                out.append((sub.lineno, sub.value))
    return out


def tree_listing(root):
    """Every path under root with its size: a new file, a deleted one and a
    rewritten one all change it."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            out.append(os.path.relpath(os.path.join(dirpath, name), root) + "/")
        for name in filenames:
            p = os.path.join(dirpath, name)
            out.append("%s:%d" % (os.path.relpath(p, root),
                                  os.path.getsize(p)))
    return sorted(out)


def module_imports(path):
    """(all top-level module names imported, names imported inside a try)."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    every, optional = set(), set()
    tries = [n for n in ast.walk(tree) if isinstance(n, ast.Try)]
    for node in ast.walk(tree):
        names = set()
        if isinstance(node, ast.Import):
            names = {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names = {(node.module or "").split(".")[0]}
        if not names:
            continue
        every |= names
        if any(node in ast.walk(t) for t in tries):
            optional |= names
    return every, optional


class TestPlatform(unittest.TestCase):
    """D-07/D-09/D-20: the per-platform branches are proven by injecting a
    platform and an environment. Real macOS and Windows behavior is NOT
    verified here; there is no such machine in this environment."""

    PERMITTED = {"__future__", "argparse", "collections", "datetime", "glob",
                 "gzip", "hashlib", "json", "os", "pathlib", "re",
                 "subprocess", "sys", "webbrowser", "zstandard"}

    def test_cache_dir_darwin(self):
        self.assertEqual(
            br.cache_dir(platform="darwin", env={"HOME": "/Users/bob"}),
            "/Users/bob/Library/Caches/burnrate")

    def test_cache_dir_win32(self):
        lad = "C:\\Users\\bob\\AppData\\Local"
        self.assertEqual(br.cache_dir(platform="win32",
                                      env={"LOCALAPPDATA": lad}),
                         os.path.join(lad, "burnrate"))
        self.assertEqual(
            br.cache_dir(platform="win32", env={"USERPROFILE": "C:/Users/bob"}),
            os.path.join("C:/Users/bob/AppData/Local", "burnrate"))

    def test_cache_dir_linux(self):
        self.assertEqual(
            br.cache_dir(platform="linux", env={"HOME": "/home/alice"}),
            "/home/alice/.cache/burnrate")
        self.assertEqual(
            br.cache_dir(platform="linux",
                         env={"HOME": "/home/alice", "XDG_CACHE_HOME": "/c"}),
            "/c/burnrate")
        # a relative XDG_CACHE_HOME is ignored, per the spec
        self.assertEqual(
            br.cache_dir(platform="linux",
                         env={"HOME": "/home/alice", "XDG_CACHE_HOME": "rel"}),
            "/home/alice/.cache/burnrate")

    def test_file_uri(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            self.assertEqual(
                br.file_uri("C:\\Users\\bob\\My Reports\\dashboard.html",
                            flavour="win32"),
                "file:///C:/Users/bob/My%20Reports/dashboard.html")
            self.assertEqual(
                br.file_uri("/home/alice/my reports/x.html", flavour="posix"),
                "file:///home/alice/my%20reports/x.html")
        # relative paths are absolutized by file_uri itself, not by the caller
        uri = br.file_uri("reports/dashboard.html")
        self.assertTrue(uri.startswith("file:///"), uri)
        self.assertTrue(uri.endswith("/reports/dashboard.html"), uri)

    def test_every_text_open_names_an_encoding(self):
        self.assertEqual(encodingless_opens(BR), [])

    def test_the_encoding_walk_is_not_vacuous(self):
        probe = os.path.join(TMP, "probe_encoding.py")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write('import gzip\n'
                     'open("a", "w")\n'
                     'gzip.open("b", "rt")\n'
                     'open("c", "rb")\n'
                     'open("d", "w", encoding="utf-8")\n')
        self.assertEqual(encodingless_opens(probe), [2, 3])

    def test_imports_stay_stdlib(self):
        every, optional = module_imports(BR)
        self.assertEqual(every - self.PERMITTED, set())
        self.assertEqual(optional, {"zstandard"})


def report(out):
    with open(os.path.join(out, "dashboard.html"), encoding="utf-8") as fh:
        return fh.read()


def injected_payload(html):
    """The raw text burnrate injected into the viewer's <script> block."""
    line = next(ln for ln in html.splitlines() if ln.startswith("const DATA ="))
    return line[len("const DATA = "):].rstrip().rstrip(";")


class TestViewer(unittest.TestCase):
    """D-21: the four phase-1 deferrals, now that D-08 unlocks PAGE."""

    def test_payload_cannot_close_the_script_element(self):
        _, out = run()
        html = report(out)
        raw = injected_payload(html)
        # every "<" is escaped, not just the ones starting "</": "<!--" and
        # "<script" are breakout vectors of their own
        self.assertNotIn("<", raw)
        # "<" is valid JSON string content, so the title round-trips
        data = json.loads(raw)
        titles = [s[2] for s in data["sessions"]]
        self.assertIn(EXP["title"], titles)
        for vector in ("</script>", "<!--", "<script"):
            self.assertIn(vector, EXP["title"])
        self.assertEqual(html.count("</script>"), 1)

    def test_the_script_element_closes_under_a_real_tokenizer(self):
        # string assertions cannot see script-data-double-escaped state, which
        # is the failure the "</"-only escape let through: walk the HTML
        # tokenizer's script-data states and confirm the element really ends
        _, out = run()
        html = report(out)
        i = html.index("<script>") + len("<script>")
        escaped = doubled = False
        end = -1
        while i < len(html):
            if html.startswith("<!--", i):
                escaped = True
            elif escaped and html.startswith("-->", i):
                escaped = doubled = False
            elif escaped and html.startswith("<script", i):
                doubled = True
            elif html.startswith("</script", i):
                if not doubled:
                    end = i
                    break
                doubled = False
            i += 1
        self.assertNotEqual(end, -1, "the script element never closes")
        self.assertEqual(html[end:].strip(),
                         "</script>\n</body>\n</html>".strip())

    def test_subtitle_names_the_archive_actually_used(self):
        # the subtitle itself is rendered client-side; what is checkable here
        # is that the hardcoded source name is gone and the payload carries the
        # value the template now reads
        self.assertNotIn("hindsight archive", br.PAGE)
        self.assertIn("DATA.archive_label", br.PAGE)
        _, plain = run()
        self.assertIn("live transcripts only", br.PAGE)
        self.assertFalse(payload(plain)["archive_used"])
        self.assertIsNone(payload(plain)["archive_label"])
        _, arch = run(["--archive", P["backup"]])
        self.assertTrue(payload(arch)["archive_used"])
        self.assertEqual(payload(arch)["archive_label"], "archive: backup")

    def test_all_projects_checkbox_honors_its_own_state(self):
        lines = br.PAGE.splitlines()
        i = next(n for n, ln in enumerate(lines)
                 if "#projall" in ln and "onchange" in ln)
        handler = " ".join(lines[i:i + 3])
        self.assertIn("e.target.checked", handler)
        self.assertIn("new Set()", handler)

    def marker(self):
        m = os.path.join(tempfile.mkdtemp(dir=TMP), "launched")
        return m

    def launched_uris(self, m):
        if not os.path.exists(m):
            return []
        with open(m, encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip()]

    @unittest.skipIf(sys.platform == "win32", "needs a POSIX shell stand-in")
    def test_a_slow_browser_holds_neither_the_prompt_nor_the_pipe(self):
        # the deliverable of D-21(4). run() captures output, so this measures
        # exactly what `out=$(burnrate.py)` or a CI step sees: the stand-in
        # lives 8s without redirecting its fds, so an undetached child keeps
        # the pipe open and subprocess.run() cannot return before it dies.
        m = self.marker()
        started = time.monotonic()
        proc, out = run(env=base_env(BROWSER=SLOW_BROWSER, MARKER=m),
                        flags=("--quiet",))
        elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 6.0, "the browser launch held the pipe open")
        # and it really launched: without this the case passes against an
        # open_report() that returns True having spawned nothing
        self.assertEqual(self.launched_uris(m),
                         [br.file_uri(os.path.join(out, "dashboard.html"))])

    @unittest.skipIf(sys.platform == "win32", "needs a POSIX shell stand-in")
    def test_a_launched_browser_prints_no_fallback_note(self):
        # the note must not fire when a browser did open. Pinning the elapsed
        # time as well is what keeps this from passing against the pre-fix
        # code, which also stayed silent for an exit-0 browser -- but only
        # after blocking for its whole lifetime.
        m = self.marker()
        started = time.monotonic()
        proc, out = run(env=base_env(BROWSER=SLOW_BROWSER, MARKER=m),
                        flags=("--quiet",))
        elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("no browser opened", proc.stderr)
        self.assertLess(elapsed, 6.0)
        self.assertEqual(self.launched_uris(m),
                         [br.file_uri(os.path.join(out, "dashboard.html"))])

    @unittest.skipIf(sys.platform == "win32", "needs a POSIX shell stand-in")
    def test_a_browser_that_cannot_start_prints_the_note(self):
        # the return value must not lie in the other direction either: a
        # $BROWSER naming a nonexistent binary registers fine with webbrowser
        # and fails only in the grandchild, so the settle window is the only
        # thing that catches it.
        # DISPLAY stays unset on purpose -- with it, webbrowser also registers
        # the host's real xdg-open, which would both mask the failure and open
        # an actual window on the machine running the suite.
        env = base_env(BROWSER=DEAD_BROWSER)
        for k in ("DISPLAY", "WAYLAND_DISPLAY"):
            env.pop(k, None)
        proc, out = run(env=env, flags=("--quiet",))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("no browser opened", proc.stderr)
        self.assertIn(os.path.join(out, "dashboard.html"), proc.stderr)

    @unittest.skipIf(sys.platform == "win32", "POSIX display convention")
    def test_no_display_and_no_browser_declines_and_says_so(self):
        # with neither, webbrowser registers terminal browsers (w3m, lynx)
        # that steal the tty. Refuse, and tell the user where the file is
        # instead of leaving them with nothing.
        env = base_env()
        for k in ("DISPLAY", "WAYLAND_DISPLAY", "BROWSER"):
            env.pop(k, None)
        proc, out = run(env=env, flags=("--quiet",))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("no browser opened", proc.stderr)
        self.assertIn(os.path.join(out, "dashboard.html"), proc.stderr)

    @unittest.skipIf(sys.platform == "win32", "needs a POSIX shell stand-in")
    def test_an_explicit_browser_beats_a_missing_display(self):
        # WSL (wslview) and VS Code Remote-SSH both export $BROWSER with no
        # DISPLAY. Refusing there would silently stop opening their report.
        m = self.marker()
        env = base_env(BROWSER=FAST_BROWSER, MARKER=m)
        for k in ("DISPLAY", "WAYLAND_DISPLAY"):
            env.pop(k, None)
        proc, out = run(env=env, flags=("--quiet",))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("no browser opened", proc.stderr)
        self.assertEqual(self.launched_uris(m),
                         [br.file_uri(os.path.join(out, "dashboard.html"))])

    def test_open_report_declines_only_when_nothing_can_open(self):
        # the platform branch directly, without spawning anything
        self.assertFalse(br.open_report("x.html", env={}, platform="linux"))
        self.assertFalse(
            br.open_report("x.html", env={"TERM": "xterm"}, platform="linux"))

    def test_no_output_tells_the_user_to_run_the_installer(self):
        """The repository now ships the installer, but the tool must still
        never point anyone at it. Wrapping a status bar is opt-in and
        separately documented; a report that names the installer turns a
        missing card into an instruction to modify settings.json."""
        _, out = run()
        self.assertNotIn("install_usage_logger.sh", report(out))
        with open(BR, encoding="utf-8") as fh:
            self.assertNotIn("install_usage_logger.sh", fh.read())


NODE = shutil.which("node")


def day_range_prologue(html):
    """The viewer's day-range prologue, taken verbatim out of the generated
    report: the payload, the day helpers, the MINDAY/MAXDAY bounds and
    rangeFromPreset(). Nothing in that span touches the DOM, so it runs under a
    bare JS engine exactly as the browser runs it."""
    body = html[html.index("<script>") + len("<script>"):]
    start = body.index("const DATA =")
    end = body.index("function rangeFromPreset(){")
    return body[start:body.index("\n}", end) + 2]


class TestEmptyReport(unittest.TestCase):
    """D-18 keeps an empty root running, so the report it writes has to be a
    working page rather than one that renders 'undefined'."""

    @unittest.skipIf(not NODE, "needs a JS engine")
    def test_an_empty_report_still_has_a_real_day_range(self):
        proc, out = run(["--root", EMPTY_ROOT])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(payload(out)["daily"], [])
        js = day_range_prologue(report(out)) + """
for (const p of ['30', 'all', '7']) {
  state.preset = p;
  rangeFromPreset();
  if (typeof state.d0 !== 'string' || typeof state.d1 !== 'string') {
    console.error('preset ' + p + ': d0=' + state.d0 + ' d1=' + state.d1);
    process.exit(1);
  }
}
console.log(JSON.stringify([MINDAY, MAXDAY, state.d0, state.d1]));
"""
        path = os.path.join(tempfile.mkdtemp(dir=TMP), "range.js")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(js)
        r = subprocess.run([NODE, path], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        bounds = json.loads(r.stdout)
        for v in bounds:
            self.assertRegex(v, r"^\d{4}-\d{2}-\d{2}$", bounds)
        self.assertEqual(bounds[0], bounds[1])   # MINDAY == MAXDAY == today

    def test_both_day_bounds_carry_a_fallback(self):
        # the same claim without a JS engine, so the fix stays guarded on a
        # machine that has none
        for name in ("MINDAY", "MAXDAY"):
            line = next(ln for ln in br.PAGE.splitlines()
                        if ln.startswith(f"const {name} ="))
            self.assertIn("||", line, line)


class TestBilledUnitIsLabeled(unittest.TestCase):
    """Billed-equivalent is a weighted token count, never a currency amount.
    Every place the page prints one of those numbers has to say so on its own,
    because a reader who lands on a table does not scroll to the footer."""

    def test_the_sessions_column_names_the_unit(self):
        # a bare "Billed" over comma-separated numbers reads as money
        self.assertIn('<th class="num">Billed-equiv</th>', br.PAGE)
        self.assertNotIn('<th class="num">Billed</th>', br.PAGE)

    def test_the_hero_tile_disclaims_outside_the_delta_branch(self):
        # the first window has no prior window, so delta is null there; the
        # disclaimer has to survive that branch rather than ride along with it
        self.assertIn("}weighted tokens, not a bill", br.PAGE)


def viewer_slice(html, start, end):
    """The verbatim text between two markers in the generated report's script
    block, so a case runs the shipped code rather than a paraphrase of it."""
    body = html[html.index("<script>") + len("<script>"):]
    i = body.index(start)
    return body[i:body.index(end, i) + len(end)]


RL_STUBS = """
// DOM and chart stubs: enough for the rate-limit block, nothing more.
const ELS = {};
const elFor = id => (ELS[id] = ELS[id] || {style: {}, innerHTML: null});
globalThis.document = { querySelector: s => elFor(s) };
const CALLS = [];
function lineChart(elId, days, seriesArr, unit){
  CALLS.push({elId, days, series: seriesArr, unit});
}
"""


class TestRateLimitCard(unittest.TestCase):
    """AC5: the cap card appears only when the selected range holds more than
    one sample. Proven at the payload level and by running the report's own
    rate-limit block under a JS engine with DOM stubs -- not in a browser,
    which nothing here has."""

    def rl_block(self, out):
        html = report(out)
        inrange = next(ln for ln in html.splitlines()
                       if ln.startswith("function inRange(d)"))
        return (RL_STUBS + day_range_prologue(html) + "\n" + inrange + "\n"
                + viewer_slice(html, "// rate-limit card",
                               "} else $('#rlCard').style.display = 'none';")
                + """
console.log(JSON.stringify({
  display: document.querySelector('#rlCard').style.display,
  calls: CALLS,
  rl: DATA.rl.length}));
""")

    def render_rl(self, out):
        path = os.path.join(tempfile.mkdtemp(dir=TMP), "rl.js")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.rl_block(out))
        r = subprocess.run([NODE, path], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(r.stdout)

    def one_sample_root(self):
        """A copy of the fixture's primary tree whose log holds exactly one
        in-range sample, taken verbatim from the fixture's own first line."""
        d = tempfile.mkdtemp(dir=TMP)
        root = os.path.join(d, ".claude", "projects")
        shutil.copytree(P["primary"], root)
        log = os.path.join(d, ".claude", "usage-logger", "usage-log.jsonl")
        os.makedirs(os.path.dirname(log))
        with open(P["rl_log"], encoding="utf-8") as fh:
            first = fh.readline()
        with open(log, "w", encoding="utf-8") as fh:
            fh.write(first)
        return root

    def test_the_payload_carries_the_card_both_ways(self):
        # the same claim without a JS engine, so it stays guarded on a machine
        # that has none
        _, out = run()
        self.assertEqual(payload(out)["rl"], EXP["rl_rows"])
        self.assertTrue(payload(out)["rl_installed"])
        _, absent = run(["--root", P["alt"]])
        self.assertEqual(payload(absent)["rl"], [])
        self.assertFalse(payload(absent)["rl_installed"])

    @unittest.skipIf(not NODE, "needs a JS engine")
    def test_samples_in_range_show_the_card(self):
        _, out = run()
        got = self.render_rl(out)
        self.assertEqual(got["display"], "")
        self.assertEqual(len(got["calls"]), 1, got)
        call = got["calls"][0]
        self.assertEqual(call["elId"], "#rlChart")
        self.assertEqual(len(call["days"]), 2, call["days"])
        self.assertEqual([s["name"] for s in call["series"]],
                         ["5-hour window", "7-day window"])
        for s in call["series"]:
            self.assertEqual(len(s["values"]), 2, s)
            for v in s["values"]:
                self.assertIsInstance(v, (int, float), s)

    @unittest.skipIf(not NODE, "needs a JS engine")
    def test_no_log_hides_the_card(self):
        _, out = run(["--root", P["alt"]])
        got = self.render_rl(out)
        self.assertEqual(got["rl"], 0)
        self.assertEqual(got["display"], "none")
        self.assertEqual(got["calls"], [])

    @unittest.skipIf(not NODE, "needs a JS engine")
    def test_a_single_sample_still_hides_the_card(self):
        """The case that actually pins the gate at more-than-one. An absent
        log is falsy under `> 1` and `> 0` alike and the fixture's four samples
        are true under both, so without a one-sample input nothing here can
        tell the gate from `rl.length > 0` -- and the chart itself refuses to
        draw fewer than two points, so relaxing it would render the card empty
        rather than hidden."""
        _, out = run(["--root", self.one_sample_root()])
        self.assertEqual(len(payload(out)["rl"]), 1)
        got = self.render_rl(out)
        self.assertEqual(got["rl"], 1)
        self.assertEqual(got["display"], "none")
        self.assertEqual(got["calls"], [])


SKILL_DIR = os.path.join(REPO, "skills", "burnrate")
SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")
SKILL_SCRIPT = os.path.join(SKILL_DIR, "scripts", "burnrate_skill.py")
PLUGIN_MANIFEST = os.path.join(REPO, ".claude-plugin", "plugin.json")
MARKET_MANIFEST = os.path.join(REPO, ".claude-plugin", "marketplace.json")


@unittest.skipUnless(os.path.exists(SKILL_SCRIPT),
                     "no skills/ in this checkout")
class TestSkillAnswers(unittest.TestCase):
    """D-14: the /burnrate skill answers from the daily rows, which are twelve
    unlabeled positions -- a wrong column index prints a plausible wrong number
    and nothing else notices. These cases are that notice. The script is run
    the way Claude Code runs it (a subprocess, no in-process import) because it
    resolves both burnrate.py and its output directory from its own __file__.

    Skipped rather than failed when skills/ is absent: burnrate.py is a
    standalone tool and a checkout with the skill removed must still report no
    failures (AC6)."""

    @classmethod
    def setUpClass(cls):
        # one cache home for the whole class, so the first case pays for the
        # payload and the rest exercise the reuse path
        cls.cache = tempfile.mkdtemp(dir=TMP)

    def skill(self, *args, expect=0):
        proc = subprocess.run(
            [sys.executable, SKILL_SCRIPT, *args],
            env=base_env(XDG_CACHE_HOME=self.cache,
                         CLAUDE_PROJECTS=P["primary"]),
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, expect, proc.stderr)
        return proc

    def answer(self, *args):
        proc = self.skill(*args)
        return json.loads(proc.stdout)

    def written_payload(self):
        with open(os.path.join(self.cache, "burnrate", "report",
                               "dashboard_data.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def tolerance(self, data):
        """One token per daily row: the payload rounds each row on its own, so
        a regrouped sum can drift by that much. Nowhere near enough slack to
        absorb reading input tokens (index 7) as billed-equiv (index 6)."""
        return len(data["daily"])

    def test_by_project_matches_the_fixture_per_label(self):
        got = self.answer("ask", "--by", "project")
        data = self.written_payload()
        tol = self.tolerance(data)
        rows = {r["project"]: r["billed_equiv"] for r in got["rows"]}
        self.assertEqual(sorted(rows), sorted(EXP["label_be"]))
        for label, want in EXP["label_be"].items():
            self.assertLessEqual(abs(rows[label] - want), tol,
                                 f"{label}: {rows[label]} vs {want}")
        self.assertLessEqual(
            abs(got["total"]["billed_equiv"] - EXP["total_be"]), tol,
            got["total"])

    def test_by_day_sums_to_the_same_total(self):
        by_day = self.answer("ask", "--by", "day")
        by_proj = self.answer("ask", "--by", "project")
        tol = self.tolerance(self.written_payload())
        self.assertLessEqual(
            abs(sum(r["billed_equiv"] for r in by_day["rows"])
                - by_proj["total"]["billed_equiv"]), tol)
        self.assertEqual(by_day["total"]["billed_equiv"],
                         by_proj["total"]["billed_equiv"])
        self.assertTrue(by_day["reused"] or by_proj["reused"],
                        "a second answer inside the freshness window rebuilt")

    def test_words_reach_this_repos_own_burnrate(self):
        argv = json.loads(
            self.skill("run", "--dry-run", "7d", "rebuild",
                       "no-archive").stdout)
        self.assertEqual(argv[1], BR)       # upward resolution, not a copy
        for flag in ("--rebuild", "--no-archive", "--json", "--out"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--range") + 1], "7")

    def test_an_unrecognized_word_is_refused(self):
        proc = self.skill("run", "--dry-run", "bogus", expect=2)
        self.assertIn("bogus", proc.stderr)

    def test_blocks_join_projects_and_close_five_hours_later(self):
        got = self.answer("blocks", "--last", "1")
        data = self.written_payload()
        self.assertEqual(len(got["blocks"]), 1)
        b = got["blocks"][0]
        self.assertEqual(b["billed_equiv"], data["blocks"][-1][3])
        self.assertEqual(b["window_end_epoch"] - b["start_epoch"], 5 * 3600)
        ids = {p["id"] for p in data["projects"]}
        self.assertLessEqual(set(b["top_projects"]), ids)

    def test_an_exact_project_label_beats_the_one_it_prefixes(self):
        """The fixture's 'home/alice/work/api' is a prefix of
        'home/alice/work/api#2'. Matching exact OR substring in one pass sums
        both and reports a confidently wrong figure."""
        want = EXP["label_be"]["home/alice/work/api"]
        got = self.answer("ask", "--by", "project",
                          "--project", "home/alice/work/api")
        tol = self.tolerance(self.written_payload())
        self.assertEqual([r["project"] for r in got["rows"]],
                         ["home/alice/work/api"])
        self.assertLessEqual(abs(got["total"]["billed_equiv"] - want), tol,
                             got["total"])
        # a substring matching no label exactly still spans every label it hits
        both = self.answer("ask", "--by", "project", "--project", "alice/work")
        self.assertEqual(sorted(r["project"] for r in both["rows"]),
                         ["home/alice/work/api", "home/alice/work/api#2"])

    def test_a_malformed_date_window_is_refused(self):
        """Day strings are compared as strings: '2026-3-1' sorts below every
        payload day and silently empties the selection, and 'banana' sorts
        above so --until applies no filter at all. Both must fail instead."""
        for flag, value in (("--since", "2026-3-1"), ("--until", "banana"),
                            ("--since", "03/01/2026")):
            proc = self.skill("ask", "--by", "day", flag, value, expect=2)
            self.assertIn(flag, proc.stderr)

    def test_a_last_below_one_is_refused(self):
        """rows[-0:] is the WHOLE list and rows[1:] drops the current window --
        the one a 'how much is left' question is about."""
        for n in ("0", "-1"):
            for cmd in ("blocks", "ask"):
                proc = self.skill(cmd, "--last", n, expect=2)
                self.assertIn("N >= 1", proc.stderr)

    def test_a_bad_day_word_fails_before_the_payload_is_built(self):
        """Validation after ensure_payload pays for a full transcript reparse
        before printing a usage error. A cold cache proves the ordering: the
        run must fail without ever writing a payload."""
        cold = tempfile.mkdtemp(dir=TMP)
        proc = subprocess.run(
            [sys.executable, SKILL_SCRIPT, "ask", "--day", "bogus"],
            env=base_env(XDG_CACHE_HOME=cold, CLAUDE_PROJECTS=P["primary"]),
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("--day", proc.stderr)
        self.assertFalse(
            os.path.exists(os.path.join(cold, "burnrate", "report",
                                        "dashboard_data.json")),
            "the payload was rebuilt before the day word was checked")


def frontmatter(path):
    """The leading `---` block as a flat dict. A line scan, not a YAML parser:
    the suite is stdlib only, and the only thing read from it here is a name."""
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    out = {}
    if not lines or lines[0].strip() != "---":
        return out
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            key, value = line.split(":", 1)
            out[key.strip()] = value.strip()
    return out


@unittest.skipUnless(os.path.exists(PLUGIN_MANIFEST)
                     and os.path.exists(MARKET_MANIFEST),
                     "no .claude-plugin/ in this checkout")
class TestPluginPackaging(unittest.TestCase):
    """PLUG-01: the repository is its own single-plugin marketplace, so
    `claude plugin install burnrate@burnrate` puts /burnrate in projects that
    are not this one. The manifests are what Claude Code reads, and the skill's
    own trigger is its frontmatter name -- neither is exercised by any other
    case here, and a typo in either is invisible until an install fails.

    Skipped rather than failed when .claude-plugin/ is absent, the same rule
    the skill cases follow: a checkout stripped of an optional piece skips."""

    @classmethod
    def setUpClass(cls):
        with open(PLUGIN_MANIFEST, encoding="utf-8") as fh:
            cls.plugin = json.load(fh)
        with open(MARKET_MANIFEST, encoding="utf-8") as fh:
            cls.market = json.load(fh)

    def plugin_copy(self):
        """A plugin-shaped copy: burnrate.py and skills/, nothing else. No
        .git, no tools/, no extras/ -- which is what an installed plugin's
        cache directory has of ours that the skill actually runs. __pycache__
        is excluded on the way in so a case about bytecode measures the run,
        not the copy."""
        d = tempfile.mkdtemp(dir=TMP)
        shutil.copy2(BR, os.path.join(d, "burnrate.py"))
        shutil.copytree(os.path.join(REPO, "skills"),
                        os.path.join(d, "skills"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        return d

    def dry_run(self, root, *words):
        """`run --dry-run` from a plugin-shaped copy, returning the argv it
        would have executed."""
        script = os.path.join(root, "skills", "burnrate", "scripts",
                              "burnrate_skill.py")
        proc = subprocess.run(
            [sys.executable, script, "run", "--dry-run", *words],
            env=base_env(XDG_CACHE_HOME=os.path.join(
                tempfile.mkdtemp(dir=TMP), "cache"),
                CLAUDE_PROJECTS=P["primary"]),
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_the_plugin_manifest_names_burnrate(self):
        self.assertEqual(self.plugin.get("name"), "burnrate")
        for key in ("version", "description", "license"):
            self.assertTrue(self.plugin.get(key), f"plugin.json lacks {key}")

    def test_the_marketplace_lists_this_plugin_once(self):
        # a marketplace with no description fails `plugin validate --strict`
        self.assertTrue(self.market.get("description"),
                        "marketplace.json lacks a description")
        entries = self.market.get("plugins")
        self.assertEqual(len(entries), 1, entries)
        entry = entries[0]
        self.assertEqual(entry.get("name"), self.plugin["name"])
        self.assertEqual(entry.get("source"), "./")
        # `claude plugin tag` validates that a marketplace entry's version and
        # plugin.json's agree, so plugin.json stays the only place one is
        # written and the entry carries none to drift from it
        self.assertNotIn("version", entry)

    def test_the_skill_sits_at_the_default_discovery_path(self):
        """skills/<name>/SKILL.md is where the loader looks with no `skills`
        key in plugin.json, and the frontmatter name is what makes the trigger
        /burnrate -- rename either and the plugin installs but does nothing."""
        self.assertTrue(os.path.exists(SKILL_MD), SKILL_MD)
        self.assertEqual(frontmatter(SKILL_MD).get("name"), "burnrate")

    def test_skill_md_calls_the_helper_through_the_plugin_root(self):
        """Under a plugin, $CLAUDE_PROJECT_DIR is the USER's project, which has
        no helper, and `git rev-parse` resolves the user's repository root --
        so the old path either fails or runs something unrelated. Only
        ${CLAUDE_PLUGIN_ROOT}, which the loader substitutes into this text,
        names the installed copy."""
        with open(SKILL_MD, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn(
            "${CLAUDE_PLUGIN_ROOT}/skills/burnrate/scripts/burnrate_skill.py",
            body)
        self.assertNotIn("CLAUDE_PROJECT_DIR", body)
        self.assertNotIn("rev-parse", body)
        # The loader substitutes the token with a GLOBAL regex over the whole
        # body, so a second occurrence -- prose explaining the unsubstituted
        # case -- is rewritten too, and a guard phrased against it reads as its
        # own opposite under a correct plugin load. The command may name it; no
        # other line may.
        self.assertEqual(
            body.count("${CLAUDE_PLUGIN_ROOT}"), 1,
            "${CLAUDE_PLUGIN_ROOT} must appear exactly once, in the command")

    def test_a_plugin_shaped_copy_runs_its_own_tool(self):
        """An installed plugin is a copy of this repo under a cache directory
        with no git and no checkout around it. The helper resolves burnrate.py
        by walking up from its own file, so it must land on THAT copy's tool
        and never on this working tree's."""
        root = self.plugin_copy()
        argv = self.dry_run(root, "7d")
        self.assertEqual(argv[1], os.path.join(root, "burnrate.py"))
        self.assertNotEqual(argv[1], BR)
        self.assertEqual(argv[argv.index("--range") + 1], "7")

    def test_a_run_writes_nothing_into_the_plugin_directory(self):
        """The helper execs burnrate.py through the source loader, which caches
        bytecode beside the file it loaded -- inside the installed plugin's own
        directory, which Claude Code owns and re-copies on update. It also
        falsifies the helper's own docstring, which promises the only writes
        are burnrate.py's, inside the resolved output directory."""
        root = self.plugin_copy()
        self.dry_run(root, "7d")            # asserts rc 0 itself
        found = [os.path.join(dirpath, name)
                 for dirpath, dirs, files in os.walk(root)
                 for name in dirs + files
                 if name == "__pycache__" or name.endswith(".pyc")]
        self.assertEqual(found, [], found)


class TestRateLimitLogReader(unittest.TestCase):
    """LOG-01: the reader parses a file another program appends to many times a
    second, so its ordering has to survive the shapes that file really takes --
    a second holding several samples, and a window the payload omitted."""

    def log(self, *lines):
        d = tempfile.mkdtemp(dir=TMP)
        path = os.path.join(d, "usage-log.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("".join(ln + "\n" for ln in lines))
        return path

    def test_an_absent_window_in_a_shared_second_does_not_raise(self):
        """Sorting whole rows compares None against a float as soon as two
        samples share a timestamp and one window is missing -- the wrapper's
        timestamps are second-resolution and each window is independently
        absent, so this is a normal file. The TypeError escapes past the
        per-line guard and the whole run produces no report."""
        path = self.log(
            '{"ts":"2026-07-31T15:31:11Z","five_hour":null,"seven_day":30}',
            '{"ts":"2026-07-31T15:31:11Z","five_hour":23.5,"seven_day":30}')
        rows = br.read_rl_log(path)
        self.assertEqual([r[1] for r in rows], [None, 23.5], rows)

    def test_samples_in_one_second_keep_the_order_they_were_written(self):
        """File order is write order, so the last row of a second is the newest
        sample. Sorting on the values instead makes the viewer's lastOf() report
        the LARGEST sample of that second -- visibly wrong right after a 5-hour
        window resets, which is exactly when several samples land together."""
        path = self.log(
            '{"ts":"2026-07-31T15:31:11Z","five_hour":88.0,"seven_day":40}',
            '{"ts":"2026-07-31T15:31:11Z","five_hour":91.0,"seven_day":40}',
            '{"ts":"2026-07-31T15:31:11Z","five_hour":3.0,"seven_day":41}')
        rows = br.read_rl_log(path)
        self.assertEqual([r[1] for r in rows], [88.0, 91.0, 3.0], rows)

    def test_rows_from_different_seconds_still_sort_ascending(self):
        path = self.log(
            '{"ts":"2026-07-31T15:31:20Z","five_hour":2}',
            '{"ts":"2026-07-31T15:31:11Z","five_hour":1}')
        self.assertEqual([r[1] for r in br.read_rl_log(path)], [1, 2])

    def test_a_run_over_such_a_log_still_writes_its_report(self):
        # the user-visible half: the raise is outside read_rl_log's per-line
        # try, so a single duplicated second cost the entire dashboard and the
        # only recovery was hand-editing the log
        d = tempfile.mkdtemp(dir=TMP)
        root = os.path.join(d, ".claude", "projects")
        shutil.copytree(P["primary"], root)
        log = os.path.join(d, ".claude", "usage-logger", "usage-log.jsonl")
        os.makedirs(os.path.dirname(log))
        with open(P["rl_log"], encoding="utf-8") as fh:
            body = fh.read()
        with open(log, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.write('{"ts":"2026-03-01T12:00:00Z","five_hour":null,'
                     '"seven_day":30}\n')
        proc, out = run(["--root", root])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(os.path.join(out, "dashboard.html")))
        self.assertEqual(len(payload(out)["rl"]), EXP["rl_samples"] + 1)


def rl_payload(five=23.5, seven=41.2, sid="sess-1", model="Sonnet 4.6"):
    """A statusline payload shaped the way Claude Code hands one to the
    configured command, on one line, with both cap windows present."""
    return json.dumps({
        "session_id": sid,
        "model": {"id": "claude-sonnet-4-6", "display_name": model},
        "workspace": {"current_dir": "/home/alice/work/api"},
        "rate_limits": {
            "five_hour": {"used_percentage": five, "resets_at": 1772380800},
            "seven_day": {"used_percentage": seven, "resets_at": 1772985600},
        }})


@unittest.skipIf(sys.platform == "win32", "the logger is bash-only")
class TestUsageLoggerWrapper(unittest.TestCase):
    """LOG-01: the wrapper is a tee, so the statusline it wraps has to behave
    exactly as it did unwrapped -- same stdout, same exit status -- however
    badly the logging half fails. Driven by subprocess rather than a shell test
    runner: this repository ships no shell harness and the suite already
    executes POSIX shell stand-ins."""

    def logger_dir(self, inner=None):
        d = os.path.join(tempfile.mkdtemp(dir=TMP), "usage-logger")
        os.makedirs(d)
        if inner is not None:
            with open(os.path.join(d, "inner-command"), "w",
                      encoding="utf-8") as fh:
                fh.write(inner)
        return d

    def feed(self, d, payload="{}"):
        """Run the wrapper BY ITS OWN PATH, so a lost executable bit fails
        here rather than in the user's status bar."""
        env = base_env(CLAUDE_USAGE_LOGGER_DIR=d,
                       HOME=tempfile.mkdtemp(dir=TMP),
                       CLAUDE_CONFIG_DIR=tempfile.mkdtemp(dir=TMP))
        return subprocess.run([WRAPPER], input=payload, env=env,
                              capture_output=True, text=True)

    def log_lines(self, d):
        path = os.path.join(d, "usage-log.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [ln for ln in fh.read().splitlines() if ln.strip()]

    def test_the_inner_commands_output_and_status_both_survive(self):
        # Claude Code uses a statusline child's stdout only when it exited 0,
        # and classifies anything else as a failure: a swallowed exit status
        # silently blanks the status bar
        d = self.logger_dir("printf STATUS; exit 3")
        proc = self.feed(d, rl_payload())
        self.assertEqual(proc.stdout, "STATUS")
        self.assertEqual(proc.returncode, 3)

    @unittest.skipIf(os.name != "posix" or os.geteuid() == 0,
                     "needs POSIX modes and a non-root user")
    def test_an_unwritable_log_dir_still_renders_the_statusline(self):
        d = self.logger_dir("printf STATUS; exit 3")
        os.chmod(d, 0o500)
        # restored so the atexit rmtree can remove it: rmtree(ignore_errors)
        # cannot delete inside a 0o500 directory and would leak TMP silently
        self.addCleanup(os.chmod, d, 0o700)
        proc = self.feed(d, rl_payload())
        self.assertEqual(proc.stdout, "STATUS")
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(self.log_lines(d), [])

    def test_no_inner_command_emits_nothing_and_succeeds(self):
        # printing anything here would clobber a status bar the user never
        # asked this script to draw
        d = self.logger_dir()
        proc = self.feed(d, rl_payload())
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.returncode, 0)

    def test_one_line_per_change_with_scalar_percentages(self):
        d = self.logger_dir()
        self.feed(d, rl_payload(23.5, 41.2))
        lines = self.log_lines(d)
        self.assertEqual(len(lines), 1, lines)
        rec = json.loads(lines[0])
        self.assertRegex(rec["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        for key in ("five_hour", "seven_day"):
            self.assertIsInstance(rec[key], (int, float), rec)
            self.assertNotIsInstance(rec[key], bool)
        # the statusline fires many times a second, so an unchanged pair must
        # not become a row
        self.feed(d, rl_payload(23.5, 41.2))
        self.assertEqual(len(self.log_lines(d)), 1)
        self.feed(d, rl_payload(24.5, 41.2))
        self.assertEqual(len(self.log_lines(d)), 2)

    def test_a_payload_past_the_pipe_buffer_keeps_the_inner_status(self):
        """Nothing obliges a statusline command to read its stdin, and the
        moment the payload outgrows the 64 KiB pipe buffer the printf feeding
        it takes SIGPIPE. Under pipefail that 141 becomes the pipeline's
        status, and Claude Code discards the output of a statusline that did
        not exit 0 -- so a command that worked unwrapped renders blank. Between
        65000 and 66000 bytes is where it starts."""
        d = self.logger_dir("printf STATUS; exit 0")
        big = json.dumps(dict(json.loads(rl_payload()), pad="x" * 200000))
        proc = self.feed(d, big)
        self.assertEqual(proc.stdout, "STATUS")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_partial_reader_of_a_big_payload_still_reports_success(self):
        """The same break for a command that reads only its first bytes, which
        is what any statusline that stops at one JSON field does. It has to
        exit 0 to show the defect: pipefail reports the RIGHTMOST nonzero
        status, so an inner command that fails on its own already outranks the
        SIGPIPE, and only a successful one gets its status overwritten."""
        d = self.logger_dir("head -c 5 >/dev/null; printf STATUS; exit 0")
        proc = self.feed(d, json.dumps({"pad": "x" * 200000}))
        self.assertEqual(proc.stdout, "STATUS")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_payload_without_rate_limits_writes_nothing(self):
        # every invocation before a session's first API response looks like
        # this, and they must not cost a file
        d = self.logger_dir()
        proc = self.feed(d, json.dumps({"session_id": "s", "model": {}}))
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(d, "usage-log.jsonl")))

    def test_what_the_wrapper_writes_is_what_the_reader_reads(self):
        """The one case that ties the shipped script to burnrate's own reader:
        the log is a wire contract between two files that never import each
        other, and this phase adds no ingestion code, so the script is what has
        to conform."""
        d = self.logger_dir()
        self.feed(d, rl_payload(23.5, 41.2))
        self.feed(d, rl_payload(24.5, 42.5))
        rows = br.read_rl_log(os.path.join(d, "usage-log.jsonl"))
        self.assertEqual(len(rows), 2, rows)
        for ts, five, seven in rows:
            self.assertIsInstance(ts, int)
            self.assertIsInstance(five, float)
            self.assertIsInstance(seven, float)
        self.assertLessEqual(rows[0][0], rows[1][0])
        self.assertEqual([r[1] for r in rows], [23.5, 24.5])
        self.assertEqual([r[2] for r in rows], [41.2, 42.5])


FULL_SETTINGS = {
    "statusLine": {"type": "command", "command": "echo hi", "padding": 0,
                   "refreshInterval": 1000, "hideVimModeIndicator": True},
    "cleanupPeriodDays": 365,
}


@unittest.skipIf(sys.platform == "win32", "the installer is bash-only")
class TestUsageLoggerInstall(unittest.TestCase):
    """LOG-01: the installer edits the file that draws the user's status bar,
    so every case asserts on settings.json itself rather than on what the
    script said it would do. The fixture settings carry the optional keys
    Claude Code accepts, since a file holding only {command, type} could not
    catch a rewrite that drops the rest."""

    def config(self, settings=None, raw=None):
        """A throwaway config dir. HOME is thrown away too, so a derivation
        that falls back to $HOME/.claude cannot reach the real one. `raw`
        writes the file verbatim, for the shapes json.dump cannot produce."""
        d = tempfile.mkdtemp(dir=TMP)
        if settings is not None or raw is not None:
            with open(os.path.join(d, "settings.json"), "w",
                      encoding="utf-8") as fh:
                fh.write(raw if raw is not None
                         else json.dumps(settings, indent=2))
        return d

    def install(self, cfg, *args, logger_dir=None):
        env = base_env(CLAUDE_CONFIG_DIR=cfg, HOME=tempfile.mkdtemp(dir=TMP),
                       CLAUDE_USAGE_LOGGER_DIR=logger_dir)
        return subprocess.run([INSTALLER, *args], env=env,
                              capture_output=True, text=True)

    def read(self, path, mode="r"):
        with open(path, mode, **({} if "b" in mode else
                                 {"encoding": "utf-8"})) as fh:
            return fh.read()

    def settings(self, cfg):
        return json.loads(self.read(os.path.join(cfg, "settings.json")))

    def backups(self, cfg):
        return [f for f in os.listdir(cfg) if ".bak." in f]

    def installed(self, cfg, logger_dir=None):
        return os.path.join(logger_dir or os.path.join(cfg, "usage-logger"),
                            "usage_logger.sh")

    def test_a_dry_run_changes_nothing_on_disk(self):
        cfg = self.config(FULL_SETTINGS)
        path = os.path.join(cfg, "settings.json")
        before = self.read(path, "rb")
        proc = self.install(cfg)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read(path, "rb"), before)
        self.assertFalse(os.path.exists(os.path.join(cfg, "usage-logger")))
        self.assertEqual(self.backups(cfg), [])
        self.assertIn(self.installed(cfg), proc.stdout)

    def test_apply_installs_a_copy_and_keeps_every_other_key(self):
        cfg = self.config(FULL_SETTINGS)
        path = os.path.join(cfg, "settings.json")
        before = self.read(path, "rb")
        proc = self.install(cfg, "--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)

        copy = self.installed(cfg)
        self.assertTrue(os.access(copy, os.X_OK), copy)
        self.assertEqual(self.read(copy, "rb"), self.read(WRAPPER, "rb"))
        self.assertEqual(
            self.read(os.path.join(cfg, "usage-logger", "inner-command")),
            "echo hi")

        baks = self.backups(cfg)
        self.assertEqual(len(baks), 1, baks)
        self.assertEqual(self.read(os.path.join(cfg, baks[0]), "rb"), before)

        got = self.settings(cfg)
        self.assertEqual(got["statusLine"]["command"], copy)
        self.assertEqual(got["cleanupPeriodDays"], 365)
        for key in ("type", "padding", "refreshInterval",
                    "hideVimModeIndicator"):
            self.assertEqual(got["statusLine"][key],
                             FULL_SETTINGS["statusLine"][key], key)

    def test_apply_edits_in_place_and_keeps_a_symlinked_settings_a_symlink(self):
        """A settings.json managed by stow/yadm is a SYMLINK into a dotfiles
        repo. Replacing the name instead of writing through it deletes the
        link, so the repo never sees the change and the next `stow -R` puts
        the old statusLine back while inner-command still exists - after
        which the clobber guard refuses every reinstall. Falsified by
        restoring the mkstemp+os.replace write: the link becomes a regular
        file and the repo copy still says `echo hi`."""
        cfg = self.config()
        repo = tempfile.mkdtemp(dir=TMP)
        target = os.path.join(repo, "settings.json")
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(FULL_SETTINGS, fh, indent=2)
        os.chmod(target, 0o600)
        link = os.path.join(cfg, "settings.json")
        os.symlink(target, link)
        inode = os.stat(target).st_ino

        proc = self.install(cfg, "--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)

        self.assertTrue(os.path.islink(link), "the symlink was replaced")
        self.assertEqual(os.stat(target).st_ino, inode, "not written in place")
        self.assertEqual(os.stat(target).st_mode & 0o7777, 0o600)
        # the dotfiles repo's own copy is what has to carry the change
        got = json.loads(self.read(target))
        self.assertEqual(got["statusLine"]["command"], self.installed(cfg))
        self.assertEqual(got["cleanupPeriodDays"], 365)
        self.assertEqual(got["statusLine"]["padding"],
                         FULL_SETTINGS["statusLine"]["padding"])
        self.assertEqual([f for f in os.listdir(repo)
                          if f.startswith(".settings-")], [])

    def test_apply_does_not_break_a_hardlinked_settings(self):
        """Same rail as the symlink case, one the resolved-path variant would
        still have broken: a rename detaches every other name for the inode.
        Falsified by any write that replaces rather than truncates - link
        count drops to 1 and the second name keeps the stale content."""
        cfg = self.config()
        repo = tempfile.mkdtemp(dir=TMP)
        target = os.path.join(repo, "settings.json")
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(FULL_SETTINGS, fh, indent=2)
        os.link(target, os.path.join(cfg, "settings.json"))

        self.assertEqual(self.install(cfg, "--apply").returncode, 0)

        self.assertEqual(os.stat(target).st_nlink, 2, "hardlink was broken")
        self.assertEqual(json.loads(self.read(target))["statusLine"]["command"],
                         self.installed(cfg))

    def test_a_second_apply_refuses_to_double_wrap(self):
        cfg = self.config(FULL_SETTINGS)
        self.assertEqual(self.install(cfg, "--apply").returncode, 0)
        inner = os.path.join(cfg, "usage-logger", "inner-command")
        proc = self.install(cfg, "--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Already wrapped", proc.stdout)
        self.assertEqual(self.settings(cfg)["statusLine"]["command"],
                         self.installed(cfg))
        self.assertEqual(self.read(inner), "echo hi")

    def test_an_existing_inner_command_is_never_overwritten(self):
        # the state this guard exists for: the wrapper was uninstalled from
        # settings.json but the saved original was left behind. Overwriting it
        # would lose the user's real statusline for good.
        cfg = self.config(FULL_SETTINGS)
        os.makedirs(os.path.join(cfg, "usage-logger"))
        inner = os.path.join(cfg, "usage-logger", "inner-command")
        with open(inner, "w", encoding="utf-8") as fh:
            fh.write("my-real-statusline --flag")
        proc = self.install(cfg, "--apply")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("my-real-statusline --flag", proc.stderr)
        self.assertEqual(self.settings(cfg)["statusLine"]["command"],
                         "echo hi")
        self.assertEqual(self.read(inner), "my-real-statusline --flag")

    def test_an_unparseable_settings_file_stops_before_anything_is_written(self):
        """A settings.json the installer cannot read is not a settings.json
        with no statusline. An unchecked read fails to an empty command, which
        reads as "(none configured)" for a file that configures one, and the
        apply that follows saves an EMPTY inner-command over the user's real
        status bar."""
        cfg = self.config(raw='{"statusLine": {"command": "echo hi",},}\n')
        path = os.path.join(cfg, "settings.json")
        before = self.read(path, "rb")
        for args in ((), ("--apply",)):
            proc = self.install(cfg, *args)
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
            self.assertNotIn("none configured", proc.stdout)
            self.assertIn(path, proc.stderr)
            self.assertEqual(self.read(path, "rb"), before)
            self.assertFalse(os.path.exists(os.path.join(cfg, "usage-logger")))
            self.assertEqual(self.backups(cfg), [])

    def test_a_statusline_of_the_wrong_shape_is_refused_not_replaced(self):
        """Discarding these into a backup and writing the wrapper over them
        loses a working status bar: a string statusLine goes wholesale, and a
        list command lands in inner-command as a Python repr that no shell can
        run, while the installer reports it preserved verbatim."""
        for raw in ('{"statusLine": "my-real-statusline"}',
                    '{"statusLine": {"type": "command",'
                    ' "command": ["bash", "-c", "real"]}}'):
            cfg = self.config(raw=raw + "\n")
            path = os.path.join(cfg, "settings.json")
            before = self.read(path, "rb")
            proc = self.install(cfg, "--apply")
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
            self.assertEqual(self.read(path, "rb"), before, raw)
            self.assertFalse(os.path.exists(os.path.join(cfg, "usage-logger")),
                             raw)
            self.assertEqual(self.backups(cfg), [])

    @unittest.skipIf(os.name != "posix" or os.geteuid() == 0,
                     "needs POSIX modes and a non-root user")
    def test_a_failed_apply_leaves_nothing_that_blocks_the_next_run(self):
        """Whatever a half-finished install leaves behind, an inner-command is
        the one thing that traps the user: the clobber guard then refuses every
        later attempt, and the undo instructions naming that file print only on
        the success path they never reach. The saved original therefore takes
        that name last, after every step that can fail."""
        cfg = self.config(FULL_SETTINGS)
        elsewhere = os.path.join(tempfile.mkdtemp(dir=TMP), "usage-logger")
        # the backup is written beside settings.json, so an unwritable config
        # dir fails the run at a point where the state dir is already populated
        os.chmod(cfg, 0o500)
        self.addCleanup(os.chmod, cfg, 0o700)
        proc = self.install(cfg, "--apply", logger_dir=elsewhere)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertFalse(os.path.exists(os.path.join(elsewhere,
                                                     "inner-command")))
        self.assertEqual(self.backups(cfg), [])
        self.assertEqual(self.settings(cfg)["statusLine"]["command"], "echo hi")

        os.chmod(cfg, 0o700)
        retry = self.install(cfg, "--apply", logger_dir=elsewhere)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(self.read(os.path.join(elsewhere, "inner-command")),
                         "echo hi")

    def test_a_config_dir_holding_a_space_still_runs(self):
        """Claude Code runs statusLine.command through a shell, so the value
        this installer writes has to survive one. WSL is a supported target and
        /mnt/c/Users/First Last is its ordinary shape: unquoted, the shell
        word-splits it, the statusline exits 127 and goes blank, and the
        installer exits 0 reporting success. Asserted by RUNNING the string
        that landed in settings.json, since comparing it to a path is exactly
        the check that cannot see the difference."""
        cfg = os.path.join(tempfile.mkdtemp(dir=TMP), "D dir")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "settings.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(FULL_SETTINGS, fh, indent=2)
        proc = self.install(cfg, "--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)

        cmd = self.settings(cfg)["statusLine"]["command"]
        env = base_env(CLAUDE_CONFIG_DIR=cfg, HOME=tempfile.mkdtemp(dir=TMP))
        ran = subprocess.run(["bash", "-c", cmd], input=rl_payload(), env=env,
                             capture_output=True, text=True)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(ran.stdout, "hi\n", ran.stderr)
        self.assertTrue(os.path.exists(os.path.join(cfg, "usage-logger",
                                                    "usage-log.jsonl")))
        # and the double-wrap guard still recognizes what it wrote
        again = self.install(cfg, "--apply")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("Already wrapped", again.stdout)

    def test_the_inner_command_refusal_says_how_to_recover(self):
        # the refusal is reachable on every retry, so a message that only
        # names the obstacle leaves the user stuck at it
        cfg = self.config(FULL_SETTINGS)
        os.makedirs(os.path.join(cfg, "usage-logger"))
        inner = os.path.join(cfg, "usage-logger", "inner-command")
        with open(inner, "w", encoding="utf-8") as fh:
            fh.write("my-real-statusline --flag")
        proc = self.install(cfg, "--apply")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("rm -f", proc.stderr)
        self.assertIn(inner, proc.stderr.split("rm -f", 1)[1])

    def test_settings_without_a_statusline_gain_a_valid_one(self):
        cfg = self.config({"cleanupPeriodDays": 365})
        proc = self.install(cfg, "--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sl = self.settings(cfg)["statusLine"]
        self.assertEqual(sl, {"type": "command",
                              "command": self.installed(cfg)})

    def test_a_statusline_missing_its_type_gets_one(self):
        """The only shape in which setdefault does any work: with `type`
        already present it is a no-op, and with no statusLine at all the
        installer takes its isinstance branch instead. Claude Code rejects a
        statusLine without a type, so dropping the setdefault would leave a
        dark status bar after a run that reported success."""
        cfg = self.config({"statusLine": {"command": "echo hi"}})
        proc = self.install(cfg, "--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sl = self.settings(cfg)["statusLine"]
        self.assertEqual(sl["type"], "command")
        self.assertEqual(sl["command"], self.installed(cfg))

    def test_the_logger_dir_override_moves_the_whole_install(self):
        """$CLAUDE_USAGE_LOGGER_DIR is the escape hatch for the divergence
        between where the logger writes and where burnrate reads: burnrate
        looks for the log beside the transcript root it resolved, so a
        $CLAUDE_PROJECTS tree whose parent is not the config dir needs the log
        pointed at that parent instead."""
        cfg = self.config(FULL_SETTINGS)
        elsewhere = os.path.join(tempfile.mkdtemp(dir=TMP), "usage-logger")
        proc = self.install(cfg, "--apply", logger_dir=elsewhere)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        copy = os.path.join(elsewhere, "usage_logger.sh")
        self.assertTrue(os.access(copy, os.X_OK), copy)
        self.assertTrue(os.path.exists(os.path.join(elsewhere,
                                                    "inner-command")))
        self.assertFalse(os.path.exists(os.path.join(cfg, "usage-logger")))
        self.assertEqual(self.settings(cfg)["statusLine"]["command"], copy)


class TestLoggerIsNeverASideEffect(unittest.TestCase):
    """LOG-02: installing the logger wraps the user's status bar, so it must
    only ever happen because the user ran the installer themselves. Nothing in
    the tool or the skill may bootstrap it, and neither may so much as name a
    path it would execute."""

    def clean_home(self):
        """A config home holding transcripts and nothing else: no
        usage-logger, no settings.json."""
        d = tempfile.mkdtemp(dir=TMP)
        shutil.copytree(P["primary"], os.path.join(d, "projects"))
        return d

    def assertInstalledNothing(self, home, before, what):
        self.assertEqual(tree_listing(home), before, what)
        self.assertFalse(os.path.exists(os.path.join(home, "usage-logger")),
                         what)
        self.assertFalse(os.path.exists(os.path.join(home, "settings.json")),
                         what)

    def test_a_dashboard_run_installs_nothing(self):
        home = self.clean_home()
        before = tree_listing(home)
        proc, out = run(env=base_env(CLAUDE_CONFIG_DIR=home))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertInstalledNothing(home, before, "burnrate.py")
        data = payload(out)
        self.assertEqual(data["rl"], [])
        self.assertFalse(data["rl_installed"])

    @unittest.skipUnless(os.path.exists(SKILL_SCRIPT),
                         "no skills/ in this checkout")
    def test_a_skill_run_installs_nothing(self):
        # a REAL run, never --dry-run: cmd_run prints its argv and returns
        # before subprocess.run, so a dry run executes nothing at all and its
        # before/after snapshot would be identical whatever the real path did
        home = self.clean_home()
        before = tree_listing(home)
        env = base_env(CLAUDE_CONFIG_DIR=home, XDG_CACHE_HOME=os.path.join(
            tempfile.mkdtemp(dir=TMP), "cache"),
            CLAUDE_PROJECTS=os.path.join(home, "projects"))
        for args in (["run", "--no-open"], ["ask", "--by", "day"]):
            proc = subprocess.run([sys.executable, SKILL_SCRIPT, *args],
                                  env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertInstalledNothing(home, before, " ".join(args))

    def test_neither_the_tool_nor_the_skill_can_reach_the_installer(self):
        # scoped to INVOCATION, not to the bare substring "extras/": the tool
        # deliberately names extras/usage_logger.sh in its --help and in the
        # report footer, as the thing a user may choose to install
        paths = [BR] + ([SKILL_SCRIPT] if os.path.exists(SKILL_SCRIPT) else [])
        launched = 0
        for path in paths:
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn("settings.json", src, path)
            self.assertNotIn("install_usage_logger.sh", src, path)
            strings = launch_call_strings(path)
            launched += len(strings)
            for lineno, text in strings:
                self.assertNotIn("extras", text, f"{path}:{lineno}")
        self.assertTrue(launched, "no launcher call found: the walk is vacuous")


def _private_tokens():
    """Strings identifying the machine running this suite, derived rather than
    hardcoded so the repo ships no one's identity and every user's run checks
    their own.

    All of these genuinely reach the subprocess: base_env() starts from the
    real environment and overrides only HOME and the XDG vars, and run()
    invokes burnrate by its absolute path, so the account name, the real home
    and burnrate's own source tree are all visible to it. A hit therefore means
    burnrate emitted something the fixture never gave it.

    A token the fixture itself contains cannot tell a leak apart from ordinary
    fixture content, so it is dropped instead of failing the run on a machine
    whose username happens to be a common word. Same for a token under 4
    characters, which would match almost anything.
    """
    try:
        user = getpass.getuser()
    except Exception:               # no passwd entry (some containers)
        user = ""
    fixture = json.dumps(EXP).lower()
    out = []
    for tok in (user, os.path.expanduser("~"), REPO, os.path.dirname(REPO)):
        tok = (tok or "").lower().rstrip("/")
        if len(tok) < 4 or tok in fixture or tok in out:
            continue
        out.append(tok)
    return tuple(out)


class TestPrivacy(unittest.TestCase):
    """AC7: nothing of the machine that ran this survives in a report built
    from a fixture that supplied none of it."""

    TOKENS = _private_tokens()

    def _files(self, out):
        with open(os.path.join(out, "dashboard_data.json"),
                  encoding="utf-8") as fh:
            yield "dashboard_data.json", fh.read()
        yield "dashboard.html", report(out)

    def test_nothing_of_the_environment_leaks(self):
        # scoped so no fixture-supplied token can account for a hit: with an
        # empty config home and --no-archive the hindsight auto-detect cannot
        # fire, so archive_label is never set to the literal "hindsight archive"
        _, out = run(["--no-archive"], env=base_env(XDG_CONFIG_HOME=EMPTY_CONF))
        self.assertTrue(self.TOKENS, "no usable token: the check would pass "
                                     "vacuously")
        for name, text in self._files(out):
            low = text.lower()
            for tok in (*self.TOKENS, "hindsight"):
                self.assertNotIn(tok, low, f"{tok!r} found in {name}")

    def test_nothing_under_extras_carries_an_identity(self):
        """The shipped shell scripts and their doc go out to strangers, and
        every path in them has to be $HOME- or env-derived. os.walk rather
        than git, so this holds in an extracted archive too."""
        self.assertTrue(self.TOKENS, "no usable token: the check would pass "
                                     "vacuously")
        seen = 0
        for dirpath, _dirs, files in os.walk(EXTRAS):
            for name in sorted(files):
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8", errors="replace") as fh:
                    low = fh.read().lower()
                seen += 1
                for tok in self.TOKENS:
                    self.assertNotIn(tok, low, f"{tok!r} found in {path}")
        self.assertTrue(seen, "extras/ is empty: the walk found nothing")

    def test_with_the_archive_on_the_only_hindsight_is_the_label(self):
        if not EXP["zstd"]:
            self.skipTest("zstandard not installed")
        _, out = run(env=base_env(XDG_CONFIG_HOME=P["config_home"]))
        for name, text in self._files(out):
            low = text.lower()
            for tok in self.TOKENS:
                self.assertNotIn(tok, low, f"{tok!r} found in {name}")
            starts = [i for i in range(len(low))
                      if low.startswith("hindsight", i)]
            self.assertTrue(starts, f"expected the label in {name}")
            for i in starts:
                self.assertTrue(low.startswith("hindsight archive", i),
                                f"stray 'hindsight' in {name} at {i}")


class TestHindsightConfig(unittest.TestCase):
    """D-17: the config path honors $XDG_CONFIG_HOME and base_dir may be
    quoted either way, bare, relative or ~-prefixed."""

    def _conf(self, body_tmpl):
        """A config home whose hindsight config names <d>/store as base_dir."""
        d = tempfile.mkdtemp(dir=TMP)
        os.makedirs(os.path.join(d, "hindsight"))
        os.makedirs(os.path.join(d, "store", "archive"))
        with open(os.path.join(d, "hindsight", "config.toml"), "w",
                  encoding="utf-8") as fh:
            fh.write(body_tmpl % os.path.join(d, "store"))
        return d

    def test_quoted_and_bare_values(self):
        for tmpl in ('base_dir = "%s"\n', "base_dir = '%s'\n",
                     "base_dir = %s\n", '[storage]\nbase_dir = "%s"\n'):
            d = self._conf(tmpl)
            self.assertEqual(br.hindsight_archive({"XDG_CONFIG_HOME": d}),
                             os.path.join(d, "store", "archive"),
                             f"failed for {tmpl!r}")

    def test_relative_and_tilde(self):
        d = tempfile.mkdtemp(dir=TMP)
        os.makedirs(os.path.join(d, "hindsight", "store", "archive"))
        with open(os.path.join(d, "hindsight", "config.toml"), "w",
                  encoding="utf-8") as fh:
            fh.write("base_dir = store\n")
        self.assertEqual(br.hindsight_archive({"XDG_CONFIG_HOME": d}),
                         os.path.join(d, "hindsight", "store", "archive"))

        h = tempfile.mkdtemp(dir=TMP)
        os.makedirs(os.path.join(h, "store", "archive"))
        d2 = tempfile.mkdtemp(dir=TMP)
        os.makedirs(os.path.join(d2, "hindsight"))
        with open(os.path.join(d2, "hindsight", "config.toml"), "w",
                  encoding="utf-8") as fh:
            fh.write('base_dir = "~/store"\n')
        self.assertEqual(
            br.hindsight_archive({"XDG_CONFIG_HOME": d2, "HOME": h}),
            os.path.join(h, "store", "archive"))

    def test_absent_config_is_not_an_error(self):
        self.assertIsNone(br.hindsight_archive({"XDG_CONFIG_HOME": EMPTY_CONF}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
