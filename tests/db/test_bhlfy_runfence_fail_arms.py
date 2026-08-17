# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-bhlfy: the four missing ``_fence_fail`` arms.

T1 scratch investigation (entry 18a25792, tagged tjf7l/debug/runfence)
found 14 documents stranded in ``index_state='indexing'`` forever: a
failed upload calls ``_fence_begin``/``_fence_begin_many`` but no code
path ever calls the paired ``_fence_fail``, so the fence never resolves
past 'indexing' — direct log evidence traced 12 of them to one real
``nx index repo`` run that hit a 504 Gateway Timeout.

Four producers were missing the fail arm:

1. The ``nx index repo`` ChunkBatcher hot path — ``indexer.py``'s
   ``_batched_file_failed`` (wired as ``ChunkBatcher``'s
   ``on_file_failed``) only logged; it never called ``_fence_fail``.
   ChunkBatcher already fires this callback exactly once per
   PERMANENTLY-failed file (bisect-on-failure retries each half of a
   failed multi-file flush independently down to file granularity — see
   ``chunk_batcher.py``'s own unit tests), so no new ChunkBatcher
   callback was needed — this deviates from an earlier sketch that
   proposed a brand-new ``on_batch_failed`` callback; ``on_file_failed``
   already exists and already fires at the right granularity.
2. ``indexer.py``'s PDF legacy per-file fallback (reached when
   ChunkBatcher rejects the file or is absent).
3. ``prose_indexer.py``'s legacy per-file fallback.
4. ``code_indexer.py``'s legacy per-file fallback.

All three legacy-fallback fixes mirror ``commands/store.py``'s
nexus-cotmr precedent verbatim: wrap the upload in try/except, stamp
'failed' unconditionally on the exception path (``_fence_fail`` never
raises), then re-raise so the original exception is never masked.

This file drives producers #2-4 (the legacy per-file fallback, reached
unconditionally with a non-``HttpVectorClient`` T3 — ``ChunkBatcher`` is
gated on ``isinstance(db, HttpVectorClient)``, see ``indexer.py``).
Producer #1 (the ChunkBatcher hot path, service-mode only) and the
no-double-report bisect scenario are covered in
``tests/db/test_vw594_runfence_coverage.py`` alongside its existing
batcher-path fence tests.

Fixtures mirror ``tests/test_prose_indexer_doc_id.py`` /
``tests/test_code_indexer_doc_id.py`` (real engine catalog via the
suite-wide substrate, fake local T3 for chunk content, ``seed_manifest_
chunks`` to satisfy ``fk_catalog_chunks_chunk`` on every write that is
allowed to land).
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
    """Run the real ``index_repository`` pipeline; every write succeeds
    and is FK-seeded (mirrors test_prose_indexer_doc_id.py's ``_do_index``)."""
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


def _index_with_injected_failure(
    repo: Path, t3: T3Database, monkeypatch: pytest.MonkeyPatch, poison_marker: str,
) -> None:
    """Run ``index_repository``; ``t3._write_batch`` raises for exactly
    the write whose chunk text contains *poison_marker*, propagating the
    original exception (matching the real upload-failure shape the fence
    fail arm must handle). Non-poisoned writes proceed and are FK-seeded
    normally, so a co-indexed healthy file is unaffected."""
    from nexus.indexer import index_repository
    from tests._catalog_fixture_ops import seed_manifest_chunks

    monkeypatch.setenv("NX_LOCAL", "1")
    reg = RepoRegistry(repo.parent / "repos.json")
    reg.add(repo)

    orig_write_batch = t3._write_batch

    def _selective_write_batch(col, collection_name, ids, documents, metadatas,
                                embeddings=None, **kwargs):
        if any(poison_marker in d for d in documents):
            raise RuntimeError(
                f"nexus-bhlfy kill-control: injected upload failure ({poison_marker})"
            )
        orig_write_batch(col, collection_name, ids, documents, metadatas,
                          embeddings, **kwargs)
        seed_manifest_chunks(collection_name, ids)

    monkeypatch.setattr(t3, "_write_batch", _selective_write_batch)

    with patch("nexus.db.make_t3", return_value=t3), \
         patch("nexus.config.get_credential", side_effect=fake_credentials()):
        index_repository(repo, reg, force=False)


