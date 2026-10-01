# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-wbfpw.19 (RDR-192 Step 10, client half): ``nx store list --reapable``.

Read-only listing of the chunks the engine's reapable predicate selects, from
``POST /v1/vectors/reapable``. Wire-level: a real ``HttpVectorClient`` over a faked transport,
through the real CLI. The engine side is proven in ``test_wbfpw19_store_list_reapable_substrate.py``.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.http_vector_client import (
    HttpVectorClient,
    VectorServiceError,
    reset_http_vector_client_for_tests,
)

_COLL = "knowledge__nexus-1-1__voyage-context-3__v1"


def _chash(i: int) -> str:
    return f"{i:064x}"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def real_client():
    reset_http_vector_client_for_tests()
    yield HttpVectorClient()
    reset_http_vector_client_for_tests()


def _row(i: int, *, days_old: int = 40, doc: str | None = None) -> dict:
    created = (datetime.now(UTC) - timedelta(days=days_old)).isoformat().replace("+00:00", "Z")
    return {"chash": _chash(i), "created_at": created, "last_written_at": created,
            "title": f"title-{i}", "catalog_doc_id": doc}


class _Engine:
    def __init__(self, rows: list[dict], *, page: int = 300, error: Exception | None = None) -> None:
        self.rows, self.page, self.error = rows, page, error
        self.posted: list[tuple[str, dict]] = []

    def post(self, path: str, body: dict, **_kw):
        self.posted.append((path, dict(body)))
        if path != "/v1/vectors/reapable":
            raise AssertionError(f"unexpected engine route {path}: --reapable is read-only")
        if self.error is not None:
            raise self.error
        assert "offset" not in body, "keyset, never offset"
        limit = min(int(body["limit"]), self.page)
        after = body.get("after_chash")
        rows = [r for r in self.rows if after is None or r["chash"] > after][:limit]
        return {"collection": body["collection"], "grace_seconds": None, "returned": len(rows),
                "next_after": rows[-1]["chash"] if len(rows) >= limit else None, "chunks": rows}


def _invoke(runner, real_client, engine: _Engine, args: list[str]):
    with (
        patch("nexus.db.http_vector_client._post", engine.post),
        patch("nexus.db.http_vector_client._get", lambda *a, **k: []),  # an empty stats listing
        patch("nexus.commands.store.make_t3", return_value=real_client),
    ):
        return runner.invoke(main, ["store", "list", *args])


def test_reapable_without_collection_is_a_usage_error(runner, real_client):
    engine = _Engine([_row(1)])
    result = _invoke(runner, real_client, engine, ["--reapable"])
    assert result.exit_code == 2, result.output
    assert "--reapable requires --collection" in result.output
    assert engine.posted == []


def test_reapable_with_the_default_value_spelled_out_is_accepted(runner, real_client):
    """The rule is that the operator NAMED a collection, not that it differs from the default."""
    engine = _Engine([_row(1)])
    result = _invoke(runner, real_client, engine, ["--reapable", "-c", "knowledge"])
    assert result.exit_code == 0, result.output


def test_reapable_lists_chash_created_age_title_and_catalog_doc_id(runner, real_client):
    engine = _Engine([_row(1, days_old=40, doc="1.2.3"), _row(2, days_old=3, doc=None)])
    result = _invoke(runner, real_client, engine, ["--reapable", "-c", _COLL])
    assert result.exit_code == 0, result.output
    line1 = next(line for line in result.output.splitlines() if _chash(1) in line)
    assert "title-1" in line1 and "1.2.3" in line1 and "40d" in line1
    assert "T" in line1  # the created_at timestamp is printed
    line2 = next(line for line in result.output.splitlines() if _chash(2) in line)
    assert "title-2" in line2 and "3d" in line2
    # No grace is passed: the default listing is what a gc pass would take.
    assert all("grace_seconds" not in body for _p, body in engine.posted)


def test_nothing_reapable_prints_the_empty_line_and_exits_zero(runner, real_client):
    engine = _Engine([])
    result = _invoke(runner, real_client, engine, ["--reapable", "-c", _COLL])
    assert result.exit_code == 0, result.output
    assert f"0 reapable chunks in {_COLL}" in result.output


def test_the_listing_pages_by_keyset(runner, real_client):
    engine = _Engine([_row(i) for i in range(1, 6)], page=2)
    result = _invoke(runner, real_client, engine, ["--reapable", "-c", _COLL])
    assert result.exit_code == 0, result.output
    for i in range(1, 6):
        assert _chash(i) in result.output
    assert [b.get("after_chash") for _p, b in engine.posted] == [None, _chash(2), _chash(4)]


def test_limit_bounds_the_rows_shown_and_says_more_exist(runner, real_client):
    engine = _Engine([_row(i) for i in range(1, 6)])
    result = _invoke(runner, real_client, engine, ["--reapable", "-c", _COLL, "--limit", "3"])
    assert result.exit_code == 0, result.output
    assert _chash(3) in result.output and _chash(4) not in result.output
    assert "--limit" in result.output


def test_reapable_is_exclusive_with_docs_and_offset(runner, real_client):
    engine = _Engine([_row(1)])
    for extra in (["--docs"], ["--offset", "5"]):
        result = _invoke(runner, real_client, engine, ["--reapable", "-c", _COLL, *extra])
        assert result.exit_code == 2, result.output
        assert "cannot be combined" in result.output
    assert engine.posted == []


def test_an_engine_without_the_route_says_so(runner, real_client):
    engine = _Engine([], error=VectorServiceError("no route", code=404))
    result = _invoke(runner, real_client, engine, ["--reapable", "-c", _COLL])
    assert result.exit_code != 0
    assert "predates" in result.output
