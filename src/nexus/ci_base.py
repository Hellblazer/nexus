# SPDX-License-Identifier: AGPL-3.0-or-later
"""The base of a branch's CI board topics (RDR-252, bead nexus-igea9): ``board/ci/<base>``, ``<base>--done``, ``--owner``, ``--push``.

The base is ``<repo>-<branch>``, as the CI adapter names the topic and :func:`nexus.ci_wait.done_subspace` reads it:
``nexus-develop`` for Hellblazer/nexus pushing ``develop``. ``nx ci record-push`` and ``nx ci check-hold`` take it as
``--base``, or derive it here from the ``origin`` remote's URL (its last path component, without ``.git``) and the branch
being pushed. A base that cannot be derived is refused with an error that names ``--base``: it is never guessed, and never
falls back to a repo this code happens to live in.
"""
from __future__ import annotations

import re
from pathlib import Path

from nexus.bounded_subprocess import run_bounded

#: A board topic's base: what the ``board/ci/<topic>`` template accepts before ``--owner`` and ``--push``.
BASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class BaseUnderivableError(ValueError):
    """The base could not be derived; the message names the ``--base`` flag."""


def _name_from_url(url: str) -> str:
    """The repo name in a remote URL: the last path component (``https://host/owner/name.git``, ``git@host:owner/name``,
    ``ssh://git@host/owner/name/``, a local path), without ``.git``. "" when there is none."""
    tail = re.split(r"[/:\\]", url.strip().rstrip("/\\"))[-1]
    return tail.removesuffix(".git")


def derive(remote: str, branch: str | None, *, cwd: str | Path | None = None) -> str:
    """``<repo>-<branch>`` for the URL of *remote* (read with ``git remote get-url``, run in *cwd*) and *branch*. Raises
    :class:`BaseUnderivableError` for no branch, a branch that is no topic part, no such remote, or a URL with no name."""
    hint = "pass --base <repo>-<branch> (for Hellblazer/nexus pushing develop: --base nexus-develop)"
    if not branch:
        raise BaseUnderivableError(f"the target branch is unknown, so the board base cannot be derived: {hint}")
    if not BASE.fullmatch(branch):
        raise BaseUnderivableError(f"the branch {branch!r} is no board topic part, so the board base cannot be derived: {hint}")
    try:
        done = run_bounded(["git", "remote", "get-url", remote], timeout=10, cwd=cwd)
    except Exception as exc:  # noqa: BLE001 -- no git, a timeout, a spawn failure: all mean the repo name cannot be read
        raise BaseUnderivableError(f"the remote {remote!r} could not be read ({type(exc).__name__}): {hint}") from exc
    url = done.stdout.strip()
    if done.returncode != 0 or not url:
        raise BaseUnderivableError(f"there is no remote {remote!r} to read the repo name from: {hint}")
    repo = _name_from_url(url)
    if not BASE.fullmatch(repo):
        raise BaseUnderivableError(f"the repo name cannot be read from the remote URL {url!r}: {hint}")
    return f"{repo}-{branch}"
