#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Detect a develop commit whose code was never exercised by pytest (nexus-of2x8).

THE HOLE THIS CLOSES. ``ci.yml``'s doc-only fast lane and its cancel-in-progress
concurrency group are each individually correct, and their composition has a
gap: a code commit's own run can be CANCELLED by a later push, and if that
later push (or a run of pushes after it) is itself docs-only, the doc-only
predicate's ``github.event.before``..HEAD range never contains the cancelled
commit -- it starts AFTER it. The result is silent: a SKIPPED pytest job
reports SUCCESS to branch protection (the "skipped == success" idiom ``ci.yml``
and CI Cost Discipline both rely on deliberately), so develop goes green while
carrying a commit whose code no test has ever touched. Measured sequence,
2026-09-21/22:

    dbf255efe  (touches src/nexus/pdf_extractor.py) -- pushed, run 35683993461
               started, then CANCELLED by a later push's concurrency group.
    df3a688fd  (three docs-only commits on top) -- pushed. The replacement run's
               own doc-only predicate diffed dbf255efe..df3a688fd (three docs
               files) and correctly said docs-only. dbf255efe itself, the
               EXCLUSIVE lower bound of that range, was never in it.

Neither mechanism is wrong on its own -- see AGENTS.md's CI Cost Discipline,
which sanctions BOTH "skipped == success for branch protection" and "every
workflow has a concurrency group; superseded runs cancel" BY NAME. This script
is remedy (c) from the bead: DETECT rather than prevent. It adds an observer,
touches neither mechanism, and does not fight the cost discipline the other
remedies would have had to argue against.

THE MODEL, not range reconstruction. Reconstructing each historical push's own
``github.event.before`` and unioning ranges is unnecessary and fragile (GitHub
does not expose "before" on a completed run after the fact). pytest runs
against the FULL TREE at a run's head commit, not just its diff -- so a LATER
run that actually exercises code (any of the ``pytest (Python ...)``,
``pytest (lint markers)`` or ``pytest (mode-declarations census)`` jobs
concludes ``success``, meaning ``ci.yml``'s ``changes`` job resolved
``code=true`` and pytest genuinely ran) proves every commit reachable from that
run's head was in the tested tree -- INCLUDING an ancestor commit whose own
run was cancelled. Coverage is therefore two rules, applied per commit *C* on
develop between the most recent such run's head (call it *H*) and the current
tip:

    (a) *C* is itself docs-only (its own diff against its parent matches
        ``ci.yml``'s doc-only predicate) -- it needs no code run at all, or
    (b) *C* is an ancestor of (or equal to) *H* -- some run already proved
        code was exercised on a tree that included *C*.

Every commit strictly newer than *H* that fails (a) is UNCOVERED: nobody has
ever run pytest against a tree containing it. This is exactly
``git rev-list H..HEAD`` checked individually against ``is_docs_only_commit``
-- no range-union bookkeeping, no durable state to maintain between runs
(closing option (a)'s objection: this script re-derives *H* from the GitHub
Actions API on every invocation instead of requiring a stored "last exercised
sha").

DOC-ONLY PREDICATE, ported not re-derived. :func:`is_docs_only_path` mirrors
the exact ``case`` block in ``ci.yml``'s ``changes`` job byte for byte (same
match order, same patterns) rather than approximating it -- the DELIBERATE
FRICTION the bead calls out is that "the bead is a report, the workflow is
the source of truth". ``tests/scripts/test_check_ci_commit_coverage.py``
pins the EXACT LITERAL TEXT of that case block (not just its order) against
a live parse of ``ci.yml``, so an edit to one without the other reds the
suite instead of silently drifting.

WHY A PORT, NOT A LIVE RE-EXECUTION of ci.yml's own ``run:`` block (this
repo does have that heavier precedent -- ``tests/test_docs_only_predicate_
git_direct.py`` and ``tests/test_dorny_guard_shell_logic.py`` extract a
workflow step's literal script and run it via ``bash -c`` for maximum
fidelity). Considered and rejected here: the real script hardcodes
``git diff "${BEFORE_SHA}" HEAD`` -- to reuse it verbatim per HISTORICAL
commit, "HEAD" would have to genuinely BE that commit, which means checking
out each one into a scratch worktree purely to re-derive a predicate that is
eight ``case`` arms of plain string matching. The exact-text pin above buys
the same "any drift is loud" guarantee without that machinery.

