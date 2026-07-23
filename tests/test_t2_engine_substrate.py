"""RDR-155 P4b P0a' (decision D-A): the engine-backed T2 test substrate.

Smoke + isolation contract for tests/_engine_substrate.py and the
``t2_service_env`` opt-in fixture: a bare ``T2Database`` in service mode
round-trips through the REAL engine over hermetic PG, and two tests'
tenants are invisible to each other (server-side RLS on the per-test
tenant — the whole isolation story for the conftest pin flip).
"""
from __future__ import annotations

from pathlib import Path

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
