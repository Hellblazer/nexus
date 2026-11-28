#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The 2-week check review and the step 7 batch proposal (RDR-258 Phase 1 steps 5 and 7, nexus-2jomg.2).

    check_review.py record --since DATE --until DATE [--repo .] [--rev origin/develop] [--artifacts DIR | --slug OWNER/REPO]
                           [--prior RECORD ...] [--format md|json] [--quarantine-mark NAME] [--numeric PATH ...]
    check_review.py deletion-plan RECORD [--repo .]
    check_review.py batch --report FILE [--repo .] [--size N] [--demoted CHECK ...] [--values FILE] [--target 0.30]
                          [--format md|json]

``record`` writes the review record, one markdown document ready to paste into T2 ``nexus/check-value-review-<date>``
(its last block is the whole record as JSON; ``--format json`` prints only that). A check's unit is its ``class_a`` tests
(a module can hold one marked test among twenty). For each class-A check in report mode the record gives the clock (the
oldest commit of the current unbroken stretch that carries the mark, so a re-promotion resets it; its date is the first
develop push CI run whose head contains it, else ``max(committer date, first usable snapshot)``, and the record says
which), the days since, the failing items per run, the guard catches, the header at demotion and at tip, the evidence
coverage, and for a numeric check its value, headroom and trend (``storage_touch value=...`` lines).

Evidence is data, not declared windows. A snapshot (one develop push's ``class-a-items`` artifact) counts for a check only
when the payload's ``required`` entry for that check's prefix examined at least one test. Coverage is the share of days
since the clock with a usable snapshot, and the largest gap between them; a check is due only at coverage of
``COVERAGE_MIN_PCT`` or more and a gap of ``GAP_MAX_DAYS`` or less. Expired artifacts in the window and a capped run list
are holes. ``--prior`` chains earlier records' usable days (artifacts live 30 days; a review window is 14).

A guard catch is rule (b): an item that failed in one usable snapshot and not in the next (the line number in a
``path:line`` id is ignored), across a range that touched a guarded path (matched on a path separator) and no ledger,
ceiling or allowlist file (``red_run_report.is_ledger_file``, plus a conftest set the check reads); the failing count must
also fall, or the failing path be among the files fixed. A mixed fix is no catch and is listed. A reviewer may mark a
listed catch ``non_substantive`` with a reason in a record; a later run honours it.

Due list: every check at 60 days or more with no catch and complete evidence. ``disposition: delete`` by default (Sam,
2026-11-27, retirement by default); ``keep`` needs a non-empty ``keep_reason``; ``retire-early`` (the cause is removed)
may be proposed at any age and needs the cause-fix evidence listed under ``deletion-plan``. A check not due for an
evidence reason and past ``STUCK_AFTER_DAYS`` goes to ``stuck`` (delete, restart the clock, or keep with a reason).

``deletion-plan`` reads a record whose dispositions a human has settled. It cross-checks the record (every due check has an
entry, every entry names a due check unless it is retire-early, something is planned) and prints, per ``delete`` or
``retire-early`` entry, the marked tests to remove and the unmarked tests that stay (``git rm`` of a module only when every
test in it is class_a, or ``module_whole: true`` with a ``module_whole_reason``), the consumers by ``git grep`` of the
entry's ``names`` with a before count per name, and the exact after-check. It edits nothing. A ``retire-early`` needs a
full ``cause_fix_sha`` that is an ancestor of the record's rev, is not the check's demotion or birth commit and touches
its guards or ``behaviour_paths``; non-empty ``leaves_unguarded``, ``reopen_condition`` and ``replacement_guard``; and an
``evidence`` object (``guards_readings``, ``red_classification``, ``consumer_names``, optionally ``ablation``).

``batch`` proposes the next step 7 batch from the Phase 0 baseline (the red-run extraction, or a small JSON form): class-A
checks not yet demoted and not owned by Phase 2 to 4 (an explicit list, ``OWNED_CHECKS``), largest baseline red-run count
first, each with its alarm line (numeric checks only, from ``--values``: a PROPOSAL) and the test-level and push-gate
changes its consumers need, stopping when the projected accounting share falls under ``--target`` (exact integer
arithmetic). A check with no ``class:`` header is listed as unclassified and never batched.

The header reader here is a minimal copy of the grammar in ``scripts/check_headers.py`` on feature/rdr258-check-headers
(not on this base). To swap when it lands: delete ``parse_header``, ``Header`` and ``_HEADER_LINE`` here, import
``parse_header`` from ``check_headers`` (it returns a ``HeaderResult`` whose ``.header`` has ``owner``, ``guard_ids``,
``guard_paths``, ``klass``), adapt the three call sites in ``evaluate`` and ``build_batch``, and keep
``tests/scripts/test_check_review.py::test_the_header_grammar_samples`` running over both readers.

Keeps no state: every output is recomputed from git, the artifacts and the baseline. Standard library and git only;
``gh api`` is read-only (``--method GET``), one call per page, no polling. python3 >= 3.11.
"""
from __future__ import annotations

import argparse
import ast
import collections
import gzip
import io
import json
import math
import re
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))   # the sibling red_run_report, as that script finds its own
import red_run_report as rr  # noqa: E402

Row = dict[str, Any]

RETIRE_AFTER_DAYS = 60
STUCK_AFTER_DAYS = 75
COVERAGE_MIN_PCT = 80
GAP_MAX_DAYS = 3
RUN_LIST_CAP = 1000
QUARANTINE_MAX_DAYS = 14
DEFAULT_SLUG = "Hellblazer/nexus"
ARTIFACT_PREFIX = "class-a-items"
LOG_JOB = "pytest-gate"
TARGET_SHARE = "0.30"
RDR_FIGURE = {"accounting_runs_max": 122, "other_runs": 286, "accounting_runs": 501, "source": "RDR-258 Phase 1 step 7 (strand B baseline)"}
DISPOSITIONS = ("delete", "retire-early", "keep")
RECORD_MARK = "<!-- check-review-record:json -->"
#: Numeric report-mode checks have no marker to find; a module listed here (or passed as --numeric) that carries an
#: ``alarm_line`` key is one. Phase 3 adds its ceilings here.
NUMERIC_MODULES = ("tests/_storage_touch_fan_in.py",)
#: What steps 1 to 4 of Phase 1 demote, named as the red-run report names them.
STEPS_1_4 = frozenset({"storage-touch ratchet", "docs_reference_rot", "mode-declarations census", "module_seam"})
#: Checks another phase owns, by exact check name or module stem (a pattern would catch ``ddl`` in ``middleware``).
OWNED_CHECKS: dict[str, str] = {
    **dict.fromkeys(("wire pairing", "wire_contract_pairing_lint", "check_wire_contract_pairing"), "Phase 2 (wire pairing)"),
    **dict.fromkeys(("storage_touch_guard_report", "_storage_touch_fan_in", "ddl-bytes ratchet", "ddl_ratchet", "ddl_uncounted_clusters_lint",
                     "file_size_ratchet", "file-size ratchet", "context_budgets", "sleep_ratchet", "wal_ceiling"),
                    "Phase 3 (ceilings and budgets)"),
    **dict.fromkeys(("plugin drift ledger", "plugin_ledger", "ci_release_ledger_gate", "cloud_leg", "check_release_ci_evidence",
                     "changelog_validate_precondition_lint", "changelog_fold", "push_check_gate"), "Phase 4 (ledgers)"),
}
#: Check names the red-run report derives from a step or job, not a file: the module that holds each.
STEP_CHECK_MODULES = {
    "storage-touch ratchet": "tests/_storage_touch_fan_in.py", "ddl-bytes ratchet": "scripts/ddl_ratchet.py",
    "plugin drift ledger": "scripts/plugin_ledger.py",
}
#: Where a blocking path to a check can hide besides its own module (the files a demotion has to change).
CONSUMER_FILES = ("scripts/push_check_gate.py", "scripts/select_docs_reader_tests.py", "scripts/meta_tests.py",
                  ".nexus/procedures/preflight.lua", "scripts/lab-entry-conditions.toml")
VALUE_RE = re.compile(r"\b(\w+) value=(-?\d+(?:\.\d+)?)\b.*?\balarm_line=(-?\d+(?:\.\d+)?)")
_HEADER_LINE = re.compile(r"^(?:#|//)\s*(owner|guards|class)\s*:\s*(.*?)\s*$")
_LINE_NO = re.compile(r":\d+(?=:|$)")
#: The exclusion, allowlist, ceiling and exception sets a check module names (``_MODE_LINT_EXCLUDE_FILES``): what it reads from a conftest.
_EXCLUSION_NAME = re.compile(r"\b_[A-Z0-9_]*(?:EXCLUDE|ALLOWLIST|CEILING|EXCEPT)[A-Z0-9_]*\b")


class ReviewError(Exception):
    """A failure of the tool's input or the repository, not a refusal of a plan."""


def _say(text: str = "") -> None:
    sys.stdout.write(text + "\n")


# -- git ------------------------------------------------------------------------------------------------------------------


def _git(repo: Path, *args: str, ok: Sequence[int] = (0,)) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    if done.returncode not in ok:
        raise ReviewError(f"git {' '.join(args[:3])}: {done.stderr.strip() or done.returncode}")
    return done.stdout


def _git_ok(repo: Path, *args: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False).returncode == 0


def _show(repo: Path, rev: str, path: str) -> str | None:
    done = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{path}"], capture_output=True, text=True, check=False)
    return done.stdout if done.returncode == 0 else None


def _utc_date(stamp: str) -> date:
    return datetime.fromisoformat(stamp).astimezone(timezone.utc).date()


def commit_date(repo: Path, sha: str) -> date:
    return _utc_date(_git(repo, "log", "-1", "--format=%cI", sha).strip())


def _ledger(path: str, own: Iterable[str] = ()) -> bool:
    """Ledger, ceiling and allowlist files: the Phase 0 report's list (read from there so the two cannot drift), plus the
    conftest or data files the check itself reads its exclusions from."""
    return rr.is_ledger_file(path) or path in set(own)


def under(path: str, guard: str) -> bool:
    """``path`` is ``guard`` or lies under it: matched on a path separator, so ``src/foo`` does not guard ``src/foobar``."""
    g = guard.rstrip("/")
    return path == g or path.startswith(g + "/")


def under_any(path: str, guards: Iterable[str]) -> bool:
    return any(under(path, g) for g in guards)


# -- the check header ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Header:
    owner: str
    guard_ids: tuple[str, ...]
    guard_paths: tuple[str, ...]
    klass: str

    def as_row(self) -> Row:
        return {"owner": self.owner, "guard_ids": list(self.guard_ids), "guard_paths": list(self.guard_paths), "class": self.klass}


def parse_header(text: str) -> tuple[Header | None, list[str]]:
    """The ``owner:``, ``guards:`` and ``class:`` comment lines opening a check module, and what is wrong with them.

    ``guards:`` is ``<id>[, <id>...]; <path>[, <path>...]``. Nothing here fails a build: every problem is a finding for the
    record (RDR-258 Technical Design: a gate to police the gates would repeat Gap 2). The header is None when any line is
    missing or unreadable."""
    seen: dict[str, str] = {}
    findings: list[str] = []
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith(("#", "//")):
            break
        m = _HEADER_LINE.match(line.strip())
        if m and m.group(1) in seen:
            findings.append(f"{m.group(1)}: duplicate line")
        elif m:
            seen[m.group(1)] = m.group(2)
    for field_name in ("owner", "guards", "class"):
        if not seen.get(field_name):
            findings.append(f"{field_name}: missing")
    ids: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    if seen.get("guards"):
        left, sep, right = seen["guards"].partition(";")
        ids = tuple(p.strip() for p in left.split(",") if p.strip())
        paths = tuple(p.strip() for p in right.split(",") if p.strip())
        if not sep or not ids or not paths:
            findings.append("guards: needs '<id>[, <id>...]; <path>[, <path>...]'")
    if seen.get("class") and seen["class"] not in ("U", "A"):
        findings.append(f"class: must be U or A, got {seen['class']!r}")
    if findings:
        return None, findings
    return Header(seen["owner"], ids, paths, seen["class"]), []


def read_header_file(path: Path) -> Header | None:
    try:
        return parse_header(path.read_text(encoding="utf-8"))[0]
    except (OSError, UnicodeDecodeError):
        return None


# -- the class-A checks in report mode --------------------------------------------------------------------------------------


def _mentions_class_a(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Attribute) and n.attr == "class_a" and isinstance(n.value, ast.Attribute) and n.value.attr == "mark"
               for n in ast.walk(node))


