# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-cotmr / nexus-tafjk: RUNFENCE coverage for the CLI store-path
producers (``nx store put``, ``nx memory promote``).

Round-1 diagnosis (T2 nexus/nexus-cotmr-implementation) wrongly concluded
the acquire-gate journey's NULL ``index_state`` was accepted design and
exempted every note-shaped document from doctor's stale-fence WARN.
substantive-critique (T2 nexus/nexus-cotmr-critique-2026-08-06, bead
nexus-tafjk) found the real cause: commit f55435eb (nexus-vw594 F2)
already fenced MCP ``store_put`` (``_fence_begin`` / ``_fence_fail`` /
``manifest_complete``), but the CLI store-path entry points
(``commands/store.py::put_cmd``, ``commands/memory.py``'s promote) never
called those helpers at all, despite the F2 AST-tripwire allowlist
(``tests/test_vw594_fence_coverage_gate.py``) already claiming coverage
for "MCP store_put / nx store put" — a claim the code did not deliver for
the CLI half. This file locks the fix: both CLI producers now mirror MCP
``store_put``'s F2 pattern verbatim (fence begin before the vector put,
fence fail on either failure path, ``manifest_complete`` riding the
existing ``fire_store_chains`` call).

Real (service) catalog + real T3 via the same factories production code
uses — mocks appear only at the failure-injection points, matching
``test_b6enc_store_put_ghost_compensation.py``'s established pattern
(which this file's fixtures are copied from) and the integration-over-
mocks rule.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction

from nexus.db.t3 import T3Database
from tests.conftest import make_vector_test_client
from tests._catalog_fixture_ops import documents_by_title


@pytest.fixture
def catalog_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    return catalog_dir


def _local_t3() -> T3Database:
    """Fake in-process T3 (InMemoryVectorClient) — fine for the
    manifest-failure tests below, which mock the manifest write itself
    and never need the engine's own T3 presence check."""
    return T3Database(
        _client=make_vector_test_client(),
        _ef_override=DefaultEmbeddingFunction(),
    )


def _real_t3():
    """REAL engine-backed T3 (nexus-cotmr): required for any assertion
    that a document reaches ``index_state == 'complete'``. Completion is
    fail-closed (memo §3.3) — the engine's ``manifest_verify`` checks the
    chash is actually PRESENT in T3 before stamping, and the fake
    in-memory T3 above is a completely separate, in-process store the
    real engine can never see (confirmed empirically: an in-memory-T3
    write manifest-verifies as ``missing=1`` and the stamp is correctly
    REFUSED — the same real-engine substrate ``tests/db/test_5xn3k_
    runfence_gate.py`` uses for its own end-to-end completion proofs)."""
    from nexus.db.http_vector_client import HttpVectorClient

    return HttpVectorClient()


def _invoke_store_put(tmp_path: Path, t3, title: str, content: str):
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


def _invoke_promote(tmp_path: Path, t3, title: str, content: str):
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


def _index_state_for(title: str) -> str | None:
    rows = documents_by_title(title)
    assert len(rows) == 1, f"expected exactly one document for {title!r}, got {rows}"
    return rows[0].index_state


# ── CLI `nx store put` fences ────────────────────────────────────────────────


class TestCliStorePutFence:
    def test_success_stamps_complete(self, catalog_env: Path, tmp_path: Path) -> None:
        title = "cotmr-cli-put-complete"
        result = _invoke_store_put(tmp_path, _real_t3(), title, "cli fence success body")
        assert result.exit_code == 0, result.output
        assert _index_state_for(title) == "complete"

    def test_manifest_failure_rolls_back_the_freshly_minted_row(
        self, catalog_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RDR-192 Step 3a fix-round 1 (nexus-wbfpw.28, Important, both
        reviewers): superseding this test's old 'stamped failed, row
        survives' contract. A manifest failure on a document THIS CALL
        minted now rolls the catalog row back entirely (mirroring the
        sibling t3.put-failure branch's rollback_minted_catalog_entry
        exactly) rather than leaving a chunk_count=0 ghost stamped
        'failed' — the fence stamp fires first (still exercised, just no
        longer observable afterward since the row it was on is gone)."""
        title = "cotmr-cli-put-failed"
        # RDR-223 P2.6 (nexus-z0o2p.16): the chunk and its owner rows are one
        # request, so the failure to inject is a refused request. The engine
        # refuses the document (a 4xx answer), nothing of the note lands, and
        # put_note removes the row it minted.
        from nexus.catalog.http_catalog_client import HttpCatalogClient

        real = HttpCatalogClient.write_manifest_many

        def _refused(self, docs, *a, **k):
            doc, rows = docs[0]
            return real(self, [(doc, [*rows, {"chash": "f" * 64, "position": len(rows)}])], *a, **k)

        monkeypatch.setattr(HttpCatalogClient, "write_manifest_many", _refused)
        result = _invoke_store_put(tmp_path, _local_t3(), title, "cli fence failure body")
        assert result.exit_code != 0
        assert documents_by_title(title) == [], (
            "a manifest failure on a freshly-minted document must roll "
            "back the catalog row, not leave a chunk_count=0 ghost "
            "stamped 'failed'"
        )


# ── CLI `nx memory promote` fences ───────────────────────────────────────────


class TestMemoryPromoteFence:
    def test_success_stamps_complete(self, catalog_env: Path, tmp_path: Path) -> None:
        title = "cotmr-promote-complete"
        result = _invoke_promote(tmp_path, _real_t3(), title, "promote fence success body")
        assert result.exit_code == 0, result.output
        assert _index_state_for(title) == "complete"

    def test_a_refused_write_rolls_back_the_freshly_minted_row(
        self, catalog_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RDR-192 Step 3a fix-round 1 (nexus-wbfpw.28, Important, both
        reviewers): a failed write on a document THIS CALL minted rolls the
        catalog row back entirely rather than leaving a chunk_count=0 ghost
        stamped 'failed'. RDR-223 P2.7 (nexus-z0o2p.17): promote writes its note
        in one request through ``put_note``, so the failure is injected where
        it happens now, the engine refusing that request (a manifest row naming
        a chunk the request does not carry; the per-document transaction rolls
        back). The fence stamp fires first, then the minted row is removed."""
        from nexus.catalog.http_catalog_client import HttpCatalogClient

        real = HttpCatalogClient.write_manifest_many

        def refused(self, docs, *a, **k):
            doc, rows = docs[0]
            return real(self, [(doc, [*rows, {"chash": "f" * 64, "position": len(rows)}])], *a, **k)

        monkeypatch.setattr(HttpCatalogClient, "write_manifest_many", refused)
        title = "cotmr-promote-failed"
        with patch("nexus.doc_indexer._fence_fail") as fence_fail:
            result = _invoke_promote(tmp_path, _real_t3(), title, "promote fence failure body")
        assert result.exit_code != 0
        assert fence_fail.call_count == 1, "the failed write stamps the fence failed"
        assert documents_by_title(title) == [], (
            "a failed write on a freshly-minted document must roll "
            "back the catalog row, not leave a chunk_count=0 ghost "
            "stamped 'failed'"
        )


# ── The acquire-gate journey, end to end ─────────────────────────────────────


class TestAcquireGateJourneyDoctorClean:
    """Reproduces nexus-cotmr's own bead text: the acquire-gate journey is
    ``store put`` then ``nx doctor``. Round 1 made this clean by EXEMPTING
    note-shaped documents from the check. That exemption is reverted
    (nexus-tafjk); this class proves the SAME journey now ends clean
    because the CLI producer is genuinely fenced — no exemption involved.
    """

    def test_cli_store_put_then_doctor_is_clean(
        self, catalog_env: Path, tmp_path: Path,
    ) -> None:
        import nexus.health as h

        title = "cotmr-acquire-gate-journey"
        result = _invoke_store_put(tmp_path, _real_t3(), title, "acquire gate journey body")
        assert result.exit_code == 0, result.output
        assert _index_state_for(title) == "complete"

        results = h._check_stale_indexing_runs()
        warns = [r for r in results if r.warn]
        assert not warns, (
            f"the acquire-gate journey (CLI store put) must not trip the "
            f"stale-fence WARN on a fence-live engine now that the "
            f"producer is genuinely fenced: {results}"
        )

    def test_artificially_unfenced_document_still_warns(
        self, catalog_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """KILL CONTROL / non-vacuity companion (critique item 5): a document
        whose producer never fenced (no ``_fence_begin``, no completion stamp)
        keeps a NULL index_state and doctor's check WARNs — proving the clean
        result above is powered by the real fence wiring, not a
        coincidentally-quiet check. Doctor's check itself is UNCHANGED by
        nexus-cotmr round 2 (the note-exemption was reverted).

        RDR-223 P2.6 (nexus-z0o2p.16): ``nx store put`` can no longer produce
        such a document — ``put_note`` begins the fence and the completion stamp
        rides the note's one request, so disabling ``_fence_begin`` still ends
        ``complete`` — so the unfenced producer is built directly: register the
        document and write its note with ``write_note`` and no ``content_hash``,
        the exact pre-cotmr gap."""
        import nexus.health as h
        from nexus.catalog.note_write import write_note
        from nexus.catalog.store_hook import (
            catalog_store_hook_tracked,
            note_manifest_metadata,
            note_pieces,
        )
        from nexus.corpus import t3_collection_name

        title = "cotmr-artificially-unfenced"
        content = "artificially unfenced body"
        col = t3_collection_name("fixture-subject", for_write=True)
        pieces = note_pieces(content, col)
        first, _ = note_manifest_metadata(pieces)
        doc, _minted = catalog_store_hook_tracked(title=title, doc_id=first, collection_name=col)
        assert doc
        write_note(catalog_doc_id=doc, collection=col, pieces=pieces, content_hash=None)
        assert _index_state_for(title) is None, (
            "a note written with no fence and no stamp must keep a NULL index_state"
        )

        results = h._check_stale_indexing_runs()
        warns = [r for r in results if r.warn and r.ok is False]
        assert warns, (
            f"an artificially-unfenced document must still trip the "
            f"stale-fence WARN — doctor's check itself is UNCHANGED by "
            f"nexus-cotmr round 2 (the note-exemption was reverted), so "
            f"this proves the round-1 exemption is genuinely gone: {results}"
        )
