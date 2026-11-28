#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Red-run report over a snapshot of develop CI runs (RDR-258 Phase 0 step 2, nexus-2jomg.1).

    red_run_report.py fetch-green-jobs --snapshot DIR --out GREENDIR
    red_run_report.py extract --snapshot DIR --green-jobs GREENDIR --repo . --out scripts/red-run-extraction-2026-09-23_2026-11-23.jsonl.gz
    red_run_report.py report  --extraction FILE [--top 15] [--repo .] [--sample RULE N --seed S]
    red_run_report.py mvv     --extraction FILE
    red_run_report.py candidates --extraction FILE --out FILE.json[.gz]

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

Resolution. A red run is resolved only by a later run of the same workflow, on the red sha or a
descendant of it, in which a job of the same name (the matrix-expanded name included) succeeded,
for every job that failed in the red run. A later run that skipped the job, or that has no job of
that name, does not resolve it; a doc-only green that skipped every pytest shard once closed a red
and hid the real fix. The jobs of those later runs are not in the snapshot: ``fetch-green-jobs``
fetches them (``gh api``, read-only) into a directory that ``extract --green-jobs`` reads. A red run
not resolved within its next ``GREEN_CANDIDATES_MAX`` successful runs is unresolved-unverified.

Fix signal. The commits in ``<red sha>..<resolving green sha>`` are the fix range, so the range ends
at the first run that passed the failing job, not at the first success. For an item the fix is the
FIRST commit in that range that touched the failing test's file, at whatever distance: the range is
already bounded by the job passing, so no commit-count window is applied (an earlier 10-commit
window missed census fixes at distance 11 to 15). ``fix_distance`` records how far it is.
``fix_touched_product`` says whether that commit also touched ``src/`` or ``service/src/main/``
(the 258-research-15 reading), ``fix_is_fix`` whether its subject is ``fix``-typed, and
``fix_table_only`` whether its diff to the file is only table-like lines. A run whose resolving
green ran on the same sha is a rerun-green. ``fix_product_commit`` on the run is the range-wide form.

