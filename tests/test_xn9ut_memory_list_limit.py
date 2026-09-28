# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-xn9ut: /v1/memory/list takes a limit end to end.

t2_prefix_scan renders five titles per project but listed the whole project
(3,840 rows, 2.4s, against a 9s hook bound; T2 only grows). The engine now
reads only the newest ``limit`` rows, and the client asks for them. An older
engine ignores the parameter, so the client cuts the result itself: callers
get at most ``limit`` rows either way.
"""
from __future__ import annotations

import pytest

from nexus.db.t2.http_memory_store import HttpMemoryStore

pytestmark = pytest.mark.integration


def test_the_engine_returns_only_the_newest_limit_rows(t2_service_env, tmp_path, monkeypatch):
    from nexus.db.t2 import T2Database

    store = T2Database(tmp_path / "t2.db").memory
    for i in range(5):
        store.put(project="xn9ut-limit", title=f"xn9ut-{i}", content="c")

    sent: list[dict] = []
    real_get = store._get

    def _spy(path, params=None, **kw):
        sent.append(dict(params or {}))
        return real_get(path, params=params, **kw)

    monkeypatch.setattr(store, "_get", _spy)
    rows = store.list_entries(project="xn9ut-limit", limit=2)

    assert [r["title"] for r in rows] == ["xn9ut-4", "xn9ut-3"]
    assert sent[-1].get("limit") == "2", "the bound must reach the engine"
    assert len(store.list_entries(project="xn9ut-limit")) == 5


def test_an_engine_that_ignores_limit_still_yields_at_most_limit_rows(monkeypatch):
    store = HttpMemoryStore.__new__(HttpMemoryStore)
    old_engine_rows = [
        {"id": i, "project": "p", "title": f"t{i}", "agent": None, "timestamp": "2026-09-28T00:00:00Z"}
        for i in range(6)
    ]
    monkeypatch.setattr(store, "_get", lambda path, params=None, **kw: old_engine_rows)

    rows = store.list_entries(project="p", limit=3)

    assert [r["title"] for r in rows] == ["t0", "t1", "t2"]


def test_a_non_positive_limit_is_refused_before_any_request():
    store = HttpMemoryStore.__new__(HttpMemoryStore)
    with pytest.raises(ValueError):
        store.list_entries(project="p", limit=0)
