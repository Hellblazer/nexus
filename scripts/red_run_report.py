#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Red-run report over a snapshot of develop CI runs (RDR-258 Phase 0 step 2, nexus-2jomg.1).

    red_run_report.py extract --snapshot DIR --repo . --out scripts/red-run-extraction-2026-09-23.jsonl.gz
    red_run_report.py report  --extraction FILE [--top 15] [--repo .]
    red_run_report.py mvv     --extraction FILE

``extract`` reads the Phase 0 step 1 snapshot (``runs.jsonl``, ``jobs/<run>.json``,
``logs/<run>/<job>.log``) and the git history, and writes one compressed jsonl file with four
record kinds: ``meta``, ``run`` (one per red run, with its fix range), ``item`` (one per failing
item: run, sha, workflow, job, venue, failing test or step, fix signal) and ``commit`` (the files
and subject of each commit in any fix range). ``report`` and ``mvv`` read only that file, so they
need no snapshot and no git; the commit-mix figures are the one exception and read git when
``--repo`` is given.

Scope. A "red run" is a run whose conclusion is in ``--conclusions`` (default ``failure``) and,
unless ``--include-jobless``, that has at least one failed job record. On the 2026-09-23..11-23
snapshot ``failure`` gives 789 runs and 788 with the jobless one dropped, against strand B's 787
(258-research-1); ``cancelled`` (440) and ``startup_failure`` (1) are not red runs. Strand B's own
scope is not recorded, so the option exists to make the choice explicit, not to fit the figure.

Fix signal. The next green run of the same workflow ends the red episode; the commits in
``<red sha>..<green sha>`` are the fix range. For an item: ``touched_test`` is whether a commit in
the range touched the failing test's file, ``fix_touched_product`` whether one of the commits that
touched it also touched ``src/`` or ``service/src/main/`` (the 258-research-15 reading). A run whose
next green ran on the same sha is a rerun-green (flake signal). ``fix_product_commit`` on the run
is whether a ``fix``-typed commit in the range touched product code.

