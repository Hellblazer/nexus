# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fast, non-``integration`` unit coverage for
``tests/test_search_fanout_recall_parity.py``'s bounded-retry decision
logic (nexus-d9xt2 critique round 2 Significant).

The live-cloud recall-parity gate itself (``-m integration``, skipped by
default) is the only place that previously exercised
``_is_confirmed_regression`` -- meaning a regression in the RETRY logic
itself (e.g. reverting to "accept whichever measurement came back last,
win or lose") had no test coverage that runs in the default suite. This
module imports the pure decision function directly and pins its
contract with plain values -- no live network, no ``-m integration``
opt-in required.
"""
from __future__ import annotations

from tests.test_search_fanout_recall_parity import (
    _JACCARD_FLOOR,
    _is_confirmed_regression,
)


def test_first_measurement_at_or_above_floor_is_never_a_failure():
    assert _is_confirmed_regression(_JACCARD_FLOOR, None) is False
    assert _is_confirmed_regression(1.0, None) is False


def test_below_floor_first_measurement_that_does_not_reproduce_is_not_a_failure():
    """The jitter case this bead's own live measurement documented:
    0.818 -> 1.0 on an immediate rerun. A single dip below the floor,
    followed by a retry that clears it, must NOT fail the gate."""
    assert _is_confirmed_regression(0.818, 1.0) is False
    assert _is_confirmed_regression(0.0, _JACCARD_FLOOR) is False


def test_below_floor_first_measurement_that_reproduces_on_retry_is_a_confirmed_failure():
    """The critique's 'a second failure fails' requirement: if the retry
    ALSO comes back below the floor, this is a real, reproducing
    regression and must be reported as a failure -- not silently
    accepted because "we already retried once"."""
    assert _is_confirmed_regression(0.5, 0.5) is True
    assert _is_confirmed_regression(0.0, 0.0) is True
    assert _is_confirmed_regression(0.818, 0.85) is True  # still < floor


def test_below_floor_first_measurement_with_no_retry_recorded_is_a_confirmed_failure():
    """A caller bug (a below-floor first measurement that never actually
    ran the retry) must fail loud, not be silently treated as passing
    jitter -- the function has no way to know the dip didn't reproduce
    if nothing checked."""
    assert _is_confirmed_regression(0.5, None) is True
