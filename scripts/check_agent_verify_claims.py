#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Orchestrator-side checker for checkable agent reports (bead
nexus-cnzei.6 item 3).

RDR-205's ``ledger/<session_id>`` tuple space records THAT an agent
reported. ``tuple_ledger_project.py``'s item-2 extension (bead
nexus-cnzei.6) additionally fills each ``kind=report`` row's optional
``commit`` / ``t2_ref`` / ``verify`` dims by parsing the stopping agent's
own ``VERIFY:`` lines. Neither step checks whether those CLAIMS are
true — a fabricated sha or a real commit that touches nothing the bead
named would sail through both untouched. This script is that check: for
every report row in a session, it confirms a claimed commit actually
exists and touches at least one of the caller-named paths, and it flags
a row that carries no verified claim at all (``verify`` absent or not
``"present"``) as a finding, never a silent pass.

Non-vacuity (nexus-moht0 doctrine, ``AGENTS.md`` "Gates fail loud on
absent dependencies"): a run that examines ZERO report rows for the
named session — a mistyped session id, or a check run before any report
landed — exits 2, distinct from "checked and clean" (exit 0) and
"checked and found something" (exit 1). A gate that reads silence as a
pass is the exact defect this project's own doctrine names.

Usage:
    python3 scripts/check_agent_verify_claims.py <session_id> \\
        [--path REPO_RELATIVE_PATH ...] [--repo PATH] [--json]

``--path`` names the paths the bead under review is expected to touch
(from the design-of-record brief's file:line list, or ``bd show``'s own
notes) — repeatable. A commit claim with no ``--path`` given is checked
for existence only; supply at least one to also check relevance. Omit
entirely to check verify-presence only.

Exit codes:
    0  clean — every row examined carries a checkable, verified claim
       (or, with no ``--path`` given, a commit that at least exists)
    1  findings — at least one row failed a check
    2  non-vacuity failure — zero report rows found for this session id
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Finding:
    agent_id: str
    reason: str

    def __str__(self) -> str:  # pragma: no cover — trivial
        return f"agent_id={self.agent_id or '(unknown)'}: {self.reason}"


@dataclass
class CheckResult:
    examined: int
    findings: list[Finding] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.findings


def _fetch_report_rows(session_id: str) -> list:
    """Every ``ledger/<session_id>`` row with ``kind=report``, via the
    same ``HttpTupleStore`` every other T2 tuple-space caller (the ``nx``
    CLI's ``tuple rd``, the MCP ``tuple_rd`` tool) uses — it resolves its
    own endpoint and credentials, no separate wiring needed here."""
    from nexus.db.t2.http_tuple_store import HttpTupleStore  # noqa: PLC0415 — deferred: CLI-shaped script

    store = HttpTupleStore()
    return store.rd(f"ledger/{session_id}", {"kind": "report"}, n=300)


def _commit_exists(repo: Path, sha: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True, timeout=10,
    ).returncode == 0


def _commit_touched_paths(repo: Path, sha: str) -> set[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), "diff-tree", "--no-commit-id", "--name-only", "-r", sha],
        capture_output=True, text=True, timeout=10,
    )
    if proc.returncode != 0:
        return set()
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def _touches_any(touched: set[str], expected_paths: list[str]) -> bool:
    for expected in expected_paths:
        norm = expected.rstrip("/")
        if any(t == norm or t.startswith(norm + "/") for t in touched):
            return True
    return False


def check(rows: list, *, repo: Path, expected_paths: list[str]) -> CheckResult:
    result = CheckResult(examined=len(rows))
    for row in rows:
        agent_id = row.keys.get("agent_id", "")
        dims = row.dims or {}
        verify = dims.get("verify")
        commit = dims.get("commit")

        if verify != "present":
            result.findings.append(Finding(agent_id, f"verify={verify or 'absent'}"))

        if commit:
            if not _commit_exists(repo, commit):
                result.findings.append(
                    Finding(agent_id, f"commit {commit} does not exist in {repo}")
                )
            elif expected_paths:
                touched = _commit_touched_paths(repo, commit)
                if not _touches_any(touched, expected_paths):
                    result.findings.append(
                        Finding(
                            agent_id,
                            f"commit {commit} touches none of {expected_paths} "
                            f"(touched: {sorted(touched) or '(nothing)'})",
                        )
                    )
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("session_id")
    parser.add_argument(
        "--path", dest="paths", action="append", default=[],
        help="A repo-relative path the bead is expected to touch (repeatable).",
    )
    parser.add_argument(
        "--repo", type=Path, default=_REPO_ROOT,
        help="Repo root to check commits against (default: this checkout).",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv[1:])

    rows = _fetch_report_rows(args.session_id)

    if not rows:
        if args.json:
            print(json.dumps({
                "session_id": args.session_id, "examined": 0,
                "findings": [], "error": "no report rows found for this session id",
            }))
        else:
            print(
                f"NO REPORT ROWS for session_id={args.session_id!r} — nothing was checked. "
                "This is a non-vacuity failure, not a clean pass (nexus-moht0 doctrine): "
                "confirm the session id and that at least one agent has reported.",
                file=sys.stderr,
            )
        return 2

    result = check(rows, repo=args.repo, expected_paths=args.paths)

    if args.json:
        print(json.dumps({
            "session_id": args.session_id,
            "examined": result.examined,
            "findings": [{"agent_id": f.agent_id, "reason": f.reason} for f in result.findings],
        }))
    else:
        print(f"Examined {result.examined} report row(s) for session_id={args.session_id!r}.")
        if result.clean:
            print("CLEAN — every row carries a checkable, verified claim.")
        else:
            print(f"{len(result.findings)} finding(s):")
            for finding in result.findings:
                print(f"  - {finding}")

    return 0 if result.clean else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
