# SPDX-License-Identifier: AGPL-3.0-or-later
"""The watchdog that watches the scheduled gates must itself be falsifiable.

nexus-x7xjj. Its silent-pass mode is "found nothing to check" — a parser change
or a moved directory yields a serene all-clear over zero workflows. Every test
here exists to make one of those silences loud.
"""

from __future__ import annotations

import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scheduled_workflow_watchdog import (  # noqa: E402
    Finding,
    classify,
    fetch_latest_runs,
    name_claim_findings,
    render,
    scheduled_paths,
)

NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)


def _run(conclusion="success", days_ago=1, branch="main"):
    return {
        "conclusion": conclusion,
        "created_at": (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z"),
        "html_url": "https://example.invalid/run/1",
        "head_branch": branch,
    }


# ── The three failure modes ──────────────────────────────────────────────────


def test_failing_latest_run_is_reported() -> None:
    findings = classify({"p.yml": "p.yml"}, {"p.yml": _run("failure")}, NOW)
    assert [f.kind for f in findings] == ["failing"]
    assert "FAILED" in findings[0].detail


def test_workflow_that_never_ran_is_reported() -> None:
    """A declared schedule with zero runs means the schedule is not firing.

    GitHub disables schedules on inactive repos and accepts an invalid cron
    without error, so "no runs" is a real and silent state — not a new workflow
    that simply hasn't come round yet in any way we can distinguish.
    """
    findings = classify({"p.yml": "p.yml"}, {"p.yml": None}, NOW)
    assert [f.kind for f in findings] == ["never-ran"]


def test_stale_green_run_is_reported() -> None:
    """The quietest mode: last run was GREEN, and months ago."""
    findings = classify({"p.yml": "p.yml"}, {"p.yml": _run("success", days_ago=90)}, NOW)
    assert [f.kind for f in findings] == ["stale"]
    assert "90 days old" in findings[0].detail


def test_recent_green_run_is_not_reported() -> None:
    assert classify({"p.yml": "p.yml"}, {"p.yml": _run("success", days_ago=1)}, NOW) == []


def test_cancelled_and_skipped_are_not_failures() -> None:
    """Concurrency cancellations are routine here and must not page anyone."""
    for conclusion in ("cancelled", "skipped", "success", None):
        findings = classify({"p.yml": "p.yml"}, {"p.yml": _run(conclusion)}, NOW)
        assert findings == [], f"{conclusion!r} should not be a finding"


# ── Non-vacuity: the watchdog's own silent-pass mode ─────────────────────────


def test_zero_scheduled_workflows_is_a_FINDING_not_an_all_clear() -> None:
    """THE point of this module. If the parser stops recognising schedules, or
    the directory moves, the sweep examines nothing — and must say so loudly
    rather than reporting a clean bill of health over an empty set."""
    findings = classify({}, {}, NOW)
    assert [f.kind for f in findings] == ["nothing-to-watch"]
    assert "all-clear" in findings[0].detail


def test_render_of_no_findings_says_so_explicitly() -> None:
    assert "green and firing on cadence" in render([])


def test_render_lists_every_finding() -> None:
    out = render([
        Finding("a.yml", "p/a.yml", "failing", "x"),
        Finding("b.yml", "p/b.yml", "stale", "y"),
    ])
    assert "a.yml" in out and "b.yml" in out
    assert "failing" in out and "stale" in out


# ── Trigger parsing, including the YAML `on:` -> True trap ───────────────────


def test_bare_on_key_parsed_as_boolean_is_still_detected(tmp_path: Path) -> None:
    """`on:` is parsed by YAML as the BOOLEAN True, not the string "on".

    A detector reading only ``doc.get("on")`` finds nothing, reports zero
    scheduled workflows, and — before the nothing-to-watch finding existed —
    would have declared everything healthy. This pins both spellings.
    """
    (tmp_path / "sched.yml").write_text(
        "name: scheduled\non:\n  schedule:\n    - cron: '0 3 * * *'\njobs: {}\n"
    )
    found = scheduled_paths(tmp_path)
    assert list(found) == [".github/workflows/sched.yml"]


def test_quoted_on_key_is_detected(tmp_path: Path) -> None:
    (tmp_path / "q.yml").write_text(
        "name: q\n'on':\n  schedule:\n    - cron: '0 3 * * *'\njobs: {}\n"
    )
    assert list(scheduled_paths(tmp_path)) == [".github/workflows/q.yml"]


def test_dispatch_only_workflow_is_not_watched(tmp_path: Path) -> None:
    """The three P4b-retired rehearsals are dispatch-only now; they must not
    be reported forever as never-ran."""
    (tmp_path / "d.yml").write_text("name: d\non:\n  workflow_dispatch:\njobs: {}\n")
    assert scheduled_paths(tmp_path) == {}


def test_unparseable_workflow_aborts_rather_than_skipping(tmp_path: Path) -> None:
    """Stepping over a malformed workflow is how a sweep goes quietly blind."""
    (tmp_path / "bad.yml").write_text("name: [unclosed\n")
    with pytest.raises(SystemExit):
        scheduled_paths(tmp_path)


# ── API failures must not read as health ─────────────────────────────────────


def test_api_error_aborts_rather_than_reporting_green() -> None:
    def boom(_url):
        raise urllib.error.HTTPError("u", 500, "server error", {}, None)

    with pytest.raises(SystemExit):
        fetch_latest_runs("o/r", "t", ["p.yml"], api=boom)


@pytest.mark.parametrize("code", [403, 429, 500, 502])
def test_non_404_errors_all_abort(code: int) -> None:
    """Rate limits and outages must never be read as health."""
    def boom(_url):
        raise urllib.error.HTTPError("u", code, "nope", {}, None)

    with pytest.raises(SystemExit):
        fetch_latest_runs("o/r", "t", ["p.yml"], api=boom)


def test_404_becomes_never_ran_not_an_abort() -> None:
    """A workflow GitHub has no record of is UNREGISTERED, not an API failure.

    Found by running this script against the live repo for the first time,
    before it had been pushed: its own file 404'd and the sweep aborted. A
    workflow on disk that GitHub does not know about has, by definition, never
    fired — which is exactly what `never-ran` reports. Aborting instead loses
    every other workflow's result to one unregistered file.
    """
    def missing(_url):
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

    out = fetch_latest_runs("o/r", "t", ["p.yml"], api=missing)
    assert out == {"p.yml": None}
    assert [f.kind for f in classify({"p.yml": "p.yml"}, out, NOW)] == ["never-ran"]


def test_empty_run_list_becomes_never_ran_not_missing() -> None:
    captured = {}

    def fake(url):
        captured["url"] = url
        return {"workflow_runs": []}

    out = fetch_latest_runs("o/r", "t", [".github/workflows/p.yml"], api=fake)
    assert out == {".github/workflows/p.yml": None}
    # The path must be URL-encoded — a bare slash would 404 into a false green.
    assert "%2F" in captured["url"]


# ── Against the real repository ──────────────────────────────────────────────


def test_real_workflow_dir_parses_and_the_detector_binds() -> None:
    """Guards the live wiring: the directory exists, parses, and the schedule
    detector recognises at least one workflow. If a future change removes every
    schedule this fails — deliberately, because that is the state where this
    whole watchdog quietly stops meaning anything."""
    repo_workflows = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    assert repo_workflows.is_dir()
    found = scheduled_paths(repo_workflows)
    assert found, (
        "no workflow in this repo declares an on.schedule trigger — either they "
        "were all removed, or the detector broke. Both need a human."
    )


# ── name-claims-cadence (nexus-idtjs) ────────────────────────────────────────


def _write_wf(tmp_path: Path, name_line: str, on_block: str) -> Path:
    d = tmp_path / "workflows"
    d.mkdir(exist_ok=True)
    (d / "wf.yml").write_text(f"name: {name_line}\non:\n{on_block}\njobs: {{}}\n")
    return d


def test_name_claiming_weekly_with_dispatch_only_trigger_is_a_finding(tmp_path) -> None:
    """nexus-idtjs RED case: the exact live specimen (era-hop-mvv titled
    'weekly' with a workflow_dispatch-only on: block, invisible to
    scheduled_paths by construction)."""
    d = _write_wf(tmp_path, "Journey (weekly + on ladder-path changes)", "  workflow_dispatch:")
    findings = name_claim_findings(d)
    kinds = {f.kind for f in findings}
    assert kinds == {"name-claims-cadence"}
    details = " | ".join(f.detail for f in findings)
    assert "weekly" in details
    assert "on ladder-path changes" in details  # both claims reported


def test_name_claiming_weekly_with_a_real_schedule_is_clean(tmp_path) -> None:
    d = _write_wf(tmp_path, "Journey (weekly)", "  schedule:\n    - cron: '0 0 * * 1'\n  workflow_dispatch:")
    assert name_claim_findings(d) == []


def test_on_changes_claim_satisfied_by_push_or_pull_request(tmp_path) -> None:
    d = _write_wf(tmp_path, "Gate (on migration-path changes)", "  push:\n    paths: ['src/**']")
    assert name_claim_findings(d) == []
    d2 = _write_wf(tmp_path, "Gate (on migration-path changes)", "  pull_request:")
    assert name_claim_findings(d2) == []


def test_unclaiming_name_with_dispatch_only_is_clean(tmp_path) -> None:
    d = _write_wf(tmp_path, "Journey (dispatch-only parts donor)", "  workflow_dispatch:")
    assert name_claim_findings(d) == []


def test_real_workflow_dir_has_no_lying_names() -> None:
    """Live wiring pin: every workflow name in this repo must currently tell
    the truth about its triggers (era-hop-mvv and guided-upgrade-mvv were
    renamed to the truth in the same diff that added this detector)."""
    repo_workflows = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    findings = name_claim_findings(repo_workflows)
    assert findings == [], [f"{f.workflow}: {f.detail}" for f in findings]
