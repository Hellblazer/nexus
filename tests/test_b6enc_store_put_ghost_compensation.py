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
    substrate), but the note write (``note_write.write_note``,
    independent of the hook chain) always goes through
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


def _mcp_store_put_with(
    t3, content: str, title: str, *, write_error: Exception | None = None,
) -> str:
    """Run the MCP ``store_put`` tool with the post-store chains dead.

    RDR-223 P2.2 (nexus-z0o2p.12): a note is written by ``write_note`` (one
    ``write_manifest_many`` request), no longer by ``t3.put``, so *t3* only
    resolves the collection. *write_error* makes that one request raise, the
    successor of the old ``_FailingT3.put`` failure injection.
    """
    from contextlib import ExitStack

    from nexus.mcp.core import store_put

    with ExitStack() as stack:
        stack.enter_context(patch("nexus.mcp.core._get_t3", return_value=t3))
        for name in ("fire_single", "fire_batch", "fire_document"):
            stack.enter_context(patch(f"nexus.mcp.core._hooks.{name}", side_effect=_no_op))
        stack.enter_context(patch("nexus.mcp.core._catalog_auto_link", return_value=0))
        if write_error is not None:
            stack.enter_context(patch(
                "nexus.catalog.note_write.write_note", side_effect=write_error))
        return store_put(content=content, collection="fixture-subject", title=title)


_WRITE_500 = RuntimeError("engine 500: write_manifest_many failed")


# ── C2: ghost-register compensation (MCP) ────────────────────────────────────


