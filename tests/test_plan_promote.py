# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for RDR-079 P6 — ``nx plan promote`` CLI with gates.

Closes SC-9: the gate rejects plans that fall below use-count or
success-rate thresholds; ``--dry-run`` reports the verdict without
any filesystem side effects.

Gates (shipped defaults):
  * ``use_count >= 3`` — the plan has actually been run three times.
  * ``success_count / (success_count + failure_count) >= 0.80`` — at
    least 80 percent of runs closed as success.
  * description clarity — non-empty, ≥ 20 characters.

Tier targets: ``project`` writes the YAML into ``.nexus/plans/`` of
the current repo; ``global`` writes into the plugin's
``plans/builtin/`` directory.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner


@pytest.fixture()
def library(tmp_path: Path):
    from nexus.db.migrations import _add_plan_dimensional_identity
    from nexus.db.t2.plan_library import PlanLibrary

    lib = PlanLibrary(tmp_path / "plans.db")
    _add_plan_dimensional_identity(lib.conn)
    lib.conn.commit()
    return lib


def _insert_plan(
    library, *,
    query: str = "test plan",
    plan_json: str | None = None,
    use_count: int = 0,
    success_count: int = 0,
    failure_count: int = 0,
    name: str = "promote-probe",
    scope: str = "project",
) -> int:
    body = plan_json or json.dumps(
        {"steps": [{"tool": "search", "args": {"query": "$intent"}}]},
    )
    plan_id = library.save_plan(
        query=query, plan_json=body, tags="rdr-079,test",
        project="nexus-test", name=name, verb="research", scope=scope,
        dimensions=json.dumps(
            {"verb": "research", "scope": scope, "strategy": "default"},
            sort_keys=True, separators=(",", ":"),
        ),
    )
    # Manually bump metrics — PlanLibrary's public increment methods
    # take a single plan_id; direct UPDATE is fine for test setup.
    with library._lock:
        library.conn.execute(
            "UPDATE plans SET use_count=?, success_count=?, failure_count=? "
            "WHERE id=?",
            (use_count, success_count, failure_count, plan_id),
        )
        library.conn.commit()
    return plan_id


# ── Gate logic (pure function, no I/O) ─────────────────────────────────────


def test_gate_pass_when_all_thresholds_met(library) -> None:
    from nexus.plans.promote import evaluate_gates

    plan_id = _insert_plan(
        library, use_count=5, success_count=5, failure_count=0,
        query="a suitably descriptive query over a dozen characters",
    )
    verdict = evaluate_gates(library, plan_id)
    assert verdict.passed is True, verdict.reasons
    assert verdict.reasons == []


def test_gate_fails_on_low_use_count(library) -> None:
    from nexus.plans.promote import evaluate_gates

    plan_id = _insert_plan(
        library, use_count=2, success_count=2, failure_count=0,
        query="a suitably descriptive query over a dozen characters",
    )
    verdict = evaluate_gates(library, plan_id)
    assert verdict.passed is False
    assert any("use_count" in r for r in verdict.reasons)


def test_gate_fails_on_low_success_rate(library) -> None:
    from nexus.plans.promote import evaluate_gates

    plan_id = _insert_plan(
        library, use_count=10, success_count=5, failure_count=5,
        query="a suitably descriptive query over a dozen characters",
    )
    verdict = evaluate_gates(library, plan_id)
    assert verdict.passed is False
    assert any("success_rate" in r for r in verdict.reasons)


def test_gate_fails_on_short_description(library) -> None:
    from nexus.plans.promote import evaluate_gates

    plan_id = _insert_plan(
        library, use_count=5, success_count=5, failure_count=0,
        query="short",
    )
    verdict = evaluate_gates(library, plan_id)
    assert verdict.passed is False
    assert any("description" in r for r in verdict.reasons)


def test_gate_fails_when_plan_missing(library) -> None:
    from nexus.plans.promote import evaluate_gates

    verdict = evaluate_gates(library, 9999)
    assert verdict.passed is False
    assert any("not found" in r for r in verdict.reasons)


def test_gate_zero_runs_counts_as_failure(library) -> None:
    """A plan with zero completed runs MUST NOT be promoted — no evidence."""
    from nexus.plans.promote import evaluate_gates

    plan_id = _insert_plan(
        library, use_count=0, success_count=0, failure_count=0,
        query="a suitably descriptive query over a dozen characters",
    )
    verdict = evaluate_gates(library, plan_id)
    assert verdict.passed is False


