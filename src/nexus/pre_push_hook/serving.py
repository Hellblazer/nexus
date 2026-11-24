# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which board bases the local identity's groups serve, and whether a repository is one of them (RDR-252, nexus-igea9).

``nx upgrade`` installs the managed pre-push hook only where a live group the local identity belongs to serves the repo's
base, because the hook can refuse a push. A read that fails (no identity, an engine that is down) installs nothing: the
answer is ``None``, never an empty set that would read as "serves nothing".
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nexus import ci_base
from nexus.ci_wait import BOARD_BRANCHES

__all__ = ["repo_bases", "served_bases"]


def served_bases(identity: str | None = None, client: Any = None) -> frozenset[str] | None:
    """The union of ``serves`` over every live group *identity* belongs to, or None when it cannot be read.

    *identity* defaults to the one this process holds (:func:`nexus.ci_ack.counted_identity`); an empty one belongs to no
    group, so there is nothing to read and the answer is None. *client* defaults to ``GroupsClient.from_config()``, closed
    here; a client passed in is the caller's to close."""
    owned = client is None
    try:
        if identity is None:
            from nexus import ci_ack  # noqa: PLC0415 -- deferred: leaf import cost

            identity = ci_ack.counted_identity()
        if not identity:
            return None
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
        return frozenset(bases)
    except Exception:  # noqa: BLE001 -- an unreadable answer installs nothing; it is never "serves nothing"
        return None
    finally:
        if owned and client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001, S110 -- closing a client that failed to open
                pass


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
