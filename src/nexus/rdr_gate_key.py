# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""What an RDR gate record is keyed to (nexus-3jjkg).

The gate used to name the branch commit it judged (``commit:``), and a
rebase before landing orphaned that sha: the T2 fix-check title, the
Revision History line and every doc citation then named a commit nobody
could open. A gate now also records ``blob:``, the git blob id of the RDR
file it judged (``git rev-parse HEAD:<rel>``). The blob is the exact text,
so it survives any history rewrite that leaves the file's text alone.

A record carrying ``blob:`` is keyed by it; a record with only ``commit:``
(every record written before this change) keeps the commit key and the
``<commit>..HEAD`` range exactly as before. Nothing rewrites an old record.

Stdlib plus :mod:`nexus.bounded_subprocess` only: the SessionStart hook
imports :func:`prefix_match` from here.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

import structlog

from nexus.bounded_subprocess import run_bounded

_log = structlog.get_logger(__name__)

#: Hex digits of a blob id written into a record, a fix-check title and a
#: Revision History line. Longer than the commit abbreviation (``%h``) the
#: records carried before, because a blob id has no history to disambiguate.
BLOB_ABBREV = 12

#: A key shorter than this never matches by prefix (same floor the commit
#: comparisons always had).
MIN_PREFIX = 7

_GIT_TIMEOUT_S = 20


def clean_field(raw: str | None) -> str:
    """A parsed T2 field value with quotes, backticks and spaces removed."""
    return (raw or "").strip().strip('"').strip("`").strip()


def prefix_match(a: str, b: str) -> bool:
    """True when the shorter of *a* and *b* is at least :data:`MIN_PREFIX`
    long and is a prefix of the other."""
    n = min(len(a), len(b))
    return n >= MIN_PREFIX and a[:n].lower() == b[:n].lower()


@dataclass(frozen=True)
class GatedKey:
    """The key a gate record carries: its blob when it has one, else its
    commit, else nothing. ``commit`` is kept even for a blob key because the
    fix commits are still listed from it when it resolves."""

    kind: str  # "blob" | "commit" | ""
    value: str
    commit: str = ""

    @property
    def is_blob(self) -> bool:
        return self.kind == "blob"

    def matches(self, sha: str) -> bool:
        """Whether *sha* (a ``fix_check:`` pointer) names this key."""
        return bool(self.value) and prefix_match(sha, self.value)


def gated_key(blob: str | None, commit: str | None) -> GatedKey:
    """Blob if present, else commit, else the empty key."""
    b, c = clean_field(blob), clean_field(commit)
    if b:
        return GatedKey("blob", b, c)
    if c:
        return GatedKey("commit", c, c)
    return GatedKey("", "", "")


def _git(repo_root: str, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str] | None:
    """One bounded git call; ``None`` when it could not run or timed out."""
    try:
        return run_bounded(["git", "-C", repo_root, *args], timeout=_GIT_TIMEOUT_S, input=input_text)
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("rdr_gate_key_git_failed", args=list(args), error=str(exc))
        return None


def git_path(rel: str) -> str:
    """*rel* with forward slashes, the only separator git's ``<rev>:<path>``
    syntax reads. ``os.path.relpath`` gives backslashes on Windows, and
    ``HEAD:docs\\rdr\\x.md`` resolves to nothing, which the callers read as an
    untracked file (the quiet miss nexus-3jjkg exists to remove)."""
    return rel.replace("\\", "/")


def file_blob(repo_root: str, rel: str, rev: str = "HEAD", *, abbrev: int | None = BLOB_ABBREV) -> str | None:
    """The blob id of *rel* at *rev*, abbreviated to *abbrev* hex digits
    (``None`` for the full id), or ``None`` when git cannot say (an
    untracked file, a bad rev)."""
    proc = _git(repo_root, "rev-parse", "--verify", "-q", f"{rev}:{git_path(rel)}")
    if proc is None or proc.returncode != 0:
        return None
    full = proc.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", full):
        return None
    return full if abbrev is None else full[:abbrev]


def commit_resolves(repo_root: str, commit: str) -> bool:
    """Whether *commit* names a commit object this clone still holds."""
    if not commit:
        return False
    proc = _git(repo_root, "cat-file", "-e", f"{commit}^{{commit}}")
    return proc is not None and proc.returncode == 0


