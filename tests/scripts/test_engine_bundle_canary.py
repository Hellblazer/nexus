# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scripts/engine_bundle_canary.py``: parse, compare and exit logic, plus the workflow that runs it.

The script's pure core takes the pytest output and the ceiling file and returns an exit code and the
lines to print, so every verdict path runs here without a network, a bundle or an engine. The workflow
pins (lint bucket) hold the cost discipline the canary was designed under: it fires once per engine tag
(release published, engine-service-v* only, or a dispatch naming a tag) and never on a push or pull
request, and its concurrency group is per tag. ``scripts/`` is on ``pythonpath``.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

import engine_bundle_canary as canary

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "engine-bundle-canary.yml"
ENGINE_RELEASE_WORKFLOW = REPO / ".github" / "workflows" / "engine-service-release.yml"

TAG = "engine-service-v0.1.195"
EXACT = canary.Ceiling(2578, 0.0)

# What the terminal summary of a real run prints (tests/_ddl_counter.py::summary_line).
_DDL_LINE = "ddl: migration=2578 tests=112 per_test=1.00 wal: migration=2.0MB tests=1.0MB per_test=9KB (report: /tmp/x)"


def _evaluate(measured, *, passed=3, rc=0, ceiling=EXACT):
    return canary.evaluate(tag=TAG, measured=measured, passed=passed, pytest_rc=rc, ceiling=ceiling)


class TestParse:
    def test_reads_the_migration_number_from_the_summary_line(self) -> None:
        out = f"....\n{_DDL_LINE}\n3 passed in 4.10s\n"
        assert canary.parse_migration_ddl(out) == 2578
        assert canary.parse_passed(out) == 3

    def test_a_line_that_is_not_at_the_start_of_a_line_does_not_count(self) -> None:
        assert canary.parse_migration_ddl("note: ddl: migration=7 was measured") is None

    def test_no_line_is_none_not_zero(self) -> None:
        assert canary.parse_migration_ddl("3 passed in 1s\nddl: no substrate counted (report: /x)\n") is None

    def test_the_last_line_wins(self) -> None:
        assert canary.parse_migration_ddl("ddl: migration=1 tests=1\nddl: migration=2 tests=1\n") == 2

    def test_no_passed_summary_is_zero(self) -> None:
        assert canary.parse_passed("no tests ran in 0.01s") == 0


class TestCeiling:
    def test_reads_the_committed_ceiling_file(self) -> None:
        ceiling = canary.read_ceiling()
        assert ceiling.migration_ddl_per_worker > 0
        assert ceiling.tolerance == 0.0, "the migration's DDL count is exact; the canary assumes it"

    def test_reads_a_value_and_its_tolerance(self, tmp_path: Path) -> None:
        f = tmp_path / "c.toml"
        f.write_text(textwrap.dedent("""\
            [python_unit]
            migration_ddl_per_worker = 100
            migration_ddl_per_worker_tolerance = 0.05
            """))
        assert canary.read_ceiling(f) == canary.Ceiling(100, 0.05)

    @pytest.mark.parametrize("body", ["[python_unit]\n", "[python_unit]\nmigration_ddl_per_worker = \"measure\"\n"])
    def test_a_missing_or_unmeasured_ceiling_raises(self, tmp_path: Path, body: str) -> None:
        f = tmp_path / "c.toml"
        f.write_text(body)
        with pytest.raises((KeyError, ValueError)):
            canary.read_ceiling(f)

    @pytest.mark.parametrize(("measured", "ok"), [(2578, True), (2579, False), (2577, False)])
    def test_zero_tolerance_is_exact_in_both_directions(self, measured: int, ok: bool) -> None:
        assert canary.within_ceiling(measured, EXACT) is ok

    def test_a_fractional_tolerance_widens_the_band(self) -> None:
        wide = canary.Ceiling(1000, 0.01)
        assert canary.within_ceiling(1010, wide) and canary.within_ceiling(990, wide)
        assert not canary.within_ceiling(1011, wide)


