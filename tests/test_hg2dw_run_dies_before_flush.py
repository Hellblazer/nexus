# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-hg2dw acceptance test: a run that dies AFTER catalog registration
and BEFORE any chunk ever flushes must leave every registered document
either fence-stamped 'indexing' turned 'failed' at exit, or genuinely
'complete' if it was never touched (an unchanged file this run's
registration determined did not need work). None may be left 'indexing'
and none may be left unfenced ('index_state' reported but NULL with a
post-anchor ``indexed_at``) — the exact shape ``nx doctor``'s "stale
index-run fences" check (``health._check_stale_indexing_runs``) flags.

Mechanism traced in T2 nexus/unfenced-producer-hg2dw-b9m7a-2026-09-04
[24311]: Pass 1 (``indexer._catalog_hook``) registers every discovered
file — bumping ``indexed_at`` — before any chunk is produced; the repo
index path previously began a document's fence only at its first chunk
flush. A run that died in between left the registered document reported-
but-NULL forever.

Fixtures mirror ``tests/test_cp46b_runfence_repo_staleness.py`` and
``tests/db/test_bhlfy_runfence_fail_arms.py``: real engine catalog via
the suite-wide substrate, fake local T3 for chunk content.
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
    ``force=False``)."""
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


class TestRunDiesAfterRegistrationBeforeAnyFlush:
    def test_new_documents_end_failed_not_indexing_not_null(
        self, tmp_path: Path, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "a.md").write_text("# A\n\nBrand new content.\n", encoding="utf-8")
        (repo / "b.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        _commit(repo)

        # Simulate the run dying immediately after registration + the
        # registration-time begin-fence, before any per-file work (and
        # therefore before any chunk ever flushes) — the exact window
        # nexus-hg2dw closes.
        import nexus.indexer as indexer_mod
        real_begin = indexer_mod._fence_begin_needs_fence

        def _begin_then_die(needs_fence):
            real_begin(needs_fence)
            raise RuntimeError("nexus-hg2dw test: simulated crash before any flush")

        monkeypatch.setattr(indexer_mod, "_fence_begin_needs_fence", _begin_then_die)

        with pytest.raises(RuntimeError, match="simulated crash"):
            self._run(repo, local_t3, monkeypatch)

        for suffix in ("a.md", "b.py"):
            entry = _entry_ending_with(suffix)
            assert entry.index_state == "failed", (
                f"{suffix}: expected 'failed' after a crash between "
                f"registration and the first flush, got {entry.index_state!r} "
                f"(nexus-hg2dw: 'indexing' or None here means the fence "
                f"closure did not work)"
            )

    def test_doctor_stale_fence_check_reports_clean_afterward(
        self, tmp_path: Path, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.health import _check_stale_indexing_runs

        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "a.md").write_text("# A\n\nBrand new content.\n", encoding="utf-8")
        _commit(repo)

        import nexus.indexer as indexer_mod
        real_begin = indexer_mod._fence_begin_needs_fence

        def _begin_then_die(needs_fence):
            real_begin(needs_fence)
            raise RuntimeError("nexus-hg2dw test: simulated crash before any flush")

        monkeypatch.setattr(indexer_mod, "_fence_begin_needs_fence", _begin_then_die)

        with pytest.raises(RuntimeError, match="simulated crash"):
            self._run(repo, local_t3, monkeypatch)

        results = _check_stale_indexing_runs()
        label = "stale index-run fences"
        matching = [r for r in results if r.label == label]
        assert matching, f"expected a '{label}' result, got {results}"
        for r in matching:
            assert r.ok, f"nexus-hg2dw: doctor still flags a stale fence: {r.detail}"

    def test_unchanged_existing_document_is_left_complete_not_reprocessed(
        self, tmp_path: Path, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-regression control: a document already 'complete' from a
        PRIOR successful run, whose file content this run does NOT
        change, must never be touched by the registration-time fence —
        even though the crash-injected run also registers a brand-new
        sibling file that DOES need fencing."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "stable.md").write_text(
            "# Stable\n\nNever changes.\n", encoding="utf-8",
        )
        _commit(repo)
        self._run(repo, local_t3, monkeypatch)  # normal, successful run
        stable_before = _entry_ending_with("stable.md")
        assert stable_before.index_state == "complete"

        (repo / "new.md").write_text(
            "# New\n\nAdded on the second, crashing run.\n", encoding="utf-8",
        )
        _commit(repo)

        import nexus.indexer as indexer_mod
        real_begin = indexer_mod._fence_begin_needs_fence

        def _begin_then_die(needs_fence):
            real_begin(needs_fence)
            raise RuntimeError("nexus-hg2dw test: simulated crash before any flush")

        monkeypatch.setattr(indexer_mod, "_fence_begin_needs_fence", _begin_then_die)

        with pytest.raises(RuntimeError, match="simulated crash"):
            self._run(repo, local_t3, monkeypatch)

        stable_after = _entry_ending_with("stable.md")
        assert stable_after.index_state == "complete", (
            "nexus-hg2dw: an unchanged, already-complete document must "
            "never be re-fenced just because a sibling file's crashed "
            "registration needed fencing this run"
        )
        new_after = _entry_ending_with("new.md")
        assert new_after.index_state == "failed"

    @staticmethod
    def _run(repo: Path, t3: T3Database, monkeypatch: pytest.MonkeyPatch) -> None:
        _index(repo, t3, monkeypatch)
