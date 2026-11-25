#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The push-time check gate of ``scripts/git-push-develop.sh`` (nexus-igea9, Sam 2026-11-22).

Five develop reds in one day (a write-seam 422, the wire ledger, the cloud-leg routes, the plugin ledger, a size
ceiling) each came from a check that was not run, or was run against the working tree instead of the pushed tree. This
runs the checks a push's diff reaches, against the TIP's tree, and refuses the push on a red.

The tip's tree is materialised as a detached git worktree (so ``git tag`` and history work for the ledger checks, which
read both) and removed on every exit path. The outbound diff selects the checks:

==============  ====================================================================  ===========================
check           selected by                                                           runs
==============  ====================================================================  ===========================
file-size       always                                                                scripts/file_size_ratchet.py check
changelog       always                                                                scripts/changelog_fold.py check
context-budget  always (under a second); a path context_budgets.py covers (.md           scripts/context_budgets.py check
                under <plugin>/skills|agents|commands|resources of conexus, sn,
                prose-edit, tool-spill, its ceilings file, the script) is named
wire-ledger     what check_wire_contract_pairing.py treats as wire (service/, the    scripts/check_wire_contract_pairing.py
                client http_* and indexer modules, tests with a raw envelope), or
                the ledger docs/wire-contract-pending.md itself
