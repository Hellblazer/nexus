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
wire-ledger     what check_wire_contract_pairing.py treats as wire (service/, the    scripts/check_wire_contract_pairing.py
                client http_* and indexer modules, tests with a raw envelope), or
                the ledger docs/wire-contract-pending.md itself
cloud-leg       docs/ (the ledger is under it)                                       tests/scripts/test_cloud_leg.py
plugin-ledger   conexus/, sn/, tool-spill/, prose-edit/, marketplace.json             tests/test_plugin_release_drift_ledger.py
docs-readers    docs/ (heavy: a PROOF, never a live run)                             proof of scripts/select_docs_reader_tests.py
write-seam      ci.yml's write-seam-gate path filter (heavy: a PROOF)                 proof of the write-seam pytest
==============  ====================================================================  ===========================

A heavy leg needs ``--proof <status file>``: the JSON ``scripts/watched_pytest.py --status`` writes, which now carries the
tree sha it ran on (``tree``), whether the checkout was clean (``tree_dirty``) and pytest's summary line. A proof counts
only when its tree is the tip's tree, the run was clean, its command is the leg's command (every selected file, the
markers CI uses), the state is ``passed`` and the summary shows enough tests passing and none failed, erroring or (for the
write-seam, whose prerequisite skip is how it goes vacuous) skipped.

A selected check whose script is missing from the tip, or whose run passes without the marker a real run prints (a
vacuous pass), is a refusal, never a skip. ``--skip NAME=REASON`` overrides one check, on the record: the reason is
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

PYTEST_BASE = ("-m", "pytest", "-p", "no:cacheprovider", "-q", "-o", "addopts=")
FAST_MARKERS = "not integration and not slow"
CHECK_TIMEOUT_S = 300
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
    why: str = ""                   # filled at selection: the paths that selected it


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
    Spec("wire-ledger", "PUSH_REFUSED_WIRE_LEDGER", "run", ("scripts/check_wire_contract_pairing.py", LEDGER_PATH),
         "uv run python scripts/check_wire_contract_pairing.py",
         f"declare each flagged commit under `## Unshipped` in {LEDGER_PATH} (or move a shipped entry to `## Shipped`)",
         marker=r"wire-contract ledger clean"),
    Spec("cloud-leg", "PUSH_REFUSED_CLOUD_LEG", "run", ("tests/scripts/test_cloud_leg.py",),
         "uv run pytest -o addopts= -m 'not integration and not slow' tests/scripts/test_cloud_leg.py",
         f"map every route of {LEDGER_PATH} in tests/e2e/lib/cloud_leg.py (the failing test names the unmapped one)",
         min_passed=40),
    Spec("plugin-ledger", "PUSH_REFUSED_PLUGIN_LEDGER", "run", ("tests/test_plugin_release_drift_ledger.py",),
         "uv run pytest -o addopts= -m 'not integration and not slow' tests/test_plugin_release_drift_ledger.py",
         "add every changed plugin file to conexus/PENDING_RELEASE.md (one path per bullet), or drop a shipped entry",
         min_passed=10),
    Spec("docs-readers", "PUSH_REFUSED_DOCS_READERS", "proof", ("scripts/select_docs_reader_tests.py",),
         "", "", min_passed=600),
    Spec("write-seam", "PUSH_REFUSED_WRITE_SEAM", "proof", (), "", "", min_passed=8),
)}
GATE_ERROR_TOKEN = "PUSH_REFUSED_CHECK_GATE_ERROR"
_GATE_ERROR_ESCAPE = "  override, on the record: --skip-check-gate '<reason>'"
ORDER = ("file-size", "changelog", "wire-ledger", "cloud-leg", "plugin-ledger", "docs-readers", "write-seam")
SKIPPABLE = (*ORDER, "check-gate")


class GateError(Exception):
    """The gate could not run: it refuses (PUSH_REFUSED_CHECK_GATE_ERROR), it never passes."""


# ── selection ───────────────────────────────────────────────────────────────

def write_seam_path(path: str) -> bool:
    return path in WRITE_SEAM_EXACT or path.startswith(WRITE_SEAM_PREFIXES)


def select(paths: set[str], is_wire: Callable[[str], bool]) -> dict[str, str]:
    """The checks the outbound *paths* reach, each with the path that selected it (or ``always``)."""
    chosen: dict[str, str] = {"file-size": "always", "changelog": "always"}

    def first(pred: Callable[[str], bool]) -> str | None:
        return next((p for p in sorted(paths) if pred(p)), None)

    if (hit := first(lambda p: p == LEDGER_PATH or is_wire(p))) is not None:
        chosen["wire-ledger"] = hit
    if (hit := first(lambda p: p.startswith("docs/"))) is not None:
        chosen["cloud-leg"] = hit
        chosen["docs-readers"] = hit
    if (hit := first(lambda p: p.startswith(PLUGIN_ROOTS) or p in PLUGIN_FILES)) is not None:
        chosen["plugin-ledger"] = hit
    if (hit := first(write_seam_path)) is not None:
        chosen["write-seam"] = hit
    return {name: chosen[name] for name in ORDER if name in chosen}


