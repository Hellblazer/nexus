# SPDX-License-Identifier: AGPL-3.0-or-later
"""The base of a branch's CI board topics (RDR-252, bead nexus-igea9): ``board/ci/<base>``, ``<base>--done``, ``--owner``, ``--push``.

The base is ``<repo>-<branch>``, as the CI adapter names the topic (its ``topic_for`` lower-cases the repo name, so a remote
``ART.git`` pushing ``main`` is the board ``art-main``) and as
:func:`nexus.ci_wait.done_subspace` reads it: ``nexus-develop`` for Hellblazer/nexus pushing ``develop``. Every ``nx ci``
command that names a board from the repo it runs in (``ack``, ``wait``, ``record-push``, ``check-hold``) reads the ``origin``
remote's URL through the one parser here (:func:`slug_from_url`, :func:`repo_from_url`), so no two of them can disagree about
which repo a URL names. ``record-push`` and ``check-hold`` take the base as ``--base``, or derive it with :func:`derive`. A
base that cannot be derived is refused with an error that names ``--base``: it is never guessed, and never falls back to a
repo this code happens to live in.
"""
from __future__ import annotations

import re
from pathlib import Path

from nexus.bounded_subprocess import run_bounded

#: A board topic's base: what the ``board/ci/<topic>`` template accepts before ``--owner`` and ``--push``.
BASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
#: The last two path segments of a remote URL: owner and repo, ``.git`` and a trailing slash dropped. Works for
#: ``git@host:owner/repo.git``, ``https://host/owner/repo`` (credentials allowed) and ``ssh://`` forms.
_SLUG = re.compile(r"[/:]([^/:@\s]+)/([^/:@\s]+?)(?:\.git)?/?$")


class BaseUnderivableError(ValueError):
    """The base could not be derived; the message names the ``--base`` flag."""


def slug_from_url(url: str) -> tuple[str, str] | None:
    """The ``(owner, repo)`` a remote URL ends in, as written (case kept, for the GitHub API). None for a URL with no usable
    pair."""
    match = _SLUG.search(url.strip())
    if match is None or any(BASE.fullmatch(g) is None for g in match.groups()):
        return None
    return match.group(1), match.group(2)


def repo_from_url(url: str) -> str | None:
    """The repository name a remote URL ends in, lower-cased as the CI adapter writes it into a board topic
    (``board/ci/<repo>-<branch>--...``, the repo part of ``nx ci wait --repo``). None for a URL with no usable name."""
    slug = slug_from_url(url)
    return slug[1].lower() if slug else None


def branch_of(base: str) -> str:
    """The branch a board *base* names: the part after its last hyphen (``develop`` for ``nexus-develop``, ``main`` for
    ``recording-rig-main``), or the whole base when it has none. A base is ``<repo>-<branch>`` and the two parts are not
    delimited, so a branch that itself holds a hyphen reads as its last word; the branches the CI adapter posts for are
    ``develop``, ``main`` and ``master``."""
    return base.rsplit("-", 1)[-1]


def remote_url(remote: str = "origin", *, cwd: str | Path | None = None) -> str:
    """The URL of *remote* in the repository *cwd* (default: the current directory) is in, or "" when there is none, no git,
    or no repository."""
    try:
        done = run_bounded(["git", "remote", "get-url", remote], timeout=10, cwd=cwd)
    except Exception:  # noqa: BLE001 -- no git, a timeout, a missing directory: all mean there is no URL to read
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def derive(remote: str, branch: str | None, *, cwd: str | Path | None = None) -> str:
    """``<repo>-<branch>`` for the URL of *remote* (run in *cwd*) and *branch*. Raises :class:`BaseUnderivableError` for no
    branch, a branch that is no topic part, no such remote, or a URL with no repo name."""
    hint = "pass --base <repo>-<branch> (for Hellblazer/nexus pushing develop: --base nexus-develop)"
    if not branch:
        raise BaseUnderivableError(f"the target branch is unknown, so the board base cannot be derived: {hint}")
    if not BASE.fullmatch(branch):
        raise BaseUnderivableError(f"the branch {branch!r} is no board topic part, so the board base cannot be derived: {hint}")
    url = remote_url(remote, cwd=cwd)
    if not url:
        raise BaseUnderivableError(f"there is no remote {remote!r} to read the repo name from: {hint}")
    repo = repo_from_url(url)
    if repo is None:
        raise BaseUnderivableError(f"the repo name cannot be read from the remote URL {url!r}: {hint}")
    return f"{repo}-{branch}"


def default_branch(remote: str = "origin", *, cwd: str | Path | None = None) -> str | None:
    """The branch ``nx ci wait`` waits on when it is not given: ``develop`` when ``refs/remotes/<remote>/develop`` exists, else
    the branch ``refs/remotes/<remote>/HEAD`` points at, else None (the caller refuses, naming ``--branch``)."""
    try:
        have = run_bounded(["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/develop"], timeout=10, cwd=cwd)
        if have.returncode == 0:
            return "develop"
        head = run_bounded(["git", "symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD"], timeout=10, cwd=cwd)
    except Exception:  # noqa: BLE001 -- no git or no repository: there is no default
        return None
    prefix = f"refs/remotes/{remote}/"
    target = head.stdout.strip()
    if head.returncode != 0 or not target.startswith(prefix):
        return None
    branch = target[len(prefix):]
    return branch if BASE.fullmatch(branch) else None