cloud-leg       docs/ (the ledger is under it)                                       tests/scripts/test_cloud_leg.py
pg-cluster      the throwaway-cluster setup: tests/_engine_substrate.py,              tests/test_integration_pg_cluster.py
                _tenant_drop.py, _child_process.py, _ddl_cluster_sites.py,            (-m "not slow": the integration marker
                _tmp_root.py, that test file, or any changed tests/ .py that          is not excluded; no engine needed)
                names pg_data_tempdir( / integration_pg_conf( / THROWAWAY_FSYNC_CONF
plugin-ledger   conexus/, sn/, tool-spill/, prose-edit/, marketplace.json             tests/test_plugin_release_drift_ledger.py
                                                                                      (NX_REQUIRE_PLUGIN_DRIFT_CHECK=1: a tagless
                                                                                      tree fails instead of skipping)
docs-readers    docs/ (heavy: a PROOF, never a live run)                             proof of scripts/select_docs_reader_tests.py
write-seam      ci.yml's write-seam-gate path filter (heavy: a PROOF)                 proof of the write-seam pytest
==============  ====================================================================  ===========================

A heavy leg needs ``--proof <status file>``: the JSON ``scripts/watched_pytest.py --status`` writes, which now carries the
tree sha it ran on (``tree``), whether the checkout was clean (``tree_dirty``) and pytest's summary line. A proof counts
only when its tree is the tip's tree, the run was clean, its command is the leg's command (every selected file, the
markers CI uses, no option that narrows the run, exactly one ``-m``), the state is ``passed`` and the summary shows enough
tests passing and none failed, erroring or (for the write-seam, whose prerequisite skip is how it goes vacuous) skipped. An
accepted proof is printed (tree, start, duration, host) and its particulars go into the push record.

A selected check whose script is missing from the tip, or whose run passes without the marker a real run prints (a
vacuous pass), or a pytest check that skips any test (except the one named opt-in skip of the cloud-leg file), is a refusal,
never a skip. ``--skip NAME=REASON`` overrides one check, on the record: the reason is
printed as ``PUSH_CHECK_SKIPPED NAME reason=...`` (the push script hands those lines to ``nx ci record-push``).

Exit codes: 0 every selected check passed (or was skipped by name), 14 a refusal (``PUSH_REFUSED_<CHECK>`` lines on standard
output, each with the command to reproduce it), 15 the gate could not run (``PUSH_REFUSED_CHECK_GATE_ERROR``). Nothing
here passes silently on a fault. A checkout whose remote branch has no ``scripts/file_size_ratchet.py`` is not armed:
one ``PUSH_CHECKS_SKIPPED`` line on stderr and exit 0 (the base decides, so a push cannot disarm the gate by deleting it).

Stdlib only, Python 3.11 or newer (the wire check reads ``tomllib``): the push script picks the interpreter.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

EXIT_OK = 0
EXIT_REFUSED = 14
EXIT_GATE_ERROR = 15

#: The check a branch must carry for the gate to be armed on it.
ARMING_SCRIPT = "scripts/file_size_ratchet.py"
LEDGER_PATH = "docs/wire-contract-pending.md"
PLUGIN_ROOTS = ("conexus/", "sn/", "tool-spill/", "prose-edit/")
PLUGIN_FILES = (".claude-plugin/marketplace.json",)

#: ci.yml's write-seam-gate ``seam`` path filter (the dorny/paths-filter list). tests/scripts/test_push_check_gate.py parses
#: ci.yml and fails when this drifts from it.
WRITE_SEAM_EXACT = (
    "src/nexus/db/http_vector_client.py",
    "service/src/main/java/dev/nexus/service/http/VectorHandler.java",
    "service/src/main/java/dev/nexus/service/http/AuthFilter.java",
    "service/src/main/java/dev/nexus/service/vectors/PgVectorRepository.java",
    "service/src/main/java/dev/nexus/service/db/CatalogRepository.java",
    "service/src/main/java/dev/nexus/service/http/CatalogHandler.java",
    "src/nexus/catalog/http_catalog_client.py",
    "src/nexus/corpus.py",
    "src/nexus/chunk_identity.py",
    "tests/db/test_write_seam_gate_integration.py",
    "tests/db/test_http_combined_query_integration.py",
    "scripts/assert_write_seam_ran.py",
    "tests/db/conftest.py",
    "tests/db/_service_fixture.py",
    ".github/workflows/ci.yml",
    "uv.lock",
    "pyproject.toml",
    "service/pom.xml",
)
WRITE_SEAM_PREFIXES = (
    "service/src/main/resources/db/changelog/",
    ".github/actions/maven-cache/",
)
WRITE_SEAM_TESTS = (
    "tests/db/test_write_seam_gate_integration.py",
    "tests/db/test_http_combined_query_integration.py",
)

PYTEST_BASE = ("-m", "pytest", "-p", "no:cacheprovider", "-q", "-rs", "-o", "addopts=")
FAST_MARKERS = "not integration and not slow"
DEFAULT_CHECK_TIMEOUT_S = 300
#: pg-cluster (halcyon, after the develop red on 2776c549e3): tests/test_integration_pg_cluster.py pins the exact lines of the
#: throwaway clusters' postgresql.conf and the census of every file that makes one. 96 of its 99 tests run in the default loop
#: and 3 in the lint bucket, so a push that changed the conf (a new line in THROWAWAY_FSYNC_CONF) reached develop red. It needs no
#: engine and no PG bundle (injected mount tables, fake process trees), so it runs live (99 tests, about 20 s), with the
#: integration marker NOT excluded (``-m "not slow"``): if a test there ever needs an engine, it is a proof leg instead.
CLUSTER_CENSUS = "tests/test_integration_pg_cluster.py"
CLUSTER_MARKERS = "not slow"
CLUSTER_PATHS = frozenset({
    "tests/_engine_substrate.py", "tests/_tenant_drop.py", "tests/_child_process.py", "tests/_ddl_cluster_sites.py",
    "tests/_tmp_root.py", CLUSTER_CENSUS,
})
#: A changed test that makes a throwaway cluster, applies its conf or names the conf constant selects the census too.
CLUSTER_CONTENT = r"pg_data_tempdir\(|integration_pg_conf\(|apply_reloadable_throwaway_conf\(|THROWAWAY_FSYNC_CONF"
#: What scripts/context_budgets.py covers when the tip's copy cannot be read (the script itself is reported missing then).
CONTEXT_PLUGINS = ("conexus", "sn", "prose-edit", "tool-spill")
CONTEXT_CEILINGS = "tests/fixtures/context-budgets.txt"
CONTEXT_SCRIPT = "scripts/context_budgets.py"
MAX_REPORT_LINES = 40
MAX_REPORT_CHARS = 3500


@dataclass(frozen=True)
class Spec:
    """One check: its name (the ``--skip`` key), the refusal token, the files it needs, what a real pass prints."""

    name: str
    token: str
    kind: str                       # "run" or "proof"
    needs: tuple[str, ...]
    reproduce: str                  # the command to reproduce it, run at the tip's tree
    fix: str
    marker: str = ""                # a regex a passing run's output must match (a vacuous pass has none)
    min_passed: int = 0             # pytest checks: at least this many tests must pass
    skip_ok: str = ""               # pytest checks: the one skip reason (a substring) that is known and legitimate
    env: tuple[tuple[str, str], ...] = ()   # extra environment the check runs with


SPECS: dict[str, Spec] = {s.name: s for s in (
    Spec("file-size", "PUSH_REFUSED_FILE_SIZE", "run", ("scripts/file_size_ratchet.py",),
         "uv run python scripts/file_size_ratchet.py check",
         "split the file, or lower the ceiling with `scripts/file_size_ratchet.py update-exceptions` (a raise needs "
         "--allow-raise and the reason in the commit message)",
         marker=r"file_size_ratchet: 0 errors"),
    Spec("changelog", "PUSH_REFUSED_CHANGELOG", "run", ("scripts/changelog_fold.py",),
         "uv run python scripts/changelog_fold.py check",
         "fix the fragment under changelog/unreleased/ the report names (one fragment per bead, never CHANGELOG.md)",
         marker=r"changelog_fold check: ok"),
    Spec("context-budget", "PUSH_REFUSED_CONTEXT_BUDGET", "run", (CONTEXT_SCRIPT,),
         "uv run python scripts/context_budgets.py check",
         "cut the file or move detail into a file loaded on demand, or record a raise in tests/fixtures/context-budgets.txt with "
         "`scripts/context_budgets.py update-exceptions --allow-raise` (the reason goes in the commit message)",
         marker=r"context_budgets: [1-9]\d* files, \d+ bytes, 0 errors"),
    Spec("wire-ledger", "PUSH_REFUSED_WIRE_LEDGER", "run", ("scripts/check_wire_contract_pairing.py", LEDGER_PATH),
         "uv run python scripts/check_wire_contract_pairing.py",
         f"declare each flagged commit under `## Unshipped` in {LEDGER_PATH} (or move a shipped entry to `## Shipped`)",
         marker=r"wire-contract ledger clean"),
    Spec("cloud-leg", "PUSH_REFUSED_CLOUD_LEG", "run", ("tests/scripts/test_cloud_leg.py",),
         "uv run pytest -o addopts= -m 'not integration and not slow' tests/scripts/test_cloud_leg.py",
         f"map every route of {LEDGER_PATH} in tests/e2e/lib/cloud_leg.py (the failing test names the unmapped one)",
         # Collected 2026-11-22: 102 tests, 101 pass and 1 is the opt-in real run (NX_CLOUDLEG_REAL_RUN), the one skip allowed.
         min_passed=80, skip_ok="NX_CLOUDLEG_REAL_RUN"),
    Spec("plugin-ledger", "PUSH_REFUSED_PLUGIN_LEDGER", "run", ("tests/test_plugin_release_drift_ledger.py",),
         "uv run pytest -o addopts= -m 'not integration and not slow' tests/test_plugin_release_drift_ledger.py",
         "add every changed plugin file to conexus/PENDING_RELEASE.md (one path per bullet), or drop a shipped entry",
         # Collected 2026-11-22: 30 tests, all pass, none skips. NX_REQUIRE_PLUGIN_DRIFT_CHECK=1 turns the tagless-checkout skip
         # (9 tests) into a failure, so a pass cannot be a tagless one.
         min_passed=20, env=(("NX_REQUIRE_PLUGIN_DRIFT_CHECK", "1"),)),
    # Collected 2026-11-22: 67 files, 2790 tests (1061 on 2026-10-05, when ci.yml's floor of 600 was set).
    Spec("pg-cluster", "PUSH_REFUSED_PG_CLUSTER", "run", (CLUSTER_CENSUS,),
         "uv run pytest -o addopts= -m 'not slow' tests/test_integration_pg_cluster.py",
         "update the pinned conf lines in tests/test_integration_pg_cluster.py with the change to the throwaway cluster conf "
         "(tests/_engine_substrate.py THROWAWAY_FSYNC_CONF, WAL_CAP_CONF, apply_reloadable_throwaway_conf), in the same push",
         # Collected 2026-11-22: 99 tests (96 default loop, 3 lint), all pass, none skip.
         min_passed=80),
    Spec("docs-readers", "PUSH_REFUSED_DOCS_READERS", "proof", ("scripts/select_docs_reader_tests.py",),
         "", "", min_passed=1500),
    Spec("write-seam", "PUSH_REFUSED_WRITE_SEAM", "proof", (), "", "", min_passed=8),
)}
GATE_ERROR_TOKEN = "PUSH_REFUSED_CHECK_GATE_ERROR"
_GATE_ERROR_ESCAPE = "  override, on the record: --skip-check-gate '<reason>'"
ORDER = ("file-size", "changelog", "wire-ledger", "context-budget", "cloud-leg", "plugin-ledger", "pg-cluster", "docs-readers", "write-seam")
SKIPPABLE = (*ORDER, "check-gate")


class GateError(Exception):
    """The gate could not run: it refuses (PUSH_REFUSED_CHECK_GATE_ERROR), it never passes."""


# ── selection ───────────────────────────────────────────────────────────────

def write_seam_path(path: str) -> bool:
    return path in WRITE_SEAM_EXACT or path.startswith(WRITE_SEAM_PREFIXES)


def cluster_path(path: str) -> bool:
    return path in CLUSTER_PATHS


def select(paths: set[str], is_wire: Callable[[str], bool], is_context: Callable[[str], bool],
           is_cluster: Callable[[str], bool] = cluster_path) -> dict[str, str]:
    """The checks the outbound *paths* reach, each with the path that selected it (or ``always``)."""
    chosen: dict[str, str] = {"file-size": "always", "changelog": "always"}

    def first(pred: Callable[[str], bool]) -> str | None:
        return next((p for p in sorted(paths) if pred(p)), None)

    # Always on, because it takes under a second: a tree already over a ceiling is caught on any push, not only on one that
    # touches a plugin file. A path the check covers is still named.
    chosen["context-budget"] = first(is_context) or "always"

    if (hit := first(lambda p: p == LEDGER_PATH or is_wire(p))) is not None:
        chosen["wire-ledger"] = hit
    if (hit := first(lambda p: p.startswith("docs/"))) is not None:
        chosen["cloud-leg"] = hit
        chosen["docs-readers"] = hit
    if (hit := first(lambda p: p.startswith(PLUGIN_ROOTS) or p in PLUGIN_FILES)) is not None:
        chosen["plugin-ledger"] = hit
    if (hit := first(is_cluster)) is not None:
        chosen["pg-cluster"] = hit
    if (hit := first(write_seam_path)) is not None:
        chosen["write-seam"] = hit
    return {name: chosen[name] for name in ORDER if name in chosen}


# ── git ─────────────────────────────────────────────────────────────────────

#: Variables that change what git or pytest does and so what a check examines: the tip's tree is what is judged, not a
#: caller's ambient environment (PYTEST_ADDOPTS=-k nothing would deselect every test and still exit 0 under a floor-less check).
_SCRUBBED = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX", "PYTEST_ADDOPTS", "PYTEST_PLUGINS",
             "PYTEST_DISABLE_PLUGIN_AUTOLOAD")


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED}
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return env


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", *args], capture_output=True, text=True,
                          env=_env(), check=False)
    if check and proc.returncode != 0:
        raise GateError(f"git {' '.join(args[:3])} failed: {(proc.stderr or proc.stdout).strip()[-300:]}")
    return proc.stdout


