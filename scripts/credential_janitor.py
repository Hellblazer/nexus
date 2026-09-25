#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Credential janitor: a release-battery leg that fails on any
credential-shaped file left under a harness-owned root (RDR-219
"The janitor", Phase 3 Step 2b, nexus-wauo1.24).

TOKEN RULE for this file itself, and for anything it prints: file NAMES
only, never contents. A finding is reported as a bare path; the matched
token or JSON body is never echoed anywhere, including in test output.

DESIGN. Two independent sweeps, deliberately not conflated:

1. FILENAME sweep for the two known credential-artifact names
   (``CREDENTIAL_FILENAMES``) across every root, repo tree included. Cheap
   (name comparison only), so it runs everywhere, and it is the only sweep
   the repo tree gets.

2. CONTENT sweep for the automation-token pattern (``TOKEN_RE``),
   restricted to TEXT files (binary files are skipped by a NUL-byte sniff
   / ``grep -I``, the same heuristic git's own diff-binary detection uses)
   and restricted to the SMALL, self-contained "harness-owned" roots only:
   ``$TMPDIR``'s ``*.artifacts``/``rdr208-mvv.*`` stage folders, and
   ``~/nexus-sandbox``.

   The repo tree is DELIBERATELY EXCLUDED from the content sweep. Two
   tracked fixtures carry a synthetic ``sk-ant-oat...`` string on purpose
   (``tests/test_claude_credentials.py``, ``tests/test_run_ladder_credentials.py``)
   and must never fail this check; restricting content-scanning to
   harness-owned roots makes that true by construction, not by a path
   exclusion list that could rot.

   The binary exclusion is what keeps the claude CLI binary itself
   (``~/.local/share/claude/versions/<v>``, which embeds the token-shaped
   regex as bytes) from ever tripping this check, wherever a harness
   happens to stage or mount a copy of it -- the exclusion is on FILE
   SHAPE (binary vs. text), never on a path, so it holds regardless of
   where that binary ends up.

$TMPDIR ITSELF, and the agent SCRATCHPAD roots (``/private/tmp/claude-*``
on macOS), get the FILENAME sweep only, bounded to ``--max-depth``
(default 4) -- never content-scanned. A real ``$TMPDIR`` on this
project's own dev boxes can carry tens of thousands of unrelated
top-level entries (nexus-wauo1.24's own scope-addition comment measured
an unbounded recursive grep there running unfinished for several
minutes). The SAME bound applies to the scratchpad roots for the same
reason, discovered while implementing this bead rather than named in its
text: a live Claude Code session's own scratchpad directory is not small
either -- measured on this box, one session alone held 65 GB, and an
unbounded content grep there did not finish inside several minutes. The
distinction that matters is not "agent-owned vs. harness-owned" but
"live and indefinitely growing" vs. "small, bounded harness OUTPUT" --
only the latter (``content_roots``: the ``*.artifacts``/``rdr208-mvv.*``
folders and ``~/nexus-sandbox``) gets the unbounded content sweep.

NON-VACUITY. ``repo_root`` and ``tmpdir`` are REQUIRED roots: if either
does not exist, the scan is a FAILURE (``ScanResult.error`` set), never a
silent clean pass. The dynamic roots (scratchpad dirs, artifact folders,
``~/nexus-sandbox``) are inherently optional -- a box with no sessions
open, or no ``~/nexus-sandbox`` yet, is not an error, and zero matches
for one of those globs is not either.

THE STATUS WARNING. The bead's own text: "the leg also runs
``claude_credentials.py status`` and prints a warning, without failing, at
30 days or fewer to expiry." ``status``'s own module contract
(``tests/e2e/lib/claude_credentials.py``) guarantees it never prints
credential material on any path, so its stdout/stderr are forwarded
verbatim; its exit code is captured only for logging and NEVER affects
this leg's own pass/fail, which depends solely on the scan.

PROCESS AND TMUX SWEEPS (nexus-wauo1.24 continuation, 2026-09-25). Every
sweep above finds credential material on DISK. T2
``nexus_rdr/219-leftover-tmux-servers-2026-09-25`` records a gap none of
them close: five orphaned ``tests/e2e/run.sh`` tmux servers (sockets
``nexus-e2e-<pid>``), each started through ``claude_credentials.py run
--``, held the automation token in the SERVER's own process environment
for about seven hours after their harness runs ended -- no file was ever
involved, so no file sweep could have seen them. Two more sweeps close
that gap, both reporting identifying metadata only (pid, elapsed time,
command name; socket name) and NEVER the matched value or the full
command line, which could carry anything:

3. A PROCESS sweep (``scan_processes``) lists this user's processes and
   flags any whose environment names a ``PROTECTED_ENV_VAR_NAMES`` entry,
   older than ``--process-min-age`` (default 2h -- the release battery may
   be running a harness concurrently) and not the janitor's own process or
   an ancestor of it. macOS reads ``ps -Eww`` (T3
   ``analysis-deep-rdr219-devfd-mcp-config-2026-09-25``: this shows the
   environment of non-Apple-platform binaries such as tmux, python and
   claude, but not of ``/bin/bash`` and other Apple platform binaries --
   an accepted, documented gap, not a defect this sweep can close). Linux
   reads ``/proc/<pid>/environ`` directly (NUL-separated, readable by the
   same uid). Neither path ever retains the matched environment text past
   the presence check.

4. A TMUX sweep (``discover_tmux_sockets``) lists tmux socket files under
   ``/private/tmp/tmux-<uid>`` and ``/tmp/tmux-<uid>`` matching a known
   harness socket-name pattern (``TMUX_SOCKET_PATTERNS`` -- collected from
   every hard-coded ``tmux -L`` invocation and socket-name default under
   ``tests/e2e`` and ``tests/cc-validation``), confirms a server is still
   alive on that name (``tmux -L <name> ls``), and applies the same age
   threshold to the socket file's mtime. A stale socket file with no live
   server behind it is not a finding -- nothing to kill, nothing holding
   the token. The remedy named in the report, ``tmux -L <name>
   kill-server``, is exactly what T2 ``nexus_rdr/219-leftover-tmux-
   servers-2026-09-25`` records the orchestrator running by hand on the
   five leftover servers this sweep exists to catch automatically.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: The two known on-disk shapes a harness's credential copy has carried
#: (RDR-219 Existing Infrastructure Audit / Phase 2 Step 2). One constant --
#: nexus-wauo1.35 (pending) may add a harness-only environment VARIABLE
#: name to a sibling constant; it does not touch this one.
CREDENTIAL_FILENAMES = frozenset({".credentials.json", ".claude-credentials.json"})

#: The automation/interactive-login token shape, as named in the bead's own
#: TOKEN RULE (`grep -rlE 'sk-ant-o(a|r)t' <dir>`) and in
#: ``tests/e2e/lib/claude_credentials.py``'s docstring
#: (`` `claude setup-token` output (`sk-ant-oat01-...`)``). One constant --
#: every content-scan call in this module reads it from here.
TOKEN_RE = re.compile(r"sk-ant-o[ar]t")

#: Bytes sniffed from a file's head to decide binary vs. text (the same
#: window ``git``'s own binary-diff heuristic reads).
_BINARY_SNIFF_BYTES = 8192

#: Directory names pruned from every filesystem walk in this module: never
#: worth walking into, and `.git` alone can be enormous.
_PRUNED_DIR_NAMES = frozenset({".git"})

_CRED_TOOL_DEFAULT = REPO_ROOT / "tests" / "e2e" / "lib" / "claude_credentials.py"

#: The one constant naming every environment-variable shape the automation
#: token has been carried under: ``CLAUDE_CODE_OAUTH_TOKEN`` is
#: ``tests/e2e/lib/claude_credentials.py``'s own ``TOKEN_ENV_VAR``;
#: ``NX_HARNESS_CLAUDE_OAUTH_TOKEN`` is the /dev/fd-mcp-config mapping name
#: T3 ``analysis-deep-rdr219-devfd-mcp-config-2026-09-25`` names (the
#: harness-side alias an ``--mcp-config`` env block carries so the token
#: never lands under the protected name in a Bash-tool child's inherited
#: environment). A process/tmux-server finding names which of these two was
#: PRESENT, never its value.
PROTECTED_ENV_VAR_NAMES = frozenset({"CLAUDE_CODE_OAUTH_TOKEN", "NX_HARNESS_CLAUDE_OAUTH_TOKEN"})

#: Default process age floor for the process/tmux sweeps: the release
#: battery may itself be running a harness leg concurrently, so a process
#: or tmux server younger than this is an in-flight run, not a leftover.
DEFAULT_PROCESS_MIN_AGE_SECONDS = 2 * 3600

#: Every harness tmux socket-name shape hard-coded under `tests/e2e` and
#: `tests/cc-validation` (collected 2026-09-25): `run.sh`'s
#: `NX_TMUX_SOCKET="nexus-e2e-$$"`, `cc-validation/runner.sh`'s
#: `cc-val-sock`, the connection-race ladder's `--sock` default
#: `veh77-ladder` (nexus-veh77), `release-sandbox.sh`'s
#: `release-sandbox-sock`, `hook-surface-shakeout`'s in-container
#: `shakeout-sock`, and `rdr208-mvv`'s in-container `rdr208`. Glob patterns,
#: matched against socket file names under a tmux socket directory.
TMUX_SOCKET_PATTERNS: "tuple[str, ...]" = (
    "nexus-e2e-*",
    "cc-val-sock",
    "veh77-ladder*",
    "release-sandbox-sock",
    "shakeout-sock",
    "rdr208",
)


@dataclass
class ProcessFinding:
    """A live process whose environment names a protected token variable.
    Deliberately carries NO value and NO full command line -- only what the
    report is allowed to print."""

    pid: int
    etime: str
    comm: str


@dataclass
class TmuxFinding:
    """A live tmux server on a known harness socket name, old enough to be
    a leftover rather than an in-flight run."""

    socket_name: str
    socket_path: pathlib.Path


@dataclass
class ScanResult:
    findings: list[pathlib.Path] = field(default_factory=list)
    process_findings: "list[ProcessFinding]" = field(default_factory=list)
    tmux_findings: "list[TmuxFinding]" = field(default_factory=list)
    #: Set only when a REQUIRED root does not exist -- distinct from an
    #: empty `findings` list, which is a genuine clean pass.
    error: "str | None" = None


def _is_binary(path: pathlib.Path) -> bool:
    """True if `path` looks binary (a NUL byte in its first
    `_BINARY_SNIFF_BYTES`), or if it cannot be read at all -- an unreadable
    file is never scannable text either way."""
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(_BINARY_SNIFF_BYTES)
    except OSError:
        return True
    return b"\x00" in chunk


def _walk(root: pathlib.Path, max_depth: "int | None" = None):
    """`os.walk` over `root`, pruning `_PRUNED_DIR_NAMES` and, when
    `max_depth` is given, never descending past it (depth 0 = files
    directly in `root`)."""
    root_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _PRUNED_DIR_NAMES)
        depth = len(pathlib.Path(dirpath).parts) - root_depth
        if max_depth is not None and depth >= max_depth:
            dirnames[:] = []
        for name in sorted(filenames):
            yield pathlib.Path(dirpath) / name


