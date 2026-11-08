# SPDX-License-Identifier: AGPL-3.0-or-later
"""``git.diff_names(base)`` and ``git.diff_stat(base)`` (RDR-242 Technical
Design 10; nexus-mdbds.8).

Both read the checkout the client runs in (its working directory) and
describe what the branch adds since ``base``: the diff from the merge base
of ``base`` and ``HEAD`` to ``HEAD`` (``git diff base...HEAD``), so a commit
on ``base``'s side after the fork is not counted. Renames are off, so a
moved file shows as its old and new paths. Each git subprocess gets the
run's remaining budget as its ``timeout``.
"""

from __future__ import annotations

import json
import subprocess
import time

from nexus.lua_runtime.verb_types import (
    VERB_RESULT_CAP_BYTES,
    JSONValue,
    ParamSpec,
    VerbError,
    VerbSpec,
    VerbTimeout,
)


def _git(args: list[str], *, deadline: float) -> str:
    left = deadline - time.monotonic()
    if left <= 0:
        raise VerbTimeout("git: the run's time budget is spent")
    try:
        proc = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            timeout=left,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise VerbTimeout("git: the run's time budget ran out during git") from None
    except OSError as exc:
        raise VerbError(f"git: cannot run git: {exc}") from None
    if proc.returncode != 0:
        err = proc.stderr.strip().splitlines()
        raise VerbError(f"git: {err[0] if err else f'exit {proc.returncode}'}")
    return proc.stdout


def _commit(base: str, *, deadline: float) -> str:
    """The commit ``base`` names, refusing what git would read as an option."""
    if not base or base.startswith("-") or "\x00" in base:
        raise VerbError(f"git: base {base!r} is not a revision")
    _git(["rev-parse", "--is-inside-work-tree"], deadline=deadline)
    try:
        out = _git(
            [
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"{base}^{{commit}}",
            ],
            deadline=deadline,
        )
    except VerbTimeout:
        raise
    except VerbError:
        out = ""
    sha = out.strip()
    if not sha:
        raise VerbError(f"git: base {base!r} does not name a commit")
    return sha


def _diff(base: str, mode: str, *, deadline: float) -> list[str]:
    sha = _commit(base, deadline=deadline)
    out = _git(
        [
            "diff",
            mode,
            "-z",
            "--no-renames",
            "--no-ext-diff",
            "--no-color",
            f"{sha}...HEAD",
            "--",
        ],
        deadline=deadline,
    )
    return [f for f in out.split("\x00") if f]


def _capped(value: JSONValue, verb: str) -> JSONValue:
    size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    if size > VERB_RESULT_CAP_BYTES:
        raise VerbError(
            f"{verb}: the diff is {size} bytes of JSON, over the {VERB_RESULT_CAP_BYTES}-byte verb result cap"
        )
    return value


def diff_names(base: str, *, deadline: float) -> JSONValue:
    """Paths the branch changed since ``base``, sorted, repo-relative."""
    return _capped(
        sorted(_diff(base, "--name-only", deadline=deadline)), "git.diff_names"
    )


def diff_stat(base: str, *, deadline: float) -> JSONValue:
    """Per-file added and deleted line counts since ``base``, with totals.

    ``{"files": [{"path", "added", "deleted", "binary"}], "added", "deleted"}``;
    a binary file's counts are null.
    """
    files = []
    for field in _diff(base, "--numstat", deadline=deadline):
        added, deleted, path = field.split("\t", 2)
        binary = added == "-"
        files.append(
            {
                "path": path,
                "added": None if binary else int(added),
                "deleted": None if binary else int(deleted),
                "binary": binary,
            }
        )
    files.sort(key=lambda f: f["path"])
    return _capped(
        {
            "files": files,
            "added": sum(f["added"] or 0 for f in files),
            "deleted": sum(f["deleted"] or 0 for f in files),
        },
        "git.diff_stat",
    )


VERBS: tuple[VerbSpec, ...] = (
    VerbSpec("git.diff_names", (ParamSpec("base", "string"),), diff_names),
    VerbSpec("git.diff_stat", (ParamSpec("base", "string"),), diff_stat),
)
