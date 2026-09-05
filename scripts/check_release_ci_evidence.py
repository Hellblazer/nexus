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

THE CORRECT SHAPE IS EVIDENCE, NOT RE-EXECUTION. The tagged SHA can be asked
directly, via the GitHub Checks API
(``GET /repos/{repo}/commits/{sha}/check-runs``), whether it already carries
a green required check -- no re-execution, just a read.

CORRECTED EMPIRICAL RECORD (nexus-au8zz, 2026-08-14): this docstring
originally claimed GitHub "copies" a PR's check-run results onto the
base-branch merge commit itself, verified against this repo's history on
2026-08-12. That was a MISREADING of the evidence. What was actually
observed was the develop-push CI run (``ci.yml``'s
``on.push.branches: [develop]``) landing on the merge commit SOME TIME
AFTER the merge -- fired by the mandatory develop back-merge (release
checklist step 11b), not by any GitHub check-run copy mechanism. That run
takes on the order of ~15 minutes to complete. v7.7.0 (run 31791811425) hit
exactly the gap this created: the release checklist's own step 9 says tag
IMMEDIATELY after the PR merges, so the tag lands well before the
back-merge's CI run has had time to populate the merge commit's
check-runs, and this gate found nothing there and blocked the publish.
See the SECOND-PARENT EVIDENCE FALLBACK below for the fix.

SECOND-PARENT EVIDENCE FALLBACK (nexus-au8zz): when the tagged SHA's own
check-runs do not (yet) prove every required context green, and the SHA is
a two-parent merge commit, this script re-asks the SAME question of
``parents[1]`` -- the PR head that fed the merge (GitHub always orders a
merge commit's parents as ``[base-tip-before-merge, PR-head]``). The PR
head's check-runs were populated at PR-merge time, well before any tag
exists, so they are available immediately -- no waiting on the
asynchronous develop-push run. This is still evidence, not re-execution:
the PR head's checks already ran as part of merging the release PR; this
just reads them from a different commit than the merge commit itself.
Guarded to ONLY the second parent (the PR head) -- ``parents[0]`` is the
branch tip the release PR was merged INTO, which proves nothing about the
release PR's own tree, so it is never consulted. A single-parent commit
(not a merge at all) or a commit with more than two parents (an octopus
merge -- not how this repo merges release PRs) never triggers the
fallback and instead reports failure exactly as before this fix. Any API
error while resolving the parent or its check-runs degrades to the
existing ``CANNOT VERIFY`` (exit 2) path -- "could not verify" is never
"must be fine", the same doctrine ``check()``'s original error handling
already carries.

TRUST MODEL / ACCEPTED RISK (nexus-au8zz round 2, 2026-08-14): round 1 of
this fallback trusted ``parents[1]``'s check-runs on the two-parent shape
alone, with no binding to the tagged commit's actual tree. Stacked review
proved that gap LIVE against this repo's real GitHub configuration (not a
hypothetical): ``gh api repos/Hellblazer/nexus/branches/main/protection``
shows ``enforce_admins=false`` and no ``required_pull_request_reviews``
rule; ``repos/Hellblazer/nexus/tags/protection`` 404s (unconfigured); 4 of
this repo's 5 collaborators are non-admin with push access. So "main gets
there only via a PR whose required checks passed" is a PROCESS convention
(the release skill's checklist + human discipline), not a server-side
technical control -- any of those collaborators could push a two-parent
commit fabricated via ``git commit-tree -p <anything> -p <any
historically-green sha>`` plus a ``v*`` tag, and round 1's fallback would
print OK for a tree that never went through review at all.

THE FIX: before trusting ``parents[1]``'s check-runs, ``check()`` now
calls ``GET /repos/{repo}/commits/{sha}/pulls`` (:func:`fetch_associated_pull_requests`)
and requires GitHub's OWN merge record to tie the two together --
:func:`_find_verified_parent_pr` demands an associated pull request that
is actually merged (``merged_at`` set), based on ``main``, whose
``merge_commit_sha`` equals the TAGGED sha, and whose ``head.sha`` equals
``parents[1]``. Only a real, GitHub-recorded merge of exactly that PR head
into exactly that tagged commit satisfies all four; a fabricated
commit-tree is not any PR's ``merge_commit_sha`` and fails this check, so
it falls through to the ordinary absence-is-failure BLOCKED path (with a
note that the shape looks like a hand-crafted merge) rather than
borrowing unrelated evidence. An API error resolving the association
degrades to the same ``CANNOT VERIFY`` (exit 2) path as every other
network failure in this script.

DEFENSE IN DEPTH -- PLATFORM-SIDE CLOSURE (2026-08-14, same day): the
OWN-SHA path (the original, pre-fallback evidence check) still has no
comparable binding at the CODE layer -- it never checks that the tagged
sha is reachable from ``main``, so any historically-green sha (from ANY
commit, merged or not) could in principle be tagged directly and pass on
its own evidence, and a fabricated merge commit's own evidence gap is
what this script's fallback closes at the code layer, not the tag-creation
act itself. That residual is now closed at the PLATFORM layer instead:
Hal authorized, and GitHub ruleset ``release-tags-admin-only`` (id
``20842268``, ``enforcement=active``) now restricts creation, update,
deletion, and non-fast-forward pushes of ``refs/tags/v*`` and
``refs/tags/engine-service-v*`` to repository admins (bypass actor:
``RepositoryRole`` admin) -- so pushing a ``v*`` tag at all, whether it
names a genuine merge commit or a hand-crafted one, now requires an admin
actor, not merely a collaborator with generic push access. This is
deliberate defense in depth, not redundancy: this script's merged-PR
association binding (above) is the CODE-side layer -- it holds even for
an admin who tags a stale or fabricated sha by mistake -- while the
ruleset is the PLATFORM-side layer -- it holds even if this script is
bypassed, disabled, or run with a compromised token. Neither layer alone
was sufficient; both together are the accepted closure.

THE NUANCE THIS SCRIPT EXISTS TO HONOR: a SKIPPED check reports
``conclusion=success`` in GitHub's aggregate sense (branch-protection-safe
skip pattern), so this script asserts on the SPECIFIC required check-run
NAMEs (:data:`REQUIRED_CHECK_CONTEXTS`), not on any aggregate/overall
conclusion, and "no check run found under this name for this SHA" is a
FAILURE, never a silent pass -- that absence-is-success shape is exactly the
bug class this script exists to close (nexus-moht0).

