# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/red_run_report.py over a small synthetic snapshot and a throwaway git repo (nexus-2jomg.1).

No network, no tenant fixture: the snapshot is files in ``tmp_path`` and the history is a repo
``git init``-ed there. The MVV tolerance math is tested on counts, not on the real snapshot.

Run it with ``NX_TEST_T2_SUBSTRATE=none uv run pytest tests/test_red_run_report.py`` after ``uv sync``:
the conftest refuses to start the engine substrate in a worktree whose service jar is not built, and
this file needs no engine.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib
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


# --- log parsing ----------------------------------------------------------------------------


def test_parse_log_reads_pytest_java_and_infra() -> None:
    lines = [
        "﻿2026-10-01T10:00:00.1Z FAILED tests/test_x.py::test_a[p q] - ValueError: no\n",
        "2026-10-01T10:00:00.2Z ERROR tests/test_y.py::test_b - boom\n",
        "2026-10-01T10:00:00.3Z FAILED tests/test_x.py::test_a[p q] - ValueError: no\n",
        "2026-10-01T10:00:01Z [ERROR] Failures: \n",
        "2026-10-01T10:00:01Z [ERROR]   dev.nexus.FooTest.bar:664 \n",
        "2026-10-01T10:00:01Z [ERROR] \n",
        "2026-10-01T10:00:01Z [ERROR]   NotATest.after:1 \n",
        "2026-10-01T10:00:02Z ##[error]Unexpected HTTP response: 504\n",
        "2026-10-01T10:00:03Z FAILED tests\\daemon\\test_w.py::test_win - OSError\n",   # Windows path
    ]
    got = rrr.parse_log(lines)
    assert [(t["kind"], t["file"], t["name"]) for t in got["tests"]] == [
        ("pytest", "tests/test_x.py", "test_a[p q]"), ("pytest", "tests/test_y.py", "test_b"),
        ("java", "FooTest", "bar"), ("pytest", "tests/daemon/test_w.py", "test_win")]
    assert got["infra"] is True and got["errors"] == ["Unexpected HTTP response: 504"]
    assert got["evidence"] is True


def test_parse_log_keeps_a_parametrize_id_with_a_dash_and_strips_the_java_outer_class() -> None:
    got = rrr.parse_log(["FAILED tests/t.py::test_a[x - y] - boom\n",
                         "[ERROR] Failures: \n", "[ERROR]   Outer$Inner.method:3 \n"])
    assert [(t["file"], t["name"], t["msg"]) for t in got["tests"]] == [
        ("tests/t.py", "test_a[x - y]", "boom"), ("Outer", "method", "")]


def test_parse_log_sums_failed_and_error_counts_and_reads_failure_sections() -> None:
    got = rrr.parse_log(["==== 3 failed, 5 passed, 2 errors in 4.1s ====\n"])
    assert got["failed_count"] == 5 and got["tests"] == []
    got = rrr.parse_log(["____ TestPre.test_a[x] ____\n", "E       AssertionError: over budget\n",
                         "==== 1 failed in 1.0s ====\n"])
    assert [(t["file"], t["name"], t["msg"]) for t in got["tests"]] == [
        ("", "TestPre.test_a[x]", "AssertionError: over budget")]
    assert got["first_e"] == "AssertionError: over budget"


def test_parse_log_marks_a_bare_exit_status_as_no_evidence() -> None:
    assert rrr.parse_log(["##[error]Process completed with exit code 1.\n"])["evidence"] is False
    assert rrr.parse_log(["##[error]the real-PID tests did not pass\n"])["evidence"] is True


def test_an_infra_signature_counts_only_on_error_annotations() -> None:
    noise = ["UserWarning: PG-bundle self-provisioning failed (failed to download x)\n",
             "WARN Voyage rate limit exceeded, HTTP 500 retrying\n"]
    assert rrr.parse_log(noise)["infra"] is False          # test-output noise is not the failure
    assert rrr.parse_log(["##[error]failed to download x\n"])["infra"] is True


@pytest.mark.parametrize(("labels", "want"), [
    (["ubuntu-latest"], "hosted"), (["ubuntu-24.04-arm"], "hosted"), (["macos-14"], "hosted"),
    (["lab-6c18g"], "self-hosted"), (["self-hosted", "Linux", "X64", "qwen-linux"], "self-hosted"),
    (["hellmini"], "self-hosted"), (["win-release"], "windows")])
def test_venue_from_runner_labels(labels: list[str], want: str) -> None:
    assert rrr.venue(labels) == want


# --- extraction -----------------------------------------------------------------------------


def test_extract_scope_rows_and_coverage(extraction: list[dict]) -> None:
    meta = extraction[0]
    assert meta["t"] == "meta"
    # r12 has no failed job record (dropped); r11 is cancelled (not red)
    assert meta["red_runs"] == 23 and meta["dropped_jobless"] == 1
    assert meta["failed_jobs"] == 24 and meta["failed_jobs_with_log"] == 19
    assert meta["manifest"] is False and meta["final"] is False
    assert meta["gaps"] == 5 and meta["gaps_explained"] == 0       # r1 gate, r5, r10 (empty), r13 (.err), r32
    items = [r for r in extraction if r["t"] == "item"]
    by = {(i["run"], i["item"]): i for i in items}
    test = by[(1, "test_x[a b]")]
    assert (test["kind"], test["file"], test["venue"], test["derived"]) == ("test", "tests/test_a.py", "hosted", False)
    assert test["touched_test"] is True and test["fix_touched_product"] is True
    assert (test["fix_distance"], test["fix_is_fix"], test["fix_table_only"]) == (1, True, False)
    assert by[(1, "Verify the pytest suite or its legitimate skip")]["derived"] is True
    assert by[(9, "bar")]["kind"] == "java" and by[(9, "bar")]["venue"] == "self-hosted"
    assert by[(13, "Windows test set")]["venue"] == "windows"
    runs = {r["id"]: r for r in extraction if r["t"] == "run"}
    assert runs[1]["fix_product_commit"] and runs[1]["green_run"] == 2 and runs[1]["range_len"] == 1
    assert runs[3]["rerun_green"] and runs[3]["fix_commits"] == []
    assert runs[24]["unresolved"] and runs[24]["green_run"] is None
    assert by[(24, "m")]["touched_test"] is None                   # never green: no fix signal
    assert runs[1]["event"] == "push"
    commits = {r["sha"]: r for r in extraction if r["t"] == "commit"}
    assert sorted(commits[runs[1]["fix_commits"][0]]["files"]) == ["src/nexus/a.py", "tests/test_a.py"]