def _in_base(repo: Path, rev: str, base: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", rev, base], capture_output=True,
                          env=_env(), check=False).returncode == 0


def outbound_paths(repo: Path, base: str, tip: str) -> set[str]:
    """Every path the outbound range touches: the net diff of the two trees, plus each non-merge commit's own diff (so a path
    touched and reverted inside the range still selects its checks), plus what each merge brings to the base side.

    A merge contributes ONE diff, against the parent already in *base* (else its first parent): what it adds to develop.
    ``git log -m`` diffs a merge against every parent, so merging a stale branch counted everything develop had changed since
    the branch's base, files the push never changes relative to develop."""
    paths: set[str] = set()
    chunks = [_git(repo, "diff", "--name-only", "--no-renames", "-z", base, tip),
              _git(repo, "log", "--no-merges", "--name-only", "--no-renames", "--format=", "-z", f"{base}..{tip}")]
    for line in _git(repo, "rev-list", "--merges", "--parents", f"{base}..{tip}").splitlines():
        merge, *parents = line.split()
        side = next((p for p in parents if _in_base(repo, p, base)), parents[0])
        chunks.append(_git(repo, "diff", "--name-only", "--no-renames", "-z", side, merge))
    for chunk in chunks:
        paths.update(p for p in chunk.split("\0") if p.strip())
    return paths


def cluster_content_paths(repo: Path, tip: str, paths: set[str]) -> set[str]:
    """The changed ``tests/**/*.py`` among *paths* whose content at *tip* names the throwaway-cluster setup (``pg_data_tempdir(``,
    ``integration_pg_conf(``, ``apply_reloadable_throwaway_conf(``, ``THROWAWAY_FSYNC_CONF``)."""
    candidates = sorted(p for p in paths if p.startswith("tests/") and p.endswith(".py") and has_in_tree(repo, tip, p))
    found: set[str] = set()
    for i in range(0, len(candidates), 100):
        out = _git(repo, "grep", "-l", "-E", CLUSTER_CONTENT, tip, "--", *candidates[i:i + 100], check=False)
        found.update(line.split(":", 1)[1] for line in out.splitlines() if ":" in line)
    return found


