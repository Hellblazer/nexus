# SPDX-License-Identifier: AGPL-3.0-or-later
"""rename_collection_cascade Phase-0 encapsulation tests (RDR-112 P0.3 / nexus-btrw).

The cascade has always been atomic via a dedicated transaction (K4 /
nexus-nhyh). Phase 0 hardens the encapsulation: no production caller
opens its own ``sqlite3.Connection`` to feed the ``_conn`` test-seam,
and a non-``_conn`` route still exercises rollback so the seam's
removal in a hypothetical Phase 6 audit cannot mask the regression.
"""

from __future__ import annotations

import inspect
import sqlite3
import threading
from pathlib import Path

import pytest

from nexus.db.t2 import T2Database


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    """Seed all six tables that the cascade touches so the rename has work."""
    db_path = tmp_path / "memory.db"
    with T2Database(db_path) as t2:
        t2.chash_index.upsert(chash="aa", collection="code__old")
        t2.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, centroid_hash, doc_count, terms, created_at) "
            "VALUES ('T', 'code__old', 'h1', 1, '[]', '2026-05-13T00:00:00Z')"
        )
        t2.taxonomy.conn.execute(
            "INSERT INTO taxonomy_meta (collection, last_discover_at) "
            "VALUES ('code__old', '2026-05-13T00:00:00Z')"
        )
        t2.taxonomy.conn.commit()
    return db_path


# -- Encapsulation contract --------------------------------------------------


def test_rename_cascade_runs_without_external_conn(seeded_db: Path) -> None:
    """Default call path: no ``_conn`` argument; method opens its own."""
    with T2Database(seeded_db) as t2:
        counts = t2.rename_collection_cascade(old="code__old", new="code__new")
    assert counts["chash"] == 1
    assert counts["tax_topics"] == 1
    assert counts["tax_meta"] == 1


def test_conn_parameter_is_keyword_only_and_underscore_prefixed() -> None:
    """The seam stays awkward to reach for: keyword-only + underscore prefix."""
    sig = inspect.signature(T2Database.rename_collection_cascade)
    param = sig.parameters.get("_conn")
    assert param is not None
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


def test_no_production_caller_outside_whitelist() -> None:
    """Whitelist guard: production code referring to ``rename_collection_cascade``
    is limited to the impl module and one orchestrator.

    Any new ``src/`` path here should be reviewed for whether it slipped
    a ``_conn`` injection into production code.
    """
    repo = Path(__file__).resolve().parent.parent / "src"
    callers = sorted(
        str(p.relative_to(repo.parent))
        for p in repo.rglob("*.py")
        if "rename_collection_cascade" in p.read_text(encoding="utf-8", errors="ignore")
    )
    assert callers == [
        "src/nexus/collection_rename.py",
        # RDR-112 P1.2 (nexus-qy0u): daemon exposes rename_collection_cascade
        # as a top-level RPC ("database.rename_collection_cascade"). Neither
        # of these paths passes ``_conn`` — the daemon calls with only keyword
        # args (old=, new=) and lets the impl open its own connection.
        "src/nexus/daemon/t2_client.py",
        "src/nexus/daemon/t2_daemon.py",
        "src/nexus/db/t2/__init__.py",
    ]


# -- Atomicity (non-_conn route) ---------------------------------------------


def test_atomic_rollback_without_conn_seam(
    seeded_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomicity holds when the cascade opens its own connection.

    Replaces ``sqlite3.connect`` inside the impl module so the cascade's
    self-opened connection is a wrapper that raises mid-cascade. No
    ``_conn`` argument is supplied — this is the production code path.
    """
    real_connect = sqlite3.connect

    class _BombingConnection:
        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real
            self._fail_armed = False

        def execute(self, sql: str, params=(), **kw):
            stripped = sql.strip()
            # Arm-on-BEGIN is needed because this test patches
            # ``sqlite3.connect`` AFTER ``T2Database`` is built. Any
            # remaining setup/cleanup queries the impl runs before
            # opening a fresh cascade connection therefore hit this
            # wrapper too; arming only after the cascade's own BEGIN
            # ensures the bomb fires inside the transaction so rollback
            # is the failure mode under test. (The companion
            # ``test_collection_rename.py`` seam test does NOT need
            # this guard because it injects via ``_conn`` rather than
            # monkeypatching the connector.)
            if stripped.upper() == "BEGIN":
                self._fail_armed = True
                return self._real.execute(sql, params, **kw)
            if (
                self._fail_armed
                and "document_aspects" in stripped
                and stripped.upper().startswith("UPDATE")
            ):
                raise RuntimeError("simulated mid-cascade failure")
            return self._real.execute(sql, params, **kw)

        def rollback(self):
            return self._real.rollback()

        def commit(self):
            return self._real.commit()

        def close(self):
            return self._real.close()

    def _spawn_bombing(path: str, *args, **kwargs):
        return _BombingConnection(real_connect(path, *args, **kwargs))

    # Build the T2Database BEFORE patching so the domain stores' own
    # connections (which need real schema setup) escape the bomb. The
    # patch then catches the dedicated connection that
    # ``rename_collection_cascade`` opens itself.
    import nexus.db.t2 as t2_mod
    with T2Database(seeded_db) as t2:
        monkeypatch.setattr(t2_mod.sqlite3, "connect", _spawn_bombing)
        with pytest.raises(RuntimeError, match="mid-cascade"):
            t2.rename_collection_cascade(old="code__old", new="code__new")

    # Verify every table still carries the OLD name — rollback succeeded.
    raw = real_connect(str(seeded_db))
    try:
        chash_rows = raw.execute(
            "SELECT physical_collection FROM chash_index WHERE chash='aa'"
        ).fetchone()
        topic_rows = raw.execute(
            "SELECT collection FROM topics WHERE label='T'"
        ).fetchone()
        meta_rows = raw.execute(
            "SELECT collection FROM taxonomy_meta"
        ).fetchone()
    finally:
        raw.close()

    assert chash_rows[0] == "code__old"
    assert topic_rows[0] == "code__old"
    assert meta_rows[0] == "code__old"


# -- Concurrency-friendly smoke ---------------------------------------------


def test_rename_cascade_owned_conn_closes_after_success(seeded_db: Path) -> None:
    """The owned connection must be closed when the rename succeeds.

    Smoke: two consecutive cascades in the same process must not leak a
    connection that holds a write lock against the next call. Reading
    via ``sqlite3.connect`` immediately after must succeed without
    SQLITE_BUSY.
    """
    with T2Database(seeded_db) as t2:
        t2.rename_collection_cascade(old="code__old", new="code__mid")
        t2.rename_collection_cascade(old="code__mid", new="code__new")

    # Independent connection must be able to read without contention.
    raw = sqlite3.connect(str(seeded_db), timeout=0.5)
    try:
        row = raw.execute(
            "SELECT physical_collection FROM chash_index WHERE chash='aa'"
        ).fetchone()
    finally:
        raw.close()
    assert row[0] == "code__new"
