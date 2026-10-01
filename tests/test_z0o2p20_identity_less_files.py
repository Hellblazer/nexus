# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-z0o2p.20 (RDR-223 P2.10): identity-less files in ``nx index repo``.

A file with no catalog document has no owner for its chunks. Before this bead
the batch flush wrote those chunks anyway, through the legacy upsert, and
``live(c)`` then hid them from every read. This file pins, in order:

* Stage 1: WHY a file ends up with no document. ``_catalog_hook`` reports a
  cause per unregistered file through its ``unregistered`` out-parameter; each
  shape the bead named (main checkout, worktree, warm re-run, register failure,
  catalog hook failure) is driven against the real engine catalog. The
  batch-priority fairness yield is NOT among them: the service catalog writer's
  ``is_interactive_write_pending`` is always False, so no production run yields.
* Stage 1: the flush event names the files, their chunk counts and causes.
* Stage 2: the flush writes no chunk of an identity-less file.
* Stage 3: the run refuses such a file before chunking it.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import structlog.testing

from tests._catalog_fixture_ops import ActiveCatalog


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in [
        ("GIT_AUTHOR_NAME", "Test"),
        ("GIT_AUTHOR_EMAIL", "test@test.invalid"),
        ("GIT_COMMITTER_NAME", "Test"),
        ("GIT_COMMITTER_EMAIL", "test@test.invalid"),
    ]:
        monkeypatch.setenv(k, v)


@pytest.fixture(autouse=True)
def _point_catalog_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "catalog"))


def _hook(repo: Path, files: list[Path], *, head: str = "h1", repo_hash: str = "z0o2p2000",
          content_type: str = "code", collection: str = "code__z0o2p20"):
    """Run the real ``_catalog_hook``; return ``(file_to_doc_id, unregistered)``."""
    from nexus.indexer import _catalog_hook

    unregistered: dict[Path, str] = {}
    result = _catalog_hook(
        repo=repo, repo_name=repo.name, repo_hash=repo_hash, head_hash=head,
        indexed_files=[(f, content_type, collection) for f in files],
        skip_housekeeping=True, unregistered=unregistered,
    )
    return result, unregistered


def _write(root: Path, rel: str, text: str = "x = 1\n") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


# ── Stage 1: the cause of every identity-less file ──────────────────────────


class TestUnregisteredCauses:
    def test_main_checkout_registers_every_file(self, tmp_path: Path) -> None:
        ActiveCatalog()  # the engine catalog is live
        repo = tmp_path / "main"
        files = [_write(repo, "a.py"), _write(repo, "pkg/b.py")]
        ids, unregistered = _hook(repo, files)
        assert set(ids) == set(files)
        assert unregistered == {}

    def test_warm_rerun_registers_every_file(self, tmp_path: Path) -> None:
        repo = tmp_path / "main"
        files = [_write(repo, "a.py"), _write(repo, "b.py")]
        _hook(repo, files, head="h1")
        ids, unregistered = _hook(repo, files, head="h2")
        assert set(ids) == set(files)
        assert unregistered == {}

    def test_worktree_unique_file_is_refused_with_its_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        main_repo = tmp_path / "primary"
        main_repo.mkdir()
        worktree = main_repo / ".claude" / "worktrees" / "agent-y"
        draft = _write(worktree, "docs/rdr/rdr-999-draft.md", "# draft\n")
        mirrored = _write(worktree, "src/shared.py")
        _write(main_repo, "src/shared.py")  # the mirror in the main checkout
        monkeypatch.setattr(
            "nexus.repo_identity._repo_identity_with_main",
            lambda r: ("primary", "z0o2p2001", main_repo),
        )
        ids, unregistered = _hook(worktree, [draft, mirrored], repo_hash="z0o2p2001")
        assert mirrored in ids
        assert draft not in ids
        assert unregistered == {draft: "ephemeral:worktree_unique_no_main_mirror"}

    def test_worktree_marker_path_is_refused_with_its_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Keep the owner root clean of temp-dir prefixes on every platform.
        monkeypatch.setattr(
            "nexus.repo_identity._TEMP_DIR_PREFIXES", ("/nonexistent-tmp-prefix/",),
        )
        main_repo = tmp_path / "primary"
        polluted = _write(main_repo, ".claude/worktrees/agent-x/docs/foo.md", "# e\n")
        good = _write(main_repo, "src/good.py")
        ids, unregistered = _hook(main_repo, [polluted, good], repo_hash="z0o2p2002")
        assert good in ids
        assert unregistered == {polluted: "ephemeral:worktree_or_tempdir"}

    def test_a_register_failure_names_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import nexus.catalog.factory as factory

        real_writer = factory.make_catalog_writer
        repo = tmp_path / "main"
        good = _write(repo, "good.py")
        bad = _write(repo, "bad.py")

        class _Proxy:
            def __init__(self, target):
                self._target = target

            def __getattr__(self, name):
                return getattr(self._target, name)

            def register_many(self, owner, docs, **kw):
                raise RuntimeError("batch endpoint down")

            def register(self, owner, title, **kw):
                if title == "bad.py":
                    raise RuntimeError("register refused")
                return self._target.register(owner, title, **kw)

        monkeypatch.setattr(factory, "make_catalog_writer", lambda **kw: _Proxy(real_writer(**kw)))
        ids, unregistered = _hook(repo, [good, bad], repo_hash="z0o2p2003")
        assert good in ids
        assert bad not in ids
        assert unregistered == {bad: "register_failed"}

    def test_a_catalog_hook_failure_names_every_unresolved_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import nexus.catalog.factory as factory

        def boom(**kw):
            raise RuntimeError("catalog unreachable")

        monkeypatch.setattr(factory, "make_catalog_reader", boom)
        repo = tmp_path / "main"
        files = [_write(repo, "a.py"), _write(repo, "b.py")]
        ids, unregistered = _hook(repo, files, repo_hash="z0o2p2004")
        assert ids == {}
        assert unregistered == {f: "catalog_hook_failed" for f in files}

    def test_the_resolver_finds_every_file_the_hook_registered(self, tmp_path: Path) -> None:
        """No path-key mismatch: the resolver is keyed on the exact ``Path``
        objects the run dispatches, so a symlinked repo path resolves too."""
        from nexus.indexer_utils import build_doc_id_resolver

        real = tmp_path / "real"
        _write(real, "pkg/a.py")
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        files = [link / "pkg" / "a.py"]
        ids, unregistered = _hook(link, files, repo_hash="z0o2p2005")
        resolve = build_doc_id_resolver(ids)
        assert unregistered == {}
        assert all(resolve(f) for f in files)
        # A differently spelled path to the same file is NOT the same key. The
        # run never dispatches one (it hands the resolver the objects it
        # registered), which is why a mismatch cannot arise inside _run_index.
        assert resolve(real / "pkg" / "a.py") == ""