def has_in_tree(repo: Path, rev: str, path: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{rev}:{path}"], capture_output=True, env=_env(),
                          check=False).returncode == 0


class Tree:
    """The tip's tree as a detached worktree under *workdir*, removed on every exit path."""

    def __init__(self, repo: Path, tip: str, workdir: Path) -> None:
        self.repo, self.tip, self.workdir = repo, tip, workdir
        self.path = Path(tempfile.mkdtemp(prefix="pc-", dir=workdir)) / "tree"

    def __enter__(self) -> Path:
        try:
            _git(self.repo, "worktree", "add", "-q", "--detach", str(self.path), self.tip)
        except GateError:
            self._remove()
            raise
        return self.path

    def __exit__(self, *exc: object) -> None:
        self._remove()

    def _remove(self) -> None:
        subprocess.run(["git", "-C", str(self.repo), "worktree", "remove", "--force", str(self.path)], capture_output=True,
                       env=_env(), check=False)
        if self.path.exists():
            gitdir = self._linked_gitdir()
            shutil.rmtree(self.path, ignore_errors=True)
            if gitdir is not None:
                shutil.rmtree(gitdir, ignore_errors=True)
        shutil.rmtree(self.path.parent, ignore_errors=True)

    def _linked_gitdir(self) -> Path | None:
        try:
            text = (self.path / ".git").read_text(encoding="utf-8")
        except OSError:
            return None
        match = re.match(r"gitdir:\s*(.+)", text.strip())
        return Path(match.group(1)) if match else None


# ── running a check ─────────────────────────────────────────────────────────

_SUMMARY = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)")


def summary_counts(text: str) -> dict[str, int]:
    """pytest's counts from a summary line (``131 passed, 1 skipped in 12.6s``); ``error`` and ``errors`` are one key."""
    counts: dict[str, int] = {}
    for number, word in _SUMMARY.findall(text or ""):
        key = "error" if word.startswith("error") else word
        counts[key] = counts.get(key, 0) + int(number)
    return counts


@dataclass
class Outcome:
    name: str
    ok: bool
    detail: str = ""                # why it is red: the excerpt of its output, or the vacuity reason
    seconds: float = 0.0
    lines: list[str] = field(default_factory=list)


_NOISE = re.compile(r"^(nexus test tmp:|tenant pool:|-+ posix_spawn census|\d+ spawns:|NOTE: nexus-)")
_WARNINGS = re.compile(r"=+ warnings summary =+.*?(?==+ short test summary info =+|\Z)", re.S)


def _excerpt(text: str) -> str:
    """The tail of a check's output that says why it is red: pytest's warnings block and this suite's own footer lines are
    dropped, then the last :data:`MAX_REPORT_LINES` lines are kept."""
    text = _WARNINGS.sub("", text)
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip() and not _NOISE.match(ln)]
    kept = lines[-MAX_REPORT_LINES:]
    out = "\n".join(kept)
    return out[-MAX_REPORT_CHARS:]


def pytest_python(repo: Path, python: str) -> list[str]:
    """The interpreter command that can import pytest: *python*, else ``uv run --frozen --no-sync --project <repo> python``
    (it never touches the lock file or the environment); a GateError when neither can, naming the remedy."""
    if subprocess.run([python, "-c", "import pytest"], capture_output=True, env=_env(), check=False).returncode == 0:
        return [python]
    uv = shutil.which("uv")
    if uv is None:
        raise GateError(f"{python} has no pytest and uv is not on PATH: run `uv sync` in {repo} (or set NX_PUSH_CHECK_PYTHON "
                        "to an interpreter that has pytest)")
    via_uv = [uv, "run", "--frozen", "--no-sync", "--project", str(repo), "python"]
    if subprocess.run([*via_uv, "-c", "import pytest"], capture_output=True, env=_env(), check=False).returncode != 0:
        raise GateError(f"{python} has no pytest, and neither does {repo}'s environment (uv run --frozen --no-sync): run "
                        "`uv sync` in it, or set NX_PUSH_CHECK_PYTHON to an interpreter that has pytest")
    return via_uv


def command_for(name: str, python: list[str], pytest_cmd: list[str]) -> list[str]:
    if name == "file-size":
        return [*python, "scripts/file_size_ratchet.py", "check"]
    if name == "changelog":
        return [*python, "scripts/changelog_fold.py", "check"]
    if name == "wire-ledger":
        return [*python, "scripts/check_wire_contract_pairing.py"]
    if name == "context-budget":
        return [*python, "scripts/context_budgets.py", "check"]
    if name == "cloud-leg":
        return [*pytest_cmd, *PYTEST_BASE, "-m", FAST_MARKERS, "tests/scripts/test_cloud_leg.py"]
    if name == "plugin-ledger":
        return [*pytest_cmd, *PYTEST_BASE, "-m", FAST_MARKERS, "tests/test_plugin_release_drift_ledger.py"]
    if name == "pg-cluster":
        return [*pytest_cmd, *PYTEST_BASE, "-m", CLUSTER_MARKERS, CLUSTER_CENSUS]
    raise GateError(f"no command for check {name}")


_CHILDREN: set[subprocess.Popen] = set()


def check_timeout_s() -> int:
    """How long one check may run: ``NX_PUSH_CHECK_TIMEOUT_S`` (a positive whole number), else 300."""
    text = os.environ.get("NX_PUSH_CHECK_TIMEOUT_S", "")
    return int(text) if text.isdigit() and int(text) > 0 else DEFAULT_CHECK_TIMEOUT_S


def kill_children() -> None:
    """Kill every check still running, with whatever it started (each runs in its own session). Never raises. For the gate
    being stopped; a check that merely ran past its time kills only itself."""
    for child in list(_CHILDREN):
        _kill_group(child)


def _kill_group(child: subprocess.Popen) -> None:
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except OSError:
        pass