def _parse(text: str) -> ast.Module | None:
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


def applies_class_a(text: str) -> bool:
    """True when the module applies ``pytest.mark.class_a`` (a decorator or ``pytestmark``), not when it only names it."""
    tree = _parse(text)
    return tree is not None and _mentions_class_a(tree)


def _module_marks(tree: ast.Module) -> list[ast.expr]:
    return [n.value for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in n.targets)]


def class_a_tests(text: str) -> tuple[list[str], list[str]]:
    """(marked, unmarked) test names of a module: a module-level ``pytestmark``, a class decorator or a test's own decorator
    applies ``class_a``. Methods read ``Class::test``."""
    tree = _parse(text)
    if tree is None:
        return [], []
    module_marked = any(_mentions_class_a(m) for m in _module_marks(tree))
    marked: list[str] = []
    unmarked: list[str] = []

    def add(name: str, node: ast.AST, inherited: bool) -> None:
        own = any(_mentions_class_a(d) for d in getattr(node, "decorator_list", []))
        (marked if inherited or own else unmarked).append(name)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            add(node.name, node, module_marked)
        elif isinstance(node, ast.ClassDef):
            class_marked = module_marked or any(_mentions_class_a(d) for d in node.decorator_list)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name.startswith("test"):
                    add(f"{node.name}::{sub.name}", sub, class_marked)
    return marked, unmarked


def module_level_lint(text: str) -> bool:
    """True when every test of the module is in the lint bucket by a module-level ``pytestmark`` (the module is the check)."""
    tree = _parse(text)
    return tree is not None and any(isinstance(n, ast.Attribute) and n.attr in ("lint", "census") for m in _module_marks(tree) for n in ast.walk(m))


def has_alarm_line(path: str, text: str) -> bool:
    """True when the module carries an ``alarm_line`` key or identifier: a toml key, or a python name, attribute, argument or
    short string (a docstring that names it does not count)."""
    if path.endswith(".toml"):
        def walk(d: Any) -> bool:
            return isinstance(d, dict) and any("alarm_line" in k or walk(v) for k, v in d.items())
        try:
            return walk(tomllib.loads(text))
        except tomllib.TOMLDecodeError:
            return False
    tree = _parse(text)
    if tree is None:
        return False
    for n in ast.walk(tree):
        name = n.id if isinstance(n, ast.Name) else n.attr if isinstance(n, ast.Attribute) else n.arg if isinstance(n, ast.arg) \
            else n.name if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) else n.value if isinstance(n, ast.Constant) and isinstance(n.value, str) \
            else ""
        if "alarm_line" in name and re.fullmatch(r"\w+", name):
            return True
    return False


@dataclass(frozen=True)
class Module:
    path: str
    numeric: bool = False

    @property
    def check(self) -> str:
        return Path(self.path).stem

    def in_report_mode(self, path: str, text: str) -> bool:
        return has_alarm_line(path, text) if self.numeric else applies_class_a(text)


def discover(repo: Path, rev: str, numeric: Iterable[str] = NUMERIC_MODULES) -> list[Module]:
    out = _git(repo, "grep", "-l", "-E", r"mark\.class_a", rev, "--", "tests", "scripts", ok=(0, 1))
    found = []
    for line in out.splitlines():
        path = line.partition(":")[2]
        text = _show(repo, rev, path)
        if text and applies_class_a(text):
            found.append(Module(path))
    for path in numeric:
        text = _show(repo, rev, path)
        if text and has_alarm_line(path, text):
            found.append(Module(path, numeric=True))
    return sorted(found, key=lambda m: m.path)


def stretch_start(repo: Path, rev: str, mod: Module) -> tuple[str, str] | None:
    """(sha, path at that sha) of the oldest commit of the current unbroken stretch in which the module is in report mode.
    ``--follow`` carries a rename; a stretch broken by a revert starts again at the re-promotion."""
    out = _git(repo, "log", "--follow", "--format=@%H", "--name-only", rev, "--", mod.path)
    commits: list[list[str]] = []
    for line in out.splitlines():
        if line.startswith("@"):
            commits.append([line[1:], ""])
        elif line.strip() and commits and not commits[-1][1]:
            commits[-1][1] = line.strip()
    start = None
    for sha, path in commits:
        text = _show(repo, sha, path)
        if text is not None and mod.in_report_mode(path, text):
            start = (sha, path)
        else:
            break
    return start


def first_commit(repo: Path, rev: str, path: str) -> str:
    out = _git(repo, "log", "--follow", "--diff-filter=A", "--format=%H", rev, "--", path).split()
    return out[-1] if out else ""


def class_a_inventory(repo: Path, rev: str) -> list[str]:
    """Modules whose header says ``class: A``: the inventory a clock should exist for once demoted."""
    out = _git(repo, "grep", "-l", "-E", r"^(#|//) *class: *A *$", rev, "--", "tests", "scripts", ok=(0, 1))
    return sorted(line.partition(":")[2] for line in out.splitlines())


# -- the uploaded artifacts ------------------------------------------------------------------------------------------------


@dataclass
class Snap:
    """One develop push's report data: the items of every class-A pytest check and the value lines of numeric ones."""
    sha: str
    date: date
    has_items: bool = False
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errored: int = 0
    items: dict[str, list[str]] = field(default_factory=dict)
    required: dict[str, int] = field(default_factory=dict)      # --require prefix -> tests examined
    values: dict[str, tuple[float, float]] = field(default_factory=dict)

    def examined(self, module: str) -> int:
        stem = module.removesuffix(".py")
        return sum(n for p, n in self.required.items() if p == stem or p.startswith(stem + "::"))


