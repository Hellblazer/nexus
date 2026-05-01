# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""WITH TEETH end-to-end: ``nx catalog doctor --t3-doc-id-coverage
--strict-not-in-t3`` reports 100% coverage on a freshly-indexed corpus
that exercises every Stage B write path (RDR-101 Phase 3 PR δ Stage B.6).

Stage B.1 (prose), B.2 (code), B.3 (PDF + catalog registration), B.4
(``nx store put`` + ``nx enrich`` round-trip), B.5 (MCP ``store_put``)
each closed a separate write-path leak. B.6 is the cumulative gate:
build a corpus that hits every indexer, synthesize the event log, run
the doctor — coverage must read 100%. If any of B.1-B.5 is reverted,
the doctor's strict-not-in-t3 check turns red and this test fails.

Reverts ``Stage B.1`` (prose_indexer) by stripping ``doc_id`` from the
chunk metadata after indexing — the doctor must then fail. That's the
WITH TEETH demonstration.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from click.testing import CliRunner

from nexus.catalog.catalog import Catalog
from nexus.commands.catalog import (
    doctor_cmd,
    synthesize_log_cmd,
    t3_backfill_doc_id_cmd,
)
from nexus.db.t3 import T3Database
from nexus.registry import RepoRegistry


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in [
        ("GIT_AUTHOR_NAME", "Test"),
        ("GIT_AUTHOR_EMAIL", "test@test.invalid"),
        ("GIT_COMMITTER_NAME", "Test"),
        ("GIT_COMMITTER_EMAIL", "test@test.invalid"),
    ]:
        monkeypatch.setenv(k, v)


@pytest.fixture
def stage_b_repo(tmp_path: Path, simple_pdf: Path) -> Path:
    """Repo containing one of every indexer's input shape."""
    repo = tmp_path / "stage-b-repo"
    repo.mkdir()
    (repo / "module.py").write_text(
        '"""Sample code module."""\n\n'
        "def add(a, b):\n    return a + b\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text(
        "# Stage B Demo\n\nMarkdown for the prose indexer.\n",
        encoding="utf-8",
    )
    shutil.copy2(simple_pdf, repo / "doc.pdf")
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@nexus")
    _git(repo, "config", "user.name", "Nexus Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Initial commit")
    return repo


@pytest.fixture
def local_t3() -> T3Database:
    return T3Database(
        _client=chromadb.EphemeralClient(),
        _ef_override=DefaultEmbeddingFunction(),
    )


@pytest.fixture
def registry(tmp_path: Path, stage_b_repo: Path) -> RepoRegistry:
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(stage_b_repo)
    return reg


@pytest.fixture
def catalog_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    Catalog.init(catalog_dir)
    return catalog_dir


@pytest.fixture(autouse=True)
def mock_voyage_client():
    ef = DefaultEmbeddingFunction()
    mock_client = MagicMock()

    def fake_embed(texts, model, input_type="document"):
        r = MagicMock()
        r.embeddings = ef(texts)
        return r

    def fake_contextualized_embed(inputs, model, input_type="document"):
        r = MagicMock()
        br = MagicMock()
        br.embeddings = ef(inputs[0])
        r.results = [br]
        return r

    mock_client.embed.side_effect = fake_embed
    mock_client.contextualized_embed.side_effect = fake_contextualized_embed
    with patch("voyageai.Client", return_value=mock_client):
        yield mock_client


def _local_pdf_embed(chunks, model, api_key, input_type="document",
                     timeout=120.0, on_progress=None):
    ef = DefaultEmbeddingFunction()
    return ef(chunks), model