def _run_child(command: list[str], cwd: Path, env: dict[str, str], timeout: int | None = None) -> tuple[int, str]:
    """Run *command* in its own session, registered so a TERM to the gate stops it; its exit code and (stdout + stderr).
    Raises ``subprocess.TimeoutExpired`` past *timeout* (default :func:`check_timeout_s`), after killing THIS command and what
    it started, and nothing else: a sibling check is not stopped by another's timeout."""
    limit = timeout or check_timeout_s()
    child = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             start_new_session=True)
    _CHILDREN.add(child)
    try:
        out, _ = child.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        _kill_group(child)
        child.communicate()
        raise
    finally:
        _CHILDREN.discard(child)
    return child.returncode, out or ""


_SKIPPED_LINE = re.compile(r"^SKIPPED \[(\d+)\] (.*)$", re.M)


def skip_problem(name: str, out: str) -> str | None:
    """Why the skips in a pytest check's *out* make a pass vacuous, or None. A skipped test examined nothing, so none is
    allowed except the check's one named, known opt-in skip (``Spec.skip_ok``, matched on the skip reason)."""
    spec = SPECS[name]
    summary = next((ln for ln in reversed(out.splitlines()) if _SUMMARY.search(ln) and " in " in ln), "")
    skipped = summary_counts(summary).get("skipped", 0)
    lines = [(int(n), reason) for n, reason in _SKIPPED_LINE.findall(out)]
    allowed = sum(n for n, reason in lines if spec.skip_ok and spec.skip_ok in reason)
    other = [reason for n, reason in lines if not (spec.skip_ok and spec.skip_ok in reason)]
    if skipped == allowed and not other:
        return None
    what = f"; the one allowed skip is the {spec.skip_ok} opt-in" if spec.skip_ok else ""
    return (f"{skipped} test(s) skipped{what}: a skipped test examined nothing, so a pass with skips is vacuous, not green "
            f"(skip reasons: {'; '.join(other)[:300] or 'not printed'})")


def run_check(spec: Spec, tree: Path, python: list[str], pytest_cmd: list[str]) -> Outcome:
    started = time.monotonic()
    missing = [n for n in spec.needs if not (tree / n).exists()]
    if missing:
        return Outcome(spec.name, False, f"the tip's tree has no {', '.join(missing)}: a selected check that is missing is a "
                       "refusal, not a skip", time.monotonic() - started)
    env = _env()
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(tree / "src"), env.get("PYTHONPATH", "")) if p)
    env["NX_TEST_T2_SUBSTRATE"] = "none"
    env.update(dict(spec.env))
    try:
        returncode, out = _run_child(command_for(spec.name, python, pytest_cmd), tree, env)
    except subprocess.TimeoutExpired as exc:
        return Outcome(spec.name, False, f"ran past {exc.timeout:.0f}s and was stopped (only this check was stopped)",
                       time.monotonic() - started)
    except OSError as exc:
        return Outcome(spec.name, False, f"could not start: {exc}", time.monotonic() - started)
    seconds = time.monotonic() - started
    if returncode != 0:
        return Outcome(spec.name, False, _excerpt(out), seconds)
    if spec.marker and not re.search(spec.marker, out):
        return Outcome(spec.name, False, f"exit 0 but its output has no /{spec.marker}/: a pass that examined nothing is "
                       f"vacuous, not green. Last lines:\n{_excerpt(out)}", seconds)
    if spec.min_passed:
        counts = summary_counts(next((ln for ln in reversed(out.splitlines()) if _SUMMARY.search(ln) and " in " in ln), ""))
        if counts.get("passed", 0) < spec.min_passed:
            return Outcome(spec.name, False, f"exit 0 but only {counts.get('passed', 0)} test(s) passed (floor "
                           f"{spec.min_passed}): a run that skipped its corpus is vacuous, not green. Last lines:\n"
                           f"{_excerpt(out)}", seconds)
        if (problem := skip_problem(spec.name, out)) is not None:
            return Outcome(spec.name, False, f"exit 0 but {problem}. Last lines:\n{_excerpt(out)}", seconds)
    return Outcome(spec.name, True, "", seconds)


# ── proofs ──────────────────────────────────────────────────────────────────

def _pytest_args(command: list[str]) -> list[str]:
    """The arguments after the ``pytest`` word (``uv run pytest ...`` or ``python -m pytest ...``), so a leading ``python -m``
    is not mistaken for a marker option."""
    for i, arg in enumerate(command):
        if arg == "pytest" or arg.endswith(("/pytest", "\\pytest")):
            return command[i + 1:]
    return []


def _opt_value(args: list[str], flag: str) -> str | None:
    for i, arg in enumerate(args[:-1]):
        if arg == flag:
            return args[i + 1]
    return None


#: pytest options that narrow, stop early or only list a run, matched as a PREFIX (``-kEXPR``, ``--ignore=x``, ``-xvs``), so
#: a proof of the whole leg carries none. ``--co`` is matched exactly: ``--color`` is not narrowing.
_NARROWING_PREFIXES = ("-k", "--keyword", "--ignore", "--ignore-glob", "--deselect", "--lf", "--last-failed", "--sw",
                       "--stepwise", "--maxfail", "-x", "--exitfirst", "--collect-only")
_NARROWING_EXACT = ("--co",)


def _narrowing(args: list[str]) -> str | None:
    return next((a for a in args if a.startswith(_NARROWING_PREFIXES) or a in _NARROWING_EXACT), None)


