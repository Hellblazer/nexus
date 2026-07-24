"""RDR-155 P4b P0a' (decision D-A): the engine-backed T2 test substrate.

Smoke + isolation contract for tests/_engine_substrate.py and the
``t2_service_env`` opt-in fixture: a bare ``T2Database`` in service mode
round-trips through the REAL engine over hermetic PG, and two tests'
tenants are invisible to each other (server-side RLS on the per-test
tenant — the whole isolation story for the conftest pin flip).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.db.t2 import T2Database


def _db(tmp_path: Path) -> T2Database:
    # The path arg is vestigial in service mode — exactly how the 317
    # existing construction sites will hit the substrate after the flip.
    return T2Database(tmp_path / "memory.db")


class TestEngineSubstrateSmoke:
    def test_memory_round_trip(self, t2_service_env, tmp_path) -> None:
        db = _db(tmp_path)
        db.memory.put("proj", "title-1", "content body", tags="a,b")
        row = db.memory.get("proj", "title-1")
        assert row is not None
        assert row["content"] == "content body"

    def test_per_test_tenant_starts_empty(self, t2_service_env, tmp_path) -> None:
        """Runs after test_memory_round_trip in file order: a FRESH tenant
        must not see the previous test's rows — the no-cleanup isolation
        contract the pin flip depends on."""
        db = _db(tmp_path)
        assert db.memory.get("proj", "title-1") is None

    def test_two_tenants_isolated_within_one_test(self, t2_service_env,
                                                  tmp_path, monkeypatch) -> None:
        """Tenant binds to the BEARER server-side (AuthFilter Decision 1)
        — a second minted token must not see the first tenant's rows.
        db_a keeps working because Http* stores bake their resolved token
        per instance; only NEW constructions pick up the swapped env."""
        from tests._engine_substrate import ensure_engine, mint_test_tenant

        db_a = _db(tmp_path)
        db_a.memory.put("proj", "only-in-a", "secret", tags="")
        _, other_token = mint_test_tenant(ensure_engine())
        monkeypatch.setenv("NX_SERVICE_TOKEN", other_token)
        db_b = _db(tmp_path)
        assert db_b.memory.get("proj", "only-in-a") is None
        assert db_a.memory.get("proj", "only-in-a") is not None

    def test_plan_library_round_trip(self, t2_service_env, tmp_path) -> None:
        """Second store class through the same substrate — the facade's
        store wiring, not just one endpoint."""
        db = _db(tmp_path)
        plan_id = db.plans.save_plan(
            query="how to test the substrate",
            plan_json='{"steps": []}',
            verb="query",
        )
        assert plan_id is not None
        assert db.plans.get_plan(plan_id) is not None


class TestB6encStoreHookEngineSubstrate:
    """nexus-b6enc CRE Minor 6: the C2/C3 primitives against the REAL
    engine catalog (``HttpCatalogClient``), not the sqlite-pinned local
    Catalog the main b6enc suite uses. Real registration, real direct
    manifest write + verify, real rollback — over the per-test engine
    tenant."""

    @pytest.fixture(autouse=True)
    def _fresh_shared_catalog_client(self):
        """The service-mode catalog factory memoizes one process-lifetime
        HttpCatalogClient (nexus-53x7s); it must rebind to THIS test's
        freshly minted tenant token."""
        from nexus.catalog.factory import (
            reset_shared_service_catalog_client_for_tests,
        )

        reset_shared_service_catalog_client_for_tests()
        yield
        reset_shared_service_catalog_client_for_tests()

    def test_register_manifest_rollback_round_trip(
        self, t2_service_env, tmp_path,
    ) -> None:
        from nexus.catalog.factory import make_catalog_reader
        from nexus.catalog.store_hook import (
            catalog_store_hook_tracked,
            rollback_minted_catalog_entry,
            single_chunk_manifest_metadata,
            store_put_manifest_direct,
        )

        content = "b6enc engine substrate smoke content"
        col = "knowledge__engine__bge-base-en-v15-768__v1"
        chash, metadatas = single_chunk_manifest_metadata(content)

        tumbler, created = catalog_store_hook_tracked(
            title="b6enc-engine-smoke", doc_id=chash, collection_name=col,
        )
        assert created is True and tumbler, (
            "first registration on a fresh tenant must MINT (created=True)"
        )

        # Idempotent dedup: the same doc_id must NOT mint a second row —
        # the created-flag contract the C2 compensation keys on.
        tumbler2, created2 = catalog_store_hook_tracked(
            title="b6enc-engine-smoke", doc_id=chash, collection_name=col,
        )
        assert created2 is False and tumbler2 == tumbler

        # Direct fail-loud manifest write + its verify leg, over the wire.
        store_put_manifest_direct(tumbler, metadatas)
        reader = make_catalog_reader()
        assert {r.chash for r in reader.get_manifest(tumbler)} == {chash}

        # C2 compensation over the wire: the minted row is deleted and
        # no longer resolvable by its chunk natural id.
        assert rollback_minted_catalog_entry(
            tumbler, original_error="engine smoke: simulated put failure",
        ) is True
        assert make_catalog_reader().by_doc_id(chash) is None