def test_a_green_on_a_commit_that_does_not_descend_from_the_red_sha_is_skipped(
        extraction: list[dict]) -> None:
    runs = {r["id"]: r for r in extraction if r["t"] == "run"}
    # r37 is a success on the side branch and the only later CI green after r29..r36 (red at c6)
    assert runs[29]["green_run"] is None and runs[36]["unresolved"] is True


def test_fix_signal_ties_the_failure_to_the_first_commit_touching_the_file(extraction: list[dict]) -> None:
    items = {(i["run"], i["item"]): i for i in extraction if i["t"] == "item"}
    entries = items[(18, "test_entries")]
    assert (entries["fix_distance"], entries["fix_table_only"]) == (1, True)
    old = items[(20, "test_o")]
    assert (old["fix_distance"], old["fix_touched_product"], old["fix_is_fix"]) == (1, False, False)
    foo = items[(22, "bar")]
    assert (foo["touched_test"], foo["fix_touched_product"], foo["fix_is_fix"]) == (True, True, True)
    assert items[(14, "test_flaky")]["touched_test"] is False and items[(14, "test_flaky")]["fix_distance"] is None


def test_labels_name_the_rule_that_moved_each_run(extraction: list[dict]) -> None:
    an = rrr.analyze(extraction)
    got = {rid: (r["category"], r["rules"]) for rid, r in an["runs"].items()}
    assert got[1] == (rrr.PRODUCT, ["product:fix-touched-src"])
    assert got[3] == (rrr.ACCOUNTING, ["accounting:job"])       # accounting wins over the rerun-green
    assert got[5] == (rrr.ACCOUNTING, ["accounting:step"])
    assert got[7] == (rrr.FLAKE, ["flake:infra-signature"])
    assert got[9] == (rrr.UNATTRIBUTED, ["unattributed:resolved-without-test-fix"])   # no commit touched it
    assert got[10] == (rrr.BUILD, ["build:step-name"])
    assert got[13] == (rrr.UNLABELLED, ["unlabelled:no-log"])  # no log: stays visible
    assert got[14] == (rrr.UNATTRIBUTED, ["unattributed:resolved-without-test-fix"])
    assert got[16] == (rrr.STALE, ["stale:windows-venue-failed-count"])
    assert got[17] == (rrr.UNLABELLED, ["unlabelled:no-rule"])   # a log, but no name, count or reason
    assert got[18] == (rrr.ACCOUNTING, ["accounting:fix-table-edit"])
    assert got[20] == (rrr.STALE, ["stale:fix-touched-test"])
    assert got[22] == (rrr.PRODUCT, ["product:fix-touched-src"])         # product on a Java test
    assert got[24] == (rrr.UNATTRIBUTED, ["unattributed:no-verified-green"])
    assert got[25] == (rrr.ACCOUNTING, ["accounting:meta-msg"])          # a named Windows test, "budget"
    assert got[26] == (rrr.FLAKE, ["flake:timing-message"])
    assert got[27] == (rrr.FLAKE, ["flake:rerun-green"])
    assert got[29] == (rrr.HARNESS, ["harness:file"])
    assert got[30] == (rrr.ACCOUNTING, ["accounting:meta-file"])
    assert got[31] == (rrr.ACCOUNTING, ["accounting:meta-msg"])
    assert got[32] == (rrr.FLAKE, ["flake:infra-step"])
    assert got[33] == (rrr.STALE, ["stale:unnamed-test-failures"])
    assert got[36] == (rrr.UNLABELLED, ["unlabelled:no-rule"])  # the signature is not on an ##[error] line


def _item(**kw: object) -> dict:
    base = {"kind": "test", "file": "tests/test_z.py", "item": "test_z",
            "job": "pytest (Python 3.12, shard 1/6)", "step": "Run tests", "msg": "", "infra": False,
            "venue": "hosted", "has_log": True, "evidence": True, "failed_count": 0, "fix_distance": None,
            "fix_table_only": None, "fix_touched_product": None, "fix_is_fix": False,
            "touched_test": None, "derived": False}
    return {**base, **kw}


_RUN = {"rerun_green": False, "unresolved": False}


def test_a_touch_at_any_distance_is_the_fix_because_the_range_ends_at_the_verified_green() -> None:
    far = rrr.label_item(_item(fix_distance=40, touched_test=True, fix_touched_product=False), _RUN)
    assert far == (rrr.STALE, "stale:fix-touched-test")      # an earlier 10-commit window missed d=11..15


def test_a_test_with_no_verified_green_and_no_other_rule_is_unattributed() -> None:
    item = _item()
    assert rrr.label_item(item, {"rerun_green": False, "unresolved": True}) == (
        rrr.UNATTRIBUTED, "unattributed:no-verified-green")
    assert rrr.label_item(item, _RUN) == (rrr.UNATTRIBUTED, "unattributed:resolved-without-test-fix")


def test_a_count_pin_on_an_entries_dict_is_accounting_by_what_it_checks() -> None:
    pin = _item(file="", item="TestRealWindows.test_the_walk_finds_the_uv_entries", venue="windows",
                msg="AssertionError: {'conexus': 18, 'sn': 4}")
    other = _item(file="", item="TestRealWindows.test_x", venue="windows", msg="AssertionError: {0: 2, 1: 3}")
    assert rrr.label_item(pin, _RUN) == (rrr.ACCOUNTING, "accounting:meta-msg")
    assert rrr.label_item(other, _RUN) == (rrr.STALE, "stale:windows-venue")
    assert rrr.COUNT_PIN_RE.search("assert {'a': 1} == {'a': 2}")
    assert not rrr.COUNT_PIN_RE.search("AssertionError: {'a': [1, 2]}")