def _config_problem(args: list[str]) -> str | None:
    """Why *args* change the configuration pytest reads, or None. Allowed: ``-o addopts=`` and the ``-o faulthandler_timeout=N``
    that scripts/watched_pytest.py appends. Refused, spaced or attached: any other ``-o`` / ``--override-ini``, ``-c`` /
    ``--config-file``, ``--rootdir`` and ``-p`` (they swap the ini file, the root, the cache or the plugins a run loads)."""
    def short(a: str, letter: str) -> bool:           # -X or -Xvalue, never a --long option
        return a.startswith(f"-{letter}") and not a.startswith("--")

    for i, a in enumerate(args):
        if a in ("-o", "--override-ini"):
            value = args[i + 1] if i + 1 < len(args) else ""
            if value != "addopts=" and not re.fullmatch(r"faulthandler_timeout=\d+", value):
                return (f"its command passes {a} {value!r}: only -o addopts= (and watched_pytest's faulthandler_timeout) may change "
                        "the configuration, so a smuggled option could narrow the run (addopts, cache_dir, ...)")
        elif short(a, "o") or a.startswith("--override-ini="):
            return f"its command passes {a!r}: -o must be the spaced -o addopts= (an attached override can change the configuration)"
        elif a in ("-c", "--config-file", "--rootdir", "-p") or short(a, "c") or short(a, "p") or a.startswith(("--config-file=", "--rootdir=")):
            return f"its command passes {a!r}, which swaps the configuration, root or plugins a run loads (-c, --config-file, --rootdir, -p)"
    return None


def proof_problem(proof: dict, leg: str, tree_sha: str, expected_files: list[str]) -> str | None:
    """Why *proof* (a parsed status file) does not prove *leg* on the tree *tree_sha*, or None when it does."""
    spec = SPECS[leg]
    command = [str(a) for a in proof.get("command") or []]
    if proof.get("state") != "passed" or proof.get("exit_code") != 0:
        return f"state {proof.get('state')!r}, exit {proof.get('exit_code')!r}: the run did not pass"
    if not proof.get("tree"):
        return "it carries no tree sha: produce it with this checkout's scripts/watched_pytest.py, which stamps it"
    if proof["tree"] != tree_sha:
        return f"it ran on tree {str(proof['tree'])[:12]}, the tip's tree is {tree_sha[:12]}"
    if proof.get("tree_dirty") is not False:
        return "the checkout it ran on was not clean (tree_dirty is not false): commit first, then run"
    args = _pytest_args(command)
    if not args:
        return "its command is not a pytest run"
    missing = [f for f in expected_files if f not in command]
    if missing:
        return f"its command does not name {len(missing)} of the {len(expected_files)} files of the leg (first: {missing[0]})"
    if (narrowing := _narrowing(args)) is not None:
        return f"its command narrows or cuts short the run ({narrowing}; -k, --ignore, --deselect, --lf, --sw, --maxfail, -x, --co)"
    addopts = [a for a in args if a.startswith("addopts=")]
    if not addopts or any(a != "addopts=" for a in addopts):
        return "its command does not clear addopts exactly (-o addopts=), so tests were deselected or options were smuggled in"
    if (config := _config_problem(args)) is not None:
        return config
    expected = FAST_MARKERS if leg == "docs-readers" else "integration"
    markers = [a for a in args if a == "-m" or (a.startswith("-m") and not a.startswith("--"))]
    if len(markers) != 1 or markers[0] != "-m":
        return f"its command must carry exactly one -m {expected!r}, spaced (found {markers or 'none'})"
    marker = _opt_value(args, "-m")
    if marker != expected:
        return f"its marker is {marker!r}, CI runs -m {expected!r}"
    counts = summary_counts(str(proof.get("summary") or ""))
    if not counts:
        return "it carries no pytest summary line (rerun with this checkout's scripts/watched_pytest.py)"
    if counts.get("failed") or counts.get("error"):
        return f"the summary shows failures or errors: {proof.get('summary')}"
    if leg == "write-seam" and counts.get("skipped"):
        return ("the summary shows skipped tests: a skip here is the missing jar, java or docker prerequisite, which makes "
                f"the gate vacuous ({proof.get('summary')})")
    if counts.get("passed", 0) < spec.min_passed:
        return f"only {counts.get('passed', 0)} test(s) passed, the floor is {spec.min_passed}"
    return None


def docs_reader_files(tree: Path, python: str) -> list[str]:
    proc = subprocess.run([python, "scripts/select_docs_reader_tests.py", "--repo", "."], cwd=tree, capture_output=True,
                          text=True, env=_env(), check=False)
    if proc.returncode != 0:
        raise GateError(f"scripts/select_docs_reader_tests.py failed at the tip: {(proc.stderr or proc.stdout).strip()[-300:]}")
    return proc.stdout.split()


def proof_commands(leg: str, files: list[str], tip: str) -> str:
    """How to produce the proof of *leg*: the same three steps for both legs (AGENTS.md rule 13: only the engine build runs
    unwrapped, as ``scripts/build-gate-jar.sh --lab-run <class>``; the Python run goes through lab-remote, under lab-run).
    (a) warms lab-remote's copy of the clean worktree on chas, (b) builds the jar in that very copy over ssh, (c) runs pytest
    through lab-remote against the same copy, which keeps the jar. Both legs need the jar: the write-seam boots it, the docs
    readers use the engine substrate."""
    if leg == "docs-readers":
        status, env, pytest = "docs.json", "", f"-o addopts= -m \"{FAST_MARKERS}\" {' '.join(files)}"
    else:
        status, env, pytest = "seam.json", "env NX_REQUIRE_SERVICE_JAR=1 ", f"-o addopts= -m integration {' '.join(WRITE_SEAM_TESTS)}"
    return (f"three steps on chas (AGENTS.md rule 13: only the engine build runs unwrapped; the pytest run goes through lab-remote). "
            f"(a) from a clean worktree at {tip[:12]}: lab-remote --host chas -c lab-s -- true   (it warms the copy; it prints the "
            "copy's path as `lab-remote: chas lab-s in <path>`, under nxtest's home)   "
            "(b) ssh nxtest@chas, cd into that copy (the path (a) printed), then: scripts/build-gate-jar.sh --lab-run lab-m   "
            f"(c) from the laptop, in the same worktree: lab-remote --host chas -c lab-m --fetch {status} -- {env}python3 "
            f"scripts/watched_pytest.py --status {status} -- uv run pytest {pytest}")


_PROOF_FILES = {"docs-readers": "docs.json", "write-seam": "seam.json"}


def repush_line(prefix: str, suffix: str, legs: list[str]) -> str:
    """The push command again with a ``--proof`` for each leg in *legs*, between the flags (*prefix*) and the shas (*suffix*)."""
    proofs = " ".join(f"--proof {_PROOF_FILES[leg]}" for leg in legs)
    return " ".join(part for part in (prefix, proofs, suffix) if part)