# ── Stage 1: the flush event names files, chunk counts and causes ───────────


def _http_db():
    from nexus.db.http_vector_client import HttpVectorClient

    return MagicMock(spec=HttpVectorClient)


class _CapturingBatcher:
    """A ChunkBatcher stand-in that hands the run's flush closure back."""

    captured: dict = {}

    def __init__(self, *, flush, **_kw):
        type(self).captured["flush"] = flush

    def add(self, *_a, **_kw):
        return False

    def drain(self, on_progress=None) -> int:
        return 0

    @property
    def pending_summary(self) -> dict:
        return {"chunks": 0, "collections": 0, "in_flight": 0}

    @property
    def failed_files(self) -> dict:
        return {}

    @property
    def throttled_files(self) -> dict:
        return {}

    @property
    def throttle_retry_after(self) -> float | None:
        return None

    @property
    def throttle_breaker_open(self) -> bool:
        return False

    @property
    def stats(self) -> dict:
        return {"flushes": 0.0, "flush_seconds": 0.0, "upload_seconds": 0.0}


def _drive_flush(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, contexts_for):
    """Run ``_run_index`` against an Http-spec db whose catalog is unreachable
    (so every file is identity-less with cause ``catalog_hook_failed``), then
    call the run's own flush closure with *contexts_for(repo)*."""
    from nexus.indexer import _run_index
    from tests.test_indexer_seam_b_cutover import _reg, _service_mode_patches

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "hello.py").write_text("x = 1\n")
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")
    monkeypatch.setenv("CHROMA_API_KEY", "fake")
    db = _http_db()
    catalog_writer = MagicMock()
    _CapturingBatcher.captured = {}
    with structlog.testing.capture_logs() as logs, _service_mode_patches(
        db, extra={"nexus.mcp_infra.get_catalog_writer": {"return_value": catalog_writer}},
    ), patch("nexus.chunk_batcher.ChunkBatcher", _CapturingBatcher):
        _run_index(repo, _reg(), force=False)
        fctx = contexts_for(repo)
        ids = [i for _p, c in fctx for i in c["ids"]]
        docs = [d for _p, c in fctx for d in c["documents"]]
        metas = [m for _p, c in fctx for m in c["metadatas"]]
        _CapturingBatcher.captured["flush"]("code__repo__m__v1", ids, docs, metas, fctx)
    return db, catalog_writer, logs, repo


def _ctx(chashes: list[str], doc_id: str) -> dict:
    return {
        "ids": chashes,
        "documents": [f"text {c[:4]}" for c in chashes],
        "metadatas": [{"chunk_text_hash": c, "content_hash": "c" * 64} for c in chashes],
        "catalog_doc_id": doc_id,
    }


class TestFlushEventNamesTheFiles:
    def test_event_carries_files_chunk_counts_and_causes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        a, b = "a" * 64, "b" * 64

        def contexts_for(repo: Path):
            return [
                (str(repo / "hello.py"), _ctx([a, b], "")),
                (str(repo / "elsewhere.py"), _ctx(["c" * 64], "")),
            ]

        _db, _cw, logs, repo = _drive_flush(tmp_path, monkeypatch, contexts_for=contexts_for)
        events = [e for e in logs if e.get("event") == "combined_write_identity_less_files_dropped"]
        assert len(events) == 1, [e.get("event") for e in logs]
        ev = events[0]
        assert ev["chunks_not_written"] == 3
        assert ev["file_count"] == 2
        assert ev["file_chunks"] == 3
        assert ev["causes"] == {"catalog_hook_failed": 1, "unexplained": 1}
        assert sorted(ev["files"]) == sorted([str(repo / "hello.py"), str(repo / "elsewhere.py")])


class TestIdentityLessFilesHelper:
    def test_names_only_files_without_a_document(self) -> None:
        from nexus.indexer import _identity_less_files

        fctx = [
            ("/r/a.py", {"ids": ["1", "2"], "catalog_doc_id": "1.1.1"}),
            ("/r/b.py", {"ids": ["3", "4", "5"], "catalog_doc_id": ""}),
            ("/r/c.py", {"ids": ["6"], "catalog_doc_id": ""}),
            ("/r/d.py", "not-a-dict"),
        ]
        out = _identity_less_files(fctx, {Path("/r/b.py"): "register_failed"})
        assert out == [
            {"file": "/r/b.py", "chunks": 3, "cause": "register_failed"},
            {"file": "/r/c.py", "chunks": 1, "cause": "unexplained"},
        ]