Categories. The six of strand B plus ``unattributed``, assigned by the ordered rules in
``label_item``: build step, infra signature (``##[error]`` lines of a step with no named test),
accounting (lint-bucket jobs, ratchet and floor steps, census and pin messages, repo-text-pin file
names, count pins on an entries dict), harness, then for a named test a timing message is flake, a
fix commit that changed the test and product code is a product defect, a test edited to go green is
stale or ambient-coupled, and a test that went green with no commit touching it, or that no later
run verifiably resolved, is ``unattributed``: the record cannot say whether it was flaky, ambient,
or fixed in ``src/`` without touching the test. ``unattributed`` is counted in none of strand B's six;
the MVV prints it beside flake and stale with the bracket each implies. A step with no named test
falls back to the pytest failed-count (Windows: portability). A run takes the category of the
majority of its non-derived items (ties by ``PRIORITY``). Strand B's classifier is gone; the rules
are rebuilt from 258-research-1, -2 and -15, hand-sampled, and each item records the rule that
labelled it. They are not fitted to strand B's counts.

``candidates`` writes the flake and unattributed named tests as JSON (one row per test).

Check headers (wren's ``check_headers.py``) are not read here. ``guard_catches`` takes a
``check -> guard paths`` mapping so the header parser plugs in later.
"""

from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from collections.abc import Callable, Iterable, Mapping, Sequence
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
#: Data files (not code) whose name marks them as a ledger, ceiling or allowlist.
NON_GUARD_RE: re.Pattern[str] = re.compile(
    r"(allowlist|ceiling|exceptions)[^/]*\.(toml|txt|md|json|ya?ml|csv)$", re.I)
#: A red run not resolved within its next this-many successful runs of the workflow is unresolved.
GREEN_CANDIDATES_MAX: int = 60
#: The Phase 0 step 1 archive (T2 nexus/rdr258-phase0-baseline-snapshot), for the candidates header.
PHASE0_ARCHIVE_SHA256: str = "520722bfbf0a9e75b566093f5ab19078ef3e3c41f39af4a356714d07b7f8a968"
PLACEHOLDER_JOB: str = "(jobs file missing)"

# --- categories ----------------------------------------------------------------------------

PRODUCT: str = "product_defect"
ACCOUNTING: str = "accounting"
FLAKE: str = "flake_infra"
STALE: str = "stale_ambient"
HARNESS: str = "harness"
BUILD: str = "build_ruff"
UNATTRIBUTED: str = "unattributed"
UNLABELLED: str = "unlabelled"
#: Order of the categories, for tables and for breaking a majority tie.
PRIORITY: tuple[str, ...] = (PRODUCT, ACCOUNTING, STALE, HARNESS, BUILD, FLAKE, UNATTRIBUTED)

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
#: Narrow on purpose: ``Coverage``, ``_floor``, ``_wal``, ``manifest`` and ``pending`` matched real
#: product tests (TaxonomyCoverageTest, test_hooks_json_verb_release_floor, catalog_walk,
#: test_manifest_write_many) and are gone; a product test that trips an accounting guard is caught
#: by the message rule instead.
META_FILE_RE: re.Pattern[str] = re.compile(
    r"(ratchet|census|_lint|lint_|ledger|reference_rot|invariant|_seam|budget|ceiling|"
    r"allowlist|tripwire|drift|coverage_audit|pairing_lint|declarations|scrub|"
    r"changelog|file_size|folder_shape|storage_touch|tmp_root|ddl_|wal_(ceiling|ratchet)|"
    r"sleep_ratchet|_pin\b|pinned|forbidden|verb_rot|_rot\b|gate_test|SourceScan|GateTest|"
    r"Census|Ratchet|Lint\b|Drift|Ledger|Tripwire|CodegenDrift|ScanTest|(skip|junit|executed)_floor)")
#: A test that asserts a set of sites "is/are allowlisted" is a census of repository text. Only this
#: phrase is matched on a function name: ``drift``, ``ledger`` and ``scrub`` also name product
#: behaviour (DocCountDrift, a hook payload scrub) and matched wrongly in a hand sample.
ALLOWLISTED_FUNC_RE: re.Pattern[str] = re.compile(r"(is|are)_allowlisted")
#: An assertion message that names a census, floor, pin, ceiling or a scan of repository text.
META_MSG_RE: re.Pattern[str] = re.compile(
    r"(StorageTouchError|not allowlisted|skip ceiling|do not resolve|undeclared|unregistered|"
    r"ceiling|ratchet|census|\bcall sites?\b|\ba file other than\b|\bpinned\b|source[- ]scan|"
    r"is not in the report|floor (is|of)\b|exceeds? the .{0,30}(limit|budget|cap)|"
    r"stale (entry|declaration)|pending ledger|no longer .{0,20}declared|\bbudget\b|"
    r"\bsanctioned|\bdrifted\b|\bstill (says|names|mentions)\b|\bbypass(es|ing)?\b|"
    r"explicitly (excluded|exempt|listed))", re.I)
#: A message of a timing or concurrency failure: a test that waits or races, which goes green again
#: without anyone changing it. A missing file or permission error on the host ("[Errno ...]") is
#: ambient state, not timing, and is left to the stale/ambient rules.
TIMING_MSG_RE: re.Pattern[str] = re.compile(
    r"(predicate not satisfied|within \d+(\.\d+)?\s?s\b|timed out|timeout|deadline|\bp9\d\b|"
    r"starv|deadlock|status=50\d|\b5xx\b|recycled|EBUSY|were served while)", re.I)
#: A count pin on an entries dict: the assertion message is just ``{'name': 18, 'other': 4}``.
COUNT_PIN_RE: re.Pattern[str] = re.compile(r"^(AssertionError: )?(assert )?\{('[\w./ -]+': \d+(, )?)+\}")
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
    r"bad gateway|service unavailable|failed to connect to .* port|failed to download)", re.I)
#: An error annotation that carries no reason: the step exit status and nothing else.
GENERIC_ERROR_RE: re.Pattern[str] = re.compile(r"Process completed with exit code \d+\.?")
#: Steps that report on other steps; derived when anything else in the run also failed.
DERIVED_STEP_RE: re.Pattern[str] = re.compile(
    r"^(Verify the|Non-vacuity|Executed-count floor|Enforce lint-leg|Every shard split)")

# --- log parsing ---------------------------------------------------------------------------

_TS_RE = re.compile(r"^﻿?\d{4}-\d\d-\d\dT[\d:.]+Z ?")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
#: A node id has no spaces outside ``[...]``, so a ``" - "`` inside a parametrize id is not a split.
_PYTEST_RE = re.compile(
    r"^(FAILED|ERROR) ([\w./\\-]+\.py)(?:::((?:[^\[\s]|\[[^\]]*\])*))?(?: - (.*))?$")
#: The pytest summary line: ``== 3 failed, 10 passed, 2 errors in 4.1s ==``.
_SUMMARY_RE = re.compile(r"^(?:=+ )?(\d+ (?:failed|passed|errors?|skipped|deselected|xfailed|xpassed|"
                         r"warnings?)\b.*) in [\d.]+s")
_COUNT_RE = re.compile(r"(\d+) (failed|errors?)\b")
_SECTION_RE = re.compile(r"^_{3,} (.+?) _{3,}$")
_JAVA_ROW_RE = re.compile(r"^\[ERROR\]\s{2,}([\w$.]+)\.(\w+)(?::\d+)?\s*(.*)$")


def parse_log(lines: Iterable[str]) -> dict[str, Any]:
    """Failing tests and infra evidence from one job log. Streams; never holds the log."""
    tests: dict[tuple[str, str, str], str] = {}
    errors: list[str] = []
    failed_count = 0
    infra = False
    in_java = False
    pending: str | None = None
    first_e = ""
    sections: dict[str, str] = {}
    for raw in lines:
        line = _ANSI_RE.sub("", _TS_RE.sub("", raw.rstrip("\n"))).rstrip()
        if line.startswith("##[error]"):
            if len(errors) < 20:
                errors.append(line[9:200])
            if INFRA_LOG_RE.search(line):
                infra = True
            continue
        sm = _SUMMARY_RE.match(line)
        if sm:
            failed_count = max(failed_count, sum(int(n) for n, _ in _COUNT_RE.findall(sm.group(1))))
            continue
        if not first_e and line.startswith("E  "):
            first_e = line[1:].strip()[:300]
        sec = _SECTION_RE.match(line)
        if sec:
            pending = sec.group(1)
            continue
        if pending is not None and line.startswith("E  "):
            sections.setdefault(pending, line[1:].strip()[:300])
            pending = None
            continue
        m = _PYTEST_RE.match(line)
        if m:
            tests.setdefault(("pytest", m.group(2).replace("\\", "/"), (m.group(3) or "")),
                             (m.group(4) or "")[:300])
            continue
        if line.startswith("[ERROR] Failures:") or line.startswith("[ERROR] Errors:"):
            in_java = True
            continue
        if in_java:
            j = _JAVA_ROW_RE.match(line)
            if j:
                cls = j.group(1).rsplit(".", 1)[-1].split("$")[0]
                tests.setdefault(("java", cls, j.group(2)), j.group(3)[:300])
            elif line.startswith("[ERROR] Tests run:") or re.fullmatch(r"\[ERROR\]\s*", line):
                in_java = False
    if not tests:
        # No FAILED lines (the Windows leg runs with -rs only): name the failures by their
        # section headers; the file is not in the log.
        for name, msg in sections.items():
            tests[("pytest", "", name)] = msg
    return {
        "tests": [{"kind": k, "file": f, "name": n, "msg": m} for (k, f, n), m in tests.items()],
        "errors": errors,
        "infra": infra,
        "failed_count": failed_count,
        "evidence": any(not GENERIC_ERROR_RE.fullmatch(e) for e in errors),
        "first_e": first_e,
    }


def read_log(snapshot: Path, run_id: int, job_id: int,
             extra: Sequence[Path] = ()) -> dict[str, Any] | None:
    """Parsed log for a job, or None when no directory has a usable log (empty, or only a .err).

    ``extra`` are fallback log roots of the same ``<run>/<job>.log`` layout, searched after the
    snapshot's own ``logs/``.
    """
    for root in (snapshot / "logs", *extra):
        path = root / str(run_id) / f"{job_id}.log"
        if path.is_file() and path.stat().st_size > 0:
            with path.open(encoding="utf-8", errors="replace") as fh:
                return {**parse_log(fh), "extra": root != snapshot / "logs"}
    return None


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
    if not item["file"]:
        return re.split(r"[\[.]", item["item"])[0]
    stem = Path(item["file"]).stem.removeprefix("test_")
    return FILE_CHECKS.get(stem, stem)


def item_matches_path(item: Row, path: str) -> bool:
    """Whether a changed path is the file of the failing item (a test file or a Java class)."""
    if item["kind"] == "java":
        return path.endswith(f"/{item['file']}.java")
    return bool(item.get("file")) and path == item["file"]


# --- git -----------------------------------------------------------------------------------


def _git(repo: Path, *args: str, stdin: str | None = None) -> str:
    """Stdout of a git command; a failure raises, so a bad ``--repo`` cannot read as an empty history."""
    out = subprocess.run(["git", "-C", str(repo), *args], input=stdin, capture_output=True,
                         text=True, check=False)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])} failed in {repo}: {out.stderr.strip()[:200]}")
    return out.stdout


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
            if r.returncode not in (0, 1):    # 1 is "not an ancestor"; anything else is an error
                raise RuntimeError(f"git merge-base failed in {self.repo}: {r.stderr.decode()[:200]}")
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

    def table_only(self, sha: str, path: str) -> bool | None:
        """Whether a commit's change to one file is only table-like lines (None: no change found)."""
        out = _git(self.repo, "show", "--no-color", "--no-renames", "-U0", "--format=", sha, "--", path)
        changed = [ln[1:] for ln in out.splitlines()
                   if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
        if not changed:
            return None
        return all(is_table_line(ln) for ln in changed)

FIX_SUBJECT_RE: re.Pattern[str] = re.compile(r"^fix(\(|:|!)", re.I)
#: A diff line that is data, not logic: a string, tuple, list or dict entry, a comment, a blank, or
#: a bare key/value. A census, pin or allowlist table edit is made only of these; an assertion,
#: control flow or definition line is not.
_LOGIC_RE: re.Pattern[str] = re.compile(
    r"^\s*(assert|def|class|import|from|return|if|elif|else|for|while|with|raise|yield|try|except|"
    r"@|async)\b")
_TABLE_RE: re.Pattern[str] = re.compile(
    r"""^\s*(#.*|["'(\[{].*|[\w./:-]+\s*[:=]\s*["'(\[{\d].*|[\w./-]+,?|[)\]}],?)?\s*$""")


def is_table_line(line: str) -> bool:
    return not _LOGIC_RE.match(line) and bool(_TABLE_RE.match(line))


GH_SLUG: str = "Hellblazer/nexus"


def _load_green_jobs(green_dir: Path | None, run_id: int) -> list[Row] | None:
    """The jobs of a later successful run (name, conclusion), or None when the file is absent."""
    if green_dir is None:
        return None
    path = green_dir / f"{run_id}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def job_names_passed(jobs: Sequence[Row], names: Iterable[str]) -> bool:
    """Whether every name has a job of that exact (matrix-expanded) name that succeeded."""
    passed = {j["name"] for j in jobs if j.get("conclusion") == "success"}
    return all(n in passed for n in names)


def _gh_run_jobs(run_id: int, slug: str = GH_SLUG) -> list[Row]:
    """Jobs of a run's latest attempt through ``gh api`` (read-only, one call per page)."""
    for attempt in range(4):
        out = subprocess.run(
            ["gh", "api", f"repos/{slug}/actions/runs/{run_id}/jobs?per_page=100&filter=latest",
             "--paginate", "--jq", ".jobs[] | {id, name, conclusion, run_attempt}"],
            capture_output=True, text=True, check=False)
        if out.returncode == 0:
            return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"gh api jobs of run {run_id} failed: {out.stderr.strip()[:200]}")


def fetch_green_jobs(snapshot: Path, out: Path, *, conclusions: Sequence[str] = DEFAULT_CONCLUSIONS,
                     since: str = "0000", until: str = "9999", workers: int = 4, max_candidates: int = GREEN_CANDIDATES_MAX,
                     fetch: Callable[[int], list[Row]] = _gh_run_jobs) -> Row:
    """Fetch the jobs of the later successful runs a red run needs to be resolved.

    For each red run it walks the later successful runs of the same workflow, oldest first, and
    fetches each one's jobs until one has every failed job's name succeeded (or ``max_candidates``
    are tried). The snapshot holds jobs only for red-scope runs, so this is the one network step the
    resolution needs; files already in ``out`` are not fetched again.
    """
    out.mkdir(parents=True, exist_ok=True)
    runs = [r for r in _load_runs(snapshot) if since <= r["created_at"][:10] <= until]
    by_wf: dict[str, list[Row]] = collections.defaultdict(list)
    for r in sorted(runs, key=lambda r: (r["created_at"], r["id"])):
        by_wf[r["name"]].append(r)
    count = {"fetched": 0}
    lock = threading.Lock()

    def jobs_of(g: Row) -> list[Row]:
        cached = _load_green_jobs(out, g["id"])
        if cached is not None:
            return cached
        got = fetch(g["id"])
        tmp = out / f"{g['id']}.json.{threading.get_ident()}.tmp"
        tmp.write_text(json.dumps(got), encoding="utf-8")
        os.replace(tmp, out / f"{g['id']}.json")
        with lock:
            count["fetched"] += 1
        return got

    def walk(red: Row) -> None:
        jobs = _load_jobs(snapshot, red["id"])
        names = [j["name"] for j in _failed_jobs(red, jobs or [])]
        if not names:
            return
        later = [g for g in by_wf[red["name"]] if g["conclusion"] == "success"
                 and (g["created_at"], g["id"]) > (red["created_at"], red["id"])]
        for g in later[:max_candidates]:
            if job_names_passed(jobs_of(g), names):
                return

    reds = [r for r in runs if r["conclusion"] in conclusions]
    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(walk, reds))
    return {"reds": len(reds), "fetched": count["fetched"]}


def resolve_green(red: Row, later: Sequence[Row], names: Sequence[str], hist: History,
                  green_dir: Path | None) -> tuple[Row | None, list[int]]:
    """The first later run that passed every failed job of ``red``, and the later runs it skipped.

    A candidate must be on the red sha or a descendant of it. A run is skipped when it is such a
    candidate but one of the names did not succeed in it. With no failed job names (a run whose jobs
    list is missing) nothing can be verified and the run stays unresolved.
    """
    skipped: list[int] = []
    if not names:
        return None, skipped
    for g in later[:GREEN_CANDIDATES_MAX]:
        if g["head_sha"] != red["head_sha"] and not hist.is_ancestor(red["head_sha"], g["head_sha"]):
            continue
        jobs = _load_green_jobs(green_dir, g["id"])
        if jobs is None:
            raise RuntimeError(f"no jobs file for later run {g['id']} in {green_dir}: "
                               "run fetch-green-jobs, then pass --green-jobs")
        if job_names_passed(jobs, names):
            return g, skipped
        skipped.append(g["id"])
    return None, skipped


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


def _failed_jobs(run: Row, jobs: Sequence[Row]) -> list[Row]:
    """Failed jobs of the run's final attempt; a re-run's jobs list holds every attempt's jobs."""
    attempt = run.get("run_attempt", 1)
    return sorted((j for j in jobs
                   if j.get("conclusion") == "failure" and j.get("run_attempt", attempt) == attempt),
                  key=lambda j: j["id"])


def _has_evidence(item: Row) -> bool:
    return item["kind"] != "step" or bool(item["failed_count"] or item["evidence"] or item["infra"])


def _mark_derived(items: list[Row]) -> None:
    """A step that reports on other steps (the gate verdict, a floor) is derived only when some other
    item in the run shows what failed: a named test, a failed-count, or an error with a reason."""
    for it in items:
        it["derived"] = False
    for it in items:
        if it["kind"] == "step" and DERIVED_STEP_RE.match(it["item"]):
            it["derived"] = any(
                o is not it and not (o["kind"] == "step" and DERIVED_STEP_RE.match(o["item"]))
                and _has_evidence(o) for o in items)


def _items_for_run(run: Row, jobs: Sequence[Row], snapshot: Path,
                   extra_logs: Sequence[Path] = ()) -> tuple[list[Row], Row]:
    """Failing items of a run, plus log coverage counts and the jobs whose log is missing."""
    failed = _failed_jobs(run, jobs)
    cov: Row = {"jobs": len(failed), "logs": 0, "extra": 0, "gaps": []}
    items: list[Row] = []
    for job in failed:
        steps = [s["name"] for s in job.get("steps", []) if s.get("conclusion") == "failure"]
        parsed = read_log(snapshot, run["id"], job["id"], extra_logs)
        if parsed is None:
            cov["gaps"].append({"run": run["id"], "job": job["id"], "kind": "log",
                                "steps": len(job.get("steps", []))})
        else:
            cov["logs"] += 1
            cov["extra"] += int(parsed["extra"])
        base = {"t": "item", "run": run["id"], "sha": run["head_sha"], "workflow": run["name"],
                "job": job["name"], "venue": venue(job.get("labels", [])), "job_id": job["id"],
                "infra": bool(parsed and parsed["infra"]), "errors": (parsed or {}).get("errors", [])[:2],
                "has_log": parsed is not None, "failed_count": (parsed or {}).get("failed_count", 0),
                "evidence": bool(parsed and parsed["evidence"])}
        emitted_tests = False
        if parsed and parsed["tests"]:
            first = steps[0] if steps else job["name"]
            for t in parsed["tests"]:
                kind = "java" if t["kind"] == "java" else "test"
                items.append({**base, "kind": kind, "step": first, "file": t["file"],
                              "item": t["name"], "msg": t["msg"]})
            emitted_tests = True
        for idx, step in enumerate(steps or [job["name"]]):
            if emitted_tests and idx == 0:
                continue
            items.append({**base, "kind": "step", "step": step, "file": "", "item": step,
                          "msg": (parsed or {}).get("first_e", "")})
    _mark_derived(items)
    return items, cov


def _placeholder_item(run: Row) -> Row:
    return {"t": "item", "run": run["id"], "sha": run["head_sha"], "workflow": run["name"],
            "job": PLACEHOLDER_JOB, "venue": "unknown", "job_id": 0, "infra": False, "errors": [],
            "has_log": False, "failed_count": 0, "evidence": False, "kind": "step",
            "step": PLACEHOLDER_JOB, "file": "", "item": PLACEHOLDER_JOB, "msg": "", "derived": False}


def _decided(runs: Sequence[Row]) -> list[Row]:
    return [{"t": "decided", "wf": r["name"], "id": r["id"], "s": r["created_at"],
             "e": r["updated_at"], "c": "s" if r["conclusion"] == "success" else "f"}
            for r in sorted(runs, key=lambda r: (r["created_at"], r["id"]))
            if r["conclusion"] in ("success", "failure")]


def snapshot_status(snapshot: Path, gaps: Sequence[Row]) -> Row:
    """Whether the snapshot is final: MANIFEST present, and every in-scope gap (a failed job with no
    log, a run with no jobs file) is one MANIFEST lists: a no-step job whose log is a 404 in
    ``missing.tsv``, or a run named in the MANIFEST's ``jobs_missing``."""
    manifest_path = snapshot / "MANIFEST.json"
    if not manifest_path.is_file():
        return {"manifest": False, "final": False, "gaps": len(gaps), "gaps_explained": 0,
                "buckets": ""}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tsv: dict[tuple[int, int, str], str] = {}
    tsv_path = snapshot / "missing.tsv"
    if tsv_path.is_file():
        for line in tsv_path.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[0].isdigit():
                tsv[(int(parts[0]), 0 if parts[1] == "-" else int(parts[1]), parts[2])] = (
                    parts[3] if len(parts) > 3 else "")
    jobs_missing = {int(x) for x in re.findall(r"run (\d+)", str(manifest.get("jobs_missing", "")))}
    explained = 0
    for g in gaps:
        if g["kind"] == "log":
            explained += int(g["steps"] == 0 and tsv.get((g["run"], g["job"], "log"), "").startswith("HTTP 404"))
        else:
            explained += int(g["run"] in jobs_missing and (g["run"], 0, "jobs") in tsv)
    return {"manifest": True, "final": explained == len(gaps), "gaps": len(gaps),
            "gaps_explained": explained, "buckets": str(manifest.get("red_scope_buckets", ""))}


def _fix_signal(it: Row, rng: Sequence[str], green: Row | None, hist: History) -> None:
    """Fix fields of one item: the first commit in the fix range that touched the failing file."""
    touching = [(n, c) for n, c in enumerate(rng, 1)
                if any(item_matches_path(it, f) for f in hist.files(c))]
    it["touched_test"] = None if green is None else bool(touching)
    it["fix_commit"] = touching[0][1][:12] if touching else None
    it["fix_distance"] = touching[0][0] if touching else None
    it["fix_touched_product"] = None if green is None else (
        any(is_product(f) for f in hist.files(touching[0][1])) if touching else False)
    it["fix_table_only"] = None
    it["fix_is_fix"] = bool(touching) and bool(
        FIX_SUBJECT_RE.match(hist.commits.get(touching[0][1], {}).get("subj", "")))
    if touching:
        path = next(f for f in hist.files(touching[0][1]) if item_matches_path(it, f))
        it["fix_table_only"] = hist.table_only(touching[0][1], path)


def extract(snapshot: Path, repo: Path, *, conclusions: Sequence[str] = DEFAULT_CONCLUSIONS,
            include_jobless: bool = False, since: str = WINDOW_START,
            until: str = WINDOW_END, extra_logs: Sequence[Path] = (),
            green_jobs: Path | None = None) -> list[Row]:
    """Build the extraction records (see the module docstring) from a snapshot and a repo."""
    runs = [r for r in _load_runs(snapshot) if since <= r["created_at"][:10] <= until]
    by_wf: dict[str, list[Row]] = collections.defaultdict(list)
    for r in sorted(runs, key=lambda r: (r["created_at"], r["id"])):
        by_wf[r["name"]].append(r)
    hist = History(repo)
    out_runs: list[Row] = []
    out_items: list[Row] = []
    cov = {"jobs": 0, "logs": 0, "extra": 0}
    gaps: list[Row] = []
    dropped_jobless = 0
    missing_jobs = 0
    for red in sorted((r for r in runs if r["conclusion"] in conclusions),
                      key=lambda r: (r["created_at"], r["id"])):
        jobs = _load_jobs(snapshot, red["id"])
        if jobs is None:
            missing_jobs += 1
            gaps.append({"run": red["id"], "job": 0, "kind": "jobs", "steps": 0})
            items, c = [_placeholder_item(red)], {"jobs": 0, "logs": 0, "extra": 0, "gaps": []}
        else:
            items, c = _items_for_run(red, jobs, snapshot, extra_logs)
        if not items and not include_jobless:
            dropped_jobless += 1
            continue
        for k in ("jobs", "logs", "extra"):
            cov[k] += c[k]
        gaps.extend(c["gaps"])
        later = [g for g in by_wf[red["name"]] if g["conclusion"] == "success"
                 and (g["created_at"], g["id"]) > (red["created_at"], red["id"])]
        names = [j["name"] for j in _failed_jobs(red, jobs)] if jobs is not None else []
        green, skipped = resolve_green(red, later, names, hist, green_jobs)
        rng = hist.range(red["head_sha"], green["head_sha"]) if green else []
        hist.load(rng)
        rec: Row = {"t": "run", "id": red["id"], "sha": red["head_sha"], "workflow": red["name"],
                    "conclusion": red["conclusion"], "attempt": red.get("run_attempt", 1),
                    "event": red.get("event", ""),
                    "created": red["created_at"], "ended": red["updated_at"],
                    "green_run": green["id"] if green else None,
                    "green_sha": green["head_sha"] if green else None,
                    "green_ended": green["updated_at"] if green else None,
                    "unresolved": green is None, "green_skipped": skipped,
                    "rerun_green": bool(green and green["head_sha"] == red["head_sha"]),
                    "fix_commits": [c[:12] for c in rng], "range_len": len(rng),
                    "range_ledger_edit": any(is_ledger_file(f) for c in rng for f in hist.files(c)),
                    "range_touched_product": any(is_product(f) for c in rng for f in hist.files(c)),
                    "fix_product_commit": any(
                        FIX_SUBJECT_RE.match(hist.commits.get(c, {}).get("subj", ""))
                        and any(is_product(f) for f in hist.files(c)) for c in rng)}
        out_runs.append(rec)
        for it in items:
            _fix_signal(it, rng, green, hist)
            out_items.append(it)
    if any(r["green_run"] and not r["rerun_green"] for r in out_runs) and not hist.commits:
        raise RuntimeError(f"no commit was read from {repo} although runs have later greens: "
                           "is --repo the nexus checkout with the window's history?")
    commits = [{"t": "commit", "sha": s[:12], "subj": v["subj"], "files": v["files"]}
               for s, v in sorted(hist.commits.items())]
    status = snapshot_status(snapshot, gaps)
    meta = {"t": "meta", "window": [since, until], "conclusions": list(conclusions),
            "include_jobless": include_jobless, "runs_in_window": len(runs),
            "red_runs": len(out_runs), "dropped_jobless": dropped_jobless,
            "runs_missing_jobs_file": missing_jobs, "failed_jobs": cov["jobs"],
            "failed_jobs_with_log": cov["logs"], "logs_from_extra_roots": cov["extra"],
            "manifest": status["manifest"], "final": status["final"],
            "gaps": status["gaps"], "gaps_explained": status["gaps_explained"],
            "scope_buckets": status["buckets"]}
    return [meta] + out_runs + out_items + commits + _decided(runs)


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

#: What a named test with no tied fix, no rerun and no timing message is: it went green again and
#: nothing in the record says whether it was flaky, ambient or fixed in src/ (hand samples of the
#: runs under the earlier stale and flake labels agreed on 6 of 14 and 11 of 14, each a different
#: reading of the same runs).
RESIDUAL: tuple[str, str] = (UNATTRIBUTED, "unattributed:resolved-without-test-fix")


def is_ledger_file(path: str) -> bool:
    """A ledger, ceiling or allowlist data file (an edit to one is not a guard catch)."""
    return path in NON_GUARD_FILES or bool(NON_GUARD_RE.search(path))



def _accounting_msg(msg: str) -> bool:
    return bool(META_MSG_RE.search(msg) or COUNT_PIN_RE.search(msg))


def label_item(item: Row, run: Row) -> tuple[str, str]:
    """(category, rule) for one failing item. First matching rule wins; see the module docstring."""
    named = item["kind"] != "step"
    stem = item["file"] if item["kind"] == "java" else Path(item["file"]).stem
    if named and not item["file"]:
        # a Windows log names the test but not its file: use the test function's name
        stem = re.split(r"\[", item["item"].rsplit(".", 1)[-1])[0]
    job = item["job"]
    if job == PLACEHOLDER_JOB:
        return UNLABELLED, "unlabelled:jobs-missing"
    if not named and BUILD_STEP_RE.search(item["item"]):
        return BUILD, "build:step-name"
    if not named and (item["infra"] or INFRA_STEP_RE.search(item["item"])):
        return FLAKE, "flake:infra-signature" if item["infra"] else "flake:infra-step"
    if any(job.startswith(j) or j in job for j in ACCOUNTING_JOBS):
        return ACCOUNTING, "accounting:job"
    if not named:
        if ACCOUNTING_STEP_RE.search(item["item"]):
            return ACCOUNTING, "accounting:step"
        if item.get("failed_count"):
            if TIMING_MSG_RE.search(item["msg"]):
                return FLAKE, "flake:timing-message"
            if _accounting_msg(item["msg"]):
                return ACCOUNTING, "accounting:meta-msg"
            if item["venue"] == "windows":
                return STALE, "stale:windows-venue-failed-count"
            return STALE, "stale:unnamed-test-failures"
        if run["rerun_green"]:
            return FLAKE, "flake:rerun-green"
        return UNLABELLED, "unlabelled:no-rule" if item["has_log"] else "unlabelled:no-log"
    if TIMING_MSG_RE.search(item.get("msg", "")):
        return FLAKE, "flake:timing-message"
    if _accounting_msg(item.get("msg", "")):
        return ACCOUNTING, "accounting:meta-msg"
    func = re.split(r"\[", item["item"].rsplit(".", 1)[-1])[0]
    if META_FILE_RE.search(stem) or ALLOWLISTED_FUNC_RE.search(func):
        return ACCOUNTING, "accounting:meta-file"
    if item["file"].startswith("tests/scripts/") or HARNESS_FILE_RE.search(stem):
        return HARNESS, "harness:file"
    if run["rerun_green"]:
        return FLAKE, "flake:rerun-green"
    if not item["file"] and item["venue"] == "windows":
        return STALE, "stale:windows-venue"
    if run["unresolved"]:
        return UNATTRIBUTED, "unattributed:no-verified-green"
    if item["fix_distance"] is not None:
        if item["fix_table_only"]:
            return ACCOUNTING, "accounting:fix-table-edit"
        if item["fix_touched_product"] and item["fix_is_fix"]:
            return PRODUCT, "product:fix-touched-src"
        return STALE, "stale:fix-touched-test"
    return RESIDUAL


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
        hit = any(f.startswith(tuple(paths)) and not is_ledger_file(f) for f in files)
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


def commit_mix(repo: Path, since: str, until: str, ref: str = "origin/develop") -> dict[str, int]:
    """Non-merge commit mix over a window: product, tests-only, ledger-only, other."""
    out = _git(repo, "log", "--no-merges", "--no-renames", "--name-only", "--format=@@%H",
               f"--since={since}", f"--until={until}T23:59:59", ref)
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
    if not mix["total"]:
        raise RuntimeError(f"no commit in {since}..{until} on origin/develop of {repo}")
    return dict(mix)


# --- report --------------------------------------------------------------------------------


def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:.1f}%" if d else "n/a"