def _find_by_filename_python(
    root: pathlib.Path, max_depth: "int | None" = None
) -> list[pathlib.Path]:
    """Pure-Python filename sweep -- the fallback when `find` is not on
    PATH. Correct but slow at scale (measured: see `_find_by_filename`)."""
    return [p for p in _walk(root, max_depth=max_depth) if p.name in CREDENTIAL_FILENAMES]


def _find_by_filename(root: pathlib.Path, max_depth: "int | None" = None) -> list[pathlib.Path]:
    """Filename sweep for `CREDENTIAL_FILENAMES`, shelling out to `find`
    when available. Not just for the huge-`$TMPDIR` case: a single
    harness-owned root here (a hook-cli-skew rehearsal's `.artifacts`
    staging tree) can itself hold thousands of files (~8000 in one
    measured on this box, ~54000 summed across the 26 such roots present
    at scan time), and a per-entry Python `os.walk`/`os.scandir` over that
    many entries -- even bounded to `max_depth` for the `$TMPDIR` case --
    measurably does not finish inside two minutes (a real dev-box
    `$TMPDIR` was measured at ~79000 top-level entries alone), while
    `find -maxdepth 4` covers the same tree in ~22s. `max_depth=None`
    omits `-maxdepth` entirely (unbounded, for the small harness-owned
    roots). Falls back to the pure-Python walker when `find` is not on
    PATH."""
    find_bin = shutil.which("find")
    if find_bin is None:
        return _find_by_filename_python(root, max_depth=max_depth)
    args = [find_bin, str(root)]
    if max_depth is not None:
        args += ["-maxdepth", str(max_depth)]
    for i, name in enumerate(sorted(CREDENTIAL_FILENAMES)):
        args += (["-o"] if i else ["("]) + ["-name", name]
    args += [")", "-print0"]
    proc = subprocess.run(args, capture_output=True, text=True)
    # A transient ENOENT on one race-deleted entry (e.g. a sibling test's
    # own tmp cleanup mid-walk) is not a scan failure -- `find` still
    # prints every match it found on other branches; only trust the
    # result when stdout parses, regardless of a nonzero/racy stderr.
    names = [n for n in proc.stdout.split("\0") if n]
    return sorted(pathlib.Path(n) for n in names)


