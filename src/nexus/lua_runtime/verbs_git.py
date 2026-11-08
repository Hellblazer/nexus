# SPDX-License-Identifier: AGPL-3.0-or-later
"""``git.diff_names(base)`` and ``git.diff_stat(base)`` (RDR-242 Technical
Design 10; nexus-mdbds.8).

Both read the repository ``run_script`` was called in (``ctx.repo_root``,
never a process's current directory) and describe what its branch adds since
``base``: the diff from the merge base of ``base`` and ``HEAD`` to ``HEAD``
(``git diff base...HEAD``), so a commit on ``base``'s side after the fork is
not counted. Committed work only: uncommitted changes are not in it. Renames
are off, so a moved file shows as its old path and its new one. Paths are
always repository-relative, whatever the user's ``diff.relative``. Each git
subprocess gets the run's remaining budget as its ``timeout``, and an
environment without the caller's ``GIT_*`` variables, so a ``GIT_DIR`` set by a
hook cannot point it at another repository. Needs git 2.28 or later
(``--no-relative``).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from nexus.bounded_subprocess import run_bounded
from nexus.lua_runtime.verb_types import (
    JSONValue,
    ParamSpec,
    VerbContext,
    VerbError,
    VerbSpec,
    VerbTimeout,
    encode_result,
)


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["LC_ALL"] = "C"
    return env


class _NoCommit(VerbError):
    """rev-parse --verify --quiet found no such commit (exit 1, nothing said)."""


def _git(args: list[str], *, cwd: Path, deadline: float) -> str:
    left = deadline - time.monotonic()
    if left <= 0:
        raise VerbTimeout("git: the run's time budget is spent")
    try:
        # core.quotepath=false: paths as bytes, never C-quoted. Output is read
        # as bytes and decoded with replacement, so a path that is not UTF-8
        # never carries a lone surrogate into the result. run_bounded resolves
        # git off the working directory.
        proc = run_bounded(
            ["git", "-c", "core.quotepath=false", *args],
            timeout=left,
            cwd=cwd,
            env=_env(),
            text=False,
            encoding=None,
            errors=None,
        )
    except subprocess.TimeoutExpired:
        raise VerbTimeout("git: the run's time budget ran out during git") from None
    except OSError as exc:
        raise VerbError(f"git: cannot run git: {exc.strerror}") from None
    if proc.returncode == 1 and not proc.stderr.strip() and args[0] == "rev-parse":
        raise _NoCommit("no such commit")
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise VerbError(f"git: {err[0][:300] if err else f'exit {proc.returncode}'}")
    return proc.stdout.decode("utf-8", "replace")


def _repo(ctx: VerbContext) -> Path:
    if ctx.repo_root is None:
        raise VerbError("git: the run was not started inside a git repository")
    return ctx.repo_root


def _commit(base: str, *, cwd: Path, deadline: float) -> str:
    """The commit ``base`` names, refusing what git would read as an option.

    Two layers, each enough alone: a leading ``-`` is refused here, and
    ``--end-of-options`` stops rev-parse reading one. No test can tell them
    apart, since no ref name starts with ``-``.
    """
    if not base or base.startswith("-") or "\x00" in base:
        raise VerbError(f"git: base {base[:200]!r} is not a revision")
    try:
        out = _git(
            [
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"{base}^{{commit}}",
            ],
            cwd=cwd,
            deadline=deadline,
        )
    except _NoCommit:
        out = ""
    sha = out.strip()
    if not sha:
        raise VerbError(f"git: base {base[:200]!r} does not name a commit")
    return sha


def _diff(base: str, mode: str, ctx: VerbContext, *, deadline: float) -> list[str]:
    cwd = _repo(ctx)
    sha = _commit(base, cwd=cwd, deadline=deadline)
    out = _git(
        [
            "diff",
            mode,
            "-z",
            "--no-renames",
            "--no-relative",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            f"{sha}...HEAD",
            "--",
        ],
        cwd=cwd,
        deadline=deadline,
    )
    return [f for f in out.split("\x00") if f]


def _capped(value: JSONValue) -> JSONValue:
    encode_result(value)  # VerbError past the verb result cap
    return value


def diff_names(base: str, *, deadline: float, ctx: VerbContext) -> JSONValue:
    """Paths the branch changed since ``base``, sorted, repo-relative."""
    return _capped(sorted(_diff(base, "--name-only", ctx, deadline=deadline)))


def diff_stat(base: str, *, deadline: float, ctx: VerbContext) -> JSONValue:
    """Per-file added and deleted line counts since ``base``, with totals.

    ``{"files": [{"path", "added", "deleted", "binary"}], "added", "deleted"}``;
    a binary file's counts are null.
    """
    files = []
    for field in _diff(base, "--numstat", ctx, deadline=deadline):
        parts = field.split("\t", 2)
        if len(parts) != 3:
            raise VerbError("git.diff_stat: git printed a numstat line it cannot read")
        added, deleted, path = parts
        binary = added == "-"
        try:
            counts = (None, None) if binary else (int(added), int(deleted))
        except ValueError:
            raise VerbError(
                "git.diff_stat: git printed a count it cannot read"
            ) from None
        files.append(
            {"path": path, "added": counts[0], "deleted": counts[1], "binary": binary}
        )
    files.sort(key=lambda f: f["path"])
    return _capped(
        {
            "files": files,
            "added": sum(f["added"] or 0 for f in files),
            "deleted": sum(f["deleted"] or 0 for f in files),
        }
    )


VERBS: tuple[VerbSpec, ...] = (
    VerbSpec("git.diff_names", (ParamSpec("base", "string"),), diff_names),
    VerbSpec("git.diff_stat", (ParamSpec("base", "string"),), diff_stat),
)
