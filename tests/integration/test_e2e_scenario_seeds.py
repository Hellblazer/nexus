# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end scenario-seed tests — RDR-079 P7 (nexus-wc3.3).

One parametrized test per seed YAML shipped at
``nx/plans/builtin/*.yml``: plan_match the seed's description, run the
matched plan through the real ``_default_dispatcher``, assert each step
emits a runner-contract-conformant dict.

These tests require live ``claude auth`` for operator steps. They are
marked ``@pytest.mark.integration`` and are skipped by default. Enable
with ``pytest -m integration``.

SC coverage:
  * SC-1 — all 9 seeds execute end-to-end.
  * SC-5 — unit-suite regression: this module is opt-in, so a failing
    live seed doesn't block the deterministic CI suite.
  * SC-8 — per-seed latency is recorded by the pool's structlog
    ``pool_dispatch_turn`` events (not asserted here — baselines live in
    ``docs/rdr/rdr-079-latency-baselines.md``).

Every test carries an inline ``claude auth status`` probe and is skipped
(not failed) if auth is unavailable — lets the module load on
developer machines without credentials configured.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import pytest_asyncio

pytestmark = pytest.mark.integration


_SEED_DIR = Path(__file__).resolve().parents[2] / "nx" / "plans" / "builtin"


def _claude_auth_available() -> bool:
    """Return True iff ``claude auth status --json`` reports loggedIn."""
    try:
        result = subprocess.run(
            ["claude", "auth", "status", "--json"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    return bool(data.get("loggedIn"))


_SEEDS = sorted(p.name for p in _SEED_DIR.glob("*.yml")) if _SEED_DIR.exists() else []


@pytest.fixture(scope="module", autouse=True)
def _skip_without_auth():
    if not _claude_auth_available():
        pytest.skip(
            "claude auth not available — SC-10 unit tests cover the "
            "degradation branch; these are opt-in live-auth tests.",
        )


@pytest_asyncio.fixture(autouse=True)
async def _reset_pool_between_tests():
    """Every parametrized test runs in a fresh event loop (pytest-asyncio
    default), and the operator pool's subprocess StreamReader is bound
    to the loop that spawned it. Without this teardown the SECOND test
    in the parametrized series hits
    ``RuntimeError: got Future ... attached to a different loop``.

    Shutdown every live pool (kill its workers, close its session)
    BEFORE clearing the singletons so subprocesses don't leak across
    tests.
    """
    yield
    from nexus import mcp_infra
    # Shutdown per-operator pools (where workers actually live).
    for pool in list(mcp_infra._operator_pools.values()):
        try:
            await pool.shutdown()
        except Exception:
            pass
    # Shutdown the default-bucket pool if any.
    default_pool = mcp_infra._operator_pool
    if default_pool is not None:
        try:
            await default_pool.shutdown()
        except Exception:
            pass
    mcp_infra.reset_operator_pool()


@pytest.fixture()
def library(tmp_path: Path):
    from nexus.db.migrations import _add_plan_dimensional_identity
    from nexus.db.t2.plan_library import PlanLibrary

    lib = PlanLibrary(tmp_path / "plans.db")
    _add_plan_dimensional_identity(lib.conn)
    lib.conn.commit()
    return lib


@pytest.fixture()
def loaded_library(library, tmp_path: Path):
    """Seed the library with the 9 builtin plans."""
    from nexus.plans.loader import load_all_tiers

    plugin_root = _SEED_DIR.parents[1]
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    load_all_tiers(
        plugin_root=plugin_root, repo_root=repo_root, library=library,
    )
    return library


@pytest.mark.asyncio
@pytest.mark.parametrize("seed_name", _SEEDS)
async def test_seed_executes_end_to_end(seed_name: str, loaded_library) -> None:
    """Each of the 9 seed plans runs end-to-end via the real
    ``_default_dispatcher``. The test looks the plan up by its exact
    ``(project, dimensions)`` identity (NOT via ``plan_match``) so the
    scoring loop doesn't conflate one seed with another. Each step's
    output must be a dict (runner contract).

    All ``required_bindings`` are satisfied with plausible placeholder
    strings so the run gets past binding-resolution. The seed plans
    are free to fail on semantics (bad ``$stepN.field`` refs, missing
    step outputs, etc.) — those failures are the P7 finding, surfaced
    here as test output.

    Success is measured structurally (dict returns, no runner error);
    model prose is not asserted since operator output varies turn-to-
    turn even with a pinned schema.
    """
    import json as _json

    import yaml

    from nexus.plans.match import Match
    from nexus.plans.runner import plan_run
    from nexus.plans.schema import canonical_dimensions_json

    seed_path = _SEED_DIR / seed_name
    seed = yaml.safe_load(seed_path.read_text()) or {}

    dims = seed.get("dimensions") or {}
    dims_json = canonical_dimensions_json(dims)
    project_label = "" if dims.get("scope") == "global" else dims.get("scope", "")
    row = loaded_library.get_plan_by_dimensions(
        project=project_label, dimensions=dims_json,
    )
    assert row is not None, (
        f"seed {seed_name!r} was not seeded under "
        f"project={project_label!r} dims={dims_json!r}"
    )

    # Fabricate harmless placeholders for every required binding so the
    # plan clears binding resolution; let the dispatcher handle the
    # rest.
    plan_json_body = _json.loads(row["plan_json"])
    required = list(plan_json_body.get("required_bindings") or [])
    bindings = {name: f"rdr-079-p7-placeholder-{name}" for name in required}

    match = Match(
        plan_id=row["id"], name=row.get("name") or seed_name,
        description=row.get("query") or "", confidence=1.0,
        dimensions=dims,
        tags=row.get("tags") or "", plan_json=row["plan_json"],
        required_bindings=required,
        optional_bindings=list(plan_json_body.get("optional_bindings") or []),
        default_bindings=_json.loads(row.get("default_bindings") or "null") or {},
        parent_dims=None,
    )
    result = await plan_run(match, bindings=bindings)
    assert result.steps, f"seed {seed_name!r} produced zero step outputs"
    for idx, step_out in enumerate(result.steps):
        assert isinstance(step_out, dict), (
            f"seed {seed_name!r} step {idx} returned "
            f"{type(step_out).__name__}, expected dict (runner contract)"
        )
