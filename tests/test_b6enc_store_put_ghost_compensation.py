# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-b6enc (GH #1419 Issue 8): silent store_put data loss — client side.

Four seams, all locked here:

- **C2 ghost-register compensation**: both MCP ``store_put`` and CLI
  ``nx store put`` register the catalog row BEFORE ``t3.put``. A put
  failure must delete the row minted IN THIS CALL (never a pre-existing
  dedup target) and still surface the original error.
- **C3 manifest leg out of best-effort**: the manifest write no longer
  rides the swallowing ``fire_batch`` chain for the store_put producer;
  it is called directly and VERIFIED. Failure yields an explicit
  "stored ... but NOT cataloged" result, never a bare "Stored:".
- **C4 delete asymmetry**: MCP ``store_delete`` removes the
  store_put-origin catalog row (manifest cascades) so no row survives
  with a stale chunk_count.
- Success parity: chunk_count == manifest count == T3 chunks, even with
  every fire_* chain dead (the manifest leg is independent now).

Tests use a real local Catalog (tmp ``NEXUS_CATALOG_PATH``) + a real
in-memory T3; mocks appear only at the failure-injection points, per the
integration-over-mocks rule.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from nexus.catalog.catalog import Catalog
from nexus.catalog.tumbler import Tumbler
from nexus.db.t3 import T3Database
from tests.conftest import make_vector_test_client


@pytest.fixture(autouse=True)
def _pin_local_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the sqlite/local backend so these local-Catalog-seeded tests
    stay deterministic under the ``NX_TEST_T2_SUBSTRATE=engine`` flip
    (which sets ``NX_STORAGE_BACKEND=service`` globally and would
    re-route the catalog hooks at the freshly minted engine tenant while
    the assertions read the local tmp catalog)."""
    monkeypatch.setenv("NX_STORAGE_BACKEND", "sqlite")


@pytest.fixture
def catalog_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    Catalog.init(catalog_dir)
    return catalog_dir


@pytest.fixture
def local_t3() -> T3Database:
    db = T3Database(
        _client=make_vector_test_client(),
        _ef_override=DefaultEmbeddingFunction(),
    )
    from nexus.mcp_infra import inject_t3
    inject_t3(db)
    yield db
    inject_t3(None)


class _FailingT3:
    """T3 stub whose ``put`` always raises — the Steve-report failure
    shape (engine skew / CPU-pegged / 500 on upsert-chunks)."""

    def list_collections(self):  # for t3_collection_name's probe
        return []

    def put(self, **kwargs):
        raise RuntimeError("engine 500: upsert-chunks failed")

    # ``nx memory promote`` consumes T3 via ``with make_t3() as t3:``.
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _catalog_rows(catalog_env: Path, title: str) -> list:
    cat = Catalog(catalog_env, catalog_env / ".catalog.db")
    try:
        return cat._db.execute(
            "SELECT tumbler, chunk_count FROM documents WHERE title = ?",
            (title,),
        ).fetchall()
    finally:
        cat._db.close()


def _manifest_rows(catalog_env: Path, tumbler: str) -> list:
    cat = Catalog(catalog_env, catalog_env / ".catalog.db")
    try:
        return cat._db.execute(
            "SELECT chash FROM document_chunks WHERE doc_id = ?",
            (tumbler,),
        ).fetchall()
    finally:
        cat._db.close()


def _no_op(*args, **kwargs):
    pass


def _mcp_store_put_with(t3, content: str, title: str) -> str:
    from nexus.mcp.core import store_put

    with patch("nexus.mcp.core._get_t3", return_value=t3), \
         patch("nexus.mcp.core._hooks.fire_single", side_effect=_no_op), \
         patch("nexus.mcp.core._hooks.fire_batch", side_effect=_no_op), \
         patch("nexus.mcp.core._hooks.fire_document", side_effect=_no_op), \
         patch("nexus.mcp.core._catalog_auto_link", return_value=0):
        return store_put(content=content, collection="knowledge", title=title)


# ── C2: ghost-register compensation (MCP) ────────────────────────────────────


class TestMcpGhostRegisterCompensation:
    def test_t3_failure_rolls_back_minted_row(self, catalog_env: Path) -> None:
        """t3.put raising must surface the error AND leave no catalog
        row for the title (pre-fix: permanent ghost, content lost)."""
        result = _mcp_store_put_with(
            _FailingT3(), "ghost content one", "b6enc-ghost-mcp",
        )
        assert result.startswith("Error"), result
        assert "engine 500" in result
        assert _catalog_rows(catalog_env, "b6enc-ghost-mcp") == [], (
            "catalog row minted before the failed t3.put must be rolled back"
        )

    def test_t3_failure_preserves_preexisting_deduped_row(
        self, catalog_env: Path,
    ) -> None:
        """A row the register DEDUPED onto (by_doc_id hit) pre-existed
        this call and must NEVER be deleted by the compensation."""
        content = "dedup content survives"
        chash = hashlib.sha256(content.encode()).hexdigest()
        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        owner = cat.register_owner("knowledge", "curator")
        cat.register(
            owner, "b6enc-dedup-mcp", content_type="knowledge",
            physical_collection="knowledge__knowledge__voyage-context-3__v1",
            meta={"doc_id": chash},
        )
        cat._db.close()

        result = _mcp_store_put_with(_FailingT3(), content, "b6enc-dedup-mcp")
        assert result.startswith("Error"), result
        assert len(_catalog_rows(catalog_env, "b6enc-dedup-mcp")) == 1, (
            "pre-existing deduped row must survive the compensation"
        )

    def test_compensation_failure_does_not_mask_original_error(
        self, catalog_env: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The compensating delete itself blowing up must not mask the
        original t3.put error (fail-loud: both logged, original wins)."""
        from nexus.catalog import store_hook as sh

        real = sh.rollback_minted_catalog_entry

        def _rollback_with_broken_writer(tumbler, *, original_error=""):
            # Break the writer factory ONLY inside the rollback, so the
            # earlier register path is untouched.
            with patch(
                "nexus.catalog.factory.make_catalog_writer",
                side_effect=RuntimeError("writer also down"),
            ):
                return real(tumbler, original_error=original_error)

        monkeypatch.setattr(
            sh, "rollback_minted_catalog_entry", _rollback_with_broken_writer,
        )
        result = _mcp_store_put_with(
            _FailingT3(), "mask check content", "b6enc-mask-mcp",
        )
        assert result.startswith("Error"), result
        assert "engine 500" in result, (
            "compensation failure must not mask the original t3.put error"
        )