def proof_line(leg: str, proof: dict, path: Path) -> str:
    """The summary line of an accepted proof: its tree, when it started, how long it ran and where, which the push record
    carries too. A field the status file does not have is ``unknown``, never invented."""
    started, elapsed = proof.get("started_at"), proof.get("elapsed_s")
    when = (datetime.fromtimestamp(float(started), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if isinstance(started, (int, float)) else "unknown")
    seconds = str(round(float(elapsed))) if isinstance(elapsed, (int, float)) else "unknown"
    host = re.sub(r"\s+", "_", str(proof.get("host") or "unknown").strip()) or "unknown"
    return (f"PUSH_PROOF_ACCEPTED {leg} tree={str(proof.get('tree', ''))[:12]} started_at={when} elapsed_s={seconds} "
            f"host={host} file={path.name}")


# ── the gate ────────────────────────────────────────────────────────────────

def _load_module(script: Path, name: str):  # noqa: ANN202 -- a module object
    spec = importlib.util.spec_from_file_location(name, script)
    if spec is None or spec.loader is None:
        raise GateError(f"cannot load the tip's {script.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module          # its dataclasses resolve their field types through sys.modules
    try:
        spec.loader.exec_module(module)
    except (ImportError, SyntaxError) as exc:
        raise GateError(f"cannot load the tip's {script.name}: {exc}") from exc
    finally:
        sys.modules.pop(spec.name, None)
    return module


def _load_wire_predicates(tree: Path, repo: Path, tip: str) -> Callable[[str], bool]:
    """What the TIP's check_wire_contract_pairing.py treats as wire: the engine tree, the client modules, and a test
    carrying a raw envelope. Read from the tip's own copy, so the gate selects by the rule the check runs by."""
    script = tree / "scripts" / "check_wire_contract_pairing.py"
    if not script.exists():
        # The wire check itself is reported missing when it is selected; with no script to read the rule from, select on
        # the engine tree, the one part that is the same by definition.
        return lambda p: p.startswith("service/")
    module = _load_module(script, "_push_gate_wire_pairing")
    try:
        engine, client, envelope = module._is_engine_path, module._is_client_module_path, module._is_client_test_envelope
    except AttributeError as exc:
        raise GateError("the tip's scripts/check_wire_contract_pairing.py no longer has the predicates this gate reads "
                        f"(_is_engine_path, _is_client_module_path, _is_client_test_envelope): {exc}") from exc
    return lambda p: bool(engine(p) or client(p) or envelope(tip, p, repo_root=repo))


def _load_context_predicate(tree: Path) -> Callable[[str], bool]:
    """What the TIP's context_budgets.py covers: a ``.md`` under ``<plugin>/<kind>/``, its ceilings file, and itself. Read from
    the tip's own copy (PLUGINS, KINDS, CEILINGS); with no copy to read, every path under a plugin selects the check, which
    is then reported missing."""
    script = tree / CONTEXT_SCRIPT
    if not script.exists():
        return lambda p: p.startswith(tuple(f"{plugin}/" for plugin in CONTEXT_PLUGINS)) or p in (CONTEXT_CEILINGS, CONTEXT_SCRIPT)
    module = _load_module(script, "_push_gate_context_budgets")
    try:
        prefixes = tuple(f"{plugin}/{kind}/" for plugin in module.PLUGINS for kind in module.KINDS)
        ceilings = module.CEILINGS
    except AttributeError as exc:
        raise GateError(f"the tip's {CONTEXT_SCRIPT} no longer has PLUGINS, KINDS and CEILINGS, which this gate reads: {exc}") from exc
    return lambda p: (p.endswith(".md") and p.startswith(prefixes)) or p in (ceilings, CONTEXT_SCRIPT)


def _reproduce_note(tip: str) -> str:
    return (f"to reproduce at the tip: git worktree add --detach ../pc-{tip[:12]} {tip} && cd ../pc-{tip[:12]} && uv sync")


def run_gate(repo: Path, base: str, tip: str, proofs: list[Path], skips: dict[str, str], workdir: Path, python: str,
             out=sys.stdout, err=sys.stderr, repush: tuple[str, str] = ("", "")) -> int:
    if "check-gate" in skips:
        print(f"PUSH_CHECK_SKIPPED check-gate reason={skips['check-gate']}", file=out)
        return EXIT_OK
    if not has_in_tree(repo, base, ARMING_SCRIPT):
        print(f"PUSH_CHECKS_SKIPPED reason=the remote branch has no {ARMING_SCRIPT}, so the check gate is not armed here",
              file=err)
        return EXIT_OK
    started = time.monotonic()
    tree_sha = _git(repo, "rev-parse", f"{tip}^{{tree}}").strip()
    refusals: list[str] = []
    skipped: list[str] = []
    ran: list[str] = []
    missing_proofs: list[str] = []
    with Tree(repo, tip, workdir) as tree:
        paths = outbound_paths(repo, base, tip)
        by_content = cluster_content_paths(repo, tip, paths)
        selected = select(paths, _load_wire_predicates(tree, repo, tip), _load_context_predicate(tree),
                          lambda p: cluster_path(p) or p in by_content)
        runnable = [n for n in selected if SPECS[n].kind == "run"]
        proof_legs = [n for n in selected if SPECS[n].kind == "proof"]
        pytest_cmd = [python]
        if any(SPECS[n].min_passed and n not in skips for n in runnable):
            pytest_cmd = pytest_python(repo, python)
        specs = {n: SPECS[n] for n in selected}

        def one(name: str) -> Outcome:
            return run_check(specs[name], tree, [python], pytest_cmd)

        outcomes: dict[str, Outcome] = {}
        live = [n for n in runnable if n not in skips]
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(live))) as pool:
            for name, outcome in zip(live, pool.map(one, live)):
                outcomes[name] = outcome
        for name in runnable:
            if name in skips:
                skipped.append(name)
                print(f"PUSH_CHECK_SKIPPED {name} reason={skips[name]}", file=out)
                continue
            outcome = outcomes[name]
            if outcome.ok:
                ran.append(f"{name}({outcome.seconds:.1f}s)")
                continue
            spec = SPECS[name]
            refusals.append(name)
            print(f"{spec.token} {name} is red at the tip's tree ({tip[:12]}); selected by {selected[name]}.", file=out)
            print(outcome.detail, file=out)
            print(f"  reproduce: {spec.reproduce}   ({_reproduce_note(tip)})", file=out)
            if spec.fix:
                print(f"  fix: {spec.fix}", file=out)
            print(f"  override, on the record: --skip-{name} '<reason>'", file=out)
        loaded_proofs = _load_proofs(proofs)
        for name in proof_legs:
            if name in skips:
                skipped.append(name)
                print(f"PUSH_CHECK_SKIPPED {name} reason={skips[name]}", file=out)
                continue
            if name == "docs-readers" and not (tree / "scripts" / "select_docs_reader_tests.py").exists():
                refusals.append(name)
                print(f"{SPECS[name].token} {name} is selected by {selected[name]} but the tip's tree has no "
                      "scripts/select_docs_reader_tests.py: a selected check that is missing is a refusal, not a skip.",
                      file=out)
                print(f"  override, on the record: --skip-{name} '<reason>'", file=out)
                continue
            files = docs_reader_files(tree, python) if name == "docs-readers" else list(WRITE_SEAM_TESTS)
            problems: list[str] = []
            accepted: tuple[Path, dict] | None = None
            for path, proof in loaded_proofs:
                problem = proof_problem(proof, name, tree_sha, files) if isinstance(proof, dict) else str(proof)
                if problem is None:
                    accepted = (path, proof)
                    break
                problems.append(f"{path.name}: {problem}")
            if accepted is not None:
                print(proof_line(name, accepted[1], accepted[0]), file=out)
                ran.append(f"{name}(proof)")
                continue
            refusals.append(name)
            missing_proofs.append(name)
            print(f"{SPECS[name].token} {name} needs a proof of a green run on this tree and has none: selected by "
                  f"{selected[name]}; tree {tree_sha[:12]}.", file=out)
            for line in problems:
                print(f"  rejected proof {line}", file=out)
            print(f"  produce it: {proof_commands(name, files, tip)}", file=out)
            print(f"  override, on the record: --skip-{name} '<reason>'", file=out)
    elapsed = time.monotonic() - started
    if missing_proofs and repush[0]:
        print(f"then re-push: {repush_line(repush[0], repush[1], missing_proofs)}", file=out)
    if refusals:
        print(f"PUSH_CHECKS_REFUSED refused={','.join(refusals)} ran={','.join(ran) or 'none'} in {elapsed:.1f}s", file=err)
        return EXIT_REFUSED
    print(f"PUSH_CHECKS_PASSED tree={tree_sha[:12]} ran={','.join(ran) or 'none'} skipped={','.join(skipped) or 'none'} "
          f"in {elapsed:.1f}s", file=err)
    return EXIT_OK