def test_a_product_fix_needs_a_fix_subject_and_a_table_edit_is_accounting() -> None:
    feat = _item(fix_distance=1, touched_test=True, fix_touched_product=True, fix_is_fix=False)
    fix = _item(fix_distance=1, touched_test=True, fix_touched_product=True, fix_is_fix=True)
    table = _item(fix_distance=1, touched_test=True, fix_table_only=True)
    assert rrr.label_item(feat, _RUN) == (rrr.STALE, "stale:fix-touched-test")    # an in-flight feature
    assert rrr.label_item(fix, _RUN) == (rrr.PRODUCT, "product:fix-touched-src")
    assert rrr.label_item(table, _RUN) == (rrr.ACCOUNTING, "accounting:fix-table-edit")


def test_the_name_regex_no_longer_swallows_product_tests() -> None:
    for stem in ("TaxonomyCoverageTest", "test_hooks_json_verb_release_floor", "test_catalog_walk",
                 "test_manifest_write_many"):
        item = _item(kind="java", file=stem) if stem[0].isupper() else _item(file=f"tests/{stem}.py")
        assert not rrr.META_FILE_RE.search(stem), stem
        assert rrr.label_item(item, _RUN)[0] != rrr.ACCOUNTING, stem
    for stem in ("test_storage_touch_guard", "test_module_seam", "SqlStringLiteralGateTest",
                 "test_wal_ceiling", "test_skip_floor"):
        assert rrr.META_FILE_RE.search(stem), stem


def test_a_test_function_name_and_a_drift_message_can_mark_an_accounting_check() -> None:
    by_name = _item(file="tests/daemon/test_lifecycle_gate.py", item="test_flock_acquire_sites_are_allowlisted")
    drifted = _item(msg="AssertionError: MCP wire schema drifted from the committed snapshot")
    sanctioned = _item(kind="java", file="ExtensionBootstrapSiteConformanceTest",
                       msg="[CREATE EXTENSION string literal outside SANCTIONED_JAVA_FILES]")
    assert rrr.label_item(by_name, _RUN) == (rrr.ACCOUNTING, "accounting:meta-file")
    behaviour = _item(item="test_the_jvm_env_allowlist_does_not_carry_the_password")
    drift_name = _item(kind="java", file="TaxonomyRepositoryTest", item="getDocCountDrift_namesOnlyTopics")
    assert rrr.label_item(behaviour, _RUN)[0] != rrr.ACCOUNTING          # an env allowlist is behaviour
    assert rrr.label_item(drift_name, _RUN)[0] != rrr.ACCOUNTING         # DocCountDrift is a feature
    assert rrr.label_item(drifted, _RUN) == (rrr.ACCOUNTING, "accounting:meta-msg")
    assert rrr.label_item(sanctioned, _RUN) == (rrr.ACCOUNTING, "accounting:meta-msg")


def test_message_rules_for_census_and_timing_come_before_the_file_rule() -> None:
    census = _item(msg="AssertionError: HTTP call sites that neither send the header nor say why")
    timing = _item(file="tests/test_x_census.py", msg="AssertionError: predicate not satisfied within 5.0s")
    assert rrr.label_item(census, _RUN) == (rrr.ACCOUNTING, "accounting:meta-msg")
    assert rrr.label_item(timing, _RUN) == (rrr.FLAKE, "flake:timing-message")


def test_step_items_without_a_name_use_the_first_error_line_then_the_failed_count() -> None:
    step = {"kind": "step", "file": "", "item": "Windows test set", "venue": "windows", "failed_count": 2}
    served = _item(**step, msg="AssertionError: 33 >= 36 right GETs were served while x")
    assert rrr.label_item(served, _RUN) == (rrr.FLAKE, "flake:timing-message")
    over = _item(**step, msg="AssertionError: over budget")
    assert rrr.label_item(over, _RUN) == (rrr.ACCOUNTING, "accounting:meta-msg")
    plain = _item(**step, msg="OSError: WinError 1314")
    assert rrr.label_item(plain, _RUN) == (rrr.STALE, "stale:windows-venue-failed-count")
    hosted = _item(**{**step, "venue": "hosted"}, msg="")
    assert rrr.label_item(hosted, _RUN) == (rrr.STALE, "stale:unnamed-test-failures")


def test_an_infra_signature_labels_only_items_with_no_named_test() -> None:
    step = _item(kind="step", file="", item="Run tests", infra=True)
    named = _item(infra=True, fix_distance=1, touched_test=True, fix_touched_product=False)
    assert rrr.label_item(step, _RUN) == (rrr.FLAKE, "flake:infra-signature")
    assert rrr.label_item(named, _RUN) == (rrr.STALE, "stale:fix-touched-test")


def test_a_named_windows_test_with_no_fix_is_windows_venue() -> None:
    item = _item(file="", item="TestPre.test_x", venue="windows")
    assert rrr.label_item(item, _RUN) == (rrr.STALE, "stale:windows-venue")


def test_a_fileless_test_is_matched_by_its_function_name_not_its_class() -> None:
    pin = _item(file="", item="test_flock_acquire_sites_are_allowlisted", venue="windows")
    cls = _item(file="", item="TestCensus.test_the_exact_spelling_still_matches", venue="windows")
    assert rrr.label_item(pin, _RUN) == (rrr.ACCOUNTING, "accounting:meta-file")
    assert rrr.label_item(cls, _RUN) == (rrr.STALE, "stale:windows-venue")    # a pid census, not a repo one


