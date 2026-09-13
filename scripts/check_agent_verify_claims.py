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
true — a fabricated sha, a real commit that touches nothing the bead
named, or a ``t2=`` pointer to an entry that was never written would sail
through both untouched. This script is that check: for every report row
in a session, it confirms a claimed commit actually exists and touches at
least one of the caller-named paths, confirms a claimed ``t2_ref`` exists
in T2, and flags a row that carries no verified claim at all (``verify``
absent or not ``"present"``) as a finding, never a silent pass.

WHAT THIS DOES NOT CHECK (fix round 1, coordinator's ask): whether a
claimed command actually ran, or ran successfully, or was the RIGHT
command to run for the change under review. A ``VERIFY: <command> => rc=0
N passed`` line is read as evidence a VERIFY block exists (feeding the
``verify`` dim's present/absent state) but this script has no way to
re-derive "was N the right count" or "did rc=0 really happen" from the
ledger alone — that is the orchestrator's own job, reading the command
line and re-running it, per ``conexus/skills/orchestration/SKILL.md``
"VERIFY Line Convention".

UNVERIFIABLE ENGINES (fix round 1, critic Critical 2). The three dims
this checker reads ship in engine-service-v0.1.118 (nexus-d9k5h); a
below-floor engine's ``tuple_ledger_project.py`` write path strips them
from EVERY report row (the HTTP-400 schema-fallback), so every
well-behaved report from such an engine would otherwise read as
``verify=absent`` — a false finding on 100% of good reports, not a real
one. Before checking anything, this script asks the CONNECTED ENGINE's
own tuple template registry whether it declares commit/t2_ref/verify at
all (never assumes from this repo's checked-out ``ledger.yaml``, which
can be ahead of the engine actually running). If it does not, the run
reports UNVERIFIABLE and exits 3 — no findings, because there is nothing
here to distrust; the absence is structural, not evidence of a bad
report.

Non-vacuity (nexus-moht0 doctrine, ``AGENTS.md`` "Gates fail loud on
absent dependencies"): a run that examines ZERO report rows for the
named session — a mistyped session id, or a check run before any report
landed — exits 2, distinct from "checked and clean" (exit 0) and
"checked and found something" (exit 1). A gate that reads silence as a
pass is the exact defect this project's own doctrine names. This is
checked only AFTER the engine is confirmed to declare the dims — an
UNVERIFIABLE engine short-circuits before any row fetch, so exit 3 never
collides with exit 2's meaning.

Usage:
    python3 scripts/check_agent_verify_claims.py <session_id> \\
        [--path REPO_RELATIVE_PATH ...] [--repo PATH] [--json]

``--path`` names the paths the bead under review is expected to touch
(from the design-of-record brief's file:line list, or ``bd show``'s own
notes) — repeatable. A commit claim with no ``--path`` given is checked
for existence only; supply at least one to also check relevance. The
CLEAN message says explicitly which of the two it did (fix round 1,
critic Significant b) — omitting ``--path`` silently narrows the check,
and a caller reading only "CLEAN" must not mistake that for "and it
touched the right files."

Exit codes:
    0  clean — every row examined carries a checkable, verified claim
       (or, with no ``--path`` given, a commit that at least exists)
    1  findings — at least one row failed a check
    2  non-vacuity failure — zero report rows found for this session id
    3  unverifiable — the connected engine's ledger template does not
       declare commit/t2_ref/verify yet; nothing here was checked
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_NON_VACUITY = 2
EXIT_UNVERIFIABLE = 3

#: The literal template name as declared in
#: service/src/main/resources/tuples/templates/ledger.yaml — a template
#: pattern name, never substituted per-session.
_LEDGER_TEMPLATE_NAME = "ledger/<session_id>"

#: bead nexus-d9k5h's three dims. All three ship together in one engine
#: bump, so checking any would signal the same floor crossing; checking
#: all three is the same predicate this checker's OWN reads depend on.
_REQUIRED_VERIFY_DIMS = frozenset({"commit", "t2_ref", "verify"})


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


def _make_store() -> Any:
    """The same ``HttpTupleStore`` every other T2 tuple-space caller (the
    ``nx`` CLI's ``tuple rd``/``tuple templates``, the MCP ``tuple_rd``
    tool) uses — it resolves its own endpoint and credentials, no
    separate wiring needed here."""
    from nexus.db.t2.http_tuple_store import HttpTupleStore  # noqa: PLC0415 — deferred: CLI-shaped script

    return HttpTupleStore()


def _make_memory_store() -> Any:
    """The same client path ``memory_get``'s own ``db.resolve_title``
    uses underneath (``nexus.mcp.core.memory_get`` -> ``T2Database`` ->
    this store) — constructed directly here, the same way ``_make_store``
    constructs ``HttpTupleStore`` directly, rather than pulling in the
    MCP session-context glue this standalone script has no use for."""
    from nexus.db.t2.http_memory_store import HttpMemoryStore  # noqa: PLC0415 — deferred: CLI-shaped script

    return HttpMemoryStore()


def _declared_ledger_dims(store: Any) -> set[str]:
    """The dimension names the CONNECTED ENGINE's ledger template
    currently declares, straight from its own template registry — never
    assumed from this repo's checked-out ``ledger.yaml``, which can be
    ahead of a running below-floor engine (fix round 1, critic
    Critical 2). Returns an empty set if the ledger template cannot be
    found in the registry response at all (should not happen -- RDR-205
    ships it unconditionally -- but a checker must not crash on a
    malformed registry response)."""
    registry = store.registry()
    templates = registry.get("templates") if isinstance(registry, dict) else None
    if not isinstance(templates, list):
        return set()
    for t in templates:
        if isinstance(t, dict) and t.get("name") == _LEDGER_TEMPLATE_NAME:
            dims = t.get("dimensions")
            return set(dims) if isinstance(dims, dict) else set()
    return set()


def _fetch_report_rows(store: Any, session_id: str) -> list:
    """Every ``ledger/<session_id>`` row with ``kind=report``."""
    return store.rd(f"ledger/{session_id}", {"kind": "report"}, n=300)


def _commit_exists(repo: Path, sha: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True, timeout=10,
    ).returncode == 0


def _commit_touched_paths(repo: Path, sha: str) -> set[str]:
    """Paths *sha* changed relative to its FIRST PARENT — correct for
    both plain and merge commits (fix round 1, CRE finding 1, measured
    2026-09-13 live: ``git diff-tree --no-commit-id --name-only -r <sha>``
    with no ``-m``/``-c`` returns EMPTY for a real ``--no-ff`` merge
    commit, which would falsely convict a true "commit touches path X"
    claim whenever the landed commit happened to be a merge — a false
    POSITIVE in the direction opposite this tool's purpose).

    ``git diff --name-only <sha>^1 <sha>`` is the first-parent comparison
    directly, and gives the IDENTICAL result to plain ``diff-tree`` for a
    non-merge commit too (verified), so one code path covers both shapes
    without branching on commit kind. Falls back to the root-commit form
    (plain ``diff-tree``, which diffs against the empty tree when a
    commit has no parent) when ``<sha>^1`` does not resolve — the very
    first commit in a repo's history, vanishingly unlikely for a real
    bead commit but cheap to handle correctly.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-only", f"{sha}^1", sha],
        capture_output=True, text=True, timeout=10,
    )
    if proc.returncode != 0:
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


def _t2_entry_exists(memory_store: Any, t2_ref: str) -> bool:
    """True iff *t2_ref* (``project/title``) resolves to a real T2 entry,
    via the same exact-then-prefix ``resolve_title`` path ``memory_get``
    itself uses. An ambiguous prefix (multiple candidates, no unique
    entry) does NOT count as existing -- the claim named one specific
    entry, and ambiguity means this checker cannot confirm THAT one."""
    if "/" not in t2_ref:
        return False
    project, title = t2_ref.split("/", 1)
    if not project or not title:
        return False
    entry, _candidates = memory_store.resolve_title(project=project, title=title)
    return entry is not None


def check(
    rows: list, *, repo: Path, expected_paths: list[str], memory_store: Any = None,
) -> CheckResult:
    result = CheckResult(examined=len(rows))
    for row in rows:
        agent_id = row.keys.get("agent_id", "")
        dims = row.dims or {}
        verify = dims.get("verify")
        commit = dims.get("commit")
        t2_ref = dims.get("t2_ref")

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

        if t2_ref and memory_store is not None:
            if not _t2_entry_exists(memory_store, t2_ref):
                result.findings.append(Finding(agent_id, f"t2_ref {t2_ref!r} not found in T2"))
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
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

    store = _make_store()
    declared = _declared_ledger_dims(store)
    missing = _REQUIRED_VERIFY_DIMS - declared

    if missing:
        reason = (
            "the connected engine's ledger template does not declare "
            f"{sorted(missing)} yet (below engine-service-v0.1.118, nexus-d9k5h) "
            "-- every report row from it structurally lacks these dims, so this "
            "is not evidence of a bad report, and nothing was checked"
        )
        if args.json:
            print(json.dumps({
                "session_id": args.session_id, "unverifiable": True, "reason": reason,
            }))
        else:
            print(f"UNVERIFIABLE: {reason}", file=sys.stderr)
        return EXIT_UNVERIFIABLE

    rows = _fetch_report_rows(store, args.session_id)

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
        return EXIT_NON_VACUITY

    memory_store = _make_memory_store()
    result = check(rows, repo=args.repo, expected_paths=args.paths, memory_store=memory_store)

    if args.json:
        print(json.dumps({
            "session_id": args.session_id,
            "examined": result.examined,
            "path_scope": "existence+relevance" if args.paths else "existence-only",
            "findings": [{"agent_id": f.agent_id, "reason": f.reason} for f in result.findings],
        }))
    else:
        print(f"Examined {result.examined} report row(s) for session_id={args.session_id!r}.")
        if result.clean:
            if args.paths:
                print("CLEAN — every row carries a checkable, verified claim.")
            else:
                print(
                    "CLEAN — commit existence checked only; no --path given, so "
                    "relevance (whether any commit touched what the bead names) "
                    "was NOT checked. Pass --path to also check that."
                )
        else:
            print(f"{len(result.findings)} finding(s):")
            for finding in result.findings:
                print(f"  - {finding}")

    return EXIT_CLEAN if result.clean else EXIT_FINDINGS


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
