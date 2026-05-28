# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-137 followup CRITICAL-3, CRITICAL-4, CRITICAL-5 (epic nexus-43qgm).

CRITICAL-3: _CatalogBackedRegistry.update passes model_version="1"
instead of "v1". Per parse_conformant_collection_name (corpus.py:125)
every other caller derives "v1". Second --corpus knowledge invocation
fails the idempotency check in register_collection, emits a duplicate
CollectionCreated event, overwrites model_version "v1" to "1".

CRITICAL-4: _migrate_repos_json_to_catalog deletes a malformed
repos.json silently because _read_repos_json catches JSONDecodeError
and returns {}. Migration sees zero entries, vacuously declares
parity, unlinks the file.

CRITICAL-5: ensure_owner_for_repo is check-then-act; two concurrent
calls for the same repo_hash both pass the lookup, both allocate
distinct prefixes, both INSERT. Schema lacks UNIQUE(repo_hash) so
duplicate owner rows persist.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
from pathlib import Path

import pytest
import structlog
from structlog.testing import capture_logs

from nexus.catalog.catalog import Catalog
from nexus.commands.index import _CatalogBackedRegistry
from nexus.repos import _read_repos_json


@pytest.fixture(autouse=True)
def _enable_debug_logging():
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
    )
    yield
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
    )


@pytest.fixture
def cat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Catalog:
    cfg = tmp_path / "config"
    cat_dir = cfg / "catalog"
    cat_dir.mkdir(parents=True)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(cat_dir))
    Catalog.init(cat_dir)
    return Catalog(cat_dir, cat_dir / ".catalog.db")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "myrepo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    return r


class TestCriticalThreeModelVersionV1:
    def test_corpus_knowledge_register_uses_v1_form(
        self, cat: Catalog, repo: Path, tmp_path: Path,
    ) -> None:
        """The adapter's update(docs_collection=...) must register the
        collection with model_version='v1' (conformant), not '1'.
        Pre-fix the second --corpus knowledge invocation would replace
        the stored 'v1' with '1' via a second CollectionCreated event."""
        adapter = _CatalogBackedRegistry(
            cat=cat, registry_path=tmp_path / "repos.json",
        )
        adapter.add(repo)
        adapter.update(
            repo,
            docs_collection="knowledge__myrepo-1-1__voyage-context-3__v1",
        )

        row = cat._db.execute(
            "SELECT model_version FROM collections "
            "WHERE name = 'knowledge__myrepo-1-1__voyage-context-3__v1'"
        ).fetchone()
        assert row is not None
        assert row[0] == "v1", (
            f"Expected conformant 'v1' form; saw {row[0]!r}. "
            "Indicates the model_version='1' bug regressed."
        )

    def test_idempotent_corpus_knowledge_doesnt_drift_model_version(
        self, cat: Catalog, repo: Path, tmp_path: Path,
    ) -> None:
        """Repeat --corpus knowledge invocations must NOT drift the
        stored model_version. Pre-fix the second call emitted a new
        CollectionCreated event overwriting the row's model_version."""
        adapter = _CatalogBackedRegistry(
            cat=cat, registry_path=tmp_path / "repos.json",
        )
        adapter.add(repo)
        col_name = "knowledge__myrepo-1-1__voyage-context-3__v1"
        # First registration via the indexer's catalog hook would set
        # model_version='v1'. Simulate that:
        cat.register_collection(
            col_name, content_type="knowledge", owner_id="myrepo-1-1",
            embedding_model="voyage-context-3", model_version="v1",
        )

        # Now the operator runs `nx index repo . --corpus knowledge`
        # — the adapter's update fires:
        adapter.update(repo, docs_collection=col_name)

        # model_version must still be 'v1'.
        row = cat._db.execute(
            "SELECT model_version FROM collections WHERE name = ?",
            (col_name,),
        ).fetchone()
        assert row is not None
        assert row[0] == "v1"