Categories (strand B's six) are assigned by the ordered rules in ``label_item`` and a run takes the
category of the majority of its non-derived items (ties by ``PRIORITY``). Strand B's classifier is
gone; these rules are rebuilt from 258-research-1, -2 and -15 and each item records the rule that
labelled it, so a category that moves against strand B can be explained by rule. They are not fitted
to strand B's counts.

Check headers (wren's ``check_headers.py``) are not read here. ``guard_catches`` takes a
``check -> guard paths`` mapping so the header parser plugs in later.
"""

from __future__ import annotations

import argparse
import collections
import gzip
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

Row = dict[str, Any]

EXIT_OK: int = 0
EXIT_FAIL: int = 1

# --- scope ---------------------------------------------------------------------------------

DEFAULT_CONCLUSIONS: tuple[str, ...] = ("failure",)
WINDOW_START: str = "2026-09-23"
WINDOW_END: str = "2026-11-23"

PRODUCT_PREFIXES: tuple[str, ...] = ("src/", "service/src/main/")
TEST_PREFIXES: tuple[str, ...] = ("tests/", "service/src/test/")
#: Ledger, ceiling and allowlist files. A fix that touched only these is not a guard catch.
NON_GUARD_FILES: tuple[str, ...] = (
    "docs/wire-contract-pending.md",
    "conexus/PENDING_RELEASE.md",
    "tests/ddl_ceiling.toml",
    "tests/storage_touch_ceiling.toml",
    "scripts/file-size-exceptions.txt",
)
NON_GUARD_RE: re.Pattern[str] = re.compile(r"(allowlist|ceiling|exceptions|pending)", re.I)

# --- categories ----------------------------------------------------------------------------

PRODUCT: str = "product_defect"
ACCOUNTING: str = "accounting"
FLAKE: str = "flake_infra"
STALE: str = "stale_ambient"
HARNESS: str = "harness"
BUILD: str = "build_ruff"
UNLABELLED: str = "unlabelled"
#: Order of the six, for tables and for breaking a majority tie.
PRIORITY: tuple[str, ...] = (PRODUCT, ACCOUNTING, STALE, HARNESS, BUILD, FLAKE)

#: Strand B's figures (258-research-1, -2); the MVV compares against these.
STRAND_B_TOTAL: int = 787
STRAND_B_WORKFLOWS: dict[str, int] = {
    "CI": 602, "windows-pg-bundle-rehearsal": 85, "Service CI": 62, "plugin drift ledger": 24,
    "other": 14,
}
STRAND_B_CHECKS: dict[str, int] = {
    "storage-touch ratchet": 68, "docs_reference_rot": 56, "mode-declarations census": 47,
    "cli_runner_output_invariant": 27, "module_seam": 26, "wire pairing": 21, "tmp_root": 20,
}
STRAND_B_CATEGORIES: dict[str, int] = {
    PRODUCT: 19, ACCOUNTING: 501, FLAKE: 119, STALE: 86, HARNESS: 59, BUILD: 4,
}
WORKFLOW_TOL: float = 0.02
CHECK_TOL: tuple[float, int] = (0.10, 3)
CATEGORY_TOL: tuple[float, int] = (0.10, 3)

# --- rule tables ---------------------------------------------------------------------------

#: Job-level checks: every failing item in the job belongs to this check.
JOB_CHECKS: tuple[tuple[str, str], ...] = (
    ("mode-declarations census", "mode-declarations census"),
    ("pinned-tag freshness", "plugin drift ledger"),
    ("Every develop commit since the last code-exercised", "commit coverage audit"),
    ("write-seam behavioural gate", "write-seam gate"),
    ("pytest (docs readers)", "docs readers"),
)
#: Step-level checks (a ratchet or floor step has no failing test to name).
STEP_CHECKS: tuple[tuple[str, str], ...] = (
    ("storage-touch fan-in", "storage-touch ratchet"),
    ("DDL/bytes ratchet", "ddl-bytes ratchet"),
    ("Non-vacuity", "non-vacuity floor"),
    ("Executed-count floor", "executed-count floor"),
    ("Every shard split", "shard split check"),
    ("Verify the", "pytest-gate verdict"),
    ("Run the ledger contract", "plugin drift ledger"),
)
#: Test-file stem (without ``test_``) -> check name; any other file is its own stem.
FILE_CHECKS: dict[str, str] = {
    "wire_contract_pairing_lint": "wire pairing",
    "docs_reference_rot": "docs_reference_rot",
    "cli_runner_output_invariant": "cli_runner_output_invariant",
    "module_seam": "module_seam",
    "tmp_root": "tmp_root",
}

#: Jobs whose every failing item is accounting by construction (the lint bucket and its siblings).
ACCOUNTING_JOBS: tuple[str, ...] = (
    "pytest (lint markers", "pytest (mode-declarations census)", "pytest (docs readers)",
    "pinned-tag freshness", "Every develop commit since the last code-exercised",
)
ACCOUNTING_STEP_RE: re.Pattern[str] = re.compile(
    r"(ratchet|census|non-vacuity|executed-count floor|floor \(|drift|ledger contract|"
    r"coverage since|shard split|enforce lint-leg)", re.I)
#: Test-file stem or Java class that is a repo-text pin, census, ratchet or ledger (strand B's META_FILE).
META_FILE_RE: re.Pattern[str] = re.compile(
    r"(ratchet|census|_lint|lint_|ledger|reference_rot|invariant|_seam|budget|ceiling|_floor|"
    r"allowlist|tripwire|drift|coverage_audit|pairing|pending|manifest|declarations|scrub|"
    r"changelog|file_size|folder_shape|storage_touch|tmp_root|ddl|_wal|sleep_|_pin|pinned|"
    r"forbidden|_guard|verb_rot|_rot\b|gate_test|SourceScan|GateTest|Census|Ratchet|Lint|"
    r"Drift|Ledger|Tripwire|Floor|CodegenDrift|Coverage)")
META_MSG_RE: re.Pattern[str] = re.compile(
    r"(not allowlisted|do not resolve|undeclared|ceiling|ratchet|census|exceeds? the .{0,30}"
    r"(limit|budget|cap)|stale (entry|declaration)|pending ledger|no longer .{0,20}declared)", re.I)
HARNESS_FILE_RE: re.Pattern[str] = re.compile(
    r"(watched_pytest|lab_run|lab_remote|select_affected|push_check|git_push|worktree|xdist|"
    r"suite_lease|check_headers|ci_gate|ci_junit|ci_shared|ci_wait|ci_board|ci_cmd|"
    r"release_(gate|battery|holds|artifact_gate)|build_gate|mvnw|preflight|phase_review|"
    r"meta_tests|conftest|tests_agents|agent_worktree)", re.I)
BUILD_STEP_RE: re.Pattern[str] = re.compile(
    r"(^Lint src|ruff|^Build (service JAR|shaded|the bundle)|^Compile )", re.I)
INFRA_STEP_RE: re.Pattern[str] = re.compile(
    r"(^Set up job|^Run actions/|^Install ripgrep|^Resolve Python|^Initialize CodeQL|^Upload |"
    r"^Fetch the service jar|^Real PID namespace|^Stop stray|^Sign$|^Notarize$|^Post )", re.I)
INFRA_LOG_RE: re.Pattern[str] = re.compile(
    r"(unexpected http response: 5\d\d|unable to download|econnreset|connection reset|"
    r"could not resolve host|temporary failure in name resolution|rate limit exceeded|"
    r"no space left on device|runner has received a shutdown signal|lost communication with the "
    r"server|the operation was canceled|read timed out|HTTP 5\d\d|returned (error|status) 5\d\d|"
    r"bad gateway|service unavailable|failed to connect to .* port)", re.I)
#: Steps that report on other steps; derived when anything else in the run also failed.
DERIVED_STEP_RE: re.Pattern[str] = re.compile(
    r"^(Verify the|Non-vacuity|Executed-count floor|Enforce lint-leg|Every shard split)")

# --- log parsing ---------------------------------------------------------------------------

_TS_RE = re.compile(r"^﻿?\d{4}-\d\d-\d\dT[\d:.]+Z ?")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PYTEST_RE = re.compile(r"^(FAILED|ERROR) ([\w./-]+\.py)(?:::(.*?))?(?: - (.*))?$")
_JAVA_ROW_RE = re.compile(r"^\[ERROR\]\s{2,}([\w$.]+)\.(\w+)(?::\d+)?\s*(.*)$")


def parse_log(lines: Iterable[str]) -> dict[str, Any]:
    """Failing tests and infra evidence from one job log. Streams; never holds the log."""
    tests: dict[tuple[str, str, str], str] = {}
    errors: list[str] = []
    infra = False
    in_java = False
    for raw in lines:
        line = _ANSI_RE.sub("", _TS_RE.sub("", raw.rstrip("\n"))).rstrip()
        if line.startswith("##[error]"):
            if len(errors) < 5:
                errors.append(line[9:200])
            if INFRA_LOG_RE.search(line):
                infra = True
            continue
        if "Unable to download" in line or "Unexpected HTTP response: 5" in line:
            infra = True
        m = _PYTEST_RE.match(line)
        if m:
            tests.setdefault(("pytest", m.group(2), (m.group(3) or "")), (m.group(4) or "")[:120])
            continue
        if line.startswith("[ERROR] Failures:") or line.startswith("[ERROR] Errors:"):
            in_java = True
            continue
        if in_java:
            j = _JAVA_ROW_RE.match(line)
            if j:
                cls = j.group(1).rsplit(".", 1)[-1]
                tests.setdefault(("java", cls, j.group(2)), j.group(3)[:120])
            elif line.startswith("[ERROR] Tests run:") or re.fullmatch(r"\[ERROR\]\s*", line):
                in_java = False
    return {
        "tests": [{"kind": k, "file": f, "name": n, "msg": m} for (k, f, n), m in tests.items()],
        "errors": errors,
        "infra": infra,
    }


def read_log(snapshot: Path, run_id: int, job_id: int) -> dict[str, Any] | None:
    """Parsed log for a job, or None when the snapshot has no usable log (empty, or only a .err)."""
    path = snapshot / "logs" / str(run_id) / f"{job_id}.log"
    if not path.is_file() or path.stat().st_size == 0:
        return None
    with path.open(encoding="utf-8", errors="replace") as fh:
        return parse_log(fh)


# --- venue, checks, paths ------------------------------------------------------------------


def venue(labels: Sequence[str]) -> str:
    low = [x.lower() for x in labels]
    if any(x.startswith("win") for x in low):
        return "windows"
    if any(x.startswith(("ubuntu-", "macos-")) for x in low):
        return "hosted"
    return "self-hosted"


def is_product(path: str) -> bool:
    return path.startswith(PRODUCT_PREFIXES)


def workflow_bucket(name: str) -> str:
    return name if name in STRAND_B_WORKFLOWS else "other"


def item_check(item: Row) -> str:
    """The check an item belongs to: job-level, step-level, then the failing file's name."""
    for needle, name in JOB_CHECKS:
        if needle in item["job"]:
            return name
    if item["kind"] == "step":
        for needle, name in STEP_CHECKS:
            if needle in item["item"]:
                return name
        return item["item"]
    if item["kind"] == "java":
        return item["file"]
    stem = Path(item["file"]).stem.removeprefix("test_")
    return FILE_CHECKS.get(stem, stem)


def item_matches_path(item: Row, path: str) -> bool:
    """Whether a changed path is the file of the failing item (a test file or a Java class)."""
    if item["kind"] == "java":
        return path.endswith(f"/{item['file']}.java")
    return bool(item.get("file")) and path == item["file"]


# --- git -----------------------------------------------------------------------------------


def _git(repo: Path, *args: str, stdin: str | None = None) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], input=stdin, capture_output=True,
                         text=True, check=False)
    return out.stdout if out.returncode == 0 else ""


