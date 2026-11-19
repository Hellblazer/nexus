# SPDX-License-Identifier: AGPL-3.0-or-later
"""The arithmetic of the T2 starvation gates in tests/test_t2_concurrency.py, on fixed samples.

Kept out of that module because its ``pytestmark`` provisions a tenant for every test, and this one touches no
storage (the pytest-gate provisioned-tenant ratchet counts it).
"""
from __future__ import annotations

import pytest

from tests.test_t2_concurrency import _LOAD_ROUNDS, _READS_PER_PHASE, _assert_not_starved


def test_the_starvation_gate_reads_the_runs_own_tail() -> None:
    """The gate's arithmetic on the observed numbers: develop run 44337073652's slow-box sample passes (nothing was
    starved), and a read blocked behind a writer fails on a fast box and on a slow one."""
    rows = _LOAD_ROUNDS * _READS_PER_PHASE

    def sample(median: float, p95: float) -> list[float]:
        return sorted([median] * (rows - rows // 10) + [p95] * (rows // 10))

    _assert_not_starved("slow box, not starved", sample(19.79, 56.15), sample(24.86, 60.36), 50.0)
    with pytest.raises(AssertionError, match="starvation ceiling"):
        _assert_not_starved("fast box, starved tail", sample(1.5, 2.0), sample(1.6, 200.0), 50.0)
    with pytest.raises(AssertionError, match="starvation ceiling"):
        _assert_not_starved("slow box, starved tail", sample(19.79, 56.15), sample(24.86, 600.0), 50.0)
    with pytest.raises(AssertionError, match="median inflated"):
        _assert_not_starved("every read serialized", sample(1.5, 2.0), sample(40.0, 45.0), 50.0)
