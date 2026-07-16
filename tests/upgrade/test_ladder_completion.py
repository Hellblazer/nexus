# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-185 P0.2 (nexus-n7u38.2): ladder-local completion records + derived position.

The completion store is LADDER-LOCAL machine state (independent-audit HIGH
finding): its own sqlite file, raw ``sqlite3.connect``, bootstrapped with
``CREATE TABLE IF NOT EXISTS`` on open — deliberately OUTSIDE the
``T2Database`` facade and OUTSIDE ``apply_pending``, because it must exist
before (and independently of) the t2-schema rung whose completion it
records. Exempt from RDR-158 retirement; never registered in
``migration/etl_registry.py``.

Ladder position is DERIVED at read time — the max contiguous verified
prefix of the rung order (RQ6). There is no stored position and no setter:
the RDR-142 bug class (a version pointer advanced past unfinished work) is
made unrepresentable, not merely guarded.
"""
from __future__ import annotations

import pathlib
import sqlite3

import pytest

from nexus.upgrade_ladder.completion import CompletionRecord, CompletionStore

ORDER = ("t2-schema", "substrate-etl", "third-rung")


@pytest.fixture
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "ladder.db"


@pytest.fixture
def store(db_path: pathlib.Path) -> CompletionStore:
    with CompletionStore(db_path, now_fn=lambda: "2026-07-16T00:00:00+00:00") as s:
        yield s


# ── Bootstrap / substrate ────────────────────────────────────────────────────


def test_open_bootstraps_schema_and_wal(db_path: pathlib.Path) -> None:
    with CompletionStore(db_path) as store:
        assert store.ladder_position(ORDER) == 0
    conn = sqlite3.connect(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "rung_completions" in tables
    finally:
        conn.close()


def test_reopen_is_idempotent_and_durable(db_path: pathlib.Path) -> None:
    with CompletionStore(db_path, now_fn=lambda: "t0") as store:
        store.record_verified("t2-schema", package_version="6.12.0")
    # A second open must not re-create / clobber anything.
    with CompletionStore(db_path, now_fn=lambda: "t1") as store:
        assert store.verified_rungs() == frozenset({"t2-schema"})
        assert store.ladder_position(ORDER) == 1


# ── Recording ────────────────────────────────────────────────────────────────


def test_record_and_read_roundtrip(store: CompletionStore) -> None:
    store.record_verified("t2-schema", package_version="6.12.0", detail="42 steps")
    records = store.completions()
    assert records == {
        "t2-schema": CompletionRecord(
            rung_name="t2-schema",
            verified_at="2026-07-16T00:00:00+00:00",
            package_version="6.12.0",
            detail="42 steps",
        )
    }


def test_re_record_upserts_single_durable_fact(store: CompletionStore) -> None:
    """One durable verified fact per rung (Flyway-history / GitLab-Finished
    shape): re-verification replaces the row, never duplicates it."""
    store.record_verified("t2-schema", package_version="6.12.0")
    store.record_verified("t2-schema", package_version="6.13.0", detail="re-verified")
    records = store.completions()
    assert len(records) == 1
    assert records["t2-schema"].package_version == "6.13.0"
    assert records["t2-schema"].detail == "re-verified"


# ── Position derivation (RQ6: max contiguous verified prefix) ────────────────


def test_empty_store_position_is_zero(store: CompletionStore) -> None:
    assert store.ladder_position(ORDER) == 0


def test_full_prefix_position(store: CompletionStore) -> None:
    for rung in ORDER:
        store.record_verified(rung, package_version="6.12.0")
    assert store.ladder_position(ORDER) == len(ORDER)


@pytest.mark.parametrize(
    ("verified", "expected"),
    [
        (set(), 0),
        ({"t2-schema"}, 1),
        ({"t2-schema", "substrate-etl"}, 2),
        ({"substrate-etl"}, 0),                    # gap: rung 1 missing
        ({"substrate-etl", "third-rung"}, 0),      # everything but the first
        ({"t2-schema", "third-rung"}, 1),          # hole at rung 2 pins at 1
    ],
)
def test_position_is_max_contiguous_verified_prefix(
    tmp_path: pathlib.Path, verified: set[str], expected: int
) -> None:
    with CompletionStore(tmp_path / "ladder.db") as store:
        for rung in verified:
            store.record_verified(rung, package_version="6.12.0")
        assert store.ladder_position(ORDER) == expected


def test_write_order_does_not_matter(store: CompletionStore) -> None:
    """Derivation reads the table, not the write sequence: recording out of
    order (later rung first) still derives the correct contiguous prefix."""
    store.record_verified("substrate-etl", package_version="6.12.0")
    assert store.ladder_position(ORDER) == 0
    store.record_verified("t2-schema", package_version="6.12.0")
    assert store.ladder_position(ORDER) == 2


def test_rows_outside_the_order_are_ignored(store: CompletionStore) -> None:
    """Interim wrapped-verb rungs may record completions under names not in
    the canonical order — they never perturb the derived position."""
    store.record_verified("interim-wrapped-verb", package_version="6.12.0")
    assert store.ladder_position(ORDER) == 0
    store.record_verified("t2-schema", package_version="6.12.0")
    assert store.ladder_position(ORDER) == 1


# ── Position is not settable (RQ6 / RDR-142 made unrepresentable) ────────────


def test_no_position_setter_exists(store: CompletionStore) -> None:
    """The store exposes NO API that accepts a position and stores NO
    position column — the pointer cannot be advanced past verified work."""
    setter_like = [
        attr
        for attr in dir(store)
        if "position" in attr.lower() and attr != "ladder_position"
    ]
    assert setter_like == []


def test_no_position_column_in_schema(db_path: pathlib.Path) -> None:
    with CompletionStore(db_path):
        pass
    conn = sqlite3.connect(db_path)
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(rung_completions)")}
    finally:
        conn.close()
    assert columns == {"rung_name", "verified_at", "package_version", "detail"}
    assert not any("position" in c for c in columns)


# ── Transactional shape ──────────────────────────────────────────────────────


def test_record_is_one_transaction(db_path: pathlib.Path) -> None:
    """One transaction per rung record (locked contract): a record is either
    fully visible to a concurrent reader or absent — WAL gives readers a
    consistent snapshot, and a second connection sees the committed row
    immediately after record_verified returns."""
    with CompletionStore(db_path, now_fn=lambda: "t0") as writer:
        reader = sqlite3.connect(db_path)
        try:
            writer.record_verified("t2-schema", package_version="6.12.0")
            row = reader.execute(
                "SELECT rung_name, package_version FROM rung_completions"
            ).fetchall()
            assert row == [("t2-schema", "6.12.0")]
        finally:
            reader.close()


def test_two_stores_on_same_path_coexist(db_path: pathlib.Path) -> None:
    """WAL: a second store (concurrent process shape) reads what the first
    committed without either erroring."""
    with CompletionStore(db_path) as a, CompletionStore(db_path) as b:
        a.record_verified("t2-schema", package_version="6.12.0")
        assert b.verified_rungs() == frozenset({"t2-schema"})