class History:
    """Fix ranges and per-commit file lists out of a git repository, memoised."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self._anc: dict[tuple[str, str], bool] = {}
        self.commits: dict[str, Row] = {}

    def is_ancestor(self, older: str, newer: str) -> bool:
        key = (older, newer)
        if key not in self._anc:
            r = subprocess.run(["git", "-C", str(self.repo), "merge-base", "--is-ancestor",
                                older, newer], capture_output=True, check=False)
            self._anc[key] = r.returncode == 0
        return self._anc[key]

    def range(self, red: str, green: str) -> list[str]:
        """Commits in ``red..green``, oldest first (empty when the same sha or unrelated)."""
        out = _git(self.repo, "rev-list", "--reverse", "--no-merges", f"{red}..{green}")
        return out.split()

    def load(self, shas: Iterable[str]) -> None:
        need = sorted(set(shas) - self.commits.keys())
        if not need:
            return
        out = _git(self.repo, "log", "--no-walk=unsorted", "--no-merges", "--no-renames",
                   "--name-only", "--format=@@%H%x09%s", "--stdin", stdin="\n".join(need) + "\n")
        cur: Row | None = None
        for line in out.splitlines():
            if line.startswith("@@"):
                sha, _, subj = line[2:].partition("\t")
                cur = {"files": [], "subj": subj[:100]}
                self.commits[sha] = cur
            elif line and cur is not None:
                cur["files"].append(line)

    def files(self, sha: str) -> list[str]:
        return self.commits.get(sha, {}).get("files", [])


FIX_SUBJECT_RE: re.Pattern[str] = re.compile(r"^fix(\(|:|!)", re.I)


def next_green(red: Row, later_green: Sequence[Row], hist: History) -> Row | None:
    """First later success run of the workflow whose sha is the red sha or descends from it."""
    for g in later_green:
        if g["head_sha"] == red["head_sha"] or hist.is_ancestor(red["head_sha"], g["head_sha"]):
            return g
    return None


# --- extraction ----------------------------------------------------------------------------


def _load_runs(snapshot: Path) -> list[Row]:
    with (snapshot / "runs.jsonl").open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _load_jobs(snapshot: Path, run_id: int) -> list[Row] | None:
    path = snapshot / "jobs" / f"{run_id}.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["jobs"] if isinstance(data, dict) else data


def _items_for_run(run: Row, jobs: Sequence[Row], snapshot: Path) -> tuple[list[Row], Row]:
    """Failing items of a run, plus log coverage counts."""
    failed = [j for j in jobs if j.get("conclusion") == "failure"]
    cov = {"jobs": len(failed), "logs": 0}
    items: list[Row] = []
    any_other_failure = len(failed) > 1 or any(
        sum(1 for s in j.get("steps", []) if s.get("conclusion") == "failure") > 1 for j in failed)
    for job in failed:
        steps = [s["name"] for s in job.get("steps", []) if s.get("conclusion") == "failure"]
        parsed = read_log(snapshot, run["id"], job["id"])
        if parsed is not None:
            cov["logs"] += 1
        base = {"t": "item", "run": run["id"], "sha": run["head_sha"], "workflow": run["name"],
                "job": job["name"], "venue": venue(job.get("labels", [])), "job_id": job["id"],
                "infra": bool(parsed and parsed["infra"]), "errors": (parsed or {}).get("errors", [])[:2]}
        emitted_tests = False
        if parsed and parsed["tests"]:
            first = steps[0] if steps else job["name"]
            for t in parsed["tests"]:
                kind = "java" if t["kind"] == "java" else "test"
                items.append({**base, "kind": kind, "step": first, "file": t["file"],
                              "item": t["name"], "msg": t["msg"], "derived": False})
            emitted_tests = True
        for idx, step in enumerate(steps or [job["name"]]):
            if emitted_tests and idx == 0:
                continue
            derived = bool(DERIVED_STEP_RE.match(step)) and (any_other_failure or emitted_tests)
            items.append({**base, "kind": "step", "step": step, "file": "", "item": step,
                          "msg": "", "derived": derived})
    return items, cov


def _placeholder_item(run: Row) -> Row:
    return {"t": "item", "run": run["id"], "sha": run["head_sha"], "workflow": run["name"],
            "job": "(jobs file missing)",
            "venue": "unknown", "job_id": 0, "infra": False, "errors": [], "kind": "step",
            "step": "(jobs file missing)", "file": "", "item": "(jobs file missing)", "msg": "",
            "derived": False}


def _decided(runs: Sequence[Row]) -> list[Row]:
    return [{"t": "decided", "wf": r["name"], "id": r["id"], "s": r["created_at"],
             "e": r["updated_at"], "c": "s" if r["conclusion"] == "success" else "f"}
            for r in runs if r["conclusion"] in ("success", "failure")]


def extract(snapshot: Path, repo: Path, *, conclusions: Sequence[str] = DEFAULT_CONCLUSIONS,
            include_jobless: bool = False, since: str = WINDOW_START,
            until: str = WINDOW_END) -> list[Row]:
    """Build the extraction records (see the module docstring) from a snapshot and a repo."""
    runs = [r for r in _load_runs(snapshot)
            if since <= r["created_at"][:10] <= until]
    by_wf: dict[str, list[Row]] = collections.defaultdict(list)
    for r in sorted(runs, key=lambda r: (r["created_at"], r["id"])):
        by_wf[r["name"]].append(r)
    hist = History(repo)
    out_runs: list[Row] = []
    out_items: list[Row] = []
    cov = {"jobs": 0, "logs": 0}
    dropped_jobless = 0
    missing_jobs = 0
    for red in [r for r in runs if r["conclusion"] in conclusions]:
        jobs = _load_jobs(snapshot, red["id"])
        if jobs is None:
            # No jobs file yet (a snapshot still being fetched): keep the run, visibly unlabelled.
            missing_jobs += 1
            items, c = [_placeholder_item(red)], {"jobs": 0, "logs": 0}
        else:
            items, c = _items_for_run(red, jobs, snapshot)
        if not items and not include_jobless:
            dropped_jobless += 1
            continue
        cov["jobs"] += c["jobs"]
        cov["logs"] += c["logs"]
        later = [g for g in by_wf[red["name"]]
                 if g["conclusion"] == "success" and (g["created_at"], g["id"]) > (red["created_at"], red["id"])]
        green = next_green(red, later, hist)
        rng = hist.range(red["head_sha"], green["head_sha"]) if green else []
        hist.load(rng)
        rec: Row = {"t": "run", "id": red["id"], "sha": red["head_sha"], "workflow": red["name"],
                    "conclusion": red["conclusion"], "attempt": red.get("run_attempt", 1),
                    "created": red["created_at"], "ended": red["updated_at"],
                    "green_run": green["id"] if green else None,
                    "green_sha": green["head_sha"] if green else None,
                    "green_ended": green["updated_at"] if green else None,
                    "unresolved": green is None,
                    "rerun_green": bool(green and green["head_sha"] == red["head_sha"]),
                    "fix_commits": [c[:12] for c in rng],
                    "range_touched_product": any(is_product(f) for c in rng for f in hist.files(c)),
                    "fix_product_commit": any(
                        FIX_SUBJECT_RE.match(hist.commits.get(c, {}).get("subj", ""))
                        and any(is_product(f) for f in hist.files(c)) for c in rng)}
        out_runs.append(rec)
        for it in items:
            touching = [c for c in rng if any(item_matches_path(it, f) for f in hist.files(c))]
            it["touched_test"] = None if green is None else bool(touching)
            it["fix_touched_product"] = (None if green is None else
                                         any(is_product(f) for c in touching for f in hist.files(c)))
            out_items.append(it)
    commits = [{"t": "commit", "sha": s[:12], "subj": v["subj"], "files": v["files"]}
               for s, v in sorted(hist.commits.items())]
    meta = {"t": "meta", "window": [since, until], "conclusions": list(conclusions),
            "include_jobless": include_jobless, "runs_in_window": len(runs),
            "red_runs": len(out_runs), "dropped_jobless": dropped_jobless,
            "runs_missing_jobs_file": missing_jobs, "failed_jobs": cov["jobs"],
            "failed_jobs_with_log": cov["logs"], "manifest": (snapshot / "MANIFEST.json").is_file()}
    return ([meta] + sorted(out_runs, key=lambda r: r["id"]) + out_items + commits
            + _decided(runs))


def write_extraction(records: Iterable[Row], path: Path) -> None:
    """Gzip jsonl with a zeroed mtime, so the same records give the same bytes."""
    with path.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0,
                                                compresslevel=9) as gz:
        for rec in records:
            gz.write((json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n").encode())


def read_extraction(path: Path) -> list[Row]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# --- labelling -----------------------------------------------------------------------------


def label_item(item: Row, run: Row) -> tuple[str, str]:
    """(category, rule) for one failing item. First matching rule wins; see the module docstring."""
    step, job = item["step"], item["job"]
    stem = Path(item["file"]).stem if item["kind"] == "test" else item["file"]
    if item["kind"] == "step" and BUILD_STEP_RE.search(item["item"]):
        return BUILD, "build:step-name"
    if item["infra"]:
        return FLAKE, "flake:infra-signature"
    if item["kind"] == "step" and INFRA_STEP_RE.search(item["item"]):
        return FLAKE, "flake:infra-step"
    if any(job.startswith(j) or j in job for j in ACCOUNTING_JOBS):
        return ACCOUNTING, "accounting:job"
    if item["kind"] == "step" and ACCOUNTING_STEP_RE.search(item["item"]):
        return ACCOUNTING, "accounting:step"
    if item["kind"] != "step" and META_FILE_RE.search(stem):
        return ACCOUNTING, "accounting:meta-file"
    if item["kind"] != "step" and META_MSG_RE.search(item.get("msg", "")):
        return ACCOUNTING, "accounting:meta-msg"
    if item["kind"] != "step" and HARNESS_FILE_RE.search(stem):
        return HARNESS, "harness:file"
    if run["rerun_green"]:
        return FLAKE, "flake:rerun-green"
    if item["kind"] == "step":
        return UNLABELLED, "unlabelled:step-without-log"
    if run["unresolved"]:
        return STALE, "stale:no-green"
    if run["fix_product_commit"] and (item["touched_test"] is False or item["fix_touched_product"]):
        return PRODUCT, "product:fix-commit-touched-src"
    if item["touched_test"]:
        return STALE, "stale:fix-touched-test"
    return STALE, "stale:ambient-no-test-fix"


def label_run(items: Sequence[Row]) -> tuple[str, collections.Counter[str]]:
    """Majority category of a run's non-derived items; derived items only when nothing else."""
    pool = [i for i in items if not i["derived"]] or list(items)
    votes: collections.Counter[str] = collections.Counter(
        i["category"] for i in pool if i["category"] != UNLABELLED)
    if not votes:
        return UNLABELLED, collections.Counter()
    top = max(votes.values())
    tied = [c for c in PRIORITY if votes.get(c) == top]
    return tied[0], votes



