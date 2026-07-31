#!/usr/bin/env python3
"""
burnrate -- on-demand Claude Code burn dashboard, all projects, user-set ranges.

Scans your live transcript tree (--root, else $CLAUDE_PROJECTS, else
$CLAUDE_CONFIG_DIR/projects, else ~/.claude/projects) plus any archive you point
--archive at, parses each session ONCE into a compact per-message cache, and
emits one self-contained HTML dashboard whose date-range and project filters run
entirely client-side. Re-runs only parse sessions whose files changed, so "on
demand" stays fast.

What it measures (per assistant message, deduped globally by .message.id):
    input / cache-write (5m and 1h split) / cache-read / output tokens
    billed-equivalent = input + 1.25*cache_write + 0.10*cache_read
    context footprint = input + cache_write + cache_read  (the live window size)

What it reconstructs:
    - daily burn at (day x project x command x model x effort x main|agent) grain
    - 5-hour rate-limit blocks, account-wide: a block opens at the top of the
      hour of the first activity after the previous block expires (matches how
      resets_at lands on the hour) and lasts 5h
    - per-session summaries: peak context, compactions, interrupts, agents
    - the rate-limit logger's samples (extras/usage_logger.sh), when installed

Attribution: any <command-name> in a user line (or "skill":"..." tool call)
opens a segment that stays open until the next one; a subagent attaches to the
segment open at its first timestamp.

Stdlib + zstandard (archive only). The parse cache is one gzipped file per
source set under the platform's cache directory: $XDG_CACHE_HOME/burnrate
(else ~/.cache/burnrate) on Linux, ~/Library/Caches/burnrate on macOS,
%LOCALAPPDATA%/burnrate on Windows. Delete that directory to force a
full reparse, or pass --rebuild.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

CW, CR = 1.25, 0.10
BLOCK_H = 5 * 3600
CACHE_VER = 2

CMD_RE = re.compile(r"<command-name>/?([A-Za-z0-9:_.-]+)</command-name>")
SKILL_RE = re.compile(r'"skill"\s*:\s*"([A-Za-z0-9:_.-]+)"')
BASE_DIR_RE = re.compile(r"""\s*base_dir\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s#]+))""")

try:
    import zstandard as zstd
except ImportError:
    zstd = None


# ---------------------------------------------------------------- primitives

def parse_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def dglob(directory, *pattern, recursive=False):
    """glob `pattern` under a LITERAL directory.

    Every directory here comes from the user's own filesystem, where '*', '?',
    '[' and ']' are all legal characters -- a project whose cwd is
    /home/alice/[work]/api encodes to a dirname carrying brackets, and a backup
    tree can live anywhere. Handing such a path to glob.glob() unescaped turns
    it into a character class that matches nothing, so the walk silently
    reports zero sessions. glob.escape() applies only to the directory: the
    pattern parts are ours and must stay patterns."""
    return glob.glob(os.path.join(glob.escape(directory), *pattern),
                     recursive=recursive)


def expanduser(p, env=None):
    """expanduser against an injectable environment, so the resolver can be
    exercised with a faked HOME without mutating the process."""
    env = os.environ if env is None else env
    if p.startswith("~") and (len(p) == 1 or p[1] in "/\\"):
        home = (env.get("HOME") or env.get("USERPROFILE")
                or os.path.expanduser("~"))
        p = home + p[1:]
    return p


def hindsight_archive(env=None):
    """The hindsight archive directory, or None. The config lives under
    $XDG_CONFIG_HOME (falling back to ~/.config) and its base_dir may be
    double-quoted, single-quoted or bare, absolute, relative or ~-prefixed."""
    env = os.environ if env is None else env
    xdg = env.get("XDG_CONFIG_HOME")
    base = xdg if xdg and os.path.isabs(xdg) else expanduser("~/.config", env)
    conf = os.path.join(base, "hindsight", "config.toml")
    try:
        with open(conf, "r", encoding="utf-8") as fh:
            for line in fh:
                m = BASE_DIR_RE.match(line)
                if not m:
                    continue
                val = next(g for g in m.groups() if g is not None).strip()
                if not val:
                    continue
                val = expanduser(val, env)
                if not os.path.isabs(val):
                    val = os.path.join(os.path.dirname(conf), val)
                p = os.path.join(val, "archive")
                return p if os.path.isdir(p) else None
    except OSError:
        pass
    return None


def file_uri(path, flavour=None):
    """A percent-encoded file: URI. Concatenating "file://" onto a path is
    wrong for a Windows drive letter and for any path holding a space, '#' or
    '%'. The path is absolutized here, not at the call site: as_uri() raises on
    a relative path, so `--out reports` would otherwise crash the run *after*
    the report was already written."""
    if flavour is None:
        return pathlib.Path(os.path.abspath(path)).as_uri()
    cls = pathlib.PureWindowsPath if flavour == "win32" else pathlib.PurePosixPath
    p = cls(path)
    if not p.is_absolute():
        p = cls("C:/" if flavour == "win32" else "/") / p
    return p.as_uri()


LAUNCH_SETTLE = 0.4


