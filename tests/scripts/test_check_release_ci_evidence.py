# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``scripts/check_release_ci_evidence.py`` (nexus-jvhsw).

release.yml's ``publish`` job had zero ``needs:`` and no test jobs -- a tag
pushed at an arbitrary SHA published to PyPI with no evidence the tree ever
passed CI. This gate reads (never re-runs) GitHub's check-run record for the
tagged commit and fails loud when the specific required context is missing,
pending, or did not conclude ``success`` -- a SKIPPED check reports
``success`` in GitHub's aggregate sense, so the whole point is asserting on
the named context, never an aggregate.

``scripts/`` is on ``pythonpath`` via ``[tool.pytest.ini_options]`` in
``pyproject.toml``, so ``check_release_ci_evidence`` imports directly.
"""
from __future__ import annotations

import urllib.error

import pytest

import check_release_ci_evidence as gate

_SHA = "ce0ac901c6d7bf9162fc05ed922dfb4f8ad7cd4e"
_REPO = "Hellblazer/nexus"


def _run(name: str, status: str = "completed", conclusion: str = "success", completed_at: str = "2026-08-11T00:00:00Z") -> dict:
    return {"name": name, "status": status, "conclusion": conclusion, "completed_at": completed_at}


# ── evaluate(): pure logic, the part that must never silently pass ─────────


def test_all_required_contexts_green_passes():
    runs = [_run("pytest-gate"), _run("ruff lint"), _run("some other job", conclusion="failure")]
    ok, problems = gate.evaluate(runs, required=("pytest-gate",))
    assert ok is True
    assert problems == []


def test_missing_required_context_is_a_failure_not_a_pass():
    """THE bug class: absence must never read as success."""
    runs = [_run("ruff lint"), _run("some other job")]
    ok, problems = gate.evaluate(runs, required=("pytest-gate",))
    assert ok is False
    assert any("no check-run named" in p and "pytest-gate" in p for p in problems)


def test_empty_check_run_list_is_a_failure():
    ok, problems = gate.evaluate([], required=("pytest-gate",))
    assert ok is False
    assert problems


def test_required_context_present_but_skipped_is_a_failure():
    """The nuance the bead calls out explicitly: a SKIPPED check reports
    success in GitHub's aggregate sense, but must NOT satisfy a required
    context here -- this must assert on the SPECIFIC conclusion, not an
    aggregate."""
    runs = [_run("pytest-gate", conclusion="skipped")]
    ok, problems = gate.evaluate(runs, required=("pytest-gate",))
    assert ok is False
    assert any("skipped" in p for p in problems)


def test_required_context_present_but_failed_is_a_failure():
    runs = [_run("pytest-gate", conclusion="failure")]
    ok, problems = gate.evaluate(runs, required=("pytest-gate",))
    assert ok is False
    assert any("'failure'" in p for p in problems)


def test_required_context_still_pending_is_a_failure():
    runs = [_run("pytest-gate", status="in_progress", conclusion=None)]
    ok, problems = gate.evaluate(runs, required=("pytest-gate",))
    assert ok is False
    assert any("in_progress" in p for p in problems)


def test_multiple_required_contexts_all_must_be_green():
    runs = [_run("pytest-gate"), _run("ruff lint", conclusion="failure")]
    ok, problems = gate.evaluate(runs, required=("pytest-gate", "ruff lint"))
    assert ok is False
    assert len(problems) == 1
    assert "ruff lint" in problems[0]


def test_duplicate_named_runs_takes_the_newest():
    """A re-run can leave two check-runs under the same name -- the newest
    (by completed_at) must be the one that decides, not an arbitrary pick
    that could land on a stale failed attempt."""
    runs = [
        _run("pytest-gate", conclusion="failure", completed_at="2026-08-11T00:00:00Z"),
        _run("pytest-gate", conclusion="success", completed_at="2026-08-11T01:00:00Z"),
    ]
    ok, _problems = gate.evaluate(runs, required=("pytest-gate",))
    assert ok is True


def test_real_repo_check_run_shape_evaluates_clean():
    """Regression pin against the ACTUAL check-run payload this script will
    see in production (captured 2026-08-12 via `gh api
    repos/Hellblazer/nexus/commits/<v7.6.1 merge sha>/check-runs`) -- proves
    the logic works against real GitHub response shapes, not just synthetic
    fixtures."""
    runs = [
        _run("pytest-gate"),
        _run("ruff lint"),
        _run("engine-service native build (PR trip-wire)"),
        _run("write-seam behavioural gate (nexus-h29w1)"),
        _run("Build and publish to PyPI"),
    ]
    ok, problems = gate.evaluate(runs, required=gate.REQUIRED_CHECK_CONTEXTS)
    assert ok is True, problems


# ── fetch_check_runs(): the thin API wrapper ────────────────────────────────


def test_fetch_check_runs_requests_the_right_url_and_returns_the_list():
    seen_urls = []

    def fake_api(url):
        seen_urls.append(url)
        return {"total_count": 1, "check_runs": [_run("pytest-gate")]}

    runs = gate.fetch_check_runs(_REPO, _SHA, token="tok", api=fake_api)
    assert len(runs) == 1
    assert runs[0]["name"] == "pytest-gate"
    assert seen_urls == [
        f"https://api.github.com/repos/{_REPO}/commits/{_SHA}/check-runs?per_page=100"
    ]


def test_fetch_check_runs_tolerates_a_missing_check_runs_key():
    ok = gate.fetch_check_runs(_REPO, _SHA, token="tok", api=lambda u: {"total_count": 0})
    assert ok == []


# ── check(): end-to-end wiring, incl. the fail-closed unverifiable path ────


def test_check_passes_when_evidence_is_green(capsys):
    rc = gate.check(_REPO, _SHA, "tok", api=lambda u: {"check_runs": [_run("pytest-gate")]})
    assert rc == 0
    assert "OK" in capsys.readouterr().out


def test_check_blocks_when_evidence_is_missing(capsys):
    rc = gate.check(_REPO, _SHA, "tok", api=lambda u: {"check_runs": []})
    assert rc == 1
    err = capsys.readouterr().err
    assert "BLOCKED" in err
    assert "pytest-gate" in err
    assert "never test the same tree twice" not in err  # remedy references the directive by name, not by re-quoting it
    assert "release.yml" in err  # remedy names where NOT to add a test job


def test_check_unverifiable_on_http_error_fails_closed(capsys):
    def boom(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    rc = gate.check(_REPO, _SHA, "tok", api=boom)
    assert rc == 2
    assert "CANNOT VERIFY" in capsys.readouterr().err


def test_check_unverifiable_on_network_error_fails_closed(capsys):
    def boom(url):
        raise urllib.error.URLError("connection refused")

    rc = gate.check(_REPO, _SHA, "tok", api=boom)
    assert rc == 2
    assert "CANNOT VERIFY" in capsys.readouterr().err


def test_check_missing_inputs_fails_closed_not_vacuously_ok(capsys):
    """No repo/sha/token must never silently pass -- 'could not verify' is
    never 'must be fine'."""
    rc = gate.check("", "", "", api=lambda u: {"check_runs": [_run("pytest-gate")]})
    assert rc == 2
    assert "CANNOT VERIFY" in capsys.readouterr().err


def test_main_reads_defaults_from_env(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_REPOSITORY", _REPO)
    monkeypatch.setenv("GITHUB_SHA", _SHA)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    import unittest.mock as mock

    with mock.patch.object(gate, "fetch_check_runs", return_value=[_run("pytest-gate")]):
        rc = gate.main([])
    assert rc == 0


# ── MANDATORY REGRESSION PIN: this repo's real v7.6.1 evidence must be green ─
#
# Live network call against the real GitHub API -- both tests below are
# marked `integration` (registered in pyproject.toml, "end-to-end tests
# requiring real services") and are therefore EXCLUDED from the default
# `-m 'not integration and not slow and not lint'` unit-suite addopts.
#
# Review finding (nexus-moht0 follow-up, 2026-08-12): the ORIGINAL unmarked
# version of test_v7_6_1_merge_commit_has_real_green_evidence was collected
# into the default unit suite, where CI has no GITHUB_TOKEN and `gh` is
# unauthenticated -- so the "mandatory regression pin" the bead demanded
# SILENTLY skipped in the very pipeline it was meant to gate, which is
# exactly the success-shaped-emptiness class this whole fix exists to close,
# one layer in. Marking it `integration` moves it to where a token and `gh`
# auth actually exist (a developer's machine, or a future CI leg that
# exports GITHUB_TOKEN for `-m integration` runs); every `-m integration`
# invocation in this repo's workflows already carries `-rs` (report-skipped),
# so a still-missing-auth skip stays VISIBLE rather than silent -- same
# doctrine as tests/db/_service_fixture.py's "skips loudly" jar-freshness
# check.
#
# SECOND review round (nexus-93j33): a marker alone only proves these CAN
# run somewhere; it does not prove they DID. Both tests below also carry
# `mandatory_regression_pin`, which tests/conftest.py's session-level
# non-vacuity guard (`_check_mandatory_pin_non_vacuity`) reads: any
# `-m integration` invocation that actually collects them and sees them
# both skip FAILS the run instead of reporting green.
def _live_github_token() -> str:
    import os
    import subprocess

    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        return token
    try:
        gh = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        # `gh` not installed at all -- distinct from "installed but not
        # logged in" (returncode != 0), but both mean "no token available"
        # to this helper's callers.
        return ""
    return gh.stdout.strip() if gh.returncode == 0 else ""


@pytest.mark.integration
@pytest.mark.mandatory_regression_pin
def test_v7_6_1_merge_commit_has_real_green_evidence():
    import subprocess

    token = _live_github_token()
    if not token:
        pytest.skip(
            "no GITHUB_TOKEN / authenticated `gh` available -- cannot hit "
            "the live GitHub API. This is the nexus-jvhsw regression pin; "
            "run with a token (`gh auth login`, or export GITHUB_TOKEN) to "
            "actually exercise it -- see the section comment above."
        )

    tag_check = subprocess.run(
        ["git", "rev-parse", "v7.6.1^{commit}"], capture_output=True, text=True,
    )
    if tag_check.returncode != 0:
        pytest.skip("checkout has no v7.6.1 tag (shallow CI clone)")
    sha = tag_check.stdout.strip()

    rc = gate.check(_REPO, sha, token)
    assert rc == 0, (
        "this repo's real v7.6.1 merge commit is expected to carry a green "
        "pytest-gate check-run -- if this now fails, either GitHub's "
        "check-run retention expired for this old commit, or the gate has a "
        "real regression"
    )


@pytest.mark.integration
@pytest.mark.mandatory_regression_pin
def test_required_check_contexts_matches_live_branch_protection():
    """Drift check for the LOW flagged at review: REQUIRED_CHECK_CONTEXTS is
    a hand-maintained constant (same shape as
    check_client_release_precondition.py's ENGINE_CLIENT_PRECONDITIONS) with
    no lint against live branch protection. This compares it against the
    real API so a rename shows up here first, instead of as a mysteriously
    red release gate naming a check that "never ran"."""
    import json
    import subprocess

    token = _live_github_token()
    if not token:
        pytest.skip(
            "no GITHUB_TOKEN / authenticated `gh` available -- cannot verify "
            "REQUIRED_CHECK_CONTEXTS against live branch protection."
        )

    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{_REPO}/branches/main/protection",
             "--jq", ".required_status_checks.contexts"],
            capture_output=True, text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # GITHUB_TOKEN can be set without `gh` being installed at all --
        # _live_github_token() only shells out to `gh` when GITHUB_TOKEN is
        # absent, so this is reachable even after that helper returned a
        # token.
        pytest.skip(f"`gh` unavailable to query branch protection: {exc}")
    if proc.returncode != 0:
        pytest.skip(
            f"could not read live branch protection for {_REPO} "
            f"(gh exit {proc.returncode}: {proc.stderr.strip()}) -- "
            "requires push access to read branch protection, which not "
            "every contributor's token carries."
        )
    live_contexts = set(json.loads(proc.stdout))
    assert set(gate.REQUIRED_CHECK_CONTEXTS) == live_contexts, (
        f"REQUIRED_CHECK_CONTEXTS {gate.REQUIRED_CHECK_CONTEXTS} has drifted "
        f"from main's live required contexts {sorted(live_contexts)} -- "
        "update the constant (and its module-docstring justification) in "
        "scripts/check_release_ci_evidence.py"
    )