def analyze(records: Sequence[Row]) -> dict[str, Any]:
    """Label every item and run. Pure over the extraction records."""
    runs = {r["id"]: r for r in records if r["t"] == "run"}
    items = [dict(r) for r in records if r["t"] == "item"]
    by_run: dict[int, list[Row]] = collections.defaultdict(list)
    for it in items:
        it["category"], it["rule"] = label_item(it, runs[it["run"]])
        it["check"] = item_check(it)
        by_run[it["run"]].append(it)
    for rid, run in runs.items():
        run["category"], run["votes"] = label_run(by_run[rid])
        run["rules"] = sorted({i["rule"] for i in by_run[rid] if i["category"] == run["category"]
                               and not i["derived"]})
    return {"runs": runs, "items": items, "by_run": by_run,
            "meta": next((r for r in records if r["t"] == "meta"), {}),
            "decided": [r for r in records if r["t"] == "decided"],
            "commits": {r["sha"]: r for r in records if r["t"] == "commit"}}


def check_counts(an: Mapping[str, Any]) -> collections.Counter[str]:
    """Distinct red runs per check, over non-derived items."""
    seen: set[tuple[str, int]] = set()
    cnt: collections.Counter[str] = collections.Counter()
    for it in an["items"]:
        if it["derived"] or (it["check"], it["run"]) in seen:
            continue
        seen.add((it["check"], it["run"]))
        cnt[it["check"]] += 1
    return cnt


