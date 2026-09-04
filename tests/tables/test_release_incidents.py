# SPDX-License-Identifier: AGPL-3.0-or-later
"""The two production incidents RDR-201 was justified by, against the table as built.

RDR-201's Problem Statement said "a table would have reported both before
either shipped". Nothing asserted that until the 2026-09-04 reanalysis
asked where the cells were. This file names them.

GH #1402 (2026-07): a floor-bumped client was published with no deploy
armed, so cloud clients refused the managed service as below-identity. In
the table that is the bare floor gate seeing a reachable cloud that reports
a version below ``REQUIRED_ENGINE_VERSION``: it must refuse, exit 1, so the
release stops before the tag.

The 7.1.0/v0.1.62 inversion is NOT pinned here, deliberately. Its failure
class is in the table (the two ``ledger_blocked`` rows, exit 1, asserted
below), but Sam's 2026-09-02 ruling (nexus-j9z30.26) holds that the
incident itself was which moment a human ran which gate, which the skills
carry and the table's inputs cannot express. A test naming a cell as
"the inversion" would be the vacuous kind.
"""
from __future__ import annotations

import pytest

import release_choreography as choreo  # scripts/ is on pythonpath via pyproject


def test_gh_1402_cloud_below_floor_on_the_bare_gate_refuses() -> None:
    row = choreo.resolve_choreography_row(
        "check_floor_bare", {"pin_currency": "passes", "probe": "success_stale"}
    )
    assert row.id == "check_floor_bare::bare_probe_stale_via_success", row.id
    assert row.outcome["exit_code"] == "1", "a reachable cloud below the floor must stop the release"


def test_gh_1402_shape_is_allowed_only_when_a_deploy_is_armed() -> None:
    """The same cloud state passes under --paired-deploy, because the deploy
    fires at tag push. That is the paired-release choreography, and it is
    the reason #1402's cell is a refusal only on the BARE gate."""
    row = choreo.resolve_choreography_row(
        "check_floor_paired", {"battery": "passes", "probe": "success_below_floor"}
    )
    assert row.id == "check_floor_paired::paired_probe_ack_via_success", row.id
    assert row.outcome["exit_code"] == "0", "with the deploy armed the same cloud state is allowed"


@pytest.mark.parametrize("probe", ["unreachable", "ms_error"])
def test_an_unverifiable_cloud_never_reads_as_current_on_the_bare_gate(probe: str) -> None:
    """The failure class #1402 belongs to: a gate that cannot see the cloud
    must not pass. Both unverifiable probes are non-zero."""
    row = choreo.resolve_choreography_row(
        "check_floor_bare", {"pin_currency": "passes", "probe": probe}
    )
    assert row.outcome["exit_code"] != "0", row.id


@pytest.mark.parametrize("function", ["check_wire_contract_ledger", "check_client_lag_ledger"])
def test_the_inversion_failure_class_is_a_refusal(function: str) -> None:
    """A blocking ledger entry stops the release. This is the class the
    7.1.0/v0.1.62 inversion belongs to, not the incident."""
    row = choreo.resolve_choreography_row(function, {"ledger": "blocking"})
    assert row.id == f"{function}::ledger_blocked"
    assert row.outcome["exit_code"] == "1"