class TestVerdict:
    def test_equal_is_exit_zero(self) -> None:
        code, lines = _evaluate(2578)
        assert code == canary.EXIT_OK
        assert "2578" in lines[0] and TAG in lines[0]

    def test_a_higher_number_names_the_measurement_the_ceiling_and_the_remedy(self) -> None:
        code, lines = _evaluate(2725)
        text = "\n".join(lines)
        assert code == canary.EXIT_MISMATCH
        assert "ddl: migration=2725" in text
        assert "migration_ddl_per_worker=2578" in text
        assert "raise the ceiling with the measured reason in the release that bumps REQUIRED_ENGINE_VERSION" in text
        assert "147 above" in text

    def test_a_lower_number_is_also_a_mismatch_and_says_lower(self) -> None:
        code, lines = _evaluate(2500)
        text = "\n".join(lines)
        assert code == canary.EXIT_MISMATCH
        assert "78 below" in text
        assert "lower the ceiling" in text

    def test_no_ddl_line_is_a_failure_not_a_pass(self) -> None:
        code, lines = _evaluate(None)
        assert code == canary.EXIT_NOTHING_MEASURED
        assert "no 'ddl: migration=' line" in lines[0]

    def test_no_ddl_line_with_a_failing_pytest_still_reports_both(self) -> None:
        code, lines = _evaluate(None, rc=1, passed=0)
        assert code == canary.EXIT_NOTHING_MEASURED
        assert any("pytest exited 1" in line for line in lines)

    def test_a_matching_number_with_a_red_pytest_is_not_a_pass(self) -> None:
        code, lines = _evaluate(2578, rc=1)
        assert code == canary.EXIT_TESTS_FAILED
        assert "pytest exited 1" in "\n".join(lines)

    def test_a_matching_number_with_no_passed_test_is_not_a_pass(self) -> None:
        code, _ = _evaluate(2578, passed=0)
        assert code == canary.EXIT_NOTHING_MEASURED

    def test_a_mismatch_and_a_red_pytest_exit_with_the_mismatch_and_print_both(self) -> None:
        code, lines = _evaluate(2725, rc=1)
        assert code == canary.EXIT_MISMATCH
        assert len(lines) == 2


