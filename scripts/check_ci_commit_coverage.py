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
docs-only OR already covered" exits 0.

THE RACE THIS SCRIPT'S OWN TRIGGER CREATES (nexus-of2x8 round 2,
2026-09-22, the check's own first live run). This audit is ``on: push`` --
the SAME event that starts ``ci.yml``. Its first real run (35770759535)
started 18 seconds after a push and searched for "the most recent
COMPLETED code-exercised run" while ci.yml's own matrix for that EXACT
push (35770759430) was still ``in_progress``. The search fell back to the
PREVIOUS push's run and reported all four just-pushed commits BLOCKED --
every one of them was, at that instant, being tested by the run in flight.
Reporting them covered (a silent PASS, exit 0) would have been wrong for
the same reason the original defect is wrong: a check that answers a
question it cannot yet answer, in either direction, is not evidence.

ROUND 3 CORRECTION, replacing round 2's fix (kept here because the wrong
turn is instructive, not because it is live): round 2 moved the trigger to
``on: workflow_run`` keyed to ``ci.yml``'s own ``completed`` event, so the
covering run for the push under audit would always have concluded by the
time this script ran. VERIFIED WRONG against GitHub's own documentation
(events-that-trigger-workflows): both ``workflow_run`` and ``schedule``
"will only trigger a workflow run if the workflow file exists on the
default branch" -- and this repo's default branch is ``main``, which only
advances at a release (AGENTS.md's Worktrees section; all routine work
lands on ``develop``). A ``workflow_run``-triggered audit added on
``develop`` would have been DORMANT until the next release promoted it to
``main``, and would then fire using ``main``'s copy of the file, not
``develop``'s -- a silent absence indistinguishable from a check that keeps
passing, which is the nexus-moht0 vacuous-gate class by name, and strictly
worse than the noisy false positive it would have replaced. No working
``workflow_run`` example exists anywhere in this repo's workflows to have
caught that assumption before it shipped.

THE ACTUAL FIX stays on ``on: push`` and narrows the CLAIM instead of
guessing about commits whose evidence does not exist yet. :func:`check`
computes *H* exactly as before (the most recent COMPLETED code-exercised
run) and asserts coverage only over commits it can ALREADY resolve today:
everything at or before *H* (rule (b)), plus any commit after *H* that is
itself docs-only (rule (a), which needs no CI evidence at all). A
code-touching commit after *H* that only an IN-FLIGHT run's tree might
eventually cover is neither -- :func:`check` PRINTS it (the sha, and which
in-flight run might resolve it) rather than staying silent about it, and
does not fold it into the exit code. Exit 0 means "everything this
invocation actually examined is clean", a narrower, always-true-or-loud
claim rather than a guess in either direction (this is the "a check's
domain must contain its claim" principle -- see the
``feedback_a_checks_domain_must_contain_the_claim`` T2 record). Genuinely
lost coverage -- a commit covered by NOTHING, not even something in flight
-- still reports BLOCKED (exit 1) immediately and takes priority; a
completed run whose pytest jobs SKIPPED is not in flight (it already
concluded) and can never excuse a code commit into the out-of-scope tail
-- see :func:`classify_pending_commits` and the priority order in
:func:`check`.

THE RESIDUAL GAP, stated rather than hidden: if develop goes quiet
immediately after a run is cancelled, nothing re-audits, and the
out-of-scope tail stays unexamined indefinitely -- it is PRINTED every
run, so it is visible in the log rather than lost, but visible-in-a-log is
weaker than a red check, and a human has to go looking for it rather than
being told. ``workflow_dispatch`` (declared below, and unlike
``workflow_run``/``schedule`` it DOES work from any branch per the same
GitHub documentation) lets someone manually re-run this audit against the
same head once the in-flight run concludes, closing the gap on demand
rather than automatically. See the module's own report for this repo's
assessment of how much that closes versus a genuinely automatic re-check.

Usage::

    uv run python scripts/check_ci_commit_coverage.py
    uv run python scripts/check_ci_commit_coverage.py --repo Hellblazer/nexus \\
        --branch develop --head <sha>