def _find_by_content_python(root: pathlib.Path) -> list[pathlib.Path]:
    """Pure-Python content sweep -- the fallback when `grep` is not on
    PATH. Correct but slow at scale (measured: see `_find_by_content`)."""
    hits = []
    for path in _walk(root):
        if _is_binary(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            continue
        if TOKEN_RE.search(text):
            hits.append(path)
    return hits


def _find_by_content(root: pathlib.Path) -> list[pathlib.Path]:
    """Content sweep for the token pattern, restricted to text files.
    Shells out to `grep -rlIE` when available: one process per root
    instead of tens of thousands of individual Python `open()`/`read()`
    calls, measured necessary rather than a style preference -- a single
    harness-owned root here (a hook-cli-skew rehearsal's `.artifacts`
    staging tree) can itself hold ~8000 files (54000+ summed across the 26
    such roots present on this box at scan time), and the pure-Python
    per-file walker at that scale did not finish inside two minutes.
    `grep -I` skips binary files -- verified equivalent to this module's
    own NUL-byte sniff (`_is_binary`) on both BSD grep (macOS's system
    grep) and GNU grep: a file containing the token bytes alongside a NUL
    byte is excluded by `-I` exactly as `_find_by_content_python` excludes
    it. The pattern is `TOKEN_RE.pattern` itself -- one source, never
    retyped as a shell string. Falls back to the pure-Python walker when
    `grep` is not on PATH."""
    grep_bin = shutil.which("grep")
    if grep_bin is None:
        return _find_by_content_python(root)
    proc = subprocess.run(
        [grep_bin, "-rlIE", TOKEN_RE.pattern, str(root)],
        capture_output=True, text=True,
    )
    # grep's own exit code is not consulted: 1 means "no matches" (not an
    # error), and a race-deleted file mid-walk still leaves every other
    # match on stdout -- the same "trust what was found" posture as
    # `_find_by_filename`.
    return sorted(pathlib.Path(line) for line in proc.stdout.splitlines() if line)


def _repo_tree_candidates(repo_root: pathlib.Path) -> list[pathlib.Path]:
    """Every tracked-or-untracked-but-not-ignored file under `repo_root`,
    via `git ls-files` -- same enumeration convention as
    `test_claude_credentials_single_source_lint.py`'s `_tracked_corpus`.
    Cheap and naturally skips `.git/`, `.venv/`, `service/target/` and any
    other gitignored build output."""
    out = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        # Not a git checkout (or git unavailable): fall back to a plain
        # walk rather than silently scanning nothing.
        return _find_by_filename(repo_root, max_depth=None)
    names = [n for n in out.stdout.split("\0") if n]
    return [repo_root / n for n in names if pathlib.Path(n).name in CREDENTIAL_FILENAMES]


# ===========================================================================
# Process sweep -- a live process holding a protected token in its
# environment, never a file on disk.
# ===========================================================================


def _own_ancestor_pids() -> "set[int]":
    """This process's own pid plus every ancestor up to (but not including)
    pid 1/`init`, via repeated `ps -o ppid=`. Bounded to 64 hops so a
    corrupt or cyclic ppid chain can never loop forever."""
    pids = {os.getpid()}
    pid = os.getpid()
    for _ in range(64):
        proc = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True,
        )
        text = proc.stdout.strip()
        if not text:
            break
        try:
            ppid = int(text)
        except ValueError:
            break
        if ppid <= 1 or ppid in pids:
            break
        pids.add(ppid)
        pid = ppid
    return pids