def test_checks_are_named_and_derived_items_do_not_count(extraction: list[dict]) -> None:
    counts = rrr.check_counts(rrr.analyze(extraction))
    assert counts["storage-touch ratchet"] == 1 and counts["docs_reference_rot"] == 1
    assert counts["a"] == 1 and counts["FooIntegrationTest"] == 1 and counts["Lint src/ with ruff"] == 1
    assert counts["Windows test set"] == 2 and counts["BarTest"] == 1
    assert counts["TestPre"] == 1                      # a fileless Windows test is named by its class
    assert "pytest-gate verdict" not in counts         # r1's gate step is derived from the shard failure


def test_a_floor_step_is_derived_only_when_another_item_shows_what_failed() -> None:
    quiet = {"failed_count": 0, "evidence": False, "infra": False}
    floor = {"kind": "step", "item": "Non-vacuity floor", **quiet}
    silent = {"kind": "step", "item": "Windows test set", **quiet}
    named = {"kind": "test", "item": "t", **quiet}
    only, with_silent, with_named = [dict(floor)], [dict(silent), dict(floor)], [dict(named), dict(floor)]
    for items in (only, with_silent, with_named):
        rrr._mark_derived(items)
    assert only[0]["derived"] is False and with_silent[1]["derived"] is False
    assert with_named[1]["derived"] is True


def test_only_the_final_attempts_failed_jobs_count() -> None:
    jobs = [_job(1, "a", [], attempt=1), _job(2, "a", [], attempt=2),
            _job(3, "b", [], conclusion="success", attempt=2)]
    assert [j["id"] for j in rrr._failed_jobs({"run_attempt": 2}, jobs)] == [2]
    assert [j["id"] for j in rrr._failed_jobs({"run_attempt": 1}, [_job(5, "x", [])])] == [5]


def test_extra_log_roots_are_searched_after_the_snapshot(tmp_path: Path) -> None:
    snap, extra = tmp_path / "snap", tmp_path / "extra"
    (extra / "7").mkdir(parents=True)
    (extra / "7" / "9.log").write_text("FAILED tests/test_q.py::test_q - boom\n")
    assert rrr.read_log(snap, 7, 9) is None
    got = rrr.read_log(snap, 7, 9, [extra])
    assert got is not None and got["extra"] is True and got["tests"][0]["file"] == "tests/test_q.py"


def test_a_run_with_no_jobs_file_stays_visible_and_is_a_gap(
        snapshot: Path, history: tuple[Path, dict[str, str]]) -> None:
    (snapshot / "jobs" / "26.json").unlink()
    recs = _extract(snapshot, history[0], since="2026-10-01", until="2026-10-01")
    assert recs[0]["runs_missing_jobs_file"] == 1 and recs[0]["red_runs"] == 23
    assert recs[0]["gaps"] == 6                      # the five log gaps and r26's missing jobs list
    assert rrr.analyze(recs)["runs"][26]["rules"] == ["unlabelled:jobs-missing"]


def test_extraction_is_deterministic_independent_of_input_order_and_round_trips(
        snapshot: Path, history: tuple[Path, dict[str, str]], tmp_path: Path) -> None:
    first = _extract(snapshot, history[0])
    runs = snapshot / "runs.jsonl"
    runs.write_text("".join(reversed(runs.read_text().splitlines(keepends=True))))
    second = _extract(snapshot, history[0])
    a, b = tmp_path / "a.jsonl.gz", tmp_path / "b.jsonl.gz"
    rrr.write_extraction(first, a)
    rrr.write_extraction(second, b)
    assert a.read_bytes() == b.read_bytes()
    assert rrr.read_extraction(a) == json.loads(json.dumps(first))


def test_scope_options_are_explicit(snapshot: Path, history: tuple[Path, dict[str, str]]) -> None:
    kw = {"since": "2026-10-01", "until": "2026-10-01"}
    # a cancelled run has no failed job record, so widening the conclusions alone adds nothing
    assert _extract(snapshot, history[0], conclusions=("failure", "cancelled"), **kw)[0]["red_runs"] == 23
    wide = _extract(snapshot, history[0], conclusions=("failure", "cancelled"), include_jobless=True, **kw)[0]
    assert wide["red_runs"] == 25 and wide["dropped_jobless"] == 0
    assert _extract(snapshot, history[0], since="2026-10-02", until="2026-10-03")[0]["red_runs"] == 0


# --- git failures are not empty histories ----------------------------------------------------


def test_a_bad_repo_raises_instead_of_reading_as_an_empty_history(snapshot: Path, tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="git"):
        rrr.extract(snapshot, tmp_path / "no-such-repo", since="2026-10-01", until="2026-10-01")
    with pytest.raises(RuntimeError, match="git"):
        rrr.commit_mix(tmp_path / "no-such-repo", "2026-10-01", "2026-10-02")


def test_an_extraction_that_read_no_commit_is_refused_by_the_guard_not_by_git(
        snapshot: Path, history: tuple[Path, dict[str, str]], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rrr.History, "load", lambda self, shas: None)     # git works; no commit is read
    with pytest.raises(RuntimeError, match="no commit was read"):
        _extract(snapshot, history[0])


def test_commit_mix_counts_the_fixture_history(history: tuple[Path, dict[str, str]]) -> None:
    repo, _ = history
    mix = rrr.commit_mix(repo, "2000-01-01", "2037-12-31", ref="origin/develop")
    assert mix["total"] == 7 and mix["product"] == 3        # c1 c2 c6 touch src/ or service/src/main
    assert mix["tests_only"] == 3 and mix.get("ledger_or_ceiling", 0) == 0 and mix["other"] == 1   # c3 c4 c5; c0
    with pytest.raises(RuntimeError, match="no commit"):
        rrr.commit_mix(repo, "2000-01-01", "2000-01-02", ref="origin/develop")


def test_is_table_line_separates_data_from_logic() -> None:
    for line in ("    'b',", '    "src/x.py",', "# a comment", "", "    (1, 2),", "X = 'a'", "]"):
        assert rrr.is_table_line(line), line
    for line in ("    assert x == 1", "def test_a():", "    return 1", "    if y:", "    call(arg)"):
        assert not rrr.is_table_line(line), line