Exit codes: ``0`` every commit this invocation could actually examine
(everything up to the last completed code-exercised run, plus any
docs-only or otherwise-resolvable commits after it) is covered -- any
commit left unresolved because its only potential covering run has not
concluded is PRINTED, not silently passed, and does not affect this exit
code; ``1`` BLOCKED -- one or more commits touch code and are covered by
nothing at all, not even an in-flight run (names them; takes priority over
the out-of-scope tail in the same window); ``2`` CANNOT VERIFY -- the
absent-dependency cases only (missing token/repo, API/git error, no
completed code-exercised run found within the scanned window, a scanned
window that does not reach the audited head, or a completed run whose jobs
could not be read) -- "could not verify" is never "must be fine", but an
in-flight covering run is no longer one of these cases (see ROUND 3
CORRECTION above).

ROUND 4, the unreadable window (2026-09-23, run 35862641493). The check
reported 1257 commits BLOCKED; a rerun over the IDENTICAL head reported 97,
from a different floor. Two different answers to one question is a
malfunction whichever is nearer the truth, and both were false: the same
script, same API, same head, run from a workstation chose the correct floor
two commits back every time. The loop had two silent paths that could
produce a wrong floor without saying so -- a completed run whose jobs came
back empty was indistinguishable from one that skipped pytest, and a run
rejected as not-code-exercised printed nothing at all. Both are now loud:
empty jobs is CANNOT VERIFY, and every rejection prints the pytest job
conclusions it actually saw. :func:`window_reaches_head` adds the guard that
makes a wrong floor unreachable rather than merely diagnosable, by checking
the one thing that must be true of a window that reaches the present.

STALE WINDOWS, measured (2026-09-23). The cause is now known and it is not
this script's: GitHub intermittently serves a stale cached page for this
repo's ``ci.yml`` run list. 25 consecutive identical requests returned 23
current windows and 2 stale ones, and both stale samples were the SAME page,
newest run 2026-09-07T10:04:00Z, against a repo whose newest run was that
day's. A first hypothesis that page size selected the behaviour was
falsified within the minute: ``per_page=3`` returned the stale window while
``per_page=100`` returned the current one, the exact inverse of the pairing
that suggested it. So it is one cached page served at roughly 8% of
requests, independent of ``per_page``, and a retry is the remedy rather than
a different query. :data:`WINDOW_FETCH_ATTEMPTS` refetches before believing a
short window, and each stale observation is logged rather than swallowed, so
the phenomenon stays visible if its rate changes.
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

#: How many times to fetch the run list before believing a window that does
#: not reach the audited head. See STALE WINDOWS in the module docstring: the
#: measured per-request rate is about 8%, so three attempts takes a false
#: CANNOT VERIFY from roughly 1 push in 12 to roughly 1 in 2000. Raising this
#: buys very little and delays a genuine refusal; lowering it to 1 restores
#: the noisy behaviour.
WINDOW_FETCH_ATTEMPTS: int = 3

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
    jobs: dict[
        str, str
    ]  # job name -> conclusion ("success", "skipped", "cancelled", ...)


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


def window_reaches_head(raw_runs: list[dict], head_sha: str) -> bool:
    """True iff *raw_runs* contains a run for *head_sha* itself.

    THE SANITY CHECK THE ANCHOR SEARCH LACKED. :func:`check` treats the
    scanned window as "the recent runs" and walks it for a coverage floor.
    Nothing verified that assumption, so a window that did not actually
    reach the present still produced a floor -- just a very old one, with
    every commit since reported BLOCKED. Measured 2026-09-23 on run
    35862641493: the first invocation chose a floor 1257 commits back, the
    rerun over the IDENTICAL head chose a different one 97 commits back,
    while the same script against the same API from a workstation chose the
    correct floor two commits back every time.

    The invariant that makes this checkable: this audit is push-triggered on
    the same branch as ``ci.yml``, so the push being audited necessarily
    started a ``ci.yml`` run for this exact sha. That run must be in any
    window that genuinely reaches the present. Its absence is decisive
    evidence about the WINDOW, which is why the caller returns CANNOT VERIFY
    rather than picking a floor out of it.
    """
    return any(r.get("head_sha") == head_sha for r in raw_runs)