def _wrap_fence_helpers(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Patch ``nexus.doc_indexer._fence_begin``/``_fence_fail`` (the
    module-level deferred-import patch targets every producer resolves
    at call time — same convention ``test_cotmr_cli_store_fence.py``
    uses) to record call order while still calling through to the real
    implementation, so the real engine catalog is genuinely stamped."""
    import nexus.doc_indexer as doc_indexer_mod

    call_order: list[tuple[str, str]] = []
    real_begin = doc_indexer_mod._fence_begin
    real_fail = doc_indexer_mod._fence_fail

    def _wrapped_begin(doc_id: str, content_hash: str, collection: str) -> None:
        call_order.append(("begin", doc_id))
        real_begin(doc_id, content_hash, collection)

    def _wrapped_fail(doc_id: str, error: str) -> None:
        call_order.append(("fail", doc_id))
        real_fail(doc_id, error)

    monkeypatch.setattr(doc_indexer_mod, "_fence_begin", _wrapped_begin)
    monkeypatch.setattr(doc_indexer_mod, "_fence_fail", _wrapped_fail)
    return call_order


def _assert_begin_before_fail(call_order: list[tuple[str, str]], doc_id: str) -> None:
    begins = [i for i, c in enumerate(call_order) if c == ("begin", doc_id)]
    fails = [i for i, c in enumerate(call_order) if c == ("fail", doc_id)]
    assert len(begins) == 1, f"expected exactly one begin for {doc_id}, got {call_order}"
    assert len(fails) == 1, f"expected exactly one fail for {doc_id}, got {call_order}"
    assert begins[0] < fails[0], (
        f"begin must fire before fail for {doc_id}, got order {call_order}"
    )


# ── prose_indexer.py legacy fallback (producer #6 in the vw594 table) ──────


class TestProseIndexerLegacyFallbackFenceFail:
    def test_success_never_calls_fence_fail(
        self, tmp_path: Path, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        call_order = _wrap_fence_helpers(monkeypatch)

        repo = tmp_path / "prose-good"
        _init_repo(repo)
        (repo / "good.md").write_text(
            "# Good\n\nHealthy prose content.\n", encoding="utf-8",
        )
        _commit(repo)

        _index(repo, local_t3, monkeypatch)

        assert not [c for c in call_order if c[0] == "fail"], (
            f"a successful upload must never call _fence_fail: {call_order}"
        )
        entry = _entry_ending_with("good.md")
        assert entry.index_state == "complete"

    def test_upload_failure_stamps_failed_and_reraises_unmasked(
        self, tmp_path: Path, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        call_order = _wrap_fence_helpers(monkeypatch)

        marker = "BHLFY_POISON_PROSE_MARKER"
        repo = tmp_path / "prose-bad"
        _init_repo(repo)
        (repo / "bad.md").write_text(
            f"# Bad\n\n{marker} unique content for failure injection.\n",
            encoding="utf-8",
        )
        _commit(repo)

        with pytest.raises(RuntimeError, match=marker):
            _index_with_injected_failure(repo, local_t3, monkeypatch, marker)

        entry = _entry_ending_with("bad.md")
        assert entry.index_state == "failed", (
            f"expected the fence to stamp 'failed' after the injected "
            f"upload failure, got {entry.index_state!r} — the fence "
            f"wedged at 'indexing' (the nexus-bhlfy bug) or never began"
        )
        _assert_begin_before_fail(call_order, str(entry.tumbler))


# ── code_indexer.py legacy fallback (producer #5 in the vw594 table) ───────


class TestCodeIndexerLegacyFallbackFenceFail:
    def test_success_never_calls_fence_fail(
        self, tmp_path: Path, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        call_order = _wrap_fence_helpers(monkeypatch)

        repo = tmp_path / "code-good"
        _init_repo(repo)
        (repo / "good.py").write_text(
            "def good():\n    return 1\n", encoding="utf-8",
        )
        _commit(repo)

        _index(repo, local_t3, monkeypatch)

        assert not [c for c in call_order if c[0] == "fail"], (
            f"a successful upload must never call _fence_fail: {call_order}"
        )
        entry = _entry_ending_with("good.py")
        assert entry.index_state == "complete"

    def test_upload_failure_stamps_failed_and_reraises_unmasked(
        self, tmp_path: Path, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        call_order = _wrap_fence_helpers(monkeypatch)

        marker = "BHLFY_POISON_CODE_MARKER"
        repo = tmp_path / "code-bad"
        _init_repo(repo)
        (repo / "bad.py").write_text(
            f'def bad():\n    """{marker} unique content for failure injection."""\n'
            "    return 1\n",
            encoding="utf-8",
        )
        _commit(repo)

        with pytest.raises(RuntimeError, match=marker):
            _index_with_injected_failure(repo, local_t3, monkeypatch, marker)

        entry = _entry_ending_with("bad.py")
        assert entry.index_state == "failed", (
            f"expected the fence to stamp 'failed' after the injected "
            f"upload failure, got {entry.index_state!r} — the fence "
            f"wedged at 'indexing' (the nexus-bhlfy bug) or never began"
        )
        _assert_begin_before_fail(call_order, str(entry.tumbler))


# ── indexer.py PDF legacy fallback (producer #9 in the vw594 table) ────────


class TestPdfIndexerLegacyFallbackFenceFail:
    def test_upload_failure_stamps_failed_and_reraises_unmasked(
        self, tmp_path: Path, catalog_env: Path, local_t3: T3Database,
        simple_pdf: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        call_order = _wrap_fence_helpers(monkeypatch)

        def _boom(*a, **kw):
            raise RuntimeError("nexus-bhlfy kill-control: injected PDF upload failure")

        # Single-file PDF repo — no co-located healthy file, so an
        # unconditional raise on every write is sufficient (isolation
        # from a batch-mate is covered by the ChunkBatcher-path bisect
        # test in test_vw594_runfence_coverage.py, not this legacy
        # single-file fallback).
        monkeypatch.setattr(local_t3, "_write_batch", _boom)

        repo = tmp_path / "pdf-bad"
        _init_repo(repo)
        import shutil
        shutil.copy(simple_pdf, repo / "bad.pdf")
        _commit(repo)

        reg = RepoRegistry(repo.parent / "repos.json")
        reg.add(repo)
        monkeypatch.setenv("NX_LOCAL", "1")

        from nexus.indexer import index_repository

        with patch("nexus.db.make_t3", return_value=local_t3), \
             patch("nexus.config.get_credential", side_effect=fake_credentials()), \
             pytest.raises(RuntimeError, match="injected PDF upload failure"):
            index_repository(repo, reg, force=False)

        entry = _entry_ending_with("bad.pdf")
        assert entry.index_state == "failed", (
            f"expected the fence to stamp 'failed' after the injected "
            f"PDF upload failure, got {entry.index_state!r}"
        )
        _assert_begin_before_fail(call_order, str(entry.tumbler))
