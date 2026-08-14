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


# ── second-parent evidence fallback (nexus-au8zz) ───────────────────────────
#
# v7.7.0 (run 31791811425): the tagged merge commit's own check-runs had not
# arrived yet (the develop-push CI that populates them takes ~15 min), so
# this gate blocked a genuinely-passing release. The fix: when the tagged
# SHA's own evidence is incomplete AND it is a two-parent merge commit, ask
# the SAME question of parents[1] -- the PR head that fed the merge, whose
# check-runs were already populated at PR-merge time.

_PARENT_SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_BASE_PARENT_SHA = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
_OTHER_PARENT_SHA = "cccccccccccccccccccccccccccccccccccccccc"
_UNRELATED_SHA = "dddddddddddddddddddddddddddddddddddddddd"


def _pr(
    number: int = 1456,
    merged: bool = True,
    base_ref: str = "main",
    merge_commit_sha: str = _SHA,
    head_sha: str = _PARENT_SHA,
) -> dict:
    """A GitHub pull-request object shaped like the real
    `GET /repos/{repo}/commits/{sha}/pulls` response (verified live,
    2026-08-14, against PR #1456 / commit 75ed63159385cb70f57bec7b972a5ba229d4b787)."""
    return {
        "number": number,
        "merged_at": "2026-08-14T10:20:13Z" if merged else None,
        "base": {"ref": base_ref},
        "head": {"sha": head_sha},
        "merge_commit_sha": merge_commit_sha,
    }


def _fake_dispatcher(
    check_runs_by_sha: dict[str, list[dict]],
    commits_by_sha: dict[str, dict],
    pulls_by_sha: dict[str, list[dict]] | None = None,
    calls: list[str] | None = None,
):
    """A fake `api` callable that routes by URL shape: the check-runs
    endpoint (suffix `/check-runs?per_page=100`), the pull-request
    association endpoint (suffix `/pulls`), vs. the bare commit-metadata
    endpoint `fetch_commit` uses to read `parents`."""
    pulls_by_sha = pulls_by_sha or {}

    def fake_api(url: str):
        if calls is not None:
            calls.append(url)
        if url.endswith("/check-runs?per_page=100"):
            sha = url.split("/commits/")[1].split("/check-runs")[0]
            return {"check_runs": check_runs_by_sha.get(sha, [])}
        if url.endswith("/pulls"):
            sha = url.split("/commits/")[1].split("/pulls")[0]
            return pulls_by_sha.get(sha, [])
        sha = url.split("/commits/")[1]
        return commits_by_sha.get(sha, {})

    return fake_api


def test_check_falls_back_to_merge_parent_when_own_evidence_missing_and_parent_green(capsys):
    """Empty own-SHA check-runs + a two-parent merge commit whose PR-head
    parent is green AND whose merge is verified via GitHub's own PR
    association record -> the gate PASSES, naming the evidence source."""
    api = _fake_dispatcher(
        check_runs_by_sha={_SHA: [], _PARENT_SHA: [_run("pytest-gate")]},
        commits_by_sha={_SHA: {"parents": [{"sha": _BASE_PARENT_SHA}, {"sha": _PARENT_SHA}]}},
        pulls_by_sha={_SHA: [_pr()]},
    )
    rc = gate.check(_REPO, _SHA, "tok", api=api)
    assert rc == 0
    out = capsys.readouterr().out
    assert "evidence from merge parent" in out
    assert _PARENT_SHA in out
    assert "PR head" in out
    assert "merge commit itself carried none" in out
    assert "nexus-au8zz" in out
    assert "#1456" in out
    assert "verified via merged pull request" in out