def snapshot_banner(meta: Mapping[str, Any]) -> str | None:
    """A PARTIAL line unless the snapshot is final (MANIFEST present, every gap one it lists)."""
    if meta.get("final"):
        return None
    return (f"PARTIAL: MANIFEST {'present' if meta.get('manifest') else 'absent'}; "
            f"{meta.get('gaps', 0) - meta.get('gaps_explained', 0)} of {meta.get('gaps', 0)} missing "
            f"logs or jobs files are not listed in it; logs read for {meta.get('failed_jobs_with_log')} "
            f"of {meta.get('failed_jobs')} failed jobs")


def accounting_caveat(an: Mapping[str, Any]) -> str:
    """The accounting share as a range, with the product-catch caveat (258-research-15)."""
    cats = category_counts(an)
    total = len(an["runs"])
    attributed = total - cats.get(UNATTRIBUTED, 0) - cats.get(UNLABELLED, 0)
    acc = cats.get(ACCOUNTING, 0)
    items = [i for i in an["items"] if i["category"] == ACCOUNTING and not i["derived"]]
    tied = [i for i in items if i.get("fix_distance") is not None]
    prod = [i for i in tied if i.get("fix_touched_product")]
    return (f"accounting share {_pct(acc, total)} of all {total} red runs (floor) to {_pct(acc, attributed)} "
            f"of the {attributed} attributed runs (unattributed and unlabelled runs left out); where the "
            "unattributed runs fall is not known. Caveat (258-research-15): accounting is what a check "
            f"guards, not whether a given red was a product defect; {len(items)} accounting items, "
            f"{len(tied)} followed by a fix commit that touched the failing file, {len(prod)} of those "
            "by a commit that also touched src/ or service/src/main/ (research-15 found 3 real "
            "product catches among 18 such items)")