def _parse_etime(etime: str) -> int:
    """Parses a `ps -o etime=` value (`[[DD-]HH:]MM:SS`) into seconds."""
    etime = etime.strip()
    days = 0
    if "-" in etime:
        day_part, etime = etime.split("-", 1)
        days = int(day_part)
    parts = [int(p) for p in etime.split(":")]
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = 0, parts[0], parts[1]
    elif len(parts) == 1:
        h, m, s = 0, 0, parts[0]
    else:
        raise ValueError(f"unrecognized etime format: {etime!r}")
    return days * 86400 + h * 3600 + m * 60 + s


def _format_etime(total_seconds: float) -> str:
    """The inverse shape of `_parse_etime`, for ages computed on the Linux
    `/proc` path (which has no `ps`-formatted etime string to reuse)."""
    total = int(total_seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days:
        return f"{days}-{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _run_ps_macos() -> str:
    """`ps -Eww` over every process: `-E` prepends each process's
    environment to its command text (for the binary shapes that show it --
    see the module docstring's PROCESS AND TMUX SWEEPS section), `-ww`
    disables output truncation so a long env block is never cut off."""
    proc = subprocess.run(
        ["ps", "-Eww", "-axo", "pid=,etime=,comm=,command="],
        capture_output=True, text=True,
    )
    return proc.stdout


def _scan_processes_macos(
    *, min_age_seconds: int, exclude_pids: "set[int]", ps_runner=None,
) -> "list[ProcessFinding]":
    runner = ps_runner or _run_ps_macos
    output = runner()
    findings: list[ProcessFinding] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 3)
        if len(parts) < 3:
            continue
        pid_str, etime_str, comm = parts[0], parts[1], parts[2]
        rest = parts[3] if len(parts) > 3 else ""
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        if pid in exclude_pids:
            continue
        matched = any(
            re.search(rf"(?:^|\s){re.escape(name)}=", rest) for name in PROTECTED_ENV_VAR_NAMES
        )
        del rest  # the command/env text is never retained past this check
        if not matched:
            continue
        try:
            age = _parse_etime(etime_str)
        except ValueError:
            continue
        if age < min_age_seconds:
            continue
        findings.append(ProcessFinding(pid=pid, etime=etime_str, comm=comm))
    return findings