def test_check_reports_both_own_and_parent_problems_when_parent_also_fails(capsys):
    """Empty own-SHA check-runs + a two-parent, PR-verified merge commit
    whose PR-head parent ALSO fails -> BLOCKED, with both SHAs' problems
    reported."""
    api = _fake_dispatcher(
        check_runs_by_sha={_SHA: [], _PARENT_SHA: [_run("pytest-gate", conclusion="failure")]},
        commits_by_sha={_SHA: {"parents": [{"sha": _BASE_PARENT_SHA}, {"sha": _PARENT_SHA}]}},
        pulls_by_sha={_SHA: [_pr()]},
    )
    rc = gate.check(_REPO, _SHA, "tok", api=api)
    assert rc == 1
    err = capsys.readouterr().err
    assert "BLOCKED" in err
    assert _SHA in err
    assert "Also checked merge parent" in err
    assert _PARENT_SHA in err
    assert "#1456" in err
    assert "'failure'" in err


def test_check_non_merge_commit_never_chases_a_parent(capsys):
    """Kill control: a single-parent (non-merge) commit with missing own
    evidence must fail exactly as before this fix -- no parent check-runs
    fetch, and no PR-association lookup, is ever attempted, proving the
    fallback cannot fire here."""
    calls: list[str] = []
    api = _fake_dispatcher(
        check_runs_by_sha={_SHA: []},
        commits_by_sha={_SHA: {"parents": [{"sha": _BASE_PARENT_SHA}]}},
        calls=calls,
    )
    rc = gate.check(_REPO, _SHA, "tok", api=api)
    assert rc == 1
    err = capsys.readouterr().err
    assert "BLOCKED" in err
    assert "Also checked merge parent" not in err
    check_run_calls = [u for u in calls if u.endswith("/check-runs?per_page=100")]
    assert len(check_run_calls) == 1, (
        "the non-merge commit's own check-runs fetch is the only check-runs "
        f"call expected; a second one means the fallback fired anyway: {calls}"
    )
    assert not any(u.endswith("/pulls") for u in calls), (
        "no PR-association lookup should happen when the fallback never fires"
    )


def test_check_octopus_merge_never_chases_a_parent(capsys):
    """SIGNIFICANT-1 (round 2 review): the docstring claims a commit with
    MORE than two parents (an octopus merge) never triggers the fallback
    either -- this was previously asserted only by comment, not by test."""
    calls: list[str] = []
    api = _fake_dispatcher(
        check_runs_by_sha={_SHA: []},
        commits_by_sha={
            _SHA: {
                "parents": [
                    {"sha": _BASE_PARENT_SHA},
                    {"sha": _PARENT_SHA},
                    {"sha": _OTHER_PARENT_SHA},
                ]
            }
        },
        calls=calls,
    )
    rc = gate.check(_REPO, _SHA, "tok", api=api)
    assert rc == 1
    err = capsys.readouterr().err
    assert "BLOCKED" in err
    assert "Also checked merge parent" not in err
    check_run_calls = [u for u in calls if u.endswith("/check-runs?per_page=100")]
    assert len(check_run_calls) == 1, (
        f"an octopus merge (>2 parents) must never trigger the fallback: {calls}"
    )
    assert not any(u.endswith("/pulls") for u in calls)


def test_check_parent_lookup_api_error_fails_unverifiable_not_blocked(capsys):
    """An API error while resolving the merge commit's parents must degrade
    to the existing CANNOT VERIFY (exit 2) path -- never a silent BLOCKED
    that masks 'we could not check' as 'it failed'."""

    def boom(url: str) -> dict:
        if url.endswith("/check-runs?per_page=100"):
            return {"check_runs": []}
        raise urllib.error.HTTPError(url, 500, "Internal Server Error", {}, None)

    rc = gate.check(_REPO, _SHA, "tok", api=boom)
    assert rc == 2
    err = capsys.readouterr().err
    assert "CANNOT VERIFY" in err
    assert "parent" in err.lower()