def unattributed_caveat(an: Mapping[str, Any]) -> str:
    """What the unattributed runs are, and what bracket they put on flake and stale."""
    cats = category_counts(an)
    runs = [r for r in an["runs"].values() if r["category"] == UNATTRIBUTED]
    n = len(runs)
    rules = collections.Counter(r for run in runs for r in run["rules"])
    product = sum(1 for r in runs if r["fix_product_commit"])
    short = sum(1 for r in runs if r["range_len"] <= 1)
    return (f"unattributed {n} runs {dict(sorted(rules.items()))}: no commit touched the failing test "
            "before a later run verifiably passed its job, or no such run exists in the window. The "
            "record cannot say whether each was flaky, ambient-coupled or fixed in src/ without touching "
            f"the test; {product} have a fix-typed commit touching src/ or service/src/main in their fix "
            f"range (product candidates), {short} a fix range of one commit or none. Counted in none of "
            f"strand B's six; flake is {cats.get(FLAKE, 0)}..{cats.get(FLAKE, 0) + n} and stale "
            f"{cats.get(STALE, 0)}..{cats.get(STALE, 0) + n} by that bracket")


def render_report(an: Mapping[str, Any], top: int = 15, repo: Path | None = None) -> str:
    meta = an["meta"]
    total = len(an["runs"])
    lines = [f"red runs: {total}  window {meta.get('window')}  conclusions {meta.get('conclusions')}"]
    banner = snapshot_banner(meta)
    if banner:
        lines.append(banner)
    lines += ["", "by workflow"]
    lines += [f"  {k:34s} {v:5d}  {_pct(v, total)}" for k, v in workflow_counts(an).most_common()]
    events = collections.Counter(r.get("event", "") for r in an["runs"].values())
    lines += ["", "by event (information): " + ", ".join(f"{k} {v}" for k, v in events.most_common()),
              f"snapshot scope buckets (information): {meta.get('scope_buckets') or 'n/a'}"]
    lines += ["", f"by check (top {top}; distinct red runs, derived items excluded)"]
    lines += [f"  {k:44s} {v:5d}" for k, v in check_counts(an).most_common(top)]
    cats = category_counts(an)
    lines += ["", "by category"]
    for c in (*PRIORITY, UNLABELLED):
        lines.append(f"  {c:16s} {cats.get(c, 0):5d}  {_pct(cats.get(c, 0), total)}")
    lines += ["", accounting_caveat(an), "", unattributed_caveat(an), "",
              "by rule (runs whose category rested on the rule)"]
    rules = collections.Counter(r for run in an["runs"].values() for r in run["rules"])
    lines += [f"  {k:40s} {v:5d}" for k, v in sorted(rules.items())]
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