def _load_proofs(paths: list[Path]) -> list[tuple[Path, object]]:
    loaded: list[tuple[Path, object]] = []
    for path in paths:
        try:
            loaded.append((path, json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError) as exc:
            loaded.append((path, f"cannot read it as JSON: {exc}"))
    return loaded


def parse_skip(text: str) -> tuple[str, str]:
    name, sep, reason = text.partition("=")
    if not sep or name not in SKIPPABLE or not reason.strip():
        raise argparse.ArgumentTypeError(f"--skip takes NAME=REASON with NAME one of {', '.join(SKIPPABLE)} and a reason")
    return name, " ".join(reason.split())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--repo", type=Path, required=True, help="the checkout (any worktree of the repository)")
    parser.add_argument("--base", required=True, help="the remote branch the push replaces, e.g. refs/remotes/origin/develop")
    parser.add_argument("--tip", required=True, help="the commit to push; its tree is what the checks read")
    parser.add_argument("--proof", action="append", default=[], type=Path, help="a watched_pytest.py --status file (repeatable)")
    parser.add_argument("--skip", action="append", default=[], type=parse_skip, help="NAME=REASON (repeatable)")
    parser.add_argument("--repush-prefix", default="", help="the push command up to its shas, for the re-push line of a refusal")
    parser.add_argument("--repush-suffix", default="", help="the shas of the push command, for the re-push line of a refusal")
    parser.add_argument("--workdir", type=Path, default=None, help="a directory for the tip's tree (default: a temp dir)")
    parser.add_argument("--python", default=sys.executable, help="the interpreter the checks run under")
    args = parser.parse_args(argv)

    def _term(_signum: int, _frame: object) -> None:
        kill_children()                       # the checks first, so the worktree they read can be removed
        raise SystemExit(EXIT_GATE_ERROR)

    signal.signal(signal.SIGTERM, _term)
    own_workdir = args.workdir is None
    try:
        workdir = args.workdir or Path(tempfile.mkdtemp(prefix="push-check-gate."))
        if not workdir.is_dir():
            raise GateError(f"the work directory {workdir} does not exist")
        try:
            return run_gate(args.repo, args.base, args.tip, args.proof, dict(args.skip), workdir, args.python,
                            repush=(args.repush_prefix, args.repush_suffix))
        finally:
            if own_workdir:
                shutil.rmtree(workdir, ignore_errors=True)
    except GateError as exc:
        sys.stdout.write(f"{GATE_ERROR_TOKEN} the check gate could not run: {exc}\n{_GATE_ERROR_ESCAPE}\n")
        return EXIT_GATE_ERROR
    except OSError as exc:
        sys.stdout.write(f"{GATE_ERROR_TOKEN} the check gate could not run: {type(exc).__name__}: {exc}\n{_GATE_ERROR_ESCAPE}\n")
        return EXIT_GATE_ERROR


if __name__ == "__main__":
    sys.exit(main())