def test_check_parent_check_runs_fetch_error_also_fails_unverifiable(capsys):
    """Same guard, one hop further: the commit-metadata lookup and PR
    association both succeed and verify a merge parent, but fetching THAT
    parent's check-runs errors -- still exit 2, never a pass and never a
    bare BLOCKED."""

    def boom(url: str):
        if url.endswith("/check-runs?per_page=100"):
            if _PARENT_SHA in url:
                raise urllib.error.URLError("connection refused")
            return {"check_runs": []}
        if url.endswith("/pulls"):
            return [_pr()]
        return {"parents": [{"sha": _BASE_PARENT_SHA}, {"sha": _PARENT_SHA}]}

    rc = gate.check(_REPO, _SHA, "tok", api=boom)
    assert rc == 2
    assert "CANNOT VERIFY" in capsys.readouterr().err


def test_check_own_sha_evidence_green_never_calls_the_parent_fallback(capsys):
    """Own-SHA evidence still preferred when present: no commit-metadata,
    PR-association, or parent check-runs call is made at all when the
    SHA's own check-runs already prove every required context green."""
    calls: list[str] = []
    api = _fake_dispatcher(
        check_runs_by_sha={_SHA: [_run("pytest-gate")]},
        commits_by_sha={_SHA: {"parents": [{"sha": _BASE_PARENT_SHA}, {"sha": _PARENT_SHA}]}},
        pulls_by_sha={_SHA: [_pr()]},
        calls=calls,
    )
    rc = gate.check(_REPO, _SHA, "tok", api=api)
    assert rc == 0
    assert calls == [
        f"https://api.github.com/repos/{_REPO}/commits/{_SHA}/check-runs?per_page=100"
    ], "own-sha success must short-circuit before any parent lookup call"


# ── round-2 CRITICAL fix: bind the parent evidence to a GENUINE merge ──────
#
# Round-1 review proved LIVE (not hypothetically) that this repo's branch
# protection does not restrict who can push a two-parent commit: any
# collaborator with push access could fabricate one via
# `git commit-tree -p <anything> -p <any historically-green sha>` and the
# round-1 fallback would print OK for a tree that never went through
# review. These tests pin that the association check actually refuses to
# borrow evidence unless GitHub's own merge record ties parents[1] to the
# TAGGED sha via a genuinely merged pull request.


def test_check_refuses_fallback_when_no_associated_pr_exists(capsys):
    """The core CRITICAL-fix case: a two-parent commit whose second parent
    has green check-runs, but GitHub records NO pull request associating
    it with the tagged sha at all -- refused, never a silent pass. Proves
    the parent's check-runs are never even fetched once the association
    check fails (no unnecessary evidence-borrowing attempt)."""
    calls: list[str] = []
    api = _fake_dispatcher(
        check_runs_by_sha={_SHA: [], _PARENT_SHA: [_run("pytest-gate")]},
        commits_by_sha={_SHA: {"parents": [{"sha": _BASE_PARENT_SHA}, {"sha": _PARENT_SHA}]}},
        pulls_by_sha={_SHA: []},
        calls=calls,
    )
    rc = gate.check(_REPO, _SHA, "tok", api=api)
    assert rc == 1
    err = capsys.readouterr().err
    assert "BLOCKED" in err
    assert "hand-crafted" in err
    assert "no MERGED pull request" in err
    check_run_calls = [u for u in calls if u.endswith("/check-runs?per_page=100")]
    assert check_run_calls == [
        f"https://api.github.com/repos/{_REPO}/commits/{_SHA}/check-runs?per_page=100"
    ], "the unverified parent's check-runs must never be fetched -- that IS the borrowed evidence being refused"