def sample_runs(an: Mapping[str, Any], rule: str, n: int, seed: int) -> list[Row]:
    """A deterministic draw of up to ``n`` red runs whose category rested on ``rule``, for hand checks."""
    cands = sorted(rid for rid, run in an["runs"].items() if rule in run["rules"])
    picked = sorted(random.Random(seed).sample(cands, min(n, len(cands))))
    out: list[Row] = []
    for rid in picked:
        it = next(i for i in an["by_run"][rid] if i["rule"] == rule and not i["derived"])
        fix = an["commits"].get(it.get("fix_commit") or "", {})
        out.append({"run": rid, "workflow": it["workflow"], "job": it["job"], "item": it["item"],
                    "file": it["file"], "msg": it["msg"], "rule": rule, "candidates": len(cands),
                    "fix_commit": it.get("fix_commit"), "fix_subject": fix.get("subj", ""),
                    "fix_distance": it.get("fix_distance"), "fix_product": it.get("fix_touched_product"),
                    "fix_table_only": it.get("fix_table_only"),
                    "run_fix_commits": an["runs"][rid]["range_len"]})
    return out


def render_sample(an: Mapping[str, Any], rule: str, n: int, seed: int) -> str:
    rows = sample_runs(an, rule, n, seed)
    head = f"sample of {len(rows)} runs for rule {rule} (seed {seed}; {rows[0]['candidates'] if rows else 0} candidates)"
    body = [f"{r['run']} | {r['workflow']} | {r['job'][:32]} | {(r['file'] + '::' if r['file'] else '')}"
            f"{r['item'][:50]} | msg: {r['msg'][:90]!r} | fix {r['fix_commit']} d={r['fix_distance']} "
            f"of {r['run_fix_commits']} prod={r['fix_product']} table={r['fix_table_only']} "
            f"{r['fix_subject'][:60]!r}" for r in rows]
    return "\n".join([head, *body])