def run_is_in_flight(raw_run: dict) -> bool:
    """True iff *raw_run* (a raw GitHub Actions run dict) has not yet
    concluded. ``status`` is one of ``queued``, ``in_progress``, ``waiting``,
    ``requested``, ``pending`` or ``completed`` -- everything except
    ``completed`` means the run's verdict does not exist yet, regardless of
    what ``conclusion`` currently reads (GitHub leaves it ``null`` until the
    run finishes)."""
    return raw_run.get("status") != "completed"


def classify_pending_commits(
    commits_since_h: list[CommitInfo],
    in_flight_head_shas: list[str],
    is_ancestor_or_equal: Callable[[str, str], bool],
) -> tuple[list[CommitInfo], list[CommitInfo]]:
    """Split the code-touching commits in *commits_since_h* into
    ``(blocked, pending)``.

    A commit already excluded by :func:`find_uncovered_commits` (docs-only)
    is in neither list -- it needs no run at all. Of the remainder: PENDING
    is a commit that is an ancestor of (or equal to) some run in
    *in_flight_head_shas* -- a run that has not concluded, so the verdict
    for this commit does not exist yet (CANNOT VERIFY, never a silent pass
    -- see the module docstring's THE RACE section). BLOCKED is everything
    else: covered by nothing at all, not even a run in progress.

    *is_ancestor_or_equal* is injected (real git in :func:`check`, a fake
    in tests) so this classification is unit-testable without a git
    subprocess.
    """
    uncovered = find_uncovered_commits(commits_since_h)
    blocked: list[CommitInfo] = []
    pending: list[CommitInfo] = []
    for c in uncovered:
        if any(is_ancestor_or_equal(c.sha, ihs) for ihs in in_flight_head_shas):
            pending.append(c)
        else:
            blocked.append(c)
    return blocked, pending


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