def load_snapshots(repo: Path, art: Path, rev: str, until: date) -> tuple[list[Snap], list[str]]:
    """Snapshots from ``art/<sha>/*``, oldest first by position in ``rev``'s history; and the notes on what was skipped."""
    log = [ln.split() for ln in _git(repo, "log", "--topo-order", "--reverse", "--format=%H %cI", rev).splitlines()]
    order = {sha: i for i, (sha, _) in enumerate(log)}
    stamps = dict(log)
    snaps: list[Snap] = []
    notes: list[str] = []
    for sub in sorted(p for p in art.iterdir() if p.is_dir()) if art.is_dir() else []:
        sha = sub.name if sub.name in order else next((s for s in order if s.startswith(sub.name)), "") if len(sub.name) >= 7 else ""
        if not sha:
            notes.append(f"artifact directory {sub.name}: not a commit on {rev}, skipped")
            continue
        snap = Snap(sha, _utc_date(stamps[sha]))
        if snap.date > until:
            continue
        for f in sorted(sub.iterdir()):
            if f.suffix == ".json":
                try:
                    payload = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    notes.append(f"{sub.name}/{f.name}: unreadable, skipped")
                    continue
                if isinstance(payload, dict) and "items" in payload:
                    snap.has_items = True
                    for key in ("passed", "failed", "skipped", "errored"):
                        setattr(snap, key, getattr(snap, key) + int(payload.get(key, 0) or 0))
                    for row in payload["items"]:
                        snap.items.setdefault(row["check"], []).append(row["id"])
                    for req in payload.get("required", []):
                        snap.required[req["prefix"]] = snap.required.get(req["prefix"], 0) + int(req.get("examined", 0) or 0)
            elif f.suffix in (".log", ".txt", ".md"):
                for m in VALUE_RE.finditer(f.read_text(encoding="utf-8", errors="replace")):
                    snap.values[m.group(1)] = (float(m.group(2)), float(m.group(3)))
        snaps.append(snap)
    snaps.sort(key=lambda s: order[s.sha])
    return snaps, notes


def run_gh(endpoint: str, binary: bool = False) -> Any:
    """``gh api --method GET <endpoint>``: JSON, or raw bytes. Read-only; the caller makes one call per page."""
    done = subprocess.run(["gh", "api", "--method", "GET", endpoint], capture_output=True, check=False)
    if done.returncode != 0:
        raise ReviewError(f"gh api {endpoint}: {done.stderr.decode(errors='replace').strip()}")
    return done.stdout if binary else json.loads(done.stdout)