def within(ours: int, want: int, rel: float, floor: int = 0) -> bool:
    """Tolerance math of the MVV: |ours - want| <= max(rel * want, floor)."""
    return abs(ours - want) <= max(rel * want, floor)


def mvv_lines(an: Mapping[str, Any]) -> list[tuple[bool | None, str]]:
    """(verdict, text) per MVV line; a verdict of None is information and never fails the MVV."""
    out: list[tuple[bool | None, str]] = []
    total = len(an["runs"])
    out.append((within(total, STRAND_B_TOTAL, WORKFLOW_TOL),
                f"total red runs: ours {total} vs strand B {STRAND_B_TOTAL} (2%)"))
    wf = workflow_counts(an)
    for k, want in STRAND_B_WORKFLOWS.items():
        out.append((within(wf.get(k, 0), want, WORKFLOW_TOL), f"workflow {k}: ours {wf.get(k, 0)} vs {want} (2%)"))
    ck = check_counts(an)
    for k, want in STRAND_B_CHECKS.items():
        out.append((within(ck.get(k, 0), want, *CHECK_TOL),
                    f"check {k}: ours {ck.get(k, 0)} vs {want} (max(10%,3))"))
    cats = category_counts(an)
    for k, want in STRAND_B_CATEGORIES.items():
        ok = within(cats.get(k, 0), want, *CATEGORY_TOL)
        line = f"category {k}: ours {cats.get(k, 0)} vs {want} (max(10%,3))"
        if not ok:
            rules = collections.Counter(r for run in an["runs"].values() if run["category"] == k
                                        for r in run["rules"])
            line += f"  rules: {dict(rules.most_common(6))}"
        out.append((ok, line))
    n_un = cats.get(UNATTRIBUTED, 0)
    out.append((None, f"unattributed {n_un} runs, counted in none of strand B's six: flake "
                      f"{cats.get(FLAKE, 0)}..{cats.get(FLAKE, 0) + n_un} (strand B {STRAND_B_CATEGORIES[FLAKE]}), "
                      f"stale {cats.get(STALE, 0)}..{cats.get(STALE, 0) + n_un} (strand B "
                      f"{STRAND_B_CATEGORIES[STALE]}); see the unattributed caveat in the report"))
    unl = [run for run in an["runs"].values() if run["category"] == UNLABELLED]
    gaps = [r for r in unl if set(r["rules"]) <= {"unlabelled:jobs-missing", "unlabelled:no-log"}]
    out.append((None, f"unlabelled runs: {len(unl)}; {len(gaps)} are gaps the MANIFEST lists, "
                      f"{len(unl) - len(gaps)} have a log with no failing-test name and no rule"))
    return out


