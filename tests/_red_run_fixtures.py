# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared fixtures and builders for the red-run report tests (nexus-2jomg.1).

A throwaway repo (``history``), a synthetic run/job snapshot (``snapshot``), and the extraction over
both (``extraction``). ``test_red_run_report.py`` and ``test_red_run_report_mvv.py`` import them by name;
this module has no tests of its own.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import red_run_report as rrr

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, **_GIT_ENV}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True, env=env).stdout.strip()


def _commit(repo: Path, subject: str, files: dict[str, str]) -> str:
    """Write each file with the given text (appended when it exists), commit, return the sha."""
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((path.read_text() if path.exists() else "") + text)
    _git(repo, "add", *files)
    _git(repo, "commit", "-q", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def history(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "develop")
    sha = {"c0": _commit(repo, "chore: base",
                         {"README.md": "x\n", "tests/test_k.py": "ENTRIES = [\n    'a',\n]\n"})}
    _git(repo, "switch", "-q", "-c", "side")
    sha["side"] = _commit(repo, "chore: a commit on a branch develop never merges", {"side.txt": "s\n"})
    _git(repo, "switch", "-q", "develop")
    sha["c1"] = _commit(repo, "feat: a", {"src/nexus/a.py": "A = 1\n"})
    sha["c2"] = _commit(repo, "fix(a): repair", {"src/nexus/a.py": "A = 2\n",
                                                 "tests/test_a.py": "def test_x():\n    assert 1\n"})
    sha["c3"] = _commit(repo, "test: touch old", {"tests/test_old.py": "def test_o():\n    assert 1\n"})
    sha["c4"] = _commit(repo, "chore: ledger", {"tests/ddl_ceiling.toml": "x = 1\n"})
    sha["c5"] = _commit(repo, "fix(k): add entry", {"tests/test_k.py": "    'b',\n"})
    sha["c6"] = _commit(repo, "fix(svc): foo", {"service/src/main/java/dev/Foo.java": "class Foo {}\n",
                                                "service/src/test/java/dev/FooTest.java": "class FooTest {}\n"})
    _git(repo, "update-ref", "refs/remotes/origin/develop", sha["c6"])
    return repo, sha


def _run(rid: int, name: str, sha: str, conclusion: str, minute: int, attempt: int = 1,
         event: str = "push") -> dict:
    ts = f"2026-10-01T10:{minute:02d}:00Z"
    return {"id": rid, "name": name, "head_sha": sha, "conclusion": conclusion, "created_at": ts,
            "updated_at": f"2026-10-01T10:{minute:02d}:30Z", "run_attempt": attempt, "event": event}


def _job(jid: int, name: str, steps: list[tuple[str, str]], labels: list[str] | None = None,
         conclusion: str = "failure", attempt: int = 1) -> dict:
    return {"id": jid, "name": name, "conclusion": conclusion, "labels": labels or ["ubuntu-latest"],
            "steps": [{"name": n, "conclusion": c} for n, c in steps], "run_attempt": attempt}


def _shard(jid: int, n: int = 1, step: str | None = None) -> dict:
    return _job(jid, f"pytest (Python 3.12, shard {n}/6)", [(step or f"Run tests (shard {n}/6)", "failure")])


_PYTEST_FAIL = (
    "2026-10-01T10:00:00.1Z =========================== short test summary info ====\n"
    "2026-10-01T10:00:00.2Z FAILED {node} - AssertionError: boom\n"
    "2026-10-01T10:00:00.3Z ##[error]Process completed with exit code 1.\n")


def _fail(node: str, msg: str) -> str:
    return (f"2026-10-01T10:00:00.2Z FAILED {node} - {msg}\n"
            "2026-10-01T10:00:00.3Z ##[error]Process completed with exit code 1.\n")


_WIN = ["win-release"]
_JAVA_JOB = "Java tests + jOOQ codegen drift guard"


@pytest.fixture
def snapshot(tmp_path: Path, history: tuple[Path, dict[str, str]]) -> Path:
    _, c = history
    snap = tmp_path / "snap"
    (snap / "jobs").mkdir(parents=True)
    wr = "windows-pg-bundle-rehearsal"
    runs = [
        # r1: behavioural test red at c1, fixed by a fix() commit touching src and the test
        _run(1, "CI", c["c1"], "failure", 1), _run(2, "CI", c["c2"], "success", 2),
        # r3: lint-marked docs_reference_rot red at c3, rerun green on the same sha
        _run(3, "CI", c["c3"], "failure", 3), _run(4, "CI", c["c3"], "success", 4),
        # r5: storage-touch ratchet step red at c4, rerun green
        _run(5, "CI", c["c4"], "failure", 5), _run(6, "CI", c["c4"], "success", 6),
        # r7: shard job with a hosted-runner network failure, rerun green
        _run(7, "CI", c["c4"], "failure", 7), _run(8, "CI", c["c4"], "success", 8),
        # r9: Java test red; its next green (r23) is on c6
        _run(9, "Service CI", c["c3"], "failure", 9),
        # r10: ruff step; r11: cancelled (not red); r12: failure with no job records
        _run(10, "CI", c["c4"], "failure", 10), _run(11, "CI", c["c4"], "cancelled", 11),
        _run(12, "ci.yml", c["c4"], "failure", 12),
        # r13: Windows rehearsal, test red with no log
        _run(13, wr, c["c3"], "failure", 13),
        # r14: test red at c2 whose next green (c3) never touched the test: no tied fix
        _run(14, "CI", c["c2"], "failure", 14), _run(15, "CI", c["c3"], "success", 15),
        # r16: Windows run with a failed-count summary but no names; r17: a job that died silently
        _run(16, wr, c["c4"], "failure", 16), _run(17, "CI", c["c4"], "failure", 17),
        # r18: a census table test, fixed by an entry added to the table (c5)
        _run(18, "CI", c["c4"], "failure", 18), _run(19, "CI", c["c5"], "success", 19),
        # r20: a test whose file the first commit after it edits (c3 is the first of four)
        _run(20, "CI", c["c2"], "failure", 20), _run(21, "CI", c["c6"], "success", 21),
        # r22: a Java test fixed by a fix commit touching the test and product code
        _run(22, "Service CI", c["c5"], "failure", 22), _run(23, "Service CI", c["c6"], "success", 23),
        # r24: never green again
        _run(24, "Service CI", c["c6"], "failure", 24),
        # r25: Windows named by section headers, message names a budget
        _run(25, wr, c["c6"], "failure", 25),
        # r26 r27: red at c6, green again on c6 (rerun-green)
        _run(26, "CI", c["c6"], "failure", 26), _run(27, "CI", c["c6"], "failure", 27),
        _run(28, "CI", c["c6"], "success", 28),
        # r29..r33, r36: one rule each, never green after
        _run(29, "CI", c["c6"], "failure", 29), _run(30, "CI", c["c6"], "failure", 30),
        _run(31, "CI", c["c6"], "failure", 31), _run(32, "CI", c["c6"], "failure", 32),
        _run(33, "CI", c["c6"], "failure", 33), _run(36, "CI", c["c6"], "failure", 36),
        # a green on a commit that does not descend from the reds above must not close their episode
        _run(37, "CI", c["side"], "success", 37),
    ]
    (snap / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in runs))
    jobs = {
        1: [_job(101, "pytest (Python 3.12, shard 1/6)",
                 [("Set up job", "success"), ("Run tests (shard 1/6)", "failure")]),
            _job(102, "pytest-gate", [("Verify the pytest suite or its legitimate skip", "failure")])],
        3: [_job(301, "pytest (lint markers, group 1/2)", [("Run lint-marked tests", "failure")])],
        5: [_job(501, "pytest-gate",
                 [("Tenant-saving ratchet over the whole run (storage-touch fan-in)", "failure")])],
        7: [_shard(701, 2)],
        9: [_job(901, _JAVA_JOB, [("Run Java test suite", "failure")], labels=["lab-6c18g"])],
        10: [_job(1001, "ruff lint", [("Lint src/ with ruff", "failure")])],
        11: [_job(1101, "pytest (Python 3.12, shard 1/6)", [("Run tests (shard 1/6)", "cancelled")],
                  conclusion="cancelled")],
        12: [],
        13: [_job(1301, "Windows test set (win-release)", [("Windows test set", "failure")], labels=_WIN)],
        14: [_shard(1401)],
        16: [_job(1601, "Windows test set (win-release)", [("Windows test set", "failure")], labels=_WIN)],
        17: [_shard(1701, 3)],
        18: [_shard(1801)], 20: [_shard(2001, 2)],
        22: [_job(2201, _JAVA_JOB, [("Run Java test suite", "failure")])],
        24: [_job(2401, _JAVA_JOB, [("Run Java test suite", "failure")])],
        25: [_job(2501, "Windows test set (win-release)", [("Windows test set", "failure")], labels=_WIN)],
        26: [_shard(2601)], 27: [_shard(2701)], 29: [_shard(2901)], 30: [_shard(3001)], 31: [_shard(3101)],
        32: [_shard(3201, 1, "Install ripgrep")], 33: [_shard(3301, 4)], 36: [_shard(3601, 5)],
    }
    for rid, js in jobs.items():
        (snap / "jobs" / f"{rid}.json").write_text(json.dumps(js))
    # the later successful runs: every job any red run of their workflow failed in, succeeded
    wf_of = {r["id"]: r["name"] for r in runs}
    greens = snap / "green_jobs"
    greens.mkdir()
    for r in runs:
        if r["conclusion"] == "success":
            names = sorted({j["name"] for rid, js in jobs.items() if wf_of[rid] == r["name"] for j in js})
            ok = [{"id": r["id"] * 100 + n, "name": name, "conclusion": "success", "run_attempt": 1}
                  for n, name in enumerate(names)]
            (greens / f"{r['id']}.json").write_text(json.dumps(ok))
    java = ("2026-10-01T10:00:00Z [ERROR] Failures: \n"
            "2026-10-01T10:00:00Z [ERROR]   {cls}.{method}:12 expected 1\n"
            "2026-10-01T10:00:00Z [ERROR] \n"
            "2026-10-01T10:00:00Z [ERROR] Tests run: 5, Failures: 1\n")
    logs = {
        (1, 101): _PYTEST_FAIL.format(node="tests/test_a.py::test_x[a b]"),
        (3, 301): _PYTEST_FAIL.format(node="tests/test_docs_reference_rot.py::test_cited"),
        (7, 701): "2026-10-01T10:00:00Z ##[error]The Unable to download CodeQL: Unexpected HTTP response: 504\n",
        (9, 901): java.format(cls="FooIntegrationTest", method="bar"),
        (14, 1401): _PYTEST_FAIL.format(node="tests/test_b.py::test_flaky"),
        (16, 1601): "2026-10-01T10:00:00Z ==== 40 failed, 1139 passed, 45 skipped in 94.24s ====\n",
        (17, 1701): "2026-10-01T10:00:00Z Cleaning up orphan processes\n",
        (18, 1801): _fail("tests/test_k.py::test_entries", "AssertionError: not in entries"),
        (20, 2001): _fail("tests/test_old.py::test_o", "AssertionError: stale"),
        (22, 2201): java.format(cls="dev.FooTest", method="bar"),
        (24, 2401): java.format(cls="BarTest$Inner", method="m"),
        (25, 2501): ("2026-10-01T10:00:00Z ____ TestPre.test_sessionstart_under_budget ____\n"
                     "2026-10-01T10:00:00Z E       AssertionError: bounded components 7042B >= budget 7000B\n"
                     "2026-10-01T10:00:00Z ==== 1 failed, 5 passed in 1.0s ====\n"),
        (26, 2601): _fail("tests/test_t.py::test_t", "AssertionError: predicate not satisfied within 5.0s"),
        (27, 2701): _fail("tests/test_r.py::test_r", "AssertionError: boom"),
        (29, 2901): _fail("tests/scripts/test_tool.py::test_x", "AssertionError: boom"),
        (30, 3001): _fail("tests/test_x_census.py::test_a", "AssertionError: z"),
        (31, 3101): _fail("tests/test_m.py::test_m", "AssertionError: HTTP call sites that neither send the header"),
        (33, 3301): "2026-10-01T10:00:00Z ==== 3 failed, 5 passed, 2 errors in 4.1s ====\n",
        (36, 3601): ("2026-10-01T10:00:00Z UserWarning: PG-bundle self-provisioning failed (failed to download x)\n"
                     "2026-10-01T10:00:00Z ##[error]Process completed with exit code 75.\n"),
    }
    for (rid, jid), text in logs.items():
        d = snap / "logs" / str(rid)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{jid}.log").write_text(text)
    # an empty .log and a .err stub must both count as "no log"
    (snap / "logs" / "10").mkdir(parents=True)
    (snap / "logs" / "10" / "1001.log").write_text("")
    (snap / "logs" / "13").mkdir(parents=True)
    (snap / "logs" / "13" / "1301.log.err").write_text("the response contains terminal escape sequences")
    return snap