class TestCriticalFourMalformedReposJsonNotDeleted:
    def test_malformed_repos_json_kept_with_warning(
        self, tmp_path: Path,
    ) -> None:
        """A truncated/malformed repos.json must NOT be silently
        deleted by the migration verb. Pre-fix _read_repos_json
        returned {} on JSONDecodeError, vacuously satisfying the
        parity check, then unlink() ran."""
        from nexus.commands.upgrade import _migrate_repos_json_to_catalog

        cfg = tmp_path / "config"
        cfg.mkdir()
        cat_dir = cfg / "catalog"
        cat_dir.mkdir()
        Catalog.init(cat_dir)

        import os
        os.environ["NEXUS_CONFIG_DIR"] = str(cfg)
        os.environ["NEXUS_CATALOG_PATH"] = str(cat_dir)
        try:
            # Truncated JSON — recoverable but malformed.
            reg_path = cfg / "repos.json"
            reg_path.write_text('{"repos": {"a": {"name": "a"}}, ')

            with capture_logs() as cap:
                _migrate_repos_json_to_catalog(dry_run=False)

            # File MUST still exist.
            assert reg_path.exists(), (
                "Malformed repos.json was deleted — data loss."
            )
            # Warning event MUST fire.
            assert any(
                e.get("event") == "repos_json_malformed"
                for e in cap
            ), f"Expected repos_json_malformed warning; saw events: {[e.get('event') for e in cap]}"
        finally:
            os.environ.pop("NEXUS_CONFIG_DIR", None)
            os.environ.pop("NEXUS_CATALOG_PATH", None)

    def test_read_repos_json_returns_distinct_sentinel_for_malformed(
        self, tmp_path: Path,
    ) -> None:
        """_read_repos_json must let callers distinguish 'absent'
        (return {}) from 'malformed' (warn + raise OR return None).
        Either contract is fine; the current 'return {} silently'
        contract conflates the two and is the proximate cause of
        CRITICAL-4 data loss."""
        from nexus.repos import _read_repos_json

        # Absent → {}
        absent_result = _read_repos_json(tmp_path / "nope.json")
        assert absent_result == {}

        # Malformed → either raises or returns a non-{} sentinel.
        # The migration verb must be able to tell these apart.
        malformed = tmp_path / "bad.json"
        malformed.write_text("{ not json")
        with capture_logs() as cap:
            try:
                result = _read_repos_json(malformed)
                # If we tolerate the silent path, the warning event
                # is mandatory.
                assert any(
                    e.get("event") == "repos_json_malformed"
                    for e in cap
                )
            except (json.JSONDecodeError, ValueError):
                # Or raise — also acceptable.
                pass


class TestCriticalFiveTOCTOUOwnerRace:
    def test_concurrent_ensure_owner_for_repo_no_duplicate_rows(
        self, cat: Catalog, repo: Path,
    ) -> None:
        """Two threads racing ensure_owner_for_repo on the same
        repo_hash must produce exactly ONE owner row (same tumbler
        for both). Pre-fix the check-then-act window allowed both
        threads to allocate distinct prefixes and INSERT both."""
        results: list[str] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def _run():
            try:
                barrier.wait(timeout=5.0)
                owner = cat.ensure_owner_for_repo(repo)
                results.append(str(owner))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=_run)
        t2 = threading.Thread(target=_run)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"Unexpected errors: {errors}"
        assert len(results) == 2
        # Both threads must receive the SAME tumbler.
        assert results[0] == results[1], (
            f"TOCTOU race created distinct owners: {results}"
        )

        # Catalog must have exactly ONE owner row for this repo_hash.
        from nexus.repo_identity import _repo_identity
        _, repo_hash = _repo_identity(repo)
        rows = cat._db.execute(
            "SELECT tumbler_prefix FROM owners WHERE repo_hash = ?",
            (repo_hash,),
        ).fetchall()
        assert len(rows) == 1, (
            f"Expected 1 owner row for repo_hash={repo_hash!r}; "
            f"saw {len(rows)}: {rows}. TOCTOU race created duplicates."
        )

    def test_register_owner_rechecks_repo_hash_inside_lock(
        self, cat: Catalog, repo: Path,
    ) -> None:
        """register_owner re-checks repo_hash inside its locked
        critical section and returns the existing owner instead of
        allocating a new prefix. This is the cross-process fix path:
        the directory flock serializes processes, then the re-check
        returns the winner's tumbler. (Threads can't easily simulate
        the flock-serialized cross-process case in a unit test, so we
        exercise the synchronous re-check directly — a second
        register_owner call for the same repo_hash must NOT mint a
        second prefix.)

        The projector uses INSERT OR REPLACE which does NOT raise
        IntegrityError on the UNIQUE(repo_hash) conflict, so the
        re-check — not the index error path — is the load-bearing
        guarantee.
        """
        from nexus.repo_identity import _repo_identity
        _, repo_hash = _repo_identity(repo)

        first = cat.register_owner(
            "myrepo", "repo", repo_hash=repo_hash, repo_root=str(repo),
        )
        # Direct second register_owner with the SAME repo_hash — the
        # in-lock re-check must short-circuit and return `first`.
        second = cat.register_owner(
            "myrepo", "repo", repo_hash=repo_hash, repo_root=str(repo),
        )
        assert str(first) == str(second)

        rows = cat._db.execute(
            "SELECT tumbler_prefix FROM owners WHERE repo_hash = ?",
            (repo_hash,),
        ).fetchall()
        assert len(rows) == 1

    def test_curator_owners_exempt_from_repo_hash_recheck(
        self, cat: Catalog,
    ) -> None:
        """Curator owners (no repo_hash) are exempt from the re-check;
        two curator registrations with distinct names produce distinct
        owners (the composite UNIQUE(name, owner_type) still applies)."""
        a = cat.register_owner("papers-a", "curator")
        b = cat.register_owner("papers-b", "curator")
        assert str(a) != str(b)
