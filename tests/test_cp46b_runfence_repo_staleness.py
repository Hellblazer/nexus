# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-cp46b: the repo-path freshness/skip decision must consult the
RUNFENCE ``index_state``, not just content-hash + embedding_model.

Live-verified 2026-08-17: after the bhlfy root-cause fix, a normal
``nx index repo`` run (rc=0, clean) skipped every stranded-'indexing' doc
as "index fresh" — the doc_indexer.py single-doc path already checks the
fence (``_index_run_fresh``, nexus-5xn3k.3), but ``indexer_utils.
check_staleness`` — the gate ``code_indexer.py`` / ``prose_indexer.py``
actually use for a repo-path run — never consulted ``index_state`` at
all, so a stranded doc whose content is unchanged could NEVER drain via
a normal run; only ``--force`` (a full-repo re-embed) cleared it.

Fixtures mirror ``tests/db/test_bhlfy_runfence_fail_arms.py``: real engine
catalog via the suite-wide substrate, fake local T3 for chunk content,
``seed_manifest_chunks`` to satisfy ``fk_catalog_chunks_chunk`` on every
write that is allowed to land.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction
from nexus.db.t3 import T3Database
from nexus.registry import RepoRegistry
from tests._catalog_fixture_ops import active_reader
from tests.conftest import fake_credentials, make_vector_test_client


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@nexus")
    _git(repo, "config", "user.name", "Nexus Test")


def _commit(repo: Path) -> None:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "seed")


def _entry_ending_with(suffix: str):
    matches = [
        e for e in active_reader().all_documents()
        if str(getattr(e, "file_path", "")).endswith(suffix)
    ]
    assert len(matches) == 1, f"expected exactly one document ending with {suffix!r}, got {matches}"
    return matches[0]


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
def local_t3() -> T3Database:
    return T3Database(
        _client=make_vector_test_client(),
        _ef_override=DefaultEmbeddingFunction(),
    )


@pytest.fixture
def catalog_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    return catalog_dir


@pytest.fixture(autouse=True)
def mock_voyage_client():
    """Local-mode test: voyageai client is never called, but
    `voyageai.Client` may still be constructed by the orchestrator."""
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


