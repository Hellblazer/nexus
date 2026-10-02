# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-wbfpw.19 (RDR-192 Step 10, client half): ``nx store list --reapable``.

Read-only listing of the chunks the engine's reapable predicate selects, from
``POST /v1/vectors/reapable``. Wire-level: a real ``HttpVectorClient`` over a faked transport,
through the real CLI. The engine side is proven in ``test_wbfpw19_store_list_reapable_substrate.py``.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import re

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


def _ts(days_old: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_old)).isoformat().replace("+00:00", "Z")


def _row(
    i: int, *, days_old: int = 40, doc: str | None = None, created_days_old: int | None = None,
    ownerless_days_old: int | None = None, with_ownerless: bool = True,
) -> dict:
    """*days_old* is the age of last_written_at; created_at is the same unless *created_days_old* says
    it is older (it is write-once, so it can only be older). ``ownerless_since`` (the engine's grace
    clock, the later of the write and the orphaning record) defaults to the write; pass
    *ownerless_days_old* (younger than the write) for a chunk that lost its owner after it was
    written, or ``with_ownerless=False`` for an engine that predates the field."""
    written = _ts(days_old)
    created = _ts(created_days_old) if created_days_old is not None else written
    row = {"chash": _chash(i), "created_at": created, "last_written_at": written,
           "title": f"title-{i}", "catalog_doc_id": doc}
    if with_ownerless:
        row["ownerless_since"] = _ts(ownerless_days_old) if ownerless_days_old is not None else written
    return row


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


def _stats(*names: str) -> list[dict]:
    return [{"name": n, "count": 10, "stored_count": 10, "lifecycle_state": "live"} for n in names]


def _invoke(runner, real_client, engine: _Engine, args: list[str], *,
            known: tuple[str, ...] = (_COLL,), catalog_knows: bool = False):
    fake_cat = MagicMock()
    fake_cat.get_collection.return_value = object() if catalog_knows else None
    with (
        patch("nexus.db.http_vector_client._post", engine.post),
        patch("nexus.db.http_vector_client._get", lambda *a, **k: _stats(*known)),
        patch("nexus.commands.store.make_t3", return_value=real_client),
        patch("nexus.catalog.factory.make_catalog_reader", return_value=fake_cat),
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
    # What the bare name resolves to depends on the box's embedding model; pin it to a collection
    # the stats listing knows so the test is about the flag, not about the resolver.
    with patch("nexus.commands.store.t3_collection_name", return_value=_COLL):
        result = _invoke(runner, real_client, engine, ["--reapable", "-c", "knowledge"])
    assert result.exit_code == 0, result.output


def test_reapable_lists_chash_ownerless_age_written_created_title_and_catalog_doc_id(runner, real_client):
    engine = _Engine([_row(1, days_old=40, doc="1.2.3"), _row(2, days_old=33, doc=None)])
    result = _invoke(runner, real_client, engine, ["--reapable", "-c", _COLL])
    assert result.exit_code == 0, result.output
    line1 = next(line for line in result.output.splitlines() if _chash(1) in line)
    assert "title-1" in line1 and "1.2.3" in line1 and "40d" in line1
    assert "T" in line1  # the timestamps are printed
    line2 = next(line for line in result.output.splitlines() if _chash(2) in line)
    assert "title-2" in line2 and "33d" in line2
    # No grace is passed: the default listing is what a gc pass would take.
    assert engine.posted and all("grace_seconds" not in body for _p, body in engine.posted)


def test_the_age_is_days_since_ownerless_since_not_since_the_write_or_creation(runner, real_client):
    """The engine's grace runs from the later of the last write and the orphaning record
    (``ownerless_since``), never from created_at (write-once, always overstates). A chunk created 400
    days ago, last written 200 days ago and orphaned 31 days ago shows 31d, the answer to "how long
    ownerless"; the write and creation stamps are printed beside it and no age is derived from them."""
    engine = _Engine([_row(1, days_old=200, created_days_old=400, ownerless_days_old=31)])
    result = _invoke(runner, real_client, engine, ["--reapable", "-c", _COLL])
    assert result.exit_code == 0, result.output
    line = next(line for line in result.output.splitlines() if _chash(1) in line)
    assert re.findall(r"\b\d+d\b", line) == ["31d"], "exactly one age is shown, and it is the ownerless age"
    assert _ts(31)[:10] in line and _ts(200)[:10] in line and _ts(400)[:10] in line, (
        "ownerless_since, last_written_at and created_at are all printed")
    assert "days ownerless" in result.output  # the header names the clock


def test_an_engine_without_ownerless_since_falls_back_to_the_write_age(runner, real_client):
    """An engine older than the field omits the key: the listing still works and the age is the write
    age (>= the grace for every listed chunk), never a crash and never a blank."""
    engine = _Engine([_row(1, days_old=35, created_days_old=400, with_ownerless=False)])
    result = _invoke(runner, real_client, engine, ["--reapable", "-c", _COLL])
    assert result.exit_code == 0, result.output
    line = next(line for line in result.output.splitlines() if _chash(1) in line)
    assert re.findall(r"\b\d+d\b", line) == ["35d"]


def test_an_unknown_collection_is_refused_not_reported_as_clean(runner, real_client):
    """The engine answers a reapable listing for ANY name with an empty 200, so a typo used to print
    "0 reapable chunks in X", the words a clean collection gets. gc refuses an unknown name; so does this."""
    engine = _Engine([])
    result = _invoke(runner, real_client, engine, ["--reapable", "-c", _COLL.replace("nexus-1-1", "typo")])
    assert result.exit_code == 1, result.output
    assert "no collection named" in result.output
    assert "0 reapable chunks" not in result.output
    assert engine.posted == [], "no listing request is made for a name nothing knows"


def test_a_catalog_registered_empty_collection_is_known_and_reads_clean(runner, real_client):
    """A registered collection with no chunks is absent from the chunk listing but is a real name."""
    engine = _Engine([])
    result = _invoke(runner, real_client, engine, ["--reapable", "-c", _COLL], known=(), catalog_knows=True)
    assert result.exit_code == 0, result.output
    assert f"0 reapable chunks in {_COLL}" in result.output


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
