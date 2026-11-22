# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""scripts/lab-preflight: tell ``lab-remote`` before dispatch that a run would exit 75 at once (labrun-0uy.2.4).

Real files under ``tmp_path`` and real pids; nothing patches the stdlib. The drift tests compare the script's
copied constants and verdicts with the conftest / lease code they were read from, so a change there turns this
file red instead of letting the script disagree with the run it predicts.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

from tests import _suite_lease as suite_lease_module
from tests import _substrate_preflight
from tests import conftest as root_conftest
from tests.db import _service_fixture

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "lab-preflight"
JAR_REL = Path("service/target/nexus-service-1.0-SNAPSHOT.jar")
PYTEST_ARGV = ["uv", "run", "pytest", "-n", "auto", "tests/test_x.py"]


def _load(path: Path = SCRIPT) -> ModuleType:
    loader = importlib.machinery.SourceFileLoader("lab_preflight_under_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def lp() -> ModuleType:
    return _load()


def _repo(tmp_path: Path, *, jar: bool) -> Path:
    root = tmp_path / "checkout"
    root.mkdir(parents=True)
    if jar:
        (root / JAR_REL).parent.mkdir(parents=True)
        (root / JAR_REL).write_bytes(b"PK")
    return root


def _run(lp: ModuleType, argv: list[str], root: Path, tmp_path: Path, **env: str) -> tuple[int, list[str]]:
    # A lease root of our own, so the box's real leases never reach a verdict.
    return lp.check(argv, {"NX_BUILD_LEASE_ROOT": str(tmp_path / "leases"), **env}, root)


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


# --- (a) refusal -----------------------------------------------------------------------------


def test_missing_jar_with_a_pytest_engine_argv_exits_75_with_one_remedy_line(lp, tmp_path):
    code, lines = _run(lp, PYTEST_ARGV, _repo(tmp_path, jar=False), tmp_path)
    assert code == 75
    assert len(lines) == 1
    assert "scripts/build-gate-jar.sh --lab-run <class>" in lines[0] and "own step" in lines[0]
    assert "NX_TEST_T2_SUBSTRATE=none" in lines[0]
    assert "service/target/nexus-service-1.0-SNAPSHOT.jar" in lines[0]


@pytest.mark.parametrize(
    "argv",
    [
        ["uv", "run", "pytest"],
        ["pytest", "tests/"],
        ["python3", "-m", "pytest", "-x"],
        ["python3", "scripts/watched_pytest.py", "--stall", "180", "--status", "s.txt", "--", "uv", "run", "pytest", "tests/a.py"],
        ["uv run pytest -n auto tests/a.py"],  # one argument holding the whole command line
        ["NX_TEST_T2_SUBSTRATE=optin", "uv", "run", "pytest"],
        ["uv", "run", "pytest", "-m", "lint"],  # the engine preflight reads the env var only (measured 2026-11-22)
        ["uv", "run", "pytest", "--collect-only", "-q"],  # ditto
        ["uv", "run", "pytest", "-m", "integration"],
    ],
    ids=lambda argv: " ".join(argv)[:60],
)
def test_every_pytest_shape_that_reaches_the_session_start_is_refused(lp, tmp_path, argv):
    code, _ = _run(lp, argv, _repo(tmp_path, jar=False), tmp_path)
    assert code == 75


def test_the_environment_variable_unset_or_optin_boots_an_engine(lp, tmp_path):
    root = _repo(tmp_path, jar=False)
    assert _run(lp, PYTEST_ARGV, root, tmp_path)[0] == 75
    assert _run(lp, PYTEST_ARGV, root, tmp_path, NX_TEST_T2_SUBSTRATE="optin")[0] == 75


# --- (b) exit 0 when no engine or the jar is present --------------------------------------------


def test_substrate_none_in_the_environment_needs_no_engine(lp, tmp_path):
    assert _run(lp, PYTEST_ARGV, _repo(tmp_path, jar=False), tmp_path, NX_TEST_T2_SUBSTRATE="none") == (0, [])


def test_substrate_none_in_the_command_wins_over_the_environment(lp, tmp_path):
    root = _repo(tmp_path, jar=False)
    argv = ["NX_TEST_T2_SUBSTRATE=none", *PYTEST_ARGV]
    assert _run(lp, argv, root, tmp_path, NX_TEST_T2_SUBSTRATE="optin") == (0, [])
    # and the other way round: the command's value is the one the run sees
    assert _run(lp, ["NX_TEST_T2_SUBSTRATE=optin", *PYTEST_ARGV], root, tmp_path, NX_TEST_T2_SUBSTRATE="none")[0] == 75


@pytest.mark.parametrize("value", ["sqlite", "per-test", "engine"])
def test_a_retired_substrate_value_is_refused_by_name_and_boots_nothing(lp, tmp_path, value):
    assert _run(lp, PYTEST_ARGV, _repo(tmp_path, jar=False), tmp_path, NX_TEST_T2_SUBSTRATE=value) == (0, [])


@pytest.mark.parametrize(
    "argv",
    [
        ["ls", "-l"],
        ["scripts/build-gate-jar.sh"],
        ["uv", "run", "ruff", "check", "scripts/lab-preflight"],
        ["uv", "run", "python", "scripts/file_size_ratchet.py", "check"],
        ["bash", "-c", "echo tests/test_pytest_helpers.py"],  # a file NAMED like pytest is not pytest
        [],
        ["uv", "run", "pytest", "--help"],
        ["uv", "run", "pytest", "--version"],
    ],
    ids=lambda argv: " ".join(argv)[:60] or "empty",
)
def test_a_command_that_starts_no_pytest_session_needs_no_engine(lp, tmp_path, argv):
    assert _run(lp, argv, _repo(tmp_path, jar=False), tmp_path) == (0, [])


@pytest.mark.parametrize(
    "argv",
    [
        # a compound command may build the jar first: fail open
        ["scripts/build-gate-jar.sh", "&&", "uv", "run", "pytest", "-n", "auto"],
        ["scripts/build-gate-jar.sh --lab-run lab-l && uv run pytest -n auto"],
        ["bash", "-c", "scripts/build-gate-jar.sh && uv run pytest tests/a.py"],
        ["sh", "-c", "scripts/build-gate-jar.sh; uv run pytest"],
        ["./mvnw", "-f", "service/pom.xml", "package", ";", "uv", "run", "pytest"],
        ["scripts/mvnw-leased.sh", "--lab-run", "lab-s", "package", "&&", "uv", "run", "pytest"],
        ["export", "NX_FOO=1;", "uv", "run", "pytest"],
        ["export NX_TEST_T2_SUBSTRATE=optin; uv run pytest"],
        ["true", "||", "uv", "run", "pytest"],
        ["uv", "run", "pytest", "|", "tee", "out.txt"],
        ["uv", "run", "pytest", "&"],
        ["uv run pytest\nuv run pytest"],
        # the word pytest is an argument, not the command
        ["uv", "pip", "install", "pytest"],
        ["echo", "pytest"],
        ["grep", "pytest", "pyproject.toml"],
        ["pip", "install", "pytest-xdist"],
        ["cat", "tests/test_pytest.py"],
        # prints and exits before any session starts
        ["uv", "run", "pytest", "--markers"],
        ["python3", "-m", "pytest", "-h"],
        # a wrapper with nothing after its `--`
        ["python3", "scripts/watched_pytest.py", "--stall", "180"],
        # the substrate named after the wrapper's `--`
        ["python3", "scripts/watched_pytest.py", "--stall", "180", "--", "env", "NX_TEST_T2_SUBSTRATE=none", "uv", "run", "pytest"],
        ["python3", "scripts/watched_pytest.py", "--", "NX_TEST_T2_SUBSTRATE=none", "uv", "run", "pytest"],
        ["env", "-i", "NX_TEST_T2_SUBSTRATE=none", "uv", "run", "pytest"],
    ],
    ids=lambda argv: " ".join(argv)[:70].replace("\n", "\\n"),
)
def test_a_command_that_is_compound_or_only_mentions_pytest_is_not_judged(lp, tmp_path, argv):
    assert _run(lp, argv, _repo(tmp_path, jar=False), tmp_path) == (0, [])


@pytest.mark.parametrize(
    "argv",
    [
        ["env", "NX_TEST_T2_SUBSTRATE=optin", "uv", "run", "pytest"],
        ["export", "NX_TEST_T2_SUBSTRATE=optin", "uv", "run", "pytest"],
        ["python3", "scripts/watched_pytest.py", "--stall", "180", "--", "env", "NX_TEST_T2_SUBSTRATE=optin", "uv", "run", "pytest"],
        ["uv", "run", "--no-sync", "pytest", "tests/a.py"],
        ["/usr/bin/env", "-i", "uv", "run", "python3.12", "-m", "pytest"],
        ["uv", "run", "py.test"],
    ],
    ids=lambda argv: " ".join(argv)[:70],
)
def test_the_command_word_is_found_through_its_wrappers(lp, tmp_path, argv):
    assert _run(lp, argv, _repo(tmp_path, jar=False), tmp_path)[0] == 75


def test_the_jar_present_exits_0_without_output(lp, tmp_path):
    assert _run(lp, PYTEST_ARGV, _repo(tmp_path, jar=True), tmp_path) == (0, [])


def test_a_dangling_jar_link_counts_as_missing_like_the_conftest_check(lp, tmp_path):
    root = _repo(tmp_path, jar=False)
    (root / JAR_REL).parent.mkdir(parents=True)
    (root / JAR_REL).symlink_to(tmp_path / "gone.jar")
    assert _run(lp, PYTEST_ARGV, root, tmp_path)[0] == 75


def test_an_unreadable_service_target_counts_as_a_present_jar(lp, tmp_path):
    root = _repo(tmp_path, jar=True)
    target = root / "service" / "target"
    target.chmod(0)
    try:
        try:
            (target / "nexus-service-1.0-SNAPSHOT.jar").exists()
            pytest.skip("this user can stat through a mode-0 directory (root): no EACCES to fail open on")
        except OSError:
            pass
        assert lp.jar_present(root) is True
        assert _run(lp, PYTEST_ARGV, root, tmp_path) == (0, [])
    finally:
        target.chmod(0o755)


def test_a_huge_pgid_in_the_build_lease_does_not_crash_the_check(lp, tmp_path):
    root = _repo(tmp_path, jar=True)
    lease = tmp_path / "leases" / "service"
    lease.mkdir(parents=True)
    (lease / "pgid").write_text("9" * 40)  # os.killpg raises OverflowError, not OSError
    assert lp.build_lease_held(tmp_path / "leases", root, {}) is None
    assert _run(lp, PYTEST_ARGV, root, tmp_path) == (0, [])


def test_a_huge_pid_in_the_suite_lease_does_not_crash_the_check(lp, tmp_path):
    root = _repo(tmp_path, jar=True)
    _hold_suite_lease(tmp_path, "9" * 40)
    assert _run(lp, PYTEST_ARGV, root, tmp_path) == (0, [])


def test_main_fails_open_on_its_own_error(lp, capsys):
    # a repo root of the wrong type makes check() raise AttributeError; main must say so and exit 0
    assert lp.main(PYTEST_ARGV, {}, 123) == 0  # type: ignore[arg-type]
    assert "skipped on its own error" in capsys.readouterr().err


# --- (c) leases only warn ---------------------------------------------------------------------


def _hold_suite_lease(tmp_path: Path, pid: int | str) -> None:
    lease = tmp_path / "leases" / "suite"
    lease.mkdir(parents=True)
    (lease / "pid").write_text(str(pid))
    (lease / "label").write_text("nexus-wt: pytest tests/")


def _build_lease(tmp_path: Path, *, worktree: Path | None, pid: int = 0) -> None:
    lease = tmp_path / "leases" / "service"
    lease.mkdir(parents=True)
    (lease / "pid").write_text(str(pid or os.getpid()))
    (lease / "label").write_text("mvnw-leased")
    if worktree is not None:
        (lease / "worktree").write_text(str(worktree))


def test_a_held_suite_lease_warns_and_exits_0(lp, tmp_path):
    root = _repo(tmp_path, jar=True)
    _hold_suite_lease(tmp_path, os.getpid())
    code, lines = _run(lp, PYTEST_ARGV, root, tmp_path)
    assert code == 0
    assert len(lines) == 1 and "suite lease is held" in lines[0] and f"pid {os.getpid()}" in lines[0]


def test_a_held_build_lease_for_this_checkout_warns_and_exits_0(lp, tmp_path):
    root = _repo(tmp_path, jar=True)
    _build_lease(tmp_path, worktree=root)
    code, lines = _run(lp, PYTEST_ARGV, root, tmp_path)
    assert code == 0
    assert len(lines) == 1 and "build lease" in lines[0]


def test_both_leases_held_give_two_warnings_and_exit_0(lp, tmp_path):
    root = _repo(tmp_path, jar=True)
    _hold_suite_lease(tmp_path, os.getpid())
    _build_lease(tmp_path, worktree=root)
    code, lines = _run(lp, PYTEST_ARGV, root, tmp_path)
    assert code == 0 and len(lines) == 2


def test_a_build_in_another_checkout_and_a_dead_holder_are_silent(lp, tmp_path):
    root = _repo(tmp_path, jar=True)
    other = tmp_path / "other"
    other.mkdir()
    _build_lease(tmp_path, worktree=other)
    _hold_suite_lease(tmp_path, _dead_pid())
    assert _run(lp, PYTEST_ARGV, root, tmp_path) == (0, [])


def test_leases_are_not_read_for_a_run_that_needs_no_engine(lp, tmp_path):
    root = _repo(tmp_path, jar=True)
    _hold_suite_lease(tmp_path, os.getpid())
    assert _run(lp, ["ls"], root, tmp_path) == (0, [])
    assert _run(lp, PYTEST_ARGV, root, tmp_path, NX_TEST_T2_SUBSTRATE="none") == (0, [])


def test_a_held_lease_does_not_hide_a_missing_jar(lp, tmp_path):
    root = _repo(tmp_path, jar=False)
    _hold_suite_lease(tmp_path, os.getpid())
    assert _run(lp, PYTEST_ARGV, root, tmp_path)[0] == 75


# --- (a)/(e) drift: the script's copies against the code they were read from --------------------


def test_drift_the_jar_path_is_the_conftest_fixtures_jar(lp):
    assert Path(*lp.SERVICE_JAR) == _service_fixture._SERVICE_JAR.relative_to(_service_fixture._REPO_ROOT)
    assert lp.jar_path(REPO) == _service_fixture._SERVICE_JAR


def test_drift_the_jar_the_script_checks_is_the_one_the_conftests_preflight_asks_about(lp, monkeypatch):
    """The conftest's session start refuses through ``_substrate_preflight.refusal`` -> ``_service_fixture.jar_freshness_skip_reason``.

    Replace that function with a sentinel: the preflight must carry the sentinel, i.e. it reaches the very function whose
    ``not jar.exists()`` branch this script copies. A rename or a second jar check in front of it breaks this.
    """
    monkeypatch.setattr(_service_fixture, "jar_freshness_skip_reason", lambda *a, **k: "SENTINEL-JAR-REASON")
    assert "SENTINEL-JAR-REASON" in (_substrate_preflight.refusal() or "")
    assert (_substrate_preflight.refusal() or "").startswith(_substrate_preflight.PREFIX)
    assert "_preflight_engine_substrate" in Path(root_conftest.__file__).read_text()  # the gate that calls it


def test_drift_a_missing_jar_is_what_the_fixtures_freshness_check_calls_not_built(lp, tmp_path, monkeypatch):
    monkeypatch.setenv("NX_BUILD_LEASE_ROOT", str(tmp_path / "leases"))  # no live build can answer first
    absent = tmp_path / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
    assert "service jar not built" in (_service_fixture.jar_freshness_skip_reason(absent) or "")
    assert lp.check(PYTEST_ARGV, {}, tmp_path)[0] == 75  # the same absence, as the script reads it


def test_drift_the_retired_substrates_and_the_boot_predicate_match_the_conftest(lp, monkeypatch):
    assert lp.RETIRED_SUBSTRATES == frozenset(root_conftest._RETIRED_T2_SUBSTRATES)
    assert lp.SUBSTRATE_ENV == "NX_TEST_T2_SUBSTRATE"
    values = [None, "none", "optin", "", "anything-else", *sorted(root_conftest._RETIRED_T2_SUBSTRATES)]
    assert len(values) >= 8
    for value in values:
        if value is None:
            monkeypatch.delenv(lp.SUBSTRATE_ENV, raising=False)
        else:
            monkeypatch.setenv(lp.SUBSTRATE_ENV, value)
        assert lp.substrate_boots_engine(value) is root_conftest._selected_t2_substrate_boots_engine(), value


def test_drift_the_lease_names_and_root_match_the_lease_modules(lp, monkeypatch):
    assert lp.SUITE_LEASE == suite_lease_module.RESOURCE
    assert lp.BUILD_LEASE_ROOT_ENV == "NX_BUILD_LEASE_ROOT"
    monkeypatch.delenv("NX_BUILD_LEASE_ROOT", raising=False)
    assert lp.lease_root(REPO, {}) == _service_fixture._build_lease_root()
    monkeypatch.setenv("NX_BUILD_LEASE_ROOT", "/some/where")
    assert lp.lease_root(REPO, {"NX_BUILD_LEASE_ROOT": "/some/where"}) == _service_fixture._build_lease_root()


def test_drift_the_wrapper_the_script_recognises_still_exists():
    assert (REPO / "scripts" / "watched_pytest.py").is_file()


def test_drift_the_suite_lease_verdict_matches_the_lease_module_on_real_lease_files(lp, tmp_path):
    cases: dict[str, int | str | None] = {
        "no lease": None,
        "live pid": os.getpid(),
        "dead pid": _dead_pid(),
        "garbage pid": "not-a-pid",
        "empty pid": "",
    }
    seen = set()
    for name, pid in cases.items():
        root = tmp_path / name.replace(" ", "-")
        if pid is not None:
            lease = root / "suite"
            lease.mkdir(parents=True)
            (lease / "pid").write_text(str(pid))
        mine = lp.suite_lease_held(root)
        theirs = suite_lease_module.holder(root)
        assert (mine is None) == (theirs is None), name
        seen.add(mine is None)
    assert seen == {True, False}, "the cases must cover both a held and a free verdict"


def test_drift_the_build_lease_verdict_matches_the_fixture_on_real_lease_files(lp, tmp_path):
    checkout = tmp_path / "checkout"
    other = tmp_path / "other"
    checkout.mkdir()
    other.mkdir()
    dead = str(_dead_pid())
    me, my_group = str(os.getpid()), str(os.getpgid(0))
    # (pid, pgid, worktree, age_s): pgid wins over pid; a missing pid and pgid reads the populate grace
    cases = {
        "live pid, this checkout": (me, "", str(checkout), 0),
        "live pid, no worktree file": (me, "", "", 0),
        "live pid, other checkout": (me, "", str(other), 0),
        "dead pid": (dead, "", str(checkout), 0),
        "live pgid beats dead pid": (dead, my_group, str(checkout), 0),
        "dead pgid beats live pid": (me, "999999999", str(checkout), 0),
        "garbage pid": ("-1", "", str(checkout), 0),
        "empty and fresh": ("", "", "", 0),
        "empty and old": ("", "", "", 3600),
        "unreadable worktree": (me, "", str(tmp_path / "missing"), 0),
    }
    verdicts = set()
    for name, (pid, pgid, worktree, age) in cases.items():
        root = tmp_path / name.replace(" ", "-").replace(",", "")
        lease = root / "service"
        lease.mkdir(parents=True)
        (lease / "pid").write_text(pid)
        (lease / "pgid").write_text(pgid)
        if worktree:
            (lease / "worktree").write_text(worktree)
        if age:
            old = time.time() - age
            os.utime(lease, (old, old))
        mine = lp.build_lease_held(root, checkout, {})
        theirs = _service_fixture.build_in_progress_reason(root, repo_root=checkout)
        assert (mine is None) == (theirs is None), name
        verdicts.add(mine is None)
    assert verdicts == {True, False}, "the cases must cover both a held and a free verdict"
    root = tmp_path / "absent-lease"
    assert lp.build_lease_held(root, checkout, {}) is None
    assert _service_fixture.build_in_progress_reason(root, repo_root=checkout) is None


# --- (d) the real command line: exit code, `--`, and the cap -----------------------------------

#: lab-remote kills the script at 10 s; the timeout below sits well under it, so a script that grew slow fails here
#: instead of silently becoming a no-op in production. No wall-clock assertion: a loaded box must not make this flaky.
CAP_S = 5


def _installed_copy(tmp_path: Path, *, jar: bool) -> Path:
    root = _repo(tmp_path, jar=jar)
    (root / "scripts").mkdir()
    shutil.copy2(SCRIPT, root / "scripts" / "lab-preflight")
    return root


def _cli(root: Path, tmp_path: Path, *argv: str, **env: str) -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if k != "NX_TEST_T2_SUBSTRATE"}
    clean.update({"NX_BUILD_LEASE_ROOT": str(tmp_path / "leases"), **env})
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "lab-preflight"), *argv],
        capture_output=True, text=True, env=clean, cwd=root, timeout=CAP_S,
    )


