# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-z0o2p.20 (RDR-223 P2.10, Technical Design 4): the ownerless route.

``nx index repo`` used to send the chunks of a file with no catalog document
through the legacy upsert. Nothing owned them, so ``live(c)`` hid them from
every read, and the engine's Phase 3 refusal would abort the run on them.

These tests pin the route that replaced it:

* the batch flush writes NO chunk of an identity-less file, counts the file and
  names it;
* the run refuses such a file BEFORE chunking it, and says so in its summary;
* a file that has a document is dispatched exactly as before;
* a refused worktree file and a fairness-deferred file are not chunked either.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.test_z0o2p20_identity_less_files import (
    _CapturingBatcher,
    _ctx,
    _http_db,
)
from tests.test_indexer_seam_b_cutover import _reg, _service_mode_patches


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in [
        ("GIT_AUTHOR_NAME", "Test"),
        ("GIT_AUTHOR_EMAIL", "test@test.invalid"),
        ("GIT_COMMITTER_NAME", "Test"),
        ("GIT_COMMITTER_EMAIL", "test@test.invalid"),
        ("NEXUS_CATALOG_PATH", str(tmp_path / "catalog")),
        ("NX_STORAGE_BACKEND_VECTORS", "service"),
        ("NX_LOCAL", "0"),
        ("VOYAGE_API_KEY", "fake"),
        ("CHROMA_API_KEY", "fake"),
    ]:
        monkeypatch.setenv(k, v)


@pytest.fixture(autouse=True)
def _collectors():
    from nexus.mcp_infra import (
        reset_ephemeral_registration_skips,
        reset_manifest_identity_drops,
    )

    reset_manifest_identity_drops()  # also arms the collector
    reset_ephemeral_registration_skips()
    yield


def _real_catalog_patches() -> dict:
    """``_service_mode_patches`` stubs the catalog to None; hand the run the
    real engine catalog instead."""
    import nexus.catalog.factory as factory
    import nexus.config as config

    return {
        "nexus.catalog.factory.make_catalog_reader": {"side_effect": factory.make_catalog_reader},
        "nexus.catalog.factory.make_catalog_writer": {"side_effect": factory.make_catalog_writer},
        # The stub returns "fake-key" for EVERY credential, including the
        # engine endpoint the real catalog client reads.
        "nexus.config.get_credential": {"side_effect": config.get_credential},
    }


def _run(repo: Path, *, extra: dict | None = None, real_catalog: bool = False):
    from nexus.indexer import _run_index

    db = _http_db()
    patches = dict(_real_catalog_patches()) if real_catalog else {}
    patches.update(extra or {})
    with _service_mode_patches(db, extra=patches) as mocks, patch(
        "nexus.chunk_batcher.ChunkBatcher", _CapturingBatcher,
    ):
        stats = _run_index(repo, _reg(), force=False)
    return stats, mocks, db