# ── C3: manifest leg fail-loud (MCP) ─────────────────────────────────────────


class TestMcpManifestFailLoud:
    def test_manifest_failure_returns_not_cataloged(
        self, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "nexus.catalog.store_hook.store_put_manifest_direct",
            lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("manifest write refused")
            ),
        )
        result = _mcp_store_put_with(
            local_t3, "manifest fail content", "b6enc-manifest-mcp",
        )
        assert "NOT cataloged" in result, result
        assert "manifest write refused" in result
        assert "Stored:" not in result, (
            "a manifest failure must never produce a bare 'Stored:' result"
        )
        # Content IS in T3 (recoverable) — only the catalog leg failed.
        chash = hashlib.sha256(b"manifest fail content").hexdigest()
        cols = [c["name"] for c in local_t3.list_collections()
                if c["name"].startswith("knowledge__")]
        assert cols, "expected the knowledge collection to exist in T3"
        assert local_t3.get_by_id(cols[0], chash) is not None

    def test_success_counts_align_without_fire_batch(
        self, catalog_env: Path, local_t3: T3Database,
    ) -> None:
        """chunk_count == manifest count == 1 == T3 chunks, with every
        fire_* chain dead — the manifest leg no longer rides the
        swallowing hook chain (C3's exact silent-drift mechanism)."""
        content = "healthy store_put content"
        result = _mcp_store_put_with(local_t3, content, "b6enc-healthy-mcp")
        assert result.startswith("Stored:"), result

        rows = _catalog_rows(catalog_env, "b6enc-healthy-mcp")
        assert len(rows) == 1
        tumbler, chunk_count = rows[0]
        assert chunk_count == 1, (
            f"chunk_count must be resynced to 1, got {chunk_count}"
        )
        manifest = _manifest_rows(catalog_env, tumbler)
        chash = hashlib.sha256(content.encode()).hexdigest()
        assert [r[0] for r in manifest] == [chash]
        stored_col = result.split("->")[-1].strip()
        assert local_t3.get_by_id(stored_col, chash) is not None