def test_cli_exits_75_with_one_stderr_line(tmp_path):
    root = _installed_copy(tmp_path, jar=False)
    done = _cli(root, tmp_path, "--", "uv", "run", "pytest", "-n", "auto")
    assert done.returncode == 75
    assert done.stdout == ""
    assert len(done.stderr.splitlines()) == 1 and "scripts/build-gate-jar.sh --lab-run" in done.stderr


def test_cli_exits_0_for_no_engine_and_for_a_present_jar_and_reads_the_environment(tmp_path):
    missing = _installed_copy(tmp_path / "a", jar=False)
    present = _installed_copy(tmp_path / "b", jar=True)
    for root, argv, env in [
        (missing, ("--", "uv", "run", "pytest"), {"NX_TEST_T2_SUBSTRATE": "none"}),
        (missing, ("--", "ls"), {}),
        (missing, ("--", "scripts/build-gate-jar.sh", "&&", "uv", "run", "pytest"), {}),
        (present, ("--", "uv", "run", "pytest"), {}),
    ]:
        done = _cli(root, tmp_path, *argv, **env)
        assert (done.returncode, done.stderr) == (0, ""), argv
    assert _cli(missing, tmp_path, "--", "uv", "run", "pytest").returncode == 75


# A plugin for a child pytest: aim the fixture's jar at a path that does not exist, the state of every lab-remote copy
# (patching the fixture's own default, as tests/test_substrate_preflight.py does), so the child's real session start runs.
_JARLESS_SEAM = """
import functools, os
from pathlib import Path
import tests.db._service_fixture as sf
sf.jar_freshness_skip_reason = functools.partial(sf.jar_freshness_skip_reason, Path(os.environ["SEAM_JAR"]))
"""


