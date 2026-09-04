# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""check_dependency_drift.py — the weekly fresh-resolution watch (gap 3 of
T2 ``nexus/release-protocol-gap-audit-2026-08-14`` [22511], nexus-l2ku5
class).

Logic tests run entirely against captured/synthetic ``uv lock --upgrade
--dry-run`` output -- no real network resolution, deterministic and fast.
One live smoke test at the bottom runs the actual script against this repo's
real ``pyproject.toml`` to prove the parser survives real uv output shapes
(multi-version "Update" lines like torch's, real package names with hyphens
and dots) -- it asserts only that the script *runs and returns a report*,
never a specific rc, since live upstream drift is expected to fluctuate.
"""
from __future__ import annotations

import importlib
import json
import subprocess

import pytest

import check_dependency_drift as drift

_SAMPLE_OUTPUT = """\
Resolved 262 packages in 428ms
Update accelerate v1.13.0 -> v1.14.0
Update torch v2.8.0, v2.10.0 -> v2.13.0
Add python-oxmsg v0.0.2
Remove qwen-vl-utils v0.0.14
Update starlette v0.52.1 -> v1.6.0
Update sigstore v4.3.0 -> v4.5.0
"""


class TestParsing:
    def test_parses_update_add_remove_lines(self):
        report = drift.parse_dry_run_output(_SAMPLE_OUTPUT)
        names = {f.name for f in report.updates}
        assert names == {"accelerate", "torch", "starlette", "sigstore"}
        assert report.added == ("python-oxmsg",)
        assert report.removed == ("qwen-vl-utils",)

    def test_multi_version_update_line_takes_the_last_old_version(self):
        """torch's `v2.8.0, v2.10.0 -> v2.13.0` shape (two platform/marker-
        specific locked versions) must resolve to the HIGHEST old version
        (2.10.0), not the first, so the major-bump comparison is correct."""
        report = drift.parse_dry_run_output(_SAMPLE_OUTPUT)
        torch = next(f for f in report.updates if f.name == "torch")
        assert torch.old_version == "2.10.0"
        assert torch.new_version == "2.13.0"

    def test_non_version_lines_are_ignored(self):
        report = drift.parse_dry_run_output(_SAMPLE_OUTPUT)
        # "Resolved 262 packages in 428ms" must not be mistaken for an
        # Update/Add/Remove line.
        assert len(report.updates) + len(report.added) + len(report.removed) == 6

    def test_empty_output_parses_to_empty_report(self):
        report = drift.parse_dry_run_output("")
        assert report.updates == () and report.added == () and report.removed == ()
        assert report.ok


class TestMajorBumpDetection:
    def test_minor_bump_is_not_flagged(self):
        report = drift.parse_dry_run_output("Update accelerate v1.13.0 -> v1.14.0\n")
        assert report.major_bumps == ()
        assert report.ok

    def test_major_bump_is_flagged(self):
        report = drift.parse_dry_run_output("Update starlette v0.52.1 -> v1.6.0\n")
        assert len(report.major_bumps) == 1
        assert report.major_bumps[0].name == "starlette"
        assert not report.ok

    def test_calver_leading_component_bump_is_flagged(self):
        """structlog-style calver (25.5.0 -> 26.1.0): the leading component
        is what a pyproject.toml `<NEXT_MAJOR>` cap actually holds at, for
        calver just as much as semver -- must be caught the same way."""
        report = drift.parse_dry_run_output("Update structlog v25.5.0 -> v26.1.0\n")
        assert len(report.major_bumps) == 1

    def test_multiple_minor_and_patch_components_do_not_confuse_leading_component(self):
        report = drift.parse_dry_run_output("Update rpds-py v0.30.0 -> v0.31.9\n")
        assert report.major_bumps == ()


class TestShapeSensitiveDetection:
    """nexus-jd8fi drift: a minor bump of a fixture-locked package is a finding."""

    def test_mineru_minor_bump_is_flagged(self):
        # 3.3.0: a version nobody has gated, so not in SHAPE_SENSITIVE_ACKNOWLEDGED.
        report = drift.parse_dry_run_output("Update mineru v3.1.11 -> v3.3.0\n")
        assert report.major_bumps == ()
        assert [f.name for f in report.shape_sensitive_updates] == ["mineru"]
        assert not report.ok

    def test_docling_patch_bump_is_flagged(self):
        report = drift.parse_dry_run_output("Update docling v2.76.0 -> v2.76.1\n")
        assert not report.ok

    def test_unlisted_minor_bump_stays_clean(self):
        report = drift.parse_dry_run_output("Update accelerate v1.13.0 -> v1.14.0\n")
        assert report.shape_sensitive_updates == ()
        assert report.ok

    def test_render_names_the_shape_sensitive_package_and_the_remedy(self):
        report = drift.parse_dry_run_output("Update mineru v3.1.11 -> v3.3.0\n")
        body = drift.render_report(report)
        assert "mineru: 3.1.11 -> 3.3.0" in body
        assert "shape-sensitive" in body
        assert "_SHAPE_SENSITIVE" in body

    def test_acknowledged_refusal_is_rendered_but_does_not_fail(self):
        """A version a bead already refused is known pressure: the weekly run
        must not stay red on it (code review [24211] finding 1)."""
        report = drift.parse_dry_run_output("Update mineru v3.1.11 -> v3.1.15\n")
        assert report.shape_sensitive_updates and not report.unacknowledged_shape_sensitive_updates
        assert report.ok and report.shape_ok
        body = drift.render_report(report)
        assert "mineru: 3.1.11 -> 3.1.15" in body and "acknowledged: nexus-6ht8u" in body

    def test_fail_on_shape_ignores_major_bumps_but_not_new_shape_changes(self, monkeypatch):
        monkeypatch.setattr(drift, "run_uv_dry_run_upgrade", lambda **kw: "Update starlette v0.52.1 -> v1.6.0\n")
        assert drift.check(fail_on="shape")[0] == 0
        assert drift.check(fail_on="any")[0] == 1
        monkeypatch.setattr(drift, "run_uv_dry_run_upgrade", lambda **kw: "Update docling v2.125.0 -> v2.125.1\n")
        assert drift.check(fail_on="shape")[0] == 1

    def test_acknowledged_rows_name_only_shape_sensitive_packages(self):
        assert {n for n, _ in drift.SHAPE_SENSITIVE_ACKNOWLEDGED} <= drift.SHAPE_SENSITIVE

    def test_set_mirrors_the_lint_table(self):
        """The lint holds the cap; the watch reports the pressure. A package
        the lint pins must be one the watch reports, or a bump lands with
        no warning between weekly runs."""
        lint = importlib.import_module("tests.test_dependency_bounds_lint")
        assert set(lint._SHAPE_SENSITIVE) <= drift.SHAPE_SENSITIVE


class TestReportRendering:
    def test_clean_report_says_clean(self):
        report = drift.parse_dry_run_output("Update accelerate v1.13.0 -> v1.14.0\n")
        body = drift.render_report(report)
        assert "clean" in body.lower()
        assert "starlette" not in body

    def test_dirty_report_names_the_bumped_packages(self):
        report = drift.parse_dry_run_output(_SAMPLE_OUTPUT)
        body = drift.render_report(report)
        assert "starlette" in body
        assert "0.52.1 -> 1.6.0" in body
        # a package with no major bump must not appear in the flagged list
        assert "accelerate: " not in body


class TestCheck:
    def test_uv_unavailable_is_exit_2_not_clean(self, monkeypatch):
        """'Could not check' must never be reported as 'nothing found' --
        same non-vacuity discipline as the sibling release-gate scripts."""
        monkeypatch.setattr(drift, "run_uv_dry_run_upgrade", lambda **kw: drift.UV_UNAVAILABLE)
        rc, body, report = drift.check()
        assert rc == 2
        assert report is None
        assert "could not run" in body.lower()

    def test_clean_resolution_is_exit_0(self, monkeypatch):
        monkeypatch.setattr(
            drift, "run_uv_dry_run_upgrade",
            lambda **kw: "Update accelerate v1.13.0 -> v1.14.0\n",
        )
        rc, body, report = drift.check()
        assert rc == 0
        assert report.ok

    def test_kill_control_major_bump_is_exit_1(self, monkeypatch):
        """Kill-control: a synthetic dry-run output containing a genuine
        major-component bump must fail the check. Proves the detector
        actually detects rather than vacuously passing because the real
        repo happened to be clean on any given day."""
        monkeypatch.setattr(
            drift, "run_uv_dry_run_upgrade",
            lambda **kw: "Update some-package v1.0.0 -> v2.0.0\n",
        )
        rc, body, report = drift.check()
        assert rc == 1
        assert not report.ok
        assert report.major_bumps[0].name == "some-package"

    def test_never_writes_the_lockfile(self, monkeypatch):
        """Guards the --dry-run contract itself: the subprocess invocation
        must carry --dry-run, since a script meant to run unattended on a
        weekly schedule must never mutate the committed lock."""
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        drift.run_uv_dry_run_upgrade()
        assert "--dry-run" in captured["cmd"]
        assert "--upgrade" in captured["cmd"]

    def test_timeout_is_treated_as_unavailable(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 300))

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = drift.run_uv_dry_run_upgrade()
        assert result is drift.UV_UNAVAILABLE

    def test_uv_missing_is_treated_as_unavailable(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            raise FileNotFoundError("uv not found")

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = drift.run_uv_dry_run_upgrade()
        assert result is drift.UV_UNAVAILABLE

    def test_uv_nonzero_exit_is_treated_as_unavailable(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="error: locking failed")

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = drift.run_uv_dry_run_upgrade()
        assert result is drift.UV_UNAVAILABLE


class TestMain:
    def test_main_returns_the_check_rc(self, monkeypatch, capsys):
        monkeypatch.setattr(drift, "check", lambda **kw: (1, "some drift found", None))
        rc = drift.main([])
        assert rc == 1
        assert "some drift found" in capsys.readouterr().out

    def test_main_json_mode_emits_parseable_json(self, monkeypatch, capsys):
        report = drift.DriftReport(
            updates=(drift.DriftFinding(name="pkg", old_version="1.0.0", new_version="2.0.0"),),
        )
        monkeypatch.setattr(drift, "check", lambda **kw: (1, "body text", report))
        rc = drift.main(["--json"])
        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["rc"] == 1
        assert payload["ok"] is False
        assert payload["major_bumps"] == [{"name": "pkg", "old": "1.0.0", "new": "2.0.0"}]


@pytest.mark.slow
def test_live_smoke_runs_against_real_repo():
    """Not asserting a specific rc (live upstream drift fluctuates day to
    day) -- proves the script actually runs end-to-end against this repo's
    real pyproject.toml/uv.lock and produces a well-formed report, and that
    --dry-run genuinely never wrote the lock (checked via git diff)."""
    proc = subprocess.run(["git", "diff", "--quiet", "uv.lock"], capture_output=True)
    lock_was_clean_before = proc.returncode == 0

    rc, body, report = drift.check()

    assert rc in (0, 1, 2)
    if rc != 2:
        assert isinstance(body, str) and body

    proc_after = subprocess.run(["git", "diff", "--quiet", "uv.lock"], capture_output=True)
    lock_was_clean_after = proc_after.returncode == 0
    assert lock_was_clean_before == lock_was_clean_after, (
        "uv.lock's clean/dirty state changed across the dry-run call -- "
        "--dry-run must never write the lockfile"
    )