:data:`REQUIRED_CHECK_CONTEXTS` is the subset of this repo's live
branch-protection required contexts for ``main`` that this script can
actually treat as EVIDENCE. As of 2026-08-22 ``main`` requires three
contexts -- ``pytest-gate``, ``service change detection`` and ``Java tests
+ jOOQ codegen drift guard`` -- verified live via
``gh api repos/Hellblazer/nexus/branches/main/protection --jq
'.required_status_checks.contexts'``. Only the first is evidence-checkable;
the other two are enumerated in :data:`DEFERRED_REQUIRED_CONTEXTS` with the
reason. Together the two constants must equal the live set, which
``test_required_check_contexts_matches_live_branch_protection`` asserts.
It is a hand-maintained
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
from pathlib import Path
from typing import Callable

try:
    from nexus.gate_advisory import passed_by_default
except ModuleNotFoundError:
    # release.yml runs this before `uv sync`, under the runner's bare python3.
    # nexus.gate_advisory is stdlib-only, so the in-repo source tree is enough;
    # tests/scripts/test_check_release_ci_evidence.py runs this file under
    # `python3 -I -S` to keep it that way (v7.31.0's first publish run red'd here).
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from nexus.gate_advisory import passed_by_default

#: This repo's LIVE required status-check contexts for `main` (the branch
#: every release tag's merge commit lands on). See the module docstring for
#: how this was verified and why it is a constant, not a live API query.
REQUIRED_CHECK_CONTEXTS: tuple[str, ...] = ("pytest-gate",)

#: Contexts that ``main``'s branch protection REQUIRES but this script
#: deliberately does NOT treat as evidence. Enumerated, not silently omitted:
#: :data:`REQUIRED_CHECK_CONTEXTS` + this tuple must equal the live required
#: set, so protection changing in either direction reds the drift test rather
#: than passing quietly.
#:
#: Both live in ``.github/workflows/service-ci.yml`` and both are unusable as
#: publish-time evidence, for two INDEPENDENT reasons -- either alone is
#: disqualifying:
#:
#: 1. ABSENCE on the merge commit. That workflow's ``push`` trigger keeps a
#:    ``paths: service/**`` filter (deliberately -- pushes gate nothing, so
#:    filtering there is pure cost saving). A release that touches no
#:    ``service/`` file therefore produces NO service-ci run on the merge
#:    commit at all, and :func:`evaluate` counts absence as a hard failure.
#: 2. ``skipped`` conclusion. ``Java tests + jOOQ codegen drift guard`` is
#:    ``if: needs.changes.outputs.service == 'true'``. Branch protection
#:    treats a job skipped via ``if:`` as SUCCESS; :func:`evaluate` treats
#:    every conclusion other than ``success`` -- ``skipped`` named
#:    explicitly -- as a problem. That asymmetry is intentional on both
#:    sides and is not a bug in either.
#:
#: So requiring these here would red the publish gate on every release that
#: does not touch the engine, which is most of them. Closing the gap properly
#: needs a per-context PR-head fallback plus a skipped-is-acceptable rule
#: bounded to conditionally-skipped jobs; that is a deliberate design change
#: to release-critical machinery, not a constant edit, and is NOT done here.
#: What IS closed here: the constant no longer claims main requires only
#: ``pytest-gate``, which was false from the moment service-ci became
#: required (Hal directive 2026-07-31) and went undetected because the only
#: check against live protection runs solely under an admin-scoped token.
DEFERRED_REQUIRED_CONTEXTS: tuple[str, ...] = (
    "service change detection",
    "Java tests + jOOQ codegen drift guard",
)

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


