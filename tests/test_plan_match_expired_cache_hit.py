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


def _import(library, query: str, *, created_days: float, last_used_days: float | None) -> int:
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
    )


@pytest.mark.t2_tenant
def test_expired_cache_hit_is_dropped_and_evicted(library) -> None:
    from nexus.plans.matcher import plan_match

    never_used = _import(library, "a0ksy never used", created_days=400, last_used_days=None)
    long_unused = _import(library, "a0ksy long unused", created_days=400, last_used_days=200)
    live = _import(library, "a0ksy used recently", created_days=400, last_used_days=5)
    # The engine agrees on which rows are expired, so the test rule matches its rule.
    active = {r["id"] for r in library.list_active_plans(outcome="success")}
    assert live in active and never_used not in active and long_unused not in active

    cache = _RecordingCache([(never_used, 0.01), (long_unused, 0.02), (live, 0.03)])
    matches = plan_match(intent="a0ksy probe", library=library, cache=cache, min_confidence=0.5, n=5)

    assert [m.plan_id for m in matches] == [live]
    assert sorted(cache.removed) == sorted([never_used, long_unused])
    # Nothing touched the expired rows: their last_used is what was imported.
    assert library.get_plan(never_used)["last_used"] is None
    assert library.get_plan(long_unused)["last_used"].startswith(_iso(200)[:10])


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