class TestStorePutManifestDirectUnit:
    def test_silent_no_op_write_fails_verify(
        self, catalog_env: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The VERIFY leg: a write path that silently no-ops (the exact
        C3 damage shape) must RAISE, never return clean. Mutation
        target: deleting the 'did not land' raise makes this fail."""
        from nexus.catalog.store_hook import store_put_manifest_direct

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        owner = cat.register_owner("knowledge", "curator")
        t = cat.register(
            owner, "verify-target", content_type="knowledge",
            physical_collection="knowledge__x",
            meta={"doc_id": "f" * 64},
        )
        cat._db.close()

        monkeypatch.setattr(
            Catalog, "atomic_manifest_replace", lambda self, d, c: None,
        )
        monkeypatch.setattr(
            Catalog, "resync_chunk_count_cache", lambda self, d: None,
        )
        with pytest.raises(RuntimeError, match="did not land"):
            store_put_manifest_direct(str(t), [{
                "chunk_text_hash": "f" * 64,
                "chunk_start_char": 0,
                "chunk_end_char": 10,
            }])

    def test_empty_metadata_raises(self, catalog_env: Path) -> None:
        from nexus.catalog.store_hook import store_put_manifest_direct

        with pytest.raises(RuntimeError, match="nothing to catalog"):
            store_put_manifest_direct("1.2.3", [{}])

    def test_blank_doc_id_is_a_no_op(self) -> None:
        """No catalog tumbler (no-catalog / opt-out path): nothing to
        write, nothing to verify — must not raise."""
        from nexus.catalog.store_hook import store_put_manifest_direct

        store_put_manifest_direct("", [{"chunk_text_hash": "a" * 64}])


# ── C2/C3: CLI nx store put ──────────────────────────────────────────────────


class TestCliStorePut:
    def _invoke(self, tmp_path: Path, t3, title: str, content: str):
        from click.testing import CliRunner

        from nexus.cli import main

        f = tmp_path / "note.md"
        f.write_text(content)
        with patch("nexus.commands.store._t3", lambda: t3):
            return CliRunner().invoke(main, [
                "store", "put", str(f),
                "--collection", "knowledge",
                "--title", title,
            ])

    def test_t3_failure_rolls_back_minted_row(
        self, catalog_env: Path, tmp_path: Path,
    ) -> None:
        result = self._invoke(
            tmp_path, _FailingT3(), "b6enc-ghost-cli", "cli ghost content",
        )
        assert result.exit_code != 0
        assert _catalog_rows(catalog_env, "b6enc-ghost-cli") == [], (
            "CLI put failure must roll back the just-minted catalog row"
        )

    def test_manifest_failure_is_explicit_error(
        self, catalog_env: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import nexus.commands.store as store_mod

        local = T3Database(
            _client=make_vector_test_client(),
            _ef_override=DefaultEmbeddingFunction(),
        )
        monkeypatch.setattr(
            store_mod, "_store_put_manifest_direct",
            lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("manifest write refused")
            ),
        )
        result = self._invoke(
            tmp_path, local, "b6enc-manifest-cli", "cli manifest fail",
        )
        assert result.exit_code != 0
        assert "NOT cataloged" in result.output
        assert "Stored:" not in result.output

    def test_success_echoes_stored(
        self, catalog_env: Path, tmp_path: Path,
    ) -> None:
        local = T3Database(
            _client=make_vector_test_client(),
            _ef_override=DefaultEmbeddingFunction(),
        )
        result = self._invoke(
            tmp_path, local, "b6enc-ok-cli", "cli healthy content",
        )
        assert result.exit_code == 0, result.output
        assert "Stored:" in result.output
        rows = _catalog_rows(catalog_env, "b6enc-ok-cli")
        assert len(rows) == 1 and rows[0][1] == 1


# ── C2/C3: nx memory promote (critic Critical nexus-v4paa) ──────────────────


class TestPromoteGhostRegisterCompensation:
    """``nx memory promote`` shared the identical register-before-put
    seam the two store_put producers were fixed for — same compensation,
    same fail-loud manifest leg, locked here promote-shaped."""

    def _invoke_promote(self, tmp_path: Path, t3, title: str, content: str):
        from click.testing import CliRunner

        from nexus.cli import main
        from nexus.db.t2 import T2Database

        db = T2Database(tmp_path / "promote-t2.db")
        row_id = db.put(project="proj", title=title, content=content, ttl=7)
        with patch("nexus.commands.memory.t2_handle", return_value=db), \
             patch("nexus.db.make_t3", return_value=t3):
            return CliRunner().invoke(main, [
                "memory", "promote", str(row_id),
                "--collection", "knowledge",
            ])

    def test_t3_failure_rolls_back_minted_row(
        self, catalog_env: Path, tmp_path: Path,
    ) -> None:
        result = self._invoke_promote(
            tmp_path, _FailingT3(), "b6enc-ghost-promote", "promote ghost content",
        )
        assert result.exit_code != 0
        assert "engine 500" in result.output
        assert _catalog_rows(catalog_env, "b6enc-ghost-promote") == [], (
            "promote's t3.put failure must roll back the just-minted "
            "catalog row (nexus-v4paa)"
        )

    def test_t3_failure_preserves_preexisting_deduped_row(
        self, catalog_env: Path, tmp_path: Path,
    ) -> None:
        content = "promote dedup content survives"
        chash = hashlib.sha256(content.encode()).hexdigest()
        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        owner = cat.register_owner("knowledge", "curator")
        cat.register(
            owner, "b6enc-dedup-promote", content_type="knowledge",
            physical_collection="knowledge__knowledge__voyage-context-3__v1",
            meta={"doc_id": chash},
        )
        cat._db.close()

        result = self._invoke_promote(
            tmp_path, _FailingT3(), "b6enc-dedup-promote", content,
        )
        assert result.exit_code != 0
        assert len(_catalog_rows(catalog_env, "b6enc-dedup-promote")) == 1, (
            "pre-existing deduped row must survive promote's compensation"
        )

    def test_manifest_failure_surfaces_loudly(
        self, catalog_env: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        local = T3Database(
            _client=make_vector_test_client(),
            _ef_override=DefaultEmbeddingFunction(),
        )
        monkeypatch.setattr(
            "nexus.catalog.store_hook.store_put_manifest_direct",
            lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("manifest write refused")
            ),
        )
        result = self._invoke_promote(
            tmp_path, local, "b6enc-manifest-promote", "promote manifest fail",
        )
        assert result.exit_code != 0
        assert "NOT cataloged" in result.output
        assert "manifest write refused" in result.output
        assert "Promoted:" not in result.output, (
            "a manifest failure must never produce a bare 'Promoted:' echo"
        )

    def test_success_counts_align(
        self, catalog_env: Path, tmp_path: Path,
    ) -> None:
        local = T3Database(
            _client=make_vector_test_client(),
            _ef_override=DefaultEmbeddingFunction(),
        )
        content = "healthy promote content"
        result = self._invoke_promote(
            tmp_path, local, "b6enc-ok-promote", content,
        )
        assert result.exit_code == 0, result.output
        assert "Promoted:" in result.output
        rows = _catalog_rows(catalog_env, "b6enc-ok-promote")
        assert len(rows) == 1
        tumbler, chunk_count = rows[0]
        assert chunk_count == 1
        chash = hashlib.sha256(content.encode()).hexdigest()
        assert [r[0] for r in _manifest_rows(catalog_env, tumbler)] == [chash]


# ── CRE Imp 1: writer.close guard in catalog_store_hook_tracked ─────────────


class TestWriterCloseGuard:
    def test_writer_close_failure_preserves_created_flag(
        self, catalog_env: Path,
    ) -> None:
        """Mutation target (CRE Imp 1): ``writer.close()`` raising in the
        finally AFTER a successful ``register()`` must not discard the
        ``(tumbler, True)`` return — return-in-try + raising-finally
        semantics would otherwise propagate the close error, the caller's
        boundary except would default ``catalog_row_minted=False``, and a
        row that WAS minted would become uncompensable."""
        from nexus.catalog.factory import make_catalog_writer as real_make
        from nexus.catalog.store_hook import (
            catalog_store_hook_tracked,
            rollback_minted_catalog_entry,
        )

        class _CloseBomb:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def close(self):
                self._inner.close()
                raise RuntimeError("writer close blew up post-register")

        with patch(
            "nexus.catalog.factory.make_catalog_writer",
            side_effect=lambda *a, **k: _CloseBomb(real_make(*a, **k)),
        ):
            tumbler, created = catalog_store_hook_tracked(
                title="b6enc-closebomb",
                doc_id="a1" * 32,
                collection_name="knowledge__x",
            )
        assert created is True and tumbler, (
            "a raising writer.close() must not swallow the minted "
            "(tumbler, True) return"
        )
        assert len(_catalog_rows(catalog_env, "b6enc-closebomb")) == 1

        # ...and the compensation the flag exists for still works.
        assert rollback_minted_catalog_entry(
            tumbler, original_error="simulated put failure",
        ) is True
        assert _catalog_rows(catalog_env, "b6enc-closebomb") == []


# ── Critic Sig 2 / CRE Minor 4: direct write + live hook coexistence ────────


class TestDirectPlusHookCoexistence:
    def test_double_write_converges_with_live_fire_batch(
        self, catalog_env: Path, local_t3: T3Database,
    ) -> None:
        """End-to-end store_put with ``fire_batch`` LIVE (not mocked):
        the direct ``store_put_manifest_direct`` write followed by the
        best-effort ``manifest_write_batch_hook`` re-write must converge
        — manifest rows are EXACTLY the expected set (no dupes, no
        drops) and ``chunk_count`` is correct. Locks the production
        sequential-double-write path every other test in this file mocks
        away."""
        from nexus.mcp.core import store_put

        content = "coexistence double write content"
        with patch("nexus.mcp.core._get_t3", return_value=local_t3), \
             patch("nexus.mcp.core._hooks.fire_single", side_effect=_no_op), \
             patch("nexus.mcp.core._hooks.fire_document", side_effect=_no_op), \
             patch("nexus.mcp.core._catalog_auto_link", return_value=0):
            result = store_put(
                content=content, collection="knowledge",
                title="b6enc-coexist",
            )
        assert result.startswith("Stored:"), result

        rows = _catalog_rows(catalog_env, "b6enc-coexist")
        assert len(rows) == 1
        tumbler, chunk_count = rows[0]
        assert chunk_count == 1, (
            f"chunk_count must converge to 1 after direct+hook writes, "
            f"got {chunk_count}"
        )
        chash = hashlib.sha256(content.encode()).hexdigest()
        manifest = [r[0] for r in _manifest_rows(catalog_env, tumbler)]
        assert manifest == [chash], (
            f"manifest rows must be exactly the expected set after the "
            f"double write, got {manifest}"
        )


# ── C4: store_delete asymmetry ───────────────────────────────────────────────


class TestStoreDeleteAsymmetry:
    def test_delete_removes_store_put_origin_catalog_row(
        self, catalog_env: Path, local_t3: T3Database,
    ) -> None:
        content = "delete me cleanly"
        put_result = _mcp_store_put_with(local_t3, content, "b6enc-del")
        assert put_result.startswith("Stored:"), put_result
        rows = _catalog_rows(catalog_env, "b6enc-del")
        assert len(rows) == 1
        tumbler = rows[0][0]

        from nexus.mcp.core import store_delete
        chash = hashlib.sha256(content.encode()).hexdigest()
        with patch("nexus.mcp.core._get_t3", return_value=local_t3):
            del_result = store_delete(chash, collection="knowledge")
        assert del_result.startswith("Deleted:"), del_result
        assert "WARNING" not in del_result

        assert _catalog_rows(catalog_env, "b6enc-del") == [], (
            "store_delete must not leave a catalog row outliving its chunks"
        )
        assert _manifest_rows(catalog_env, tumbler) == []

    def test_delete_leaves_file_backed_docs_alone(
        self, catalog_env: Path, local_t3: T3Database,
    ) -> None:
        """A non-store_put-origin doc (file_path set) sharing the chunk
        id must survive — cleanup is scoped to the store_put signature."""
        content = "file backed content"
        chash = hashlib.sha256(content.encode()).hexdigest()
        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        cat.register(
            owner, "b6enc-filebacked", content_type="prose",
            file_path="notes/file.md",
            physical_collection="knowledge__knowledge__voyage-context-3__v1",
            meta={"doc_id": chash},
        )
        cat._db.close()

        col = "knowledge__knowledge__voyage-context-3__v1"
        local_t3.put(collection=col, content=content, title="b6enc-filebacked")

        from nexus.mcp.core import store_delete
        with patch("nexus.mcp.core._get_t3", return_value=local_t3):
            del_result = store_delete(chash, collection="knowledge")
        assert del_result.startswith("Deleted:"), del_result
        assert len(_catalog_rows(catalog_env, "b6enc-filebacked")) == 1, (
            "file-backed (indexer-origin) rows are out of scope for the "
            "store_delete cleanup"
        )
