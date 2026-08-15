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
            "--collection", "knowledge",
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
            "--collection", "knowledge",
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

    def test_manifest_failure_stamps_failed(
        self, catalog_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import nexus.commands.store as store_mod

        title = "cotmr-cli-put-failed"
        monkeypatch.setattr(
            store_mod, "_store_put_manifest_direct",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("manifest write refused")),
        )
        result = _invoke_store_put(tmp_path, _local_t3(), title, "cli fence failure body")
        assert result.exit_code != 0
        assert _index_state_for(title) == "failed"


# ── CLI `nx memory promote` fences ───────────────────────────────────────────


class TestMemoryPromoteFence:
    def test_success_stamps_complete(self, catalog_env: Path, tmp_path: Path) -> None:
        title = "cotmr-promote-complete"
        result = _invoke_promote(tmp_path, _real_t3(), title, "promote fence success body")
        assert result.exit_code == 0, result.output
        assert _index_state_for(title) == "complete"

    def test_manifest_failure_stamps_failed(
        self, catalog_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        title = "cotmr-promote-failed"
        monkeypatch.setattr(
            "nexus.catalog.store_hook.store_put_manifest_direct",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("manifest write refused")),
        )
        result = _invoke_promote(tmp_path, _local_t3(), title, "promote fence failure body")
        assert result.exit_code != 0
        assert _index_state_for(title) == "failed"


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
        """KILL CONTROL / non-vacuity companion (critique item 5): with
        ``_fence_begin`` disabled (simulating the exact pre-cotmr gap —
        a producer that fires fire_batch/fire_store_chains without ever
        fencing), the document's index_state stays NULL and doctor's
        check WARNs — proving the clean result above is powered by the
        real fence wiring, not a coincidentally-quiet check. Mechanism
        note (critique T2 [21535] round 2): the NULL here comes from
        disabled-begin TOGETHER WITH this test's local T3 — against a
        real T3, completion can stamp 'complete' without a prior begin,
        so this control pins the wiring, not a begin-dependency in the
        engine. Patches the
        SAME symbol commands/store.py resolves via its deferred import
        (``from nexus.doc_indexer import _fence_begin`` — module-level
        patch target, matching the existing "test patch target" comment
        convention already on that import)."""
        import nexus.health as h

        monkeypatch.setattr(
            "nexus.doc_indexer._fence_begin",
            lambda *a, **k: None,
        )
        title = "cotmr-artificially-unfenced"
        # nexus-dbzxb (RDR-191 Phase 5 Python collateral, round 2): unlike
        # the other _local_t3() call above (test_manifest_failure_is_
        # explicit_error, which mocks the manifest write itself and never
        # reaches the real FK), this test's manifest write is REAL and
        # unmocked — fk_catalog_chunks_chunk requires a matching real
        # nexus.chunks row for it to land. Round 1 used seed_manifest_
        # chunks (idiom 1/2, a REAL T3 upsert) here, which broke this
        # test's own documented premise: per the docstring above, the
        # expected NULL index_state depends on the chunk being genuinely
        # ABSENT from the real engine's T3 (this test's local_t3 is a fake
        # client specifically so the manifest_complete ride's fail-closed
        # verify sees nothing and correctly refuses to stamp 'complete')
        # — "against a real T3, completion can stamp 'complete' without a
        # prior begin". A real seed reintroduces exactly the confound the
        # fake-T3 design exists to avoid. fk_dropped_for_dangling_seed
        # (idiom 3b) satisfies the FK without making the chunk genuinely
        # present, preserving the original test intent bit-for-bit.
        from tests._catalog_fixture_ops import fk_dropped_for_dangling_seed

        _content = "artificially unfenced body"
        with fk_dropped_for_dangling_seed():
            result = _invoke_store_put(tmp_path, _local_t3(), title, _content)
        assert result.exit_code == 0, result.output
        assert _index_state_for(title) is None, (
            "with _fence_begin disabled, index_state must stay NULL "
            "(manifest_complete's ride only stamps 'complete' for a "
            "document the fence actually began)"
        )

        results = h._check_stale_indexing_runs()
        warns = [r for r in results if r.warn and r.ok is False]
        assert warns, (
            f"an artificially-unfenced document must still trip the "
            f"stale-fence WARN — doctor's check itself is UNCHANGED by "
            f"nexus-cotmr round 2 (the note-exemption was reverted), so "
            f"this proves the round-1 exemption is genuinely gone: {results}"
        )