def render_mvv(an: Mapping[str, Any]) -> tuple[str, bool]:
    """The MVV text and whether it passes: every line passes and the snapshot is final."""
    banner = snapshot_banner(an["meta"])
    lines = mvv_lines(an)
    body = [f"{'INFO' if ok is None else 'PASS' if ok else 'FAIL'}  {text}" for ok, text in lines]
    return ("\n".join(([banner] if banner else []) + body),
            all(ok is not False for ok, _ in lines) and banner is None)


# --- candidates ----------------------------------------------------------------------------


def _test_rows(an: Mapping[str, Any], category: str) -> list[Row]:
    rows: dict[str, Row] = {}
    for it in an["items"]:
        if it["derived"] or it["category"] != category or it["kind"] == "step":
            continue
        nodeid = f"{it['file']}::{it['item']}" if it["file"] else it["item"]
        row = rows.setdefault(nodeid, {"kind": it["kind"], "runs": set(), "dates": [],
                                       "venues": set(), "rules": set()})
        row["runs"].add(it["run"])
        row["dates"].append(an["runs"][it["run"]]["created"][:10])
        row["venues"].add(it["venue"])
        row["rules"].add(it["rule"])
    return [{"nodeid": k, "kind": v["kind"], "red_runs": len(v["runs"]), "first_red": min(v["dates"]),
             "last_red": max(v["dates"]), "venues": sorted(v["venues"]), "rule": "|".join(sorted(v["rules"])),
             "run_ids": sorted(v["runs"])} for k, v in sorted(rows.items())]


