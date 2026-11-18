# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scripts/engine_bundle_canary.py``: parse, compare and exit logic, plus the workflow that runs it.

The script's pure core takes the pytest output and the ceiling file and returns an exit code and the
lines to print, so every verdict path runs here without a network, a bundle or an engine. The workflow
pins (lint bucket) hold the cost discipline the canary was designed under: it fires once per engine tag
(release published, engine-service-v* only, or a dispatch naming a tag) and never on a push or pull
request, and its concurrency group is per tag. ``scripts/`` is on ``pythonpath``.
"""
from __future__ import annotations

import importlib
import re
import textwrap
from pathlib import Path

import pytest
import yaml

import engine_bundle_canary as canary

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "engine-bundle-canary.yml"
ENGINE_RELEASE_WORKFLOW = REPO / ".github" / "workflows" / "engine-service-release.yml"

TAG = "engine-service-v0.1.195"
PINNED = "engine-service-v0.1.194"
EXACT = canary.Ceiling(2578, 0.0)

# What the terminal summary of a real run prints (tests/_ddl_counter.py::summary_line).
_DDL_LINE = "ddl: migration=2578 tests=112 per_test=1.00 wal: migration=2.0MB tests=1.0MB per_test=9KB (report: /tmp/x)"


def _m(tag, measured, *, passed=3, rc=0, pg="17.6", vec="0.8.7"):
    return canary.Measurement(tag, measured, passed, rc, pg, vec)


def _evaluate(tag_measured, pinned_measured=2578, *, tag_rc=0, pinned_rc=0, tag_passed=3, ceiling=EXACT):
    return canary.evaluate(
        tag=_m(TAG, tag_measured, rc=tag_rc, passed=tag_passed),
        pinned=_m(PINNED, pinned_measured, rc=pinned_rc, pg="17.5", vec="0.8.2"),
        ceiling=ceiling,
    )


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

    @pytest.mark.parametrize(("measured", "above"), [(2578, False), (2579, True), (2577, False)])
    def test_the_gate_is_one_sided_like_ddl_ratchet(self, measured: int, above: bool) -> None:
        assert canary.above_ceiling(measured, EXACT) is above

    def test_a_fractional_tolerance_widens_the_band_upward(self) -> None:
        wide = canary.Ceiling(1000, 0.01)
        assert not canary.above_ceiling(1010, wide)
        assert canary.above_ceiling(1011, wide)


class TestVerdict:
    def test_both_equal_the_ceiling_is_a_quiet_pass(self) -> None:
        v = _evaluate(2578)
        assert v.code == canary.EXIT_OK and not v.errors and not v.warnings
        assert any("nothing to change" in line for line in v.summary)

    @pytest.mark.parametrize(("tag_count", "word"), [(2725, "raises"), (2500, "lowers")])
    def test_a_tag_delta_is_a_warning_with_the_remedy_never_a_red(self, tag_count: int, word: str) -> None:
        v = _evaluate(tag_count)
        assert v.code == canary.EXIT_OK and not v.errors
        (warning,) = v.warnings
        assert f"{TAG}'s bundle changes migration DDL from 2578 to {tag_count}" in warning
        assert f"the client release that pins it {word} tests/ddl_ceiling.toml migration_ddl_per_worker to {tag_count} with this reason" in warning
        assert "pgvector 0.8.2 to 0.8.7" in warning and "postgres 17.5 to 17.6" in warning
        assert not any(warning in line for line in v.summary), "the warning is written once, by _report"

    def test_the_summary_names_each_bundles_postgres_and_pgvector(self) -> None:
        text = "\n".join(_evaluate(2725).summary)
        assert "postgres 17.5, pgvector 0.8.2" in text and "postgres 17.6, pgvector 0.8.7" in text

    def test_a_pinned_bundle_above_the_ceiling_is_a_red_naming_develop(self) -> None:
        v = _evaluate(2579, 2579)
        assert v.code == canary.EXIT_MISMATCH
        assert "DEVELOP DRIFTED PAST ITS CEILING" in v.errors[0] and "ddl: migration=2579" in v.errors[0]
        assert "migration_ddl_per_worker=2578" in v.errors[0]

    def test_a_pinned_bundle_below_the_ceiling_is_a_warning_naming_the_number_to_lower_it_to(self) -> None:
        v = _evaluate(2577, 2577)
        assert v.code == canary.EXIT_OK and not v.errors
        (warning,) = v.warnings
        assert "below tests/ddl_ceiling.toml migration_ddl_per_worker=2578" in warning
        assert "lower the ceiling to 2577" in warning

    def test_a_pinned_drift_is_red_even_when_the_tag_agrees_with_it(self) -> None:
        assert _evaluate(2600, 2600).code == canary.EXIT_MISMATCH

    def test_no_ddl_line_is_a_failure_not_a_pass(self) -> None:
        v = _evaluate(None)
        assert v.code == canary.EXIT_NOTHING_MEASURED
        assert "no 'ddl: migration=' line" in v.errors[0]

    def test_a_pinned_run_that_measured_nothing_is_a_failure(self) -> None:
        assert _evaluate(2578, None).code == canary.EXIT_NOTHING_MEASURED

    def test_a_red_pytest_on_either_bundle_is_not_a_pass(self) -> None:
        assert _evaluate(2578, tag_rc=1).code == canary.EXIT_TESTS_FAILED
        assert _evaluate(2578, pinned_rc=1).code == canary.EXIT_TESTS_FAILED

    def test_a_matching_number_with_no_passed_test_is_not_a_pass(self) -> None:
        assert _evaluate(2578, tag_passed=0).code == canary.EXIT_NOTHING_MEASURED

    def test_the_tag_being_the_pinned_tag_runs_once(self) -> None:
        ok = canary.evaluate(tag=_m(PINNED, 2578), pinned=None, ceiling=EXACT)
        assert ok.code == canary.EXIT_OK and not ok.warnings
        drifted = canary.evaluate(tag=_m(PINNED, 2600), pinned=None, ceiling=EXACT)
        assert drifted.code == canary.EXIT_MISMATCH

    def test_every_problem_is_printed_and_the_lowest_code_wins(self) -> None:
        v = _evaluate(None, 2600, tag_rc=1)
        assert v.code == canary.EXIT_MISMATCH and len(v.errors) == 3


class TestControlVersion:
    def test_reads_default_version(self) -> None:
        text = "# vector extension\ncomment = 'x'\ndefault_version = '0.8.7'\nrelocatable = true\n"
        assert canary.parse_control_version(text) == "0.8.7"

    def test_no_default_version_is_none(self) -> None:
        assert canary.parse_control_version("comment = 'x'\n") is None

    def test_bundle_versions_reads_the_control_file_of_an_unrunnable_bundle(self, tmp_path: Path) -> None:
        ext = tmp_path / "share" / "postgresql" / "extension"
        ext.mkdir(parents=True)
        (ext / "vector.control").write_text("default_version = '0.8.2'\n")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        assert canary.bundle_versions(bin_dir) == ("unknown", "0.8.2")


class TestSeams:
    def test_the_scoped_test_node_id_exists(self) -> None:
        path, _, name = canary.DDL_TEST.partition("::")
        assert re.search(rf"^(async )?def {re.escape(name)}\(", (REPO / path).read_text(encoding="utf-8"), re.MULTILINE), (
            f"{canary.DDL_TEST} no longer exists: the canary would measure nothing"
        )
        assert all((REPO / t).is_file() for t in canary.SMOKE_TESTS)

    def test_the_provisioning_helpers_it_calls_import(self) -> None:
        binary_install = importlib.import_module("nexus.daemon.binary_install")
        assert callable(importlib.import_module("nexus.db.pg_bundle").ensure_pg_bundle)
        assert callable(importlib.import_module("tests.db._service_fixture")._install_pg_bundle_with_tuf_retry)
        assert canary.pinned_tag() == binary_install.PINNED_SERVICE_TAG

    def test_a_non_empty_cache_dir_is_refused_before_any_fetch(self, tmp_path: Path) -> None:
        (tmp_path / "other-tag-leftover").write_text("x")
        with pytest.raises(RuntimeError, match="not empty"):
            canary.provision_bundle(TAG, tmp_path)

    def test_a_tag_with_trailing_garbage_does_not_match(self) -> None:
        assert canary._TAG.fullmatch(TAG)
        assert not canary._TAG.fullmatch(TAG + "\n")
        assert not canary._TAG.fullmatch("v8.3.0")


class TestReport:
    def test_a_warning_is_emitted_as_a_workflow_command_and_appended_to_the_step_summary_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        verdict = _evaluate(2725)
        canary._report(verdict)
        (warning,) = verdict.warnings
        assert f"::warning::{warning}" in capsys.readouterr().out
        text = summary.read_text(encoding="utf-8")
        assert text.count(warning) == 1, "the warning is in the summary once"
        assert f"\n\n**Warning:** {warning}.\n" in text, "a blank line before the bold line"
        assert text.startswith("### Engine bundle canary\n\nCeiling migration_ddl_per_worker=2578.\n\n- ")
        assert "postgres 17.5, pgvector 0.8.2" in text and "postgres 17.6, pgvector 0.8.7" in text

    def test_outside_actions_a_warning_is_plain_text_and_an_error_goes_to_stderr(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        canary._report(_evaluate(2725))
        canary._report(_evaluate(2579, 2579))
        captured = capsys.readouterr()
        assert "WARNING: " in captured.out and "::warning::" not in captured.out
        assert "ERROR: DEVELOP DRIFTED PAST ITS CEILING" in captured.err

    def test_a_clean_run_writes_no_warning_to_the_summary(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        canary._report(_evaluate(2578))
        assert "Warning" not in summary.read_text(encoding="utf-8")


class TestMain:
    def test_a_tag_outside_the_engine_namespace_is_refused_before_any_work(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert canary.main(["--tag", "v8.3.0", "--pinned-tag", PINNED]) == canary.EXIT_NO_BUNDLE
        assert "not an engine-service-vX.Y.Z tag" in capsys.readouterr().err

    def test_a_missing_bundle_dir_is_refused(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = canary.main(["--tag", TAG, "--pinned-tag", PINNED, "--bundle", str(tmp_path / "nope"), "--pinned-bundle", str(tmp_path)])
        assert rc == canary.EXIT_NO_BUNDLE
        assert "not a directory" in capsys.readouterr().err

    def test_main_measures_both_bundles_and_returns_the_verdict(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        tag_bin, pinned_bin = tmp_path / "tag", tmp_path / "pinned"
        tag_bin.mkdir()
        pinned_bin.mkdir()
        monkeypatch.setattr(canary, "read_ceiling", lambda: EXACT)
        seen: list[Path] = []

        def fake_run(pg_bin: Path, report_dir: Path, *, counts: dict = {}) -> tuple[int, str]:
            seen.append(pg_bin)
            return 0, f"ddl: migration={counts[pg_bin]} tests=1 per_test=1.00\n3 passed in 4s\n"

        for tag_count, expected in [(2578, canary.EXIT_OK), (2725, canary.EXIT_OK)]:
            counts = {pinned_bin: 2578, tag_bin: tag_count}
            seen.clear()
            monkeypatch.setattr(canary, "run_pytest", lambda b, r, _c=counts: fake_run(b, r, counts=_c))
            args = ["--tag", TAG, "--pinned-tag", PINNED, "--bundle", str(tag_bin), "--pinned-bundle", str(pinned_bin)]
            assert canary.main(args) == expected
            assert seen == [pinned_bin, tag_bin], "the pinned bundle is the control and runs first"

    def test_main_is_red_when_the_pinned_bundle_drifted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "a").mkdir()
        monkeypatch.setattr(canary, "read_ceiling", lambda: EXACT)
        monkeypatch.setattr(canary, "run_pytest", lambda b, r: (0, "ddl: migration=2600 tests=1\n3 passed in 4s\n"))
        args = ["--tag", TAG, "--pinned-tag", PINNED, "--bundle", str(tmp_path / "a"), "--pinned-bundle", str(tmp_path / "a")]
        assert canary.main(args) == canary.EXIT_MISMATCH

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
    def test_it_fires_only_on_a_release_a_call_from_the_engine_release_or_a_dispatch(self) -> None:
        on = _triggers(_workflow())
        assert set(on) == {"release", "workflow_call", "workflow_dispatch"}, (
            f"the canary runs once per engine tag; a push, pull_request or schedule trigger would run it per "
            f"commit (CI cost discipline). Triggers: {sorted(on)}"
        )
        assert on["release"] == {"types": ["published"]}
        for trigger in ("workflow_call", "workflow_dispatch"):
            assert on[trigger]["inputs"]["tag"]["required"] is True, trigger
            assert on[trigger]["inputs"]["tag"]["type"] == "string", trigger

    def test_every_job_skips_a_non_engine_release_and_still_runs_a_call_or_dispatch(self) -> None:
        jobs = _workflow()["jobs"]
        assert set(jobs) == {"service-jar", "canary"}
        expected = "${{ github.event_name != 'release' || startsWith(github.event.release.tag_name, 'engine-service-v') }}"
        for name, job in jobs.items():
            assert job.get("if") == expected, f"{name}: {job.get('if')!r}"
        # In a called workflow github.event_name is the CALLER's (push, for a tag): an equality test on
        # 'workflow_dispatch' would skip every call.
        assert "workflow_dispatch" not in expected

    def test_the_concurrency_group_is_per_tag_and_per_path_and_cancels_a_superseded_run(self) -> None:
        concurrency = _workflow()["concurrency"]
        group = concurrency["group"]
        assert group == "engine-bundle-canary-${{ github.event_name }}-${{ github.event.release.tag_name || inputs.tag }}"
        # In a called workflow event_name is the caller's (push), so the call, a dispatch and a hand-published
        # release land in three groups per tag: a hand dispatch can never cancel the in-flight call.
        assert "github.event_name" in group
        assert concurrency["cancel-in-progress"] is True

    def test_the_canary_runs_on_the_pytest_pool_class_with_the_off_switch(self) -> None:
        runs_on = _workflow()["jobs"]["canary"]["runs-on"]
        assert "lab-6c18g" in runs_on and "vars.NX_CI_POOL == 'off'" in runs_on and "ubuntu-latest" in runs_on
        assert _workflow()["jobs"]["service-jar"]["runs-on"] == "ubuntu-latest"

    def test_both_jobs_check_out_the_one_sha_the_first_job_resolved(self) -> None:
        jobs = _workflow()["jobs"]
        first = [s for s in jobs["service-jar"]["steps"] if "actions/checkout" in s.get("uses", "")]
        second = [s for s in jobs["canary"]["steps"] if "actions/checkout" in s.get("uses", "")]
        assert len(first) == 1 and len(second) == 1
        # The first resolves develop (or a dispatch's own ref, tests/test_workflow_dispatch_ref_lint.py)...
        assert first[0]["with"]["ref"] == "${{ github.event_name == 'workflow_dispatch' && github.ref_name || 'develop' }}"
        sha_step = next(s for s in jobs["service-jar"]["steps"] if s.get("id") == "revision")
        assert 'echo "revision=$(git rev-parse HEAD)" >> "$GITHUB_OUTPUT"' in sha_step["run"]  # the key the output reads
        # Not named sha, head or commit, which CodeQL reads as an untrusted pull-request head (code scanning alert 61).
        assert jobs["service-jar"]["outputs"] == {"revision": "${{ steps.revision.outputs.revision }}"}
        # ...and the second tests exactly that tree, so the jar and the tests cannot differ by a push in between.
        assert second[0]["with"]["ref"] == "${{ needs.service-jar.outputs.revision }}"
        assert jobs["canary"]["needs"] == "service-jar"

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

    def test_the_engine_release_calls_it_after_promote_release_with_the_tag(self) -> None:
        jobs = yaml.safe_load(ENGINE_RELEASE_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
        job = jobs["bundle-canary"]
        assert job["uses"] == "./.github/workflows/engine-bundle-canary.yml"
        assert job["with"] == {"tag": "${{ github.ref_name }}"}
        assert job["needs"] == "promote-release", "a tag that stayed a draft is never tested"
        assert job["permissions"] == {"contents": "read"}
        assert "if" not in job, "a custom `if` replaces the implied success() and would run after a skipped promote"

    def test_promote_release_is_unchanged_by_the_canary(self) -> None:
        # A publish made with GITHUB_TOKEN raises no release event, which is why the engine release calls the
        # canary; nothing may be dispatched from, or added to the permissions of, the promote job.
        job = yaml.safe_load(ENGINE_RELEASE_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["promote-release"]
        assert "permissions" not in job
        assert "gh workflow run" not in "\n".join(s.get("run", "") for s in job["steps"])
        assert not any("engine-bundle-canary" in str(s) for s in job["steps"])