def fetch_commit(
    repo: str, sha: str, token: str, api: Callable[[str], dict] | None = None
) -> dict:
    """The commit object for *sha* -- used only to inspect its ``parents``.

    Backs the second-parent evidence fallback (nexus-au8zz): before
    concluding a BLOCKED verdict, ``check()`` calls this to determine
    whether *sha* is a two-parent merge commit whose second parent (the PR
    head) might carry the evidence *sha* itself does not have yet. Never
    called when the SHA's own check-runs already prove every required
    context green (see the module docstring's SECOND-PARENT EVIDENCE
    FALLBACK section).
    """
    call = api or (lambda u: _api(u, token))
    url = f"https://api.github.com/repos/{repo}/commits/{sha}"
    return call(url)


def fetch_associated_pull_requests(
    repo: str, sha: str, token: str, api: Callable[[str], object] | None = None
) -> list[dict]:
    """Pull requests GitHub associates with *sha*.

    Backs the round-2 CRITICAL fix (nexus-au8zz): before the second-parent
    fallback trusts a parent commit's check-runs as evidence for *sha*, it
    must find a genuinely merged pull request whose ``merge_commit_sha`` IS
    *sha* -- see :func:`_find_verified_parent_pr` and the module
    docstring's TRUST MODEL section. Returns ``[]`` for any non-list
    response shape rather than raising, since an unexpected shape here
    should read as "no verified association", not a crash.
    """
    call = api or (lambda u: _api(u, token))
    url = f"https://api.github.com/repos/{repo}/commits/{sha}/pulls"
    data = call(url)
    return list(data) if isinstance(data, list) else []


