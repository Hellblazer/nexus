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
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys
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


@dataclass
class ScanResult:
    findings: list[pathlib.Path] = field(default_factory=list)
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


def scan(
    *,
    repo_root: pathlib.Path,
    tmpdir: pathlib.Path,
    scratchpad_roots: "list[pathlib.Path]",
    content_roots: "list[pathlib.Path]",
    max_depth: int = 4,
) -> ScanResult:
    """Run both sweeps. `repo_root` and `tmpdir` are REQUIRED to exist.

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

    return ScanResult(findings=sorted(findings))


def format_report(result: ScanResult) -> str:
    lines: list[str] = []
    if result.error is not None:
        lines.append(f"CREDENTIAL JANITOR FAILED -- {result.error}")
        return "\n".join(lines)
    for path in result.findings:
        lines.append(f"credential-shaped file: {path}")
    if result.findings:
        lines.append(f"CREDENTIAL JANITOR FAILED -- {len(result.findings)} file(s) found")
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

    result = scan(
        repo_root=repo_root,
        tmpdir=tmpdir,
        scratchpad_roots=scratchpad_roots,
        content_roots=content_roots,
        max_depth=args.max_depth,
    )

    print(_run_status_warning(pathlib.Path(args.cred_tool)))
    print(format_report(result))
    return 1 if (result.error is not None or result.findings) else 0


if __name__ == "__main__":
    sys.exit(main())
