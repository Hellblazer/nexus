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
import structlog.testing

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


def _run(repo: Path, *, extra: dict | None = None, real_catalog: bool = False, **run_kw):
    from nexus.indexer import _run_index

    db = _http_db()
    patches = dict(_real_catalog_patches()) if real_catalog else {}
    patches.update(extra or {})
    with _service_mode_patches(db, extra=patches) as mocks, patch(
        "nexus.chunk_batcher.ChunkBatcher", _CapturingBatcher,
    ):
        stats = _run_index(repo, _reg(), force=False, **run_kw)
    return stats, mocks, db


def _hook_naming(causes: dict[str, str]):
    """A stand-in ``_catalog_hook``: registers every offered file whose name is
    not a key of *causes*, and reports the named ones as unregistered."""
    def _hook(**kw):
        ids: dict[Path, str] = {}
        for i, (path, _t, _c) in enumerate(kw["indexed_files"], 1):
            if path.name in causes:
                kw["unregistered"][path] = causes[path.name]
            else:
                ids[path] = f"1.1.{i}"
        return ids
    return _hook


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
        with structlog.testing.capture_logs() as logs:
            stats, mocks, _db = _run(worktree, real_catalog=True)
        dispatched = {c.args[0].name for c in mocks["_index_code_file"].call_args_list}
        assert dispatched == {"mirrored.py"}
        # Already reported by ephemeral_path_registration_skipped; no second
        # WARNING for a deliberate refusal.
        assert not [
            e for e in logs if e.get("event") == "identity_less_files_refused_before_chunking"
        ]
        assert [s["reason"] for s in get_ephemeral_registration_skips()] == [
            "worktree_unique_no_main_mirror"
        ]
        # A deliberate refusal has its own summary line; it is not a drop.
        assert get_manifest_identity_drops() == []
        assert stats["identity_less_dropped_files"] == 0


class TestSinceHeadBaseWaitsForRefusedFiles:
    def test_base_is_not_advanced_past_a_dropped_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.indexer import index_repository

        monkeypatch.setenv("HOME", str(tmp_path))
        registry = MagicMock()
        registry.get.return_value = {
            "collection": "code__repo", "code_collection": "code__repo",
            "docs_collection": "docs__repo", "status": "registered",
        }
        with structlog.testing.capture_logs() as logs, patch(
            "nexus.indexer._run_index", return_value={"identity_less_dropped_files": 1},
        ), patch("nexus.indexer._current_head", return_value="abc"), patch(
            "nexus.indexer._set_owner_head_hash",
        ) as mock_set:
            index_repository(tmp_path, registry)
        mock_set.assert_not_called()
        held = [e for e in logs if e.get("event") == "since_head_base_not_advanced"]
        assert held and held[0]["log_level"] == "warning", logs


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


# ── Every content kind is filtered, not only code ───────────────────────────