# ── git ─────────────────────────────────────────────────────────────────────

def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX")}
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return env


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", *args], capture_output=True, text=True,
                          env=_env(), check=False)
    if check and proc.returncode != 0:
        raise GateError(f"git {' '.join(args[:3])} failed: {(proc.stderr or proc.stdout).strip()[-300:]}")
    return proc.stdout


def outbound_paths(repo: Path, base: str, tip: str) -> set[str]:
    """Every path the outbound range touches: the net diff of the two trees, plus each commit's own diff (a merge's per
    parent), so a path touched and reverted inside the range still selects its checks."""
    paths: set[str] = set()
    for chunk in (_git(repo, "diff", "--name-only", "--no-renames", "-z", base, tip),
                  _git(repo, "log", "-m", "--name-only", "--no-renames", "--format=", "-z", f"{base}..{tip}")):
        paths.update(p for p in chunk.split("\0") if p.strip())
    return paths


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
    """The interpreter command that can import pytest: *python*, else ``uv run --project <repo> python``; a GateError when
    neither can, naming the remedy."""
    ok = subprocess.run([python, "-c", "import pytest"], capture_output=True, env=_env(), check=False).returncode == 0
    if ok:
        return [python]
    uv = shutil.which("uv")
    if uv is None:
        raise GateError(f"{python} has no pytest and uv is not on PATH: run `uv sync` in {repo} (or set NX_PUSH_CHECK_PYTHON "
                        "to an interpreter that has pytest)")
    return [uv, "run", "--project", str(repo), "python"]


def command_for(name: str, python: list[str], pytest_cmd: list[str]) -> list[str]:
    if name == "file-size":
        return [*python, "scripts/file_size_ratchet.py", "check"]
    if name == "changelog":
        return [*python, "scripts/changelog_fold.py", "check"]
    if name == "wire-ledger":
        return [*python, "scripts/check_wire_contract_pairing.py"]
    if name == "cloud-leg":
        return [*pytest_cmd, *PYTEST_BASE, "-m", FAST_MARKERS, "tests/scripts/test_cloud_leg.py"]
    if name == "plugin-ledger":
        return [*pytest_cmd, *PYTEST_BASE, "-m", FAST_MARKERS, "tests/test_plugin_release_drift_ledger.py"]
    raise GateError(f"no command for check {name}")


_CHILDREN: set[subprocess.Popen] = set()


def kill_children() -> None:
    """Kill every check still running, with whatever it started (each runs in its own session). Never raises."""
    for child in list(_CHILDREN):
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except OSError:
            pass


def _run_child(command: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str]:
    """Run *command* in its own session, registered so a TERM to the gate stops it; its (stdout + stderr) and exit code.
    Raises ``subprocess.TimeoutExpired`` (after killing it) past :data:`CHECK_TIMEOUT_S`."""
    child = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             start_new_session=True)
    _CHILDREN.add(child)
    try:
        out, _ = child.communicate(timeout=CHECK_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        kill_children()
        child.communicate()
        raise
    finally:
        _CHILDREN.discard(child)
    return child.returncode, out or ""


def run_check(spec: Spec, tree: Path, python: list[str], pytest_cmd: list[str]) -> Outcome:
    started = time.monotonic()
    missing = [n for n in spec.needs if not (tree / n).exists()]
    if missing:
        return Outcome(spec.name, False, f"the tip's tree has no {', '.join(missing)}: a selected check that is missing is a "
                       "refusal, not a skip", time.monotonic() - started)
    env = _env()
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(tree / "src"), env.get("PYTHONPATH", "")) if p)
    env["NX_TEST_T2_SUBSTRATE"] = "none"
    try:
        returncode, out = _run_child(command_for(spec.name, python, pytest_cmd), tree, env)
    except subprocess.TimeoutExpired:
        return Outcome(spec.name, False, f"ran past {CHECK_TIMEOUT_S}s and was stopped", time.monotonic() - started)
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