def _emit_out_of_scope_annotation(
    pending: "list[CommitInfo]", in_flight_head_shas: "list[str]"
) -> None:
    """Raise the out-of-scope note from the log body to a run ANNOTATION.

    THE GAP THIS NARROWS, and it does not close it. CANNOT-VERIFY /
    out-of-scope exists in this script's logic and did not exist in its
    SIGNAL: the note prints inside a run that concludes GREEN, and nobody
    reads the log of a green run. So the third outcome degraded to "pass"
    at exactly the moment it matters — develop going quiet after a
    cancelled run, with nobody revisiting.

    A `::warning::` workflow command renders as an annotation on the run
    summary and on the commit's checks UI, which is visible WITHOUT opening
    the log and without failing anything. That is the one surface GitHub
    Actions offers between "log line" and "red check".

    Why not a red check instead: a red that fires on the routine
    back-to-back-push case is a red people learn to ignore, and an ignored
    red is worse than an honest annotation. Why not `neutral`: a job's
    conclusion is success/failure/cancelled/skipped — Actions gives a
    normal job no neutral conclusion to return.

    Emitted only under GITHUB_ACTIONS so a local or `uv run` invocation
    prints clean text. Annotations are capped by GitHub per run, so this
    emits ONE covering the whole set rather than one per commit.
    """
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    shas = ", ".join(c.sha[:12] for c in pending)
    heads = ", ".join(sorted(set(s[:12] for s in in_flight_head_shas)))
    print(
        f"::warning title=Commit coverage not yet determinable::"
        f"{len(pending)} commit(s) touching code are OUT OF SCOPE for this audit "
        f"({shas}). Their only potential covering run has not concluded "
        f"(in-flight: {heads}). This run's GREEN verdict excludes them. "
        f"If that run is cancelled and develop goes quiet, re-run this audit "
        f"via workflow_dispatch rather than assuming they were covered."
    )


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

    raw_runs: list[dict] = []
    for attempt in range(1, WINDOW_FETCH_ATTEMPTS + 1):
        try:
            raw_runs = fetch_push_runs(
                repo, token, branch, workflow_file, max_runs_scanned, api=api
            )
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            print(
                f"CANNOT VERIFY: GitHub API error listing {workflow_file} runs: {exc}",
                file=sys.stderr,
            )
            return 2

        if not raw_runs:
            print(
                f"CANNOT VERIFY: no push-triggered {workflow_file} runs found on "
                f"{branch!r} in {repo!r} -- either the branch/workflow name is "
                "wrong, or this repo genuinely has no CI history yet.",
                file=sys.stderr,
            )
            return 2

        if window_reaches_head(raw_runs, head_sha):
            break
        print(
            f"note: attempt {attempt}/{WINDOW_FETCH_ATTEMPTS} got a window of "
            f"{len(raw_runs)} run(s) whose newest is for "
            f"{raw_runs[0].get('head_sha', '<none>')}, not reaching the "
            f"audited head {head_sha} -- refetching (see STALE WINDOWS).",
            file=sys.stderr,
        )
    else:
        newest = raw_runs[0].get("head_sha", "<none>")
        print(
            f"CANNOT VERIFY: {WINDOW_FETCH_ATTEMPTS} fetches of the "
            f"{workflow_file} run list all returned a window that does not "
            f"contain a run for {head_sha}, the very commit being audited "
            f"(newest in the last window is for {newest}). Every push to "
            f"{branch!r} starts a {workflow_file} run for that same sha, so "
            "the head's own run missing from every window means this is not "
            "the intermittent stale page the retry exists for. Choosing a "
            "coverage floor from it would name a commit hundreds of pushes "
            "back and report everything since as BLOCKED, which is a false "
            "alarm, not a finding.",
            file=sys.stderr,
        )
        return 2

    last_covering_run: RunRecord | None = None
    in_flight_head_shas: list[str] = []
    for raw in raw_runs:
        run_id = raw.get("id")
        candidate_sha = raw.get("head_sha", "")
        if not run_id or not candidate_sha:
            continue

        if run_is_in_flight(raw):
            # Not yet concluded -- its own verdict does not exist yet, so it
            # can never become H (last_covering_run), but it CAN mean a
            # commit reachable from it is merely PENDING rather than
            # BLOCKED (see the module docstring's THE RACE section). Record
            # it and keep walking further back for a genuine completed H --
            # do not stop the search here.
            if git_commit_exists(repo_path, candidate_sha):
                in_flight_head_shas.append(candidate_sha)
            else:
                print(
                    f"note: in-flight run {run_id}'s head {candidate_sha} is "
                    "not resolvable in this checkout -- not counted as a "
                    "pending-coverage candidate",
                    file=sys.stderr,
                )
            continue

        try:
            jobs = fetch_run_jobs(repo, run_id, token, api=api)
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            print(
                f"CANNOT VERIFY: GitHub API error fetching jobs for run {run_id}: {exc}",
                file=sys.stderr,
            )
            return 2
        if not jobs:
            # An absent dependency, not a verdict. A completed run ALWAYS
            # has jobs; an empty mapping means the jobs endpoint gave this
            # invocation nothing -- a permission it lacks, a truncated
            # response, an eventual-consistency gap. Walking past it
            # silently is how the anchor search ended up hundreds of pushes
            # back (nexus-moht0: absence of the dependency is never a pass,
            # and it is not a "this run did not exercise code" either).
            print(
                f"CANNOT VERIFY: the jobs endpoint returned no jobs for "
                f"completed run {run_id} (head {candidate_sha}). A completed "
                "run always has jobs, so this is a failure to read the "
                "evidence, not evidence that the run skipped pytest.",
                file=sys.stderr,
            )
            return 2
        if not run_is_code_exercised(jobs):
            # Say WHY, so a wrong floor can be diagnosed from the log
            # instead of reproduced. The first version of this loop was
            # silent here, which is why two CI invocations that chose two
            # different ancient floors left nothing to read.
            pytest_jobs = {
                name: conclusion
                for name, conclusion in jobs.items()
                if any(name.startswith(p) for p in CODE_EXERCISED_JOB_PREFIXES)
            }
            detail = (
                ", ".join(f"{n}={c}" for n, c in sorted(pytest_jobs.items()))
                if pytest_jobs
                else f"no pytest-prefixed jobs among {len(jobs)} job(s)"
            )
            print(
                f"note: run {run_id} ({candidate_sha}) did not exercise code "
                f"-- {detail}",
                file=sys.stderr,
            )
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
        pending_shas = git_rev_list_range(
            repo_path, last_covering_run.head_sha, head_sha
        )
    except RuntimeError as exc:
        print(f"CANNOT VERIFY: {exc}", file=sys.stderr)
        return 2

    commits: list[CommitInfo] = []
    try:
        for sha in pending_shas:
            commits.append(
                CommitInfo(sha=sha, changed_files=git_changed_files(repo_path, sha))
            )
    except RuntimeError as exc:
        print(f"CANNOT VERIFY: {exc}", file=sys.stderr)
        return 2

    blocked, pending = classify_pending_commits(
        commits,
        in_flight_head_shas,
        is_ancestor_or_equal=lambda c, r: git_is_ancestor(repo_path, c, r),
    )

    if blocked:
        print(
            f"BLOCKED: {len(blocked)} commit(s) on {branch!r} between the last "
            f"code-exercised run ({last_covering_run.head_sha}) and {head_sha} "
            "touch code and are covered by NOTHING -- not a completed run, "
            "not even one still in progress:",
            file=sys.stderr,
        )
        for c in blocked:
            print(
                f"  - {c.sha}: {', '.join(c.changed_files) or '(no files -- malformed diff)'}",
                file=sys.stderr,
            )
        print(f"\n{_REMEDY}", file=sys.stderr)
        return 1

    audited_count = len(pending_shas) - len(pending)
    if pending:
        # OUT OF SCOPE, not CANNOT VERIFY (round 3 correction -- see the
        # module docstring). This is printed to STDOUT, not stderr: it is
        # not a problem the exit code reflects, so it must not read like one
        # to a caller that only checks stderr for trouble. The claim this
        # invocation makes narrows to exclude these shas rather than
        # guessing about them in either direction.
        print(
            f"NOTE: {len(pending)} commit(s) on {branch!r} touch code and are "
            "OUT OF SCOPE for this invocation -- their only potential "
            "covering run has not concluded yet (in-flight head(s): "
            f"{', '.join(sorted(set(in_flight_head_shas)))}). This is not a "
            "pass on these commits and not a failure of this run -- the "
            "claim below is scoped to exclude them. The next push's audit "
            "re-derives from scratch and will report them BLOCKED for real "
            "if the in-flight run turns out to have been cancelled with "
            "nothing else covering them, or resolve them into the covered "
            "set if it succeeds. If develop goes quiet before another push "
            "arrives, re-run this audit manually via workflow_dispatch once "
            "the in-flight run concludes to close this out sooner:"
        )
        for c in pending:
            print(
                f"  - {c.sha}: {', '.join(c.changed_files) or '(no files -- malformed diff)'}"
            )
        print()
        _emit_out_of_scope_annotation(pending, in_flight_head_shas)

    print(
        f"OK: every commit this invocation could examine, between the last "
        f"code-exercised run ({last_covering_run.head_sha}) and {head_sha} on "
        f"{branch!r}, is either docs-only or already covered by that run's "
        f"tree ({audited_count}/{len(pending_shas)} commit(s) audited"
        f"{f', {len(pending)} left out of scope' if pending else ''})."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--repo", default=os.environ.get("GITHUB_REPOSITORY", ""), help="owner/name"
    )
    ap.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN", ""),
        help="token with actions:read",
    )
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