def category_counts(an: Mapping[str, Any]) -> collections.Counter[str]:
    return collections.Counter(r["category"] for r in an["runs"].values())


def workflow_counts(an: Mapping[str, Any]) -> collections.Counter[str]:
    return collections.Counter(workflow_bucket(r["workflow"]) for r in an["runs"].values())


def guard_catches(an: Mapping[str, Any], guards: Mapping[str, Sequence[str]]) -> dict[str, int]:
    """Runs per check whose fix range touched a path its ``guards:`` header names.

    ``guards`` maps a check name (as ``item_check`` spells it) to guarded path prefixes; the header
    parser (scripts/check_headers.py, not yet on develop) supplies it. Ledger, ceiling and allowlist
    files never count (Q1, Sam 2026-11-27).
    """
    out: dict[str, int] = {}
    seen: set[tuple[str, int]] = set()
    for it in an["items"]:
        paths = guards.get(it["check"])
        if not paths or it["derived"] or (it["check"], it["run"]) in seen:
            continue
        seen.add((it["check"], it["run"]))
        run = an["runs"][it["run"]]
        files = {f for c in run["fix_commits"] for f in an["commits"].get(c, {}).get("files", [])}
        hit = any(f.startswith(tuple(paths)) and f not in NON_GUARD_FILES and not NON_GUARD_RE.search(f)
                  for f in files)
        out[it["check"]] = out.get(it["check"], 0) + int(hit)
    return out