# ── CLI: --dry-run has zero side effects ───────────────────────────────────


def test_cli_dry_run_never_writes_file(library, tmp_path, monkeypatch) -> None:
    """SC-9: ``--dry-run`` MUST NOT create any YAML. Even when the gate
    passes, the file system is untouched."""
    from nexus.commands.plan_cmd import plan as plan_group

    plan_id = _insert_plan(
        library, use_count=5, success_count=5, failure_count=0,
        query="a suitably descriptive query over a dozen characters",
    )

    project_dir = tmp_path / "repo"
    (project_dir / ".nexus" / "plans").mkdir(parents=True)
    before = sorted((project_dir / ".nexus" / "plans").iterdir())

    runner = CliRunner()
    result = runner.invoke(
        plan_group,
        [
            "promote", str(plan_id),
            "--target", "project",
            "--dry-run",
            "--db-path", str(tmp_path / "plans.db"),
            "--repo-root", str(project_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    after = sorted((project_dir / ".nexus" / "plans").iterdir())
    assert before == after, "dry-run must not create files"
    assert "DRY RUN" in result.output or "dry run" in result.output.lower()


def test_cli_dry_run_reports_pass_or_fail(library, tmp_path) -> None:
    from nexus.commands.plan_cmd import plan as plan_group

    # Plan that will fail the gate.
    plan_id = _insert_plan(
        library, use_count=1, success_count=1, failure_count=0,
        query="a suitably descriptive query over a dozen characters",
    )

    runner = CliRunner()
    result = runner.invoke(
        plan_group,
        [
            "promote", str(plan_id),
            "--target", "project", "--dry-run",
            "--db-path", str(tmp_path / "plans.db"),
            "--repo-root", str(tmp_path),
        ],
    )
    # Dry-run surfaces the verdict even when the gate fails.
    assert result.exit_code != 0, "dry-run must flag failing gate in exit code"
    assert "use_count" in result.output


# ── CLI: non-dry-run path writes YAML only when gate passes ────────────────


def test_cli_promotion_writes_yaml_when_gate_passes(
    library, tmp_path,
) -> None:
    from nexus.commands.plan_cmd import plan as plan_group

    plan_id = _insert_plan(
        library, use_count=5, success_count=5, failure_count=0,
        query="a suitably descriptive query over a dozen characters",
        name="my-promoted-plan",
    )

    project_dir = tmp_path / "repo"
    (project_dir / ".nexus" / "plans").mkdir(parents=True)

    runner = CliRunner()
    result = runner.invoke(
        plan_group,
        [
            "promote", str(plan_id),
            "--target", "project",
            "--db-path", str(tmp_path / "plans.db"),
            "--repo-root", str(project_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    written = list((project_dir / ".nexus" / "plans").glob("*.yml"))
    assert written, "promotion must write at least one YAML file"
    text = written[0].read_text()
    assert "my-promoted-plan" in text or "steps" in text


def test_cli_promotion_refuses_when_gate_fails(library, tmp_path) -> None:
    from nexus.commands.plan_cmd import plan as plan_group

    plan_id = _insert_plan(
        library, use_count=1, success_count=0, failure_count=1,
        query="a suitably descriptive query over a dozen characters",
    )

    project_dir = tmp_path / "repo"
    (project_dir / ".nexus" / "plans").mkdir(parents=True)

    runner = CliRunner()
    result = runner.invoke(
        plan_group,
        [
            "promote", str(plan_id),
            "--target", "project",
            "--db-path", str(tmp_path / "plans.db"),
            "--repo-root", str(project_dir),
        ],
    )
    assert result.exit_code != 0
    written = list((project_dir / ".nexus" / "plans").glob("*.yml"))
    assert not written, "failed gate must not produce a file"


def test_cli_unknown_plan_id_fails_cleanly(library, tmp_path) -> None:
    from nexus.commands.plan_cmd import plan as plan_group

    runner = CliRunner()
    result = runner.invoke(
        plan_group,
        [
            "promote", "99999",
            "--target", "project", "--dry-run",
            "--db-path", str(tmp_path / "plans.db"),
            "--repo-root", str(tmp_path),
        ],
    )
    assert result.exit_code != 0
    assert "not found" in result.output.lower()
