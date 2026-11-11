# SPDX-License-Identifier: AGPL-3.0-or-later
"""A default `nx doctor` does not run the whole-catalog reads (F30, nexus-f3yqd.6).

Measured 2026-11-10 on a laptop tenant (dev doctor, its own step timings): the taxonomy link-drift census took
9-13 s and the shared catalog walk 22 s. Each now reports "not checked" in the default sweep and names the explicit
command that runs it. The rows are informational (no warning mark), so a fresh install's doctor stays clean.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

import nexus.health as h
from nexus.commands import doctor as doctor_mod


def test_the_default_sweep_names_check_taxonomy_without_calling_the_engine(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def _must_not_run() -> None:
        raise AssertionError("the link-drift census ran in the default sweep")

    monkeypatch.setattr(doctor_mod, "_run_check_taxonomy", _must_not_run)
    monkeypatch.setattr(doctor_mod, "_SUPPLEMENTARY_CHECK_NAMES", ("taxonomy",))
    doctor_mod._run_supplementary_checks()
    out = capsys.readouterr().out
    assert "--- taxonomy ---" in out
    assert "not checked" in out and "nx doctor --check-taxonomy" in out, out
    assert "⚠" not in out and "✗" not in out


_INDEX_RUN_LABELS = ("stale index-run fences", "failed index runs with hidden chunks", "tumbler allocator drift")


def test_the_skipped_index_run_rows_name_the_flag_and_are_marked_not_checked() -> None:
    rows = h._index_run_rows_not_checked()
    assert [r.label for r in rows] == list(_INDEX_RUN_LABELS)
    for r in rows:
        assert r.ok is True and r.skipped is True and not r.warn
        assert r.detail.startswith("not checked") and "nx doctor --check-index-runs" in r.detail, r.detail


def test_a_skipped_row_renders_as_not_checked_never_as_a_pass() -> None:
    rows = h._index_run_rows_not_checked()
    out, failed = h.format_health_for_cli(rows, local_mode=True)
    assert not failed
    assert "✓" not in out and "⚠" not in out and "✗" not in out, out
    assert out.count("[ ] ") == 3, out
    payload = __import__("json").loads(h.format_health_for_json(rows, local_mode=True))
    assert {c["status"] for c in payload["checks"]} == {"skipped"}
    assert payload["summary"]["skipped"] == 3 and payload["summary"]["ok"] == 0
    assert h.health_exit_code(rows) == 0


def test_check_index_runs_walks_once_and_runs_the_three_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    walks: list[int] = []
    seen: list[object] = []
    docs = [object()]
    monkeypatch.setattr(h, "_walk_documents_for_doctor", lambda: (walks.append(1), docs)[1])
    for name, label in zip(("_check_stale_indexing_runs", "_check_failed_runs_hidden_chunks", "_check_next_seq_drift"), _INDEX_RUN_LABELS):
        monkeypatch.setattr(h, name, lambda d, label=label: (seen.append(d), [h.HealthResult(label=label, ok=True, detail="none")])[1])
    rows = h.run_index_run_checks()
    assert walks == [1] and seen == [docs, docs, docs]
    assert [r.label for r in rows] == list(_INDEX_RUN_LABELS)


@pytest.mark.parametrize(("row", "fails"), [
    (h.HealthResult(label="tumbler allocator drift", ok=False, warn=True, detail="1.12 drifted"), False),
    (h.HealthResult(label="stale index-run fences", ok=False, detail="broken", fatal=True), True),
])
def test_check_index_runs_is_a_doctor_flag_that_exits_like_the_sweep(monkeypatch: pytest.MonkeyPatch, row: h.HealthResult, fails: bool) -> None:
    monkeypatch.setattr(h, "run_index_run_checks", lambda: [row])
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    result = CliRunner().invoke(doctor_mod.doctor_cmd, ["--check-index-runs"])
    assert row.label in result.output and row.detail in result.output, result.output
    assert result.exit_code == h.health_exit_code([row]), result.output
    assert (result.exit_code != 0) is fails, result.exit_code
    assert "--check-index-runs" in doctor_mod._OPT_IN_ONLY_CHECKS


@pytest.mark.parametrize("other", [["--check-taxonomy"], ["--json"]])
def test_check_index_runs_refuses_another_mode_and_json(other: list[str]) -> None:
    result = CliRunner().invoke(doctor_mod.doctor_cmd, ["--check-index-runs", *other])
    assert result.exit_code == 2 and "--check-index-runs" in result.output, result.output
