# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Unit tests: _rename_collection_cascade_locked routes each migrated store
through its store's rename_collection() in service mode (RDR-152 nexus-gmiaf.16).

Design: each test patches all migrated stores on T2Database to spy objects, sets
the appropriate NX_STORAGE_BACKEND_<STORE>=service env vars, and asserts that:
  1. rename_collection was called on the correct store with (old=..., new=...)
  2. The raw SQLite UPDATE was NOT executed for that store (no row in SQLite)

We test each store in isolation AND all stores together (full service mode).
The SQLite connection is opened on a real tmp_path database with T2Database to
satisfy the owned-conn path — we do NOT pass _conn to test the full outer path
that opens its own connection.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: Minimum env required so T2Database's Http* store constructors don't raise.
#: The stores are replaced with spies immediately after construction, so
#: these values never need to point at a real service.
_SVC_ENV: dict[str, str] = {
    "NX_SERVICE_PORT": "9999",
    "NX_SERVICE_TOKEN": "test-token",
}


def _seed_db(db_path: Path) -> None:
    """Bootstrap the schema and insert one row in each store's collection table
    under the OLD collection name so a rename can find rows.
    """
    from nexus.db.t2 import T2Database
    T2Database.bootstrap_schema(db_path)

    import sqlite3
    conn = sqlite3.connect(str(db_path))  # epsilon-allow: test fixture direct SQLite setup
    conn.executescript("""
        INSERT INTO chash_index (chash, physical_collection, created_at)
            VALUES ('abc', 'code__old', '2026-01-01T00:00:00Z');
        INSERT INTO document_aspects
            (collection, source_path, problem_formulation, proposed_method,
             experimental_datasets, experimental_baselines, experimental_results,
             extracted_at, model_version, extractor_name)
            VALUES ('code__old', 'file.py', '', '', '', '', '', '2026-01-01T00:00:00Z', 'v1', 'test');
        INSERT INTO aspect_extraction_queue
            (collection, source_path, status, enqueued_at)
            VALUES ('code__old', 'file.py', 'pending', '2026-01-01T00:00:00Z');
        INSERT INTO document_highlights
            (doc_id, source_uri, collection, highlights_md, ingested_at)
            VALUES ('1.1', 'file://x', 'code__old', '## h', '2026-01-01T00:00:00Z');
        INSERT INTO search_telemetry
            (ts, query_hash, collection, raw_count, kept_count)
            VALUES ('2026-01-01T00:00:00Z', 'qhash', 'code__old', 5, 3);
        INSERT INTO topics (collection, label, centroid_hash, doc_count, terms, created_at)
            VALUES ('code__old', 'lbl', 'h1', 1, '[]', '2026-01-01T00:00:00Z');
    """)
    conn.commit()
    conn.close()


class _SpyStore:
    """Drop-in spy: records calls to rename_collection and returns a fixed count."""

    def __init__(self, return_value: Any = 1) -> None:
        self.calls: list[dict[str, str]] = []
        self._return_value = return_value

    def rename_collection(self, *, old: str, new: str) -> Any:
        self.calls.append({"old": old, "new": new})
        return self._return_value

    def close(self) -> None:
        pass


class _SpyTelemetry(_SpyStore):
    """Telemetry spy returns a dict (search_telemetry + hook_failures)."""

    def rename_collection(self, *, old: str, new: str) -> dict[str, int]:  # type: ignore[override]
        self.calls.append({"old": old, "new": new})
        return {"search_telemetry": 2, "hook_failures": 0}


# ---------------------------------------------------------------------------
# chash_index in service mode
# ---------------------------------------------------------------------------


class TestCascadeChashServiceMode:
    """chash_index routed through self.chash_index.rename_collection in service mode."""

    def test_routes_through_chash_store(self, tmp_path: Path) -> None:
        _seed_db(tmp_path / "t2.db")
        spy = _SpyStore()
        env = {**_SVC_ENV, "NX_STORAGE_BACKEND_CHASH_INDEX": "service"}

        from nexus.db.t2 import T2Database

        with patch.dict(os.environ, env):
            with T2Database(tmp_path / "t2.db") as db:
                db.chash_index = spy  # type: ignore[assignment]
                counts = db.rename_collection_cascade(old="code__old", new="code__new")

        assert spy.calls == [{"old": "code__old", "new": "code__new"}], spy.calls
        assert counts["chash"] == 1

    def test_sqlite_not_updated_in_service_mode(self, tmp_path: Path) -> None:
        """When chash is on SERVICE, the SQLite table must NOT be updated."""
        _seed_db(tmp_path / "t2.db")
        spy = _SpyStore(return_value=0)
        env = {**_SVC_ENV, "NX_STORAGE_BACKEND_CHASH_INDEX": "service"}

        from nexus.db.t2 import T2Database
        import sqlite3

        with patch.dict(os.environ, env):
            with T2Database(tmp_path / "t2.db") as db:
                db.chash_index = spy  # type: ignore[assignment]
                db.rename_collection_cascade(old="code__old", new="code__new")

        # Row still under old name in SQLite — service had control, SQLite untouched.
        conn = sqlite3.connect(str(tmp_path / "t2.db"))  # epsilon-allow: test verification read
        old_count = conn.execute(
            "SELECT COUNT(*) FROM chash_index WHERE physical_collection = ?",
            ("code__old",),
        ).fetchone()[0]
        conn.close()
        assert old_count == 1, "SQLite must not be updated when chash is on service mode"