def _extract(snapshot: Path, repo: Path, **kw: object) -> list[dict]:
    kw.setdefault("since", "2026-10-01")
    kw.setdefault("until", "2026-10-01")
    kw.setdefault("green_jobs", snapshot / "green_jobs")
    return rrr.extract(snapshot, repo, **kw)


def _set_green(snapshot: Path, run_id: int, conclusions: dict[str, str]) -> None:
    """Rewrite one later run's jobs: the named jobs get the given conclusion."""
    path = snapshot / "green_jobs" / f"{run_id}.json"
    jobs = json.loads(path.read_text())
    for j in jobs:
        j["conclusion"] = conclusions.get(j["name"], j["conclusion"])
    path.write_text(json.dumps(jobs))


@pytest.fixture
def extraction(snapshot: Path, history: tuple[Path, dict[str, str]]) -> list[dict]:
    return _extract(snapshot, history[0])


@pytest.fixture
def inventory_repo(tmp_path: Path) -> Path:
    """A repo with every test the ``snapshot`` fixture's flake and unattributed rows name."""
    root = tmp_path / "inventory"
    root.mkdir()
    _git(root, "init", "-q", "-b", "develop")
    _commit(root, "feat: the named tests", {
        "tests/test_t.py": "def test_t():\n    pass\n", "tests/test_r.py": "def test_r():\n    pass\n",
        "tests/test_b.py": "def test_flaky():\n    pass\n",
        "service/src/test/java/dev/BarTest.java": "class BarTest { void m() {} }\n",
        "service/src/test/java/dev/FooIntegrationTest.java": "class FooIntegrationTest { void bar() {} }\n"})
    return root