class TestEveryKindIsFilteredBeforeDispatch:
    """``_dispatch_prose`` / ``_dispatch_pdf`` / ``_dispatch_rdr`` each drop a
    file the hook could not name; ``bad.*`` are identity-less, ``good.*`` are
    not. A reverted filter would dispatch the ``bad.*`` file."""

    def _repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        (repo / "docs" / "rdr").mkdir(parents=True)
        for rel in ("good.md", "bad.md", "docs/rdr/rdr-001-good.md", "docs/rdr/rdr-002-bad.md"):
            (repo / rel).write_text(f"# {rel}\nsome text\n")
        for rel in ("good.pdf", "bad.pdf"):
            (repo / rel).write_bytes(b"%PDF-1.4\n%fake\n")
        (repo / "good.py").write_text("x = 1\n")
        (repo / "bad.py").write_text("x = 2\n")
        return repo

    def test_prose_pdf_rdr_and_code_each_drop_the_identity_less_file(
        self, tmp_path: Path,
    ) -> None:
        repo = self._repo(tmp_path)
        causes = {
            n: "register_failed"
            for n in ("bad.md", "bad.pdf", "rdr-002-bad.md", "bad.py")
        }
        stats, mocks, _db = _run(
            repo, extra={"nexus.indexer._catalog_hook": {"side_effect": _hook_naming(causes)}},
        )
        prose = {c.args[0].name for c in mocks["_index_prose_file"].call_args_list}
        pdf = {c.args[0].name for c in mocks["_index_pdf_file"].call_args_list}
        code = {c.args[0].name for c in mocks["_index_code_file"].call_args_list}
        assert prose == {"good.md", "rdr-001-good.md"}, prose  # docs prose + rdr
        assert pdf == {"good.pdf"}, pdf
        assert code == {"good.py"}, code
        assert stats["identity_less_dropped_files"] == 4
        # Refused files were never attempted, so they do not dilute the ratio.
        assert stats["files_attempted_total"] == 4  # good.py, good.md, good.pdf, rdr-001-good.md

    def test_progress_total_is_reduced_by_the_refused_files(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        causes = {"bad.md": "register_failed", "bad.pdf": "register_failed", "bad.py": "register_failed"}
        seen: list[int] = []
        _stats, _m, _db = _run(
            repo,
            extra={"nexus.indexer._catalog_hook": {"side_effect": _hook_naming(causes)}},
            on_refused=seen.append,
        )
        assert seen == [3]  # rdr files have their own total (on_rdr_start)

    def test_on_refused_is_silent_when_nothing_was_refused(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        seen: list[int] = []
        _run(
            repo,
            extra={"nexus.indexer._catalog_hook": {"side_effect": _hook_naming({})}},
            on_refused=seen.append,
        )
        assert seen == []

    def test_the_eta_ticker_total_shrinks(self) -> None:
        from nexus.commands.index import _ETATicker

        ticker = _ETATicker(interval=3600.0)
        ticker.start(10)
        try:
            ticker.reduce_total(3)
            assert ticker._total == 7
            ticker.reduce_total(99)
            assert ticker._total == 0
        finally:
            ticker.stop()


# ── The durable per-file failure record ─────────────────────────────────────


class TestRefusedFilesEnterTheDurableRecord:
    def _run_with_store(self, repo: Path, causes: dict[str, str], *, store=None):
        store = store or MagicMock()
        store.record_index_failures_batch.return_value = 1
        with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore", return_value=store):
            stats, _m, _db = _run(
                repo,
                extra={"nexus.indexer._catalog_hook": {"side_effect": _hook_naming(causes)}},
            )
        return stats, store

    def test_dropped_files_are_recorded_with_their_cause(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, "good.py", "bad.py", "worse.py")
        stats, store = self._run_with_store(
            repo, {"bad.py": "register_failed", "worse.py": "ephemeral:worktree_or_tempdir"},
        )
        store.record_index_failures_batch.assert_called_once()
        rows, = store.record_index_failures_batch.call_args.args
        assert sorted(rows) == sorted([
            (str(repo / "bad.py"), "IdentityLessFile", "register_failed", ""),
            (str(repo / "worse.py"), "IdentityLessFile", "ephemeral:worktree_or_tempdir", ""),
        ])
        # Its own run id, separate from the extraction-skip record, and the
        # extraction-skip verdict input is untouched.
        assert store.record_index_failures_batch.call_args.kwargs["run_id"]
        store.list_index_failures.assert_not_called()
        assert stats["skipped_unextractable_files"] == 0
        assert stats["index_failures_write_failed"] is False

    def test_nothing_is_written_when_every_file_has_a_document(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, "good.py")
        _stats, store = self._run_with_store(repo, {})
        store.record_index_failures_batch.assert_not_called()

    def test_a_telemetry_outage_does_not_fail_the_run(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, "bad.py")
        store = MagicMock()
        store.record_index_failures_batch.side_effect = RuntimeError("telemetry down")
        with structlog.testing.capture_logs() as logs:
            stats, _s = self._run_with_store(repo, {"bad.py": "register_failed"}, store=store)
        assert stats["identity_less_dropped_files"] == 1
        assert [e for e in logs if e.get("event") == "identity_less_durable_write_failed"]


# ── The flush event counts only what the flush really drops ─────────────────


class TestFlushEventChunkCount:
    def test_a_chash_the_identity_document_writes_is_not_counted_as_dropped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        shared, only = "8" * 64, "9" * 64
        fctx = [
            (str(tmp_path / "has_id.py"), _ctx([shared], "1.1.4")),
            (str(tmp_path / "no_id.py"), _ctx([shared, only], "")),
        ]
        with structlog.testing.capture_logs() as logs:
            _flush(fctx, monkeypatch=monkeypatch, tmp_path=tmp_path)
        ev = [e for e in logs if e.get("event") == "combined_write_identity_less_files_dropped"]
        assert len(ev) == 1, [e.get("event") for e in logs]
        assert ev[0]["chunks_not_written"] == 1  # `only`; `shared` is written
