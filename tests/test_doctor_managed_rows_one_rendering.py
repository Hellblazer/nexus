# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-njn1a finding 10: one reason, one rendering, on a managed (cloud) deployment.

Eight ``nx doctor`` rows read the database through local ``pg_credentials`` and a managed deployment has none.
On a cloud box (7.88.0 shakedown) ``nx doctor`` showed five of them as a warning and three (the RDR-225 partition
rows) as a green "not applicable: managed deployment", for the same fact: this client did not run the check.

Sam's decision (2026-10-28, recorded on nexus-g7ijj): every managed "not probeable from this client" row WARNS and
never renders a checkmark. Doctor's checkmark must never render for an unperformed check. A passing row there is
how ``upgrade_finish._poison_probe`` read an unverified store as clean (the code-review ship-blocker of round 1).
All eight rows render ``ok=False, warn=True`` with one shared reason text (``health._managed_not_probeable``, recognised by ``health.is_managed_not_probeable``).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

import nexus.health as h

_STORAGE = "_check_storage_service_health"
_ONE_ROW = (
    _STORAGE,
    "_check_migration_state",
    "_check_rls_present",
    "_check_tuple_table_bloat",
    "_check_tuple_sweep_freshness",
)
_PARTITION = "_check_tenant_model_partitions"


def _rows(name: str, missing):
    fn = getattr(h, name)
    with patch("nexus.config.is_local_mode", return_value=False):
        if name == _STORAGE:
            return fn(creds_path=missing, endpoint=None, http_get=None)
        return fn(creds_path=missing)


@pytest.mark.parametrize("name", sorted(_ONE_ROW + (_PARTITION,)))
def test_a_managed_deployment_renders_each_pg_credentials_row_as_a_warning(name, tmp_path) -> None:
    rows = _rows(name, tmp_path / "pg_credentials")
    assert rows, name
    for r in rows:
        assert r.ok is False and r.warn is True and not r.fatal, (name, r.label, r.ok, r.warn, r.detail)
        assert not r.detail.startswith("not applicable"), (name, r.label, r.detail)
        assert r.detail == h._MANAGED_NOT_PROBEABLE_DETAIL, (name, r.label, r.detail)


def test_the_sweep_has_eight_rows_for_this_reason_and_all_agree(tmp_path) -> None:
    """Vacuity guard: five single-row checks plus the three partition rows, one verdict, one text."""
    all_rows = [r for n in _ONE_ROW + (_PARTITION,) for r in _rows(n, tmp_path / "pg_credentials")]
    assert len(all_rows) == 8
    assert {(r.ok, r.warn, r.fatal) for r in all_rows} == {(False, True, False)}
    assert len({r.detail for r in all_rows}) == 1, "eight rows, one reason"
    assert all(h.is_managed_not_probeable(r) for r in all_rows)


def test_the_shared_reason_is_true_of_all_eight_rows() -> None:
    """The text is read by the operator on every one of the eight rows, so it may name only what is true of
    all of them: the credentials are local, the client does not hold them, nothing was checked. It does not
    claim the engine checks itself (it runs no equivalent of the partition read), and it cites nexus-y3wuu only
    for what that bead decided, that the client-side diagnostic path is local-only by design."""
    d = h._MANAGED_NOT_PROBEABLE_DETAIL
    assert "not probeable from this client" in d and "check was not run" in d
    assert "nexus-y3wuu" in d and "local-only by design" in d
    assert "docs/runbooks/rdr-225-cloud-deploy.md" in d, "the partition rows' operator query"
    assert "runs server-side" not in d and "engine runs no" not in d


def test_a_local_box_with_no_pg_credentials_keeps_its_own_warning(tmp_path) -> None:
    """The managed reason is not the local one: a local install with no pg_credentials is genuinely not
    configured, and keeps the text and the warning it had."""
    with patch("nexus.config.is_local_mode", return_value=True):
        (r,) = h._check_storage_service_health(creds_path=tmp_path / "pg_credentials", endpoint=None, http_get=None)
        parts = h._check_tenant_model_partitions(creds_path=tmp_path / "pg_credentials")
    assert r.ok is False and r.warn is True
    assert "pg_credentials absent" in r.detail and not h.is_managed_not_probeable(r)
    # The partition rows on a local box keep their rendering (not applicable, a pass) with the local text.
    assert {(p.ok, p.warn) for p in parts} == {(True, False)}
    assert all("pg_credentials absent" in p.detail for p in parts)
