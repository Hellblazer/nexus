# SPDX-License-Identifier: AGPL-3.0-or-later
"""Decide whether ci.yml's native-build trip-wire (``engine-service-build``) runs for this change.

Suite audit item 8 (Sam, 2026-11-19, T2 nexus/sam-ruling-suite-audit-and-cleanup-2026-11-19): the trip-wire used to
start a runner on every push and gate its steps with dorny/paths-filter, whose unset ``base`` compares a develop push
with main, so ``svc`` was always true and the native build ran on every push (about 4.7 min). This decides once, in
the ``changes`` job, and the trip-wire's job-level ``if:`` skips the whole job on ``false``.

``false`` needs two proofs; anything less answers ``true``:

1. The range touches none of :data:`INPUT_PREFIXES`. A pull request diffs from its merge base
   (``base...HEAD``); a push diffs from the branch's previous tip (``before..HEAD``).
2. On a push, the base itself is covered: its own ``engine-service-build`` check run concluded ``success`` or
   ``skipped``, or is still ``queued`` or ``in_progress`` (that run's verdict stands for its commits). develop runs two
   CI runs at a time and a newer push REPLACES a pending one, so the replaced run's commits sit in no later
   ``before..HEAD`` range; its trip-wire shows as cancelled or absent, the base reads uncovered, and this run builds.
   A ``skipped`` base is covered because its own decision required the same two proofs, back to a built commit:
   the job's ``if:`` builds whenever the ``changes`` job did not succeed, so ``skipped`` comes only from this script
   answering ``false``, never from a failed ``needs``. A base that also carries a failed run is uncovered.

An unusable base (all zeros, empty, not in the checkout), a failed check-run read, or a base whose run failed,
timed out or was cancelled answers ``true``. One residual is accepted: a base whose run is still queued or running
counts as covered, so if that run later fails, clean pushes made meanwhile have already skipped; the failed commit
stays red on its own run, and the next push that changes an input builds. An empty changed-file list between two good commits is a broken range,
not "nothing changed": exit 1, never a skip (the PR #1464 class, ci.yml ``changes`` job).

Standard library only: it runs under the runner's bare python3.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Iterable

#: The trip-wire's inputs: what its native build reads, plus this gate. A path matches when it starts with one.
INPUT_PREFIXES: tuple[str, ...] = ("service/", ".github/workflows/ci.yml", ".github/actions/maven-cache/",
                                   "scripts/native_trip_wire_svc.py", "scripts/check_native_embedded_resources.py")
#: The check-run name of the job this gates (ci.yml ``engine-service-build``'s ``name:``).
TRIP_WIRE_CHECK: str = "engine-service native build (PR trip-wire)"
ZERO_SHA: str = "0" * 40
#: A base run counts as covering its commits when it reached one of these, or is still running.
_COVERED_CONCLUSIONS = frozenset({"success", "skipped"})
#: A base run that reached one of these leaves its commits uncovered, whatever another run on the same sha says.
_FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "action_required", "startup_failure", "stale"})
_RUNNING = frozenset({"queued", "in_progress", "waiting", "pending", "requested"})

Lookup = Callable[[str], Iterable[tuple[str, "str | None"]]]


class MalformedRange(Exception):
    """Two good commits with no changed file between them: the diff is broken, not empty."""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def _reachable(repo: Path, sha: str) -> bool:
    if not sha or sha == ZERO_SHA:
        return False
    return subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
                          capture_output=True).returncode == 0


def check_runs_lookup(repository: str, token: str, api: str = "https://api.github.com") -> Lookup:
    """``(status, conclusion)`` of every check run named :data:`TRIP_WIRE_CHECK` on a commit (GitHub REST)."""
    def lookup(sha: str) -> list[tuple[str, str | None]]:
        q = urllib.parse.urlencode({"check_name": TRIP_WIRE_CHECK, "per_page": "100"})
        req = urllib.request.Request(f"{api}/repos/{repository}/commits/{sha}/check-runs?{q}",
                                     headers={"Authorization": f"Bearer {token}",
                                              "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - the GitHub API
            body = json.load(resp)
        return [(r.get("status") or "", r.get("conclusion")) for r in body.get("check_runs", [])]
    return lookup


def decide(*, repo: Path, event: str, before: str = "", base: str = "", lookup: Lookup | None) -> tuple[bool, str]:
    """``(svc, reason)``. Raises :class:`MalformedRange` on an empty diff between two good commits."""
    if event == "pull_request":
        if not _reachable(repo, base):
            return True, f"pull request base {base!r} is not usable here, so the change range is unknown"
        rng = [f"{base}...HEAD"]
    else:
        if not _reachable(repo, before):
            return True, f"push base {before!r} is not usable here (a first push or a rewritten tip)"
        rng = [before, "HEAD"]
    # --no-renames: a rename's source path counts too (moving a file out of service/ changes the build);
    # -z: no core.quotepath quoting, so a non-ASCII path still starts with its prefix.
    changed = [p for p in _git(repo, "diff", "--no-renames", "--name-only", "-z", *rng).split("\0") if p]
    if not changed:
        raise MalformedRange(f"no changed file between {' '.join(rng)}: the range is broken, not empty")
    hits = [p for p in changed if any(p.startswith(x) for x in INPUT_PREFIXES)]
    if hits:
        return True, f"{len(hits)} build input(s) changed: {', '.join(hits[:5])}"
    if event == "pull_request":
        return False, f"no build input among {len(changed)} changed file(s) since the merge base"
    try:
        runs = list(lookup(before)) if lookup is not None else []
    except Exception as exc:  # noqa: BLE001 - any failure to read coverage answers "run"
        return True, f"could not read the base's {TRIP_WIRE_CHECK!r} check run ({type(exc).__name__}); building"
    failed = [c for _s, c in runs if c in _FAILED_CONCLUSIONS]
    if not failed and any(c in _COVERED_CONCLUSIONS or s in _RUNNING for s, c in runs):
        return False, (f"no build input among {len(changed)} changed file(s), and the base {before[:12]}'s trip-wire "
                       "covers its own commits")
    seen = ", ".join(f"{s}/{c}" for s, c in runs) or "no run"
    return True, (f"base {before[:12]} is not covered ({seen}): a replaced or failed run built nothing for its "
                  "commits, so this run builds")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="native_trip_wire_svc.py")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--event", required=True)
    ap.add_argument("--before", default="")
    ap.add_argument("--base", default="")
    ap.add_argument("--output", required=True, help="file to append svc=true|false to ($GITHUB_OUTPUT)")
    ap.add_argument("--no-lookup-for-test", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    lookup: Lookup | None = None
    if not a.no_lookup_for_test:
        token, repository = os.environ.get("GH_TOKEN", ""), os.environ.get("GITHUB_REPOSITORY", "")
        if token and repository:
            lookup = check_runs_lookup(repository, token)
    try:
        svc, why = decide(repo=Path(a.repo), event=a.event, before=a.before, base=a.base, lookup=lookup)
    except MalformedRange as exc:
        sys.stdout.write(f"::error::{exc}\n")
        return 1
    sys.stdout.write(f"native trip-wire: svc={'true' if svc else 'false'}: {why}\n")
    with open(a.output, "a", encoding="utf-8") as f:
        f.write(f"svc={'true' if svc else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
