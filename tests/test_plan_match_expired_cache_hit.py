# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-a0ksy: a T1 cache hit on an expired plan is dropped, not run.

The T1 plan cache refreshes from ``list_active_plans`` only every 90 s and
``get_plan`` applies no TTL, so a plan that expired inside that window came
back from ``plan_match``. Running it would stamp ``last_used = now()`` at run
start and make the expired plan active again. These tests drive the real
matcher over the real engine with backdated rows imported through
``/v1/plans/import``.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

T0 = datetime.now(UTC)
_TTL_DAYS = 30


def _iso(delta_days: float) -> str:
    return (T0 - timedelta(days=delta_days)).strftime("%Y-%m-%dT%H:%M:%SZ")


class _RecordingCache:
    """Cache stand-in that returns scripted hits and records evictions."""

    def __init__(self, hits: list[tuple[int, float]]) -> None:
        self._hits = hits
        self.removed: list[int] = []

    @property
    def is_available(self) -> bool:
        return True

    def query(self, intent: str, n: int) -> list[tuple[int, float]]:
        return self._hits[:n]

    def remove(self, plan_id: int) -> None:
        self.removed.append(plan_id)


@pytest.fixture()
def library(tmp_path: Path):
    from nexus.db.t2 import T2Database

    return T2Database(tmp_path / "m.db").plans


def _import(
    library, query: str, *, created_days: float, last_used_days: float | None, disabled: bool = False,
) -> int:
    return library.import_plan(
        project="nexus",
        query=query,
        plan_json=json.dumps({"steps": []}),
        outcome="success",
        tags="",
        scope="global",
        ttl=_TTL_DAYS,
        verb="research",
        match_text=query,
        created_at=_iso(created_days),
        last_used=None if last_used_days is None else _iso(last_used_days),
        disabled_at=_iso(0) if disabled else None,
    )


@pytest.mark.t2_tenant
def test_inactive_cache_hits_are_dropped_evicted_and_not_resurrected(library) -> None:
    from nexus.plans.matcher import _is_expired, plan_match

    never_used = _import(library, "a0ksy never used", created_days=400, last_used_days=None)
    long_unused = _import(library, "a0ksy long unused", created_days=400, last_used_days=200)
    just_past = _import(library, "a0ksy just past ttl", created_days=400, last_used_days=_TTL_DAYS + 0.5)
    just_inside = _import(library, "a0ksy just inside ttl", created_days=400, last_used_days=_TTL_DAYS - 0.5)
    disabled = _import(library, "a0ksy disabled", created_days=1, last_used_days=None, disabled=True)
    live = _import(library, "a0ksy used recently", created_days=400, last_used_days=5)
    inactive = [never_used, long_unused, just_past, disabled]

    # The engine is the oracle: the client rule must agree with list_active_plans on every row,
    # the half-day either side of the boundary included.
    active = {r["id"] for r in library.list_active_plans(outcome="success")}
    assert active >= {live, just_inside} and not active & set(inactive), active
    for plan_id in (never_used, long_unused, just_past, just_inside, live):
        assert _is_expired(library.get_plan(plan_id)) is (plan_id not in active), plan_id
    before = {plan_id: library.get_plan(plan_id)["last_used"] for plan_id in inactive}

    hits = [(plan_id, 0.01 * (i + 1)) for i, plan_id in enumerate([*inactive, just_inside, live])]
    cache = _RecordingCache(hits)
    # The intent repeats an expired plan's own question, so the verbatim-repeat override would
    # force it through if the drop came after it.
    matches = plan_match(intent="a0ksy never used", library=library, cache=cache, min_confidence=0.5, n=10)

    assert {m.plan_id for m in matches} == {just_inside, live}
    assert sorted(cache.removed) == sorted(inactive)
    # Run every match the way nx_answer does at run start. Only matched plans are touched, so the
    # inactive rows keep their last_used and stay out of the active set: nothing resurrected them.
    for m in matches:
        library.increment_run_started(m.plan_id)
    active_after = {r["id"] for r in library.list_active_plans(outcome="success")}
    assert not active_after & set(inactive), active_after
    assert {plan_id: library.get_plan(plan_id)["last_used"] for plan_id in inactive} == before

    # The engine backstop (nexus-i1eb4): even a run start on an expired row, the case the drop above
    # keeps from happening, counts the run but leaves last_used alone, so the row stays expired.
    stamp = library.get_plan(just_past)["last_used"]
    library.increment_run_started(just_past)
    assert just_past not in {r["id"] for r in library.list_active_plans(outcome="success")}
    assert library.get_plan(just_past)["last_used"] == stamp


def test_permanent_plan_never_expires() -> None:
    from nexus.plans.matcher import _is_expired

    assert not _is_expired({"ttl": None, "created_at": _iso(4000), "last_used": None})


@pytest.mark.parametrize(
    ("last_used_days", "created_days", "expired"),
    [
        (None, 31, True),
        (None, 29, False),
        (31, 400, True),
        (29, 400, False),
    ],
)
def test_is_expired_follows_the_engine_rule(last_used_days, created_days, expired) -> None:
    from nexus.plans.matcher import _is_expired

    row = {
        "ttl": _TTL_DAYS,
        "created_at": _iso(created_days),
        "last_used": None if last_used_days is None else _iso(last_used_days),
    }
    assert _is_expired(row, now=T0) is expired


def test_unreadable_timestamp_keeps_the_row() -> None:
    from nexus.plans.matcher import _is_expired

    assert not _is_expired({"ttl": _TTL_DAYS, "created_at": "not a date", "last_used": None}, now=T0)


def test_unreadable_last_used_is_still_the_anchor() -> None:
    from nexus.plans.matcher import _is_expired

    # The engine's coalesce picks last_used whenever it is non-null; a 400-day-old created_at must
    # not take over and expire the row.
    assert not _is_expired({"ttl": _TTL_DAYS, "created_at": _iso(400), "last_used": "garbage"}, now=T0)


@pytest.mark.parametrize("stamp", ["+00:00", "+0000", ""])
def test_offset_and_naive_timestamps_read_as_utc(stamp) -> None:
    from nexus.plans.matcher import _is_expired

    base = (T0 - timedelta(days=_TTL_DAYS + 1)).strftime("%Y-%m-%dT%H:%M:%S")
    assert _is_expired({"ttl": _TTL_DAYS, "created_at": base + stamp, "last_used": None}, now=T0)