# --- red time ------------------------------------------------------------------------------


def _ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def red_hours(an: Mapping[str, Any], workflow: str = "CI") -> dict[str, float]:
    """Red wall-clock of a workflow, by category of the red run that opens each stretch.

    A stretch runs from a red run's completion to the completion of the next decided run of the
    workflow; a green run closes the episode.
    """
    dec = sorted((d for d in an["decided"] if d["wf"] == workflow), key=lambda d: (d["s"], d["id"]))
    hours: collections.Counter[str] = collections.Counter()
    for a, b in zip(dec, dec[1:]):
        if a["c"] != "f":
            continue
        run = an["runs"].get(a["id"])
        cat = run["category"] if run else UNLABELLED
        hours[cat] += max(0.0, _ts(b["e"]) - _ts(a["e"])) / 3600
    total = sum(hours.values())
    return {**{k: round(v, 2) for k, v in hours.items()}, "_total": round(total, 2)}


def commit_mix(repo: Path, since: str, until: str) -> dict[str, int]:
    """Non-merge commit mix over a window: product, tests-only, ledger-only, other."""
    out = _git(repo, "log", "--no-merges", "--no-renames", "--name-only", "--format=@@%H",
               f"--since={since}", f"--until={until}T23:59:59", "origin/develop")
    mix: collections.Counter[str] = collections.Counter()
    files: list[str] = []

    def flush() -> None:
        if any(is_product(f) for f in files):
            mix["product"] += 1
        elif files and all(f.startswith(TEST_PREFIXES) for f in files):
            mix["tests_only"] += 1
        elif files and all(f in NON_GUARD_FILES or f.startswith(TEST_PREFIXES) for f in files):
            mix["ledger_or_ceiling"] += 1
        else:
            mix["other"] += 1
        mix["total"] += 1

    started = False
    for line in out.splitlines():
        if line.startswith("@@"):
            if started:
                flush()
            files.clear()
            started = True
        elif line:
            files.append(line)
    if started:
        flush()
    return dict(mix)