def _child_pytest(tmp_path: Path, args: list[str], **extra: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "_jarless_seam.py").write_text(_JARLESS_SEAM)
    drop = {
        "NX_TEST_T2_SUBSTRATE", "NX_BUILD_LEASE_WAIT", "NX_SUITE_LEASE_HELD_BY", "NX_SUITE_LEASE_WAIT",
        "NX_SUITE_LEASE_UNGUARDED", "NEXUS_PG_BIN", "NX_TEST_SUBSTRATE_CACHE",
        "LAB_CPUS", "LAB_MEM_MB", "LAB_REMOTE", "NX_LOCAL_HEAVY",  # a grant makes the child's suite lease step aside
    }
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(
        NX_BUILD_LEASE_ROOT=str(tmp_path / "leases"),
        XDG_CACHE_HOME=str(tmp_path / "cache"),
        PYTHONPATH=str(tmp_path),
        PYTEST_ADDOPTS="",
        SEAM_JAR=str(tmp_path / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"),
        **extra,
    )
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "_jarless_seam", "-p", "no:cacheprovider", "-q",
         *args, "tests/test_arg_grouping.py"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=240,
    )


@pytest.mark.parametrize("flags", [["--collect-only"], ["-m", "lint"], []], ids=["collect-only", "m-lint", "plain"])
def test_end_to_end_the_conftest_refuses_what_the_script_refuses_collect_only_and_m_lint_included(lp, tmp_path, flags):
    """Pins the rule the script applies to ``--collect-only`` and ``-m lint``: the conftest exits 75 on them too."""
    child = _child_pytest(tmp_path, flags)
    out = child.stdout + child.stderr
    assert child.returncode == 75, out
    assert "service jar not built" in out
    assert lp.check(["uv", "run", "pytest", *flags], {}, tmp_path)[0] == 75  # the same absence, the same verdict


def test_end_to_end_a_no_engine_substrate_goes_ahead_where_the_script_exits_0(lp, tmp_path):
    child = _child_pytest(tmp_path, ["--collect-only"], NX_TEST_T2_SUBSTRATE="none")
    assert child.returncode == 0, child.stdout + child.stderr
    assert lp.check(["uv", "run", "pytest", "--collect-only"], {"NX_TEST_T2_SUBSTRATE": "none"}, tmp_path) == (0, [])