# ---------------------------------------------------------------------------
# document_aspects in service mode
# ---------------------------------------------------------------------------


class TestCascadeDocumentAspectsServiceMode:
    """document_aspects routed through self.document_aspects.rename_collection."""

    def test_routes_through_aspects_store(self, tmp_path: Path) -> None:
        _seed_db(tmp_path / "t2.db")
        spy = _SpyStore()
        env = {**_SVC_ENV, "NX_STORAGE_BACKEND_DOCUMENT_ASPECTS": "service"}

        from nexus.db.t2 import T2Database

        with patch.dict(os.environ, env):
            with T2Database(tmp_path / "t2.db") as db:
                db.document_aspects = spy  # type: ignore[assignment]
                counts = db.rename_collection_cascade(old="code__old", new="code__new")

        assert spy.calls == [{"old": "code__old", "new": "code__new"}], spy.calls
        assert counts["aspects"] == 1

    def test_sqlite_not_updated_in_service_mode(self, tmp_path: Path) -> None:
        _seed_db(tmp_path / "t2.db")
        spy = _SpyStore(return_value=0)
        env = {**_SVC_ENV, "NX_STORAGE_BACKEND_DOCUMENT_ASPECTS": "service"}

        from nexus.db.t2 import T2Database
        import sqlite3

        with patch.dict(os.environ, env):
            with T2Database(tmp_path / "t2.db") as db:
                db.document_aspects = spy  # type: ignore[assignment]
                db.rename_collection_cascade(old="code__old", new="code__new")

        conn = sqlite3.connect(str(tmp_path / "t2.db"))  # epsilon-allow: test verification read
        old_count = conn.execute(
            "SELECT COUNT(*) FROM document_aspects WHERE collection = ?",
            ("code__old",),
        ).fetchone()[0]
        conn.close()
        assert old_count == 1, "SQLite must not be updated when document_aspects on service mode"


# ---------------------------------------------------------------------------
# aspect_queue in service mode
# ---------------------------------------------------------------------------


class TestCascadeAspectQueueServiceMode:
    """aspect_queue routed through self.aspect_queue.rename_collection."""

    def test_routes_through_queue_store(self, tmp_path: Path) -> None:
        _seed_db(tmp_path / "t2.db")
        spy = _SpyStore()
        env = {**_SVC_ENV, "NX_STORAGE_BACKEND_ASPECT_QUEUE": "service"}

        from nexus.db.t2 import T2Database

        with patch.dict(os.environ, env):
            with T2Database(tmp_path / "t2.db") as db:
                db.aspect_queue = spy  # type: ignore[assignment]
                counts = db.rename_collection_cascade(old="code__old", new="code__new")

        assert spy.calls == [{"old": "code__old", "new": "code__new"}], spy.calls
        assert counts["aspect_queue"] == 1

    def test_sqlite_not_updated_in_service_mode(self, tmp_path: Path) -> None:
        _seed_db(tmp_path / "t2.db")
        spy = _SpyStore(return_value=0)
        env = {**_SVC_ENV, "NX_STORAGE_BACKEND_ASPECT_QUEUE": "service"}

        from nexus.db.t2 import T2Database
        import sqlite3

        with patch.dict(os.environ, env):
            with T2Database(tmp_path / "t2.db") as db:
                db.aspect_queue = spy  # type: ignore[assignment]
                db.rename_collection_cascade(old="code__old", new="code__new")

        conn = sqlite3.connect(str(tmp_path / "t2.db"))  # epsilon-allow: test verification read
        old_count = conn.execute(
            "SELECT COUNT(*) FROM aspect_extraction_queue WHERE collection = ?",
            ("code__old",),
        ).fetchone()[0]
        conn.close()
        assert old_count == 1, "SQLite must not be updated when aspect_queue on service mode"


# ---------------------------------------------------------------------------
# document_highlights in service mode
# ---------------------------------------------------------------------------


