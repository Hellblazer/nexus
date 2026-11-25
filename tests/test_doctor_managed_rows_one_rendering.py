# SPDX-License-Identifier: AGPL-3.0-or-later
"""One reason, one rendering, on a managed (cloud) deployment: not applicable.

Eight ``nx doctor`` rows read the database through local ``pg_credentials`` and a managed deployment has none:
Storage service health, Schema migrations, RLS policies, tuples.dead_tuple_ratio, tuples.sweep_freshness and the
three RDR-225 partition rows (Tenant partitions, Model partitions, Partition leaves).

History: 7.88.0 showed five as a warning and three as a green "not applicable". Sam's decision of 2026-10-28
(nexus-g7ijj) made all eight warn, so no unperformed check renders a checkmark. Sam's ruling of 2026-11-22
(T2 nexus/sam-ruling-doctor-cleanup-2026-11-22, "show them as n/a") replaced the warning: the check does not apply
to a client that holds no database credentials, so the row is informational (``ok=True, skipped=True``, rendered
``[ ]``, JSON status ``skipped``), counted in neither the warnings nor the verdict, and a managed box can reach a
green doctor. It still never renders a checkmark: ``health.is_managed_not_probeable`` recognises it by its reason
text, which is how ``upgrade_finish._poison_probe`` keeps an unverified store from reading as clean.
A local install is unchanged.
"""
from __future__ import annotations

import json
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
_EIGHT_LABELS = {
    "Storage service health", "Schema migrations", "RLS policies", "tuples.dead_tuple_ratio",
    "tuples.sweep_freshness", "Tenant partitions", "Model partitions", "Partition leaves",
}


def _rows(name: str, missing, *, local: bool = False):
    fn = getattr(h, name)
    with patch("nexus.config.is_local_mode", return_value=local):
        if name == _STORAGE:
            return fn(creds_path=missing, endpoint=None, http_get=None)
        return fn(creds_path=missing)


@pytest.mark.parametrize("name", sorted(_ONE_ROW + (_PARTITION,)))
def test_a_managed_deployment_renders_each_pg_credentials_row_as_not_applicable(name, tmp_path) -> None:
    rows = _rows(name, tmp_path / "pg_credentials")
    assert rows, name
    for r in rows:
        assert (r.ok, r.skipped, r.warn, r.fatal) == (True, True, False, False), (name, r.label, r.detail)
        assert r.detail == h._MANAGED_NOT_PROBEABLE_DETAIL, (name, r.label, r.detail)
        assert "not probeable from this client" in r.detail and "was not run" in r.detail


def test_the_sweep_has_eight_rows_for_this_reason_and_all_agree(tmp_path) -> None:
    """Vacuity guard: five single-row checks plus the three partition rows, one verdict, one text."""
    all_rows = [r for n in _ONE_ROW + (_PARTITION,) for r in _rows(n, tmp_path / "pg_credentials")]
    assert len(all_rows) == 8
    assert {r.label for r in all_rows} == _EIGHT_LABELS
    assert {(r.ok, r.skipped, r.warn, r.fatal) for r in all_rows} == {(True, True, False, False)}
    assert len({r.detail for r in all_rows}) == 1, "eight rows, one reason"
    assert all(h.is_managed_not_probeable(r) for r in all_rows)


def test_the_eight_rows_are_in_no_warning_count_and_leave_the_verdict_green(tmp_path) -> None:
    all_rows = [r for n in _ONE_ROW + (_PARTITION,) for r in _rows(n, tmp_path / "pg_credentials")]
    summary = json.loads(h.format_health_for_json(all_rows, local_mode=False))["summary"]
    assert summary == {"total": 8, "ok": 0, "warn": 0, "fail": 0, "skipped": 8}
    text, failed = h.format_health_for_cli(all_rows, local_mode=False)
    assert failed is False and "⚠" not in text and "✓" not in text
    assert text.count("  [ ] ") == 8
    assert h.health_exit_code(all_rows) == h.EXIT_HEALTHY


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
    (r,) = _rows(_STORAGE, tmp_path / "pg_credentials", local=True)
    assert r.ok is False and r.warn is True and not r.skipped
    assert "pg_credentials absent" in r.detail and not h.is_managed_not_probeable(r)
    # The partition rows on a local box keep their rendering (not applicable, a pass) with the local text.
    parts = _rows(_PARTITION, tmp_path / "pg_credentials", local=True)
    assert {(p.ok, p.warn, p.skipped) for p in parts} == {(True, False, False)}
    assert all("pg_credentials absent" in p.detail for p in parts)


def test_a_managed_row_is_recognised_as_the_marker_and_a_plain_pass_is_not(tmp_path) -> None:
    """The poison probe (``upgrade_finish``) reads the marker, not the rendering, so an n/a row never passes
    for a verified store."""
    (r,) = _rows("_check_migration_state", tmp_path / "pg_credentials")
    assert h.is_managed_not_probeable(r)
    assert not h.is_managed_not_probeable(h.HealthResult(label="Schema migrations", ok=True, detail="up to date"))


def test_the_human_output_counts_the_rows_that_were_not_checked(tmp_path) -> None:
    """A green managed doctor says nothing about these rows, so the output ends by saying how many were skipped."""
    all_rows = [r for n in _ONE_ROW + (_PARTITION,) for r in _rows(n, tmp_path / "pg_credentials")]
    text, _failed = h.format_health_for_cli(all_rows, local_mode=False)
    assert text.rstrip().splitlines()[-1] == "8 rows not checked on this deployment."
    one, _ = h.format_health_for_cli(all_rows[:1], local_mode=False)
    assert one.rstrip().splitlines()[-1] == "1 row not checked on this deployment."
    none, _ = h.format_health_for_cli([h.HealthResult(label="Python", ok=True, detail="3.12")], local_mode=False)
    assert "not checked" not in none