# --- report --------------------------------------------------------------------------------


def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:.1f}%" if d else "n/a"


def render_report(an: Mapping[str, Any], top: int = 15, repo: Path | None = None) -> str:
    meta = an["meta"]
    total = len(an["runs"])
    lines = [f"red runs: {total}  window {meta.get('window')}  conclusions {meta.get('conclusions')}"]
    cov = (meta.get("failed_jobs_with_log", 0), meta.get("failed_jobs", 0))
    if cov[0] < cov[1] or not meta.get("manifest", True):
        lines.append(f"PARTIAL DATA: logs for {cov[0]} of {cov[1]} failed jobs; "
                     f"manifest {'present' if meta.get('manifest') else 'absent'}")
    lines += ["", "by workflow"]
    lines += [f"  {k:34s} {v:5d}  {_pct(v, total)}" for k, v in workflow_counts(an).most_common()]
    lines += ["", f"by check (top {top}; distinct red runs, derived items excluded)"]
    lines += [f"  {k:44s} {v:5d}" for k, v in check_counts(an).most_common(top)]
    cats = category_counts(an)
    lines += ["", "by category"]
    for c in (*PRIORITY, UNLABELLED):
        lines.append(f"  {c:16s} {cats.get(c, 0):5d}  {_pct(cats.get(c, 0), total)}")
    hrs = red_hours(an)
    lines += ["", f"CI red hours {hrs['_total']}  by category "
              + ", ".join(f"{k} {v}" for k, v in sorted(hrs.items()) if k != "_total")]
    venues = collections.Counter(i["venue"] for i in an["items"] if not i["derived"])
    lines += ["", "failing items by venue: " + ", ".join(f"{k} {v}" for k, v in venues.most_common())]
    if repo is not None:
        mix = commit_mix(repo, *meta.get("window", [WINDOW_START, WINDOW_END]))
        lines += ["", "commit mix (non-merge, origin/develop): "
                  + ", ".join(f"{k} {v}" for k, v in sorted(mix.items()))]
    return "\n".join(lines)