class TestCascadeDocumentHighlightsServiceMode:
    """document_highlights routed through self.document_highlights.rename_collection."""

    def test_routes_through_highlights_store(self, tmp_path: Path) -> None:
        _seed_db(tmp_path / "t2.db")
        spy = _SpyStore()
        env = {**_SVC_ENV, "NX_STORAGE_BACKEND_DOCUMENT_HIGHLIGHTS": "service"}

        from nexus.db.t2 import T2Database

        with patch.dict(os.environ, env):
            with T2Database(tmp_path / "t2.db") as db:
                db.document_highlights = spy  # type: ignore[assignment]
                counts = db.rename_collection_cascade(old="code__old", new="code__new")

        assert spy.calls == [{"old": "code__old", "new": "code__new"}], spy.calls
        assert counts["highlights"] == 1

    def test_sqlite_not_updated_in_service_mode(self, tmp_path: Path) -> None:
        _seed_db(tmp_path / "t2.db")
        spy = _SpyStore(return_value=0)
        env = {**_SVC_ENV, "NX_STORAGE_BACKEND_DOCUMENT_HIGHLIGHTS": "service"}

        from nexus.db.t2 import T2Database
        import sqlite3

        with patch.dict(os.environ, env):
            with T2Database(tmp_path / "t2.db") as db:
                db.document_highlights = spy  # type: ignore[assignment]
                db.rename_collection_cascade(old="code__old", new="code__new")

        conn = sqlite3.connect(str(tmp_path / "t2.db"))  # epsilon-allow: test verification read
        old_count = conn.execute(
            "SELECT COUNT(*) FROM document_highlights WHERE collection = ?",
            ("code__old",),
        ).fetchone()[0]
        conn.close()
        assert old_count == 1, "SQLite must not be updated when document_highlights on service mode"


# ---------------------------------------------------------------------------
# telemetry in service mode
# ---------------------------------------------------------------------------


class TestCascadeTelemetryServiceMode:
    """telemetry (search_telemetry + hook_failures) routed through
    self.telemetry.rename_collection in service mode."""

    def test_routes_through_telemetry_store(self, tmp_path: Path) -> None:
        _seed_db(tmp_path / "t2.db")
        spy = _SpyTelemetry()
        env = {**_SVC_ENV, "NX_STORAGE_BACKEND_TELEMETRY": "service"}

        from nexus.db.t2 import T2Database

        with patch.dict(os.environ, env):
            with T2Database(tmp_path / "t2.db") as db:
                db.telemetry = spy  # type: ignore[assignment]
                counts = db.rename_collection_cascade(old="code__old", new="code__new")

        assert spy.calls == [{"old": "code__old", "new": "code__new"}], spy.calls
        assert counts["search_telemetry"] == 2
        assert counts["hook_failures"] == 0

    def test_sqlite_not_updated_in_service_mode(self, tmp_path: Path) -> None:
        _seed_db(tmp_path / "t2.db")
        spy = _SpyTelemetry()
        env = {**_SVC_ENV, "NX_STORAGE_BACKEND_TELEMETRY": "service"}

        from nexus.db.t2 import T2Database
        import sqlite3

        with patch.dict(os.environ, env):
            with T2Database(tmp_path / "t2.db") as db:
                db.telemetry = spy  # type: ignore[assignment]
                db.rename_collection_cascade(old="code__old", new="code__new")

        conn = sqlite3.connect(str(tmp_path / "t2.db"))  # epsilon-allow: test verification read
        old_count = conn.execute(
            "SELECT COUNT(*) FROM search_telemetry WHERE collection = ?",
            ("code__old",),
        ).fetchone()[0]
        conn.close()
        assert old_count == 1, "SQLite must not be updated when telemetry on service mode"


# ---------------------------------------------------------------------------
# Full service mode: all 7 migrated stores on SERVICE simultaneously
# ---------------------------------------------------------------------------