def open_report(path, env=None, platform=None):
    """Launch the default browser on `path`, detached, and report whether a
    browser was opened as far as that is knowable without waiting for it to
    exit. Returns False without raising, so the caller can print the
    open-it-yourself note.

    Three things make the obvious `webbrowser.open(uri)` wrong here, all of
    them observed rather than theoretical:

    1. `webbrowser` resolves most $BROWSER values -- and every browser at all
       on a host with no registered entry -- to GenericBrowser, whose open()
       ends `return not p.wait()`. It returns when the browser *exits*, not
       when it launches, so a cold browser holds the call for as long as the
       user keeps the window open.
    2. That child inherits our stdout/stderr, so it holds the pipe of anyone
       who captured our output (`out=$(burnrate.py)`, a CI step, a wrapper
       script) for its whole lifetime. Backgrounding the call inside this
       process does nothing about that -- the fd is already shared.
    3. With neither a display nor a $BROWSER, `webbrowser` registers terminal
       browsers (w3m, lynx, links). Launching one steals the tty and leaves
       the terminal in its alternate screen when we exit out from under it.

    So the launch goes to a detached grandchild with /dev/null stdio and its
    own session/process group, and we wait only LAUNCH_SETTLE for it. That
    window is the whole trick: `webbrowser.open()` returning False -- no such
    binary, osascript refused, no registered browser -- happens immediately,
    while a browser that actually opened either exits 0 fast (it handed the
    URL to a running instance) or holds the grandchild well past the window.
    So a grandchild still alive at the deadline means a browser is up, and a
    nonzero exit inside it means nothing opened. What this cannot detect is a
    browser that starts, stays up, and fails later -- no caller can, short of
    blocking until it exits, which is the behavior being removed.

    The display check is deliberately last-resort: an explicit $BROWSER wins
    over it, because WSL (`wslview`) and VS Code Remote-SSH both export one
    and have no DISPLAY, and refusing there would silently stop opening the
    report for them."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if (platform not in ("darwin", "win32") and not env.get("BROWSER")
            and not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))):
        return False
    try:
        webbrowser.get()  # raises webbrowser.Error when nothing is usable
        if platform == "win32":
            # start_new_session is POSIX-only; without these the grandchild
            # and the browser it starts stay attached to the caller's console
            # and die with it (CTRL_CLOSE_EVENT) after we reported success.
            kw = {"creationflags": getattr(subprocess, "DETACHED_PROCESS", 0)
                  | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        else:
            kw = {"start_new_session": True}
        p = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, webbrowser; sys.exit(0 if "
             "webbrowser.open(sys.argv[1]) else 1)",
             file_uri(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, **kw)
    except Exception:
        return False
    try:
        return p.wait(timeout=LAUNCH_SETTLE) == 0
    except subprocess.TimeoutExpired:
        return True  # still holding the browser open


def arch_sub_chunks(sub_root):
    """Every archived subagent chunk under one session's subagents/ directory:
    subagents/agent-N/*.zst and subagents/workflows/wf_*/agent-N/*.zst. A chunk
    counts only when its own directory is an agent-*, which keeps journal.jsonl
    and any other sidecar out.

    classify_tree and _discover_arch both go through here, so the sniff that
    decides whether a tree is readable cannot drift from the walk that reads
    it."""
    return [c for c in dglob(sub_root, "**", "*.zst", recursive=True)
            if os.path.basename(os.path.dirname(c)).startswith("agent-")]


def classify_tree(path):
    """Which layouts a directory holds, in read order: "live" for
    <proj>/*.jsonl (a plain backup of a transcript tree), "arch" for a
    hindsight-shaped archive, both when the directory carries both shapes, ()
    for neither.

    Every layout found is returned, not just the first. Stopping at "live"
    ingested only the jsonl half of a mixed directory -- one a user gets by
    dropping a backup tree beside an archive, or by archiving in place -- while
    the run still reported archive_used: true, so the shortfall looked like
    nothing at all.

    The "arch" sniff asks the same question the walk does, which is whether any
    session has a chunk ANYWHERE, not just at <proj>/<sid>/*.zst. A session
    whose only chunks are its subagents' is a session _discover_arch reads, so
    a tree of nothing but those used to be refused by the sniff and accepted by
    the walk: as --root it warned "no project directories" and was read
    live-shaped for zero sessions, as --archive it was skipped outright."""
    if not os.path.isdir(path):
        return ()
    found = []
    if dglob(path, "*", "*.jsonl"):
        found.append("live")
    if dglob(path, "*", "*", "*.zst") or any(
            arch_sub_chunks(s) for s in dglob(path, "*", "*", "subagents")):
        found.append("arch")
    return tuple(found)


def resolve_sources(args, env=None):
    """Everything the run reads, derived from flags and the environment.

    Root precedence: --root > $CLAUDE_PROJECTS > $CLAUDE_CONFIG_DIR/projects >
    ~/.claude/projects. The rate-limit log follows the resolved root, so a
    redirected root redirects every read rather than some of them."""
    env = os.environ if env is None else env
    root = getattr(args, "root", None) or env.get("CLAUDE_PROJECTS")
    if not root and env.get("CLAUDE_CONFIG_DIR"):
        root = os.path.join(env["CLAUDE_CONFIG_DIR"], "projects")
    root = os.path.abspath(expanduser(root or "~/.claude/projects", env))
    if not os.path.isdir(root):
        print(f"error: transcript root not found: {root}", file=sys.stderr)
        sys.exit(2)
    # the root gets the same layout sniff --archive gets. Assuming it is
    # live-shaped reads an archive-shaped one as zero sessions -- 0 projects,
    # exit 0, not a word on stderr -- and a guard that only asks whether
    # subdirectories exist says nothing about a root holding no transcripts.
    root_layouts = classify_tree(root)
    if not root_layouts:
        print(f"warning: no project directories under {root}", file=sys.stderr)
        root_layouts = ("live",)

    no_archive = bool(getattr(args, "no_archive", False))
    if no_archive:
        # "every archive source, auto-detected or not" includes the archived
        # half of the root itself. A tree archived in place carries zst frames
        # beside its jsonl, and reading those under a header that renders
        # "live transcripts only" is the report misstating its own sources.
        live_only = tuple(k for k in root_layouts if k != "arch")
        if live_only != root_layouts:
            print("warning: --no-archive skips the archived transcripts "
                  f"under {root}", file=sys.stderr)
        root_layouts = live_only or ("live",)

    config_dir = os.path.dirname(root)
    sources = [(k, root) for k in root_layouts]
    label = None
    if no_archive:
        pass  # the root's own arch half is already dropped, above
    elif getattr(args, "archive", None):
        adir = os.path.abspath(expanduser(args.archive, env))
        if not os.path.isdir(adir):
            print(f"error: archive directory not found: {adir}",
                  file=sys.stderr)
            sys.exit(2)
        layouts = classify_tree(adir)
        if not layouts:
            print(f"warning: --archive {adir} matches no known layout "
                  "-- skipped", file=sys.stderr)
        else:
            sources.extend((k, adir) for k in layouts)
            # basename only: a report must not embed the user's directory tree
            label = "archive: " + (os.path.basename(adir.rstrip("/\\")) or adir)
    else:
        arch = hindsight_archive(env)
        layouts = classify_tree(arch) if arch else ()
        if layouts:
            sources.extend((k, arch) for k in layouts)
            label = "hindsight archive"
    if zstd is None and any(k == "arch" for k, _ in sources):
        if any(k == "arch" and p != root for k, p in sources):
            print("warning: zstandard not installed -- archive skipped",
                  file=sys.stderr)
        if any(k == "arch" and p == root for k, p in sources):
            # the root is the user's own --root, never "the archive": calling
            # it that sent a reader looking for an archive flag they never
            # passed, while the D-18 emptiness warning stayed silent because
            # the sniff HAD matched a layout
            print("warning: zstandard not installed -- archived transcripts "
                  f"under {root} skipped", file=sys.stderr)
        sources = [(k, p) for k, p in sources if k != "arch"]
    if not any(p == root for _k, p in sources):
        # the root stays in the list even when nothing under it is readable.
        # cache_path keys off the first entry, and an empty list hashes the
        # empty string, so every root that lost its only layout collided on one
        # cache file -- each run overwriting the last one's sessions.
        sources.insert(0, ("live", root))
    # by PATH, not by source count: a mixed root contributes two sources and
    # no archive at all, and a dropped arch source contributes neither
    used = any(p != root for _k, p in sources)
    if not used:
        label = None
    return {"root": root, "config_dir": config_dir,
            "rl_log": os.path.join(config_dir, "usage-logger",
                                   "usage-log.jsonl"),
            "sources": sources, "archive_label": label, "archive_used": used}


def path_segments(p):
    return [s for s in re.split(r"[/\\]+", p) if s]


def canon_path(cwd):
    """The project a cwd belongs to. A worktree checkout lives under
    <project>/.claude/worktrees/<name>, so truncating there folds its work back
    into the parent project, which is the same work."""
    segs = path_segments(cwd)
    for i in range(len(segs) - 1):
        if segs[i] == ".claude" and segs[i + 1] == "worktrees":
            segs = segs[:i]
            break
    if not segs:
        return cwd
    return ("/" if cwd[:1] in "/\\" else "") + "/".join(segs)


def project_paths(sessions):
    """{project dir: canonical project path}. The path comes from the cwd the
    transcripts themselves carry: the encoded dirname is ambiguous by
    construction, since Claude Code maps both '/' and '.' to '-'. With no cwd
    anywhere in a directory, the dirname is the label, kept verbatim as one
    opaque segment rather than guessed back into separators."""
    votes = defaultdict(Counter)
    for key in sorted(sessions):
        ent = sessions[key]
        votes[ent["proj"]][(ent.get("rec") or {}).get("cwd") or ""] += 1
    out = {}
    for proj, counts in votes.items():
        real = {v: n for v, n in counts.items() if v}
        if real:
            # ties broken lexicographically so the label is deterministic
            out[proj] = canon_path(min(real.items(),
                                       key=lambda kv: (-kv[1], kv[0]))[0])
        else:
            out[proj] = proj.split("--claude-worktrees")[0].lstrip("-") or proj
    return out


def label_paths(paths):
    """{path: label}, one distinct label per distinct path.

    Every path starts labeled with its last segment; each still-colliding group
    then lengthens together, one leading segment at a time, until the labels
    part or a path runs out of segments. A group still colliding at exhaustion
    (one path is a proper suffix of another, e.g. a restored backup) keeps the
    shortest path's full label and suffixes the rest #2, #3 in path order.
    Distinctness is the point: aggregate keys per-project totals by label, so
    two entities sharing a label would silently sum into one row."""
    uniq = sorted(set(paths))
    segs = {p: (path_segments(p) or [p]) for p in uniq}
    depth = {p: 1 for p in uniq}

    def lab(p):
        return "/".join(segs[p][-depth[p]:])

    def grouped():
        g = defaultdict(list)
        for p in uniq:
            g[lab(p)].append(p)
        return g

    while True:
        grew = False
        for group in grouped().values():
            if len(group) > 1 and all(depth[p] < len(segs[p]) for p in group):
                for p in group:
                    depth[p] += 1
                grew = True
        if not grew:
            break

    out, used = {}, set()

    def claim(label):
        """The first caller keeps `label`; every later one takes label#2, #3,
        ... skipping any spelling already spoken for. Checking against the
        labels actually handed out is the whole point: '#2' appended blindly
        can land on a label some other path legitimately owns -- a project
        whose own last segment is 'api#2' -- and two entities sharing a label
        silently sum into one row, since aggregate keys per-project totals by
        label."""
        cand, i = label, 1
        while cand in used:
            i += 1
            cand = f"{label}#{i}"
        used.add(cand)
        return cand

    groups = sorted(grouped().items())
    # Every group's own label is claimed before any tiebreak suffix is handed
    # out, so a path that already reduces to a label keeps it and the #N moves
    # instead. Reserving only the SINGLETON labels was not enough: group labels
    # are distinct by construction, so this always succeeds unsuffixed, but a
    # colliding group processed earlier could still eat the base label a later
    # colliding group owned -- the path literally named 'api#2' ended up
    # labeled 'api#2#2' while a path named 'api' took its spelling.
    keeper = {base: min(group, key=lambda p: (len(segs[p]), p))
              for base, group in groups}
    for base, group in groups:
        out[keeper[base]] = claim(base)
    for base, group in groups:
        for p in sorted(x for x in group if x != keeper[base]):
            out[p] = claim(base)
    return out


def live_lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                yield line
    except OSError:
        return


def arch_lines(unit_dir):
    """All generations of one archived file, oldest first; generations overlap
    and the message-id dedupe absorbs that."""
    if zstd is None:
        return
    dctx = zstd.ZstdDecompressor()
    for chunk in sorted(dglob(unit_dir, "*.zst")):
        try:
            with open(chunk, "rb") as fh:
                with dctx.stream_reader(fh) as reader:
                    buf = b""
                    while True:
                        block = reader.read(1 << 20)
                        if not block:
                            break
                        buf += block
                        *whole, buf = buf.split(b"\n")
                        for line in whole:
                            if line:
                                yield line.decode("utf-8", "replace")
                    if buf:
                        yield buf.decode("utf-8", "replace")
        except Exception:
            continue


def iters_for(specs):
    return [live_lines(p) if k == "live" else arch_lines(p) for k, p in specs]


# ---------------------------------------------------------------- discovery

def _discover_live(live_root, sessions):
    for proj_dir in sorted(dglob(live_root, "*")):
        if not os.path.isdir(proj_dir):
            continue
        proj = os.path.basename(proj_dir)
        for main_path in sorted(dglob(proj_dir, "*.jsonl")):
            sid = os.path.basename(main_path)[:-6]
            s = sessions[proj][sid]
            if ("live", main_path) not in s["main"]:
                s["main"].append(("live", main_path))
            sub_root = os.path.join(proj_dir, sid, "subagents")
            for ap in (dglob(sub_root, "agent-*.jsonl")
                       + dglob(sub_root, "workflows", "wf_*",
                               "agent-*.jsonl")):
                key = os.path.basename(ap)[:-6]
                if ("live", ap) not in s["subs"][key]:
                    s["subs"][key].append(("live", ap))
                meta_p = ap[:-6] + ".meta.json"
                if os.path.exists(meta_p):
                    try:
                        with open(meta_p, "r", encoding="utf-8") as mf:
                            at = (json.load(mf) or {}).get("agentType")
                        if at:
                            s["meta"][key] = at
                    except Exception:
                        pass


def _discover_arch(arch_root, sessions):
    for proj_dir in sorted(dglob(arch_root, "*")):
        if not os.path.isdir(proj_dir):
            continue
        proj = os.path.basename(proj_dir)
        for sid_dir in sorted(dglob(proj_dir, "*")):
            if not os.path.isdir(sid_dir):
                continue
            sid = os.path.basename(sid_dir)
            chunks = dglob(sid_dir, "*.zst")
            subs = arch_sub_chunks(os.path.join(sid_dir, "subagents"))
            if not chunks and not subs:
                # Every subdirectory used to become a session the moment it was
                # looked up on the defaultdict, before anything checked it held
                # a chunk. Now that the arch walk also runs over live-shaped
                # roots, a stray <proj>/notes-dir counted as a session with an
                # empty signature -- which matches trivially, so it was also
                # cached forever, and the header's session count ran ahead of
                # the payload.
                continue
            s = sessions[proj][sid]
            if chunks and ("arch", sid_dir) not in s["main"]:
                s["main"].append(("arch", sid_dir))
            for chunk in subs:
                unit = os.path.dirname(chunk)
                key = os.path.basename(unit)
                spec = ("arch", unit)
                if spec not in s["subs"][key]:
                    s["subs"][key].append(spec)


def discover(sources):
    """{proj_dir: {sid: {'main': [(kind,path)], 'subs': {key: [(kind,path)]},
                         'meta': {key: agentType}}}} merged across every
    resolved source, primary first."""
    sessions = defaultdict(lambda: defaultdict(
        lambda: {"main": [], "subs": defaultdict(list), "meta": {}}))
    for layout, path in sources:
        if not os.path.isdir(path):
            continue
        (_discover_live if layout == "live" else _discover_arch)(path, sessions)
    return sessions


def session_sig(s):
    """Fingerprint of every file feeding this session; cache hit = unchanged.

    Paths are realpath'd, the same normalization cache_path applies to the
    cache key, because the two have to agree. Reaching one tree by two names --
    ~/.claude symlinked into a dotfiles checkout, a bind mount, a trailing
    separator from shell completion -- otherwise shares ONE cache file whose
    every entry each run rewrites under the other name's paths, so alternating
    roots reparses the whole tree forever."""
    files = []
    for kind, path in s["main"]:
        files.extend([path] if kind == "live"
                     else sorted(dglob(path, "*.zst")))
    for specs in s["subs"].values():
        for kind, path in specs:
            files.extend([path] if kind == "live"
                         else sorted(dglob(path, "*.zst")))
    sig = []
    for f in sorted(os.path.realpath(p) for p in files):
        try:
            st = os.stat(f)
            sig.append([f, int(st.st_mtime), st.st_size])
        except OSError:
            continue
    return sig


# ---------------------------------------------------------------- parsing

def scan_unit(iters, unit, rec, want_meta):
    """Stream one logical file (live + archive generations). Appends usage rows
    and, for the main transcript, segments and session metadata. Dedupe within
    the session: message rows by message.id, marker lines by uuid/value."""
    rows, mids = rec["rows"], rec["_mids"]
    marks = rec["_marks"]
    for it in iters:
        for line in it:
            if '"timestamp"' not in line:
                continue
            has_usage = '"usage"' in line
            is_seg = want_meta and ("<command-name>" in line or '"skill"' in line)
            is_mark = want_meta and (
                '"compact_boundary"' in line or '"isCompactSummary":true' in line
                or '"interruptedMessageId"' in line
                or '"isApiErrorMessage":true' in line
                or '"aiTitle"' in line)
            if not (has_usage or is_seg or is_mark):
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            ts = parse_ts(d.get("timestamp") or "")
            if ts is None:
                continue
            if want_meta:
                if d.get("slug"):
                    rec["slug"] = d["slug"]
                gb = d.get("gitBranch")
                if gb:
                    rec["_branch"][gb] += 1
                cwd = d.get("cwd")
                if cwd:
                    rec["_cwd"][cwd] += 1
            if is_mark:
                if d.get("type") == "ai-title" and d.get("aiTitle"):
                    rec["title"] = d["aiTitle"]
                key = None
                if d.get("subtype") == "compact_boundary" or d.get("isCompactSummary"):
                    key = ("comp", d.get("uuid") or d.get("timestamp"))
                elif d.get("interruptedMessageId"):
                    key = ("intr", d["interruptedMessageId"])
                elif d.get("isApiErrorMessage"):
                    key = ("err", d.get("uuid") or d.get("timestamp"))
                if key and key not in marks:
                    marks.add(key)
                    rec[key[0]] += 1
            if is_seg:
                uid = d.get("uuid")
                if uid is None or ("seg", uid) not in marks:
                    m = CMD_RE.search(line) if d.get("type") == "user" else None
                    if m is None:
                        m = SKILL_RE.search(line)
                    if m:
                        if uid is not None:
                            marks.add(("seg", uid))
                        rec["segs"].append([ts, m.group(1)])
            if not has_usage or d.get("type") != "assistant":
                continue
            msg = d.get("message") or {}
            if msg.get("model") == "<synthetic>":
                continue
            mid = msg.get("id")
            u = msg.get("usage") or {}
            if not mid or mid in mids or not u:
                continue
            mids.add(mid)
            cc = u.get("cache_creation_input_tokens") or 0
            cc1h = (u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0
            rows.append([mid, int(ts), msg.get("model") or "?",
                         d.get("effort") or "-", unit,
                         u.get("input_tokens") or 0, cc, min(cc1h, cc),
                         u.get("cache_read_input_tokens") or 0,
                         u.get("output_tokens") or 0])


def parse_session(s):
    rec = {"rows": [], "segs": [], "slug": None, "title": None,
           "comp": 0, "intr": 0, "err": 0, "agents": {},
           "_mids": set(), "_marks": set(), "_branch": Counter(),
           "_cwd": Counter()}
    scan_unit(iters_for(s["main"]), "", rec, want_meta=True)
    for key, specs in sorted(s["subs"].items()):
        rec["agents"][key] = s["meta"].get(key) or "unresolved"
        scan_unit(iters_for(specs), key, rec, want_meta=False)
    rec["segs"].sort(key=lambda x: x[0])
    rec["branch"] = (rec["_branch"].most_common(1) or [(None, 0)])[0][0]
    rec["cwd"] = (min(rec["_cwd"].items(), key=lambda kv: (-kv[1], kv[0]))[0]
                  if rec["_cwd"] else None)
    for k in ("_mids", "_marks", "_branch", "_cwd"):
        del rec[k]
    return rec


# ---------------------------------------------------------------- cache

def cache_dir(platform=None, env=None):
    """The platform's own cache directory, plus burnrate/."""
    platform = sys.platform if platform is None else platform
    env = os.environ if env is None else env
    if platform == "win32":
        base = env.get("LOCALAPPDATA") or expanduser("~/AppData/Local", env)
    elif platform == "darwin":
        base = expanduser("~/Library/Caches", env)
    else:
        xdg = env.get("XDG_CACHE_HOME")
        base = xdg if xdg and os.path.isabs(xdg) else expanduser("~/.cache",
                                                                 env)
    return os.path.join(base, "burnrate")


def cache_path(sources, platform=None, env=None):
    """One cache file per source set, so a run against another root - or with
    --no-archive - never evicts the normal run's entries. realpath first: a
    trailing separator or a symlink must not fork the cache.

    The LAYOUT is part of the key, not just the path. One directory read as
    "arch" and the same directory read as "live" discover entirely different
    sessions, and an arch-only root reaches the second form on two ordinary
    routes: --no-archive strips its arch half, and so does a zstandard that
    stopped importing between two runs. Keying on paths alone made those two
    runs share a file, so the one that discovers nothing wrote an empty session
    map over the full run's entries and the next real run reparsed the tree.

    Each component is hashed rather than joined on a delimiter. A path may
    legally contain a tab or a newline on POSIX, and a delimited join lets such
    a path forge a record boundary: one source named "/a\\narch\\t/b" and the
    two sources ("live", "/a") + ("arch", "/b") produce the same joined string,
    so two unrelated source sets share a file and evict each other."""
    real = [(k, os.path.realpath(p)) for k, p in sources]

    def digest(pair):
        k, p = pair
        return hashlib.sha256(f"{k}\0{p}".encode("utf-8")).hexdigest()

    key = "".join([digest(real[0])] + sorted(digest(x) for x in real[1:])) \
        if real else ""
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return os.path.join(cache_dir(platform, env), f"sessions-{h}.json.gz")


def load_cache(path):
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            c = json.load(fh)
        if c.get("ver") == CACHE_VER:
            return c["sessions"]
    except Exception:
        pass
    return {}


def save_cache(path, sessions):
    """Best effort, and deliberately so: the cache is an optimization, and it
    is written from inside collect() -- before the report exists. A read-only
    $HOME, a full disk, or a root-owned cache directory left by an earlier
    sudo run would otherwise end a run in which every session parsed fine with
    a traceback, exit 1 and an empty output directory. Warn and carry on; the
    only cost of not writing it is that the next run reparses. Returns whether
    the cache was written."""
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump({"ver": CACHE_VER, "sessions": sessions}, fh,
                      separators=(",", ":"))
        os.replace(tmp, path)
        return True
    except OSError as e:
        print(f"warning: cache not written ({e}); the next run reparses",
              file=sys.stderr)
        return False


def collect(cache_path, sources, rebuild, quiet):
    found = discover(sources)
    cache = {} if rebuild else load_cache(cache_path)
    out, parsed, total = {}, 0, sum(len(v) for v in found.values())
    done = 0
    for proj, sids in sorted(found.items()):
        for sid, s in sorted(sids.items()):
            done += 1
            key = f"{proj}/{sid}"
            sig = session_sig(s)
            hit = cache.get(key)
            if hit and hit["sig"] == sig:
                out[key] = hit
                continue
            parsed += 1
            if not quiet and parsed % 50 == 0:
                print(f"  parsing {done}/{total} sessions "
                      f"({parsed} changed)...", file=sys.stderr)
            out[key] = {"sig": sig, "proj": proj, "sid": sid,
                        "rec": parse_session(s)}
    save_cache(cache_path, out)
    if not quiet:
        print(f"  {total} sessions, {parsed} parsed, "
              f"{total - parsed} from cache", file=sys.stderr)
    return out


# ---------------------------------------------------------------- aggregate

def be_of(r):
    _, _, _, _, _, inp, cc, _, cr, _ = r
    return inp + CW * cc + CR * cr


def seg_at(segs, ts):
    cur = ""
    for sts, name in segs:
        if sts <= ts:
            cur = name
        else:
            break
    return cur


def read_rl_log(rl_log):
    """rl_log is required, never defaulted: a missed call site must be a
    TypeError, not a silent read of the real ~/.claude tree."""
    rows = []
    for path in (rl_log + ".1", rl_log):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                        ts = parse_ts(d.get("ts") or "")
                        if ts:
                            rows.append([int(ts), d.get("five_hour"),
                                         d.get("seven_day")])
                    except Exception:
                        continue
        except OSError:
            continue
    # Sort on the timestamp ALONE, never on the whole row. Either window can be
    # absent (the payload carries them independently), so a default sort
    # compares None against a float the moment two samples share a second and
    # raises TypeError out of here, past every per-line guard, and the run
    # produces no report at all. Sorting on ts is also what keeps ties in file
    # order -- Python's sort is stable, and file order is write order, so the
    # viewer's lastOf() reports the LAST sample of a second rather than the
    # largest one, which is what a 5h window reset looks like.
    rows.sort(key=lambda r: r[0])
    return rows


def aggregate(sessions, tz_offset, rl_log):
    if tz_offset is None:
        # naive fromtimestamp delegates to the OS's own local-time conversion,
        # so days follow DST on every platform. zoneinfo would need system
        # tzdata that minimal containers lack and Windows does not ship.
        def day_of(ts):
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

        gen = datetime.now().astimezone()
        offset = (gen.utcoffset() or timedelta()).total_seconds() / 3600.0
    else:
        tz = timezone(timedelta(hours=tz_offset))

        def day_of(ts):
            return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")

        gen = datetime.now(tz)
        offset = tz_offset

    # global message-id dedupe across sessions (resumed/forked sessions can
    # replay identical assistant lines); deterministic order = stable winners
    ppaths = project_paths(sessions)
    labels = label_paths(ppaths.values())
    plab = {proj: labels[path] for proj, path in ppaths.items()}

    seen = set()
    proj_be = Counter()
    all_rows = []           # (ts, proj_label, cmd, model, effort, unit, row)
    sess_rows = []
    for key in sorted(sessions):
        ent = sessions[key]
        rec, proj = ent["rec"], plab[ent["proj"]]
        rows = [r for r in rec["rows"] if not (r[0] in seen or seen.add(r[0]))]
        if not rows:
            continue
        segs = rec["segs"]
        agent_first = {}
        for r in rows:
            if r[4] and r[4] not in agent_first:
                agent_first[r[4]] = r[1]
        peak_ctx = 0
        t0 = t1 = rows[0][1]
        be = out = 0.0
        for r in rows:
            ts, unit = r[1], r[4]
            cmd = seg_at(segs, agent_first.get(unit, ts) if unit else ts)
            all_rows.append((ts, proj, cmd, r[2], r[3], "a" if unit else "m", r))
            be += be_of(r)
            out += r[9]
            t0, t1 = min(t0, ts), max(t1, ts)
            if not unit:
                peak_ctx = max(peak_ctx, r[5] + r[6] + r[8])
        proj_be[proj] += be
        sess_rows.append([ent["sid"][:8], proj,
                          rec.get("title") or rec.get("slug") or "",
                          t0, t1, round(be), round(out), len(rows), peak_ctx,
                          rec["comp"], len(rec["agents"]), rec["intr"]])

    projects = [p for p, _ in proj_be.most_common()]
    pidx = {p: i for i, p in enumerate(projects)}

    daily = defaultdict(lambda: [0.0] * 8)   # be,in,cc,cc1h,cr,out,msgs
    for ts, proj, cmd, model, effort, kind, r in all_rows:
        k = (day_of(ts), pidx[proj], cmd, model, effort, kind)
        a = daily[k]
        a[0] += be_of(r)
        a[1] += r[5]
        a[2] += r[6]
        a[3] += r[7]
        a[4] += r[8]
        a[5] += r[9]
        a[6] += 1
    daily_rows = [[d, p, c, m, e, k, round(a[0]), round(a[1]), round(a[2]),
                   round(a[3]), round(a[4]), round(a[5]), int(a[6])]
                  for (d, p, c, m, e, k), a in sorted(daily.items())]

    # 5h blocks, account-wide: window opens at the top of the hour of the first
    # activity after the previous window ends, lasts 5h
    blocks = []
    cur = None
    for ts, proj, *_rest, r in sorted(all_rows, key=lambda x: x[0]):
        if cur is None or ts >= cur["t0"] + BLOCK_H:
            cur = {"t0": (ts // 3600) * 3600, "first": ts, "last": ts,
                   "be": 0.0, "out": 0.0, "n": 0, "pp": Counter()}
            blocks.append(cur)
        b = be_of(r)
        cur["be"] += b
        cur["out"] += r[9]
        cur["n"] += 1
        cur["last"] = max(cur["last"], ts)
        cur["pp"][pidx[proj]] += b
    block_rows = [[b["t0"], b["first"], b["last"], round(b["be"]),
                   round(b["out"]), b["n"],
                   {str(i): round(v) for i, v in b["pp"].most_common(6)}]
                  for b in blocks]

    sess_rows.sort(key=lambda s: -s[5])
    return {
        "v": 1,
        "generated": gen.isoformat(timespec="seconds"),
        # a single number by design: the viewer uses it only for block times
        # and block-to-day placement, never for the day buckets above
        "tz_offset": offset,
        "projects": [{"id": p, "be": round(proj_be[p])} for p in projects],
        "daily": daily_rows,
        "blocks": block_rows,
        "sessions": sess_rows[:400],
        "rl": read_rl_log(rl_log),
    }


# ---------------------------------------------------------------- main

def tz_hours(s):
    """--tz-offset as argparse should see it: a UTC offset in hours, strictly
    inside (-24, 24), which is exactly the range datetime.timezone accepts.
    Validated here so a bad value fails like every other bad argument -- usage
    message, exit 2, nothing read -- instead of surviving as a plain float and
    raising a ValueError out of aggregate() once the whole tree is parsed."""
    try:
        v = float(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {s}")
    if not -24 < v < 24:        # also rejects nan and inf
        raise argparse.ArgumentTypeError(
            f"must be hours strictly between -24 and 24: {s}")
    return v


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tz-offset", type=tz_hours, default=None,
                    metavar="HOURS",
                    help="bucket days at a fixed UTC offset instead of the "
                         "system's local time (which is the default and "
                         "follows DST)")
    ap.add_argument("--range", choices=["7", "14", "30", "90", "all"],
                    default="30", metavar="{7,14,30,90,all}",
                    help="date window the opened report starts on, in days "
                         "(or 'all'); the report's own preset buttons still "
                         "change it client-side")
    ap.add_argument("--root", metavar="DIR",
                    help="transcript tree; default $CLAUDE_PROJECTS, else "
                         "$CLAUDE_CONFIG_DIR/projects, else ~/.claude/projects")
    ap.add_argument("--out", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--archive", metavar="DIR",
                    help="extra transcript source: either a <proj>/*.jsonl "
                         "tree or a <proj>/<sid>/*.zst archive")
    ap.add_argument("--no-archive", action="store_true",
                    help="ignore every archive source, auto-detected or not")
    ap.add_argument("--rebuild", action="store_true",
                    help="ignore the cache and reparse everything")
    ap.add_argument("--json", action="store_true",
                    help="also write dashboard_data.json")
    ap.add_argument("--no-open", action="store_true",
                    help="write the report without opening a browser")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    src = resolve_sources(a)
    os.makedirs(a.out, exist_ok=True)
    cpath = cache_path(src["sources"])
    if not a.quiet:
        print(f"scanning transcripts... (cache {cpath})", file=sys.stderr)
    sessions = collect(cpath, src["sources"], a.rebuild, a.quiet)
    data = aggregate(sessions, a.tz_offset, src["rl_log"])
    data["archive_used"] = src["archive_used"]
    data["archive_label"] = src["archive_label"]
    data["rl_installed"] = os.path.exists(src["rl_log"])

    if a.json:
        with open(os.path.join(a.out, "dashboard_data.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)

    hpath = os.path.join(a.out, "dashboard.html")
    # Escaping every "<" is the only sufficient defense. Replacing just "</"
    # leaves "<!--" and "<script" untouched, and an unclosed "<!--" in one
    # session title followed by "<script" in any later one drives the HTML
    # tokenizer into script-data-double-escaped state, where the page's own
    # </script> no longer closes the element. "\\u003c" is valid JSON string
    # content and parses back to "<" unchanged. Every "<" in the blob is
    # inside a string value -- JSON's structural characters are {}[]",: --
    # so this cannot corrupt the payload.
    blob = json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")
    # preset FIRST, data SECOND: the blob carries user strings (session
    # titles), and substituting it first would let a title containing the
    # literal __PRESET__ be rewritten by the second pass.
    with open(hpath, "w", encoding="utf-8") as fh:
        fh.write(PAGE.replace("__PRESET__", a.range).replace("__DATA__", blob))

    tot = sum(r[6] for r in data["daily"])
    days = sorted({r[0] for r in data["daily"]})
    print(f"range   : {days[0]} -> {days[-1]} ({len(days)} active days)"
          if days else "range   : no data")
    print(f"billed  : {tot:,.0f} billed-equiv tokens, "
          f"{len(data['blocks'])} five-hour blocks, "
          f"{len(data['projects'])} projects")
    print(f"rl log  : {'%d samples' % len(data['rl']) if data['rl'] else 'not installed'}")
    print(f"wrote   : {hpath}")

    if not a.no_open and not open_report(hpath):
        print(f"note    : no browser opened; open {hpath} yourself",
              file=sys.stderr)


PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Code burn</title>
<style>
:root{color-scheme:light dark}
.viz-root{
  color-scheme:light;
  --page:#f9f9f7; --surface-1:#fcfcfb;
  --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,.10);
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100;
  --s5:#e87ba4; --s6:#008300; --s7:#4a3aa7; --s8:#e34948;
  --other:#b7b5ac;
}
@media (prefers-color-scheme: dark){
  :root:where(:not([data-theme="light"])) .viz-root{
    color-scheme:dark;
    --page:#0d0d0d; --surface-1:#1a1a19;
    --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
    --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500;
    --s5:#d55181; --s6:#008300; --s7:#9085e9; --s8:#e66767;
    --other:#55544f;
  }
}
:root[data-theme="dark"] .viz-root{
  color-scheme:dark;
  --page:#0d0d0d; --surface-1:#1a1a19;
  --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500;
  --s5:#d55181; --s6:#008300; --s7:#9085e9; --s8:#e66767;
  --other:#55544f;
}
*{box-sizing:border-box;margin:0}
body{background:var(--page);color:var(--ink);
  font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;padding:20px}
.wrap{max-width:1140px;margin:0 auto}
h1{font-size:19px;font-weight:650;margin-bottom:2px}
.sub{color:var(--muted);font-size:12px;margin-bottom:14px}
.filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:16px}
.filters .lbl{color:var(--ink-2);font-size:12px;margin-right:2px}
.seg{display:flex;border:1px solid var(--border);border-radius:8px;overflow:hidden;
  background:var(--surface-1)}
.seg button{border:0;background:none;color:var(--ink-2);padding:6px 12px;
  font:inherit;font-size:13px;cursor:pointer;border-right:1px solid var(--border)}
.seg button:last-child{border-right:0}
.seg button:hover{color:var(--ink)}
.seg button.on{color:var(--ink);font-weight:650;background:
  color-mix(in srgb, var(--s1) 12%, transparent)}
.custom{display:none;gap:6px;align-items:center}
.custom.show{display:flex}
.custom input{border:1px solid var(--border);background:var(--surface-1);
  color:var(--ink);border-radius:6px;padding:5px 8px;font:inherit;font-size:13px}
.projwrap{position:relative}
.projbtn{border:1px solid var(--border);background:var(--surface-1);color:var(--ink);
  border-radius:8px;padding:6px 12px;font:inherit;font-size:13px;cursor:pointer}
.projpanel{display:none;position:absolute;z-index:30;top:calc(100% + 6px);left:0;
  background:var(--surface-1);border:1px solid var(--border);border-radius:10px;
  box-shadow:0 8px 28px rgba(0,0,0,.18);padding:8px;min-width:240px;
  max-height:340px;overflow:auto}
.projpanel.show{display:block}
.projpanel label{display:flex;gap:8px;align-items:center;padding:5px 8px;
  border-radius:6px;cursor:pointer;font-size:13px;color:var(--ink)}
.projpanel label:hover{background:color-mix(in srgb, var(--ink) 5%, transparent)}
.projpanel .cnt{margin-left:auto;color:var(--muted);font-size:11px}
.projpanel hr{border:0;border-top:1px solid var(--border);margin:6px 0}
.kpis{display:grid;grid-template-columns:minmax(220px,1.4fr) repeat(auto-fit,minmax(130px,1fr));
  gap:10px;margin-bottom:14px}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:12px;
  padding:12px 14px}
.tile .l{color:var(--ink-2);font-size:12px;margin-bottom:4px}
.tile .v{font-size:22px;font-weight:650}
.tile.hero .v{font-size:46px;font-weight:700;line-height:1.05}
.tile .d{font-size:12px;color:var(--muted);margin-top:3px}
.tile .d .up{color:#e34948}.tile .d .down{color:#006300}
:root[data-theme="dark"] .tile .d .down,
:root:not([data-theme="light"]) .tile .d .down{}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:12px;
  padding:14px 16px;margin-bottom:14px;position:relative}
.card h2{font-size:14px;font-weight:650;margin-bottom:2px}
.card .note{color:var(--muted);font-size:12px;margin-bottom:8px}
.legend{display:flex;flex-wrap:wrap;gap:12px;margin:6px 0 2px;font-size:12px;
  color:var(--ink-2)}
.legend .it{display:flex;gap:6px;align-items:center}
.legend .sw{width:10px;height:10px;border-radius:3px}
.legend .ln{width:14px;height:2px;border-radius:1px}
svg{display:block;width:100%;height:auto}
svg text{font:11px system-ui,-apple-system,"Segoe UI",sans-serif;fill:var(--muted)}
svg .val{fill:var(--ink-2);font-variant-numeric:tabular-nums}
.grid3{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}
.hb{margin-top:6px}
.hb .row{display:grid;grid-template-columns:minmax(90px,38%) 1fr 60px;gap:8px;
  align-items:center;padding:3px 0;font-size:12.5px}
.hb .row:hover{background:color-mix(in srgb, var(--ink) 4%, transparent)}
.hb .n{color:var(--ink-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hb .track{height:14px;display:flex;align-items:center}
.hb .bar{height:14px;border-radius:0 4px 4px 0;background:var(--s1);min-width:2px}
.hb .v{color:var(--ink-2);text-align:right;font-variant-numeric:tabular-nums;font-size:12px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{color:var(--muted);font-weight:500;text-align:left;padding:6px 8px;
  border-bottom:1px solid var(--grid)}
td{padding:6px 8px;border-bottom:1px solid var(--grid);color:var(--ink-2)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td.t{color:var(--ink)}
tr:hover td{background:color-mix(in srgb, var(--ink) 3%, transparent)}
details summary{cursor:pointer;color:var(--ink-2);font-size:13px;padding:4px 0}
#tt{position:fixed;z-index:99;pointer-events:none;background:var(--surface-1);
  border:1px solid var(--border);border-radius:10px;padding:8px 11px;
  box-shadow:0 8px 28px rgba(0,0,0,.20);font-size:12px;display:none;max-width:300px}
#tt .h{color:var(--muted);margin-bottom:5px}
#tt .r{display:flex;gap:8px;align-items:center;padding:1.5px 0;color:var(--ink-2)}
#tt .k{width:12px;height:2.5px;border-radius:1px;flex:none}
#tt .r b{margin-left:auto;color:var(--ink);font-weight:650;
  font-variant-numeric:tabular-nums;padding-left:14px}
.empty{color:var(--muted);padding:26px 0;text-align:center;font-size:13px}
footer{color:var(--muted);font-size:11.5px;margin:18px 0 8px}
</style>
</head>
<body class="viz-root">
<div class="wrap">
  <h1>Claude Code burn</h1>
  <div class="sub" id="gen"></div>

  <div class="filters">
    <span class="lbl">Range</span>
    <div class="seg" id="presets"></div>
    <div class="custom" id="custom">
      <input type="date" id="d0"> <span class="lbl">to</span> <input type="date" id="d1">
    </div>
    <span class="lbl" style="margin-left:10px">Projects</span>
    <div class="projwrap">
      <button class="projbtn" id="projbtn">All projects</button>
      <div class="projpanel" id="projpanel"></div>
    </div>
  </div>

  <div class="kpis" id="kpis"></div>

  <div class="card">
    <h2>Daily burn by project</h2>
    <div class="note">Billed-equivalent tokens per day (input + 1.25&times;cache write + 0.10&times;cache read)</div>
    <div class="legend" id="dailyLegend"></div>
    <div id="dailyChart"></div>
  </div>

  <div class="card">
    <h2>Five-hour blocks</h2>
    <div class="note" id="blocksNote"></div>
    <div id="blocksChart"></div>
  </div>

  <div class="grid3" style="margin-bottom:14px">
    <div class="card" style="margin:0">
      <h2>Rolling 7-day burn</h2>
      <div class="note">Trailing 7-day total, the weekly-window pacing signal</div>
      <div id="rollChart"></div>
    </div>
    <div class="card" style="margin:0">
      <h2>Token composition</h2>
      <div class="note">Billed-weighted share per day</div>
      <div class="legend" id="compLegend"></div>
      <div id="compChart"></div>
    </div>
  </div>

  <div class="grid3" style="margin-bottom:14px">
    <div class="card" style="margin:0"><h2>By command</h2><div class="hb" id="byCmd"></div></div>
    <div class="card" style="margin:0"><h2>By model</h2><div class="hb" id="byModel"></div></div>
    <div class="card" style="margin:0"><h2>By effort</h2><div class="hb" id="byEffort"></div></div>
    <div class="card" style="margin:0"><h2>Main vs subagents</h2><div class="hb" id="byKind"></div></div>
  </div>

  <div class="card" id="rlCard" style="display:none">
    <h2>Rate-limit windows</h2>
    <div class="note">Logged used-percentage from the statusline payload</div>
    <div class="legend" id="rlLegend"></div>
    <div id="rlChart"></div>
  </div>

  <div class="card">
    <h2>Top sessions</h2>
    <div class="note">Heaviest sessions in range</div>
    <div id="sessTable"></div>
  </div>

  <div class="card">
    <details><summary>Daily totals table</summary><div id="dailyTable" style="margin-top:8px"></div></details>
  </div>

  <footer id="foot"></footer>
</div>
<div id="tt"></div>

<script>
const DATA = __DATA__;
const $ = s => document.querySelector(s);
const TZ = DATA.tz_offset || 0;
const DAY = 86400;
const SLOTS = ['--s1','--s2','--s3','--s4','--s5','--s6','--s7'];

// ---- day helpers (day strings are already in report tz) ----
const dayEpoch = d => Date.parse(d + 'T00:00:00Z') / 1000;
const epochDay = e => new Date((e + TZ*3600) * 1000).toISOString().slice(0,10);
const addDays  = (d, n) => new Date((dayEpoch(d) + n*DAY)*1000).toISOString().slice(0,10);
const fmtDay   = d => { const t = new Date(dayEpoch(d)*1000);
  return t.toLocaleDateString('en-US',{month:'short',day:'numeric',timeZone:'UTC'}); };
const fmtTime  = e => new Date((e + TZ*3600)*1000).toISOString().slice(11,16);
const fmt = n => n >= 1e9 ? (n/1e9).toFixed(2)+'B' : n >= 1e6 ? (n/1e6).toFixed(1)+'M'
  : n >= 1e3 ? (n/1e3).toFixed(n<1e4?1:0)+'k' : Math.round(n).toString();
const fmtFull = n => Math.round(n).toLocaleString('en-US');

// column indexes: daily row = [day,p,cmd,model,effort,kind,be,in,cc,cc1h,cr,out,n]
const D = {day:0,p:1,cmd:2,model:3,effort:4,kind:5,be:6,inp:7,cc:8,cc1h:9,cr:10,out:11,n:12};

const allDays = [...new Set(DATA.daily.map(r => r[D.day]))].sort();
// both bounds fall back: an empty tree is a supported flow (a warned-about
// empty root, a first run before any transcript exists), and without the
// MINDAY fallback it rendered "data undefined -> <today>" and left state.d0
// undefined on the All preset
const MAXDAY = allDays[allDays.length-1] || epochDay(Date.now()/1000);
const MINDAY = allDays[0] || MAXDAY;

// ---- state ----
const state = { preset: '__PRESET__', d0: null, d1: null, projs: null };  // projs null = all
rangeFromPreset();

function rangeFromPreset(){
  state.d1 = MAXDAY;
  if (state.preset === 'all') state.d0 = MINDAY;
  else state.d0 = addDays(MAXDAY, -(+state.preset - 1));
  if (state.d0 < MINDAY) state.d0 = MINDAY;
}

// ---- filters UI ----
const PRESETS = [['7','7d'],['14','14d'],['30','30d'],['90','90d'],['all','All'],['custom','Custom']];
function buildFilters(){
  $('#presets').innerHTML = PRESETS.map(([k,l]) =>
    `<button data-k="${k}" class="${state.preset===k?'on':''}">${l}</button>`).join('');
  $('#presets').onclick = e => {
    const b = e.target.closest('button'); if (!b) return;
    state.preset = b.dataset.k;
    if (state.preset !== 'custom') rangeFromPreset();
    $('#custom').classList.toggle('show', state.preset === 'custom');
    $('#d0').value = state.d0; $('#d1').value = state.d1;
    buildFilters(); render();
  };
  $('#custom').classList.toggle('show', state.preset === 'custom');
  $('#d0').value = state.d0; $('#d1').value = state.d1;
  $('#d0').onchange = $('#d1').onchange = () => {
    if ($('#d0').value) state.d0 = $('#d0').value;
    if ($('#d1').value) state.d1 = $('#d1').value;
    if (state.d0 > state.d1) [state.d0, state.d1] = [state.d1, state.d0];
    render();
  };

  const panel = $('#projpanel');
  const rows = DATA.projects.map((p,i) =>
    `<label><input type="checkbox" data-i="${i}" ${!state.projs||state.projs.has(i)?'checked':''}>
     <span></span><span class="cnt">${fmt(p.be)}</span></label>`).join('');
  panel.innerHTML = `<label><input type="checkbox" id="projall" ${!state.projs?'checked':''}>
    <b>All projects</b></label><hr>` + rows;
  // project ids are data -> textContent, never innerHTML
  [...panel.querySelectorAll('label')].slice(1).forEach((lab,i) =>
    lab.children[1].textContent = DATA.projects[i].id);
  $('#projbtn').onclick = e => { e.stopPropagation(); panel.classList.toggle('show'); };
  document.addEventListener('click', e => {
    if (!panel.contains(e.target)) panel.classList.remove('show'); });
  $('#projall').onchange = e => {
    state.projs = e.target.checked ? null : new Set(); buildFilters(); render(); };
  panel.querySelectorAll('input[data-i]').forEach(cb => cb.onchange = () => {
    const sel = new Set([...panel.querySelectorAll('input[data-i]')]
      .filter(c => c.checked).map(c => +c.dataset.i));
    state.projs = sel.size === DATA.projects.length ? null : sel;
    buildFilters(); render();
  });
  $('#projbtn').textContent = !state.projs ? 'All projects'
    : state.projs.size === 1 ? DATA.projects[[...state.projs][0]].id
    : state.projs.size + ' projects';
}

// ---- tooltip ----
const tt = $('#tt');
function ttShow(x, y, head, rows){
  tt.innerHTML = '';
  const h = document.createElement('div'); h.className = 'h';
  h.textContent = head; tt.appendChild(h);
  for (const [color, label, val] of rows){
    const r = document.createElement('div'); r.className = 'r';
    const k = document.createElement('span'); k.className = 'k';
    k.style.background = color || 'transparent';
    const l = document.createElement('span'); l.textContent = label;
    const b = document.createElement('b'); b.textContent = val;
    r.append(k, l, b); tt.appendChild(r);
  }
  tt.style.display = 'block';
  const w = tt.offsetWidth, hh = tt.offsetHeight;
  tt.style.left = Math.min(x + 14, innerWidth - w - 10) + 'px';
  tt.style.top  = Math.min(y + 14, innerHeight - hh - 10) + 'px';
}
const ttHide = () => tt.style.display = 'none';

// ---- svg helpers ----
function niceMax(v){
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1,1.5,2,2.5,3,4,5,6,8,10]) if (m*p >= v) return m*p;
  return 10*p;
}
function barPath(x, y, w, h, r){
  r = Math.min(r, w/2, h);
  if (h <= 0.5 || w <= 0) return '';
  return `M${x},${y+h} L${x},${y+r} Q${x},${y} ${x+r},${y} L${x+w-r},${y}` +
         `Q${x+w},${y} ${x+w},${y+r} L${x+w},${y+h} Z`;
}
function axisTicks(max, plotH, padT, padL, W){
  let out = '';
  for (let i = 0; i <= 4; i++){
    const v = max*i/4, y = padT + plotH - plotH*i/4;
    out += `<line x1="${padL}" x2="${W}" y1="${y}" y2="${y}" stroke="var(--grid)" stroke-width="1"
      vector-effect="non-scaling-stroke"></line>
      <text x="${padL-6}" y="${Math.max(y+3, 10)}" text-anchor="end">${fmt(v)}</text>`;
  }
  return out;
}
function dayLabels(days, xOf, H){
  const step = Math.max(1, Math.ceil(days.length/9));
  let out = '';
  for (let i = 0; i < days.length; i += step)
    out += `<text x="${xOf(i)}" y="${H-6}" text-anchor="middle">${fmtDay(days[i])}</text>`;
  return out;
}

// ---- stacked band chart (daily + composition) ----
function stackedChart(elId, days, series, opts){
  const W = (opts && opts.W) || 960, H = 250, padT = 12, padB = 22, padL = 46;
  const plotH = H - padT - padB, n = days.length;
  const el = $(elId);
  if (!n || !series.some(s => s.values.some(v => v > 0))){
    el.innerHTML = '<div class="empty">No data in range</div>'; return;
  }
  const totals = days.map((_,i) => series.reduce((a,s) => a + s.values[i], 0));
  const max = niceMax(Math.max(...totals));
  const band = (W - padL) / n;
  const bw = Math.min(24, Math.max(2, band - 2));
  const xOf = i => padL + band*i + band/2;
  let marks = '';
  for (let i = 0; i < n; i++){
    let y = padT + plotH;
    const x = xOf(i) - bw/2;
    let topSeg = true;
    const segs = [];
    for (const s of series){
      const h = plotH * s.values[i] / max;
      if (h < 0.6) continue;
      segs.push([s, h]);
    }
    for (let j = segs.length - 1; j >= 0; j--){}  // draw bottom-up below
    let acc = 0;
    const drawn = [];
    for (const [s, h] of segs){ drawn.push([s, acc, h]); acc += h; }
    for (let j = 0; j < drawn.length; j++){
      const [s, off, h] = drawn[j];
      const isTop = j === drawn.length - 1;
      const gap = j > 0 ? 2 : 0;
      const yTop = padT + plotH - off - h;
      const hh = h - gap;
      if (hh < 0.6) continue;
      marks += isTop
        ? `<path d="${barPath(x, yTop, bw, hh, 4)}" fill="var(${s.color})"></path>`
        : `<rect x="${x}" y="${yTop}" width="${bw}" height="${hh}" fill="var(${s.color})"></rect>`;
    }
  }
  // direct label on the peak day only
  const pk = totals.indexOf(Math.max(...totals));
  const pkY = padT + plotH - plotH*totals[pk]/max;
  const peakLabel = totals[pk] > 0 ?
    `<text class="val" x="${xOf(pk)}" y="${Math.max(10, pkY-5)}" text-anchor="middle">${fmt(totals[pk])}</text>` : '';
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="aspect-ratio:${W}/${H}">
    ${axisTicks(max, plotH, padT, padL, W)}
    <line x1="${padL}" x2="${W}" y1="${padT+plotH}" y2="${padT+plotH}" stroke="var(--axis)" stroke-width="1"></line>
    ${marks}${peakLabel}
    <line id="xh" x1="0" x2="0" y1="${padT}" y2="${padT+plotH}" stroke="var(--axis)"
      stroke-width="1" opacity="0"></line>
    ${dayLabels(days, xOf, H)}
    <rect x="0" y="0" width="${W}" height="${H}" fill="transparent"></rect>
  </svg>`;
  const svg = el.firstElementChild, xh = svg.querySelector('#xh');
  svg.addEventListener('pointermove', e => {
    const r = svg.getBoundingClientRect();
    const i = Math.max(0, Math.min(n-1,
      Math.floor(((e.clientX - r.left)/r.width*W - padL)/band)));
    xh.setAttribute('x1', xOf(i)); xh.setAttribute('x2', xOf(i));
    xh.setAttribute('opacity', 1);
    const rows = series.map(s => [cssVar(s.color), s.name, fmtFull(s.values[i])])
      .filter((_,j) => series[j].values[i] > 0).reverse();
    rows.push(['transparent','total', fmtFull(totals[i])]);
    ttShow(e.clientX, e.clientY, fmtDay(days[i]) + ' · ' + days[i], rows);
  });
  svg.addEventListener('pointerleave', () => { xh.setAttribute('opacity',0); ttHide(); });
}
const cssVar = v => getComputedStyle(document.body).getPropertyValue(v).trim();

// ---- line chart (rolling 7d, rate-limit) ----
function lineChart(elId, days, seriesArr, unit){
  const W = 520, H = 200, padT = 12, padB = 20, padL = 46;
  const plotH = H - padT - padB, n = days.length;
  const el = $(elId);
  if (n < 2 || !seriesArr.some(s => s.values.some(v => v != null && v > 0))){
    el.innerHTML = '<div class="empty">Not enough data in range</div>'; return;
  }
  const max = unit === '%' ? 100 :
    niceMax(Math.max(...seriesArr.flatMap(s => s.values.filter(v => v != null))));
  const xOf = i => n === 1 ? (W+padL)/2 : padL + i*(W-padL-8)/(n-1);
  const yOf = v => padT + plotH - plotH*v/max;
  let body = '';
  for (const s of seriesArr){
    let dstr = '', started = false;
    s.values.forEach((v,i) => {
      if (v == null){ started = false; return; }
      dstr += (started ? 'L' : 'M') + xOf(i).toFixed(1) + ',' + yOf(v).toFixed(1);
      started = true;
    });
    body += `<path d="${dstr}" fill="none" stroke="var(${s.color})" stroke-width="2"
      stroke-linejoin="round" stroke-linecap="round"></path>`;
    for (let i = s.values.length-1; i >= 0; i--) if (s.values[i] != null){
      body += `<circle cx="${xOf(i)}" cy="${yOf(s.values[i])}" r="4" fill="var(${s.color})"
        stroke="var(--surface-1)" stroke-width="2"></circle>
        <text class="val" x="${Math.min(xOf(i), W-34)}" y="${Math.max(10, yOf(s.values[i])-8)}"
        text-anchor="middle">${unit==='%' ? s.values[i].toFixed(0)+'%' : fmt(s.values[i])}</text>`;
      break;
    }
  }
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" style="aspect-ratio:${W}/${H}">
    ${axisTicks(max, plotH, padT, padL, W)}
    <line x1="${padL}" x2="${W}" y1="${padT+plotH}" y2="${padT+plotH}" stroke="var(--axis)"></line>
    ${body}
    <line id="xh" x1="0" x2="0" y1="${padT}" y2="${padT+plotH}" stroke="var(--axis)" opacity="0"></line>
    ${dayLabels(days, xOf, H)}
    <rect x="0" y="0" width="${W}" height="${H}" fill="transparent"></rect></svg>`;
  const svg = el.firstElementChild, xh = svg.querySelector('#xh');
  svg.addEventListener('pointermove', e => {
    const r = svg.getBoundingClientRect();
    const i = Math.max(0, Math.min(n-1,
      Math.round(((e.clientX-r.left)/r.width*W - padL)/((W-padL-8)/(n-1)))));
    xh.setAttribute('x1', xOf(i)); xh.setAttribute('x2', xOf(i)); xh.setAttribute('opacity',1);
    ttShow(e.clientX, e.clientY, fmtDay(days[i]),
      seriesArr.filter(s => s.values[i] != null)
        .map(s => [cssVar(s.color), s.name,
          unit==='%' ? s.values[i].toFixed(1)+'%' : fmtFull(s.values[i])]));
  });
  svg.addEventListener('pointerleave', () => { xh.setAttribute('opacity',0); ttHide(); });
}

// ---- blocks chart ----
function blocksChart(blocks, d0, d1){
  const W = 960, H = 210, padT = 12, padB = 22, padL = 46;
  const plotH = H - padT - padB;
  const el = $('#blocksChart');
  if (!blocks.length){ el.innerHTML = '<div class="empty">No blocks in range</div>'; return; }
  const t0 = dayEpoch(d0) - TZ*3600, t1 = dayEpoch(d1) + DAY - TZ*3600;
  const span = t1 - t0;
  const max = niceMax(Math.max(...blocks.map(b => b[3])));
  const xOf = t => padL + (t - t0)/span * (W - padL);
  let marks = '', labels = '';
  blocks.forEach((b, bi) => {
    const x = xOf(b[0]), w = Math.max(2, 5*3600/span*(W-padL) - 1);
    const h = plotH * b[3]/max, y = padT + plotH - h;
    marks += `<path d="${barPath(x, y, w, h, 3)}" fill="var(--s1)" data-b="${bi}"></path>`;
  });
  const nd = Math.max(1, Math.round(span/DAY));
  const step = Math.max(1, Math.ceil(nd/9));
  for (let i = 0; i < nd; i += step){
    const d = addDays(d0, i);
    labels += `<text x="${xOf(dayEpoch(d) - TZ*3600 + DAY/2)}" y="${H-6}" text-anchor="middle">${fmtDay(d)}</text>`;
  }
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="aspect-ratio:${W}/${H}">
    ${axisTicks(max, plotH, padT, padL, W)}
    <line x1="${padL}" x2="${W}" y1="${padT+plotH}" y2="${padT+plotH}" stroke="var(--axis)"></line>
    ${marks}${labels}</svg>`;
  el.firstElementChild.addEventListener('pointermove', e => {
    const p = e.target.closest('[data-b]');
    if (!p){ ttHide(); return; }
    const b = blocks[+p.dataset.b];
    const rows = [['transparent','burned', fmtFull(b[3])],
                  ['transparent','output', fmtFull(b[4])],
                  ['transparent','rate', fmt(b[3]/5) + '/h'],
                  ['transparent','active', fmtTime(b[1]) + '–' + fmtTime(b[2])]];
    for (const [pi, v] of Object.entries(b[6]))
      rows.push([cssVar(colorOf(+pi)), DATA.projects[+pi].id, fmtFull(v)]);
    ttShow(e.clientX, e.clientY,
      fmtDay(epochDay(b[0])) + ' · ' + fmtTime(b[0]) + ' + 5h', rows);
  });
  el.firstElementChild.addEventListener('pointerleave', ttHide);
}

// project color: fixed by all-time rank, never repainted by filters
let topSlots = new Map();
function colorOf(pi){ return topSlots.has(pi) ? topSlots.get(pi) : '--other'; }

// ---- horizontal bar lists ----
function hbars(elId, items){
  const el = $(elId);
  if (!items.length){ el.innerHTML = '<div class="empty">No data</div>'; return; }
  const max = Math.max(...items.map(i => i.v));
  el.innerHTML = items.map(it => `<div class="row" data-t="${encodeURIComponent(it.tip||it.label)}">
    <span class="n"></span>
    <span class="track"><span class="bar" style="width:${(100*it.v/max).toFixed(1)}%"></span></span>
    <span class="v">${fmt(it.v)}</span></div>`).join('');
  [...el.children].forEach((row,i) => row.querySelector('.n').textContent = items[i].label);
  el.onpointermove = e => {
    const row = e.target.closest('.row'); if (!row) return;
    const t = decodeURIComponent(row.dataset.t); if (t) ttShow(e.clientX, e.clientY, t, []);
  };
  el.onpointerleave = ttHide;
}

// ---- render ----
function inRange(d){ return d >= state.d0 && d <= state.d1; }
function projOk(p){ return !state.projs || state.projs.has(p); }

function render(){
  const rows = DATA.daily.filter(r => inRange(r[D.day]) && projOk(r[D.p]));
  const nDays = Math.round((dayEpoch(state.d1) - dayEpoch(state.d0))/DAY) + 1;
  const days = Array.from({length: nDays}, (_,i) => addDays(state.d0, i));
  const dIdx = new Map(days.map((d,i) => [d,i]));

  // per-project stacked series (top 7 all-time in-filter projects get slots)
  const perProj = new Map();
  const perDayTotal = new Array(nDays).fill(0);
  let total = 0, totalOut = 0, totalMsgs = 0;
  for (const r of rows){
    const i = dIdx.get(r[D.day]);
    if (i == null) continue;
    if (!perProj.has(r[D.p])) perProj.set(r[D.p], new Array(nDays).fill(0));
    perProj.get(r[D.p])[i] += r[D.be];
    perDayTotal[i] += r[D.be];
    total += r[D.be]; totalOut += r[D.out]; totalMsgs += r[D.n];
  }
  // slots by ALL-TIME project rank so filtering never repaints survivors
  topSlots = new Map();
  DATA.projects.forEach((p,i) => { if (i < SLOTS.length) topSlots.set(i, SLOTS[i]); });
  const present = [...perProj.keys()].sort((a,b) => a-b);
  const series = [];
  const other = new Array(nDays).fill(0);
  for (const pi of present){
    if (topSlots.has(pi))
      series.push({name: DATA.projects[pi].id, color: topSlots.get(pi),
                   values: perProj.get(pi), be: DATA.projects[pi].be});
    else perProj.get(pi).forEach((v,i) => other[i] += v);
  }
  series.sort((a,b) => b.be - a.be);
  if (other.some(v => v > 0)) series.push({name:'other', color:'--other', values: other});
  series.reverse();  // draw biggest at the bottom of the stack
  stackedChart('#dailyChart', days, series, {});
  $('#dailyLegend').innerHTML = [...series].reverse().map(s =>
    `<span class="it"><span class="sw" style="background:var(${s.color})"></span><span></span></span>`).join('');
  [...$('#dailyLegend').children].forEach((it,i) =>
    it.children[1].textContent = [...series].reverse()[i].name);

  // KPIs (+ comparison vs the prior period of equal length)
  const prev0 = addDays(state.d0, -nDays), prev1 = addDays(state.d0, -1);
  let prevTotal = 0;
  for (const r of DATA.daily)
    if (r[D.day] >= prev0 && r[D.day] <= prev1 && projOk(r[D.p])) prevTotal += r[D.be];
  const active = perDayTotal.filter(v => v > 0).length;
  const pk = perDayTotal.indexOf(Math.max(...perDayTotal));
  const blocks = DATA.blocks.filter(b => {
    const d = epochDay(b[0]);
    return d >= state.d0 && d <= state.d1;
  });
  const bVals = blocks.map(b => b[3]).sort((a,b) => a-b);
  const med = bVals.length ? bVals[bVals.length >> 1] : 0;
  const sess = DATA.sessions.filter(s =>
    inRange(epochDay(s[3])) && (!state.projs ||
      [...state.projs].some(i => DATA.projects[i].id === s[1])));
  const peakCtx = sess.length ? Math.max(...sess.map(s => s[8])) : 0;
  const delta = prevTotal > 0 ? (total - prevTotal)/prevTotal*100 : null;
  $('#kpis').innerHTML = `
    <div class="tile hero"><div class="l">Billed-equivalent tokens</div>
      <div class="v">${fmt(total)}</div>
      <div class="d">${delta == null ? '' :
        `<span class="${delta >= 0 ? 'up' : 'down'}">${delta >= 0 ? '+' : ''}${delta.toFixed(0)}%</span> vs prior ${nDays}d`}</div></div>
    <div class="tile"><div class="l">Daily average</div><div class="v">${fmt(active ? total/active : 0)}</div>
      <div class="d">${active} active day${active===1?'':'s'}</div></div>
    <div class="tile"><div class="l">Peak day</div><div class="v">${fmt(perDayTotal[pk]||0)}</div>
      <div class="d">${perDayTotal[pk] ? fmtDay(days[pk]) : '—'}</div></div>
    <div class="tile"><div class="l">Output tokens</div><div class="v">${fmt(totalOut)}</div>
      <div class="d">${fmtFull(totalMsgs)} messages</div></div>
    <div class="tile"><div class="l">5h blocks</div><div class="v">${blocks.length}</div>
      <div class="d">median ${fmt(med)}</div></div>
    <div class="tile"><div class="l">Peak context</div><div class="v">${fmt(peakCtx)}</div>
      <div class="d">${sess.length} sessions</div></div>`;

  // blocks
  $('#blocksNote').textContent = 'Account-wide (all projects), regardless of the project filter. '
    + 'A block opens on the hour of first activity and lasts five hours; height is billed-equivalent burn.';
  blocksChart(blocks, state.d0, state.d1);

  // rolling 7d (from project-filtered rows, all days incl. before range start)
  const byDayAll = new Map();
  for (const r of DATA.daily) if (projOk(r[D.p]))
    byDayAll.set(r[D.day], (byDayAll.get(r[D.day])||0) + r[D.be]);
  const roll = days.map(d => {
    let s = 0;
    for (let i = 0; i < 7; i++) s += byDayAll.get(addDays(d, -i)) || 0;
    return s;
  });
  lineChart('#rollChart', days, [{name:'trailing 7d', color:'--s1', values: roll}]);

  // composition
  const comp = [
    {name:'fresh input', color:'--s1', values:new Array(nDays).fill(0)},
    {name:'cache write 5m ×1.25', color:'--s2', values:new Array(nDays).fill(0)},
    {name:'cache write 1h ×1.25', color:'--s3', values:new Array(nDays).fill(0)},
    {name:'cache read ×0.10', color:'--s4', values:new Array(nDays).fill(0)}];
  for (const r of rows){
    const i = dIdx.get(r[D.day]); if (i == null) continue;
    comp[0].values[i] += r[D.inp];
    comp[1].values[i] += 1.25*(r[D.cc]-r[D.cc1h]);
    comp[2].values[i] += 1.25*r[D.cc1h];
    comp[3].values[i] += 0.10*r[D.cr];
  }
  stackedChart('#compChart', days, [...comp].reverse(), {W: 520});
  $('#compLegend').innerHTML = comp.map(s =>
    `<span class="it"><span class="sw" style="background:var(${s.color})"></span>${s.name}</span>`).join('');

  // breakdowns
  const agg = (key, labelFn) => {
    const m = new Map();
    for (const r of rows) m.set(r[key], (m.get(r[key])||0) + r[D.be]);
    return [...m.entries()].map(([k,v]) => ({label: labelFn(k), v}))
      .sort((a,b) => b.v - a.v);
  };
  let cmds = agg(D.cmd, c => c || '(conversation)');
  if (cmds.length > 10){
    const rest = cmds.slice(10).reduce((a,c) => a + c.v, 0);
    cmds = cmds.slice(0,10); cmds.push({label:'(other commands)', v: rest});
    cmds.sort((a,b) => b.v - a.v);
  }
  hbars('#byCmd', cmds);
  hbars('#byModel', agg(D.model, m => m.replace(/^claude-/,'')));
  const EORD = ['max','xhigh','high','medium','low','(unspecified)'];
  hbars('#byEffort', agg(D.effort, e => e === '-' ? '(unspecified)' : e).sort((a,b) =>
    EORD.indexOf(a.label) - EORD.indexOf(b.label)));
  hbars('#byKind', agg(D.kind, k => k === 'm' ? 'main session' : 'subagents'));

  // rate-limit card
  const rl = DATA.rl.filter(s => { const d = epochDay(s[0]); return inRange(d); });
  if (rl.length > 1){
    $('#rlCard').style.display = '';
    const rdays = [...new Set(rl.map(s => epochDay(s[0])))].sort();
    const lastOf = (day, idx) => {
      const v = rl.filter(s => epochDay(s[0]) === day && s[idx] != null).pop();
      return v ? v[idx] : null;
    };
    lineChart('#rlChart', rdays, [
      {name:'5-hour window', color:'--s1', values: rdays.map(d => lastOf(d,1))},
      {name:'7-day window',  color:'--s2', values: rdays.map(d => lastOf(d,2))}], '%');
    $('#rlLegend').innerHTML =
      `<span class="it"><span class="ln" style="background:var(--s1)"></span>5-hour window</span>
       <span class="it"><span class="ln" style="background:var(--s2)"></span>7-day window</span>`;
  } else $('#rlCard').style.display = 'none';

  // sessions table
  const top = sess.slice(0, 15);
  const st = document.createElement('table');
  st.innerHTML = `<thead><tr><th>Session</th><th>Project</th><th>Day</th>
    <th class="num">Billed</th><th class="num">Output</th><th class="num">Msgs</th>
    <th class="num">Peak ctx</th><th class="num">Compact</th><th class="num">Agents</th></tr></thead>`;
  const tb = document.createElement('tbody');
  for (const s of top){
    const tr = document.createElement('tr');
    const cells = [[s[2] || s[0], 't'], [s[1]], [fmtDay(epochDay(s[3]))],
      [fmtFull(s[5]),'num'], [fmt(s[6]),'num'], [fmtFull(s[7]),'num'],
      [fmt(s[8]),'num'], [s[9] || '','num'], [s[10] || '','num']];
    for (const [txt, cls] of cells){
      const td = document.createElement('td');
      if (cls) td.className = cls;
      td.textContent = txt; tr.appendChild(td);
    }
    tb.appendChild(tr);
  }
  st.appendChild(tb);
  $('#sessTable').innerHTML = '';
  if (top.length) $('#sessTable').appendChild(st);
  else $('#sessTable').innerHTML = '<div class="empty">No sessions in range</div>';

  // daily totals table (the always-reachable table view)
  const dt = ['<table><thead><tr><th>Day</th><th class="num">Billed</th><th class="num">Fresh in</th>' +
    '<th class="num">Cache write</th><th class="num">Cache read</th><th class="num">Output</th>' +
    '<th class="num">Msgs</th></tr></thead><tbody>'];
  const dm = new Map();
  for (const r of rows){
    const a = dm.get(r[D.day]) || [0,0,0,0,0,0];
    a[0]+=r[D.be]; a[1]+=r[D.inp]; a[2]+=r[D.cc]; a[3]+=r[D.cr]; a[4]+=r[D.out]; a[5]+=r[D.n];
    dm.set(r[D.day], a);
  }
  for (const d of [...dm.keys()].sort().reverse()){
    const a = dm.get(d);
    dt.push(`<tr><td class="t">${d}</td><td class="num">${fmtFull(a[0])}</td>` +
      `<td class="num">${fmtFull(a[1])}</td><td class="num">${fmtFull(a[2])}</td>` +
      `<td class="num">${fmtFull(a[3])}</td><td class="num">${fmtFull(a[4])}</td>` +
      `<td class="num">${fmtFull(a[5])}</td></tr>`);
  }
  dt.push('</tbody></table>');
  $('#dailyTable').innerHTML = dt.join('');
}

// ---- boot ----
$('#gen').textContent = `Generated ${DATA.generated} · ` +
  `${DATA.archive_used ? 'live transcripts + ' + (DATA.archive_label || 'archive')
                       : 'live transcripts only'} · ` +
  `data ${MINDAY} → ${MAXDAY}`;
$('#foot').textContent = 'Billed-equivalent weights every token to a common unit; it is not a bill. ' +
  (DATA.rl_installed ? '' :
   'Rate-limit logger not installed: cap percentages (5h/7d windows) are not ' +
   'being recorded; they need the opt-in extras/usage_logger.sh.');
buildFilters();
render();
</script>
</body>
</html>
'''


if __name__ == "__main__":
    main()