def _index(repo: Path, t3: T3Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the real ``index_repository`` pipeline (a NORMAL run:
    ``force=False``); every write succeeds and is FK-seeded (mirrors
    test_bhlfy_runfence_fail_arms.py's ``_index``)."""
    from nexus.indexer import index_repository
    from tests._catalog_fixture_ops import seed_manifest_chunks

    monkeypatch.setenv("NX_LOCAL", "1")
    reg = RepoRegistry(repo.parent / "repos.json")
    reg.add(repo)

    orig_write_batch = t3._write_batch

    def _seeding_write_batch(col, collection_name, ids, documents, metadatas,
                              embeddings=None, **kwargs):
        orig_write_batch(col, collection_name, ids, documents, metadatas,
                          embeddings, **kwargs)
        seed_manifest_chunks(collection_name, ids)

    monkeypatch.setattr(t3, "_write_batch", _seeding_write_batch)

    with patch("nexus.db.make_t3", return_value=t3), \
         patch("nexus.config.get_credential", side_effect=fake_credentials()):
        index_repository(repo, reg, force=False)


def _wrap_fence_helpers(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Patch ``nexus.doc_indexer._fence_begin`` (module-level deferred-
    import patch target every producer resolves at call time) to record
    call order while still calling through to the real implementation."""
    import nexus.doc_indexer as doc_indexer_mod

    call_order: list[tuple[str, str]] = []
    real_begin = doc_indexer_mod._fence_begin

    def _wrapped_begin(doc_id: str, content_hash: str, collection: str) -> None:
        call_order.append(("begin", doc_id))
        real_begin(doc_id, content_hash, collection)

    monkeypatch.setattr(doc_indexer_mod, "_fence_begin", _wrapped_begin)
    return call_order


def _strand_at(state_setter, doc_id: str, content_hash: str, collection: str) -> None:
    """Re-fence *doc_id* to simulate a run that began but never finished
    (the process died before ``_fence_complete``/``_fence_fail`` ran) —
    the exact tjf7l stranding shape. Content hash is left UNCHANGED, so
    the only thing distinguishing a stranded doc from a healthy one is
    ``index_state``."""
    from nexus.catalog.factory import make_catalog_writer

    w = make_catalog_writer()
    state_setter(w, doc_id, content_hash, collection)


def _prime_owner(repo: Path, t3: T3Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """Index a throwaway file first so the catalog OWNER already exists
    before the test's real file is ever indexed.

    nexus-cp46b test-fixture note: the catalog-minted physical collection
    name for a repo's docs__ corpus depends on whether the catalog owner
    is registered yet (``_repo_collection_or_legacy`` prefers ``cat.
    collection_for_repo`` once an owner exists; falls back to a path-
    derived synth name otherwise) — a genuinely separate, pre-existing
    quirk (unrelated to this bead) where a repo's very first-ever index
    run resolves a DIFFERENT physical collection than every run after it.
    Without priming, a test's first ``_index()`` call writes chunks to
    one collection and its second call resolves and reads a DIFFERENT,
    empty one — the staleness cache never has a chance to hit regardless
    of ``index_state``, silently making any two-call staleness assertion
    vacuous. Priming reaches the steady state every real repo settles
    into after its first run, before the calls this test asserts on."""
    (repo / "prime.md").write_text(
        "# Prime\n\nOwner-registration bootstrap.\n", encoding="utf-8",
    )
    _commit(repo)
    _index(repo, t3, monkeypatch)


class TestStrandedFenceReprocessedByNormalRun:
    """nexus-cp46b: 'indexing'/'failed' must be re-processed by a NORMAL
    (non---force) run even when content is unchanged."""

    def test_indexing_state_doc_reprocessed_by_normal_run(
        self, tmp_path: Path, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        _prime_owner(repo, local_t3, monkeypatch)
        (repo / "stranded.md").write_text(
            "# Stranded\n\nContent that never changes.\n", encoding="utf-8",
        )
        _commit(repo)

        _index(repo, local_t3, monkeypatch)
        entry = _entry_ending_with("stranded.md")
        assert entry.index_state == "complete"
        doc_id = str(entry.tumbler)
        content_hash = entry.index_content_hash
        collection = entry.physical_collection

        _strand_at(
            lambda w, d, c, col: w.begin_index_run(d, c, "cp46b-stranding-run", col),
            doc_id, content_hash, collection,
        )
        stranded = _entry_ending_with("stranded.md")
        assert stranded.index_state == "indexing"

        # A NORMAL run (force=False, content unchanged) must clear the fence.
        _index(repo, local_t3, monkeypatch)

        healed = _entry_ending_with("stranded.md")
        assert healed.index_state == "complete", (
            "nexus-cp46b: a normal `nx index repo` pass must re-process a "
            "doc stranded in index_state='indexing' even when its content "
            "is unchanged (pre-fix: stayed 'indexing' forever, only "
            "--force cleared it)"
        )

    def test_failed_state_doc_reprocessed_by_normal_run(
        self, tmp_path: Path, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        _prime_owner(repo, local_t3, monkeypatch)
        (repo / "failed.md").write_text(
            "# Failed\n\nAlso never changes.\n", encoding="utf-8",
        )
        _commit(repo)

        _index(repo, local_t3, monkeypatch)
        entry = _entry_ending_with("failed.md")
        doc_id = str(entry.tumbler)

        from nexus.catalog.factory import make_catalog_writer
        w = make_catalog_writer()
        w.fail_index_run(doc_id, "cp46b: simulated upload failure")

        stranded = _entry_ending_with("failed.md")
        assert stranded.index_state == "failed"

        _index(repo, local_t3, monkeypatch)

        healed = _entry_ending_with("failed.md")
        assert healed.index_state == "complete", (
            "nexus-cp46b: a normal `nx index repo` pass must re-process a "
            "doc fenced 'failed' even when its content is unchanged"
        )

    def test_unchanged_complete_doc_still_skipped(
        self, tmp_path: Path, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Control / non-regression: an unchanged doc whose fence is
        already 'complete' must still skip — the hot path this fix must
        not slow down."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _prime_owner(repo, local_t3, monkeypatch)

        (repo / "healthy.md").write_text(
            "# Healthy\n\nStable content.\n", encoding="utf-8",
        )
        _commit(repo)
        _index(repo, local_t3, monkeypatch)
        entry = _entry_ending_with("healthy.md")
        assert entry.index_state == "complete"

        call_order = _wrap_fence_helpers(monkeypatch)
        _index(repo, local_t3, monkeypatch)

        assert not [c for c in call_order if c[0] == "begin"], (
            "an unchanged doc whose fence is already 'complete' must NOT "
            f"be re-embedded on a normal run: {call_order}"
        )