def fetch_artifacts(slug: str, since: date, until: date, out: Path, gh: Callable[..., Any] = run_gh,
                    log_job: str | None = LOG_JOB, workflow: str = "ci.yml") -> Row:
    """Download the ``class-a-items*`` artifacts of develop's push runs in the window into ``out/<head sha>/``, and (when
    ``log_job`` is set) that job's ``<name> value=...`` lines for the numeric checks. Writes ``_runs.json`` (head sha and
    creation time of every run, for the clock) and ``_fetch.json`` (what could not be read: expired artifacts, runs with
    no artifact, a run list at the API's cap) and returns the second as a dict, also printed to stderr."""
    runs_seen: list[Row] = []
    info: Row = {"runs": 0, "with_data": 0, "expired": [], "missing": [], "total_count": 0, "capped": False}
    page = 1
    while True:
        data = gh(f"repos/{slug}/actions/workflows/{workflow}/runs?branch=develop&event=push&status=completed"
                  f"&created={since.isoformat()}..{until.isoformat()}&per_page=100&page={page}")
        runs = data.get("workflow_runs", [])
        info["total_count"] = int(data.get("total_count", len(runs)) or 0)
        for run in runs:
            dest = out / run["head_sha"]
            row = {"id": run["id"], "head_sha": run["head_sha"], "created_at": run.get("created_at", "")}
            runs_seen.append(row)
            listing = gh(f"repos/{slug}/actions/runs/{run['id']}/artifacts?per_page=100")
            wrote = False
            ours = [a for a in listing.get("artifacts", []) if a["name"].startswith(ARTIFACT_PREFIX)]
            if not ours:
                info["missing"].append(row)
            for art in ours:
                if art.get("expired"):
                    info["expired"].append({**row, "name": art["name"]})
                    continue
                with zipfile.ZipFile(io.BytesIO(gh(f"repos/{slug}/actions/artifacts/{art['id']}/zip", binary=True))) as z:
                    for member in z.namelist():
                        if member.endswith(".json"):
                            dest.mkdir(parents=True, exist_ok=True)
                            (dest / Path(member).name).write_bytes(z.read(member))
                            wrote = True
            if log_job:
                jobs = gh(f"repos/{slug}/actions/runs/{run['id']}/jobs?per_page=100").get("jobs", [])
                for job in (j for j in jobs if j.get("name") == log_job):
                    log = gh(f"repos/{slug}/actions/jobs/{job['id']}/logs", binary=True).decode(errors="replace")
                    lines = [ln for ln in log.splitlines() if VALUE_RE.search(ln)]
                    if lines:
                        dest.mkdir(parents=True, exist_ok=True)
                        (dest / f"{log_job}.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
                        wrote = True
            info["with_data"] += wrote
        info["runs"] += len(runs)
        if len(runs) < 100:
            break
        if page * 100 >= RUN_LIST_CAP:
            info["capped"] = info["total_count"] > RUN_LIST_CAP
            break
        page += 1
    out.mkdir(parents=True, exist_ok=True)
    (out / "_runs.json").write_text(json.dumps(runs_seen), encoding="utf-8")
    (out / "_fetch.json").write_text(json.dumps(info), encoding="utf-8")
    sys.stderr.write(f"fetched {info['runs']} run(s) of {info['total_count']}: {info['with_data']} with data, "
                     f"{len(info['expired'])} expired artifact(s), {len(info['missing'])} run(s) with none"
                     f"{', RUN LIST CAPPED at ' + str(RUN_LIST_CAP) if info['capped'] else ''}\n")
    return info


# -- the record ------------------------------------------------------------------------------------------------------------


@dataclass
class Ctx:
    repo: Path
    rev: str
    snaps: list[Snap]
    since: date
    until: date
    priors: Sequence[Row]
    runs: list[Row]
    fetch: Row
    cache: dict[Any, Any] = field(default_factory=dict)


def _norm(item_id: str) -> str:
    """An item id without the line number of a ``path:line`` id: an unrelated edit above it moves the line, not the item."""
    return _LINE_NO.sub("", item_id)


def _resolved(fa: Sequence[str], fb: Sequence[str]) -> list[str]:
    """The normalised ids whose count fell from ``fa`` to ``fb``. A multiset, not a set: two offenders in one file read as one id
    twice, so fixing one of them is a resolution and moving a line number at an equal count is not."""
    before, after = collections.Counter(map(_norm, fa)), collections.Counter(map(_norm, fb))
    return sorted(k for k, n in before.items() if n > after[k])


def _item_path(item_id: str) -> str:
    return item_id.split("::", 1)[0].split(":", 1)[0]


def _files_between(ctx: Ctx, a: str, b: str) -> list[str]:
    if (a, b) not in ctx.cache:
        out = _git(ctx.repo, "log", "--no-renames", "--name-only", "--format=", f"{a}..{b}")
        ctx.cache[(a, b)] = sorted({ln for ln in out.splitlines() if ln})
    return ctx.cache[(a, b)]


def coverage(days_with: set[date], start: date, end: date) -> Row:
    """Share of the days ``start``..``end`` with a usable snapshot, and the largest gap in days between usable days (the
    edges count: no data at all is one gap as long as the range)."""
    span = max((end - start).days, 0) + 1
    hit = sorted(d for d in days_with if start <= d <= end)
    points = [start, *hit, end] if hit else [start, end]
    gap = max((b - a).days for a, b in zip(points, points[1:])) if len(points) > 1 else 0
    return {"usable_days": len(hit), "days": span, "pct": round(100 * len(hit) / span, 1), "max_gap_days": gap}


def landing(ctx: Ctx, sha: str) -> Row | None:
    """The first develop push CI run whose head contains ``sha`` (the day the demotion actually landed), from the fetch."""
    for run in sorted(ctx.runs, key=lambda r: r["created_at"]):
        if run.get("created_at") and _git_ok(ctx.repo, "merge-base", "--is-ancestor", sha, run["head_sha"]):
            return run
    return None


def _conftest_ledgers(repo: Path, a: str, b: str, names: Sequence[str]) -> set[str]:
    """The conftest files a range edited in a line that names one of the exclusion sets the check reads (``git log -G``): a
    conftest edit that leaves those sets alone is not a ledger edit."""
    if not names:
        return set()
    pattern = "(" + "|".join(map(re.escape, names)) + ")"
    out = _git(repo, "log", "--no-renames", "-G", pattern, "--name-only", "--format=", f"{a}..{b}", "--", "conftest.py", "**/conftest.py")
    return {ln for ln in out.splitlines() if ln}


def _prior_checks(ctx: Ctx, name: str) -> list[Row]:
    return [c for p in ctx.priors for c in p["checks"] if c["check"] == name]


def evaluate(ctx: Ctx, mod: Module) -> tuple[Row, list[Row]]:
    """One check's record entry, and the findings about it."""
    repo, rev, since, until = ctx.repo, ctx.rev, ctx.since, ctx.until
    findings: list[Row] = []
    name = mod.check

    def finding(detail: str) -> None:
        findings.append({"module": mod.path, "kind": "header", "detail": detail})

    tip_text = _show(repo, rev, mod.path) or ""
    tip, problems = parse_header(tip_text)
    for p in problems:
        finding(p)
    start = stretch_start(repo, rev, mod)
    if start is None:
        raise ReviewError(f"{mod.path} is in report mode on {rev} but no commit on it introduces that")
    sha, path_then = start
    then, _ = parse_header(_show(repo, sha, path_then) or "")
    if then is None and tip is not None:
        finding("header added after the demotion: the guards below are read from tip")
    elif then is not None and tip is not None and then != tip:
        finding("header changed since the demotion: guards, owner or class differ between the demotion commit and tip")
    header = then or tip
    guards = header.guard_paths if header else ()
    if header is not None and header.klass == "U":
        finding("class: U header on a class_a-marked check: the mark and the class disagree, so it is never due")
    name_key = next((k for s in ctx.snaps for k in s.values if k in name), "") if mod.numeric else ""

    def usable(s: Snap) -> bool:
        return name_key in s.values if mod.numeric else s.has_items and s.examined(mod.path) >= 1

    def failing(s: Snap) -> list[str]:
        if mod.numeric:
            value, alarm = s.values[name_key]
            return ["over the alarm line"] if value > alarm else []
        return s.items.get(name, [])

    same = [c for c in _prior_checks(ctx, name) if c["demotion_sha"] == sha]
    if same:
        clock, source = date.fromisoformat(same[0]["demotion_date"]), f"{same[0]['clock_source']} (carried from an earlier record)"
    elif (run := landing(ctx, sha)) is not None:
        clock, source = _utc_date(run["created_at"]), f"first develop push CI run containing it (run {run['id']})"
    else:
        first = min((s.date for s in ctx.snaps if usable(s)), default=None)
        clock = max(commit_date(repo, sha), first) if first else commit_date(repo, sha)
        source = "fallback: max(committer date, first usable snapshot); no CI run in the fetch contains it"
    days = (until - clock).days
    live = [s for s in ctx.snaps if usable(s) and s.date >= clock]
    window = [s for s in live if since <= s.date <= until]
    earlier = [s for s in live if s.date < since]
    series = ([earlier[-1]] if earlier else []) + window
    reads = sorted(set(_EXCLUSION_NAME.findall(tip_text))) if "conftest" in tip_text else []
    catches: list[Row] = []
    mixed: list[Row] = []
    for a, b in zip(live, live[1:]):
        if not (since <= b.date <= until):
            continue
        fa, fb = failing(a), failing(b)
        resolved = _resolved(fa, fb)
        if not resolved:
            continue
        files = _files_between(ctx, a.sha, b.sha)
        hit = [f for f in files if under_any(f, guards)]
        if not hit:
            continue
        own = _conftest_ledgers(repo, a.sha, b.sha, reads)
        ledger = [f for f in files if _ledger(f, own)]
        row = {"check": name, "from_sha": a.sha, "to_sha": b.sha, "items_resolved": resolved, "guard_files": hit}
        if ledger:
            mixed.append({**row, "ledger_files": ledger})
        elif len(fb) < len(fa) or any(_item_path(r) in files for r in resolved):
            catches.append(row)
    prior = [c for c in _prior_checks(ctx, name) if c["demotion_sha"] == sha]   # a re-promoted stretch starts clean
    marks = {(c["from_sha"], c["to_sha"]): c for pc in prior for c in pc["catches"]
             if c.get("non_substantive") and str(c.get("non_substantive_reason", "")).strip()}
    seen = {(c["from_sha"], c["to_sha"]) for c in catches}
    carried = [c for pc in prior for c in pc["catches"] if (c["from_sha"], c["to_sha"]) not in seen]
    for c in [*catches, *carried]:
        mark = marks.get((c["from_sha"], c["to_sha"]))
        if mark:
            c["non_substantive"], c["non_substantive_reason"] = True, mark["non_substantive_reason"]
    all_catches = [*catches, *carried]
    total = sum(1 for c in all_catches if not c.get("non_substantive"))
    dates = {s.date for s in live} | {date.fromisoformat(d) for pc in prior for d in pc.get("usable_dates", [])}
    cov = coverage(dates, clock, until)
    holes = [e for e in ctx.fetch.get("expired", []) if since <= _utc_date(e["created_at"]) <= until] if ctx.fetch else []
    capped = bool(ctx.fetch.get("capped"))
    evidence_gap = True
    if days < RETIRE_AFTER_DAYS:
        due, reason, evidence_gap = False, f"{days} days since the clock started, under {RETIRE_AFTER_DAYS}", False
    elif header is not None and header.klass == "U":
        due, reason, evidence_gap = False, "class: U header on a class_a-marked check: fix the mark or the class", False
    elif total:
        due, reason, evidence_gap = False, f"{total} guard catch(es) since the clock started", False
    elif header is None or not guards:
        due, reason = False, "evidence gap: no readable guards: header, so zero catches would be an artifact; fix the header first"
    elif holes or capped:
        due, reason = False, (f"evidence gap: {len(holes)} expired artifact(s) in the window" if holes else
                              f"evidence gap: the run list hit the API cap of {RUN_LIST_CAP}")
    elif not dates:
        due, reason = False, "evidence gap: no report run in the evidence: the check may not have run"
    elif cov["pct"] < COVERAGE_MIN_PCT or cov["max_gap_days"] > GAP_MAX_DAYS:
        due, reason = False, (f"evidence gap: coverage {cov['pct']}% of {cov['days']} days (needs {COVERAGE_MIN_PCT}%), "
                              f"largest gap {cov['max_gap_days']} days (allows {GAP_MAX_DAYS})")
    else:
        due, reason, evidence_gap = True, f"{days} days since the clock started, no guard catch, coverage {cov['pct']}%", False
    counts = [s.values[name_key][0] for s in series] if mod.numeric else [len(failing(s)) for s in series]
    rose = len(counts) >= 2 and counts[-1] > counts[0]
    marked, unmarked = ([], []) if mod.numeric else class_a_tests(tip_text)
    opportunity = int(_git(repo, "rev-list", "--count", f"--since={since.isoformat()}T00:00:00Z", f"--until={until.isoformat()}T23:59:59Z",
                           rev, "--", *guards).strip() or 0) if guards else 0
    entry: Row = {
        "check": name, "module": mod.path, "kind": "numeric" if mod.numeric else "pass_fail",
        "marked_tests": marked, "unmarked_tests": unmarked,
        "header": tip.as_row() if tip else None, "header_at_demotion": then.as_row() if then else None,
        "demotion_sha": sha, "demotion_date": clock.isoformat(), "clock_source": source, "days_since_demotion": days,
        "catches": all_catches, "catches_window": len(catches), "catches_total": total, "mixed_fixes": mixed,
        "coverage": {**cov, "min_pct": COVERAGE_MIN_PCT, "max_gap_allowed_days": GAP_MAX_DAYS},
        "usable_dates": sorted(d.isoformat() for d in dates), "opportunity": opportunity,
        "due": due, "due_reason": reason, "evidence_gap": evidence_gap, "rose": rose,
        "rose_from": _num(counts[0]) if rose else None, "rose_to": _num(counts[-1]) if rose else None,
    }
    if mod.numeric:
        entry["values"] = [{"sha": s.sha, "date": s.date.isoformat(), "value": _num(s.values[name_key][0]),
                            "alarm_line": _num(s.values[name_key][1])} for s in window]
        entry["trend"] = _num(counts[-1] - counts[0]) if len(counts) >= 2 else None
        entry["headroom"] = _num(window[-1].values[name_key][1] - window[-1].values[name_key][0]) if window else None
    else:
        entry["runs"] = [{"sha": s.sha, "date": s.date.isoformat(), "failing_items": len(failing(s)), "passed": s.passed,
                          "failed": s.failed, "skipped": s.skipped, "errored": s.errored, "examined": s.examined(mod.path)} for s in window]
    return entry, findings


def _num(x: float) -> int | float:
    return int(x) if float(x).is_integer() else x


def quarantine_report(repo: Path, rev: str, mark: str, until: date) -> Row:
    """Tests carrying ``@pytest.mark.<mark>``, each with the age of its decorator line (``git blame``)."""
    out = _git(repo, "grep", "-l", "-E", rf"mark\.{re.escape(mark)}", rev, "--", "tests", ok=(0, 1))
    found: list[Row] = []
    for line in out.splitlines():
        path = line.partition(":")[2]
        tree = _parse(_show(repo, rev, path) or "")
        if tree is None:
            continue

        def visit(body: list[ast.stmt], prefix: str) -> None:
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    for dec in node.decorator_list:
                        target = dec.func if isinstance(dec, ast.Call) else dec
                        if ast.unparse(target) == f"pytest.mark.{mark}":
                            blame = _git(repo, "blame", "-L", f"{dec.lineno},{dec.lineno}", "--porcelain", rev, "--", path)
                            stamp = next(int(ln.split()[1]) for ln in blame.splitlines() if ln.startswith("committer-time "))
                            age = (until - datetime.fromtimestamp(stamp, timezone.utc).date()).days
                            found.append({"test": f"{path}::{prefix}{node.name}", "age_days": age})
                    if isinstance(node, ast.ClassDef):
                        visit(node.body, f"{prefix}{node.name}::")
        visit(tree.body, "")
    found.sort(key=lambda t: (-t["age_days"], t["test"]))
    return {"mark": mark, "present": bool(found), "all": found,
            "over_14_days": [t for t in found if t["age_days"] > QUARANTINE_MAX_DAYS]}


def build_record(repo: Path, rev: str, since: date, until: date, art: Path, priors: Sequence[Row] = (),
                 numeric: Iterable[str] = NUMERIC_MODULES, quarantine_mark: str = "quarantine") -> Row:
    repo, art = Path(repo), Path(art)
    sha = _git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}").strip()
    snaps, notes = load_snapshots(repo, art, rev, until)

    def side(name: str, default: Any) -> Any:
        try:
            return json.loads((art / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default
    ctx = Ctx(repo, rev, snaps, since, until, priors, side("_runs.json", []), side("_fetch.json", {}))
    checks: list[Row] = []
    findings: list[Row] = [{"module": "", "kind": "artifact", "detail": n} for n in notes]
    if ctx.fetch.get("capped"):
        findings.append({"module": "", "kind": "artifact", "detail": f"the run list hit the API cap of {RUN_LIST_CAP}: the window is incomplete"})
    if ctx.fetch.get("expired"):
        findings.append({"module": "", "kind": "artifact", "detail": f"{len(ctx.fetch['expired'])} class-a-items artifact(s) expired: coverage holes"})
    modules = discover(repo, rev, numeric)
    for mod in modules:
        entry, found = evaluate(ctx, mod)
        checks.append(entry)
        findings += found
    keeps = {e["check"]: e["keep_reason"] for p in priors for e in p.get("due_for_deletion", [])
             if e.get("disposition") == "keep" and str(e.get("keep_reason", "")).strip()}
    due = []
    for c in checks:
        if not c["due"]:
            continue
        row = {"check": c["check"], "module": c["module"], "kind": c["kind"], "demotion_sha": c["demotion_sha"], "days_since_demotion": c["days_since_demotion"],
               "catches": 0, "set_aside_catches": [x for x in c["catches"] if x.get("non_substantive")],
               "marked_tests": c["marked_tests"], "unmarked_tests": c["unmarked_tests"], "module_whole": False, "module_whole_reason": "",
               "opportunity": c["opportunity"], "previous_keep_reason": keeps.get(c["check"], ""), "names": [],
               "disposition": "delete", "keep_reason": "", "cause_fix_sha": "", "leaves_unguarded": "", "replacement_guard": "",
               "reopen_condition": "", "evidence": {}}
        if c["kind"] == "numeric":
            row.update(headroom=c.get("headroom"), trend=c.get("trend"))
        due.append(row)
    inventory = class_a_inventory(repo, rev)
    clocked = {m.path for m in modules}
    unclockable = []
    for p in inventory:
        if p not in clocked:
            text = _show(repo, rev, p) or ""
            unclockable.append({"module": p, "why": "an advisory script check: no clock can be read from it" if "--advisory" in text or "advisory" in text.lower()
                                else "class A by header, not in report mode yet, or in a form this tool cannot read"})
    rose = [{"check": c["check"], "kind": "value" if c["kind"] == "numeric" else "failing_items", "from": c["rose_from"], "to": c["rose_to"]}
            for c in checks if c["rose"]]
    stuck = [{"check": c["check"], "reason": c["due_reason"], "days_since_demotion": c["days_since_demotion"],
              "choices": ["delete (add an entry with disposition delete and a delete_reason)", "restart the clock (a new demotion commit)", "keep with a written reason in the record"]}
             for c in checks if not c["due"] and c["evidence_gap"] and c["days_since_demotion"] > STUCK_AFTER_DAYS]
    return {
        "kind": "check-value-review", "rev": sha, "window": {"since": since.isoformat(), "until": until.isoformat()},
        "thresholds": {"retire_after_days": RETIRE_AFTER_DAYS, "stuck_after_days": STUCK_AFTER_DAYS, "coverage_min_pct": COVERAGE_MIN_PCT,
                       "gap_max_days": GAP_MAX_DAYS, "quarantine_max_days": QUARANTINE_MAX_DAYS},
        "checks": checks, "findings": findings, "due_for_deletion": due, "stuck": stuck, "rose": rose, "unclockable": unclockable,
        "fetch": ctx.fetch, "quarantine": quarantine_report(repo, rev, quarantine_mark, until),
        "totals": {"class_a_with_clock": len(checks), "due_for_deletion": len(due), "stuck": len(stuck), "findings": len(findings),
                   "snapshots": len(snaps)},
    }


def render_markdown(rec: Row) -> str:
    w, t = rec["window"], rec["thresholds"]
    out = [f"# Check value review, {w['since']} to {w['until']}", "",
           f"Reviewed rev `{rec['rev'][:12]}`. Due at {t['retire_after_days']} days with no guard catch and evidence coverage of at least "
           f"{t['coverage_min_pct']}% of days with a gap of at most {t['gap_max_days']} days; stuck after {t['stuck_after_days']} days. "
           "A check's clock starts at the oldest commit of its current unbroken report-mode stretch; the source of each date is on its row. "
           "Evidence is the `class-a-items` artifacts of develop's push runs.", "",
           f"{rec['totals']['class_a_with_clock']} class-A check(s) with a clock, {rec['totals']['due_for_deletion']} due for deletion, "
           f"{rec['totals']['stuck']} stuck, {rec['totals']['findings']} finding(s).", ""]
    if rec["checks"]:
        out += ["## Checks", "", "| check | clock | days | runs | failing first to last | catches | coverage | opportunity | due | why |",
                "|---|---|---|---|---|---|---|---|---|---|"]
        for c in rec["checks"]:
            series = c.get("runs") or c.get("values") or []
            key = "failing_items" if "runs" in c else "value"
            span = f"{series[0][key]} to {series[-1][key]}" if series else "none"
            cov = c["coverage"]
            out.append(f"| `{c['check']}` | {c['demotion_date']} (`{c['demotion_sha'][:10]}`; {c['clock_source']}) | {c['days_since_demotion']} | {len(series)} "
                       f"| {span} | {c['catches_total']} | {cov['pct']}% / gap {cov['max_gap_days']}d | {c['opportunity']} | {'yes' if c['due'] else 'no'} | {c['due_reason']} |")
        out.append("")
        for c in rec["checks"]:
            for m in c["mixed_fixes"]:
                out.append(f"- mixed fix, not a catch (`{c['check']}`): guarded {', '.join(m['guard_files'])} together with {', '.join(m['ledger_files'])} ({m['from_sha'][:10]}..{m['to_sha'][:10]})")
            for x in c["catches"]:
                if x.get("non_substantive"):
                    out.append(f"- catch set aside as non_substantive (`{c['check']}`, {x['from_sha'][:10]}..{x['to_sha'][:10]}): {x['non_substantive_reason']}")
        out.append("")
    out += ["## Due for deletion", ""]
    if rec["due_for_deletion"]:
        out += ["Each entry deletes by default. To keep one, set `disposition` to `keep` and write `keep_reason` in the JSON block below; "
                "for an early retirement see `deletion-plan`'s list of required fields. To mark a listed catch `non_substantive`, set it and "
                "`non_substantive_reason` on that catch.", ""]
        for e in rec["due_for_deletion"]:
            extra = f"; headroom {e['headroom']}, trend {e['trend']}" if "headroom" in e else ""
            prev = f"; PREVIOUS REVIEW KEPT IT: {e['previous_keep_reason']}" if e["previous_keep_reason"] else ""
            out.append(f"- `{e['check']}` ({e['module']}, {e['days_since_demotion']} days, no catch, {e['opportunity']} commit(s) touched its guards in the window"
                       f"{extra}): disposition `{e['disposition']}`; {len(e['marked_tests'])} marked test(s), {len(e['unmarked_tests'])} unmarked left in the module{prev}")
    else:
        out.append("None.")
    out += ["", "## Stuck (not due for an evidence reason, past the stuck age)", ""]
    out += [f"- `{s['check']}` ({s['days_since_demotion']} days): {s['reason']}. Choose: {'; '.join(s['choices'])}." for s in rec["stuck"]] or ["None."]
    out += ["", "## Rose in the window", ""]
    out += [f"- `{r['check']}` ({r['kind']}): {r['from']} to {r['to']}" for r in rec["rose"]] or ["None."]
    q = rec["quarantine"]
    out += ["", f"## Quarantined over {QUARANTINE_MAX_DAYS} days", ""]
    if not q["present"]:
        out.append(f"No test carries `pytest.mark.{q['mark']}` on this rev.")
    else:
        out += [f"- `{x['test']}`: {x['age_days']} days" for x in q["over_14_days"]] or ["None over the deadline."]
    out += ["", "## Class-A modules this tool cannot clock", ""]
    out += [f"- `{u['module']}`: {u['why']}" for u in rec["unclockable"]] or ["None."]
    out += ["", "## Findings", ""]
    out += [f"- {f['module'] or 'artifacts'}: {f['detail']}" for f in rec["findings"]] or ["None."]
    out += ["", RECORD_MARK, "```json", json.dumps(rec, indent=1, sort_keys=True), "```", ""]
    return "\n".join(out)


def load_record_text(text: str) -> Row:
    m = re.search(re.escape(RECORD_MARK) + r"\s*```json\n(.*?)\n```", text, re.S)
    try:
        return json.loads(m.group(1) if m else text)
    except ValueError as exc:
        raise ReviewError(f"not a review record (no JSON block, not JSON): {exc}") from exc


# -- deletion-plan ---------------------------------------------------------------------------------------------------------


def consumer_kind(path: str) -> str:
    if path.startswith(".github/workflows/"):
        return "ci-workflow"
    return {"scripts/push_check_gate.py": "push-gate", ".nexus/procedures/preflight.lua": "preflight",
            "scripts/meta_tests.py": "meta_tests", "scripts/lab-entry-conditions.toml": "lab-entry",
            "scripts/file-size-exceptions.txt": "file-size ledger"}.get(
        path, "docs-history" if path.startswith(("docs/", "changelog/")) or path == "CHANGELOG.md"
        else "test" if path.startswith("tests/") else "other")


def _job_of(repo: Path, path: str, lineno: int) -> str:
    try:
        lines = (repo / path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in reversed(lines[:lineno]):
        m = re.match(r"^  ([\w-]+):\s*$", line)
        if m:
            return m.group(1)
    return ""


def consumers(repo: Path, module: str, names: Sequence[str]) -> list[Row]:
    """Every line outside ``module`` that names one of ``names`` (git grep of the tracked tree), each tagged with the names it hit."""
    seen: dict[tuple[str, int], Row] = {}
    for name in names:
        out = _git(repo, "grep", "-n", "-F", "-e", name, ok=(0, 1))
        for line in out.splitlines():
            path, _, rest = line.partition(":")
            num, _, text = rest.partition(":")
            if path == module or path.endswith(".test_durations.json") or not num.isdigit():
                continue
            kind = consumer_kind(path)
            row = seen.setdefault((path, int(num)), {"path": path, "line": int(num), "text": text.strip()[:160], "kind": kind, "names": [],
                                                     "job": _job_of(repo, path, int(num)) if kind == "ci-workflow" else ""})
            row["names"].append(name)
    return sorted(seen.values(), key=lambda h: (h["path"], h["line"]))


def validate_entry(repo: Path, rec: Row, e: Row) -> list[str]:
    errs = []
    d = e.get("disposition")
    if d not in DISPOSITIONS:
        errs.append(f"disposition must be one of {', '.join(DISPOSITIONS)}, got {d!r}")
    if d == "keep" and not str(e.get("keep_reason", "")).strip():
        errs.append("keep_reason: a keep needs a written reason in the record")
    if e.get("module_whole") and not str(e.get("module_whole_reason", "")).strip():
        errs.append("module_whole_reason: module_whole: true needs a written reason")
    if d != "retire-early":
        return errs
    cause = str(e.get("cause_fix_sha", "")).strip()
    check = next((c for c in rec.get("checks", []) if c["check"] == e["check"]), {})
    guards = list((check.get("header_at_demotion") or check.get("header") or {}).get("guard_paths", [])) + list(e.get("behaviour_paths", []))
    if not re.fullmatch(r"[0-9a-f]{40}", cause):
        errs.append("cause_fix_sha: retire-early needs the full 40-character sha of the commit that removed the cause")
    elif not _git_ok(repo, "cat-file", "-e", f"{cause}^{{commit}}"):
        errs.append(f"cause_fix_sha: {cause} is not a commit in this repository")
    else:
        if not _git_ok(repo, "merge-base", "--is-ancestor", cause, rec.get("rev", "HEAD")):
            errs.append(f"cause_fix_sha: {cause[:10]} is not an ancestor of the reviewed rev {str(rec.get('rev', 'HEAD'))[:10]}")
        demotion = check.get("demotion_sha", e.get("demotion_sha", ""))
        own = {demotion, first_commit(repo, rec.get("rev", "HEAD"), e["module"])}
        if cause in own:
            errs.append("cause_fix_sha: it is the check's own demotion or birth commit, not a fix of the cause")
        elif not demotion:
            errs.append("cause_fix_sha: no demotion commit on record to measure the cause against")
        elif not _git_ok(repo, "merge-base", "--is-ancestor", demotion, cause):
            errs.append(f"cause_fix_sha: {cause[:10]} predates the demotion {demotion[:10]}: it does not descend from it")
        files = _git(repo, "show", "--no-renames", "--name-only", "--format=", cause).split()
        if not guards:
            errs.append("cause_fix_sha: the check has no guards paths on record; name the guarded behaviour in behaviour_paths")
        elif not any(under_any(f, guards) for f in files):
            errs.append(f"cause_fix_sha: {cause[:10]} touches none of the guarded paths or behaviour_paths ({', '.join(guards)})")
    for key in ("leaves_unguarded", "reopen_condition", "replacement_guard"):
        if not str(e.get(key, "")).strip():
            errs.append(f"{key}: retire-early needs it written")
    ev = e.get("evidence") or {}
    for key in ("guards_readings", "red_classification", "consumer_names"):
        if not ev.get(key):
            errs.append(f"evidence.{key}: retire-early needs it (guards readings, red classification with shas, consumer names; ablation if any)")
    return errs


def plan_entry(repo: Path, e: Row) -> tuple[list[str], list[str]]:
    """(plan lines, refusals) for one delete or retire-early entry, at the level of tests."""
    module = e["module"]
    path = repo / module
    if not path.is_file():
        return [], [f"{module} is not in the tree (already deleted?)"]
    text = path.read_text(encoding="utf-8")
    numeric = e.get("kind") == "numeric"
    marked, unmarked = ([], []) if numeric else class_a_tests(text)
    if not numeric and not marked:
        return [], [f"{module} has no class_a-marked test: nothing for this entry to remove"]
    whole = numeric or not unmarked or bool(e.get("module_whole"))
    names = list(dict.fromkeys(n for n in [module, Path(module).stem, e["check"], *e.get("names", [])] if len(n) >= 4))
    hits = consumers(repo, module, names)
    if not hits:
        return [], [f"vacuous plan: no file other than {module} names {', '.join(names)}: check the names or the entry"]
    files = sorted({h["path"] for h in hits})
    counts = {n: sum(1 for h in hits if n in h["names"]) for n in names}
    out = [f"== {e['check']} [{e['disposition']}] {module}",
           f"before: {len(hits)} reference line(s) in {len(files)} file(s) besides the module; per name: "
           + ", ".join(f"{n} {c}" for n, c in counts.items())]
    if whole:
        out.append(f"  git rm {module}")
        if unmarked and not numeric:
            out.append(f"    module_whole (reason: {e['module_whole_reason'].strip()}): also deletes {len(unmarked)} unmarked test(s): {', '.join(unmarked)}")
    else:
        out.append(f"  edit {module}: delete the {len(marked)} class_a test(s): {', '.join(marked)}")
        out.append(f"    stays: {len(unmarked)} unmarked test(s) that still block: {', '.join(unmarked)}; do not git rm the module")
    if e.get("delete_reason"):
        out.append(f"  stuck check, deleted for: {e['delete_reason'].strip()}")
    if e["disposition"] == "retire-early":
        ev = e["evidence"]
        out.append(f"  cause removed by {e['cause_fix_sha'][:10]}; leaves unguarded: {e['leaves_unguarded']}; replacement guard: {e['replacement_guard']}; "
                   f"re-open when: {e['reopen_condition']}")
        out.append(f"  evidence: guards {ev['guards_readings']}; reds {ev['red_classification']}" + (f"; ablation {ev['ablation']}" if ev.get("ablation") else ""))
    for c in e.get("set_aside_catches", []):
        out.append(f"  catch set aside as non_substantive ({c['from_sha'][:10]}..{c['to_sha'][:10]}): {c.get('non_substantive_reason', '')}")
    for p in files:
        mine = [h for h in hits if h["path"] == p]
        jobs = sorted({h["job"] for h in mine if h["job"]})
        out.append(f"  edit {p} ({mine[0]['kind']}{', job ' + ', '.join(jobs) if jobs else ''}): {len(mine)} line(s)")
        out += [f"    {p}:{h['line']}: {h['text']}" for h in mine]
    out.append("after: run `git grep -c -F " + " ".join(f"-e {n}" for n in names) + "` and expect 0 hits outside docs/ and changelog/ (history stays)"
               + ("; the unmarked tests stay, so the module's own name may remain" if not whole else ""))
    return out, []


def deletion_plan(repo: Path, rec: Row) -> tuple[list[str], list[str]]:
    lines: list[str] = []
    refused: list[str] = []
    entries = rec.get("due_for_deletion", [])
    due = {c["check"] for c in rec.get("checks", []) if c.get("due")}
    for missing in sorted(due - {e["check"] for e in entries}):
        refused.append(f"{missing}: due in this record but has no entry: a dropped entry would read as a keep with no reason")
    planned = 0
    stuck = {s["check"] for s in rec.get("stuck", [])}
    for e in entries:
        recorded = next((c for c in rec.get("checks", []) if c["check"] == e["check"]), None)
        errs = []
        if recorded:   # module and kind come from the record's check; an entry that disagrees is refused
            for key in ("module", "kind"):
                if e.get(key) and e[key] != recorded[key]:
                    errs.append(f"{key}: the entry says {e[key]!r} but the record's check says {recorded[key]!r}")
            e = {**e, "module": recorded["module"], "kind": recorded["kind"]}
        elif not e.get("module"):
            errs.append("module: the check is not in the record's checks, so the entry must name its module")
        errs += validate_entry(repo, rec, e)
        if e.get("disposition") != "retire-early" and e["check"] not in due:
            if e["check"] in stuck and e.get("disposition") in ("delete", "keep"):
                if e["disposition"] == "delete" and not str(e.get("delete_reason", "")).strip():
                    errs.append("delete_reason: deleting a stuck check (not due for an evidence reason) needs a written reason")
            else:
                errs.append("entry names a check that is not due in this record (only retire-early, or a stuck check, may)")
        if not errs and e["disposition"] == "keep":
            lines += [f"KEEP {e['check']}: {e['keep_reason'].strip()}", ""]
            planned += 1
        elif not errs:
            plan, errs = plan_entry(repo, e)
            if not errs:
                lines += plan + [""]
                planned += 1
        refused += [f"{e.get('check', '?')}: {m}" for m in errs]
    if not planned and not refused:
        refused.append("nothing is planned: the record lists nothing to delete, keep or retire")
    return lines, refused


# -- batch -----------------------------------------------------------------------------------------------------------------


@dataclass
class Baseline:
    other_runs: int
    runs: dict[str, list[str]]           # accounting red runs: run id -> the checks it failed on
    modules: dict[str, list[str]]        # check -> the modules that hold it
    check_tests: dict[str, list[str]] = field(default_factory=dict)    # check -> the test names that failed in the baseline
    other_breakdown: dict[str, int] = field(default_factory=dict)      # the non-accounting runs by category

    @property
    def accounting_runs(self) -> int:
        return len(self.runs)

    @property
    def total_runs(self) -> int:
        return self.other_runs + len(self.runs)


def baseline_from_json(data: Row) -> Baseline:
    return Baseline(int(data["other_runs"]), {str(k): list(v) for k, v in data["accounting_runs"].items()},
                    {k: list(v) for k, v in data.get("check_modules", {}).items()},
                    {k: list(v) for k, v in data.get("check_tests", {}).items()}, dict(data.get("other_breakdown", {})))


def baseline_from_extraction(path: Path) -> Baseline:
    an = rr.analyze(rr.read_extraction(path))
    runs: dict[str, list[str]] = {}
    modules: dict[str, set[str]] = {}
    tests: dict[str, set[str]] = {}
    others: dict[str, int] = {}
    for rid, run in an["runs"].items():
        items = [i for i in an["by_run"][rid] if not i["derived"]]
        if run["category"] == rr.ACCOUNTING:
            checks = {i["check"] for i in items if i["category"] == rr.ACCOUNTING} or {i["check"] for i in items}
            runs[str(rid)] = sorted(checks) or ["(no item)"]
        else:
            others[run["category"]] = others.get(run["category"], 0) + 1
        for i in items:
            if i["kind"] == "test" and i.get("file"):
                modules.setdefault(i["check"], set()).add(i["file"])
                tests.setdefault(i["check"], set()).add(re.split(r"\[", i["item"].split("::")[-1])[0])
    return Baseline(len(an["runs"]) - len(runs), runs, {k: sorted(v) for k, v in modules.items()},
                    {k: sorted(v) for k, v in tests.items()}, others)


def load_baseline(path: Path) -> Baseline:
    path = Path(path)
    if ".jsonl" in path.name:
        return baseline_from_extraction(path)
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:  # type: ignore[operator]
        return baseline_from_json(json.load(fh))


def _remaining(runs: dict[str, list[str]], demoted: set[str]) -> int:
    return sum(1 for checks in runs.values() if any(c not in demoted for c in checks))


def under_target(accounting: int, other: int, target: Fraction) -> bool:
    """share < target, exactly: a / (a + o) < t  <=>  a (1 - t) < t o."""
    return accounting * (1 - target) < target * other


def max_accounting(other: int, target: Fraction) -> int:
    """The largest accounting count whose share is strictly under ``target`` with ``other`` held constant."""
    n, d = target.numerator, target.denominator
    return -(-n * other // (d - n)) - 1


def _state(runs: dict[str, list[str]], other: int, demoted: set[str], target: Fraction) -> Row:
    n = _remaining(runs, demoted)
    return {"accounting_runs": n, "other_runs": other, "share": n / (n + other) if n + other else 0.0, "under_target": under_target(n, other, target)}


def _consumer_changes(repo: Path, module: str) -> list[str]:
    stem = Path(module).stem
    out = []
    paths = [*(p.relative_to(repo).as_posix() for p in sorted((repo / ".github/workflows").glob("*.y*ml"))), *CONSUMER_FILES]
    for rel in paths:
        try:
            lines = (repo / rel).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for n, line in enumerate(lines, 1):
            if stem in line or module in line:
                job = _job_of(repo, rel, n) if rel.startswith(".github/") else ""
                out.append(f"{rel}:{n}: names {stem}" + (f" (job {job}): move it to the advisory job or drop the line" if job else ": stops refusing, prints its failing items"))
    return out


def _owner_of(check: str, modules: Sequence[str]) -> str | None:
    for name in (check, *(Path(m).stem for m in modules)):
        if name in OWNED_CHECKS:
            return OWNED_CHECKS[name]
    return None


def _changes(repo: Path, cand: Row, check_tests: Sequence[str], values: dict[str, float], pct: float, floor: int) -> tuple[list[str], Row | None]:
    changes: list[str] = []
    for path in cand["modules"]:
        if re.match(r"tests/(?:[\w-]+/)*test_\w+\.py$", path):
            try:
                text = (repo / path).read_text(encoding="utf-8")
            except OSError:
                text = ""
            marked, unmarked = class_a_tests(text)
            every = [*marked, *unmarked]
            prefix = path.removesuffix(".py")
            if module_level_lint(text):
                changes.append(f"{path}: every test is the check (module-level lint mark): add pytest.mark.class_a to its pytestmark and `--require {prefix}={max(1, len(every))}` "
                               "(its test-function count; lower it by any skip-marked test) to the test-class-a-report step in ci.yml and its nightly twin")
            else:
                mine = [t for t in check_tests if t in every] or list(check_tests)
                rest = [t for t in every if t not in mine]
                if mine:
                    changes.append(f"{path}: add pytest.mark.class_a to ONLY these test(s) ({', '.join(mine)}) and add "
                                   + ", ".join(f"`--require {prefix}::{t}=1`" for t in mine) + " to the test-class-a-report step in ci.yml and its nightly twin; "
                                   f"the other {len(rest)} test(s) in the module stay blocking in test-lint")
                else:
                    changes.append(f"{path}: not every test in this module is the check and the baseline names no failing test: choose the tests to mark; none is chosen here")
        else:
            changes.append(f"{path}: not a pytest module: needs its own report mode (the --advisory pattern of scripts/ddl_ratchet.py)")
        changes += _consumer_changes(repo, path)
    alarm = None
    if cand["check"] in values:
        v = values[cand["check"]]
        alarm = {"label": "proposal", "value": v, "proposed": _num(math.ceil(v + max(floor, v * pct / 100))),
                 "rule": f"value + max({floor}, {pct:g}% of value); Sam sets the line"}
        changes.append("numeric: print value and trend to the job summary, fail only past the alarm line")
    return changes, alarm


def build_batch(base: Baseline, repo: Path, *, size: int = 8, demoted: Iterable[str] = (), values: dict[str, float] | None = None,
                target: float | str | Fraction = TARGET_SHARE, margin_pct: float = 3.0, margin_min: int = 5) -> Row:
    repo = Path(repo)
    values = values or {}
    t = Fraction(str(target)) if not isinstance(target, Fraction) else target
    modules = {c: list(base.modules.get(c, [])) or ([STEP_CHECK_MODULES[c]] if c in STEP_CHECK_MODULES else [])
               for c in {c for cs in base.runs.values() for c in cs}}
    klass: dict[str, str | None] = {}
    owners: dict[str, str | None] = {}
    on_tree_demoted: set[str] = set()
    for check, paths in modules.items():
        heads = [read_header_file(repo / p) for p in paths]
        klass[check] = heads[0].klass if heads and all(h and h.klass == heads[0].klass for h in heads) else None
        owners[check] = heads[0].owner if heads and heads[0] else None
        for p in paths:
            try:
                if applies_class_a((repo / p).read_text(encoding="utf-8")):
                    on_tree_demoted.add(check)
            except OSError:
                pass
    already = set(STEPS_1_4) | set(demoted) | on_tree_demoted
    counts = {c: sum(1 for cs in base.runs.values() if c in cs) for c in modules}
    candidates, unclassified, excluded = [], [], []
    for check in sorted(modules, key=lambda c: (-counts[c], c)):
        row = {"check": check, "red_runs": counts[check], "modules": modules[check]}
        phase = _owner_of(check, modules[check])
        if check in already:
            excluded.append({**row, "why": "already demoted"})
        elif phase:
            excluded.append({**row, "why": f"owned by {phase}"})
        elif klass[check] is None:
            unclassified.append(row)
        elif klass[check] == "U":
            excluded.append({**row, "why": "class U"})
        else:
            note = f"header owner {owners[check]}: confirm it is not a Phase 3 or 4 check" if owners[check] in ("release", "plugin") else ""
            candidates.append({**row, "class": "A", "ownership_note": note})
    other = base.other_runs
    owned_elsewhere = {e["check"] for e in excluded if e["why"].startswith("owned by")}
    state = set(already)
    start = _state(base.runs, other, state, t)
    batch: list[Row] = []
    reached = start["under_target"]
    for cand in candidates:
        if reached or len(batch) >= size:
            break
        state.add(cand["check"])
        cand["changes"], cand["alarm_line"] = _changes(repo, cand, base.check_tests.get(cand["check"], []), values, margin_pct, margin_min)
        batch.append(cand)
        reached = under_target(_remaining(base.runs, state), other, t)
    walk, to_target = set(already), None
    if start["under_target"]:
        to_target = 0
    else:
        for n, cand in enumerate(candidates, 1):
            walk.add(cand["check"])
            if under_target(_remaining(base.runs, walk), other, t):
                to_target = n
                break
    everything = set(already) | {c["check"] for c in candidates}
    leftover = {r: cs for r, cs in base.runs.items() if any(c not in everything for c in cs)}
    floor: dict[str, int] = {"unclassified": 0, "class_u": 0, "phase_2": 0, "phase_3": 0, "phase_4": 0}
    cls = {c["check"]: c["why"] for c in excluded}
    unc = {c["check"] for c in unclassified}
    for cs in leftover.values():
        blocking = [c for c in cs if c not in everything]
        floor["unclassified"] += any(c in unc for c in blocking)
        floor["class_u"] += any(cls.get(c) == "class U" for c in blocking)
        for key, label in (("phase_2", "Phase 2"), ("phase_3", "Phase 3"), ("phase_4", "Phase 4")):
            floor[key] += any(cls.get(c, "").startswith(f"owned by {label}") for c in blocking)
    return {
        "target": float(t), "baseline": _state(base.runs, other, set(), t), "before": start, "after": _state(base.runs, other, state, t),
        "after_if_phase_3_4_land": _state(base.runs, other, state | owned_elsewhere, t),
        "bound_from_this_extraction": {"accounting_runs_max": max_accounting(other, t), "other_runs": other, "accounting_runs": base.accounting_runs,
                                        "source": "the committed Phase 0 red-run extraction, counted by the script"},
        "bound_from_rdr": {**RDR_FIGURE},
        "other_breakdown": base.other_breakdown,
        "target_reached": reached, "checks_to_target": to_target, "batch": batch, "unclassified": unclassified, "excluded": excluded,
        "floor_runs_still_blocking_after_all_candidates": floor,
        "unmet": "" if to_target is not None else "No prefix of the classified, unowned remainder reaches the target; the floor is in the runs that stay blocking whatever is batched.",
    }


def render_batch_markdown(b: Row) -> str:
    pct = lambda s: f"{s * 100:.1f}%"  # noqa: E731
    bl, ex, rdr = b["baseline"], b["bound_from_this_extraction"], b["bound_from_rdr"]
    fl = b["floor_runs_still_blocking_after_all_candidates"]
    out = [f"# Review batch proposal (target: accounting share under {b['target'] * 100:.0f}%)", "",
           f"Baseline {bl['accounting_runs']} accounting red runs of {bl['accounting_runs'] + bl['other_runs']} ({pct(bl['share'])}). "
           f"With the checks already demoted, {b['before']['accounting_runs']} would still turn red ({pct(b['before']['share'])}). "
           f"Bound, this extraction: at most {ex['accounting_runs_max']} accounting runs with {ex['other_runs']} other red runs held constant "
           f"(other = {', '.join(f'{k} {v}' for k, v in sorted(b['other_breakdown'].items())) or 'not itemised'}). "
           f"Bound, the RDR's own figure: about {rdr['accounting_runs_max']} with {rdr['other_runs']} others against {rdr['accounting_runs']} accounting ({rdr['source']}); "
           "Phase 0 step 5 may replace the baseline. "
           f"After this batch: {b['after']['accounting_runs']} ({pct(b['after']['share'])}); if Phase 3 and 4 also land: "
           f"{b['after_if_phase_3_4_land']['accounting_runs']} ({pct(b['after_if_phase_3_4_land']['share'])}). "
           f"Target reached by this batch: {'yes' if b['target_reached'] else 'no'}. "
           + (f"the first {b['checks_to_target']} candidate(s) in this order reach it. " if b["checks_to_target"] else "") + b["unmet"], "",
           "Runs that stay blocking whatever is batched, by what holds them (a run can be in several): "
           f"unclassified {fl['unclassified']}, class U {fl['class_u']}, Phase 2 {fl['phase_2']}, Phase 3 {fl['phase_3']}, Phase 4 {fl['phase_4']}.", "",
           "## Batch (one decision; it lands as one change and starts each 60-day clock)", ""]
    for c in b["batch"]:
        out.append(f"### `{c['check']}`: {c['red_runs']} baseline red runs")
        if c.get("ownership_note"):
            out.append(f"- note: {c['ownership_note']}")
        if c["alarm_line"]:
            a = c["alarm_line"]
            out.append(f"- alarm line (proposal, not decided): {a['proposed']} from value {a['value']}, {a['rule']}")
        out += [f"- {ch}" for ch in c["changes"]] or ["- no consumer found besides its own module"]
        out.append("")
    if not b["batch"]:
        out += ["None.", ""]
    out += ["## Unclassified (no class: header; never batched)", ""]
    out += [f"- `{c['check']}`: {c['red_runs']} red runs" for c in b["unclassified"]] or ["None."]
    out += ["", "## Excluded", ""] + ([f"- `{c['check']}` ({c['red_runs']}): {c['why']}" for c in b["excluded"]] or ["None."])
    return "\n".join(out) + "\n"


# -- command line ----------------------------------------------------------------------------------------------------------


def _date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO date: {text!r}") from exc


def _cmd_record(args: argparse.Namespace) -> int:
    priors = [load_record_text(Path(p).read_text(encoding="utf-8")) for p in args.prior]
    with tempfile.TemporaryDirectory(prefix="check-review.") as tmp:
        art = args.artifacts
        if art is None:
            art = Path(tmp)
            fetch_artifacts(args.slug, args.since - timedelta(days=7), args.until, art)
        rec = build_record(args.repo, args.rev, args.since, args.until, art, priors, [*NUMERIC_MODULES, *args.numeric], args.quarantine_mark)
    _say(json.dumps(rec, indent=1, sort_keys=True) if args.format == "json" else render_markdown(rec))
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    rec = load_record_text(Path(args.record).read_text(encoding="utf-8"))
    lines, refused = deletion_plan(args.repo, rec)
    for line in lines:
        _say(line)
    for msg in refused:
        sys.stderr.write(f"refused: {msg}\n")
    return 1 if refused else 0


def _cmd_batch(args: argparse.Namespace) -> int:
    values = json.loads(Path(args.values).read_text(encoding="utf-8")) if args.values else {}
    out = build_batch(load_baseline(args.report), args.repo, size=args.size, demoted=args.demoted, values=values, target=args.target)
    _say(json.dumps(out, indent=1, sort_keys=True) if args.format == "json" else render_batch_markdown(out))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="write the 2-week review record")
    rec.add_argument("--since", type=_date, required=True)
    rec.add_argument("--until", type=_date, required=True)
    rec.add_argument("--repo", type=Path, default=Path("."))
    rec.add_argument("--rev", default="origin/develop")
    rec.add_argument("--artifacts", type=Path, default=None, help="a directory of <sha>/class-a-items*.json; default: fetch with gh api")
    rec.add_argument("--slug", default=DEFAULT_SLUG)
    rec.add_argument("--prior", action="append", default=[], help="an earlier record (md or json), chained for the 60-day evidence")
    rec.add_argument("--format", choices=("md", "json"), default="md")
    rec.add_argument("--quarantine-mark", default="quarantine")
    rec.add_argument("--numeric", action="append", default=[], help="another numeric report-mode module (carries an alarm_line key)")
    rec.set_defaults(fn=_cmd_record)
    plan = sub.add_parser("deletion-plan", help="print the deletion plan of a settled record; edits nothing")
    plan.add_argument("record")
    plan.add_argument("--repo", type=Path, default=Path("."))
    plan.set_defaults(fn=_cmd_plan)
    bat = sub.add_parser("batch", help="propose the next step 7 batch")
    bat.add_argument("--report", type=Path, required=True, help="the red-run extraction (.jsonl.gz) or the baseline JSON form")
    bat.add_argument("--repo", type=Path, default=Path("."))
    bat.add_argument("--size", type=int, default=8)
    bat.add_argument("--demoted", action="append", default=[], help="a check an earlier batch already demotes")
    bat.add_argument("--values", default=None, help="JSON {check: current value} of the numeric checks")
    bat.add_argument("--target", default=TARGET_SHARE, help="the accounting share to get under, as a decimal string")
    bat.add_argument("--format", choices=("md", "json"), default="md")
    bat.set_defaults(fn=_cmd_batch)
    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except ReviewError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
