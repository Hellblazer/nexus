# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live positive/negative controls for the RDR-196 .p2a operator quality
proxy (nexus-nyry9.14, the Phase 2 GATE).

Dispatches REAL ``claude -p`` calls at the strong tier -- costs real
money, same convention already established by
``tests/test_operator_dispatch.py::TestClaudeDispatchLiveUsage`` and
``::TestClaudeDispatchPerOperatorSchemaCheapTier`` (RDR-196 .p2b): skip
loudly when ``claude`` isn't authenticated, same
``_claude_auth_available()`` helper duplicated per that file's own
precedent rather than a shared fixture.

MARKED ``lived_in`` (nexus-pc15o, 2026-08-23). This file originally said
``integration`` only, on the belief that ``lived_in`` was "reserved for
the 5 enumerated whole-file class-D tests the local-service gate's closed
list tracks". That belief was wrong when it was written and was falsified
the same day: ``tests/test_lived_in_marker_registration.py`` asserts a
MINIMUM registry (each listed file must carry the marker), never a
maximum, and nexus-nyry9.11 / nexus-nyry9.16 added a sixth and seventh
``lived_in`` file on 2026-08-20 -- hours after this one landed. The
marker's registered meaning in pyproject.toml is exactly what this file
does: "dispatches real ``claude -p``". The cost of the miss was eight red
nightlies: these 18 tests can never authenticate a ``claude`` CLI on the
CI runner, so they skipped there and spent the local-service gate's skip
BUDGET (25) that exists to detect coverage collapse -- 51 skips against
it, with zero test failures in the run.

Budget discipline (bead DO 5: "run for real ONCE per operator"): the two
live dispatches per operator are cached at module scope
(``_live_cache``) so the positive-control test and the negative-control
test for the SAME operator share one pair of dispatches rather than
paying for it twice. Total live dispatches for a full run of this file:
2 x 6 operators = 12 -- see T2 ``nexus_rdr/196-phase2-quality-proxy`` for
the recorded budget accounting.

Run with:
  uv run pytest -m integration tests/test_operator_proxy_controls.py
"""
from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

from bench.operator_proxy import BUILDERS, DEGRADERS, score
from bench.operator_proxy_metrics import THRESHOLDS

# Whole-file class-D: EVERY test here dispatches real `claude -p`. Module
# level rather than per-class so a test added to this file later is carved
# out by construction instead of by the author remembering.
pytestmark = [pytest.mark.integration, pytest.mark.lived_in]

_OPERATORS = tuple(THRESHOLDS)  # the 6 in-scope operators, insertion order


def _claude_auth_available() -> bool:
    try:
        result = subprocess.run(
            ["claude", "auth", "status", "--json"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False
        data = json.loads(result.stdout)
        return bool(data.get("loggedIn") or data.get("isLoggedIn"))
    except Exception:
        return False


_live_cache: dict[str, tuple[dict, dict, list]] = {}
_cache_lock = asyncio.Lock()


async def _get_or_dispatch_pair(operator_name: str) -> tuple[dict, dict, list]:
    """Return (output_a, output_b, usage_records) for *operator_name*,
    dispatching live exactly once per operator per test session
    (module-scope cache).

    Code-review finding (2026-08-21): the pytest-driven live path
    previously called ``claude_dispatch`` WITHOUT ``usage_sink=``, so
    ``DispatchUsage`` (canonical model id / cost) was never captured on
    this path -- unlike the CLI driver, which does. RDR-196 R3 (verify
    the CANONICAL resolved model, not just the requested alias) was
    therefore unverified on the path the dev notes said was actually
    used for operator_filter's live run. Fixed: usage_sink threaded
    through and cached alongside the outputs.
    """
    async with _cache_lock:
        if operator_name not in _live_cache:
            from nexus.operators.dispatch import DispatchUsage, claude_dispatch
            from nexus.operators.model_tiers import resolve_model_for_tier

            prompt, schema = BUILDERS[operator_name]()
            model = resolve_model_for_tier("strong")
            usage: list[DispatchUsage] = []
            output_a = await claude_dispatch(
                prompt, schema, timeout=90.0, model=model, operator=operator_name,
                usage_sink=usage,
            )
            output_b = await claude_dispatch(
                prompt, schema, timeout=90.0, model=model, operator=operator_name,
                usage_sink=usage,
            )
            _live_cache[operator_name] = (output_a, output_b, usage)
        return _live_cache[operator_name]


class TestPositiveControl:
    """Strong-vs-strong on the same fixed input must score >= threshold.
    If it doesn't, either the metric or the threshold is wrong -- see the
    bead's own "non-vacuity" section."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("operator_name", _OPERATORS)
    async def test_strong_vs_strong_meets_threshold(self, operator_name: str) -> None:
        if not _claude_auth_available():
            pytest.skip("claude CLI not on PATH or not authenticated -- live dispatch skipped")

        output_a, output_b, _usage = await _get_or_dispatch_pair(operator_name)
        result = score(operator_name, output_a, output_b)
        threshold = THRESHOLDS[operator_name]

        if result is None:
            pytest.fail(
                f"{operator_name}: metric returned None (undefined) on a "
                f"live strong-vs-strong pair -- cannot evaluate the positive control"
            )
        assert result >= threshold, (
            f"{operator_name}: positive control scored {result:.3f}, "
            f"below threshold {threshold} -- outputs:\n"
            f"A={output_a}\nB={output_b}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("operator_name", _OPERATORS)
    async def test_canonical_model_id_recorded(self, operator_name: str) -> None:
        """RDR-196 R3: DispatchUsage must record the CANONICAL resolved
        model id (not just the requested alias) on this pytest-driven
        live path too -- a silent alias-resolution bug would otherwise go
        undetected here even though the CLI driver's path already checks
        it via ControlResult.canonical_models."""
        if not _claude_auth_available():
            pytest.skip("claude CLI not on PATH or not authenticated -- live dispatch skipped")

        _output_a, _output_b, usage = await _get_or_dispatch_pair(operator_name)
        assert len(usage) == 2, f"{operator_name}: expected 2 DispatchUsage records, got {len(usage)}"
        for record in usage:
            assert record.model is not None and record.model.strip(), (
                f"{operator_name}: DispatchUsage.model is empty/None -- "
                f"canonical model id was not recorded for this live dispatch"
            )


class TestNegativeControl:
    """Strong vs a synthetically degraded copy of the SAME run must score
    < threshold. No extra dispatch -- reuses the pair from the positive
    control's cache."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("operator_name", _OPERATORS)
    async def test_strong_vs_degraded_fails_threshold(self, operator_name: str) -> None:
        if not _claude_auth_available():
            pytest.skip("claude CLI not on PATH or not authenticated -- live dispatch skipped")

        output_a, _output_b, _usage = await _get_or_dispatch_pair(operator_name)
        degraded = DEGRADERS[operator_name](output_a)
        result = score(operator_name, output_a, degraded)
        threshold = THRESHOLDS[operator_name]

        if result is None:
            pytest.fail(
                f"{operator_name}: metric returned None (undefined) on a "
                f"live-vs-degraded pair -- cannot evaluate the negative control"
            )
        assert result < threshold, (
            f"{operator_name}: negative control scored {result:.3f}, "
            f"did NOT fall below threshold {threshold} -- the proxy cannot "
            f"distinguish a deliberately degraded output; output:\n{output_a}"
        )