def _repo(tmp_path: Path, *names: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for n in names:
        (repo / n).write_text(f"# {n}\nx = 1\n")
    return repo


# ── Stage 2: the flush ──────────────────────────────────────────────────────


def _flush(fctx: list, *, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Call the run's own ``_batch_flush`` closure with *fctx*."""
    from nexus.indexer import _run_index

    repo = _repo(tmp_path, "hello.py")
    db = _http_db()
    catalog_writer = MagicMock()
    catalog_writer.write_manifest_many.return_value = {"failed_doc_ids": [], "chunks_written": 1}
    _CapturingBatcher.captured = {}
    with _service_mode_patches(
        db, extra={"nexus.mcp_infra.get_catalog_writer": {"return_value": catalog_writer}},
    ), patch("nexus.chunk_batcher.ChunkBatcher", _CapturingBatcher):
        _run_index(repo, _reg(), force=False)
        db.reset_mock()
        catalog_writer.reset_mock()
        # The run above refused its own file (no catalog in this harness);
        # forget that so the assertions see only what the flush recorded.
        from nexus.mcp_infra import reset_manifest_identity_drops

        reset_manifest_identity_drops()
        ids = [i for _p, c in fctx for i in c["ids"]]
        docs = [d for _p, c in fctx for d in c["documents"]]
        metas = [m for _p, c in fctx for m in c["metadatas"]]
        _CapturingBatcher.captured["flush"]("code__repo__m__v1", ids, docs, metas, fctx)
    return db, catalog_writer, repo


class TestFlushWritesNothingForAnIdentityLessFile:
    def test_mixed_flush_writes_only_the_identity_files_chunks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.mcp_infra import get_manifest_identity_drops

        keep, drop = "1" * 64, "2" * 64
        fctx = [
            (str(tmp_path / "has_id.py"), _ctx([keep], "1.1.7")),
            (str(tmp_path / "no_id.py"), _ctx([drop], "")),
        ]
        db, cw, _repo_dir = _flush(fctx, monkeypatch=monkeypatch, tmp_path=tmp_path)

        # The vector client is never written to: not through the legacy
        # upsert, not through any other method.
        assert db.mock_calls == []
        cw.write_manifest_many.assert_called_once()
        sent = {c["chash"] for c in cw.write_manifest_many.call_args.kwargs["chunks"]}
        assert sent == {keep}
        drops = get_manifest_identity_drops()
        assert len(drops) == 1
        assert drops[0]["written"] is False
        assert drops[0]["batch_size"] == 1
        assert [f["file"] for f in drops[0]["files"]] == [str(tmp_path / "no_id.py")]

    def test_all_identity_less_flush_makes_no_write_at_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.mcp_infra import get_manifest_identity_drops

        fctx = [
            (str(tmp_path / "a.py"), _ctx(["3" * 64, "4" * 64], "")),
            (str(tmp_path / "b.py"), _ctx(["5" * 64], "")),
        ]
        db, cw, _r = _flush(fctx, monkeypatch=monkeypatch, tmp_path=tmp_path)
        assert db.mock_calls == []
        cw.write_manifest_many.assert_not_called()
        drops = get_manifest_identity_drops()
        assert sum(d["batch_size"] for d in drops) == 3
        assert all(d["written"] is False for d in drops)
        assert sorted(f["file"] for d in drops for f in d["files"]) == sorted(
            [str(tmp_path / "a.py"), str(tmp_path / "b.py")]
        )

    def test_a_chash_shared_with_an_identity_file_is_written_only_by_that_document(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """nexus-3mwuo used to copy a shared chash into the ownerless upsert so
        it would survive the identity document's failed write. That copy is
        gone: the chash now rides only the identity document's combined write
        (and its failed_doc_ids / fence handling). The identity-less file is
        still named as dropped."""
        from nexus.mcp_infra import get_manifest_identity_drops

        shared = "6" * 64
        fctx = [
            (str(tmp_path / "has_id.py"), _ctx([shared], "1.1.9")),
            (str(tmp_path / "no_id.py"), _ctx([shared], "")),
        ]
        db, cw, _r = _flush(fctx, monkeypatch=monkeypatch, tmp_path=tmp_path)
        assert db.mock_calls == []
        chunks = cw.write_manifest_many.call_args.kwargs["chunks"]
        assert [c["chash"] for c in chunks] == [shared]
        assert [f["file"] for d in get_manifest_identity_drops() for f in d["files"]] == [
            str(tmp_path / "no_id.py")
        ]

    def test_an_all_identity_file_flush_records_no_drop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.mcp_infra import get_manifest_identity_drops

        fctx = [(str(tmp_path / "has_id.py"), _ctx(["7" * 64], "1.1.3"))]
        db, cw, _r = _flush(fctx, monkeypatch=monkeypatch, tmp_path=tmp_path)
        cw.write_manifest_many.assert_called_once()
        assert get_manifest_identity_drops() == []


# ── Stage 3: the run refuses the file before chunking it ────────────────────


class TestRunRefusesIdentityLessFilesBeforeChunking:
    def test_files_without_a_document_are_not_dispatched(self, tmp_path: Path) -> None:
        """The catalog is unreachable (the stub reader), so no file has a
        document. None is chunked; each is a named drop, not indexed."""
        from nexus.mcp_infra import get_manifest_identity_drops

        repo = _repo(tmp_path, "hello.py", "other.py")
        stats, mocks, db = _run(repo)
        mocks["_index_code_file"].assert_not_called()
        db.upsert_chunks_with_embeddings.assert_not_called()
        assert stats["identity_less_dropped_files"] == 2
        assert stats["identity_less_deferred_files"] == 0
        drops = get_manifest_identity_drops()
        assert all(d["written"] is False for d in drops)
        named = {f["file"]: f["cause"] for d in drops for f in d["files"]}
        assert named == {
            str(repo / "hello.py"): "catalog_hook_failed",
            str(repo / "other.py"): "catalog_hook_failed",
        }

    def test_files_with_a_document_are_dispatched_as_before(self, tmp_path: Path) -> None:
        from nexus.mcp_infra import get_manifest_identity_drops

        repo = _repo(tmp_path, "hello.py", "other.py")
        stats, mocks, _db = _run(repo, real_catalog=True)
        dispatched = {
            call.args[0].name: call.kwargs["doc_id_resolver"](call.args[0])
            for call in mocks["_index_code_file"].call_args_list
        }
        assert set(dispatched) == {"hello.py", "other.py"}
        assert all(dispatched.values()), dispatched  # each carries its document id
        assert stats["identity_less_dropped_files"] == 0
        assert stats["identity_less_deferred_files"] == 0
        assert get_manifest_identity_drops() == []

    def test_a_refused_worktree_file_is_not_chunked_and_is_not_a_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.mcp_infra import get_ephemeral_registration_skips, get_manifest_identity_drops

        main_repo = tmp_path / "primary"
        main_repo.mkdir()
        worktree = main_repo / ".claude" / "worktrees" / "agent-y"
        worktree.mkdir(parents=True)
        (worktree / "mirrored.py").write_text("x = 1\n")
        (worktree / "draft.py").write_text("x = 2\n")
        (main_repo / "mirrored.py").write_text("x = 1\n")
        monkeypatch.setattr(
            "nexus.repo_identity._repo_identity_with_main",
            lambda r: ("primary", "z0o2p2010", main_repo),
        )
        monkeypatch.setattr(
            "nexus.repo_identity._repo_identity", lambda r: ("primary", "z0o2p2010"),
        )
        stats, mocks, _db = _run(worktree, real_catalog=True)
        dispatched = {c.args[0].name for c in mocks["_index_code_file"].call_args_list}
        assert dispatched == {"mirrored.py"}
        assert [s["reason"] for s in get_ephemeral_registration_skips()] == [
            "worktree_unique_no_main_mirror"
        ]
        # A deliberate refusal has its own summary line; it is not a drop.
        assert get_manifest_identity_drops() == []
        assert stats["identity_less_dropped_files"] == 0

    def test_fairness_deferred_files_are_not_chunked_and_are_counted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.mcp_infra import get_manifest_identity_drops

        monkeypatch.setenv("NX_WRITE_PRIORITY", "batch")
        monkeypatch.setattr(
            "nexus.catalog.write_priority.await_fair_window", lambda pending, on_locked: "skip",
        )
        repo = _repo(tmp_path, "hello.py", "other.py")
        stats, mocks, _db = _run(repo, real_catalog=True)
        mocks["_index_code_file"].assert_not_called()
        assert stats["identity_less_deferred_files"] == 2
        assert stats["identity_less_dropped_files"] == 0
        # A deferral is retried by the next pass; it is not a failed run.
        assert get_manifest_identity_drops() == []


class TestSinceHeadBaseWaitsForRefusedFiles:
    @pytest.mark.parametrize("key", ["identity_less_deferred_files", "identity_less_dropped_files"])
    def test_base_is_not_advanced_past_a_refused_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str,
    ) -> None:
        from nexus.indexer import index_repository

        monkeypatch.setenv("HOME", str(tmp_path))
        registry = MagicMock()
        registry.get.return_value = {
            "collection": "code__repo", "code_collection": "code__repo",
            "docs_collection": "docs__repo", "status": "registered",
        }
        with patch("nexus.indexer._run_index", return_value={key: 1}), patch(
            "nexus.indexer._current_head", return_value="abc",
        ), patch("nexus.indexer._set_owner_head_hash") as mock_set:
            index_repository(tmp_path, registry)
        mock_set.assert_not_called()


# ── The run summary names the dropped files ─────────────────────────────────


class TestSummaryNamesDroppedFiles:
    def test_summary_lists_the_files_and_their_causes(self, capsys) -> None:
        from nexus.commands._helpers import emit_identity_drop_summary
        from nexus.mcp_infra import _record_manifest_identity_drop

        _record_manifest_identity_drop(
            "code__repo__m__v1", 0, written=False,
            files=[{"file": "/r/a.py", "chunks": 0, "cause": "register_failed"}],
        )
        assert emit_identity_drop_summary(indexed_count=0) is True
        err = capsys.readouterr().err
        assert "/r/a.py" in err
        assert "register_failed" in err
        assert "nothing was written" in err


class TestCliNamesDeferredFiles:
    def test_index_repo_says_how_many_files_it_did_not_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from click.testing import CliRunner

        from nexus.cli import main

        monkeypatch.setenv("HOME", str(tmp_path))
        repo_dir = tmp_path / "myrepo"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()
        reg = MagicMock()
        reg.get.return_value = {"collection": "code__myrepo"}
        with patch("nexus.commands.index._registry", return_value=reg), patch(
            "nexus.indexer.index_repository",
            return_value={"files_changed": 0, "identity_less_deferred_files": 3},
        ):
            result = CliRunner().invoke(main, ["index", "repo", str(repo_dir)])
        assert result.exit_code == 0, result.output  # a deferral is not a failure
        assert "deferred: 3 file(s) NOT indexed this run" in result.output, result.output


class TestExitRemedyForFilesThatWroteNothing:
    def test_the_failure_does_not_point_at_a_reconcile_with_nothing_to_repair(self) -> None:
        import click

        from nexus.commands._helpers import raise_identity_drop_exception
        from nexus.mcp_infra import _record_manifest_identity_drop

        _record_manifest_identity_drop(
            "code__repo__m__v1", 0, written=False,
            files=[{"file": "/r/a.py", "chunks": 0, "cause": "register_failed"}],
        )
        with pytest.raises(click.ClickException) as exc:
            raise_identity_drop_exception()
        msg = exc.value.message
        assert "re-run the index" in msg
        assert "nx catalog reconcile" not in msg