def _find_verified_parent_pr(
    pull_requests: list[dict], tagged_sha: str, parent_sha: str
) -> dict | None:
    """The pull request (if any) that proves *parent_sha* is the head GitHub
    actually merged to PRODUCE *tagged_sha*, per its own merge record.

    All four conditions must hold: the PR is merged (``merged_at`` set --
    a closed-but-unmerged PR has ``merged_at=None``), its base branch is
    ``main`` (the branch every release tag's merge commit lands on, per
    :data:`REQUIRED_CHECK_CONTEXTS`'s own justification), its
    ``merge_commit_sha`` equals *tagged_sha* (GitHub's own record of what
    commit that merge produced), and its head sha equals *parent_sha* (the
    commit whose check-runs the fallback is about to trust). A fabricated
    two-parent commit (``git commit-tree -p ... -p ...``) is not any PR's
    ``merge_commit_sha`` and matches nothing here.
    """
    for pr in pull_requests:
        if not pr.get("merged_at"):
            continue
        base = pr.get("base") or {}
        head = pr.get("head") or {}
        if (
            base.get("ref") == "main"
            and pr.get("merge_commit_sha") == tagged_sha
            and head.get("sha") == parent_sha
        ):
            return pr
    return None


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
    if ok:
        print(
            f"OK: {sha} in {repo} carries a successful check-run for every "
            f"required context ({', '.join(REQUIRED_CHECK_CONTEXTS)})"
        )
        return 0

    # Own-SHA evidence is missing or incomplete. Before failing, check
    # whether this is a two-parent merge commit whose second parent (the PR
    # head) already carries the evidence (nexus-au8zz: closes the
    # tag-immediately-races-develop-push-CI race -- see the module
    # docstring's SECOND-PARENT EVIDENCE FALLBACK section).
    try:
        commit = fetch_commit(repo, sha, token, api=api)
    except urllib.error.HTTPError as exc:
        print(
            f"CANNOT VERIFY: GitHub API error fetching commit metadata for "
            f"{sha} in {repo} (needed for the merge-parent evidence "
            f"fallback): HTTP {exc.code} {exc.reason}",
            file=sys.stderr,
        )
        return 2
    except urllib.error.URLError as exc:
        print(
            f"CANNOT VERIFY: network error fetching commit metadata for "
            f"{sha} in {repo} (needed for the merge-parent evidence "
            f"fallback): {exc.reason}",
            file=sys.stderr,
        )
        return 2

    parents = commit.get("parents") or []
    parent_sha = parents[1].get("sha") if len(parents) == 2 else None

    if parent_sha:
        # Round-2 CRITICAL fix (nexus-au8zz): do not trust parent_sha's
        # check-runs until GitHub's own merge record ties it to THIS
        # tagged sha -- see fetch_associated_pull_requests /
        # _find_verified_parent_pr and the module docstring's TRUST MODEL
        # section for why this binding is required.
        try:
            associated_prs = fetch_associated_pull_requests(repo, sha, token, api=api)
        except urllib.error.HTTPError as exc:
            print(
                f"CANNOT VERIFY: GitHub API error fetching pull requests "
                f"associated with {sha} in {repo} (needed to bind the "
                f"merge-parent evidence fallback to a genuine merge): "
                f"HTTP {exc.code} {exc.reason}",
                file=sys.stderr,
            )
            return 2
        except urllib.error.URLError as exc:
            print(
                f"CANNOT VERIFY: network error fetching pull requests "
                f"associated with {sha} in {repo} (needed to bind the "
                f"merge-parent evidence fallback to a genuine merge): "
                f"{exc.reason}",
                file=sys.stderr,
            )
            return 2

        verified_pr = _find_verified_parent_pr(associated_prs, sha, parent_sha)
        if verified_pr is None:
            print(
                f"BLOCKED: {sha} in {repo} does not carry evidence of a green "
                f"required check ({', '.join(REQUIRED_CHECK_CONTEXTS)}):",
                file=sys.stderr,
            )
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            print(
                f"This is a two-parent commit (second parent {parent_sha}), "
                f"but GitHub records no MERGED pull request whose "
                f"merge_commit_sha is {sha}, base is 'main', and head sha "
                f"is {parent_sha} -- refusing to borrow evidence from an "
                f"unverified parent. This SHA may be a hand-crafted "
                f"two-parent commit (e.g. via `git commit-tree -p ... -p "
                f"...`) rather than a genuine PR merge.",
                file=sys.stderr,
            )
            print(f"\n{_REMEDY}", file=sys.stderr)
            return 1

        try:
            parent_check_runs = fetch_check_runs(repo, parent_sha, token, api=api)
        except urllib.error.HTTPError as exc:
            print(
                f"CANNOT VERIFY: GitHub API error fetching check-runs for "
                f"merge parent {parent_sha} (PR head) in {repo}: "
                f"HTTP {exc.code} {exc.reason}",
                file=sys.stderr,
            )
            return 2
        except urllib.error.URLError as exc:
            print(
                f"CANNOT VERIFY: network error fetching check-runs for "
                f"merge parent {parent_sha} (PR head) in {repo}: {exc.reason}",
                file=sys.stderr,
            )
            return 2

        parent_ok, parent_problems = evaluate(parent_check_runs, REQUIRED_CHECK_CONTEXTS)
        if parent_ok:
            print(
                f"OK: {sha} in {repo} -- evidence from merge parent "
                f"{parent_sha} (PR head), verified via merged pull request "
                f"#{verified_pr.get('number')} (merge_commit_sha={sha}); "
                f"the merge commit itself carried none at publish time -- "
                f"see nexus-au8zz. Every required context "
                f"({', '.join(REQUIRED_CHECK_CONTEXTS)}) is green on the "
                f"PR head."
            )
            print(
                passed_by_default(
                    "check_release_ci_evidence",
                    f"evidence borrowed from merge parent {parent_sha} (PR head); "
                    f"{sha} itself carried none (nexus-1c7oq)",
                )
            )
            return 0

        print(
            f"BLOCKED: {sha} in {repo} does not carry evidence of a green "
            f"required check ({', '.join(REQUIRED_CHECK_CONTEXTS)}):",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            f"Also checked merge parent {parent_sha} (PR head, verified via "
            f"pull request #{verified_pr.get('number')}) -- it also failed "
            f"to prove every required context green:",
            file=sys.stderr,
        )
        for problem in parent_problems:
            print(f"  - {problem}", file=sys.stderr)
        print(f"\n{_REMEDY}", file=sys.stderr)
        return 1

    print(
        f"BLOCKED: {sha} in {repo} does not carry evidence of a green "
        f"required check ({', '.join(REQUIRED_CHECK_CONTEXTS)}):",
        file=sys.stderr,
    )
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    print(f"\n{_REMEDY}", file=sys.stderr)
    return 1


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
