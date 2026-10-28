# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-njn1a finding 10: one reason, one rendering, on a managed (cloud) deployment.

Eight ``nx doctor`` rows read the database through local ``pg_credentials`` and a managed deployment has none.
On a cloud box (7.88.0 shakedown) ``nx doctor`` showed five of them as a warning and three as a green
"not applicable: managed deployment", for the same reason. The convention the codebase documents is the second:
a row with nothing for this client to check is a pass that says "not applicable: ...", never a warning
(``health._managed_not_applicable``; the RDR-225 partition rows state it in their docstring).

The partition rows keep their own reason text on purpose (the engine runs no equivalent of their local admin read;
nexus-3wh8d.18), so this pins the shared prefix and the ok/warn/fatal state, not the whole sentence.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

import nexus.health as h

_MANAGED_PREFIX = "not applicable: managed deployment"


def _rows(name: str, missing):
    fn = getattr(h, name)
    with patch("nexus.config.is_local_mode", return_value=False):
        if name == "_check_storage_service_health":
            return fn(creds_path=missing, endpoint=None, http_get=None)
        return fn(creds_path=missing)


_FIVE = (
    "_check_storage_service_health",
    "_check_migration_state",
    "_check_rls_present",
    "_check_tuple_table_bloat",
    "_check_tuple_sweep_freshness",
)
_PARTITION = ("_check_tenant_model_partitions",)


@pytest.mark.parametrize("name", sorted(_FIVE + _PARTITION))
def test_a_managed_deployment_renders_each_pg_credentials_row_as_not_applicable(name, tmp_path) -> None:
    rows = _rows(name, tmp_path / "pg_credentials")
    assert rows, name
    for r in rows:
        assert r.ok is True and r.warn is False and not r.fatal, (name, r.label, r.detail)
        assert r.detail.startswith(_MANAGED_PREFIX), (name, r.label, r.detail)


def test_the_sweep_has_eight_rows_for_this_reason_and_all_agree(tmp_path) -> None:
    """Vacuity guard: five single-row checks plus the three partition rows, none of them a warning."""
    all_rows = [r for n in _FIVE + _PARTITION for r in _rows(n, tmp_path / "pg_credentials")]
    assert len(all_rows) == 8
    assert {(r.ok, r.warn) for r in all_rows} == {(True, False)}


def test_a_local_box_with_no_pg_credentials_still_warns(tmp_path) -> None:
    """The collapse is for the managed reason only: a local install with no pg_credentials is genuinely not
    configured, and that stays a soft warning."""
    with patch("nexus.config.is_local_mode", return_value=True):
        (r,) = h._check_storage_service_health(creds_path=tmp_path / "pg_credentials", endpoint=None, http_get=None)
    assert r.ok is False and r.warn is True
    assert "pg_credentials absent" in r.detail