# --- snapshot completeness --------------------------------------------------------------------


def _manifest(snap: Path, tsv: str, jobs_missing: str = "") -> None:
    snap.mkdir(exist_ok=True)
    (snap / "MANIFEST.json").write_text(json.dumps({"jobs_missing": jobs_missing, "red_scope_buckets": "b=1"}))
    (snap / "missing.tsv").write_text(tsv)


def test_a_snapshot_is_final_only_when_every_gap_is_listed_in_the_manifest(tmp_path: Path) -> None:
    gap_404 = {"run": 5, "job": 50, "kind": "log", "steps": 0}
    assert rrr.snapshot_status(tmp_path / "none", [gap_404]) == {
        "manifest": False, "final": False, "gaps": 1, "gaps_explained": 0, "buckets": ""}
    snap = tmp_path / "snap"
    _manifest(snap, "5\t50\tlog\tHTTP 404 gh: HTTP 404\n")
    assert rrr.snapshot_status(snap, [gap_404])["final"] is True
    assert rrr.snapshot_status(snap, [])["final"] is True
    with_steps = {**gap_404, "steps": 3}                       # a job that ran steps should have a log
    unlisted = {"run": 6, "job": 60, "kind": "log", "steps": 0}
    assert rrr.snapshot_status(snap, [with_steps])["final"] is False
    assert rrr.snapshot_status(snap, [gap_404, unlisted]) == {
        "manifest": True, "final": False, "gaps": 2, "gaps_explained": 1, "buckets": "b=1"}
    _manifest(snap, "5\t50\tlog\tHTTP 500 gh: server error\n")
    assert rrr.snapshot_status(snap, [gap_404])["final"] is False      # not a 404


def test_a_run_with_no_jobs_list_is_explained_only_by_the_manifest(tmp_path: Path) -> None:
    gap = {"run": 9, "job": 0, "kind": "jobs", "steps": 0}
    snap = tmp_path / "snap"
    _manifest(snap, "9\t-\tjobs\tgh: Server Error (HTTP 502)\n", jobs_missing="run 9: jobs list HTTP 502")
    assert rrr.snapshot_status(snap, [gap])["final"] is True
    _manifest(snap, "9\t-\tjobs\tgh: Server Error (HTTP 502)\n", jobs_missing="")
    assert rrr.snapshot_status(snap, [gap])["final"] is False


# --- red time, caveat, samples ---------------------------------------------------------------


def test_red_hours_run_from_red_completion_to_the_next_decided_run_completion(extraction: list[dict]) -> None:
    hrs = rrr.red_hours(rrr.analyze(extraction))
    # Each CI red run's stretch lasts from its completion to the completion of the next decided CI
    # run, and every run here completes 30 s after it starts, so a stretch is the minutes between
    # the two start times. In minutes: r1 r3 r5 r7 r14 r17 r18 r20 r26 r27 r29..r32 r36 take 1 each
    # (15 runs), r10 -> r14 takes 4 and r33 -> r36 takes 3: 22 in all.
    stretches = {1: 1, 3: 1, 5: 1, 7: 1, 10: 4, 14: 1, 17: 1, 18: 1, 20: 1, 26: 1, 27: 1, 29: 1, 30: 1,
                 31: 1, 32: 1, 33: 3, 36: 1}
    assert hrs["_total"] == pytest.approx(sum(stretches.values()) / 60, abs=0.006)
    assert hrs["product_defect"] == pytest.approx(1 / 60, abs=0.006)      # r1 alone
    assert hrs["build_ruff"] == pytest.approx(4 / 60, abs=0.006)          # r10 -> r14 is four minutes


def test_the_accounting_caveat_counts_items_fixes_and_product_touching_fixes(extraction: list[dict]) -> None:
    text = rrr.accounting_caveat(rrr.analyze(extraction))
    assert "258-research-15" in text and "accounting items" in text
    assert "followed by a fix commit that touched the failing file" in text
    assert "of all 23 red runs (floor) to" in text and "attributed runs" in text


def test_the_unattributed_caveat_states_the_bracket(extraction: list[dict]) -> None:
    an = rrr.analyze(extraction)
    cats = rrr.category_counts(an)
    n = cats[rrr.UNATTRIBUTED]
    text = rrr.unattributed_caveat(an)
    assert n == 3 and f"unattributed {n} runs" in text
    assert f"flake is {cats[rrr.FLAKE]}..{cats[rrr.FLAKE] + n}" in text
    assert f"stale {cats[rrr.STALE]}..{cats[rrr.STALE] + n}" in text
    assert "product candidates" in text and "unattributed:no-verified-green" in text


def test_sample_is_deterministic_bounded_and_drawn_from_the_rule(extraction: list[dict]) -> None:
    an = rrr.analyze(extraction)
    both = rrr.sample_runs(an, "accounting:meta-msg", 5, 7)
    assert sorted(r["run"] for r in both) == [25, 31] and {r["rule"] for r in both} == {"accounting:meta-msg"}
    assert both[0]["candidates"] == 2 and rrr.sample_runs(an, "no:such-rule", 3, 1) == []
    assert "accounting:meta-msg" in rrr.render_sample(an, "accounting:meta-msg", 2, 7)


def _population(n: int) -> dict:
    runs = {i: {"rules": ["r"], "range_len": 1} for i in range(n)}
    items = {i: [{"rule": "r", "derived": False, "workflow": "CI", "job": "j", "item": f"t{i}", "file": "f",
                  "msg": ""}] for i in range(n)}
    return {"runs": runs, "by_run": items, "commits": {}}


def test_the_sample_depends_on_the_seed_and_only_on_the_seed() -> None:
    an = _population(60)
    draw = lambda seed: [r["run"] for r in rrr.sample_runs(an, "r", 6, seed)]   # noqa: E731
    assert draw(1) == draw(1) and draw(2) == draw(2)
    assert draw(1) != draw(2)                                   # a draw that ignored the seed would not
    assert len(set(draw(1))) == 6 and set(draw(1)) <= set(range(60))
    assert draw(1) == sorted(draw(1))                           # reported in run order


