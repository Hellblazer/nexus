# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The group gate of an aura's load (RDR-252 "Load by membership"; bead nexus-igea9.7 part A).

An aura whose file carries ``[load] group = "<name>"`` loads only in a session whose identity is a member of that
group and when the group is live. This module holds what that decision reads and decides, with no I/O of its own:
the record of a group as the load stage and ``nx aura check`` see it (:class:`GroupRecord`), the injected read that
fetches one (:data:`GroupReader`; ``nexus.groups`` supplies the HTTP one), and :func:`group_is_live`.

A group is live when it has a record, ``ended_at`` is unset and ``ends`` has not passed (RDR-252 "Group record":
"the stamper and the load stage treat a group as disbanded once ``ended_at`` is set or ``ends`` has passed, and treat
a missing record the same way"). A read that cannot be made raises :class:`GroupUnreadable`; the caller decides what
that means (the loader loads nothing gated, ``nx aura check`` reports Skipped), and it is never "no record".
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone


class GroupUnreadable(Exception):
    """A group, or an identity's memberships, could not be read (the engine is down, the credential is refused)."""


@dataclass(frozen=True)
class GroupRecord:
    """The fields of a group record this package reads. ``ends`` is an ISO instant or ``"open"``/``None`` for a
    group that does not end by itself; ``serves`` is the list of board topic bases whose reds the group owns."""

    name: str
    serves: tuple[str, ...] = ()
    ended_at: str | None = None
    ends: str | None = None
    leader: str | None = None


#: Reads one group's record by name: the record, ``None`` when the name has no record, or raises
#: :class:`GroupUnreadable`. One call per name; the caller does not cache across calls.
GroupReader = Callable[[str], GroupRecord | None]


def default_group_reader() -> GroupReader | None:
    """The group read the MCP server and ``nx aura check`` use, or ``None`` when this build has no group client.
    ``nexus.groups`` (RDR-252 Step 4 part B, over ``/v1/group``) plugs in here; until it does, a group-gated aura
    loads in no session (``group_unreadable``) and ``nx aura check`` reports the serving-group check Skipped."""
    return None


def _instant(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def group_is_live(record: GroupRecord | None, now: datetime) -> bool:
    """Whether *record* is a live group at *now*. A missing record is not live. An ``ends`` value that is not an
    instant (``open``, blank) never ends the group."""
    if record is None or record.ended_at:
        return False
    if record.ends:
        ends = _instant(record.ends)
        if ends is not None and ends <= now:
            return False
    return True


def group_status(record: GroupRecord | None, now: datetime) -> str:
    """Why a group is not live, as the loader and ``nx aura list`` name it: ``no_group_record``, ``group_disbanded``,
    or ``live``."""
    if record is None:
        return "no_group_record"
    return "live" if group_is_live(record, now) else "group_disbanded"
