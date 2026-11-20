# SPDX-License-Identifier: AGPL-3.0-or-later
"""The group gate follows the engine's ``live`` verdict (nexus-igea9.3 L3). Tenant-free; split from test_load.py, which is at its
size cap."""
from __future__ import annotations

def test_the_engines_live_verdict_is_never_overridden_by_the_local_clock_or_an_unparseable_ends() -> None:
    """igea9.3 L3: the loader recomputed liveness from the local clock and dropped the engine's ``live``, so an ``ends`` the
    client cannot parse read as never ending. A record the engine says is not live is not live; a record with no verdict
    (a hand-built one) keeps the local rule."""
    from datetime import datetime, timezone  # noqa: PLC0415

    from nexus.auras.load_gate import GroupRecord, group_is_live, group_reader_from, group_status  # noqa: PLC0415
    from nexus.groups import GroupRecord as WireRecord  # noqa: PLC0415

    now = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
    assert not group_is_live(GroupRecord("g", ends="garbage", live=False), now)
    assert group_status(GroupRecord("g", live=False), now) == "group_disbanded"
    assert group_is_live(GroupRecord("g", ends="garbage"), now), "no verdict: the local rule"
    assert not group_is_live(GroupRecord("g", ends="2026-11-19T11:00:00Z", live=True), now), "the local clock may still end it"

    class _Client:
        def record(self, name: str) -> WireRecord:
            return WireRecord.from_wire({"name": name, "leader": "i1", "live": False, "ends": "soon", "serves": []})

    mapped = group_reader_from(_Client())("g")
    assert mapped is not None and mapped.live is False, "the reader carries the engine's verdict through"
    assert not group_is_live(mapped, now)