def candidates_report(an: Mapping[str, Any], header: Row) -> Row:
    """The named tests labelled flake_infra, and those labelled unattributed, as one JSON-able dict.

    One row per test (a pytest node id with Windows paths normalised, or ``Class::method`` for Java;
    a Windows test named only by its section header has no file part). A failing step that names no
    test has no row. ``unattributed`` is a separate array so the reader can decide whether it counts.
    """
    flake, unattributed = _test_rows(an, FLAKE), _test_rows(an, UNATTRIBUTED)
    header = {**header, "red_runs": len(an["runs"]), "candidate_tests": len(flake),
              "unattributed_tests": len(unattributed)}
    return {"header": header, "candidates": flake, "unattributed": unattributed}


def write_candidates(report: Row, path: Path) -> None:
    """JSON; gzip with a zeroed mtime and no stored name when the path ends ``.gz``.

    The repo-wide source scans read every non-markdown UTF-8 file, so a committed candidates file
    that names routes through test node ids is written compressed.
    """
    text = (json.dumps(report, indent=1, sort_keys=True) + "\n").encode("utf-8")
    if path.suffix != ".gz":
        path.write_bytes(text)
        return
    with path.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0,
                                                compresslevel=9) as gz:
        gz.write(text)


def candidates_header(an: Mapping[str, Any], extraction: Path, archive_sha256: str) -> Row:
    """Header of the candidates file; ``script_sha256`` hashes this file, so a rebase does not stale it."""
    meta = an["meta"]
    return {"window": meta.get("window"), "snapshot_final": bool(meta.get("final")),
            "archive_sha256": archive_sha256, "extraction": extraction.name,
            "extraction_sha256": hashlib.sha256(extraction.read_bytes()).hexdigest(),
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


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
    ex.add_argument("--extra-logs", type=Path, action="append", default=[],
                    help="fallback log root (same <run>/<job>.log layout); repeatable")
    ex.add_argument("--green-jobs", type=Path, default=None,
                    help="directory of <run>.json jobs of later successful runs (see fetch-green-jobs)")
    ex.add_argument("--since", default=WINDOW_START)
    ex.add_argument("--until", default=WINDOW_END)
    fg = sub.add_parser("fetch-green-jobs", help="fetch the jobs of the later successful runs")
    fg.add_argument("--snapshot", type=Path, required=True)
    fg.add_argument("--out", type=Path, required=True)
    fg.add_argument("--workers", type=int, default=4)
    fg.add_argument("--since", default=WINDOW_START)
    fg.add_argument("--until", default=WINDOW_END)
    rp = sub.add_parser("report")
    rp.add_argument("--extraction", type=Path, required=True)
    rp.add_argument("--top", type=int, default=15)
    rp.add_argument("--repo", type=Path, default=None, help="adds the commit-mix figures")
    rp.add_argument("--sample", nargs=2, metavar=("RULE", "N"),
                    help="print a deterministic draw of N runs labelled by RULE for hand checking")
    rp.add_argument("--seed", type=int, default=1, help="seed for --sample")
    cd = sub.add_parser("candidates", help="write the flake and unattributed named tests as JSON")
    cd.add_argument("--extraction", type=Path, required=True)
    cd.add_argument("--out", type=Path, required=True)
    cd.add_argument("--archive-sha256", default=PHASE0_ARCHIVE_SHA256)
    mv = sub.add_parser("mvv")
    mv.add_argument("--extraction", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.cmd == "extract":
        recs = extract(args.snapshot, args.repo, conclusions=tuple(args.conclusions.split(",")),
                       include_jobless=args.include_jobless, since=args.since, until=args.until,
                       extra_logs=args.extra_logs, green_jobs=args.green_jobs)
        write_extraction(recs, args.out)
        sys.stdout.write(f"wrote {args.out}: {recs[0]}\n")
        return EXIT_OK
    if args.cmd == "fetch-green-jobs":
        res = fetch_green_jobs(args.snapshot, args.out, workers=args.workers, since=args.since,
                               until=args.until)
        sys.stdout.write(f"fetched {res['fetched']} runs' jobs for {res['reds']} red runs\n")
        return EXIT_OK
    an = analyze(read_extraction(args.extraction))
    if args.cmd == "candidates":
        report = candidates_report(an, candidates_header(an, args.extraction, args.archive_sha256))
        write_candidates(report, args.out)
        h = report["header"]
        sys.stdout.write(f"wrote {args.out}: {h['candidate_tests']} flake tests, "
                         f"{h['unattributed_tests']} unattributed tests\n")
        return EXIT_OK
    if args.cmd == "report" and args.sample:
        if not sample_runs(an, args.sample[0], 1, args.seed):
            known = sorted({r for run in an["runs"].values() for r in run["rules"]})
            sys.stderr.write(f"no run is labelled by rule {args.sample[0]!r}; rules: {known}\n")
            return EXIT_FAIL
        sys.stdout.write(render_sample(an, args.sample[0], int(args.sample[1]), args.seed) + "\n")
        return EXIT_OK
    if args.cmd == "report":
        sys.stdout.write(render_report(an, args.top, args.repo) + "\n")
        return EXIT_OK
    text, ok = render_mvv(an)
    sys.stdout.write(text + "\n")
    return EXIT_OK if ok else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