class TestMain:
    def test_a_tag_outside_the_engine_namespace_is_refused_before_any_work(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert canary.main(["--tag", "v8.3.0"]) == canary.EXIT_NO_BUNDLE
        assert "not an engine-service-vX.Y.Z tag" in capsys.readouterr().err

    def test_a_missing_bundle_dir_is_refused(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert canary.main(["--tag", TAG, "--bundle", str(tmp_path / "nope")]) == canary.EXIT_NO_BUNDLE
        assert "not a directory" in capsys.readouterr().err

    def test_main_returns_the_verdict_of_the_run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bundle = tmp_path / "bin"
        bundle.mkdir()
        monkeypatch.setattr(canary, "read_ceiling", lambda: EXACT)
        for output, rc, expected in [
            (f"{_DDL_LINE}\n3 passed in 4s\n", 0, canary.EXIT_OK),
            ("ddl: migration=2725 tests=1 per_test=1.00\n3 passed in 4s\n", 0, canary.EXIT_MISMATCH),
            ("3 passed in 4s\n", 0, canary.EXIT_NOTHING_MEASURED),
        ]:
            monkeypatch.setattr(canary, "run_pytest", lambda pg_bin, report_dir, _o=output, _r=rc: (_r, _o))
            assert canary.main(["--tag", TAG, "--bundle", str(bundle)]) == expected

    def test_the_pytest_argv_runs_the_scoped_ddl_test_and_the_smoke_subset(self) -> None:
        argv = canary.pytest_argv("py")
        assert canary.DDL_TEST in argv
        assert all(smoke in argv for smoke in canary.SMOKE_TESTS)
        assert "-n" not in argv, "migration= is a per-worker count: one worker"


# ── the workflow (lint bucket: reads .github/ only) ─────────────────────────────────────────────────────


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers(doc: dict) -> dict:
    return doc.get("on", doc.get(True))  # PyYAML reads an unquoted `on:` as the boolean True


@pytest.mark.lint
class TestWorkflowPins:
    def test_it_fires_only_on_a_published_release_or_a_dispatch(self) -> None:
        on = _triggers(_workflow())
        assert set(on) == {"release", "workflow_dispatch"}, (
            f"the canary runs once per engine tag; a push, pull_request or schedule trigger would run it per "
            f"commit (CI cost discipline). Triggers: {sorted(on)}"
        )
        assert on["release"] == {"types": ["published"]}
        assert on["workflow_dispatch"]["inputs"]["tag"]["required"] is True

    def test_every_job_is_filtered_to_engine_tags(self) -> None:
        jobs = _workflow()["jobs"]
        assert set(jobs) == {"service-jar", "canary"}
        for name, job in jobs.items():
            cond = str(job.get("if", ""))
            assert "startsWith(github.event.release.tag_name, 'engine-service-v')" in cond, f"{name}: {cond!r}"
            assert "github.event_name == 'workflow_dispatch'" in cond, f"{name}: a dispatch must still run"

    def test_the_concurrency_group_is_per_tag_and_cancels_a_superseded_run(self) -> None:
        concurrency = _workflow()["concurrency"]
        assert concurrency["group"] == "engine-bundle-canary-${{ github.event.release.tag_name || inputs.tag }}"
        assert concurrency["cancel-in-progress"] is True

    def test_the_canary_runs_on_the_pytest_pool_class_with_the_off_switch(self) -> None:
        runs_on = _workflow()["jobs"]["canary"]["runs-on"]
        assert "lab-6c18g" in runs_on and "vars.NX_CI_POOL == 'off'" in runs_on and "ubuntu-latest" in runs_on
        assert _workflow()["jobs"]["service-jar"]["runs-on"] == "ubuntu-latest"

    def test_it_checks_out_develop_by_default_and_the_dispatch_ref_otherwise(self) -> None:
        expected = "${{ github.event_name == 'workflow_dispatch' && github.ref_name || 'develop' }}"
        checkouts = [
            s for job in _workflow()["jobs"].values() for s in job["steps"] if "actions/checkout" in s.get("uses", "")
        ]
        assert len(checkouts) == 2
        assert all(s["with"]["ref"] == expected for s in checkouts)

    def test_it_builds_the_jar_the_way_ci_does_and_runs_the_script_on_the_tag(self) -> None:
        jobs = _workflow()["jobs"]
        build = "\n".join(s.get("run", "") for s in jobs["service-jar"]["steps"])
        assert "./mvnw -q -DskipTests package" in build
        assert jobs["service-jar"]["env"]["NX_JOOQ_CACHE"] == "off"
        run = "\n".join(s.get("run", "") for s in jobs["canary"]["steps"])
        assert 'scripts/engine_bundle_canary.py --tag "$ENGINE_TAG"' in run
        assert jobs["canary"]["env"]["ENGINE_TAG"] == "${{ github.event.release.tag_name || inputs.tag }}"
        assert jobs["canary"]["env"]["NX_T2_SUBSTRATE_EXPECTED"] == "1"

    def test_the_tag_reaches_the_shell_through_the_environment_only(self) -> None:
        for job in _workflow()["jobs"].values():
            for step in job["steps"]:
                assert "inputs.tag" not in step.get("run", "") and "tag_name" not in step.get("run", "")

    def test_the_sweep_sees_the_workflow(self) -> None:
        assert WORKFLOW.is_file()
        assert len(_workflow()["jobs"]["canary"]["steps"]) >= 6

    def test_promote_release_dispatches_it_because_github_token_publishes_raise_no_release_event(self) -> None:
        job = yaml.safe_load(ENGINE_RELEASE_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["promote-release"]
        steps = [s for s in job["steps"] if "engine-bundle-canary.yml" in s.get("run", "")]
        assert len(steps) == 1, "promote-release must dispatch the canary exactly once"
        step = steps[0]
        assert step["continue-on-error"] is True, "a failed dispatch must never un-publish or fail a release"
        assert "gh workflow run engine-bundle-canary.yml" in step["run"]
        assert job["permissions"]["actions"] == "write"
        assert job["permissions"]["contents"] == "write"
        publish = next(i for i, s in enumerate(job["steps"]) if "promote_engine_release.sh" in s.get("run", ""))
        assert job["steps"].index(step) > publish, "dispatch only after the release is promoted"