@dataclass(frozen=True)
class BlobDelta:
    """The RDR file now against the blob a gate judged."""

    state: str  # "same" | "changed" | "unknown"
    current: str  # the file's blob at HEAD (abbreviated), "" when unknown
    stat: tuple[str, ...] = ()  # ``git diff --stat`` lines, empty when same
    error: str = ""


def blob_delta(repo_root: str, rel: str, gated_blob: str) -> BlobDelta:
    """Compare *rel* at HEAD with *gated_blob*. No history is read, so a
    rebase between the gate and now changes nothing here."""
    current_full = file_blob(repo_root, rel, abbrev=None)
    if current_full is None:
        return BlobDelta("unknown", "", error=f"git cannot read {rel} at HEAD")
    current = current_full[:BLOB_ABBREV]
    if prefix_match(gated_blob, current_full):
        return BlobDelta("same", current)
    proc = _git(repo_root, "diff", "--stat", gated_blob, current_full)
    if proc is None or proc.returncode != 0:
        err = (proc.stderr if proc is not None else "git did not run").strip()[:160]
        return BlobDelta("changed", current, error=f"git diff exited non-zero: {err}")
    return BlobDelta("changed", current, stat=tuple(proc.stdout.strip().splitlines()))


def blob_range(gated_blob: str, current: str) -> str:
    """The command a fix check reads for a blob key."""
    return f"git diff {gated_blob} {current}"


#: What the gate prints when the RDR file on disk is not the text HEAD holds:
#: the blob is read from HEAD, so it would name text the critic never judged.
DIRTY_NOTE = (
    "RDR file has uncommitted edits; commit before gating — the blob would name "
    "text the critic did not judge."
)


def has_uncommitted_edits(repo_root: str, rel: str) -> bool:
    """True when the working-tree file differs from ``HEAD:<rel>``. ``False``
    when either side cannot be read (an untracked file has no blob to
    misname)."""
    head = file_blob(repo_root, rel, abbrev=None)
    if head is None:
        return False
    proc = _git(repo_root, "hash-object", "--", git_path(rel))
    if proc is None or proc.returncode != 0:
        return False
    return proc.stdout.strip() != head


#: Most commits :func:`fix_commits` walks back through the file's history.
FIX_LOG_CAP = 50


@dataclass(frozen=True)
class FixCommits:
    """The commits that changed the RDR file since the gated text."""

    found: bool  # the gated blob was met within the walk
    commits: tuple[str, ...] = ()  # ``<abbrev> <subject>``, newest first
    capped: bool = False  # the walk hit :data:`FIX_LOG_CAP` before finding it
    error: str = ""


def fix_commits(repo_root: str, rel: str, gated_blob: str, cap: int = FIX_LOG_CAP) -> FixCommits:
    """Walk the file's history from HEAD and stop at the first commit whose
    ``<commit>:<rel>`` blob is the gated blob; the commits above it are the
    fix commits (nexus-3jjkg). Reads no commit named by the gate record, so a
    rebase that replaced the gated commit lists the same commits, and the
    rebased copy of the gated commit is the stop, never a fix. Bounded: one
    ``git log`` and one ``cat-file --batch-check``, at most *cap* commits."""
    path = git_path(rel)
    log = _git(repo_root, "log", "--format=%H %h %s", "-n", str(cap), "--", path)
    if log is None or log.returncode != 0:
        err = (log.stderr if log is not None else "git did not run").strip()[:160]
        return FixCommits(False, error=f"git log failed: {err}")
    rows = [ln.split(" ", 2) for ln in log.stdout.splitlines() if ln.strip()]
    rows = [r for r in rows if len(r) == 3]
    if not rows:
        return FixCommits(False)
    check = _git(
        repo_root, "cat-file", "--batch-check", input_text="".join(f"{r[0]}:{path}\n" for r in rows),
    )
    if check is None or check.returncode != 0:
        err = (check.stderr if check is not None else "git did not run").strip()[:160]
        return FixCommits(False, error=f"git cat-file failed: {err}")
    blobs = [ln.split() for ln in check.stdout.splitlines()]
    fixes: list[str] = []
    for row, parts in zip(rows, blobs):
        if len(parts) >= 2 and parts[1] == "blob" and prefix_match(parts[0], gated_blob):
            return FixCommits(True, tuple(fixes))
        fixes.append(f"{row[1]} {row[2]}")
    return FixCommits(False, capped=len(rows) >= cap)
