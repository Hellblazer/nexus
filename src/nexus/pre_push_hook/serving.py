# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which board bases the local identity's groups serve, and whether a repository is one of them (RDR-252, nexus-igea9).

``nx upgrade`` installs the managed pre-push hook only where a live group the local identity belongs to serves the repo's
base, because the hook can refuse a push. A read that fails (no identity, an engine that is down) installs nothing: the
answer is ``None``, never an empty set that would read as "serves nothing".
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

from nexus import ci_base
from nexus.ci_wait import BOARD_BRANCHES

__all__ = ["Verdict", "decide", "inside_repo", "read_served", "repo_bases", "served_bases"]


def read_served(identity: str | None = None, client: Any = None) -> tuple[frozenset[str] | None, str]:
    """``(bases, why)``: the union of ``serves`` over every live group *identity* belongs to, or ``(None, <why not>)`` when it
    cannot be read; the reason is empty for a process with no identity (it belongs to no group, so there is nothing to tell).

    *identity* defaults to the one this process holds (:func:`nexus.ci_ack.counted_identity`); an empty one belongs to no
    group, so there is nothing to read. *client* defaults to ``GroupsClient.from_config()``, closed here; a client passed in is
    the caller's to close. The reason is one clause ``nx upgrade`` shows when it installs nothing because of it."""
    owned = client is None
    try:
        if identity is None:
            from nexus import ci_ack  # noqa: PLC0415 -- deferred: leaf import cost

            identity = ci_ack.counted_identity()
        if not identity:
            return None, ""   # no identity belongs to no group: nothing to read, and nothing worth telling the user
        if client is None:
            from nexus.groups import GroupsClient  # noqa: PLC0415 -- deferred: the HTTP client is not needed to import this

            client = GroupsClient.from_config()
        from nexus.auras.load_gate import group_is_live  # noqa: PLC0415 -- deferred: leaf import cost

        now = datetime.now(timezone.utc)
        bases: set[str] = set()
        for row in client.memberships(identity):
            record = client.record(row["group"])
            if record is not None and group_is_live(record, now):
                bases.update(record.serves)
        return frozenset(bases), ""
    except Exception as exc:  # noqa: BLE001 -- an unreadable answer installs nothing; it is never "serves nothing"
        return None, f"the groups could not be read ({type(exc).__name__}: {exc})"
    finally:
        if owned and client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001, S110 -- closing a client that failed to open
                pass


def served_bases(identity: str | None = None, client: Any = None) -> frozenset[str] | None:
    """:func:`read_served` without the reason."""
    return read_served(identity, client)[0]


def repo_bases(repo: Path, remote: str = "origin") -> frozenset[str]:
    """The board bases of *repo*: ``<repo>-<branch>`` for each branch the CI adapter watches, from *remote*'s URL. Empty when
    the remote or its repo name cannot be read (the base is never guessed)."""
    out: set[str] = set()
    for branch in sorted(BOARD_BRANCHES):
        try:
            out.add(ci_base.derive(remote, branch, cwd=repo))
        except ci_base.BaseUnderivableError:
            return frozenset()
    return frozenset(out)


class Verdict(NamedTuple):
    """Whether ``nx upgrade`` installs the pre-push hook in a repo, and the line that says why not when that is worth saying."""

    install: bool
    note: str = ""


def inside_repo(hooks_dir: Path, repo: Path) -> bool:
    """Whether *hooks_dir* sits inside *repo*'s work tree or its git common dir: a hooks directory anywhere else (a global
    ``core.hooksPath``) is shared with other repositories, and a stanza put there runs in all of them."""
    try:
        here = hooks_dir.resolve()
        roots = [repo.resolve()]
        from nexus._git_hooks_meta import git_common_dir  # noqa: PLC0415 -- deferred: leaf import

        roots.append(git_common_dir(repo).resolve())
    except (OSError, RuntimeError):
        return False
    return any(here == r or r in here.parents for r in roots)


def decide(repo: Path, hooks_dir: Path, read: Callable[[], tuple[frozenset[str] | None, str]]) -> Verdict:
    """The install decision for *repo* (nexus-igea9): its origin names a repo whose base a live group the local identity belongs
    to serves (*read* answers :func:`read_served`, called at most once per sweep by the caller's cache), the hooks directory
    is the repo's own, and the groups could be read at all. A repo no group serves, and a repo with no origin to derive a base
    from, are the normal case and say nothing; the other refusals say why."""
    bases = repo_bases(repo)
    if not bases:
        return Verdict(False)
    served, why = read()
    if served is None:
        return Verdict(False, f"pre-push hook not installed in any repo: {why}" if why else "")
    if not served & bases:
        return Verdict(False)
    if not inside_repo(hooks_dir, repo):
        return Verdict(False, f"pre-push hook not installed in {repo}: its hooks directory {hooks_dir} is shared with other "
                              f"repositories (core.hooksPath); `nx hooks install --pre-push {repo}` installs it there on purpose")
    return Verdict(True)
