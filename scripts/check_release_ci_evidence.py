#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Publish-time evidence gate: did the tagged commit actually pass CI? (nexus-jvhsw)

``.github/workflows/release.yml``'s ``publish`` job has zero ``needs:`` and no
test jobs -- a ``v*`` tag pushed at an ARBITRARY SHA published to PyPI with
NO verification that the tree it names ever passed a single check. The
workflow's own header states the assumption explicitly ("a v* tag always
points at a main merge commit whose identical tree passed the release PR's
required checks minutes before the tag was pushed") and then never checked
it.

THE OBVIOUS FIX IS FORBIDDEN. Adding test jobs (or a ``needs:`` on the
existing ``ci.yml`` matrix) to ``release.yml`` would RE-RUN the suite at tag
time, violating CI Cost Discipline ("never test the same tree twice" --
AGENTS.md, earned by the 2026-07-06 billing incident: 24 CI runs + 4 engine
tags in one day, the same tree tested four times en route to PyPI). Main has
no push-triggered CI at all (``ci.yml``'s ``on.push.branches: [develop]``) by
the SAME cost decision -- so re-testing at tag time would not just duplicate
work, it would be the ONLY test run for that exact merge-commit SHA, which is
worse than what this gate replaces.

THE CORRECT SHAPE IS EVIDENCE, NOT RE-EXECUTION. GitHub actually copies the
PR's check-run results onto the base-branch merge commit itself (verified
empirically against this repo's own history, 2026-08-12: ``main``'s merge
commits carry the full 21-run check-run list, including ``pytest-gate``,
identical in substance to the PR head commit's own check-run list). So the
tagged SHA can be asked directly, via the GitHub Checks API
(``GET /repos/{repo}/commits/{sha}/check-runs``), whether it already carries
a green required check -- no re-execution, just a read.

THE NUANCE THIS SCRIPT EXISTS TO HONOR: a SKIPPED check reports
``conclusion=success`` in GitHub's aggregate sense (branch-protection-safe
skip pattern), so this script asserts on the SPECIFIC required check-run
NAMEs (:data:`REQUIRED_CHECK_CONTEXTS`), not on any aggregate/overall
conclusion, and "no check run found under this name for this SHA" is a
FAILURE, never a silent pass -- that absence-is-success shape is exactly the
bug class this script exists to close (nexus-moht0).

:data:`REQUIRED_CHECK_CONTEXTS` mirrors this repo's live branch-protection
required contexts for ``main`` (currently exactly ``("pytest-gate",)`` --
verified live via
``gh api repos/Hellblazer/nexus/branches/main/protection --jq
'.required_status_checks.contexts'``, 2026-08-12). It is a hand-maintained
constant, not a live query: reading branch protection needs
``administration:read``, which the default ``GITHUB_TOKEN`` in
``release.yml`` is not granted (and should not be, to keep the publish job's
permission surface minimal). Update this constant if ``main``'s required
contexts ever change -- the same maintenance obligation
``check_client_release_precondition.py``'s ``ENGINE_CLIENT_PRECONDITIONS``
already carries for a comparable hand-maintained table.

KNOWN GAP, noted rather than fixed at review (nexus-moht0 follow-up, flagged
2026-08-12): unlike that table, THIS constant has no stale-row-style lint
against live branch protection -- if the required check is ever renamed,
this gate reds every release with a message naming the OLD name, which will
not say why the check never seems to run. ``evaluate``'s per-context failure
message names this as a candidate cause with the ``gh api`` command to
check it; a live drift check (comparing this constant against
``branches/main/protection`` via ``gh api``) would need network + a token
with ``administration:read`` and belongs with the integration-marked tests
below, not the unit suite, if one is added.

Usage::

    uv run python scripts/check_release_ci_evidence.py
    uv run python scripts/check_release_ci_evidence.py --sha <sha> --repo owner/name

Exit codes: ``0`` every required context has a successful check-run on the
named SHA, ``1`` blocked (a required context is missing, pending, or did not
conclude ``success``), ``2`` unverifiable (no token/repo, network/API error --
"could not verify" is never "must be fine").
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Callable

#: This repo's LIVE required status-check contexts for `main` (the branch
#: every release tag's merge commit lands on). See the module docstring for
#: how this was verified and why it is a constant, not a live API query.
REQUIRED_CHECK_CONTEXTS: tuple[str, ...] = ("pytest-gate",)

_REMEDY = (
    "Remedy: this SHA has no evidence of a green required check. If it is a "
    "genuine release commit, it should already carry one (main gets there "
    "only via a PR whose required checks passed) -- investigate why the "
    "check-run is missing before publishing. Do NOT respond by adding a "
    "test job to release.yml: that re-runs CI at tag time, which CI Cost "
    "Discipline (AGENTS.md, the 2026-07-06 billing incident) forbids."
)