def _linux_process_age_seconds(
    stat_path: pathlib.Path, uptime_seconds: float, clk_tck: int
) -> float:
    """`/proc/<pid>/stat`'s `starttime` field (22nd, 1-indexed; clock ticks
    since boot) against `/proc/uptime`. The comm field is parenthesized and
    may itself contain spaces, so this reads from the LAST `)` rather than
    splitting naively."""
    text = stat_path.read_text()
    right_paren = text.rindex(")")
    after = text[right_paren + 1:].split()
    starttime_ticks = int(after[19])  # field 22 == after[22-3]
    return uptime_seconds - (starttime_ticks / clk_tck)


def _scan_processes_linux(
    *,
    min_age_seconds: int,
    exclude_pids: "set[int]",
    proc_root: pathlib.Path,
    clk_tck: "int | None" = None,
) -> "list[ProcessFinding]":
    findings: list[ProcessFinding] = []
    if not proc_root.is_dir():
        return findings
    try:
        uptime_seconds = float((proc_root / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return findings
    ticks = clk_tck
    if ticks is None:
        ticks = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    for entry in sorted(proc_root.iterdir()):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in exclude_pids:
            continue
        try:
            raw = (entry / "environ").read_bytes()
        except OSError:
            continue
        matched = False
        for chunk in raw.split(b"\x00"):
            if not chunk:
                continue
            name = chunk.split(b"=", 1)[0].decode("utf-8", errors="replace")
            if name in PROTECTED_ENV_VAR_NAMES:
                matched = True
        del raw  # the environment bytes are never retained past this check
        if not matched:
            continue
        try:
            age = _linux_process_age_seconds(entry / "stat", uptime_seconds, ticks)
        except (OSError, ValueError, IndexError):
            continue
        if age < min_age_seconds:
            continue
        try:
            comm = (entry / "comm").read_text().strip()
        except OSError:
            comm = "?"
        findings.append(ProcessFinding(pid=pid, etime=_format_etime(age), comm=comm))
    return findings


def scan_processes(
    *,
    min_age_seconds: int,
    exclude_pids: "set[int]",
    platform_name: "str | None" = None,
    ps_runner=None,
    proc_root: "pathlib.Path | None" = None,
    clk_tck: "int | None" = None,
) -> "list[ProcessFinding]":
    """Lists this user's processes whose environment names a
    `PROTECTED_ENV_VAR_NAMES` entry, older than `min_age_seconds` and not
    in `exclude_pids`. Dispatches on `platform_name` (default `sys.platform`):
    `"darwin"` reads `ps -Eww` (`ps_runner`, injectable); anything else
    reads `/proc` directly (`proc_root`/`clk_tck`, injectable) -- the same
    shape a Linux box or a WSL2 appliance (RDR-218) both present."""
    plat = platform_name if platform_name is not None else sys.platform
    if plat == "darwin":
        return _scan_processes_macos(
            min_age_seconds=min_age_seconds, exclude_pids=exclude_pids, ps_runner=ps_runner,
        )
    root = proc_root if proc_root is not None else pathlib.Path("/proc")
    return _scan_processes_linux(
        min_age_seconds=min_age_seconds, exclude_pids=exclude_pids, proc_root=root, clk_tck=clk_tck,
    )


# ===========================================================================
# Tmux sweep -- a live tmux server on a known harness socket name.
# ===========================================================================


def _run_tmux_ls(socket_name: str) -> bool:
    """True iff a tmux server is actually listening on `socket_name` --
    `tmux -L <name> ls` fails against a stale socket FILE with no server
    behind it."""
    proc = subprocess.run(["tmux", "-L", socket_name, "ls"], capture_output=True, text=True)
    return proc.returncode == 0


def discover_tmux_sockets(
    *,
    roots: "list[pathlib.Path]",
    min_age_seconds: int,
    patterns: "tuple[str, ...]" = TMUX_SOCKET_PATTERNS,
    tmux_runner=None,
) -> "list[TmuxFinding]":
    """Lists tmux socket files under `roots` whose name matches a
    `patterns` glob, is old enough (socket file mtime vs. `min_age_seconds`)
    and has a live server behind it (`tmux_runner`, injectable; default
    `_run_tmux_ls`)."""
    runner = tmux_runner or _run_tmux_ls
    now = time.time()
    findings: list[TmuxFinding] = []
    for root in roots:
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if entry.is_dir():
                continue
            name = entry.name
            if not any(fnmatch.fnmatch(name, pat) for pat in patterns):
                continue
            try:
                age = now - entry.stat().st_mtime
            except OSError:
                continue
            if age < min_age_seconds:
                continue
            if not runner(name):
                continue
            findings.append(TmuxFinding(socket_name=name, socket_path=entry))
    return findings


def scan(
    *,
    repo_root: pathlib.Path,
    tmpdir: pathlib.Path,
    scratchpad_roots: "list[pathlib.Path]",
    content_roots: "list[pathlib.Path]",
    max_depth: int = 4,
    process_findings: "list[ProcessFinding] | None" = None,
    tmux_findings: "list[TmuxFinding] | None" = None,
) -> ScanResult:
    """Run the file sweeps, plus whatever `process_findings`/`tmux_findings`
    the caller already collected (via `scan_processes`/
    `discover_tmux_sockets` -- kept OUT of this function so it stays a pure
    aggregator: every existing caller that omits them gets file-sweep-only
    behavior, touching no process table or tmux socket, unchanged).
    `repo_root` and `tmpdir` are REQUIRED to exist.

    `scratchpad_roots` (a Claude Code session's own `/private/tmp/claude-*`
    directory) get the FILENAME sweep only, bounded to `max_depth`, same as
    `tmpdir` -- measured on this box (nexus-wauo1.24 implementation,
    2026-09-25): a live scratchpad root is not small. One session alone
    held 65 GB (a sibling session's own working state; this session's own
    scratchpad held 13 GB), and an unbounded `grep -rlIE` over that did not
    finish inside several minutes. The bead's scope-addition comment names
    this exact failure shape for `$TMPDIR` itself ("an unbounded recursive
    grep there does not finish"); the same reasoning applies here because
    the measured reality is the same shape, even though the comment did
    not separately measure the scratchpad root.

    `content_roots` (`$TMPDIR`'s `*.artifacts`/`rdr208-mvv.*` stage
    folders, `~/nexus-sandbox`) get BOTH sweeps, unbounded depth: these
    ARE the "harness-owned roots" the bead's scope-addition comment means
    by that phrase -- small, self-contained, bounded harness OUTPUT
    (largest measured on this box: ~8000 files, ~670 MB, ~3s to grep),
    never a live, indefinitely-growing session directory."""
    if not repo_root.is_dir():
        return ScanResult(error=f"required root does not exist: {repo_root}")
    if not tmpdir.is_dir():
        return ScanResult(error=f"required root does not exist: {tmpdir}")

    findings: set[pathlib.Path] = set()
    findings.update(_repo_tree_candidates(repo_root))
    findings.update(_find_by_filename(tmpdir, max_depth=max_depth))

    for root in scratchpad_roots:
        if root.is_dir():
            findings.update(_find_by_filename(root, max_depth=max_depth))

    for root in content_roots:
        if root.is_dir():
            findings.update(_find_by_filename(root, max_depth=None))
            findings.update(_find_by_content(root))

    return ScanResult(
        findings=sorted(findings),
        process_findings=list(process_findings) if process_findings else [],
        tmux_findings=list(tmux_findings) if tmux_findings else [],
    )


def format_report(result: ScanResult) -> str:
    lines: list[str] = []
    if result.error is not None:
        lines.append(f"CREDENTIAL JANITOR FAILED -- {result.error}")
        return "\n".join(lines)
    for path in result.findings:
        lines.append(f"credential-shaped file: {path}")
    for pf in result.process_findings:
        lines.append(
            f"credential-bearing process: pid={pf.pid} etime={pf.etime} comm={pf.comm} "
            "(protected env var present)"
        )
    for tf in result.tmux_findings:
        lines.append(
            f"credential-bearing tmux server: -L {tf.socket_name} "
            f"(remedy: tmux -L {tf.socket_name} kill-server)"
        )
    total = len(result.findings) + len(result.process_findings) + len(result.tmux_findings)
    if total:
        lines.append(
            f"CREDENTIAL JANITOR FAILED -- {len(result.findings)} file(s), "
            f"{len(result.process_findings)} process(es), {len(result.tmux_findings)} "
            "tmux server(s) found"
        )
    else:
        lines.append("CREDENTIAL JANITOR PASSED")
    return "\n".join(lines)


def _run_status_warning(cred_tool: pathlib.Path) -> str:
    """Runs `claude_credentials.py status` and returns its output verbatim
    for logging. Its exit code is NEVER consulted by the caller -- this
    leg fails only on the scan, per the bead's own text ("prints a
    warning, without failing"). `status`'s own module contract guarantees
    no credential material on any path, so forwarding its output verbatim
    is safe."""
    if not cred_tool.is_file():
        return f"(status check skipped -- {cred_tool} not found)"
    proc = subprocess.run(
        [sys.executable, str(cred_tool), "status"],
        capture_output=True, text=True,
    )
    parts = [line for line in (proc.stdout, proc.stderr) if line.strip()]
    return "\n".join(parts) if parts else "(status printed nothing)"


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument("--tmpdir", default=os.environ.get("TMPDIR", "/tmp"))
    parser.add_argument("--home", default=str(pathlib.Path.home()))
    parser.add_argument("--scratchpad-parent", default="/private/tmp")
    parser.add_argument("--cred-tool", default=str(_CRED_TOOL_DEFAULT))
    parser.add_argument("--max-depth", type=int, default=4)
    parser.add_argument(
        "--process-min-age", type=int, default=DEFAULT_PROCESS_MIN_AGE_SECONDS,
        help=(
            "Minimum age (seconds) for a credential-bearing process or tmux "
            "server to be reported -- the release battery may itself be "
            "running a harness leg concurrently. Default 2h."
        ),
    )
    parser.add_argument(
        "--tmux-socket-root", action="append", default=None,
        help=(
            "Directory to scan for harness tmux sockets (repeatable); "
            "defaults to /private/tmp/tmux-<uid> and /tmp/tmux-<uid>."
        ),
    )
    args = parser.parse_args(argv)

    repo_root = pathlib.Path(args.repo_root)
    tmpdir = pathlib.Path(args.tmpdir)
    home = pathlib.Path(args.home)
    scratchpad_parent = pathlib.Path(args.scratchpad_parent)

    scratchpad_roots = (
        sorted(scratchpad_parent.glob("claude-*")) if scratchpad_parent.is_dir() else []
    )
    content_roots = (
        (sorted(tmpdir.glob("*.artifacts")) if tmpdir.is_dir() else [])
        + (sorted(tmpdir.glob("rdr208-mvv.*")) if tmpdir.is_dir() else [])
    )
    sandbox = home / "nexus-sandbox"
    if sandbox.is_dir():
        content_roots.append(sandbox)

    if args.tmux_socket_root:
        tmux_roots = [pathlib.Path(p) for p in args.tmux_socket_root]
    else:
        uid = os.getuid()
        tmux_roots = [
            pathlib.Path(f"/private/tmp/tmux-{uid}"),
            pathlib.Path(f"/tmp/tmux-{uid}"),
        ]

    own_pids = _own_ancestor_pids()
    process_findings = scan_processes(min_age_seconds=args.process_min_age, exclude_pids=own_pids)
    tmux_findings = discover_tmux_sockets(roots=tmux_roots, min_age_seconds=args.process_min_age)

    result = scan(
        repo_root=repo_root,
        tmpdir=tmpdir,
        scratchpad_roots=scratchpad_roots,
        content_roots=content_roots,
        max_depth=args.max_depth,
        process_findings=process_findings,
        tmux_findings=tmux_findings,
    )

    print(_run_status_warning(pathlib.Path(args.cred_tool)))
    print(format_report(result))
    return 1 if (
        result.error is not None
        or result.findings
        or result.process_findings
        or result.tmux_findings
    ) else 0


if __name__ == "__main__":
    sys.exit(main())
