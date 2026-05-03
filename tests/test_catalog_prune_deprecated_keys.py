# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the RDR-101 Phase 4 ``nx catalog prune-deprecated-keys`` verb.

Coverage:
- Refuses without ``--i-have-completed-the-reader-migration``.
- Refuses on doc_id coverage gap (``--skip-coverage-check`` overrides).
- Drops the 8 deprecated keys, preserves ``title`` + all other keys.
- Idempotent: a second run on already-pruned chunks is a no-op.
- ``--collection`` filter scopes the prune.
- ``--dry-run`` reports without writing.
- ``--json`` emits a structured report.
"""
from __future__ import annotations

import json
from pathlib import Path

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from click.testing import CliRunner

from nexus.catalog import events as ev
from nexus.catalog.catalog import Catalog
from nexus.catalog.event_log import EventLog
from nexus.commands.catalog import (
    _PRUNE_DEPRECATED_KEYS,
    prune_deprecated_keys_cmd,
)


@pytest.fixture
def isolated_nexus(tmp_path: Path) -> Path:
    return tmp_path / "test-catalog"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def chroma_client():
    client = chromadb.EphemeralClient()
    for col in list(client.list_collections()):
        try:
            client.delete_collection(col.name)
        except Exception:
            pass
    return client


def _seed(client, name: str, chunks: list[dict]) -> None:
    col = client.get_or_create_collection(
        name=name, embedding_function=DefaultEmbeddingFunction(),
    )
    col.add(
        ids=[c["id"] for c in chunks],
        documents=[c["content"] for c in chunks],
        metadatas=[c["metadata"] for c in chunks],
    )


def _seed_event_log(catalog_dir: Path, events: list[ev.Event]) -> None:
    catalog_dir.mkdir(parents=True, exist_ok=True)
    Catalog.init(catalog_dir)
    log = EventLog(catalog_dir)
    log.append_many(events)


def _chunk_event(
    chunk_id: str, doc_id: str, coll_id: str,
    *, synthesized_orphan: bool = False,
) -> ev.Event:
    return ev.Event(
        type=ev.TYPE_CHUNK_INDEXED, v=0,
        payload=ev.ChunkIndexedPayload(
            chunk_id=chunk_id, chash="h", doc_id=doc_id,
            coll_id=coll_id, position=0,
            synthesized_orphan=synthesized_orphan,
        ),
        ts="2026-04-30T00:00:00Z",
    )


def _full_meta(doc_id: str = "ART-x") -> dict:
    """Pre-prune chunk metadata: 8 deprecated keys + title + a few survivors."""
    return {
        # The 5 RDR-101 Phase 4 keys this verb drops.
        "source_path": "/abs/path/file.py",
        "git_branch": "main",
        "git_commit_hash": "deadbeef",
        "git_project_name": "nexus",
        "git_remote_url": "https://github.com/Hellblazer/nexus.git",
        # Phase 5c additionally removed these from ALLOWED_TOP_LEVEL.
        # Pre-5c collections retain them until pruned.
        "corpus": "code",
        "store_type": "code",
        "git_meta": '{"branch":"main","commit":"deadbeef"}',
        # Permanently kept (per the .10.2 audit, Category C).
        "title": "file.py:1-10",
        # Other survivors.
        "doc_id": doc_id,
        "chunk_text_hash": "abc",
        "indexed_at": "2026-04-30T00:00:00Z",
        "chunk_index": 0,
    }


# ── Pre-flight gates ────────────────────────────────────────────────────────


class TestReaderMigrationGate:
    def test_refuses_without_reader_migration_flag(
        self, isolated_nexus, runner,
    ):
        """nexus-o6aa.10.3: pruning before the readers migrate produces
        silent empty results across aspect extraction, link boost,
        incremental sync, and display formatters. The verb refuses to
        run unless the operator acknowledges the migration is complete.
        """
        Catalog.init(isolated_nexus)
        result = runner.invoke(prune_deprecated_keys_cmd, ["--dry-run"])
        assert result.exit_code != 0
        assert "reader migration must be complete" in result.output.lower()
        assert "rdr-101-phase4-reader-audit.md" in result.output

    def test_missing_catalog_errors_before_gate(
        self, isolated_nexus, runner,
    ):
        result = runner.invoke(
            prune_deprecated_keys_cmd,
            ["--i-have-completed-the-reader-migration", "--dry-run"],
        )
        assert result.exit_code != 0
        assert "not initialized" in result.output.lower()


class TestCoverageGate:
    def test_refuses_on_coverage_gap(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """When the doctor's t3-doc-id-coverage reports < 100% on a
        target collection, the prune verb refuses to run rather than
        leave the missing-doc_id chunks strictly unreachable.
        """
        # Event log claims a chunk; T3 has chunks but one lacks doc_id
        # so coverage drops below 100%.
        events = [_chunk_event("ch1", "ART-A", "code__test")]
        _seed_event_log(isolated_nexus, events)
        meta_no_doc_id = {k: v for k, v in _full_meta().items() if k != "doc_id"}
        _seed(chroma_client, "code__test", [
            {"id": "ch1", "content": "x", "metadata": meta_no_doc_id},
        ])

        class _FakeT3:
            _client = chroma_client

            def list_collections(self_inner):
                return [{"name": "code__test"}]

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())

        result = runner.invoke(
            prune_deprecated_keys_cmd,
            ["--i-have-completed-the-reader-migration"],
        )
        assert result.exit_code != 0
        assert "refusing to prune" in result.output.lower()
        assert "covered" in result.output.lower()

    def test_skip_coverage_check_proceeds(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """``--skip-coverage-check`` bypasses the gate. The prune
        proceeds even when chunks lack doc_id; the operator has
        decided what to do about the orphan class post-prune.
        """
        Catalog.init(isolated_nexus)
        _seed(chroma_client, "code__test", [
            {"id": "ch1", "content": "x", "metadata": _full_meta()},
        ])

        class _FakeT3:
            _client = chroma_client

            def list_collections(self_inner):
                return [{"name": "code__test"}]

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())

        result = runner.invoke(
            prune_deprecated_keys_cmd,
            [
                "--i-have-completed-the-reader-migration",
                "--skip-coverage-check",
                "--collection", "code__test",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["chunks_updated"] == 1


# ── Prune behaviour ─────────────────────────────────────────────────────────


class TestPruneBehaviour:
    def test_drops_eight_keys_preserves_title_and_others(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """WITH TEETH: the post-prune chunk has none of the 8 deprecated
        keys (5 from RDR-101 Phase 4 + 3 from Phase 5c), but ``title``
        and every other key are preserved verbatim. A regression that
        drops ``title`` (per the original 6-key design, rejected by the
        .10.2 audit) fails here.
        """
        Catalog.init(isolated_nexus)
        _seed(chroma_client, "code__test", [
            {"id": "ch1", "content": "x", "metadata": _full_meta("ART-A")},
        ])

        class _FakeT3:
            _client = chroma_client

            def list_collections(self_inner):
                return [{"name": "code__test"}]

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())

        result = runner.invoke(
            prune_deprecated_keys_cmd,
            [
                "--i-have-completed-the-reader-migration",
                "--skip-coverage-check",
                "--collection", "code__test",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output

        col = chroma_client.get_collection("code__test")
        meta = col.get(ids=["ch1"], include=["metadatas"])["metadatas"][0]
        # All 8 deprecated keys gone.
        for k in _PRUNE_DEPRECATED_KEYS:
            assert k not in meta, (
                f"deprecated key {k!r} still present after prune"
            )
        # Title kept (audit Category C: load-bearing).
        assert meta["title"] == "file.py:1-10"
        # Every other survivor kept.
        assert meta["doc_id"] == "ART-A"
        assert meta["chunk_text_hash"] == "abc"
        assert meta["indexed_at"] == "2026-04-30T00:00:00Z"
        assert meta["chunk_index"] == 0

    def test_idempotent_second_run_is_noop(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Re-running the verb on already-pruned chunks reports them
        as ``already_pruned`` and writes nothing.
        """
        Catalog.init(isolated_nexus)
        _seed(chroma_client, "code__test", [
            {"id": "ch1", "content": "x", "metadata": _full_meta()},
        ])

        class _FakeT3:
            _client = chroma_client

            def list_collections(self_inner):
                return [{"name": "code__test"}]

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())

        first = runner.invoke(
            prune_deprecated_keys_cmd,
            [
                "--i-have-completed-the-reader-migration",
                "--skip-coverage-check",
                "--collection", "code__test",
                "--json",
            ],
        )
        assert first.exit_code == 0
        first_payload = json.loads(first.output)
        assert first_payload["chunks_updated"] == 1
        assert first_payload["chunks_already_pruned"] == 0

        second = runner.invoke(
            prune_deprecated_keys_cmd,
            [
                "--i-have-completed-the-reader-migration",
                "--skip-coverage-check",
                "--collection", "code__test",
                "--json",
            ],
        )
        assert second.exit_code == 0
        second_payload = json.loads(second.output)
        assert second_payload["chunks_updated"] == 0
        assert second_payload["chunks_already_pruned"] == 1

    def test_dry_run_reports_without_writing(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        Catalog.init(isolated_nexus)
        _seed(chroma_client, "code__test", [
            {"id": "ch1", "content": "x", "metadata": _full_meta()},
        ])

        class _FakeT3:
            _client = chroma_client

            def list_collections(self_inner):
                return [{"name": "code__test"}]

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())

        result = runner.invoke(
            prune_deprecated_keys_cmd,
            [
                "--i-have-completed-the-reader-migration",
                "--skip-coverage-check",
                "--collection", "code__test",
                "--dry-run", "--json",
            ],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["dry_run"] is True
        assert payload["chunks_updated"] == 1

        # No actual update: keys still present.
        col = chroma_client.get_collection("code__test")
        meta = col.get(ids=["ch1"], include=["metadatas"])["metadatas"][0]
        assert "source_path" in meta


class TestCollectionFilter:
    def test_filter_scopes_prune(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        Catalog.init(isolated_nexus)
        _seed(chroma_client, "code__a", [
            {"id": "ch-a", "content": "x", "metadata": _full_meta()},
        ])
        _seed(chroma_client, "code__b", [
            {"id": "ch-b", "content": "y", "metadata": _full_meta()},
        ])

        class _FakeT3:
            _client = chroma_client

            def list_collections(self_inner):
                return [{"name": "code__a"}, {"name": "code__b"}]

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())

        result = runner.invoke(
            prune_deprecated_keys_cmd,
            [
                "--i-have-completed-the-reader-migration",
                "--skip-coverage-check",
                "--collection", "code__a",
                "--json",
            ],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["chunks_updated"] == 1
        assert payload["collection_filter"] == "code__a"

        # code__a pruned; code__b untouched.
        col_a = chroma_client.get_collection("code__a")
        meta_a = col_a.get(ids=["ch-a"], include=["metadatas"])["metadatas"][0]
        assert "source_path" not in meta_a

        col_b = chroma_client.get_collection("code__b")
        meta_b = col_b.get(ids=["ch-b"], include=["metadatas"])["metadatas"][0]
        assert "source_path" in meta_b


class TestKeyConstants:
    def test_deprecated_keys_match_audit(self):
        """Lock the 8-key set: 5 from the .10.2 audit (Category B,
        RDR-101 Phase 4) + 3 dropped from ALLOWED_TOP_LEVEL by
        Phase 5c (corpus, store_type, git_meta). Title is
        intentionally absent (Category C).
        """
        assert _PRUNE_DEPRECATED_KEYS == frozenset({
            # RDR-101 Phase 4 (.10.2 audit, Category B).
            "source_path",
            "git_branch",
            "git_commit_hash",
            "git_project_name",
            "git_remote_url",
            # RDR-101 Phase 5c (nexus-o6aa.13).
            "corpus",
            "store_type",
            "git_meta",
        })
        assert "title" not in _PRUNE_DEPRECATED_KEYS, (
            "title is load-bearing (slug-shaped knowledge identity + "
            "universal display field); audit Category C keeps it."
        )