#: pytest options that narrow a run to a subset of the legs's tests, which a proof of the whole leg must not carry.
_NARROWING = ("-k", "--keyword", "--deselect", "--lf", "--last-failed", "--sw", "--stepwise", "--ff", "--maxfail")


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
    if any(arg in _NARROWING or arg.startswith(("--maxfail", "--deselect")) for arg in args):
        return "its command narrows the run (-k, --deselect, --lf, --sw, --maxfail)"
    if "addopts=" not in args:
        return "its command does not clear addopts (-o addopts=), so the lint- and integration-marked tests were deselected"
    marker = _opt_value(args, "-m")
    if leg == "docs-readers" and marker != FAST_MARKERS:
        return f"its marker is {marker!r}, CI runs -m {FAST_MARKERS!r}"
    if leg == "write-seam" and marker != "integration":
        return f"its marker is {marker!r}, the write-seam tests are integration-marked and CI runs -m integration"
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
    """How to produce the proof of *leg*, one line."""
    if leg == "docs-readers":
        return (f"lab-remote --fetch docs.json -- python scripts/watched_pytest.py --status docs.json -- uv run pytest "
                f"-o addopts= -m '{FAST_MARKERS}' {' '.join(files)}   (on a clean checkout of {tip[:12]}; then --proof docs.json)")
    return ("lab-remote --fetch seam.json -- sh -c 'scripts/build-gate-jar.sh && NX_REQUIRE_SERVICE_JAR=1 python "
            "scripts/watched_pytest.py --status seam.json -- uv run pytest -o addopts= -m integration "
            f"{' '.join(WRITE_SEAM_TESTS)}'   (a clean checkout of {tip[:12]}, Docker available; then --proof seam.json)")


# ── the gate ────────────────────────────────────────────────────────────────

def _load_wire_predicates(tree: Path, repo: Path, tip: str) -> Callable[[str], bool]:
    """What the TIP's check_wire_contract_pairing.py treats as wire: the engine tree, the client modules, and a test
    carrying a raw envelope. Read from the tip's own copy, so the gate selects by the rule the check runs by."""
    script = tree / "scripts" / "check_wire_contract_pairing.py"
    if not script.exists():
        # The wire check itself is reported missing when it is selected; with no script to read the rule from, select on
        # the engine tree, the one part that is the same by definition.
        return lambda p: p.startswith("service/")
    spec = importlib.util.spec_from_file_location("_push_gate_wire_pairing", script)
    if spec is None or spec.loader is None:
        raise GateError("cannot load the tip's scripts/check_wire_contract_pairing.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module          # its dataclasses resolve their field types through sys.modules
    try:
        spec.loader.exec_module(module)
        engine, client, envelope = module._is_engine_path, module._is_client_module_path, module._is_client_test_envelope
    except (AttributeError, ImportError, SyntaxError) as exc:
        raise GateError("the tip's scripts/check_wire_contract_pairing.py no longer has the predicates this gate reads "
                        f"(_is_engine_path, _is_client_module_path, _is_client_test_envelope): {exc}") from exc
    finally:
        sys.modules.pop(spec.name, None)
    return lambda p: bool(engine(p) or client(p) or envelope(tip, p, repo_root=repo))


def _reproduce_note(tip: str) -> str:
    return (f"to reproduce at the tip: git worktree add --detach ../pc-{tip[:12]} {tip} && cd ../pc-{tip[:12]} && uv sync")


def run_gate(repo: Path, base: str, tip: str, proofs: list[Path], skips: dict[str, str], workdir: Path, python: str,
             out=sys.stdout, err=sys.stderr) -> int:
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
    with Tree(repo, tip, workdir) as tree:
        paths = outbound_paths(repo, base, tip)
        selected = select(paths, _load_wire_predicates(tree, repo, tip))
        runnable = [n for n in selected if SPECS[n].kind == "run"]
        proof_legs = [n for n in selected if SPECS[n].kind == "proof"]
        pytest_cmd = [python]
        if any(n in ("cloud-leg", "plugin-ledger") and n not in skips for n in runnable):
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
            good = False
            for path, proof in loaded_proofs:
                problem = proof_problem(proof, name, tree_sha, files) if isinstance(proof, dict) else str(proof)
                if problem is None:
                    good = True
                    break
                problems.append(f"{path.name}: {problem}")
            if good:
                ran.append(f"{name}(proof)")
                continue
            refusals.append(name)
            print(f"{SPECS[name].token} {name} needs a proof of a green run on this tree and has none: selected by "
                  f"{selected[name]}; tree {tree_sha[:12]}.", file=out)
            for line in problems:
                print(f"  rejected proof {line}", file=out)
            print(f"  produce it: {proof_commands(name, files, tip)}", file=out)
            print(f"  override, on the record: --skip-{name} '<reason>'", file=out)
    elapsed = time.monotonic() - started
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
            return run_gate(args.repo, args.base, args.tip, args.proof, dict(args.skip), workdir, args.python)
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