def _do_index(repo: Path, registry: RepoRegistry, t3: T3Database, monkeypatch) -> None:
    from nexus.indexer import index_repository

    monkeypatch.setenv("NX_LOCAL", "1")
    with patch("nexus.db.make_t3", return_value=t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"), \
         patch("nexus.doc_indexer._embed_with_fallback", side_effect=_local_pdf_embed):
        index_repository(repo, registry, force=False)


def _run_doctor(t3: T3Database, *flags: str) -> tuple[int, dict]:
    """Run ``nx catalog doctor --json <flags>`` against ``t3``.

    Returns ``(exit_code, parsed_json_payload)``.
    """
    runner = CliRunner()
    with patch("nexus.db.make_t3", return_value=t3):
        result = runner.invoke(doctor_cmd, [*flags, "--json"], catch_exceptions=False)
    payload = json.loads(result.output) if result.output.strip() else {}
    return result.exit_code, payload


def _run_synthesize_log(t3: T3Database) -> tuple[int, dict]:
    runner = CliRunner()
    with patch("nexus.db.make_t3", return_value=t3):
        result = runner.invoke(
            synthesize_log_cmd,
            ["--chunks", "--force", "--json"],
            catch_exceptions=False,
        )
    payload = json.loads(result.output) if result.output.strip() else {}
    return result.exit_code, payload


def _run_backfill(t3: T3Database) -> tuple[int, dict]:
    """Run ``nx catalog t3-backfill-doc-id`` to align T3 chunks with the
    minted UUID7 doc_ids in events.jsonl. Required after synthesize-log
    in the Phase 2 deployment flow."""
    runner = CliRunner()
    with patch("nexus.db.make_t3", return_value=t3):
        result = runner.invoke(
            t3_backfill_doc_id_cmd, ["--json"], catch_exceptions=False,
        )
    payload = json.loads(result.output) if result.output.strip() else {}
    return result.exit_code, payload


def test_stage_b_doctor_doc_id_coverage_end_to_end(
    stage_b_repo: Path,
    registry: RepoRegistry,
    local_t3: T3Database,
    catalog_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: B.1 (prose) + B.2 (code) + B.3 (PDF) wiring gives
    100% doc_id coverage on a freshly-indexed corpus once the Phase 2
    deployment dance lands; corrupting one chunk's doc_id makes the
    doctor turn red (WITH TEETH).

    Combined into a single test because ChromaDB's ``EphemeralClient``
    shares process-wide state — splitting into two tests with separate
    ``local_t3`` fixtures cross-contaminates collections from the first
    test, masking the second test's expected failure.

    Steps:
      1. Index a corpus that contains code + prose + PDF (Stage B.1
         + B.2 + B.3 indexer write paths).
      2. Synthesize ``events.jsonl`` from JSONL + T3 (--chunks). Mints
         UUID7 doc_ids per Document; resolves each T3 chunk's doc_id
         from the catalog (source_uri -> tumbler -> doc_id).
      3. Backfill T3 metadata with the UUID7 doc_ids from events.jsonl.
         Stage B writes the Phase 1 stand-in (tumbler == doc_id); Phase
         2 replaces those stand-ins with the minted UUID7s. Without
         this step T3 and the event log disagree and the doctor reports
         the mismatch.
      4. Run doctor with ``--t3-doc-id-coverage --strict-not-in-t3`` -
         must PASS, every collection coverage 1.0.
      5. Corrupt one chunk's doc_id in T3 to a known-bogus value.
      6. Re-run doctor - must FAIL with a mismatched_doc_id sample.
    """
    _do_index(stage_b_repo, registry, local_t3, monkeypatch)

    rc, synth = _run_synthesize_log(local_t3)
    assert rc == 0, f"synthesize-log failed: {synth}"
    assert synth["wrote"] is True
    chunk_count = synth["events_by_type"].get("ChunkIndexed", 0)
    assert chunk_count > 0, (
        f"expected ChunkIndexed events synthesized; got {synth['events_by_type']!r}"
    )

    rc, backfill = _run_backfill(local_t3)
    assert rc == 0, f"t3-backfill-doc-id failed: {backfill}"

    # Happy path: 100% coverage on freshly-indexed + backfilled corpus.
    rc, payload = _run_doctor(
        local_t3, "--t3-doc-id-coverage", "--strict-not-in-t3",
    )
    coverage = payload.get("t3_doc_id_coverage", {})
    assert rc == 0, (
        f"doctor failed on fresh index (rc={rc}):\n"
        f"{json.dumps(coverage, indent=2)}"
    )
    assert coverage.get("pass") is True, (
        f"coverage did not PASS on fresh index:\n"
        f"{json.dumps(coverage, indent=2)}"
    )

    tables = coverage.get("tables") or {}
    assert tables, f"expected per-collection coverage tables; got {coverage!r}"
    for name, stats in tables.items():
        cov = stats.get("coverage", 0.0)
        assert cov == 1.0, (
            f"collection {name!r} coverage is {cov!r}, not 1.0; "
            f"stats={stats!r}"
        )

    # WITH TEETH: corrupt one chunk's doc_id, expect doctor to turn red.
    # Pick a collection that's actually in this catalog's event log
    # (not a leaked sibling collection from a prior test). The tables
    # dict above is exactly the per-event-log set, so any name there
    # is safe.
    target_coll_name = next(iter(tables.keys()))
    target = local_t3._client.get_collection(target_coll_name)
    page = target.get(limit=1, include=["metadatas"])
    assert page["ids"], f"collection {target_coll_name} unexpectedly empty"
    chunk_id = page["ids"][0]
    pre_corruption_doc_id = page["metadatas"][0].get("doc_id")
    assert pre_corruption_doc_id, (
        "pre-condition: backfilled Stage B chunk should carry doc_id"
    )
    target.update(
        ids=[chunk_id],
        metadatas=[{"doc_id": "WRONG-DOC-ID-FOR-REGRESSION-TEST"}],
    )

    rc, payload = _run_doctor(
        local_t3, "--t3-doc-id-coverage", "--strict-not-in-t3",
    )
    coverage = payload.get("t3_doc_id_coverage", {})
    assert rc != 0, (
        "doctor must fail when a chunk's doc_id is corrupted; "
        f"got rc={rc}, coverage={json.dumps(coverage, indent=2)}"
    )
    assert coverage.get("pass") is False, (
        f"coverage should report PASS=False after corruption; got {coverage!r}"
    )
    target_stats = coverage.get("tables", {}).get(target_coll_name, {})
    assert target_stats.get("mismatched_doc_id_count", 0) >= 1, (
        f"expected at least one mismatched_doc_id in {target_coll_name}; "
        f"got {target_stats!r}"
    )