def _api(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "nexus-check-release-ci-evidence",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — fixed api.github.com host
        return json.loads(resp.read())


def fetch_check_runs(
    repo: str, sha: str, token: str, api: Callable[[str], dict] | None = None
) -> list[dict]:
    """Every check-run GitHub has recorded against *sha*.

    ``per_page=100``: this repo's real check-run count for a release merge
    commit is ~21 (verified 2026-08-12), comfortably under one page. A repo
    that grows past 100 check-runs per commit would need pagination added
    here -- documented rather than silently truncated, so a future maintainer
    knows to look.
    """
    call = api or (lambda u: _api(u, token))
    url = (
        f"https://api.github.com/repos/{repo}/commits/{sha}/check-runs"
        "?per_page=100"
    )
    data = call(url)
    return list(data.get("check_runs") or [])


def evaluate(
    check_runs: list[dict], required: tuple[str, ...] = REQUIRED_CHECK_CONTEXTS
) -> tuple[bool, list[str]]:
    """Pure logic: does *check_runs* prove every context in *required* green?

    Matches by NAME, not by any aggregate/overall conclusion -- a required
    context that is absent, still pending, or concluded anything other than
    ``success`` (``skipped``, ``neutral``, ``cancelled``, ``failure``, ...)
    is a problem. Absence is explicitly a problem, not a silent pass: that
    is the entire bug class this script exists to close.
    """
    by_name: dict[str, list[dict]] = {}
    for run in check_runs:
        by_name.setdefault(run.get("name", ""), []).append(run)

    problems: list[str] = []
    for context in required:
        runs = by_name.get(context, [])
        if not runs:
            problems.append(
                f"no check-run named {context!r} found for this commit -- "
                "absence is a FAILURE, not evidence of a pass. Two distinct "
                "causes look identical here: the check genuinely never ran, "
                "OR REQUIRED_CHECK_CONTEXTS is stale because the check was "
                "RENAMED in branch protection since this constant was last "
                "verified (this script has no drift detection against live "
                "branch protection -- see the module docstring). Check with: "
                "gh api repos/<owner>/<repo>/branches/main/protection "
                "--jq '.required_status_checks.contexts'"
            )
            continue
        # Prefer the newest / most conclusive run if GitHub recorded more
        # than one under the same name (e.g. a re-run).
        #
        # KNOWN NARROW GAP (accepted at review, nexus-moht0 follow-up,
        # 2026-08-12): an in-flight re-run has no completed_at (None/empty),
        # which sorts BEFORE a stale completed success -- so this can select
        # an older PASS over a newer, still-running attempt rather than
        # failing closed on the ambiguity. Wrong direction in theory (should
        # prefer "unknown" over "trust the old one"), but the trigger
        # requires a re-run racing a tag push, which is narrow enough that
        # accepting it here (rather than fixing it) was the reviewed call.
        run = max(runs, key=lambda r: r.get("completed_at") or "")
        status = run.get("status")
        conclusion = run.get("conclusion")
        if status != "completed":
            problems.append(
                f"{context!r} has not completed (status={status!r}) for this commit"
            )
        elif conclusion != "success":
            problems.append(
                f"{context!r} concluded {conclusion!r}, not 'success', for this commit"
            )
    return (not problems, problems)


def check(repo: str, sha: str, token: str, api: Callable[[str], dict] | None = None) -> int:
    if not repo or not sha or not token:
        print(
            "CANNOT VERIFY: --repo, --sha, and a token are all required "
            "(got repo=%r sha=%r token=%s). 'could not verify' is never "
            "'must be fine'." % (repo, sha, "<set>" if token else "<empty>"),
            file=sys.stderr,
        )
        return 2

    try:
        check_runs = fetch_check_runs(repo, sha, token, api=api)
    except urllib.error.HTTPError as exc:
        print(
            f"CANNOT VERIFY: GitHub API error fetching check-runs for {sha} "
            f"in {repo}: HTTP {exc.code} {exc.reason}",
            file=sys.stderr,
        )
        return 2
    except urllib.error.URLError as exc:
        print(
            f"CANNOT VERIFY: network error fetching check-runs for {sha} "
            f"in {repo}: {exc.reason}",
            file=sys.stderr,
        )
        return 2

    ok, problems = evaluate(check_runs, REQUIRED_CHECK_CONTEXTS)
    if not ok:
        print(
            f"BLOCKED: {sha} in {repo} does not carry evidence of a green "
            f"required check ({', '.join(REQUIRED_CHECK_CONTEXTS)}):",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(f"\n{_REMEDY}", file=sys.stderr)
        return 1

    print(
        f"OK: {sha} in {repo} carries a successful check-run for every "
        f"required context ({', '.join(REQUIRED_CHECK_CONTEXTS)})"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--repo", default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="owner/name (default: $GITHUB_REPOSITORY)",
    )
    ap.add_argument(
        "--sha", default=os.environ.get("GITHUB_SHA", ""),
        help="commit SHA the tag points at (default: $GITHUB_SHA)",
    )
    ap.add_argument(
        "--token", default=os.environ.get("GITHUB_TOKEN", ""),
        help="token with read access to checks (default: $GITHUB_TOKEN)",
    )
    args = ap.parse_args(argv)
    return check(args.repo, args.sha, args.token)


if __name__ == "__main__":
    sys.exit(main())