# --- guard-catch seam -------------------------------------------------------------------------


def test_guard_catches_follow_the_mapping(extraction: list[dict]) -> None:
    an = rrr.analyze(extraction)
    # the only fix commit in range for r1 touched src/nexus/a.py and tests/test_a.py
    assert rrr.guard_catches(an, {"a": ["src/nexus/"]}) == {"a": 1}
    assert rrr.guard_catches(an, {"a": ["docs/"]}) == {"a": 0}
    assert rrr.guard_catches(an, {"unknown-check": ["src/"]}) == {}


def test_guard_catches_ignore_ledger_ceiling_and_allowlist_data_files() -> None:
    def an_with(files: list[str]) -> dict:
        return {"items": [{"check": "c", "run": 1, "derived": False}],
                "runs": {1: {"fix_commits": ["abc"]}}, "commits": {"abc": {"files": files}}}

    guards = {"c": ["tests/", "scripts/"]}
    assert rrr.guard_catches(an_with(["tests/ddl_ceiling.toml"]), guards) == {"c": 0}
    assert rrr.guard_catches(an_with(["scripts/file-size-exceptions.txt", "tests/x_allowlist.txt"]),
                             guards) == {"c": 0}
    assert rrr.guard_catches(an_with(["tests/ddl_ceiling.toml", "tests/test_z.py"]), guards) == {"c": 1}
    # a test module that merely has "allowlist" or "pending" in its name is guard code, not a ledger
    assert rrr.guard_catches(an_with(["tests/plans/test_plan_step_tool_allowlist.py"]), guards) == {"c": 1}
    assert rrr.is_ledger_file("docs/wire-contract-pending.md")
    assert not rrr.is_ledger_file("tests/test_pending_jobs.py")


# --- MVV tolerance math -------------------------------------------------------------------


@pytest.mark.parametrize(("ours", "want", "rel", "floor", "ok"), [
    (614, 602, 0.02, 0, True), (615, 602, 0.02, 0, False),      # 2% of 602 is 12.04
    (14, 14, 0.02, 0, True), (15, 14, 0.02, 0, False),          # 2% of 14 is under one run
    (74, 68, 0.10, 3, True), (75, 68, 0.10, 3, False),          # 10% of 68 is 6.8
    (23, 20, 0.10, 3, True), (24, 20, 0.10, 3, False),          # the 3-run floor beats 10% of 20
    (7, 4, 0.10, 3, True), (8, 4, 0.10, 3, False)])
def test_within_tolerance_edges(ours: int, want: int, rel: float, floor: int, ok: bool) -> None:
    assert rrr.within(ours, want, rel, floor) is ok


def _fake(workflows: dict[str, int], cats: dict[str, int], checks: dict[str, int],
          final: bool = True) -> dict:
    wf = [w for w, n in workflows.items() for _ in range(n)]
    cat = [c for c, n in cats.items() for _ in range(n)]
    cat = (cat + [rrr.FLAKE] * len(wf))[:len(wf)]
    runs = {i: {"workflow": w, "category": c, "rules": ["accounting:job"], "event": "push"}
            for i, (w, c) in enumerate(zip(wf, cat, strict=True))}
    items = [{"check": k, "run": i, "derived": False} for k, n in checks.items() for i in range(n)]
    meta = {"final": final, "manifest": final, "gaps": 0, "gaps_explained": 0, "failed_jobs": 1,
            "failed_jobs_with_log": 1}
    return {"runs": runs, "items": items, "meta": meta}


def _strand_b() -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    cats = {**rrr.STRAND_B_CATEGORIES, rrr.FLAKE: 118}      # strand B's six sum to 788 against 787 runs
    return dict(rrr.STRAND_B_WORKFLOWS), cats, dict(rrr.STRAND_B_CHECKS)


def test_mvv_passes_on_strand_b_figures_when_the_snapshot_is_final() -> None:
    text, ok = rrr.render_mvv(_fake(*_strand_b()))
    assert ok, text
    assert text.count("PASS") == 1 + 5 + 7 + 6 and "FAIL" not in text and "PARTIAL" not in text
    assert "INFO  unlabelled runs: 0" in text


def test_mvv_is_not_ok_on_partial_data_even_when_every_line_passes() -> None:
    text, ok = rrr.render_mvv(_fake(*_strand_b(), final=False))
    assert "FAIL" not in text and text.startswith("PARTIAL") and ok is False


def test_mvv_fails_per_line_and_explains_a_category_by_rule() -> None:
    wf, cats, checks = _strand_b()
    wf["CI"] += 13                                  # 615 > 602 + 2%
    cats[rrr.ACCOUNTING] -= 60                      # outside max(10%, 3) of 501
    cats[rrr.STALE] += 60
    checks["tmp_root"] += 4                         # 24 > 20 + 3
    text, ok = rrr.render_mvv(_fake(wf, cats, checks))
    assert not ok
    fails = [ln for ln in text.splitlines() if ln.startswith("FAIL")]
    assert any("workflow CI" in ln for ln in fails) and any("check tmp_root" in ln for ln in fails)
    assert any("category stale_ambient" in ln and "rules:" in ln for ln in fails)
    assert any("category accounting" in ln for ln in fails)


def test_report_prints_buckets_events_rules_and_the_caveat(extraction: list[dict]) -> None:
    text = rrr.render_report(rrr.analyze(extraction))
    assert "by event (information): push" in text and "snapshot scope buckets (information)" in text
    assert "Caveat (258-research-15)" in text and "stale:fix-touched-test" in text
    assert "unattributed " in text and "Counted in none of strand B's six" in text
    assert text.splitlines()[1].startswith("PARTIAL")