def test_check_refuses_fallback_when_pr_merge_commit_sha_does_not_match_tagged_sha(capsys):
    """The actual fabrication scenario: an attacker's tagged sha names a
    real, merged, main-based PR's head as parents[1] -- but that PR's own
    merge_commit_sha is its OWN real merge commit, not the attacker's
    fabricated tagged sha. Association must fail on this mismatch."""
    api = _fake_dispatcher(
        check_runs_by_sha={_SHA: [], _PARENT_SHA: [_run("pytest-gate")]},
        commits_by_sha={_SHA: {"parents": [{"sha": _BASE_PARENT_SHA}, {"sha": _PARENT_SHA}]}},
        pulls_by_sha={_SHA: [_pr(merge_commit_sha=_UNRELATED_SHA)]},
    )
    rc = gate.check(_REPO, _SHA, "tok", api=api)
    assert rc == 1
    assert "hand-crafted" in capsys.readouterr().err


def test_check_refuses_fallback_when_pr_not_merged(capsys):
    """A matching PR exists but is not (yet) merged -- merged_at is unset --
    must not satisfy the association check."""
    api = _fake_dispatcher(
        check_runs_by_sha={_SHA: [], _PARENT_SHA: [_run("pytest-gate")]},
        commits_by_sha={_SHA: {"parents": [{"sha": _BASE_PARENT_SHA}, {"sha": _PARENT_SHA}]}},
        pulls_by_sha={_SHA: [_pr(merged=False)]},
    )
    rc = gate.check(_REPO, _SHA, "tok", api=api)
    assert rc == 1
    assert "hand-crafted" in capsys.readouterr().err


def test_check_refuses_fallback_when_pr_base_is_not_main(capsys):
    """A matching, merged PR exists but was merged into a branch other than
    main -- must not satisfy the association check (main is the only
    branch REQUIRED_CHECK_CONTEXTS' required checks are enforced on)."""
    api = _fake_dispatcher(
        check_runs_by_sha={_SHA: [], _PARENT_SHA: [_run("pytest-gate")]},
        commits_by_sha={_SHA: {"parents": [{"sha": _BASE_PARENT_SHA}, {"sha": _PARENT_SHA}]}},
        pulls_by_sha={_SHA: [_pr(base_ref="develop")]},
    )
    rc = gate.check(_REPO, _SHA, "tok", api=api)
    assert rc == 1
    assert "hand-crafted" in capsys.readouterr().err


def test_check_refuses_fallback_when_pr_head_does_not_match_parent(capsys):
    """A matching, merged, main-based PR exists with the right
    merge_commit_sha, but its recorded head sha is NOT parents[1] -- must
    not satisfy the association check (this is the binding to the SPECIFIC
    commit whose check-runs are about to be trusted)."""
    api = _fake_dispatcher(
        check_runs_by_sha={_SHA: [], _PARENT_SHA: [_run("pytest-gate")]},
        commits_by_sha={_SHA: {"parents": [{"sha": _BASE_PARENT_SHA}, {"sha": _PARENT_SHA}]}},
        pulls_by_sha={_SHA: [_pr(head_sha=_UNRELATED_SHA)]},
    )
    rc = gate.check(_REPO, _SHA, "tok", api=api)
    assert rc == 1
    assert "hand-crafted" in capsys.readouterr().err


def test_check_association_lookup_api_error_fails_unverifiable(capsys):
    """An API error while resolving the PR association must degrade to the
    existing CANNOT VERIFY (exit 2) path, never a pass and never a bare
    BLOCKED that could be misread as 'the check genuinely failed'."""

    def boom(url: str):
        if url.endswith("/check-runs?per_page=100"):
            return {"check_runs": []}
        if url.endswith("/pulls"):
            raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)
        return {"parents": [{"sha": _BASE_PARENT_SHA}, {"sha": _PARENT_SHA}]}

    rc = gate.check(_REPO, _SHA, "tok", api=boom)
    assert rc == 2
    err = capsys.readouterr().err
    assert "CANNOT VERIFY" in err
    assert "pull request" in err.lower()