NON-VACUITY (nexus-moht0). This script's dependency is the GitHub Actions
API plus local git history, and CI's own checkout is depth-1 in most jobs
and has no beads database -- exactly the kind of dependency that goes
absent. Absence is never a silent pass: a missing token/repo, an API error,
an *H* whose head_sha is not resolvable/reachable in this checkout, or
exhausting ``--max-runs-scanned`` without ever finding a code-exercised run
all exit 2 (CANNOT VERIFY), never 0. Only "every post-*H* commit is
docs-only" exits 0.

Usage::

    uv run python scripts/check_ci_commit_coverage.py
    uv run python scripts/check_ci_commit_coverage.py --repo Hellblazer/nexus \\
        --branch develop --head <sha>

Exit codes: ``0`` every commit since the last code-exercised run is covered,
``1`` BLOCKED -- one or more uncovered code commits found (names them),
``2`` CANNOT VERIFY (missing token/repo, API/git error, or no code-exercised
run found within the scanned window -- "could not verify" is never "must be
fine").
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

#: Job-name prefixes that only ever run (never `skipped`) when ci.yml's
#: `changes` job resolved code=true -- see the `test`, `test-lint` and
#: `test-mode-census` jobs' own `if: success() && needs.changes.outputs.code
#: == 'true'` guards. Any ONE of these concluding `success` is sufficient
#: proof pytest genuinely executed against that run's tree; a docs-only run
#: reports every one of them `skipped`, never `success`.
CODE_EXERCISED_JOB_PREFIXES: tuple[str, ...] = (
    "pytest (Python",
    "pytest (lint markers)",
    "pytest (mode-declarations census)",
)

_REMEDY = (
    "Remedy: this commit's code was never exercised by pytest on any tree. "
    "If it is still on develop, the next push (even a docs-only one) will "
    "not retroactively fix this -- something must push a commit that "
    "touches code (or re-run ci.yml against this commit's tree directly) so "
    "a real pytest matrix runs over a tree containing it. Do NOT respond by "
    "weakening the doc-only fast lane or the concurrency cancellation: "
    "AGENTS.md's CI Cost Discipline sanctions both by name (nexus-of2x8)."
)


# ── The doc-only predicate, ported from ci.yml's `changes` job ─────────────
#
# Mirrors the case block's match order EXACTLY:
#   docs/*) ;;
#   web/*) ;;
#   conexus/CHANGELOG.md) ;;
#   */*) docs_only=false ;;
#   README.md) ;;
#   CHANGELOG*) ;;
#   LICENSE*) ;;
#   *) docs_only=false ;;
# `tests/scripts/test_check_ci_commit_coverage.py::
# test_predicate_order_matches_ci_yml_case_block` pins this against a live
# parse of ci.yml so an edit to one without the other reds the suite instead
# of silently drifting.


def is_docs_only_path(path: str) -> bool:
    """True iff *path* matches ci.yml's doc-only path set (see module docstring)."""
    if path.startswith("docs/"):
        return True
    if path.startswith("web/"):
        return True
    if path == "conexus/CHANGELOG.md":
        return True
    if "/" in path:
        return False
    if path == "README.md":
        return True
    if path.startswith("CHANGELOG"):
        return True
    if path.startswith("LICENSE"):
        return True
    return False


def is_docs_only_commit(changed_files: tuple[str, ...] | list[str]) -> bool:
    """True iff EVERY file in *changed_files* is doc-only.

    An empty *changed_files* is treated as NOT docs-only (conservative,
    fail-toward-code direction, mirroring ci.yml's own non-vacuity guard on
    an empty diff being a malformed input rather than a silent docs-only
    pass) -- a real commit always changes at least one file, so an empty
    list here means the caller failed to resolve the diff, not that nothing
    changed.
    """
    if not changed_files:
        return False
    return all(is_docs_only_path(f) for f in changed_files)


# ── Coverage model: pure logic over runs + commits, no I/O ─────────────────


@dataclass(frozen=True)
class RunRecord:
    head_sha: str
    jobs: dict[str, str]  # job name -> conclusion ("success", "skipped", "cancelled", ...)


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    changed_files: tuple[str, ...]


def run_is_code_exercised(jobs: dict[str, str]) -> bool:
    """True iff *jobs* proves ci.yml's `changes` job resolved code=true and
    pytest genuinely ran (see :data:`CODE_EXERCISED_JOB_PREFIXES`)."""
    return any(
        conclusion == "success"
        and any(name.startswith(prefix) for prefix in CODE_EXERCISED_JOB_PREFIXES)
        for name, conclusion in jobs.items()
    )


def find_last_code_exercised_run(runs: list[RunRecord]) -> RunRecord | None:
    """The most recent code-exercised run, given *runs* NEWEST-FIRST.

    Returns ``None`` when nothing in *runs* proves code was ever exercised --
    the caller must treat that as CANNOT VERIFY, never as "everything is
    covered" (nexus-moht0: absence of the dependency is not a free pass).
    """
    for run in runs:
        if run_is_code_exercised(run.jobs):
            return run
    return None


def find_uncovered_commits(commits_since_h: list[CommitInfo]) -> list[CommitInfo]:
    """*commits_since_h* are commits strictly newer than the last
    code-exercised run's head (rule (b) already covers everything at or
    before it). Returns those that are NOT docs-only -- nobody has ever run
    pytest against a tree containing them (rule (a) does not apply either).
    """
    return [c for c in commits_since_h if not is_docs_only_commit(c.changed_files)]


# ── GitHub API (thin; the logic above is pure and tested separately) ───────


def _api(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "nexus-check-ci-commit-coverage",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 -- fixed api.github.com host
        return json.loads(resp.read())


def fetch_push_runs(
    repo: str,
    token: str,
    branch: str,
    workflow_file: str,
    max_runs: int,
    api: Callable[[str], dict] | None = None,
) -> list[dict]:
    """Push-triggered *workflow_file* runs on *branch*, newest-first, capped
    at *max_runs* (paginated at 100/page, GitHub's own maximum)."""
    call = api or (lambda u: _api(u, token))
    runs: list[dict] = []
    page = 1
    while len(runs) < max_runs:
        per_page = min(100, max_runs - len(runs))
        url = (
            f"https://api.github.com/repos/{repo}/actions/workflows/"
            f"{urllib.parse.quote(workflow_file, safe='')}/runs"
            f"?branch={urllib.parse.quote(branch, safe='')}&event=push"
            f"&per_page={per_page}&page={page}"
        )
        data = call(url)
        page_runs = list(data.get("workflow_runs") or [])
        runs.extend(page_runs)
        if len(page_runs) < per_page:
            # A short page (or an empty one) is GitHub's own "no more pages"
            # signal -- stop here instead of spending one more call to learn
            # what a short page already told us.
            break
        page += 1
    return runs[:max_runs]


def fetch_run_jobs(
    repo: str, run_id: int, token: str, api: Callable[[str], dict] | None = None
) -> dict[str, str]:
    """``{job name: conclusion}`` for a workflow run, ``per_page=100`` --
    this repo's ci.yml has well under 100 jobs per run (verified 2026-09-22:
    ~12), so one page is enough; a future job count above that would need
    pagination added here, not a silent truncation."""
    call = api or (lambda u: _api(u, token))
    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"
    data = call(url)
    return {j.get("name", ""): j.get("conclusion") for j in (data.get("jobs") or [])}


# ── git (subprocess; the logic above never touches it directly) ────────────


def _git(repo_path: str, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell, args are git subcommands
        ["git", "-C", repo_path, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def git_commit_exists(repo_path: str, sha: str) -> bool:
    result = subprocess.run(  # noqa: S603
        ["git", "-C", repo_path, "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def git_is_ancestor(repo_path: str, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(  # noqa: S603
        ["git", "-C", repo_path, "merge-base", "--is-ancestor", ancestor, descendant],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def git_rev_list_range(repo_path: str, base_sha: str, head_sha: str) -> list[str]:
    """Commits reachable from *head_sha* but not from *base_sha* (``base..head``)."""
    out = _git(repo_path, "rev-list", f"{base_sha}..{head_sha}")
    return [line for line in out.splitlines() if line]


def git_parent_count(repo_path: str, sha: str) -> int:
    out = _git(repo_path, "rev-list", "--parents", "-n", "1", sha)
    return max(0, len(out.split()) - 1)


def git_changed_files(repo_path: str, sha: str) -> tuple[str, ...]:
    """Files *sha* changed relative to its parent.

    A MERGE commit (>1 parent) is treated as touching code unconditionally
    (the conservative, fail-toward-detection direction) rather than diffed
    against a chosen parent -- this repo's own convention is a direct push
    with no merge commits on develop (AGENTS.md Worktrees rule 8), so a
    merge commit showing up here is itself unusual enough to warrant the
    loud path, not a silent diff-against-first-parent guess.

    A ROOT commit (0 parents) is diffed against the empty tree via
    ``--root`` -- it is not a merge, and without ``--root`` `diff-tree`
    would produce nothing for it. In practice this never fires inside
    :func:`check`, since the range checked is always ``H..head`` and *H*
    itself (the coverage floor) is excluded from it -- kept correct anyway
    rather than relying on that never changing.
    """
    parents = git_parent_count(repo_path, sha)
    if parents > 1:
        return (f"<merge commit {sha}: conservatively treated as code>",)
    args = ["diff-tree", "--no-commit-id", "--name-only", "-r"]
    if parents == 0:
        args.append("--root")
    args.append(sha)
    out = _git(repo_path, *args)
    return tuple(line for line in out.splitlines() if line)


# ── Orchestration ───────────────────────────────────────────────────────────


def check(
    repo: str,
    token: str,
    branch: str,
    workflow_file: str,
    head_sha: str,
    repo_path: str,
    max_runs_scanned: int,
    api: Callable[[str], dict] | None = None,
) -> int:
    if not repo or not token:
        print(
            "CANNOT VERIFY: --repo and a token are both required "
            f"(got repo={repo!r} token={'<set>' if token else '<empty>'}). "
            "'could not verify' is never 'must be fine'.",
            file=sys.stderr,
        )
        return 2

    try:
        raw_runs = fetch_push_runs(repo, token, branch, workflow_file, max_runs_scanned, api=api)
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        print(f"CANNOT VERIFY: GitHub API error listing {workflow_file} runs: {exc}", file=sys.stderr)
        return 2

    if not raw_runs:
        print(
            f"CANNOT VERIFY: no push-triggered {workflow_file} runs found on "
            f"{branch!r} in {repo!r} -- either the branch/workflow name is "
            "wrong, or this repo genuinely has no CI history yet.",
            file=sys.stderr,
        )
        return 2

    last_covering_run: RunRecord | None = None
    for raw in raw_runs:
        run_id = raw.get("id")
        candidate_sha = raw.get("head_sha", "")
        if not run_id or not candidate_sha:
            continue
        try:
            jobs = fetch_run_jobs(repo, run_id, token, api=api)
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            print(f"CANNOT VERIFY: GitHub API error fetching jobs for run {run_id}: {exc}", file=sys.stderr)
            return 2
        if not run_is_code_exercised(jobs):
            continue
        if not git_commit_exists(repo_path, candidate_sha):
            print(
                f"note: run {run_id}'s head {candidate_sha} is code-exercised "
                "but not resolvable in this checkout -- skipping as a "
                "coverage floor, looking further back",
                file=sys.stderr,
            )
            continue
        if not git_is_ancestor(repo_path, candidate_sha, head_sha):
            print(
                f"note: run {run_id}'s head {candidate_sha} is code-exercised "
                f"but is not an ancestor of {head_sha} -- skipping as a "
                "coverage floor, looking further back",
                file=sys.stderr,
            )
            continue
        last_covering_run = RunRecord(head_sha=candidate_sha, jobs=jobs)
        break

    if last_covering_run is None:
        print(
            f"CANNOT VERIFY: scanned {len(raw_runs)} push-triggered "
            f"{workflow_file} runs on {branch!r} (max_runs_scanned="
            f"{max_runs_scanned}) and found none that both exercised code "
            f"and is an ancestor of {head_sha} -- absence is a failure to "
            "verify, never evidence that everything is fine. Widen "
            "--max-runs-scanned if this repo has gone that long without a "
            "code-touching push.",
            file=sys.stderr,
        )
        return 2

    try:
        pending_shas = git_rev_list_range(repo_path, last_covering_run.head_sha, head_sha)
    except RuntimeError as exc:
        print(f"CANNOT VERIFY: {exc}", file=sys.stderr)
        return 2

    commits: list[CommitInfo] = []
    try:
        for sha in pending_shas:
            commits.append(CommitInfo(sha=sha, changed_files=git_changed_files(repo_path, sha)))
    except RuntimeError as exc:
        print(f"CANNOT VERIFY: {exc}", file=sys.stderr)
        return 2

    uncovered = find_uncovered_commits(commits)
    if uncovered:
        print(
            f"BLOCKED: {len(uncovered)} commit(s) on {branch!r} between the last "
            f"code-exercised run ({last_covering_run.head_sha}) and {head_sha} "
            "touch code but were never exercised by pytest on any tree:",
            file=sys.stderr,
        )
        for c in uncovered:
            print(f"  - {c.sha}: {', '.join(c.changed_files) or '(no files -- malformed diff)'}", file=sys.stderr)
        print(f"\n{_REMEDY}", file=sys.stderr)
        return 1

    print(
        f"OK: every commit between the last code-exercised run "
        f"({last_covering_run.head_sha}) and {head_sha} on {branch!r} is "
        f"either docs-only or already covered by that run's tree "
        f"({len(pending_shas)} commit(s) checked)."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""), help="owner/name")
    ap.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""), help="token with actions:read")
    ap.add_argument("--branch", default="develop")
    ap.add_argument("--workflow-file", default="ci.yml")
    ap.add_argument(
        "--head",
        default=os.environ.get("GITHUB_SHA", ""),
        help="commit to check coverage up to (default: $GITHUB_SHA, else `git rev-parse HEAD`)",
    )
    ap.add_argument("--repo-path", default=".")
    ap.add_argument(
        "--max-runs-scanned",
        type=int,
        default=100,
        help="how far back to look for a code-exercised run before giving up (CANNOT VERIFY, never a silent pass)",
    )
    args = ap.parse_args(argv)

    head = args.head or _git(args.repo_path, "rev-parse", "HEAD").strip()
    return check(
        repo=args.repo,
        token=args.token,
        branch=args.branch,
        workflow_file=args.workflow_file,
        head_sha=head,
        repo_path=args.repo_path,
        max_runs_scanned=args.max_runs_scanned,
    )


if __name__ == "__main__":
    sys.exit(main())