def test_cli_extract_report_sample_and_mvv(snapshot: Path, history: tuple[Path, dict[str, str]],
                                           tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "x.jsonl.gz"
    assert rrr.main(["extract", "--snapshot", str(snapshot), "--repo", str(history[0]), "--out", str(out),
                     "--green-jobs", str(snapshot / "green_jobs"),
                     "--since", "2026-10-01", "--until", "2026-10-01"]) == rrr.EXIT_OK
    assert rrr.main(["report", "--extraction", str(out)]) == rrr.EXIT_OK
    assert "PARTIAL" in capsys.readouterr().out
    assert rrr.main(["report", "--extraction", str(out), "--sample", "accounting:job", "5",
                     "--seed", "3"]) == rrr.EXIT_OK
    assert "sample of 1 runs for rule accounting:job" in capsys.readouterr().out
    assert rrr.main(["report", "--extraction", str(out), "--sample", "no:such-rule", "5"]) == rrr.EXIT_FAIL
    assert "no run is labelled by rule 'no:such-rule'" in capsys.readouterr().err
    assert rrr.main(["mvv", "--extraction", str(out)]) == rrr.EXIT_FAIL   # 23 red runs is not 787


def test_cli_mvv_exits_zero_when_every_line_passes_on_a_final_snapshot(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(rrr, "read_extraction", lambda path: [])
    monkeypatch.setattr(rrr, "analyze", lambda records: _fake(*_strand_b()))
    assert rrr.main(["mvv", "--extraction", str(tmp_path / "x.gz")]) == rrr.EXIT_OK
    assert "FAIL" not in capsys.readouterr().out
    monkeypatch.setattr(rrr, "analyze", lambda records: _fake(*_strand_b(), final=False))
    assert rrr.main(["mvv", "--extraction", str(tmp_path / "x.gz")]) == rrr.EXIT_FAIL


def test_mvv_prints_the_unattributed_line_with_both_brackets() -> None:
    wf, cats, checks = _strand_b()
    cats = {**cats, rrr.FLAKE: 113, rrr.UNATTRIBUTED: 5}      # 787 runs
    text, ok = rrr.render_mvv(_fake(wf, cats, checks))
    assert ok, text
    line = next(ln for ln in text.splitlines() if "unattributed 5 runs" in ln)
    assert line.startswith("INFO") and "flake 113..118 (strand B 119)" in line
    assert "stale 86..91 (strand B 86)" in line and "counted in none of strand B's six" in line


# --- resolution by a passing job ------------------------------------------------------------


def test_job_names_passed_needs_every_exact_name_to_have_succeeded() -> None:
    jobs = [{"name": "pytest (Python 3.12, shard 1/6)", "conclusion": "success"},
            {"name": "pytest (Python 3.12, shard 2/6)", "conclusion": "skipped"},
            {"name": "pytest-gate", "conclusion": "success"}]
    assert rrr.job_names_passed(jobs, ["pytest (Python 3.12, shard 1/6)", "pytest-gate"])
    assert not rrr.job_names_passed(jobs, ["pytest (Python 3.12, shard 2/6)"])        # skipped
    assert not rrr.job_names_passed(jobs, ["pytest (Python 3.12, shard 1/4)"])        # no such name
    assert not rrr.job_names_passed([], ["x"])


def test_a_later_run_that_skipped_the_failing_job_does_not_resolve_the_red(
        snapshot: Path, history: tuple[Path, dict[str, str]]) -> None:
    _set_green(snapshot, 2, {"pytest (Python 3.12, shard 1/6)": "skipped"})     # r1's first later success
    runs = {r["id"]: r for r in _extract(snapshot, history[0]) if r["t"] == "run"}
    assert runs[1]["green_run"] == 4 and runs[1]["green_skipped"] == [2]
    assert runs[1]["range_len"] == 2                           # c1..c3, not c1..c2


def test_a_red_with_no_later_run_that_passed_its_job_is_unresolved(
        snapshot: Path, history: tuple[Path, dict[str, str]]) -> None:
    for rid in (19, 21, 28):                                  # every later success on a descendant of r18's sha
        _set_green(snapshot, rid, {"pytest (Python 3.12, shard 1/6)": "skipped"})
    recs = _extract(snapshot, history[0])
    run = next(r for r in recs if r["t"] == "run" and r["id"] == 18)
    assert run["unresolved"] and run["green_run"] is None and run["green_skipped"] == [19, 21, 28]
    assert rrr.analyze(recs)["runs"][18]["rules"] == ["unattributed:no-verified-green"]


def test_a_missing_green_jobs_file_is_an_error_not_a_silent_skip(
        snapshot: Path, history: tuple[Path, dict[str, str]]) -> None:
    (snapshot / "green_jobs" / "2.json").unlink()
    with pytest.raises(RuntimeError, match="no jobs file for later run 2"):
        _extract(snapshot, history[0])


def test_fetch_green_jobs_walks_until_a_job_passes_and_caches(snapshot: Path, tmp_path: Path) -> None:
    asked: list[int] = []

    def fake(run_id: int) -> list[dict]:
        asked.append(run_id)
        return [{"name": "pytest (Python 3.12, shard 1/6)", "conclusion": "skipped" if run_id == 2 else "success"},
                {"name": "pytest-gate", "conclusion": "success"}]

    out = tmp_path / "gj"
    got = rrr.fetch_green_jobs(snapshot, out, since="2026-10-01", until="2026-10-01", workers=1, fetch=fake)
    assert got["fetched"] == len(asked) == len(list(out.glob("*.json")))
    assert {2, 4} <= set(asked)                                 # r1 skipped at r2, then passed at r4
    again = rrr.fetch_green_jobs(snapshot, out, since="2026-10-01", until="2026-10-01", workers=1, fetch=fake)
    assert again["fetched"] == 0                                # cached


# --- candidates -----------------------------------------------------------------------------


def test_candidates_list_named_flake_tests_and_keep_unattributed_apart(
        extraction: list[dict], tmp_path: Path) -> None:
    an = rrr.analyze(extraction)
    report = rrr.candidates_report(an, {"archive_sha256": "abc"})
    flake = {r["nodeid"]: r for r in report["candidates"]}
    assert sorted(flake) == ["tests/test_r.py::test_r", "tests/test_t.py::test_t"]      # steps have no row
    row = flake["tests/test_t.py::test_t"]
    assert (row["red_runs"], row["first_red"], row["last_red"], row["venues"], row["rule"]) == (
        1, "2026-10-01", "2026-10-01", ["hosted"], "flake:timing-message")
    assert flake["tests/test_r.py::test_r"]["rule"] == "flake:rerun-green"
    assert [r["nodeid"] for r in report["unattributed"]] == [
        "BarTest::m", "FooIntegrationTest::bar", "tests/test_b.py::test_flaky"]
    assert report["unattributed"][0]["rule"] == "unattributed:no-verified-green"
    assert report["header"]["candidate_tests"] == 2 and report["header"]["unattributed_tests"] == 3
    assert {r["nodeid"] for r in report["candidates"]}.isdisjoint(r["nodeid"] for r in report["unattributed"])


def test_candidates_count_distinct_red_runs_and_dates_across_runs() -> None:
    def item(run: int, rule: str = "flake:timing-message") -> dict:
        return {"kind": "test", "file": "tests/test_t.py", "item": "test_t", "run": run, "derived": False,
                "category": rrr.FLAKE, "rule": rule, "venue": "windows" if run == 3 else "hosted"}

    an = {"runs": {1: {"created": "2026-10-05T10:00:00Z"}, 2: {"created": "2026-10-01T23:59:59Z"},
                   3: {"created": "2026-11-02T00:00:01Z"}},
          "items": [item(1), item(1), item(2), item(3, "flake:rerun-green")], "meta": {}}
    row = rrr.candidates_report(an, {})["candidates"][0]
    assert (row["red_runs"], row["first_red"], row["last_red"]) == (3, "2026-10-01", "2026-11-02")
    assert row["venues"] == ["hosted", "windows"] and row["rule"] == "flake:rerun-green|flake:timing-message"
    assert row["run_ids"] == [1, 2, 3]


def test_cli_candidates_writes_a_json_header_with_the_hashes(
        snapshot: Path, history: tuple[Path, dict[str, str]], tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    extraction_file, out = tmp_path / "x.jsonl.gz", tmp_path / "c.json"
    rrr.write_extraction(_extract(snapshot, history[0]), extraction_file)
    repo_root = Path(__file__).resolve().parent.parent
    assert rrr.main(["candidates", "--extraction", str(extraction_file), "--out", str(out)]) == rrr.EXIT_OK
    got = json.loads(out.read_text())
    header = got["header"]
    assert header["archive_sha256"] == rrr.PHASE0_ARCHIVE_SHA256 == (
        "520722bfbf0a9e75b566093f5ab19078ef3e3c41f39af4a356714d07b7f8a968")
    assert header["extraction_sha256"] == hashlib.sha256(extraction_file.read_bytes()).hexdigest()
    assert header["script_sha256"] == hashlib.sha256((repo_root / "scripts" / "red_run_report.py").read_bytes()).hexdigest()
    assert "script_commit" not in header and header["window"] == ["2026-10-01", "2026-10-01"]
    assert header["snapshot_final"] is False and set(got) == {"header", "candidates", "unattributed"}
    assert "2 flake tests, 3 unattributed tests" in capsys.readouterr().out
    assert out.read_text().endswith("\n") and json.loads(out.read_text()) == got


def test_the_ledger_regex_no_longer_takes_a_pending_name_for_a_ledger() -> None:
    assert not rrr.is_ledger_file("tests/test_pending_jobs.py")
    assert not rrr.is_ledger_file("docs/notes/pending-items.md")
    assert rrr.is_ledger_file("docs/wire-contract-pending.md") and rrr.is_ledger_file("tests/x_ceiling.toml")


def test_candidates_out_is_deterministic_gzip_when_the_path_ends_in_gz(
        snapshot: Path, history: tuple[Path, dict[str, str]], tmp_path: Path) -> None:
    extraction_file = tmp_path / "x.jsonl.gz"
    rrr.write_extraction(_extract(snapshot, history[0]), extraction_file)
    plain, one, two = tmp_path / "c.json", tmp_path / "one" / "c.json.gz", tmp_path / "two" / "c.json.gz"
    for out in (plain, one, two):
        out.parent.mkdir(exist_ok=True)
        assert rrr.main(["candidates", "--extraction", str(extraction_file), "--out", str(out)]) == rrr.EXIT_OK
    assert one.read_bytes()[:2] == b"\x1f\x8b" and one.read_bytes() == two.read_bytes()   # mtime 0, no name
    with gzip.open(one, "rt", encoding="utf-8") as fh:
        assert fh.read() == plain.read_text()
    assert plain.read_bytes()[:1] == b"{"


def test_the_route_scans_skip_a_red_run_data_dir_and_catch_the_same_file_elsewhere(tmp_path: Path) -> None:
    """The red-run data quotes test names that name the routes; only that directory is exempt."""
    # the module names are split: this file must not name the routes the scans look for
    ttl_scan = importlib.import_module("tests.test_catalog_ttl_" + "check_no_trigger")
    retire_scan = importlib.import_module("tests.test_retire_" + "keys_no_trigger")

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=60)
    route = "catalog_ttl_" + "check"                     # split so this file does not name the route itself
    for rel in ("scripts/red-run-data/names.txt", "scripts/elsewhere/names.txt"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(f"tests/test_{route}_x.py::test_a\n", encoding="utf-8")
    seen, _ = ttl_scan._scan(tmp_path)
    assert "scripts/elsewhere/names.txt" in seen and "scripts/red-run-data/names.txt" not in seen
    listed = {p.relative_to(tmp_path).as_posix() for p in retire_scan.TestNoAutomaticTrigger._files(tmp_path)}
    assert "scripts/elsewhere/names.txt" in listed and "scripts/red-run-data/names.txt" not in listed
    assert "red-run-data" in ttl_scan.SKIP_DIRS and "red-run-data" in retire_scan.TestNoAutomaticTrigger.SKIP_DIRS