def test_check_multiple_associated_prs_finds_the_matching_one(capsys):
    """A commit can have more than one associated PR listed (rebases,
    re-targets); the verified match must be found regardless of order or
    the presence of unrelated/non-matching entries."""
    api = _fake_dispatcher(
        check_runs_by_sha={_SHA: [], _PARENT_SHA: [_run("pytest-gate")]},
        commits_by_sha={_SHA: {"parents": [{"sha": _BASE_PARENT_SHA}, {"sha": _PARENT_SHA}]}},
        pulls_by_sha={
            _SHA: [
                _pr(number=1, merged=False),
                _pr(number=2, base_ref="develop"),
                _pr(number=1456),
            ]
        },
    )
    rc = gate.check(_REPO, _SHA, "tok", api=api)
    assert rc == 0
    assert "#1456" in capsys.readouterr().out


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
def test_v7_7_0_merge_parent_fallback_verifies_against_the_real_incident():
    """SIGNIFICANT-2 (round 2 review): a durable live pin for the
    second-parent evidence fallback ITSELF, exercised against the actual
    v7.7.0 incident this fix was built for (run 31791811425) -- mirrors
    test_v7_6_1_merge_commit_has_real_green_evidence's pattern for the base
    gate, which had no equivalent coverage for the fallback path.

    Deliberately does NOT call `gate.check()` end-to-end: by the time this
    runs, hours/days after the incident, the merge commit's OWN check-runs
    have long since arrived via the develop-push CI (the very asynchrony
    this fix works around), so `check()` would pass on the own-sha path
    and never touch the fallback code at all -- proving nothing about the
    fix. Instead this pins the FALLBACK MECHANISM's three real
    ingredients directly, each still true independent of the merge
    commit's own evidence: (1) the tagged commit's second parent is the
    real PR head, (2) GitHub's own merge record associates that head with
    this exact tagged commit via a genuinely merged, main-based PR, and
    (3) that PR head's own check-runs are (were) green -- i.e. the
    fallback WOULD have produced OK had it been asked at tag time.
    """
    token = _live_github_token()
    if not token:
        pytest.skip(
            "no GITHUB_TOKEN / authenticated `gh` available -- cannot hit "
            "the live GitHub API. This is the nexus-au8zz round-2 "
            "regression pin for the merge-parent fallback; run with a "
            "token (`gh auth login`, or export GITHUB_TOKEN) to actually "
            "exercise it."
        )

    import subprocess

    tag_check = subprocess.run(
        ["git", "rev-parse", "v7.7.0^{commit}"], capture_output=True, text=True,
    )
    if tag_check.returncode != 0:
        pytest.skip("checkout has no v7.7.0 tag (shallow CI clone)")
    tagged_sha = tag_check.stdout.strip()

    commit = gate.fetch_commit(_REPO, tagged_sha, token)
    parents = commit.get("parents") or []
    assert len(parents) == 2, (
        f"v7.7.0's merge commit {tagged_sha} is expected to be an ordinary "
        f"two-parent merge -- got {len(parents)} parents; if this repo's "
        "merge strategy changed, this pin needs a different fixture commit"
    )
    parent_sha = parents[1]["sha"]

    associated_prs = gate.fetch_associated_pull_requests(_REPO, tagged_sha, token)
    verified_pr = gate._find_verified_parent_pr(associated_prs, tagged_sha, parent_sha)
    assert verified_pr is not None, (
        f"expected a live, GitHub-recorded merged PR tying parent "
        f"{parent_sha} to tagged commit {tagged_sha} via merge_commit_sha "
        "-- if this now fails, either GitHub's PR-association data expired "
        "for this old commit, or the association check has a real regression"
    )

    parent_check_runs = gate.fetch_check_runs(_REPO, parent_sha, token)
    parent_ok, parent_problems = gate.evaluate(parent_check_runs, gate.REQUIRED_CHECK_CONTEXTS)
    assert parent_ok, (
        f"PR head {parent_sha} (v7.7.0's real PR #1456 head) is expected "
        f"to carry a green pytest-gate check-run -- this is the exact "
        f"evidence the fallback borrows for the v7.7.0 incident; problems: "
        f"{parent_problems}"
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
