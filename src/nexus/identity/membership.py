# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Group memberships of an identity (RDR-253 "Memberships and push records"; Step 6, nexus-hczdo.1.9).

A membership is a row of the engine's ``identity_memberships`` table, written through
``/v1/identity/<id>/memberships``: one per (identity, group), with a role (``leader`` or ``member``) and the time the
identity first joined, which a later role change keeps. It lasts as long as the identity does, so RDR-252 (a draft)
can key its groups on it. A tuple could not hold it (every tuple expires), which is why this is a table and not the
``member/<id>`` tuples the bead text first named.

Writes are **fenced**: the engine checks that the caller's identity token is the identity's own and its generation is
current, so a seat can join and leave groups only as itself. :meth:`IdentityClient.join_group` and ``leave_group``
attach the caller's token; a call from a process that holds none, or one that holds another identity's, is refused by
the engine (``403 identity_token_required`` / ``403 not_owner``). Reads are open.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

ROLES: tuple[str, ...] = ("leader", "member")


@dataclass(frozen=True)
class Membership:
    identity: str
    group: str
    role: str
    joined_at: str | None = None

    @classmethod
    def of(cls, row: Mapping[str, Any]) -> "Membership":
        return cls(str(row["identity"]), str(row["group"]), str(row["role"]), row.get("joined_at"))


def join(client: Any, identity: str, group: str, role: str = "member") -> Membership:
    """Join *group* as *identity*, or change the role of a membership that exists (the join time stays)."""
    return Membership.of(client.join_group(identity, group, role))


def leave(client: Any, identity: str, group: str) -> bool:
    """Leave *group*; True when the identity was a member."""
    return bool(client.leave_group(identity, group).get("removed"))


def groups(client: Any, identity: str) -> list[Membership]:
    """The groups *identity* belongs to, in join order."""
    return [Membership.of({"identity": identity, **row}) for row in client.memberships(identity)]


def roster(client: Any, group: str) -> list[Membership]:
    """The members of *group*, in join order."""
    return [Membership.of({"group": group, **row}) for row in client.roster(group)]