class TestMcpGhostRegisterCompensation:
    def test_t3_failure_rolls_back_minted_row(self, catalog_env: Path) -> None:
        """t3.put raising must surface the error AND leave no catalog
        row for the title (pre-fix: permanent ghost, content lost)."""
        result = _mcp_store_put_with(
            _FailingT3(), "ghost content one", "b6enc-ghost-mcp",
            write_error=_WRITE_500,
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

        result = _mcp_store_put_with(
            _FailingT3(), content, "b6enc-dedup-mcp", write_error=_WRITE_500)
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

        result = _mcp_store_put_with(
            _FailingT3(), content, "b6enc-dedup-fence-mcp", write_error=_WRITE_500)
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
            write_error=_WRITE_500,
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
                write_error=_WRITE_500,
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
        self, catalog_env: Path, t2_service_env: str, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RDR-192 Step 3a (nexus-wbfpw.28; Sam's ruling 2026-09-26: rollback,
        not a marker column), re-based on RDR-223 P2.2 (nexus-z0o2p.12).

        The REAL engine refuses the note's request (a manifest row naming a
        chunk that is not in the request). The request is one transaction, so
        the refusal leaves no chunk behind to roll back: the successor of the
        old 'delete the chunk t3.put wrote' assertion is 'the chunk is not
        there at all'. The catalog row THIS call minted is still rolled back
        (Decision 5), and the result is never a bare 'Stored:'."""
        import nexus.db.http_vector_client as hvc
        from nexus.catalog.http_catalog_client import HttpCatalogClient

        real = HttpCatalogClient.write_manifest_many

        def _refused(self, docs, *a, **k):
            doc, rows = docs[0]
            return real(self, [(doc, [*rows, {"chash": "f" * 64, "position": len(rows)}])], *a, **k)

        monkeypatch.setattr(HttpCatalogClient, "write_manifest_many", _refused)
        content = "manifest fail content"
        result = _mcp_store_put_with(
            local_t3, content, "b6enc-manifest-mcp",
        )
        assert result.startswith("Error"), result
        assert "could not store" in result
        assert "Stored:" not in result, (
            "a manifest failure must never produce a bare 'Stored:' result"
        )
        chash = hashlib.sha256(content.encode()).hexdigest()
        col = "knowledge__fixture-subject__bge-base-en-v15-768__v1"
        client = hvc.HttpVectorClient(tenant=t2_service_env)
        assert set(client.existing_ids(col, [chash])) == set(), (
            "the refused request must not have written the chunk: chunk and "
            "owner row are one transaction"
        )
        # fix-round 1 Important (both reviewers): the catalog row THIS
        # call minted must also be rolled back on a write failure (a ghost
        # row — chunk_count=0, zero manifest, zero chunks — is not an
        # acceptable residual).
        assert _catalog_rows(catalog_env, "b6enc-manifest-mcp") == [], (
            "a failed write must roll back the catalog row this "
            "call minted, not leave a chunk_count=0 ghost behind"
        )

    def test_success_counts_align_without_fire_batch(
        self, catalog_env: Path, t2_service_env: str, local_t3: T3Database,
    ) -> None:
        """chunk_count == manifest count == 1 == T3 chunks, with every
        fire_* chain dead: the manifest is written by the one request, not
        by any hook."""
        import nexus.db.http_vector_client as hvc

        content = "healthy store_put content"
        result = _mcp_store_put_with(local_t3, content, "b6enc-healthy-mcp")
        assert result.startswith("Stored:"), result

        rows = _catalog_rows(catalog_env, "b6enc-healthy-mcp")
        assert len(rows) == 1
        tumbler, chunk_count = rows[0]
        assert chunk_count == 1, (
            f"chunk_count must be 1, got {chunk_count}"
        )
        manifest = _manifest_rows(catalog_env, tumbler)
        chash = hashlib.sha256(content.encode()).hexdigest()
        assert [r[0] for r in manifest] == [chash]
        stored_col = result.split("->")[-1].strip()
        client = hvc.HttpVectorClient(tenant=t2_service_env)
        assert set(client.existing_ids(stored_col, [chash])) == {chash}


# ── C2/C3: CLI nx store put ──────────────────────────────────────────────────


class TestCliStorePut:
    """``nx store put``, on the note writer since RDR-223 P2.6 (nexus-z0o2p.16): the
    note's chunks and owner rows are ONE request, so a failure leaves no chunk to delete
    and the previous version untouched. *t3* only resolves the collection name."""

    _COLLECTION = "knowledge__fixture-subject__bge-base-en-v15-768__v1"

    def _invoke(self, tmp_path: Path, t3, title: str, content: str, *, write_error: Exception | None = None):
        from contextlib import ExitStack

        from click.testing import CliRunner

        from nexus.cli import main

        f = tmp_path / "note.md"
        f.write_text(content)
        with ExitStack() as stack:
            stack.enter_context(patch("nexus.commands.store._t3", lambda: t3))
            if write_error is not None:
                stack.enter_context(patch(
                    "nexus.catalog.note_write.write_note", side_effect=write_error))
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
            write_error=_WRITE_500,
        )
        assert result.exit_code != 0
        assert _catalog_rows(catalog_env, "b6enc-ghost-cli") == [], (
            "CLI put failure must roll back the just-minted catalog row"
        )

    def test_manifest_failure_rolls_back_and_is_explicit_error(
        self, catalog_env: Path, tmp_path: Path, t2_service_env: str, refused_write,
    ) -> None:
        """RDR-192 Step 3a (nexus-wbfpw.28) superseded the old 'stored but NOT
        cataloged, content left in T3' contract with a rollback of the chunk; RDR-223
        P2.6 (nexus-z0o2p.16) makes the rollback unnecessary: the engine refuses the
        one request as a whole, so no chunk of the note was added, and the row this
        call minted is removed."""
        import nexus.db.http_vector_client as hvc

        local = T3Database(
            _client=make_vector_test_client(),
            _ef_override=DefaultEmbeddingFunction(),
        )
        refused_write.arm()
        result = self._invoke(
            tmp_path, local, "b6enc-manifest-cli", "cli manifest fail",
        )
        assert result.exit_code != 0
        assert "Stored:" not in result.output
        assert "The note was not stored" in result.output
        assert "retry is safe" in result.output
        chash = hashlib.sha256(b"cli manifest fail").hexdigest()
        client = hvc.HttpVectorClient(tenant=t2_service_env)
        assert _present_in(client, self._COLLECTION, [chash]) == set(), (
            "a refused request must not leave the chunk it carried in T3"
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
    """``nx memory promote`` shared the identical register-before-write
    seam the two store_put producers were fixed for — same compensation,
    same fail-loud write, locked here promote-shaped.

    RDR-223 P2.7 (nexus-z0o2p.17): promote writes its note through
    ``note_write.put_note`` (one request), exactly as MCP ``store_put`` does,
    so the failure leg is injected the way ``_mcp_store_put_with`` does it: at
    the writer (*write_error*) or as a real engine refusal (``refused_write``).
    """

    def _invoke_promote(
        self, tmp_path: Path, t3, title: str, content: str, *,
        write_error: Exception | None = None,
    ):
        from click.testing import CliRunner

        from nexus.cli import main
        from nexus.db.t2 import T2Database

        db = T2Database(tmp_path / "promote-t2.db")
        row_id = db.put(project="proj", title=title, content=content, ttl=7)
        with patch("nexus.commands.memory.t2_handle", return_value=db), \
             patch("nexus.db.make_t3", return_value=t3):
            if write_error is None:
                return CliRunner().invoke(main, [
                    "memory", "promote", str(row_id),
                    "--collection", "fixture-subject",
                ])
            with patch("nexus.catalog.note_write.write_note", side_effect=write_error):
                return CliRunner().invoke(main, [
                    "memory", "promote", str(row_id),
                    "--collection", "fixture-subject",
                ])

    def test_write_failure_rolls_back_minted_row(
        self, catalog_env: Path, tmp_path: Path,
    ) -> None:
        result = self._invoke_promote(
            tmp_path, _FailingT3(), "b6enc-ghost-promote", "promote ghost content",
            write_error=_WRITE_500,
        )
        assert result.exit_code != 0
        assert "engine 500" in result.output or "engine 500" in str(result.exception)
        assert _catalog_rows(catalog_env, "b6enc-ghost-promote") == [], (
            "promote's write failure must roll back the just-minted "
            "catalog row (nexus-v4paa)"
        )

    def test_write_failure_preserves_preexisting_deduped_row(
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
            write_error=_WRITE_500,
        )
        assert result.exit_code != 0
        assert len(_catalog_rows(catalog_env, "b6enc-dedup-promote")) == 1, (
            "pre-existing deduped row must survive promote's compensation"
        )

    def test_refused_write_leaves_no_chunk_and_no_row_and_surfaces_loudly(
        self, catalog_env: Path, t2_service_env: str, tmp_path: Path,
        local_t3: T3Database, refused_write,
    ) -> None:
        """RDR-192 Step 3a (nexus-wbfpw.28): a failed write is a loud failure,
        never a bare 'Promoted:'. RDR-223: the engine refuses the one request,
        so no chunk reaches T3 (nothing to roll back) and the row this call
        minted is removed."""
        import nexus.db.http_vector_client as hvc

        refused_write.arm()
        content = "promote manifest fail"
        result = self._invoke_promote(
            tmp_path, local_t3, "b6enc-manifest-promote", content,
        )
        assert result.exit_code != 0
        assert "Promoted:" not in result.output, (
            "a failed write must never produce a bare 'Promoted:' echo"
        )
        chash = hashlib.sha256(content.encode()).hexdigest()
        client = hvc.HttpVectorClient(tenant=t2_service_env)
        assert _present_in(
            client, "knowledge__fixture-subject__bge-base-en-v15-768__v1", [chash],
        ) == set(), "a refused request must leave no manifest-less chunk in T3"
        assert _catalog_rows(catalog_env, "b6enc-manifest-promote") == [], (
            "a refused request must roll back the catalog row this "
            "call minted, not leave a chunk_count=0 ghost behind"
        )

    def test_success_counts_align(
        self, catalog_env: Path, tmp_path: Path, local_t3: T3Database,
    ) -> None:
        content = "healthy promote content"
        result = self._invoke_promote(
            tmp_path, local_t3, "b6enc-ok-promote", content,
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
    def test_the_batch_chain_runs_without_rewriting_the_manifest(
        self, catalog_env: Path, t2_service_env: str, local_t3: T3Database,
    ) -> None:
        """End-to-end store_put with ``fire_batch`` LIVE (not mocked).

        RDR-223 P2.2 (nexus-z0o2p.12): the one request already wrote the
        manifest and the completion stamp, so the batch chain runs with
        ``manifest_write_batch_hook`` skipped (the flush-grain combined write
        makes the same skip). The manifest is exactly the expected set, the
        chunk count is right, and the hook is never called."""
        from nexus.mcp.core import store_put

        content = "coexistence double write content"
        with patch("nexus.mcp.core._get_t3", return_value=local_t3), \
             patch("nexus.mcp.core._hooks.fire_single", side_effect=_no_op), \
             patch("nexus.mcp.core._hooks.fire_document", side_effect=_no_op), \
             patch("nexus.mcp.core._catalog_auto_link", return_value=0), \
             patch("nexus.mcp_infra.manifest_write_batch_hook") as manifest_hook:
            result = store_put(
                content=content, collection="fixture-subject",
                title="b6enc-coexist",
            )
        assert result.startswith("Stored:"), result
        manifest_hook.assert_not_called()

        rows = _catalog_rows(catalog_env, "b6enc-coexist")
        assert len(rows) == 1
        tumbler, chunk_count = rows[0]
        assert chunk_count == 1, f"chunk_count must be 1, got {chunk_count}"
        chash = hashlib.sha256(content.encode()).hexdigest()
        manifest = [r[0] for r in _manifest_rows(catalog_env, tumbler)]
        assert manifest == [chash], f"manifest rows must be exactly the expected set, got {manifest}"


# ── C4: store_delete asymmetry ───────────────────────────────────────────────


class TestStoreDeleteAsymmetry:
    def test_delete_removes_store_put_origin_catalog_row(
        self, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # store_delete reads this fake T3; the note is routed there (RDR-223 P2.2,
        # tests/_note_write_double.py) because the delete is the subject, not the write.
        from tests._note_write_double import route_note_writes_to

        route_note_writes_to(monkeypatch, local_t3)
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
    def test_registration_failure_writes_nothing_and_returns_error(
        self, catalog_env: Path, t2_service_env: str, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RDR-223 P2.2: a note with no catalog document is not written at
        all, so there is no chunk to roll back (it used to be written and
        then deleted). The error is explicit and no row or chunk exists."""
        import nexus.db.http_vector_client as hvc

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
        col = "knowledge__fixture-subject__bge-base-en-v15-768__v1"
        client = hvc.HttpVectorClient(tenant=t2_service_env)
        try:
            present = set(client.existing_ids(col, [chash]))
        except hvc.VectorServiceError as exc:
            assert "not registered" in str(exc), exc  # never written to: the collection does not exist
            present = set()
        assert present == set(), (
            "a failed registration must not write the chunk: there is no "
            "owner to write it with"
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
        self, catalog_env: Path, tmp_path: Path, t2_service_env: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import nexus.db.http_vector_client as hvc

        local = T3Database(
            _client=make_vector_test_client(),
            _ef_override=DefaultEmbeddingFunction(),
        )
        # RDR-223 P2.6 (nexus-z0o2p.16): put_cmd registers through
        # note_write.put_note, which looks catalog_store_hook_tracked up on the
        # store_hook module at call time, so that is the name to patch (the
        # module-level alias commands/store.py used to import is gone).
        monkeypatch.setattr(
            "nexus.catalog.store_hook.catalog_store_hook_tracked",
            lambda *a, **k: ("", False),
        )
        content = "wbfpw28 registration fail content cli"
        result = self._invoke(tmp_path, local, "wbfpw28-reg-cli", content)
        assert result.exit_code != 0
        assert "catalog registration failed" in result.output
        assert "Nothing was written" in result.output
        assert "Stored:" not in result.output
        assert _catalog_rows(catalog_env, "wbfpw28-reg-cli") == []
        chash = hashlib.sha256(content.encode()).hexdigest()
        client = hvc.HttpVectorClient(tenant=t2_service_env)
        assert _present_in(
            client, "knowledge__fixture-subject__bge-base-en-v15-768__v1", [chash],
        ) == set(), "a note with no catalog row is never written"


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

    def test_registration_failure_writes_nothing_and_surfaces_loudly(
        self, catalog_env: Path, t2_service_env: str, tmp_path: Path,
        local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import nexus.db.http_vector_client as hvc

        monkeypatch.setattr(
            "nexus.catalog.store_hook.catalog_store_hook_tracked",
            lambda *a, **k: ("", False),
        )
        content = "wbfpw28 registration fail content promote"
        result = self._invoke_promote(
            tmp_path, local_t3, "wbfpw28-reg-promote", content,
        )
        assert result.exit_code != 0
        assert "catalog registration failed" in result.output
        assert "Promoted:" not in result.output
        assert _catalog_rows(catalog_env, "wbfpw28-reg-promote") == []
        chash = hashlib.sha256(content.encode()).hexdigest()
        client = hvc.HttpVectorClient(tenant=t2_service_env)
        assert _present_in(
            client, "knowledge__fixture-subject__bge-base-en-v15-768__v1", [chash],
        ) == set(), "a note is never written without its catalog entry"


class TestWbfpw28ConcurrentIdenticalContentRace:
    """Plan-audit round 2 residual: identical chunk text collapses to ONE
    T3 row (CLAUDE.md § catalog/T3 split). A failed second store of the same
    content must never take away the chunk a DIFFERENT, already-succeeded
    store depends on. Simulated sequentially (store A completes fully, THEN
    store B, same content, fails). Since RDR-223 P2.2 B writes nothing when it
    fails, so the guarantee is structural; the test pins the observable."""

    def test_surviving_stores_chunk_is_not_deleted_by_the_failed_one(
        self, catalog_env: Path, t2_service_env: str, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import nexus.db.http_vector_client as hvc

        content = "wbfpw28 race shared identical content"
        chash = hashlib.sha256(content.encode()).hexdigest()

        # Store A: succeeds fully, real registration and the one real request.
        result_a = _mcp_store_put_with(local_t3, content, "wbfpw28-race-a")
        assert result_a.startswith("Stored:"), result_a
        rows_a = _catalog_rows(catalog_env, "wbfpw28-race-a")
        assert len(rows_a) == 1
        tumbler_a, chunk_count_a = rows_a[0]
        assert chunk_count_a == 1
        assert [r[0] for r in _manifest_rows(catalog_env, tumbler_a)] == [chash]

        # Store B: SAME content, DIFFERENT title, registration forced to fail.
        monkeypatch.setattr(
            "nexus.catalog.store_hook.catalog_store_hook_tracked",
            lambda *a, **k: ("", False),
        )
        result_b = _mcp_store_put_with(local_t3, content, "wbfpw28-race-b")
        assert result_b.startswith("Error"), result_b
        assert _catalog_rows(catalog_env, "wbfpw28-race-b") == []

        col = "knowledge__fixture-subject__bge-base-en-v15-768__v1"
        client = hvc.HttpVectorClient(tenant=t2_service_env)
        assert set(client.existing_ids(col, [chash])) == {chash}, (
            "a different, already-succeeded store's chunk must survive"
        )
        assert [r[0] for r in _manifest_rows(catalog_env, tumbler_a)] == [chash]


# ── RDR-192 Step 3a: recovery-bundle importer ────────────────────────────────


class TestWbfpw28RecoveryBundleRollback:
    """``catalog/recovery_bundle.py::_default_import_doc`` writes a note through
    ``note_write.put_note`` (RDR-223 P2.8, nexus-z0o2p.18), the same writer MCP ``store_put``
    uses: a registration that yields no document writes nothing, and a write that is confirmed not
    to have landed removes the catalog row this call minted. There is no chunk to roll back any
    more: the pieces and the owner rows are one request (``tests/test_z0o2p18_recovery_import.py``)."""

    def _rec(self, content: str, title: str) -> dict:
        return {
            "content": content,
            "collection": "fixture-subject",
            "title": title,
            "tags": "",
            "category": "",
        }

    def test_registration_failure_writes_nothing_and_raises(
        self, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.catalog.recovery_bundle import _default_import_doc

        monkeypatch.setattr(
            "nexus.catalog.store_hook.catalog_store_hook_tracked",
            lambda *a, **k: ("", False),
        )

        def _no_write(**kw):
            raise AssertionError("a note with no catalog document must not be written")

        monkeypatch.setattr("nexus.catalog.note_write.write_note", _no_write)
        content = "wbfpw28 recovery bundle registration fail"
        with pytest.raises(RuntimeError, match="catalog registration failed"):
            _default_import_doc(local_t3, self._rec(content, "wbfpw28-rb-reg"))
        assert _catalog_rows(catalog_env, "wbfpw28-rb-reg") == []

    def test_a_write_that_did_not_land_removes_the_row_it_minted_and_raises(
        self, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.catalog.note_write import NoteWriteError
        from nexus.catalog.recovery_bundle import _default_import_doc

        def _refused(**kw):
            raise NoteWriteError(
                catalog_doc_id=kw["catalog_doc_id"], collection=kw["collection"],
                reason="manifest write refused", manifest_empty=True)

        monkeypatch.setattr("nexus.catalog.note_write.write_note", _refused)
        content = "wbfpw28 recovery bundle manifest fail"
        with pytest.raises(RuntimeError, match="manifest write refused"):
            _default_import_doc(local_t3, self._rec(content, "wbfpw28-rb-manifest"))
        assert _catalog_rows(catalog_env, "wbfpw28-rb-manifest") == [], (
            "a write confirmed not to have landed must remove the catalog row this "
            "call minted, not leave a chunk_count=0 ghost behind"
        )

    def test_success_is_unchanged(
        self, catalog_env: Path, local_t3: T3Database,
    ) -> None:
        from nexus.catalog.recovery_bundle import _default_import_doc

        content = "wbfpw28 recovery bundle healthy import"
        _default_import_doc(local_t3, self._rec(content, "wbfpw28-rb-ok"))
        rows = _catalog_rows(catalog_env, "wbfpw28-rb-ok")
        assert len(rows) == 1
        tumbler, chunk_count = rows[0]
        assert chunk_count == 1
        chash = hashlib.sha256(content.encode()).hexdigest()
        assert [r[0] for r in _manifest_rows(catalog_env, tumbler)] == [chash]


# ── RDR-192 Step 3a (nexus-wbfpw.28): an unknown outcome is never rolled back ─
#
# A write whose outcome cannot be observed (the verify read failed, or the
# request timed out) is UNKNOWN, not failed: the note may have landed, so the
# caller must not remove the row it minted. The note writer raises
# ManifestVerifyUncertainError, a RuntimeError subclass, so callers can tell
# that apart from a confirmed refusal (NoteWriteError).


class TestWbfpw28ManifestVerifyUncertain:
    def test_mcp_reports_uncertain_and_never_rolls_back(
        self, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Caller-side branch (MCP store_put; kept by RDR-223 P2.2: an atomic
        request can still time out with an unknown result). When the note
        writer raises ManifestVerifyUncertainError, store_put must report the
        uncertainty, never claim "rolled back", and never ATTEMPT a rollback
        of the catalog row (it may hold a landed note) or a stamp restore."""
        from nexus.catalog.store_hook import ManifestVerifyUncertainError

        rollback_calls: list[tuple] = []

        def _rollback_must_not_be_called(*a, **k):
            rollback_calls.append((a, k))
            raise AssertionError(
                "rollback must never be attempted when the outcome is uncertain"
            )

        monkeypatch.setattr(
            "nexus.catalog.store_hook.rollback_minted_catalog_entry",
            _rollback_must_not_be_called,
        )
        monkeypatch.setattr(
            "nexus.catalog.store_hook.restore_pre_call_stamp",
            _rollback_must_not_be_called,
        )
        content = "wbfpw28 verify uncertain content mcp"
        result = _mcp_store_put_with(
            local_t3, content, "wbfpw28-uncertain-mcp",
            write_error=ManifestVerifyUncertainError("verify read failed: boom"),
        )

        assert result.startswith("Error"), result
        assert "confirm" in result.lower(), result
        assert "the chunk was rolled back" not in result.lower(), (
            "an uncertain outcome must never claim anything WAS rolled back: "
            f"{result!r}"
        )
        assert rollback_calls == [], (
            f"no rollback may be attempted on an uncertain outcome, got: {rollback_calls!r}"
        )
        assert len(_catalog_rows(catalog_env, "wbfpw28-uncertain-mcp")) == 1, (
            "the catalog row must survive an uncertain outcome"
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
            _read_manifest_rows_with_retry,
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
        landed = {c for _position, c in _read_manifest_rows_with_retry(str(t), context="test")}
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
            _read_manifest_rows_with_retry,
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
            _read_manifest_rows_with_retry("1.2.3", context="test")
        assert len(sleep_calls) == _MANIFEST_VERIFY_RETRY_ATTEMPTS - 1, (
            "no sleep before the FIRST attempt — only between attempts"
        )


# ── RDR-223 P2.2 (nexus-z0o2p.12): the MCP path's failure legs, real engine ──


@pytest.fixture
def refused_write(monkeypatch: pytest.MonkeyPatch):
    """Make the engine refuse the note's one request: add a manifest row that
    names a chunk which is not in the request. The refusal is real (per-document
    transaction rolled back), not a stub."""
    from nexus.catalog.http_catalog_client import HttpCatalogClient

    real = HttpCatalogClient.write_manifest_many

    def _refused(self, docs, *a, **k):
        doc, rows = docs[0]
        return real(self, [(doc, [*rows, {"chash": "f" * 64, "position": len(rows)}])], *a, **k)

    def arm():
        monkeypatch.setattr(HttpCatalogClient, "write_manifest_many", _refused)

    def disarm():
        monkeypatch.setattr(HttpCatalogClient, "write_manifest_many", real)

    return type("Refused", (), {"arm": staticmethod(arm), "disarm": staticmethod(disarm)})


def _present_in(client, collection: str, chashes: list[str]) -> set[str]:
    """Chunks stored in *collection*; a collection that was never written to has none."""
    import nexus.db.http_vector_client as hvc

    try:
        return set(client.existing_ids(collection, chashes))
    except hvc.VectorServiceError as exc:
        assert "not registered" in str(exc), exc
        return set()


class TestZ0o2p12McpFailedReput:
    """The failed-re-put scenarios the retired split-write rollback guards were written for, on
    the MCP path as it is now: one request, so a failure leaves nothing to delete and the
    previous version is untouched."""

    _COLLECTION = "knowledge__fixture-subject__bge-base-en-v15-768__v1"

    def test_a_failed_reput_leaves_the_old_note_whole_and_restores_the_stamp(
        self, catalog_env: Path, t2_service_env: str, local_t3: T3Database, refused_write,
    ) -> None:
        import nexus.db.http_vector_client as hvc

        title = "z0o2p12-failed-reput"
        old = "z0o2p12 failed reput -- original content"
        new = "z0o2p12 failed reput -- replacement content"
        old_chash = hashlib.sha256(old.encode()).hexdigest()
        new_chash = hashlib.sha256(new.encode()).hexdigest()
        client = hvc.HttpVectorClient(tenant=t2_service_env)

        assert _mcp_store_put_with(local_t3, old, title).startswith("Stored:")
        (tumbler, _count), = _catalog_rows(catalog_env, title)
        refused_write.arm()

        for attempt in (1, 2):  # the second is the retry the error text recommends
            result = _mcp_store_put_with(local_t3, new, title)
            assert result.startswith("Error"), result
            assert "retry is safe" in result
            assert _manifest_rows(catalog_env, tumbler) == [(old_chash,)], f"attempt {attempt}"
            assert _present_in(client, self._COLLECTION, [old_chash, new_chash]) == {old_chash}
            assert (documents_by_title(title)[0].meta or {}).get("doc_id") == old_chash, (
                "the identity stamp catalog_store_hook_tracked put on the row must be put back")
            assert len(_catalog_rows(catalog_env, title)) == 1

        refused_write.disarm()
        assert _mcp_store_put_with(local_t3, new, title).startswith("Stored:")
        assert _manifest_rows(catalog_env, tumbler) == [(new_chash,)]
        assert _present_in(client, self._COLLECTION, [old_chash, new_chash]) == {new_chash}, (
            "the supersede sweeps the old chunk after the request commits")

    def test_a_legacy_note_chunk_survives_a_colliding_failed_put(
        self, catalog_env: Path, t2_service_env: str, local_t3: T3Database, refused_write,
    ) -> None:
        """A manifest-less legacy note owns chash X by ``meta.doc_id`` alone. A failed put of
        byte-identical content under another title writes nothing, so X is untouched."""
        import nexus.db.http_vector_client as hvc
        from tests._catalog_fixture_ops import seed_manifest_chunks

        content = "z0o2p12 legacy note shared content"
        chash = hashlib.sha256(content.encode()).hexdigest()
        cat = ActiveCatalog()
        owner = cat.register_owner("knowledge", "curator")
        cat.register(owner, "z0o2p12-legacy-note", content_type="knowledge",
                     physical_collection=self._COLLECTION, meta={"doc_id": chash})
        seed_manifest_chunks(self._COLLECTION, [chash])
        refused_write.arm()

        result = _mcp_store_put_with(local_t3, content, "z0o2p12-colliding-put")
        assert result.startswith("Error"), result
        client = hvc.HttpVectorClient(tenant=t2_service_env)
        assert _present_in(client, self._COLLECTION, [chash]) == {chash}
        assert _catalog_rows(catalog_env, "z0o2p12-colliding-put") == []

    def test_a_split_note_that_fails_leaves_no_piece(
        self, catalog_env: Path, t2_service_env: str, local_t3: T3Database, refused_write,
    ) -> None:
        """No piece of a failed multi-piece note exists afterwards (the retired split write
        needed a compensating delete for this)."""
        import nexus.db.http_vector_client as hvc
        from nexus.catalog import store_hook as sh

        content = "z0o2p12 split note. " + ("filler sentence content number " * 300)
        pieces = sh.note_pieces(content, self._COLLECTION)
        assert len(pieces) > 1, "control: the fixture must split"
        chashes = [hashlib.sha256(p.encode()).hexdigest() for p in pieces]
        refused_write.arm()

        result = _mcp_store_put_with(local_t3, content, "z0o2p12-split-fail")
        assert result.startswith("Error"), result
        client = hvc.HttpVectorClient(tenant=t2_service_env)
        assert _present_in(client, self._COLLECTION, chashes) == set()
        assert _catalog_rows(catalog_env, "z0o2p12-split-fail") == []

    def test_the_fence_begins_before_the_write_and_completes_with_it(
        self, catalog_env: Path, local_t3: T3Database,
    ) -> None:
        """Kept: ``_fence_begin`` still fires before the write, and the completion stamp rides the request."""
        import nexus.doc_indexer as di

        order: list[str] = []
        real_begin = di._fence_begin

        def spy_begin(doc_id, content_hash, collection):
            order.append("begin")
            return real_begin(doc_id, content_hash, collection)

        with patch("nexus.doc_indexer._fence_begin", side_effect=spy_begin):
            result = _mcp_store_put_with(local_t3, "z0o2p12 fenced content", "z0o2p12-fence")
        assert result.startswith("Stored:"), result
        assert order == ["begin"]
        assert documents_by_title("z0o2p12-fence")[0].index_state == "complete"

    def test_a_failed_write_stamps_the_fence_failed_and_rolls_back_the_minted_row(
        self, catalog_env: Path, local_t3: T3Database, refused_write,
    ) -> None:
        """Kept: ``_fence_fail`` and ``rollback_minted_catalog_entry`` (Decision 5) still fire on a failed first put."""
        refused_write.arm()
        with patch("nexus.doc_indexer._fence_fail") as fail, \
             patch("nexus.catalog.store_hook.rollback_minted_catalog_entry") as rollback:
            result = _mcp_store_put_with(local_t3, "z0o2p12 first put fails", "z0o2p12-first-fail")
        assert result.startswith("Error"), result
        assert fail.call_count == 1
        assert rollback.call_count == 1

    def test_killed_at_the_transport_after_the_post_leaves_no_ownerless_chunk(
        self, catalog_env: Path, t2_service_env: str, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test Plan 8 at the tool: the process dies at the HTTP layer right after the note's one
        request went out. Whatever reached T3 has an owner row."""
        import nexus.catalog.http_catalog_client as hcc
        import nexus.db.http_vector_client as hvc

        class ClientDied(BaseException):
            pass

        real = hcc.HttpCatalogClient._post_embedding_write

        def kill_after_post(self, path, *a, **k):
            real(self, path, *a, **k)
            raise ClientDied()

        monkeypatch.setattr(hcc.HttpCatalogClient, "_post_embedding_write", kill_after_post)
        content = "z0o2p12 killed at the transport"
        with pytest.raises(ClientDied):
            _mcp_store_put_with(local_t3, content, "z0o2p12-killed")
        chash = hashlib.sha256(content.encode()).hexdigest()
        (tumbler, _count), = _catalog_rows(catalog_env, "z0o2p12-killed")
        client = hvc.HttpVectorClient(tenant=t2_service_env)
        assert _present_in(client, self._COLLECTION, [chash]) == {chash}, "control: the request committed"
        assert _manifest_rows(catalog_env, tumbler) == [(chash,)], "and the chunk has its owner"

    def test_a_timeout_the_manifest_does_not_show_is_unknown_and_keeps_the_row(
        self, catalog_env: Path, t2_service_env: str, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The engine cannot cancel an in-flight embed, so a timeout with no note in the manifest may
        still commit: 'could not confirm', not 'Nothing was written'; the minted row stays; a late
        commit lands a whole note and the retry the message recommends is an idempotent replace."""
        from nexus.catalog.http_catalog_client import HttpCatalogClient
        from nexus.errors import CombinedWriteEmbedTimeoutError

        real = HttpCatalogClient.write_manifest_many

        def timeout(self, docs, *a, **k):
            raise CombinedWriteEmbedTimeoutError(collection="c", chunk_count=1, original="ReadTimeout")

        monkeypatch.setattr(HttpCatalogClient, "write_manifest_many", timeout)
        content = "z0o2p12 timeout in flight"
        result = _mcp_store_put_with(local_t3, content, "z0o2p12-timeout")
        assert result.startswith("Error") and "could not confirm" in result, result
        assert "Nothing was written" not in result and "retry is safe" not in result
        assert len(_catalog_rows(catalog_env, "z0o2p12-timeout")) == 1, "the row must survive"

        monkeypatch.setattr(HttpCatalogClient, "write_manifest_many", real)
        assert _mcp_store_put_with(local_t3, content, "z0o2p12-timeout").startswith("Stored:")
        (tumbler, count), = _catalog_rows(catalog_env, "z0o2p12-timeout")
        assert count == 1
        assert _manifest_rows(catalog_env, tumbler) == [(hashlib.sha256(content.encode()).hexdigest(),)]

    def test_store_put_makes_no_store_put_call(
        self, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Acceptance: ``store_put`` makes no ``/store-put`` (nor ``/upsert-chunks``) call."""
        import nexus.db.http_vector_client as hvc

        paths: list[str] = []
        real_post = hvc._post

        def spy(path, *a, **k):
            paths.append(path)
            return real_post(path, *a, **k)

        monkeypatch.setattr(hvc, "_post", spy)
        result = _mcp_store_put_with(local_t3, "z0o2p12 no store-put", "z0o2p12-no-store-put")
        assert result.startswith("Stored:"), result
        assert not [p for p in paths if "store-put" in p or "upsert-chunks" in p], paths


class TestZ0o2p12McpWording:
    """What the MCP reply says for the two outcomes whose wording was imprecise (verify2 M4, M5)."""

    def test_a_refused_stamp_says_what_is_known_once_and_stays_an_error(
        self, catalog_env: Path, local_t3: T3Database,
    ) -> None:
        from nexus.catalog.note_write import StampRefusedError

        refused = StampRefusedError(
            "note 1.1.1 in c: the write was accepted but the engine refused to stamp it complete: "
            "referenced 2 != chunk_count 1", detail="referenced 2 != chunk_count 1")
        result = _mcp_store_put_with(local_t3, "z0o2p12 stamp refused", "z0o2p12-stamp-wording",
                                     write_error=refused)
        assert result.startswith("Error:") and "Stored:" not in result, result
        assert "accepted the write" in result and "'indexing'" in result
        assert "referenced 2 != chunk_count 1" in result
        assert "could not confirm" not in result and "landed" not in result, (
            "the cause is known, so the reply must not also say it could not be confirmed")
        assert result.count("refused to stamp") == 1, result

    def test_a_refused_request_does_not_claim_nothing_was_written(
        self, catalog_env: Path, local_t3: T3Database,
    ) -> None:
        """A 429 from the embedder follows the engine's metadata refresh of already-stored chunks
        (CombinedWriteService phase 2a commits, 2b embeds), so the reply names what is unchanged."""
        from nexus.catalog.note_write import NoteWriteError

        refused = NoteWriteError(
            catalog_doc_id="1.1.1", collection="c", reason="HTTP 429", manifest_empty=False)
        result = _mcp_store_put_with(local_t3, "z0o2p12 refused wording", "z0o2p12-refused-wording",
                                     write_error=refused)
        assert result.startswith("Error:"), result
        assert "Nothing was written" not in result
        assert "metadata refreshed" in result
        assert "retry is safe" in result and "earlier version of the note is unchanged" in result


class TestRestorePreCallStamp:
    """``store_hook.restore_pre_call_stamp``, the compare-and-set that ``put_note`` runs on a
    refused write. (The rest of this class drove the retired split-write replica and went with
    it at nexus-z0o2p.32; ``TestZ0o2p12McpFailedReput`` holds the MCP-path successors.)"""

    _COLLECTION = "knowledge__fixture-subject__bge-base-en-v15-768__v1"

    def test_stamp_restore_declines_when_a_concurrent_reput_restamped(
        self, catalog_env: Path,
    ) -> None:
        """Round-3 critique: the compare-then-write restore must decline
        when the document's current stamp is no longer the one this call
        wrote (a concurrent re-put landed a newer identity), and must
        restore when it still is. Both branches, same fixture shape."""
        from nexus.catalog.store_hook import restore_pre_call_stamp

        collection = self._COLLECTION
        old_chash = "a1" * 32
        ours_chash = "b2" * 32
        newer_chash = "c3" * 32

        cat = ActiveCatalog()
        owner = cat.register_owner("knowledge", "curator")
        raced = str(cat.register(
            owner, "k54nk-cas-declines", content_type="knowledge",
            physical_collection=collection, meta={"doc_id": newer_chash},
        ))
        still_ours = str(cat.register(
            owner, "k54nk-cas-restores", content_type="knowledge",
            physical_collection=collection, meta={"doc_id": ours_chash},
        ))

        restore_pre_call_stamp(raced, old_chash, ours_chash)
        restore_pre_call_stamp(still_ours, old_chash, ours_chash)

        assert (documents_by_title("k54nk-cas-declines")[0].meta or {}).get(
            "doc_id") == newer_chash, (
            "a concurrent re-put's newer stamp must never be overwritten"
        )
        assert (documents_by_title("k54nk-cas-restores")[0].meta or {}).get(
            "doc_id") == old_chash, (
            "a stamp still equal to this call's own must be restored"
        )