def within(ours: int, want: int, rel: float, floor: int = 0) -> bool:
    """Tolerance math of the MVV: |ours - want| <= max(rel * want, floor)."""
    return abs(ours - want) <= max(rel * want, floor)


def mvv_lines(an: Mapping[str, Any]) -> list[tuple[bool, str]]:
    out: list[tuple[bool, str]] = []
    total = len(an["runs"])
    out.append((within(total, STRAND_B_TOTAL, WORKFLOW_TOL), f"total red runs: ours {total} vs strand B {STRAND_B_TOTAL} (2%)"))
    wf = workflow_counts(an)
    for k, want in STRAND_B_WORKFLOWS.items():
        out.append((within(wf.get(k, 0), want, WORKFLOW_TOL), f"workflow {k}: ours {wf.get(k, 0)} vs {want} (2%)"))
    ck = check_counts(an)
    for k, want in STRAND_B_CHECKS.items():
        out.append((within(ck.get(k, 0), want, *CHECK_TOL), f"check {k}: ours {ck.get(k, 0)} vs {want} (max(10%,3))"))
    cats = category_counts(an)
    for k, want in STRAND_B_CATEGORIES.items():
        ok = within(cats.get(k, 0), want, *CATEGORY_TOL)
        line = f"category {k}: ours {cats.get(k, 0)} vs {want} (max(10%,3))"
        if not ok:
            rules = collections.Counter(r for run in an["runs"].values() if run["category"] == k
                                        for r in run["rules"])
            line += f"  rules: {dict(rules.most_common(6))}"
        out.append((ok, line))
    out.append((cats.get(UNLABELLED, 0) == 0, f"unlabelled runs (no log, no rule): {cats.get(UNLABELLED, 0)}"))
    return out


def render_mvv(an: Mapping[str, Any]) -> tuple[str, bool]:
    meta = an["meta"]
    head = []
    if meta.get("failed_jobs_with_log", 0) < meta.get("failed_jobs", 0) or not meta.get("manifest", True):
        head.append(f"PARTIAL: logs for {meta.get('failed_jobs_with_log')} of {meta.get('failed_jobs')} "
                    f"failed jobs, manifest {'present' if meta.get('manifest') else 'absent'}; "
                    "check and category lines are not final")
    lines = mvv_lines(an)
    body = [f"{'PASS' if ok else 'FAIL'}  {text}" for ok, text in lines]
    return "\n".join(head + body), all(ok for ok, _ in lines)


# --- cli -----------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("extract")
    ex.add_argument("--snapshot", type=Path, required=True)
    ex.add_argument("--repo", type=Path, default=Path("."))
    ex.add_argument("--out", type=Path, required=True)
    ex.add_argument("--conclusions", default=",".join(DEFAULT_CONCLUSIONS),
                    help="comma list of run conclusions that count as red (default: failure)")
    ex.add_argument("--include-jobless", action="store_true",
                    help="keep red runs that have no failed job record")
    ex.add_argument("--since", default=WINDOW_START)
    ex.add_argument("--until", default=WINDOW_END)
    rp = sub.add_parser("report")
    rp.add_argument("--extraction", type=Path, required=True)
    rp.add_argument("--top", type=int, default=15)
    rp.add_argument("--repo", type=Path, default=None, help="adds the commit-mix figures")
    mv = sub.add_parser("mvv")
    mv.add_argument("--extraction", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.cmd == "extract":
        recs = extract(args.snapshot, args.repo, conclusions=tuple(args.conclusions.split(",")),
                       include_jobless=args.include_jobless, since=args.since, until=args.until)
        write_extraction(recs, args.out)
        sys.stdout.write(f"wrote {args.out}: {recs[0]}\n")
        return EXIT_OK
    an = analyze(read_extraction(args.extraction))
    if args.cmd == "report":
        sys.stdout.write(render_report(an, args.top, args.repo) + "\n")
        return EXIT_OK
    text, ok = render_mvv(an)
    sys.stdout.write(text + "\n")
    return EXIT_OK if ok else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
