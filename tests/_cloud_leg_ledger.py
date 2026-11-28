# SPDX-License-Identifier: AGPL-3.0-or-later
"""The cloud-leg route-ledger check, shared by its test and the report-mode tests (RDR-258 Phase 4, nexus-2jomg.5).

Every route the wire ledger's ``## Unshipped`` section names must map to a client call in ``ROUTE_MAP``
(``tests/e2e/lib/cloud_leg.py``). In block mode (``NX_LEDGER_MODE`` unset) an unmapped route fails the check, as before; in
report mode each is listed as a finding and the check fails only when it examined no route (``scripts/ledger_mode.py``).
"""
from __future__ import annotations

from typing import Any

import ledger_mode


def check_routes(cl: Any, entries: list[Any]) -> int:
    """Run the check over *entries*; return how many routes were examined. Raises ``AssertionError`` on a block-mode
    finding, on a report-mode run that examined nothing, and when no step maps at all."""
    routes = cl.entry_routes(entries)
    assert routes, "no routes to examine: the check would be vacuous"
    steps, _skipped, unmapped = cl.release_steps(entries)
    if ledger_mode.is_report():
        findings = [f"{key} has no ROUTE_MAP entry: map it in tests/e2e/lib/cloud_leg.py ({why})" for key, why in unmapped]
        assert ledger_mode.settle("cloud-leg-routes", findings, len(routes))
    else:
        assert not unmapped, f"map these in ROUTE_MAP (or the release passes --accept-skip): {unmapped}"
    assert steps or unmapped
    return len(routes)
