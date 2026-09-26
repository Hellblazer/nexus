# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-b6enc (GH #1419 Issue 8): silent store_put data loss — client side.

Four seams, all locked here:

- **C2 ghost-register compensation**: both MCP ``store_put`` and CLI
  ``nx store put`` register the catalog row BEFORE ``t3.put``. A put
  failure must delete the row minted IN THIS CALL (never a pre-existing
  dedup target) and still surface the original error.
- **C3 manifest leg out of best-effort**: the manifest write no longer
  rides the swallowing ``fire_batch`` chain for the store_put producer;
  it is called directly and VERIFIED. Failure yields an explicit error,
  never a bare "Stored:" — updated by RDR-192 Step 3a (nexus-wbfpw.28,
  Sam's ruling 2026-09-26) to roll back the chunk it just wrote rather
  than the original "stored ... but NOT cataloged" result that left it
  recoverable in T3; see the "RDR-192 Step 3a" section below for the
  catalog-registration-failure half of that same contract, the
  concurrent-identical-content race guard, and the recovery-bundle
  importer's own two failure legs.
- **C4 delete asymmetry**: MCP ``store_delete`` removes the
  store_put-origin catalog row (manifest cascades) so no row survives
  with a stale chunk_count.
- Success parity: chunk_count == manifest count == T3 chunks, even with
  every fire_* chain dead (the manifest leg is independent now).

Tests use the live (service) catalog via the same factories the hooks use
+ a real in-memory T3; mocks appear only at the failure-injection points,
per the integration-over-mocks rule. (nexus-i711w terminal deletion: the
local-Catalog seeding/readback arm this file used is gone — seeding goes
through ActiveCatalog and readback through the active reader.)
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction

from nexus.catalog.tumbler import Tumbler
from nexus.db.t3 import T3Database
from tests.conftest import make_vector_test_client
from tests._catalog_fixture_ops import ActiveCatalog, documents_by_title


@pytest.fixture
def catalog_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # nexus-i711w: no local init — the hooks and the assertions both resolve
    # the live service catalog through the factories.
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
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
    """(tumbler, chunk_count) rows for *title*, via the active reader.

    nexus-i711w: replaces the raw local-SQLite ``SELECT tumbler,
    chunk_count FROM documents WHERE title = ?`` readback.
    """
    return [
        (str(d.tumbler), d.chunk_count) for d in documents_by_title(title)
    ]


def _manifest_rows(catalog_env: Path, tumbler: str) -> list:
    """1-tuples of manifest chashes for *tumbler*, via the active reader.

    nexus-i711w: replaces the raw local-SQLite ``SELECT chash FROM
    document_chunks WHERE doc_id = ?`` readback; tuple shape kept so
    call sites did not change.
    """
    from tests._catalog_fixture_ops import active_reader

    return [(c,) for c in active_reader().get_chunk_chashes(tumbler)]


def _no_op(*args, **kwargs):
    pass


def _seed_for_store_put(t3, content: str, collection: str = "fixture-subject") -> None:
    """Pre-seed a REAL ``nexus.chunks`` row for what a store_put-shaped
    write (MCP ``store_put``, CLI ``nx store put``, ``nx memory promote``)
    is about to write (nexus-dbzxb, RDR-191 Phase 5 Python collateral).

    This file's ``local_t3`` / ad hoc ``T3Database(_client=make_vector_
    test_client(), ...)`` fixtures inject a FAKE in-memory T3 client (see
    the module docstring: "a real in-memory T3" — real logic, fake
    substrate), but the manifest write (``store_put_manifest_direct``,
    C3's direct call, independent of the hook chain) always goes through
    the REAL engine catalog (autouse ``_pin_t2_substrate``).
    ``fk_catalog_chunks_chunk`` now requires the manifest's chash to have
    a matching REAL ``nexus.chunks`` row, which the fake T3 client can
    never provide.

    Computes the exact ``(collection, chash)`` the production write will
    use via the same derivation production code uses
    (``t3_collection_name`` / ``sha256(content)``), then seeds a real stub
    chunk — idiom 1/2. Nothing under test reads this row's content.
    """
    from nexus.corpus import t3_collection_name
    from tests._catalog_fixture_ops import seed_manifest_chunks

    col_name = t3_collection_name(collection, t3=t3)
    chash = hashlib.sha256(content.encode()).hexdigest()
    seed_manifest_chunks(col_name, [chash])


def _mcp_store_put_with(t3, content: str, title: str) -> str:
    from nexus.mcp.core import store_put

    with patch("nexus.mcp.core._get_t3", return_value=t3), \
         patch("nexus.mcp.core._hooks.fire_single", side_effect=_no_op), \
         patch("nexus.mcp.core._hooks.fire_batch", side_effect=_no_op), \
         patch("nexus.mcp.core._hooks.fire_document", side_effect=_no_op), \
         patch("nexus.mcp.core._catalog_auto_link", return_value=0):
        return store_put(content=content, collection="fixture-subject", title=title)


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
        cat = ActiveCatalog()
        owner = cat.register_owner("knowledge", "curator")
        cat.register(
            owner, "b6enc-dedup-mcp", content_type="knowledge",
            physical_collection="knowledge__fixture-subject__bge-base-en-v15-768__v1",
            meta={"doc_id": chash},
        )

        result = _mcp_store_put_with(_FailingT3(), content, "b6enc-dedup-mcp")
        assert result.startswith("Error"), result
        assert len(_catalog_rows(catalog_env, "b6enc-dedup-mcp")) == 1, (
            "pre-existing deduped row must survive the compensation"
        )

    def test_dedup_hit_then_put_failure_stamps_fence_failed(
        self, catalog_env: Path,
    ) -> None:
        """nexus-vw594 F2 fix-round IMPORTANT (code-review-expert, T1
        scratch d9173ec9): the exact wedge the reviewer named — a
        dedup-hit store_put (``catalog_row_minted=False``, so the C2
        rollback above never fires; the row is pre-existing, not a
        ghost) whose ``t3.put`` then fails. ``_fence_begin`` already
        fired before ``t3.put`` (F2); without a matching ``_fence_fail``
        in this except path the surviving row was stranded at
        ``index_state='indexing'`` forever, with only the 6h doctor
        sweep as signal. Asserts the row survives (same as the sibling
        test above) AND its fence is stamped ``'failed'``, not left
        dangling.
        """
        content = "dedup content then put fails"
        chash = hashlib.sha256(content.encode()).hexdigest()
        cat = ActiveCatalog()
        owner = cat.register_owner("knowledge", "curator")
        cat.register(
            owner, "b6enc-dedup-fence-mcp", content_type="knowledge",
            physical_collection="knowledge__fixture-subject__bge-base-en-v15-768__v1",
            meta={"doc_id": chash},
        )

        result = _mcp_store_put_with(_FailingT3(), content, "b6enc-dedup-fence-mcp")
        assert result.startswith("Error"), result
        rows = documents_by_title("b6enc-dedup-fence-mcp")
        assert len(rows) == 1, "pre-existing deduped row must survive the compensation"
        assert rows[0].index_state == "failed", (
            f"expected the fence to be stamped 'failed' after a dedup-hit "
            f"t3.put failure, got {rows[0].index_state!r} — a surviving "
            f"row stuck at 'indexing' forever is exactly the wedge this "
            f"test guards against"
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


# ── nexus-vfef0: race-loser created=False must skip rollback (MCP) ──────────


class _RaceLoserWriter:
    """Wraps the real service-mode writer, forcing ``register()`` to report
    ``created=False`` — as if THIS call were the concurrent first-put race
    LOSER — while still performing the real registration underneath, so a
    real row lands in the DB and the test can prove it survives.

    Delegates every other attribute (including ``register_owner``,
    ``update``, ``close``) to the real writer via ``__getattr__``, same
    pattern as ``TestWriterCloseGuard._CloseBomb`` above.
    """

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def register(self, *args, **kwargs):
        result = self._inner.register(*args, **kwargs)
        if kwargs.get("with_created"):
            tumbler, _created = result
            return tumbler, False
        return result


class TestVfef0RaceLoserSkipsRollback:
    def test_race_loser_created_false_never_triggers_rollback(
        self, catalog_env: Path,
    ) -> None:
        """nexus-vfef0 regression: when ``writer.register(with_created=True)``
        reports ``created=False`` (the concurrent first-put race-loser leg),
        a subsequent ``t3.put`` failure must NOT invoke
        ``rollback_minted_catalog_entry`` — the returned row belongs to a
        concurrent WINNER, not this call.

        Pre-fix, this leg was hardcoded ``created=True`` in
        ``catalog_store_hook_tracked`` (the wire carried no created-vs-
        matched signal at all), so the same failure would have deleted the
        winner's live row out from under it. This pins the literal bug
        scenario at the Python layer, one level up from the engine-side
        ``created`` contract already covered by
        ``CatalogRepositoryTest``/``Catalog016SourceUriUniqueTest``.
        """
        from nexus.catalog.factory import make_catalog_writer as real_make

        rollback_calls: list[str] = []

        def _tracking_rollback(tumbler, *, original_error=""):
            rollback_calls.append(tumbler)
            # Never actually delete — this test's assertion IS that it's
            # never called; a real delete here would also mask a bug.
            return False

        with patch(
            "nexus.catalog.factory.make_catalog_writer",
            side_effect=lambda *a, **k: _RaceLoserWriter(real_make(*a, **k)),
        ), patch(
            "nexus.catalog.store_hook.rollback_minted_catalog_entry",
            side_effect=_tracking_rollback,
        ):
            result = _mcp_store_put_with(
                _FailingT3(), "race loser content", "b6enc-race-loser",
            )

        assert result.startswith("Error"), result
        assert rollback_calls == [], (
            "created=False (race-loser leg) must never trigger "
            "rollback_minted_catalog_entry — got calls for: "
            f"{rollback_calls}"
        )
        # The row this call actually registered (via the wrapped real
        # writer) must survive untouched — from this call's perspective it
        # is indistinguishable from a genuine dedup hit.
        assert len(_catalog_rows(catalog_env, "b6enc-race-loser")) == 1, (
            "the race-loser leg's created=False must leave the registered "
            "row in place, exactly like a genuine dedup hit"
        )


# ── C3: manifest leg fail-loud (MCP) ─────────────────────────────────────────


class TestMcpManifestFailLoud:
    def test_manifest_failure_rolls_back_and_returns_error(
        self, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RDR-192 Step 3a (nexus-wbfpw.28, superseding this file's old
        'stored but NOT cataloged, content left in T3' contract — Sam's
        ruling 2026-09-26: rollback, not a marker column). A failed
        manifest write must delete the chunk it just wrote, not leave it
        as a live, manifest-less T3 row."""
        monkeypatch.setattr(
            "nexus.catalog.store_hook.store_put_manifest_direct",
            lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("manifest write refused")
            ),
        )
        result = _mcp_store_put_with(
            local_t3, "manifest fail content", "b6enc-manifest-mcp",
        )
        assert result.startswith("Error"), result
        assert "manifest write refused" in result
        assert "Stored:" not in result, (
            "a manifest failure must never produce a bare 'Stored:' result"
        )
        # RDR-192 Step 3a: the chunk is rolled back, not left recoverable
        # in T3 — a manifest-less current note is exactly the shape the
        # census flags and a future reaper would remove anyway.
        chash = hashlib.sha256(b"manifest fail content").hexdigest()
        cols = [c["name"] for c in local_t3.list_collections()
                if c["name"].startswith("knowledge__")]
        assert cols, "expected the knowledge collection to exist in T3"
        assert local_t3.get_by_id(cols[0], chash) is None, (
            "a failed manifest write must roll back the chunk it just "
            "wrote, not leave a manifest-less orphan in T3"
        )
        # fix-round 1 Important (both reviewers): the catalog row THIS
        # call minted must also be rolled back on a manifest failure,
        # mirroring the sibling t3.put-failure branch (TestMcpGhost
        # RegisterCompensation above) exactly — a ghost row (chunk_count=0,
        # zero manifest, zero chunks) is not an acceptable residual.
        assert _catalog_rows(catalog_env, "b6enc-manifest-mcp") == [], (
            "a failed manifest write must roll back the catalog row this "
            "call minted, not leave a chunk_count=0 ghost behind"
        )

    def test_success_counts_align_without_fire_batch(
        self, catalog_env: Path, local_t3: T3Database,
    ) -> None:
        """chunk_count == manifest count == 1 == T3 chunks, with every
        fire_* chain dead — the manifest leg no longer rides the
        swallowing hook chain (C3's exact silent-drift mechanism)."""
        content = "healthy store_put content"
        _seed_for_store_put(local_t3, content)
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
        from nexus.catalog.http_catalog_client import HttpCatalogClient
        from nexus.catalog.store_hook import store_put_manifest_direct

        cat = ActiveCatalog()
        owner = cat.register_owner("knowledge", "curator")
        t = cat.register(
            owner, "verify-target", content_type="knowledge",
            physical_collection="knowledge__x",
            meta={"doc_id": "f" * 64},
        )

        # nexus-i711w: the write path is the service client now — no-op the
        # same two whitelisted write ops on it (was Catalog.*).
        monkeypatch.setattr(
            HttpCatalogClient, "atomic_manifest_replace",
            lambda self, d, c, *, collection: None,
        )
        monkeypatch.setattr(
            HttpCatalogClient, "resync_chunk_count_cache",
            lambda self, d: None,
        )
        with pytest.raises(RuntimeError, match="did not land"):
            store_put_manifest_direct(str(t), [{
                "chunk_text_hash": "f" * 64,
                "chunk_start_char": 0,
                "chunk_end_char": 10,
            }], collection="knowledge__x")

    def test_empty_metadata_raises(self, catalog_env: Path) -> None:
        from nexus.catalog.store_hook import store_put_manifest_direct

        with pytest.raises(RuntimeError, match="nothing to catalog"):
            store_put_manifest_direct("1.2.3", [{}], collection="knowledge__x")

    def test_blank_doc_id_is_a_no_op(self) -> None:
        """No catalog tumbler (no-catalog / opt-out path): nothing to
        write, nothing to verify — must not raise."""
        from nexus.catalog.store_hook import store_put_manifest_direct

        store_put_manifest_direct(
            "", [{"chunk_text_hash": "a" * 64}], collection="knowledge__x")


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
                "--collection", "fixture-subject",
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

    def test_manifest_failure_rolls_back_and_is_explicit_error(
        self, catalog_env: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RDR-192 Step 3a (nexus-wbfpw.28): superseding this file's old
        'stored but NOT cataloged, content left in T3' contract — a
        failed manifest write now rolls back the chunk it just wrote."""
        import nexus.commands.store as store_mod

        local = T3Database(
            _client=make_vector_test_client(),
            _ef_override=DefaultEmbeddingFunction(),
        )
        monkeypatch.setattr(
            store_mod, "_store_put_manifest_direct_with_recovery",
            lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("manifest write refused")
            ),
        )
        result = self._invoke(
            tmp_path, local, "b6enc-manifest-cli", "cli manifest fail",
        )
        assert result.exit_code != 0
        assert "manifest write refused" in result.output
        assert "Stored:" not in result.output
        chash = hashlib.sha256(b"cli manifest fail").hexdigest()
        cols = [c["name"] for c in local.list_collections()
                if c["name"].startswith("knowledge__")]
        assert cols, "expected the knowledge collection to exist in T3"
        assert local.get_by_id(cols[0], chash) is None, (
            "a failed manifest write must roll back the chunk it just "
            "wrote, not leave a manifest-less orphan in T3"
        )
        assert _catalog_rows(catalog_env, "b6enc-manifest-cli") == [], (
            "a failed manifest write must roll back the catalog row this "
            "call minted, not leave a chunk_count=0 ghost behind"
        )

    def test_success_echoes_stored(
        self, catalog_env: Path, tmp_path: Path,
    ) -> None:
        local = T3Database(
            _client=make_vector_test_client(),
            _ef_override=DefaultEmbeddingFunction(),
        )
        _seed_for_store_put(local, "cli healthy content")
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
                "--collection", "fixture-subject",
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
        cat = ActiveCatalog()
        owner = cat.register_owner("knowledge", "curator")
        cat.register(
            owner, "b6enc-dedup-promote", content_type="knowledge",
            physical_collection="knowledge__fixture-subject__bge-base-en-v15-768__v1",
            meta={"doc_id": chash},
        )

        result = self._invoke_promote(
            tmp_path, _FailingT3(), "b6enc-dedup-promote", content,
        )
        assert result.exit_code != 0
        assert len(_catalog_rows(catalog_env, "b6enc-dedup-promote")) == 1, (
            "pre-existing deduped row must survive promote's compensation"
        )

    def test_manifest_failure_rolls_back_and_surfaces_loudly(
        self, catalog_env: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RDR-192 Step 3a (nexus-wbfpw.28): superseding this file's old
        'stored but NOT cataloged, content left in T3' contract — a
        failed manifest write now rolls back the chunk it just wrote."""
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
        assert "manifest write refused" in result.output
        assert "Promoted:" not in result.output, (
            "a manifest failure must never produce a bare 'Promoted:' echo"
        )
        chash = hashlib.sha256(b"promote manifest fail").hexdigest()
        cols = [c["name"] for c in local.list_collections()
                if c["name"].startswith("knowledge__")]
        assert cols, "expected the knowledge collection to exist in T3"
        assert local.get_by_id(cols[0], chash) is None, (
            "a failed manifest write must roll back the chunk it just "
            "wrote, not leave a manifest-less orphan in T3"
        )
        assert _catalog_rows(catalog_env, "b6enc-manifest-promote") == [], (
            "a failed manifest write must roll back the catalog row this "
            "call minted, not leave a chunk_count=0 ghost behind"
        )

    def test_success_counts_align(
        self, catalog_env: Path, tmp_path: Path,
    ) -> None:
        local = T3Database(
            _client=make_vector_test_client(),
            _ef_override=DefaultEmbeddingFunction(),
        )
        content = "healthy promote content"
        _seed_for_store_put(local, content)
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
        _seed_for_store_put(local_t3, content)
        with patch("nexus.mcp.core._get_t3", return_value=local_t3), \
             patch("nexus.mcp.core._hooks.fire_single", side_effect=_no_op), \
             patch("nexus.mcp.core._hooks.fire_document", side_effect=_no_op), \
             patch("nexus.mcp.core._catalog_auto_link", return_value=0):
            result = store_put(
                content=content, collection="fixture-subject",
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
        _seed_for_store_put(local_t3, content)
        put_result = _mcp_store_put_with(local_t3, content, "b6enc-del")
        assert put_result.startswith("Stored:"), put_result
        rows = _catalog_rows(catalog_env, "b6enc-del")
        assert len(rows) == 1
        tumbler = rows[0][0]

        from nexus.mcp.core import store_delete
        chash = hashlib.sha256(content.encode()).hexdigest()
        with patch("nexus.mcp.core._get_t3", return_value=local_t3):
            del_result = store_delete(chash, collection="fixture-subject")
        assert del_result.startswith("Deleted:"), del_result
        assert "WARNING" not in del_result

        # nexus-tz1cx FIXED by nexus-5axey: store_delete_catalog_cleanup now
        # resolves the chash via resolve_knowledge_doc_for_chash
        # (docs_for_chashes-backed) instead of by_doc_id (TUMBLER-only on
        # the service client, which always mismatched a chash and made the
        # compensation a silent no-op). The catalog row + its manifest are
        # both gone.
        assert _catalog_rows(catalog_env, "b6enc-del") == []
        assert _manifest_rows(catalog_env, tumbler) == []

    def test_delete_leaves_file_backed_docs_alone(
        self, catalog_env: Path, local_t3: T3Database,
    ) -> None:
        """A non-store_put-origin doc (file_path set) sharing the chunk
        id must survive — cleanup is scoped to the store_put signature."""
        content = "file backed content"
        chash = hashlib.sha256(content.encode()).hexdigest()
        cat = ActiveCatalog()
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        cat.register(
            owner, "b6enc-filebacked", content_type="prose",
            file_path="notes/file.md",
            physical_collection="knowledge__fixture-subject__bge-base-en-v15-768__v1",
            meta={"doc_id": chash},
        )

        col = "knowledge__fixture-subject__bge-base-en-v15-768__v1"
        local_t3.put(collection=col, content=content, title="b6enc-filebacked")

        from nexus.mcp.core import store_delete
        with patch("nexus.mcp.core._get_t3", return_value=local_t3):
            del_result = store_delete(chash, collection="fixture-subject")
        assert del_result.startswith("Deleted:"), del_result
        assert len(_catalog_rows(catalog_env, "b6enc-filebacked")) == 1, (
            "file-backed (indexer-origin) rows are out of scope for the "
            "store_delete cleanup"
        )


# ── RDR-192 Step 3a (nexus-wbfpw.28): rollback on failed catalog OR ─────────
# ── manifest write, every store_put-shaped producer ─────────────────────────
#
# Sam's ruling 2026-09-26: rollback, not a marker column. A failed catalog
# registration (catalog_store_hook_tracked returns ("", False)) or a failed
# direct manifest write must delete the chunk the call just wrote — never
# leave a live, manifest-less T3 row (the census's no-owner / legacy-
# unmanifested shape) and never return a bare "Stored:"/"Promoted:". The
# manifest-failure half of this contract is now pinned above (this file's
# three *_rolls_back_* tests, superseding the old "stored but NOT cataloged,
# content left in T3" assertions); this section adds the catalog-
# registration-failure half, the plan-audit round 2 concurrent-identical-
# content race guard, and the recovery-bundle importer's own two failure
# legs.


class TestWbfpw28McpCatalogRegistrationFailure:
    def test_registration_failure_rolls_back_and_returns_error(
        self, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "nexus.catalog.store_hook.catalog_store_hook_tracked",
            lambda *a, **k: ("", False),
        )
        content = "wbfpw28 registration fail content mcp"
        result = _mcp_store_put_with(local_t3, content, "wbfpw28-reg-mcp")
        assert result.startswith("Error"), result
        assert "catalog registration failed" in result
        assert "Stored:" not in result
        assert _catalog_rows(catalog_env, "wbfpw28-reg-mcp") == [], (
            "a failed registration must never leave a catalog row"
        )
        chash = hashlib.sha256(content.encode()).hexdigest()
        cols = [c["name"] for c in local_t3.list_collections()
                if c["name"].startswith("knowledge__")]
        assert cols, "expected the knowledge collection to exist in T3"
        assert local_t3.get_by_id(cols[0], chash) is None, (
            "a failed registration must roll back the chunk t3.put just "
            "wrote, not leave a no-owner orphan in T3"
        )


class TestWbfpw28CliCatalogRegistrationFailure:
    """Mirrors ``TestCliStorePut``'s ``_invoke`` plumbing locally (never
    subclasses a test class — that would re-collect every inherited test
    method under this class's name too)."""

    def _invoke(self, tmp_path: Path, t3, title: str, content: str):
        from click.testing import CliRunner

        from nexus.cli import main

        f = tmp_path / "note.md"
        f.write_text(content)
        with patch("nexus.commands.store._t3", lambda: t3):
            return CliRunner().invoke(main, [
                "store", "put", str(f),
                "--collection", "fixture-subject",
                "--title", title,
            ])

    def test_registration_failure_rolls_back_and_is_explicit_error(
        self, catalog_env: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        local = T3Database(
            _client=make_vector_test_client(),
            _ef_override=DefaultEmbeddingFunction(),
        )
        # commands/store.py imports catalog_store_hook_tracked ONCE at
        # module load (a module-level alias, not a deferred per-call
        # import like the MCP/promote/recovery-bundle paths) — patch the
        # local alias, not the source module's attribute.
        monkeypatch.setattr(
            "nexus.commands.store._catalog_store_hook_tracked",
            lambda *a, **k: ("", False),
        )
        content = "wbfpw28 registration fail content cli"
        result = self._invoke(tmp_path, local, "wbfpw28-reg-cli", content)
        assert result.exit_code != 0
        assert "catalog registration failed" in result.output
        assert "Stored:" not in result.output
        assert _catalog_rows(catalog_env, "wbfpw28-reg-cli") == []
        chash = hashlib.sha256(content.encode()).hexdigest()
        cols = [c["name"] for c in local.list_collections()
                if c["name"].startswith("knowledge__")]
        assert cols, "expected the knowledge collection to exist in T3"
        assert local.get_by_id(cols[0], chash) is None


class TestWbfpw28PromoteCatalogRegistrationFailure:
    """Mirrors ``TestPromoteGhostRegisterCompensation``'s
    ``_invoke_promote`` plumbing locally, same reason as the CLI
    counterpart above."""

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
                "--collection", "fixture-subject",
            ])

    def test_registration_failure_rolls_back_and_surfaces_loudly(
        self, catalog_env: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        local = T3Database(
            _client=make_vector_test_client(),
            _ef_override=DefaultEmbeddingFunction(),
        )
        monkeypatch.setattr(
            "nexus.catalog.store_hook.catalog_store_hook_tracked",
            lambda *a, **k: ("", False),
        )
        content = "wbfpw28 registration fail content promote"
        result = self._invoke_promote(
            tmp_path, local, "wbfpw28-reg-promote", content,
        )
        assert result.exit_code != 0
        assert "catalog registration failed" in result.output
        assert "Promoted:" not in result.output
        assert _catalog_rows(catalog_env, "wbfpw28-reg-promote") == []
        chash = hashlib.sha256(content.encode()).hexdigest()
        cols = [c["name"] for c in local.list_collections()
                if c["name"].startswith("knowledge__")]
        assert cols, "expected the knowledge collection to exist in T3"
        assert local.get_by_id(cols[0], chash) is None


class TestWbfpw28ConcurrentIdenticalContentRace:
    """Plan-audit round 2 residual: identical chunk text collapses to ONE
    T3 row (CLAUDE.md § catalog/T3 split), so the rollback's union guard
    must never delete a chash a DIFFERENT, already-succeeded store still
    depends on. Simulated sequentially (store A completes fully, THEN
    store B — same content, forced registration failure — attempts and
    rolls back), which is the deterministic shape a genuine concurrent
    race collapses to once the winner's manifest has landed by the time
    the loser's rollback runs."""

    def test_surviving_stores_chunk_is_not_deleted_by_the_failed_one(
        self, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        content = "wbfpw28 race shared identical content"
        chash = hashlib.sha256(content.encode()).hexdigest()
        _seed_for_store_put(local_t3, content)

        # Store A: succeeds fully — real registration, real manifest write.
        result_a = _mcp_store_put_with(local_t3, content, "wbfpw28-race-a")
        assert result_a.startswith("Stored:"), result_a
        rows_a = _catalog_rows(catalog_env, "wbfpw28-race-a")
        assert len(rows_a) == 1
        tumbler_a, chunk_count_a = rows_a[0]
        assert chunk_count_a == 1
        assert [r[0] for r in _manifest_rows(catalog_env, tumbler_a)] == [chash]

        # Store B: SAME content, DIFFERENT title, registration forced to
        # fail — must roll back only if the chash is truly unreferenced.
        # It is not: A's manifest already references it.
        monkeypatch.setattr(
            "nexus.catalog.store_hook.catalog_store_hook_tracked",
            lambda *a, **k: ("", False),
        )
        result_b = _mcp_store_put_with(local_t3, content, "wbfpw28-race-b")
        assert result_b.startswith("Error"), result_b
        assert _catalog_rows(catalog_env, "wbfpw28-race-b") == []

        # A's chunk and manifest must both survive B's rollback attempt.
        assert local_t3.get_by_id(
            "knowledge__fixture-subject__bge-base-en-v15-768__v1", chash,
        ) is not None, (
            "the union guard must never delete a chash a different, "
            "already-succeeded store's manifest still references"
        )
        assert [r[0] for r in _manifest_rows(catalog_env, tumbler_a)] == [chash]


# ── RDR-192 Step 3a: recovery-bundle importer ────────────────────────────────


class TestWbfpw28RecoveryBundleRollback:
    """``catalog/recovery_bundle.py::_default_import_doc`` shares the same
    register -> t3.put -> manifest shape; it already re-raised a manifest
    failure (unlike the three callers above, pre-fix) but never rolled
    back the chunk, and never detected a blank ``catalog_doc_id`` at all."""

    def _rec(self, content: str, title: str) -> dict:
        return {
            "content": content,
            "collection": "fixture-subject",
            "title": title,
            "tags": "",
            "category": "",
        }

    def test_registration_failure_rolls_back_and_raises(
        self, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.catalog.recovery_bundle import _default_import_doc

        monkeypatch.setattr(
            "nexus.catalog.store_hook.catalog_store_hook_tracked",
            lambda *a, **k: ("", False),
        )
        content = "wbfpw28 recovery bundle registration fail"
        with pytest.raises(RuntimeError, match="catalog registration failed"):
            _default_import_doc(local_t3, self._rec(content, "wbfpw28-rb-reg"))
        assert _catalog_rows(catalog_env, "wbfpw28-rb-reg") == []
        chash = hashlib.sha256(content.encode()).hexdigest()
        cols = [c["name"] for c in local_t3.list_collections()
                if c["name"].startswith("knowledge__")]
        assert cols, "expected the knowledge collection to exist in T3"
        assert local_t3.get_by_id(cols[0], chash) is None

    def test_manifest_failure_rolls_back_and_raises(
        self, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.catalog.recovery_bundle import _default_import_doc

        monkeypatch.setattr(
            "nexus.catalog.store_hook.store_put_manifest_direct",
            lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("manifest write refused")
            ),
        )
        content = "wbfpw28 recovery bundle manifest fail"
        with pytest.raises(RuntimeError, match="manifest write refused"):
            _default_import_doc(local_t3, self._rec(content, "wbfpw28-rb-manifest"))
        chash = hashlib.sha256(content.encode()).hexdigest()
        cols = [c["name"] for c in local_t3.list_collections()
                if c["name"].startswith("knowledge__")]
        assert cols, "expected the knowledge collection to exist in T3"
        assert local_t3.get_by_id(cols[0], chash) is None, (
            "a failed manifest write must roll back the chunk it just "
            "wrote, not leave a manifest-less orphan in T3"
        )
        assert _catalog_rows(catalog_env, "wbfpw28-rb-manifest") == [], (
            "a failed manifest write must roll back the catalog row this "
            "call minted, not leave a chunk_count=0 ghost behind"
        )

    def test_success_is_unchanged(
        self, catalog_env: Path, local_t3: T3Database,
    ) -> None:
        from nexus.catalog.recovery_bundle import _default_import_doc

        content = "wbfpw28 recovery bundle healthy import"
        _seed_for_store_put(local_t3, content)
        _default_import_doc(local_t3, self._rec(content, "wbfpw28-rb-ok"))
        rows = _catalog_rows(catalog_env, "wbfpw28-rb-ok")
        assert len(rows) == 1
        tumbler, chunk_count = rows[0]
        assert chunk_count == 1
        chash = hashlib.sha256(content.encode()).hexdigest()
        assert [r[0] for r in _manifest_rows(catalog_env, tumbler)] == [chash]


# ── RDR-192 Step 3a fix-round 1 (nexus-wbfpw.28): verify-failure ────────────
# ── over-deletion (critic Critical 1) ────────────────────────────────────────
#
# store_put_manifest_direct's verify step can fail for two DIFFERENT
# reasons that must never be handled the same way: (1) the verify READ
# SUCCEEDED and proved the expected chashes missing -- CONFIRMED not
# landed, safe to roll back; (2) the verify step's own infrastructure
# failed (no reader, or the read itself raised) -- UNKNOWN outcome, the
# write may have landed, must NOT roll back. Case 2 now raises
# ManifestVerifyUncertainError, a RuntimeError subclass, so callers can
# tell the two apart.


class TestWbfpw28ManifestVerifyUncertain:
    def test_verify_read_failure_raises_uncertain_not_confirmed(
        self, catalog_env: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unit-level, direct call to store_put_manifest_direct (mirrors
        TestStorePutManifestDirectUnit above): the WRITE succeeds for
        real (a real atomic_manifest_replace against the engine), then
        the SECOND make_catalog_reader() call (the verify read) is
        swapped for one whose get_manifest raises. Empirically confirms
        the write actually landed despite the verify failure -- exactly
        the state a caller must not treat as safe to delete."""
        from nexus.catalog.factory import make_catalog_reader as real_make_reader
        from nexus.catalog.store_hook import (
            _MANIFEST_VERIFY_RETRY_ATTEMPTS,
            ManifestVerifyUncertainError,
            store_put_manifest_direct,
        )
        from tests._catalog_fixture_ops import active_reader, seed_manifest_chunks

        chash = "a" * 64
        collection = "knowledge__fixture-subject__bge-base-en-v15-768__v1"
        cat = ActiveCatalog()
        owner = cat.register_owner("knowledge", "curator")
        t = cat.register(
            owner, "verify-uncertain-target", content_type="knowledge",
            physical_collection=collection,
            meta={"doc_id": chash},
        )
        seed_manifest_chunks(collection, [chash])

        calls = {"n": 0}

        class _VerifyReadBoom:
            def get_manifest(self, doc_id):
                raise RuntimeError("verify read boom")

        # Call 1 is store_put_manifest_direct's own nexus-bb6n2 "before"
        # read (best-effort, real reader is fine); the next
        # _MANIFEST_VERIFY_RETRY_ATTEMPTS calls are the bounded-retry
        # verify reads (fix-round 2, Decision 2) -- ALL of them must fail
        # to genuinely exhaust the retry budget and raise
        # ManifestVerifyUncertainError; a single-failure reader would
        # recover on the next attempt and the write would be (correctly)
        # reported as landed, never raising at all. Bounded rather than
        # permanent: this test's own post-assertion re-resolves a reader
        # afterward (via active_reader(), which shares this same patched
        # factory function) to empirically confirm the write landed, and
        # that read must succeed for real.
        _last_boom_call = 1 + _MANIFEST_VERIFY_RETRY_ATTEMPTS

        def _flaky_make_reader():
            calls["n"] += 1
            if 2 <= calls["n"] <= _last_boom_call:
                return _VerifyReadBoom()
            return real_make_reader()

        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader", _flaky_make_reader,
        )
        monkeypatch.setattr(
            "nexus.catalog.store_hook._manifest_verify_retry_sleep",
            lambda seconds: None,
        )
        with pytest.raises(ManifestVerifyUncertainError, match="verify failed"):
            store_put_manifest_direct(str(t), [{
                "chunk_text_hash": chash,
                "chunk_start_char": 0,
                "chunk_end_char": 10,
            }], collection=collection)

        # EMPIRICAL (critic Critical 1's "show it with a test"): the
        # manifest write committed for real, even though verify could not
        # observe it.
        landed = active_reader().get_chunk_chashes(str(t))
        assert chash in landed, (
            "the manifest write must have actually landed even though "
            "verify raised — this is the exact case a caller must treat "
            "as uncertain, not confirmed-failed"
        )

    def test_mcp_reports_uncertain_and_never_rolls_back(
        self, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Caller-side branch (MCP store_put, representative — the other
        three producers share the identical branch shape). When
        store_put_manifest_direct raises ManifestVerifyUncertainError,
        store_put must report the uncertainty, never claim "rolled
        back", and never even ATTEMPT a rollback delete."""
        from nexus.catalog.store_hook import ManifestVerifyUncertainError

        rollback_calls: list[tuple] = []

        def _rollback_must_not_be_called(*a, **k):
            rollback_calls.append((a, k))
            raise AssertionError(
                "rollback must never be attempted when the verify outcome "
                "is uncertain"
            )

        monkeypatch.setattr(
            "nexus.catalog.store_hook.store_put_manifest_direct",
            lambda *a, **k: (_ for _ in ()).throw(
                ManifestVerifyUncertainError("verify read failed: boom")
            ),
        )
        monkeypatch.setattr(
            "nexus.catalog.store_hook.rollback_uncataloged_chunk_write",
            _rollback_must_not_be_called,
        )
        content = "wbfpw28 verify uncertain content mcp"
        result = _mcp_store_put_with(local_t3, content, "wbfpw28-uncertain-mcp")

        assert result.startswith("Error"), result
        assert "confirm" in result.lower(), result
        # The message may honestly SAY "nothing was rolled back" (a true
        # negative statement) — what it must never do is CLAIM the chunk
        # WAS rolled back, a false positive since nothing was attempted.
        assert "the chunk was rolled back" not in result.lower(), (
            "an uncertain verify outcome must never claim the chunk WAS "
            "rolled back — it might not have been, and might not have "
            f"needed to be: {result!r}"
        )
        assert rollback_calls == [], (
            f"rollback must never be attempted on an uncertain verify "
            f"outcome, got calls: {rollback_calls!r}"
        )
        chash = hashlib.sha256(content.encode()).hexdigest()
        cols = [c["name"] for c in local_t3.list_collections()
                if c["name"].startswith("knowledge__")]
        assert cols, "expected the knowledge collection to exist in T3"
        assert local_t3.get_by_id(cols[0], chash) is not None, (
            "the chunk t3.put wrote must remain untouched when the "
            "outcome is uncertain"
        )


# ── RDR-192 Step 3a fix-round 1 (Significant 3): a GENUINELY interleaved ────
# ── race, not a sequential simulation ────────────────────────────────────────


class TestWbfpw28GenuineInterleavedRace:
    def test_bs_rollback_check_runs_before_as_manifest_write_commits(
        self, catalog_env: Path, t2_service_env: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Deterministic barrier seam (patched, not threaded/slept): B's
        union-guard check (orphaned_chashes) runs FIRST and (correctly,
        at that instant) finds the shared chash unreferenced; store A's
        real registration + real manifest write then runs to completion
        and commits for real; only THEN does B's actual delete call fire,
        against a chash that is by now live-referenced by A.

        Uses a REAL ``HttpVectorClient`` against the engine substrate
        (matching ``tests/test_bb6n2_supersede_reap.py``'s own low-level
        pattern) rather than the in-memory fake T3 the rest of this file
        uses — the engine's anti-join under test lives entirely on the
        server side (``PgVectorRepository.delete``, RDR-191 F10c) and an
        in-memory double cannot stand in for it.

        Establishes empirically what happens for THIS ordering (critic
        Significant 3 — the one the coordinator's own fix-round message
        names: "B's rollback check runs, then A's manifest write, then
        B's delete"): the engine's OWN anti-join excludes any chash a
        live manifest still references from the DELETE statement itself —
        no exception on either side, B's delete is simply a no-op for
        that chash (0 rows removed). A's manifest write in THIS ordering
        never touches chunk deletion at all (B's delete fires strictly
        after A's write has already landed), so A cannot fail spuriously
        FROM THIS SPECIFIC INTERLEAVING — no recovery-retry logic is
        needed for it.

        NOT covered by this test: the mirror-image ordering (B's delete
        completing BEFORE A's manifest write attempts to reference the
        chash — possible in true concurrent multi-process execution,
        where A's own manifest-write HTTP round trip could be the slower
        leg). In that ordering A's ``atomic_manifest_replace`` races a
        chash that genuinely no longer exists at INSERT time, which is a
        real ``fk_catalog_chunks_chunk`` foreign-key violation, not a
        silent no-op — a different interleaving with a different
        mechanism than this test's engine-anti-join case. That ordering
        IS addressed by this bead (fix-round 2, Decision (b)): see
        ``TestWbfpw28OppositeOrderingRecovery`` below, which drives it
        deterministically against the same ``orphaned_chashes`` seam in
        the opposite order and asserts
        :func:`store_put_manifest_direct_with_recovery`'s bounded
        re-put-and-retry recovers A's write."""
        import nexus.db.http_vector_client as hvc
        from nexus.catalog.store_hook import (
            catalog_store_hook_tracked,
            rollback_uncataloged_chunk_write,
            store_put_manifest_direct,
        )
        from nexus.indexer_utils import orphaned_chashes as real_orphaned_chashes
        from tests._catalog_fixture_ops import active_reader

        client = hvc.HttpVectorClient(tenant=t2_service_env)
        collection = "knowledge__wbfpw28-interleave__bge-base-en-v15-768__v1"
        content = "wbfpw28 genuinely interleaved race content"
        chash = hashlib.sha256(content.encode()).hexdigest()
        manifest_metadatas = [{
            "chunk_text_hash": chash, "chunk_start_char": 0,
            "chunk_end_char": len(content),
        }]

        # B writes the chunk first — the exact state put_note_pieces
        # would have left it in before reaching the rollback path.
        client.upsert_chunks_with_embeddings(
            collection, ids=[chash], documents=[content], embeddings=[],
            metadatas=[{"title": "wbfpw28-interleave-b", "chunk_text_hash": chash}],
        )

        order: list[str] = []
        tumbler_a_holder: list[str] = []

        def _interleaving_orphaned_chashes(reader, doc_id, candidates, *, collection=None):
            # B's read-then-act window: compute the (currently correct,
            # about-to-go-stale) verdict FIRST.
            verdict = real_orphaned_chashes(reader, doc_id, candidates, collection=collection)
            order.append("b_checked")
            assert chash in verdict, (
                "at this instant nothing yet references the shared "
                "chash — B's check is not wrong, just about to be stale"
            )
            # NOW let A run to completion for real — registration, chunk
            # write (an idempotent upsert onto the SAME physical row B
            # already wrote), and manifest write, all landing before B's
            # delete (below, after this function returns) ever fires.
            tumbler_a, _created = catalog_store_hook_tracked(
                title="wbfpw28-interleave-a", doc_id=chash, collection_name=collection,
            )
            client.upsert_chunks_with_embeddings(
                collection, ids=[chash], documents=[content], embeddings=[],
                metadatas=[{"title": "wbfpw28-interleave-a", "chunk_text_hash": chash}],
            )
            store_put_manifest_direct(tumbler_a, manifest_metadatas, collection=collection)
            tumbler_a_holder.append(tumbler_a)
            order.append("a_done")
            return verdict  # B proceeds on the now-STALE "unreferenced" verdict

        monkeypatch.setattr(
            "nexus.indexer_utils.orphaned_chashes", _interleaving_orphaned_chashes,
        )

        outcome = rollback_uncataloged_chunk_write(
            client, [chash], collection=collection, catalog_doc_id="",
        )

        assert order == ["b_checked", "a_done"], (
            "the interleave must run in this exact order for the test to "
            "prove anything about a stale read"
        )
        tumbler_a = tumbler_a_holder[0]

        # EMPIRICAL (critic Significant 3's "establish what happens"): the
        # engine's own anti-join protected the chunk — B's delete request,
        # even built from a stale "unreferenced" verdict, deleted NOTHING;
        # no exception fired on either side, it is a silent, safe no-op
        # at the SQL layer.
        assert outcome.deleted_count == 0, (
            f"the engine must refuse to delete a chash a live manifest "
            f"now references, even acting on a stale verdict; got "
            f"deleted_count={outcome.deleted_count}"
        )

        # A did not fail spuriously — its manifest write never touches
        # chunk deletion, so B's concurrent rollback attempt cannot
        # disturb it. No recovery/retry logic is needed on A's side.
        landed = active_reader().get_chunk_chashes(tumbler_a)
        assert chash in landed, "A's manifest must still reference the chunk"
        result = client.get_collection(collection).get(ids=[chash], include=[])
        assert chash in (result.get("ids") or []), (
            "the chunk itself must survive B's rollback attempt"
        )


# ── RDR-192 Step 3a fix-round 1 (Significant 4): honest rollback wording ────


class TestWbfpw28DescribeRollbackOutcome:
    """Direct unit coverage of describe_rollback_outcome's branches — no
    substrate needed, pure function of ChunkRollbackOutcome."""

    def test_fully_deleted_says_rolled_back_and_retry_safe(self) -> None:
        from nexus.catalog.store_hook import ChunkRollbackOutcome, describe_rollback_outcome

        outcome = ChunkRollbackOutcome(
            requested=("a",), attempted=("a",), deleted_count=1,
        )
        msg = describe_rollback_outcome(outcome)
        assert "rolled back" in msg
        assert "retry is safe" in msg

    def test_protected_says_left_in_place_not_rolled_back(self) -> None:
        from nexus.catalog.store_hook import ChunkRollbackOutcome, describe_rollback_outcome

        outcome = ChunkRollbackOutcome(requested=("a",), protected=("a",))
        msg = describe_rollback_outcome(outcome)
        assert "left in place" in msg
        assert "rolled back" not in msg

    def test_delete_error_never_claims_rolled_back_or_safe_retry(self) -> None:
        from nexus.catalog.store_hook import ChunkRollbackOutcome, describe_rollback_outcome

        outcome = ChunkRollbackOutcome(
            requested=("a",), attempted=("a",), delete_error="boom",
        )
        msg = describe_rollback_outcome(outcome)
        assert "rolled back" not in msg
        assert "retry is safe" not in msg
        assert "boom" in msg

    def test_partial_delete_names_the_split_and_does_not_claim_full_success(self) -> None:
        from nexus.catalog.store_hook import ChunkRollbackOutcome, describe_rollback_outcome

        outcome = ChunkRollbackOutcome(
            requested=("a", "b"), attempted=("a", "b"), deleted_count=1,
        )
        msg = describe_rollback_outcome(outcome)
        assert "1 of 2" in msg
        assert "retry is safe" not in msg


# ── RDR-192 Step 3a fix-round 2 (nexus-wbfpw.28): a write-call exception ────
# ── is arbitrated via verify-read, never assumed confirmed-failed ───────────
#
# critic Critical / ship-blocker: atomic_manifest_replace raising does not
# by itself mean the write failed -- the POST may have committed
# server-side with only the acknowledgment lost. Every write-call
# exception must be arbitrated by a (retried) verify read before any
# confirmed-failure conclusion is drawn.


class TestWbfpw28WriteExceptionArbitration:
    def test_write_exception_but_landed_is_treated_as_success(
        self, catalog_env: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Lets the REAL atomic_manifest_replace land, then raises anyway
        (the exact ack-lost shape) -- store_put_manifest_direct must
        return normally (no exception raised at all), and the catalog
        row + manifest must survive untouched. Mirrors fix-round 1's
        TestWbfpw28ManifestVerifyUncertain technique (let the write
        commit for real, then break the NEXT step), applied here to the
        write call itself rather than the verify step."""
        from nexus.catalog.http_catalog_client import HttpCatalogClient
        from nexus.catalog.store_hook import store_put_manifest_direct
        from tests._catalog_fixture_ops import active_reader, seed_manifest_chunks

        chash = "c" * 64
        collection = "knowledge__fixture-subject__bge-base-en-v15-768__v1"
        cat = ActiveCatalog()
        owner = cat.register_owner("knowledge", "curator")
        t = cat.register(
            owner, "write-exc-landed-target", content_type="knowledge",
            physical_collection=collection, meta={"doc_id": chash},
        )
        seed_manifest_chunks(collection, [chash])

        real_replace = HttpCatalogClient.atomic_manifest_replace

        def _replace_then_raise_ack_lost(self, doc_id, chunks, *, collection):
            real_replace(self, doc_id, chunks, collection=collection)
            raise RuntimeError("simulated ack-lost network failure")

        monkeypatch.setattr(
            HttpCatalogClient, "atomic_manifest_replace", _replace_then_raise_ack_lost,
        )
        # No exception at all -- this IS the success path.
        store_put_manifest_direct(str(t), [{
            "chunk_text_hash": chash, "chunk_start_char": 0, "chunk_end_char": 10,
        }], collection=collection)

        assert chash in active_reader().get_chunk_chashes(str(t)), (
            "the write actually landed despite the raised exception -- "
            "must not have been rolled back"
        )
        assert len(_catalog_rows(catalog_env, "write-exc-landed-target")) == 1, (
            "the catalog row must survive: this was a success, not a "
            "confirmed failure"
        )

    def test_write_exception_and_confirmed_missing_raises_plain_error(
        self, catalog_env: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The write call raises AND no write ever actually happened
        (verify confirms missing) -- CONFIRMED failure. A plain
        RuntimeError, NOT ManifestMissingChunkError, since this is a
        generic failure, not the fk_catalog_chunks_chunk shape fix-round
        2 Decision (b) recovers from."""
        from nexus.catalog.http_catalog_client import HttpCatalogClient
        from nexus.catalog.store_hook import (
            ManifestMissingChunkError, store_put_manifest_direct,
        )

        chash = "d" * 64
        collection = "knowledge__fixture-subject__bge-base-en-v15-768__v1"
        cat = ActiveCatalog()
        owner = cat.register_owner("knowledge", "curator")
        t = cat.register(
            owner, "write-exc-missing-target", content_type="knowledge",
            physical_collection=collection, meta={"doc_id": chash},
        )

        monkeypatch.setattr(
            HttpCatalogClient, "atomic_manifest_replace",
            lambda self, d, c, *, collection: (_ for _ in ()).throw(
                RuntimeError("network down before commit")
            ),
        )
        with pytest.raises(RuntimeError, match="network down before commit") as exc_info:
            store_put_manifest_direct(str(t), [{
                "chunk_text_hash": chash, "chunk_start_char": 0, "chunk_end_char": 10,
            }], collection=collection)
        assert not isinstance(exc_info.value, ManifestMissingChunkError), (
            "a generic write failure (not an fk_catalog_chunks_chunk "
            "violation) must raise a plain RuntimeError, not "
            "ManifestMissingChunkError"
        )

    def test_write_exception_then_verify_exhausted_keeps_the_write_error(
        self, catalog_env: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round-3 code review: when the write call raises AND every
        arbitration verify read also fails, the outcome is uncertain (no
        rollback), but the write's own error must still reach the raised
        message. Without it the operator sees only the verify failure and
        nothing about why the write raised."""
        from nexus.catalog.http_catalog_client import HttpCatalogClient
        from nexus.catalog.store_hook import (
            ManifestVerifyUncertainError, store_put_manifest_direct,
        )

        chash = "e" * 64
        collection = "knowledge__fixture-subject__bge-base-en-v15-768__v1"
        cat = ActiveCatalog()
        owner = cat.register_owner("knowledge", "curator")
        t = cat.register(
            owner, "write-exc-verify-exhausted-target", content_type="knowledge",
            physical_collection=collection, meta={"doc_id": chash},
        )

        monkeypatch.setattr(
            HttpCatalogClient, "atomic_manifest_replace",
            lambda self, d, c, *, collection: (_ for _ in ()).throw(
                RuntimeError("write raised: gateway reset")
            ),
        )

        def _reader_down():
            raise RuntimeError("catalog read path down")

        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader", _reader_down,
        )
        monkeypatch.setattr(
            "nexus.catalog.store_hook._manifest_verify_retry_sleep",
            lambda _s: None,
        )
        with pytest.raises(ManifestVerifyUncertainError) as exc_info:
            store_put_manifest_direct(str(t), [{
                "chunk_text_hash": chash, "chunk_start_char": 0, "chunk_end_char": 10,
            }], collection=collection)
        message = str(exc_info.value)
        assert "catalog read path down" in message
        assert "write raised: gateway reset" in message, (
            "the write call's own error must survive into the uncertain "
            "outcome's message"
        )


# ── RDR-192 Step 3a fix-round 2, critic (a) / Decision 2: bounded ───────────
# ── verify-read retry ────────────────────────────────────────────────────────


class TestWbfpw28BoundedVerifyRetry:
    def test_recovers_on_retry(
        self, catalog_env: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.catalog.factory import make_catalog_reader as real_make_reader
        from nexus.catalog.store_hook import (
            _MANIFEST_VERIFY_RETRY_ATTEMPTS,
            _read_manifest_chashes_with_retry,
        )
        from tests._catalog_fixture_ops import seed_manifest_chunks

        assert _MANIFEST_VERIFY_RETRY_ATTEMPTS >= 2, (
            "test assumes at least 2 configured attempts"
        )
        chash = "b" * 64
        collection = "knowledge__fixture-subject__bge-base-en-v15-768__v1"
        cat = ActiveCatalog()
        owner = cat.register_owner("knowledge", "curator")
        t = cat.register(
            owner, "verify-retry-target", content_type="knowledge",
            physical_collection=collection, meta={"doc_id": chash},
        )
        seed_manifest_chunks(collection, [chash])
        cat.append_manifest_chunks(str(t), [{"chash": chash, "position": 0}], collection=collection)

        calls = {"n": 0}
        sleep_calls: list[float] = []

        class _FlakyOnceReader:
            def get_manifest(self, doc_id):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("transient blip")
                return real_make_reader().get_manifest(doc_id)

            @property
            def _db(self):
                raise RuntimeError("no direct db handle in service mode")

        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader", lambda: _FlakyOnceReader(),
        )
        monkeypatch.setattr(
            "nexus.catalog.store_hook._manifest_verify_retry_sleep",
            lambda seconds: sleep_calls.append(seconds),
        )
        landed = _read_manifest_chashes_with_retry(str(t), context="test")
        assert chash in landed
        assert calls["n"] == 2, "must succeed on the second attempt, not loop further"
        assert len(sleep_calls) == 1, (
            "exactly one backoff between the failed 1st and successful "
            "2nd attempt — no real sleep, since the patched function "
            "recorded the call instead of sleeping"
        )

    def test_exhausts_to_uncertain(
        self, catalog_env: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.catalog.store_hook import (
            _MANIFEST_VERIFY_RETRY_ATTEMPTS,
            ManifestVerifyUncertainError,
            _read_manifest_chashes_with_retry,
        )

        sleep_calls: list[float] = []

        class _AlwaysFailsReader:
            def get_manifest(self, doc_id):
                raise RuntimeError("permanently down")

            @property
            def _db(self):
                raise RuntimeError("no direct db handle in service mode")

        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader", lambda: _AlwaysFailsReader(),
        )
        monkeypatch.setattr(
            "nexus.catalog.store_hook._manifest_verify_retry_sleep",
            lambda seconds: sleep_calls.append(seconds),
        )
        with pytest.raises(
            ManifestVerifyUncertainError,
            match=f"after {_MANIFEST_VERIFY_RETRY_ATTEMPTS} attempts",
        ):
            _read_manifest_chashes_with_retry("1.2.3", context="test")
        assert len(sleep_calls) == _MANIFEST_VERIFY_RETRY_ATTEMPTS - 1, (
            "no sleep before the FIRST attempt — only between attempts"
        )


# ── RDR-192 Step 3a fix-round 2, Decision (b): the opposite-ordering race ───
# ── — B's delete lands BEFORE A's manifest write, A recovers ────────────────


class TestWbfpw28OppositeOrderingRecovery:
    def test_a_recovers_via_repiece_and_retry_after_bs_delete_lands_first(
        self, catalog_env: Path, t2_service_env: str,
    ) -> None:
        """The mirror-image ordering of fix-round 1's Significant 3 race
        (there: B's delete landed AFTER A's manifest write; here: B's
        delete lands BEFORE A's manifest write even starts). Real
        HttpVectorClient against the engine substrate (the in-memory
        fake has no catalog awareness and cannot exhibit the REAL
        fk_catalog_chunks_chunk violation this test needs — see
        fix-round 1's TestWbfpw28GenuineInterleavedRace docstring for
        why the fake can't stand in for the server side here either).

        Deterministic by CONSTRUCTION, not a timing race: A's own chunk
        write and catalog registration happen for real FIRST (mirroring
        put_note_pieces's own ordering — t3.put always precedes the
        manifest write), then B's FULL rollback (the same
        rollback_uncataloged_chunk_write callers use, orphaned_chashes
        union guard included) runs to completion against the SAME
        chash, deleting it for real since nothing has manifested it
        yet. Only THEN does A's manifest write run — genuinely racing a
        chunk that is by now gone, the exact fk_catalog_chunks_chunk
        violation Decision (b) exists to recover from."""
        import nexus.db.http_vector_client as hvc
        from nexus.catalog.store_hook import (
            catalog_store_hook_tracked,
            rollback_uncataloged_chunk_write,
            store_put_manifest_direct_with_recovery,
        )
        from tests._catalog_fixture_ops import active_reader

        client = hvc.HttpVectorClient(tenant=t2_service_env)
        collection = "knowledge__wbfpw28-opposite__bge-base-en-v15-768__v1"
        content = "wbfpw28 opposite ordering race content"
        chash = hashlib.sha256(content.encode()).hexdigest()
        manifest_metadatas = [{
            "chunk_text_hash": chash, "chunk_start_char": 0,
            "chunk_end_char": len(content),
        }]

        # A's own chunk write + registration happen for real first —
        # exactly put_note_pieces's ordering (t3.put before the manifest
        # write).
        client.upsert_chunks_with_embeddings(
            collection, ids=[chash], documents=[content], embeddings=[],
            metadatas=[{"title": "wbfpw28-opposite-a", "chunk_text_hash": chash}],
        )
        tumbler_a, _created = catalog_store_hook_tracked(
            title="wbfpw28-opposite-a", doc_id=chash, collection_name=collection,
        )

        # B: registration forced to fail (simulated by calling the
        # rollback directly with a blank catalog_doc_id, exactly what
        # every producer does on a registration failure); its FULL
        # rollback — union-guard check AND delete — completes entirely
        # BEFORE A's manifest write is ever attempted.
        outcome_b = rollback_uncataloged_chunk_write(
            client, [chash], collection=collection, catalog_doc_id="",
        )
        assert outcome_b.deleted_count == 1, (
            "precondition: B's delete actually removed the shared chunk "
            "— nothing yet references it, since A has registered but "
            "has not written a manifest"
        )
        from nexus.errors import CollectionNotFoundError  # noqa: PLC0415 — test-local, only needed for this precondition check
        try:
            still_present = client.get_collection(collection).get(ids=[chash], include=[]).get("ids") or []
        except CollectionNotFoundError:
            # The collection held exactly one chunk; deleting it can
            # leave the collection itself unlisted — equally proof the
            # chash is gone.
            still_present = []
        assert not still_present, (
            "precondition: the chunk is genuinely gone before A's "
            "manifest write is attempted"
        )

        repieced: list[str] = []

        def _repiece(missing_chash: str) -> None:
            repieced.append(missing_chash)
            client.upsert_chunks_with_embeddings(
                collection, ids=[missing_chash], documents=[content], embeddings=[],
                metadatas=[{"title": "wbfpw28-opposite-a", "chunk_text_hash": missing_chash}],
            )

        # A's manifest write now genuinely races the just-deleted chunk
        # — a REAL fk_catalog_chunks_chunk violation, not simulated.
        store_put_manifest_direct_with_recovery(
            tumbler_a, manifest_metadatas, collection=collection, repiece=_repiece,
        )

        assert repieced == [chash], (
            "A must recover by re-putting exactly the chash a "
            "concurrent rollback deleted"
        )
        assert chash in active_reader().get_chunk_chashes(tumbler_a), (
            "A's manifest must reference the chash after recovery"
        )
        present = client.get_collection(collection).get(ids=[chash], include=[])
        assert chash in (present.get("ids") or []), (
            "the re-put chunk must be physically present after recovery"
        )


class TestWbfpw28RecoveryWordingTruthful:
    """Decision (b)'s closing requirement: 'make the already-deleted-by-a-
    concurrent-rollback case word itself truthfully if it still reaches
    the error path' — pure unit coverage, no substrate needed."""

    def test_repiece_failure_names_the_concurrent_rollback(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.catalog.store_hook import (
            ManifestMissingChunkError, store_put_manifest_direct_with_recovery,
        )

        def _fails_with_missing_chunk(*a, **k):
            raise ManifestMissingChunkError(
                "manifest write for 1.2.3: a concurrent rollback deleted "
                "1 of 1 chunk(s)…",
                missing_chashes=frozenset({"e" * 64}),
            )

        monkeypatch.setattr(
            "nexus.catalog.store_hook.store_put_manifest_direct",
            _fails_with_missing_chunk,
        )

        def _repiece_fails(chash: str) -> None:
            raise RuntimeError("t3.put failed: engine down")

        # Matches text only the recovery path writes; the mocked
        # ManifestMissingChunkError's own message already says "a concurrent
        # rollback deleted", so matching that alone passes with recovery off.
        with pytest.raises(
            RuntimeError,
            match="a concurrent rollback deleted chash e{16}.* re-putting it "
                  "to recover also failed: t3.put failed: engine down",
        ):
            store_put_manifest_direct_with_recovery(
                "1.2.3", [{"chunk_text_hash": "e" * 64}],
                collection="knowledge__x", repiece=_repiece_fails,
            )

    def test_retry_failure_names_the_concurrent_rollback(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.catalog.store_hook import (
            ManifestMissingChunkError, store_put_manifest_direct_with_recovery,
        )

        calls = {"n": 0}

        def _first_missing_then_fails(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ManifestMissingChunkError(
                    "manifest write for 1.2.3: a concurrent rollback "
                    "deleted 1 of 1 chunk(s)…",
                    missing_chashes=frozenset({"f" * 64}),
                )
            raise RuntimeError("still broken after retry")

        monkeypatch.setattr(
            "nexus.catalog.store_hook.store_put_manifest_direct",
            _first_missing_then_fails,
        )
        with pytest.raises(RuntimeError, match="recovered from a concurrent rollback"):
            store_put_manifest_direct_with_recovery(
                "1.2.3", [{"chunk_text_hash": "f" * 64}],
                collection="knowledge__x", repiece=lambda chash: None,
            )


class TestWbfpw28MissingChunkFkClassifier:
    """The engine's class-23 409 body names the constraint in a JSON
    field; the client's raise-for-status keeps only ``error`` in the
    message. Measured against the real engine by
    TestWbfpw28OppositeOrderingRecovery: the message is
    ``HTTP 409: integrity constraint violation``, no constraint name."""

    @staticmethod
    def _status_error(body: dict) -> Exception:
        import httpx

        req = httpx.Request("POST", "http://engine/v1/catalog/manifest/write")
        resp = httpx.Response(409, json=body, request=req)
        return httpx.HTTPStatusError(
            "HttpCatalogClient./v1/catalog/manifest/write failed: HTTP 409: "
            "integrity constraint violation",
            request=req, response=resp,
        )

    def test_constraint_named_only_in_the_response_body_is_classified(self) -> None:
        from nexus.catalog.store_hook import _is_missing_chunk_fk_violation

        exc = self._status_error({
            "error": "integrity constraint violation", "sqlstate": "23503",
            "constraint": "fk_catalog_chunks_chunk",
        })
        assert "fk_catalog_chunks_chunk" not in str(exc)
        assert _is_missing_chunk_fk_violation(exc)

    def test_wrapped_status_error_is_classified(self) -> None:
        from nexus.catalog.store_hook import _is_missing_chunk_fk_violation

        inner = self._status_error({
            "error": "integrity constraint violation",
            "constraint": "fk_catalog_chunks_chunk",
        })
        outer = RuntimeError("wrapped")
        outer.__cause__ = inner
        assert _is_missing_chunk_fk_violation(outer)

    def test_other_constraint_is_not_classified(self) -> None:
        from nexus.catalog.store_hook import _is_missing_chunk_fk_violation

        exc = self._status_error({
            "error": "integrity constraint violation", "sqlstate": "23503",
            "constraint": "fk_catalog_chunks_doc",
        })
        assert not _is_missing_chunk_fk_violation(exc)


class TestWbfpw28PartialRepieceMessage:
    def test_partial_repiece_failure_names_the_already_reput_chunks(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round-3 critique Minor 2: when repiece succeeds for one missing
        chash and fails for the next, the error names the failure and says
        the chunk already re-put is left unreferenced for the rollback."""
        import nexus.catalog.store_hook as sh

        a, b = "a" * 64, "b" * 64

        def _raise_missing(doc_id, metadatas, *, collection):
            raise sh.ManifestMissingChunkError(
                "race", missing_chashes=frozenset({a, b}),
            )

        monkeypatch.setattr(sh, "store_put_manifest_direct", _raise_missing)

        def _repiece(chash: str) -> None:
            if chash == b:
                raise RuntimeError("t3 put refused")

        with pytest.raises(RuntimeError) as exc_info:
            sh.store_put_manifest_direct_with_recovery(
                "1.1.1", [{"chunk_text_hash": a}, {"chunk_text_hash": b}],
                collection="knowledge__x__bge-base-en-v15-768__v1",
                repiece=_repiece,
            )
        message = str(exc_info.value)
        assert "t3 put refused" in message
        assert "1 other chunk(s) were re-put" in message
