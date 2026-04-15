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
    """Each of the 9 seed plans runs end-to-end: plan_match hits the row,
    plan_run drives every step through the real default dispatcher, and
    every step output is a dict matching the runner contract.

    Success is measured structurally (dict-returns, no exceptions) — the
    model's prose is not asserted since operator output varies turn-to-
    turn even with a pinned schema.
    """
    import yaml

    from nexus.plans.matcher import plan_match
    from nexus.plans.runner import plan_run

    seed_path = _SEED_DIR / seed_name
    seed = yaml.safe_load(seed_path.read_text()) or {}
    description = seed.get("description") or seed.get("name") or seed_name

    # Match against the library — the seed should have been inserted by
    # the loader fixture and now be findable via description FTS.
    matches = plan_match(
        description, library=loaded_library, min_confidence=0.0, n=1,
        project="",
    )
    assert matches, f"seed {seed_name!r} did not match its own description"

    result = await plan_run(matches[0], bindings={})
    # Every step returns a dict; at least one step actually ran.
    assert result.steps, f"seed {seed_name!r} produced zero step outputs"
    for idx, step_out in enumerate(result.steps):
        assert isinstance(step_out, dict), (
            f"seed {seed_name!r} step {idx} returned "
            f"{type(step_out).__name__}, expected dict (runner contract)"
        )