class TestCascadeAllStoresServiceMode:
    """With all 7 migrated stores on SERVICE, every rename_collection is called
    and no raw SQLite UPDATE touches their tables."""

    def test_all_stores_routed_in_full_service_mode(self, tmp_path: Path) -> None:
        _seed_db(tmp_path / "t2.db")

        spy_chash = _SpyStore()
        spy_aspects = _SpyStore()
        spy_queue = _SpyStore()
        spy_highlights = _SpyStore()
        spy_taxonomy = MagicMock()
        spy_taxonomy.rename_collection.return_value = {
            "topics": 1, "assignments": 0, "meta": 0
        }
        spy_telemetry = _SpyTelemetry()

        env = {
            **_SVC_ENV,
            "NX_STORAGE_BACKEND_CHASH_INDEX": "service",
            "NX_STORAGE_BACKEND_DOCUMENT_ASPECTS": "service",
            "NX_STORAGE_BACKEND_ASPECT_QUEUE": "service",
            "NX_STORAGE_BACKEND_DOCUMENT_HIGHLIGHTS": "service",
            "NX_STORAGE_BACKEND_TAXONOMY": "service",
            "NX_STORAGE_BACKEND_TELEMETRY": "service",
        }

        from nexus.db.t2 import T2Database

        with patch.dict(os.environ, env):
            with T2Database(tmp_path / "t2.db") as db:
                db.chash_index = spy_chash  # type: ignore[assignment]
                db.document_aspects = spy_aspects  # type: ignore[assignment]
                db.aspect_queue = spy_queue  # type: ignore[assignment]
                db.document_highlights = spy_highlights  # type: ignore[assignment]
                db.taxonomy = spy_taxonomy  # type: ignore[assignment]
                db.telemetry = spy_telemetry  # type: ignore[assignment]

                counts = db.rename_collection_cascade(old="code__old", new="code__new")

        expected_call = [{"old": "code__old", "new": "code__new"}]

        assert spy_chash.calls == expected_call, f"chash: {spy_chash.calls}"
        assert spy_aspects.calls == expected_call, f"aspects: {spy_aspects.calls}"
        assert spy_queue.calls == expected_call, f"queue: {spy_queue.calls}"
        assert spy_highlights.calls == expected_call, f"highlights: {spy_highlights.calls}"
        spy_taxonomy.rename_collection.assert_called_once_with("code__old", "code__new")
        assert spy_telemetry.calls == expected_call, f"telemetry: {spy_telemetry.calls}"

        # Verify counts are correctly populated from each store's return value
        assert counts["chash"] == 1
        assert counts["aspects"] == 1
        assert counts["aspect_queue"] == 1
        assert counts["highlights"] == 1
        assert counts["tax_topics"] == 1
        assert counts["search_telemetry"] == 2
        assert counts["hook_failures"] == 0

    def test_no_raw_sqlite_updates_in_full_service_mode(self, tmp_path: Path) -> None:
        """SQLite tables for all migrated stores must be untouched when all on SERVICE."""
        _seed_db(tmp_path / "t2.db")

        spy_chash = _SpyStore(return_value=0)
        spy_aspects = _SpyStore(return_value=0)
        spy_queue = _SpyStore(return_value=0)
        spy_highlights = _SpyStore(return_value=0)
        spy_taxonomy = MagicMock()
        spy_taxonomy.rename_collection.return_value = {
            "topics": 0, "assignments": 0, "meta": 0
        }
        spy_telemetry = _SpyTelemetry()

        env = {
            **_SVC_ENV,
            "NX_STORAGE_BACKEND_CHASH_INDEX": "service",
            "NX_STORAGE_BACKEND_DOCUMENT_ASPECTS": "service",
            "NX_STORAGE_BACKEND_ASPECT_QUEUE": "service",
            "NX_STORAGE_BACKEND_DOCUMENT_HIGHLIGHTS": "service",
            "NX_STORAGE_BACKEND_TAXONOMY": "service",
            "NX_STORAGE_BACKEND_TELEMETRY": "service",
        }

        from nexus.db.t2 import T2Database
        import sqlite3

        with patch.dict(os.environ, env):
            with T2Database(tmp_path / "t2.db") as db:
                db.chash_index = spy_chash  # type: ignore[assignment]
                db.document_aspects = spy_aspects  # type: ignore[assignment]
                db.aspect_queue = spy_queue  # type: ignore[assignment]
                db.document_highlights = spy_highlights  # type: ignore[assignment]
                db.taxonomy = spy_taxonomy  # type: ignore[assignment]
                db.telemetry = spy_telemetry  # type: ignore[assignment]
                db.rename_collection_cascade(old="code__old", new="code__new")

        conn = sqlite3.connect(str(tmp_path / "t2.db"))  # epsilon-allow: test verification read
        checks = {
            "chash_index":           conn.execute("SELECT COUNT(*) FROM chash_index WHERE physical_collection = ?", ("code__old",)).fetchone()[0],
            "document_aspects":      conn.execute("SELECT COUNT(*) FROM document_aspects WHERE collection = ?", ("code__old",)).fetchone()[0],
            "aspect_extraction_queue": conn.execute("SELECT COUNT(*) FROM aspect_extraction_queue WHERE collection = ?", ("code__old",)).fetchone()[0],
            "document_highlights":   conn.execute("SELECT COUNT(*) FROM document_highlights WHERE collection = ?", ("code__old",)).fetchone()[0],
            "search_telemetry":      conn.execute("SELECT COUNT(*) FROM search_telemetry WHERE collection = ?", ("code__old",)).fetchone()[0],
        }
        conn.close()

        for table, old_count in checks.items():
            assert old_count == 1, (
                f"SQLite table {table!r} must NOT be updated when store is on service mode; "
                f"old_count={old_count} (expected 1 = unchanged)"
            )
