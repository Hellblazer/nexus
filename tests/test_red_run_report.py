# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/red_run_report.py over a small synthetic snapshot and a throwaway git repo (nexus-2jomg.1).

No network, no tenant fixture: the snapshot is files in ``tmp_path`` and the history is a repo
``git init``-ed there. The MVV tolerance math is tested on counts, not on the real snapshot.
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


def _commit(repo: Path, subject: str, files: list[str]) -> str:
    for rel in files:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.read_text() + "x\n" if path.exists() else "x\n")
    _git(repo, "add", *files)
    _git(repo, "commit", "-q", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def history(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "develop")
    sha = {
        "c0": _commit(repo, "chore: base", ["README.md"]),
        "c1": _commit(repo, "feat: a", ["src/nexus/a.py"]),
        "c2": _commit(repo, "fix(a): repair", ["src/nexus/a.py", "tests/test_a.py"]),
        "c3": _commit(repo, "test: touch old", ["tests/test_old.py"]),
        "c4": _commit(repo, "chore: ledger", ["tests/ddl_ceiling.toml"]),
    }
    return repo, sha


def _run(rid: int, name: str, sha: str, conclusion: str, minute: int, attempt: int = 1) -> dict:
    ts = f"2026-10-01T10:{minute:02d}:00Z"
    return {"id": rid, "name": name, "head_sha": sha, "conclusion": conclusion, "created_at": ts,
            "updated_at": f"2026-10-01T10:{minute:02d}:30Z", "run_attempt": attempt}


def _job(jid: int, name: str, steps: list[tuple[str, str]], labels: list[str] | None = None,
         conclusion: str = "failure") -> dict:
    return {"id": jid, "name": name, "conclusion": conclusion, "labels": labels or ["ubuntu-latest"],
            "steps": [{"name": n, "conclusion": c} for n, c in steps]}


_PYTEST_FAIL = (
    "2026-10-01T10:00:00.1Z =========================== short test summary info ====\n"
    "2026-10-01T10:00:00.2Z FAILED {node} - AssertionError: boom\n"
    "2026-10-01T10:00:00.3Z ##[error]Process completed with exit code 1.\n")


@pytest.fixture
def snapshot(tmp_path: Path, history: tuple[Path, dict[str, str]]) -> Path:
    _, c = history
    snap = tmp_path / "snap"
    (snap / "jobs").mkdir(parents=True)
    runs = [
        # r1: behavioural test red at c1, fixed by a fix() commit touching src and the test
        _run(1, "CI", c["c1"], "failure", 1),
        _run(2, "CI", c["c2"], "success", 2),
        # r3: lint-marked docs_reference_rot red at c3, rerun green on the same sha
        _run(3, "CI", c["c3"], "failure", 3),
        _run(4, "CI", c["c3"], "success", 4),
        # r5: storage-touch ratchet step red at c4, rerun green
        _run(5, "CI", c["c4"], "failure", 5),
        _run(6, "CI", c["c4"], "success", 6),
        # r7: shard job with a hosted-runner network failure, rerun green
        _run(7, "CI", c["c4"], "failure", 7),
        _run(8, "CI", c["c4"], "success", 8),
        # r9: Java test red, never green again
        _run(9, "Service CI", c["c3"], "failure", 9),
        # r10: ruff step; r11: cancelled (not red); r12: failure with no job records
        _run(10, "CI", c["c4"], "failure", 10),
        _run(11, "CI", c["c4"], "cancelled", 11),
        _run(12, "ci.yml", c["c4"], "failure", 12),
        # r13: Windows rehearsal, test red with no log yet
        _run(13, "windows-pg-bundle-rehearsal", c["c3"], "failure", 13),
        # r14: test red at c2 whose next green (c3) never touched the test or product: green without a fix
        _run(14, "CI", c["c2"], "failure", 14),
        _run(15, "CI", c["c3"], "success", 15),
        # r16: Windows run with a failed-count summary but no failing test names; r17: a job that died silently
        _run(16, "windows-pg-bundle-rehearsal", c["c4"], "failure", 16),
        _run(17, "CI", c["c4"], "failure", 17),
    ]
    (snap / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in runs))
    jobs = {
        1: [_job(101, "pytest (Python 3.12, shard 1/6)", [("Set up job", "success"), ("Run tests (shard 1/6)", "failure")]),
            _job(102, "pytest-gate", [("Verify the pytest suite or its legitimate skip", "failure")])],
        3: [_job(301, "pytest (lint markers, group 1/2)", [("Run lint-marked tests", "failure")])],
        5: [_job(501, "pytest-gate", [("Tenant-saving ratchet over the whole run (storage-touch fan-in)", "failure")])],
        7: [_job(701, "pytest (Python 3.12, shard 2/6)", [("Run tests (shard 2/6)", "failure")])],
        9: [_job(901, "Java tests + jOOQ codegen drift guard", [("Run Java test suite", "failure")],
                 labels=["lab-6c18g"])],
        10: [_job(1001, "ruff lint", [("Lint src/ with ruff", "failure")])],
        11: [_job(1101, "pytest (Python 3.12, shard 1/6)", [("Run tests (shard 1/6)", "cancelled")], conclusion="cancelled")],
        12: [],
        13: [_job(1301, "Windows test set (win-release)", [("Windows test set", "failure")], labels=["win-release"])],
        14: [_job(1401, "pytest (Python 3.12, shard 1/6)", [("Run tests (shard 1/6)", "failure")])],
        16: [_job(1601, "Windows test set (win-release)", [("Windows test set", "failure")], labels=["win-release"])],
        17: [_job(1701, "pytest (Python 3.12, shard 3/6)", [("Run tests (shard 3/6)", "failure")])],
    }
    for rid, js in jobs.items():
        (snap / "jobs" / f"{rid}.json").write_text(json.dumps(js))
    logs = {
        (1, 101): _PYTEST_FAIL.format(node="tests/test_a.py::test_x[a b]"),
        (3, 301): _PYTEST_FAIL.format(node="tests/test_docs_reference_rot.py::test_cited"),
        (14, 1401): _PYTEST_FAIL.format(node="tests/test_b.py::test_flaky"),
        (16, 1601): "2026-10-01T10:00:00Z ==== 40 failed, 1139 passed, 45 skipped in 94.24s ====\n",
        (17, 1701): "2026-10-01T10:00:00Z Cleaning up orphan processes\n",
        (7, 701): "2026-10-01T10:00:00Z ##[error]The Unable to download CodeQL: Unexpected HTTP response: 504\n",
        (9, 901): ("2026-10-01T10:00:00Z [ERROR] Failures: \n"
                   "2026-10-01T10:00:00Z [ERROR]   FooIntegrationTest.bar:12 expected 1\n"
                   "2026-10-01T10:00:00Z [ERROR] \n"
                   "2026-10-01T10:00:00Z [ERROR] Tests run: 5, Failures: 1\n"),
    }
    for (rid, jid), text in logs.items():
        d = snap / "logs" / str(rid)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{jid}.log").write_text(text)
    # a .err stub and an empty .log must count as "no log"
    (snap / "logs" / "13").mkdir(parents=True)
    (snap / "logs" / "13" / "1301.log.err").write_text("the response contains terminal escape sequences")
    return snap


@pytest.fixture
def extraction(snapshot: Path, history: tuple[Path, dict[str, str]]) -> list[dict]:
    return rrr.extract(snapshot, history[0], since="2026-10-01", until="2026-10-01")


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


@pytest.mark.parametrize(("labels", "want"), [
    (["ubuntu-latest"], "hosted"), (["ubuntu-24.04-arm"], "hosted"), (["macos-14"], "hosted"),
    (["lab-6c18g"], "self-hosted"), (["self-hosted", "Linux", "X64", "qwen-linux"], "self-hosted"),
    (["hellmini"], "self-hosted"), (["win-release"], "windows")])
def test_venue_from_runner_labels(labels: list[str], want: str) -> None:
    assert rrr.venue(labels) == want


def test_extract_scope_rows_and_coverage(extraction: list[dict]) -> None:
    meta = extraction[0]
    assert meta["t"] == "meta"
    # r1 r3 r5 r7 r9 r10 r13 r14 r16 r17 are red; r11 is cancelled, r12 has no failed job record
    assert meta["red_runs"] == 10 and meta["dropped_jobless"] == 1
    assert meta["failed_jobs"] == 11 and meta["failed_jobs_with_log"] == 7
    assert meta["manifest"] is False
    items = [r for r in extraction if r["t"] == "item"]
    by = {(i["run"], i["item"]): i for i in items}
    test = by[(1, "test_x[a b]")]
    assert (test["kind"], test["file"], test["venue"], test["derived"]) == ("test", "tests/test_a.py", "hosted", False)
    assert test["touched_test"] is True and test["fix_touched_product"] is True
    assert by[(1, "Verify the pytest suite or its legitimate skip")]["derived"] is True
    assert by[(9, "bar")]["kind"] == "java" and by[(9, "bar")]["venue"] == "self-hosted"
    assert by[(9, "bar")]["touched_test"] is None          # never green: no fix signal
    assert by[(13, "Windows test set")]["venue"] == "windows"
    runs = {r["id"]: r for r in extraction if r["t"] == "run"}
    assert runs[1]["fix_product_commit"] and runs[1]["green_run"] == 2 and len(runs[1]["fix_commits"]) == 1
    assert runs[3]["rerun_green"] and runs[3]["fix_commits"] == []
    assert runs[9]["unresolved"] and runs[9]["green_run"] is None
    commits = {r["sha"]: r for r in extraction if r["t"] == "commit"}
    assert sorted(commits[runs[1]["fix_commits"][0]]["files"]) == ["src/nexus/a.py", "tests/test_a.py"]


def test_labels_name_the_rule_that_moved_each_run(extraction: list[dict]) -> None:
    an = rrr.analyze(extraction)
    got = {rid: (r["category"], r["rules"]) for rid, r in an["runs"].items()}
    assert got[1] == (rrr.PRODUCT, ["product:fix-touched-src"])
    assert got[3] == (rrr.ACCOUNTING, ["accounting:job"])       # accounting wins over the rerun-green
    assert got[5] == (rrr.ACCOUNTING, ["accounting:step"])
    assert got[7] == (rrr.FLAKE, ["flake:infra-signature"])
    assert got[9] == (rrr.STALE, ["stale:no-green"])             # a behavioural test, never fixed
    assert got[10] == (rrr.BUILD, ["build:step-name"])
    assert got[13] == (rrr.UNLABELLED, ["unlabelled:step-without-log"])  # no log: stays visible
    assert got[14] == (rrr.FLAKE, ["flake:green-without-fix"])
    assert got[16] == (rrr.STALE, ["stale:windows-portability"])
    assert got[17] == (rrr.FLAKE, ["flake:no-failure-evidence"])


def test_checks_are_named_and_derived_items_do_not_count(extraction: list[dict]) -> None:
    an = rrr.analyze(extraction)
    assert rrr.check_counts(an) == {
        "storage-touch ratchet": 1, "docs_reference_rot": 1, "a": 1, "FooIntegrationTest": 1,
        "Lint src/ with ruff": 1, "Windows test set": 2, "Run tests (shard 2/6)": 1, "b": 1,
        "Run tests (shard 3/6)": 1}


def test_extraction_is_deterministic_and_round_trips(extraction: list[dict], tmp_path: Path) -> None:
    a, b = tmp_path / "a.jsonl.gz", tmp_path / "b.jsonl.gz"
    rrr.write_extraction(extraction, a)
    rrr.write_extraction(extraction, b)
    assert a.read_bytes() == b.read_bytes()
    assert rrr.read_extraction(a) == json.loads(json.dumps(extraction))


def test_scope_options_are_explicit(snapshot: Path, history: tuple[Path, dict[str, str]]) -> None:
    kw = {"since": "2026-10-01", "until": "2026-10-01"}
    # a cancelled run has no failed job record, so widening the conclusions alone adds nothing
    assert rrr.extract(snapshot, history[0], conclusions=("failure", "cancelled"), **kw)[0]["red_runs"] == 10
    wide = rrr.extract(snapshot, history[0], conclusions=("failure", "cancelled"),
                       include_jobless=True, **kw)[0]
    assert wide["red_runs"] == 12 and wide["dropped_jobless"] == 0
    assert rrr.extract(snapshot, history[0], **{"since": "2026-10-02", "until": "2026-10-03"})[0]["red_runs"] == 0


def test_guard_catches_follow_the_mapping(extraction: list[dict]) -> None:
    an = rrr.analyze(extraction)
    # the only fix commit in range for r1 touched src/nexus/a.py and tests/test_a.py
    assert rrr.guard_catches(an, {"a": ["src/nexus/"]}) == {"a": 1}
    assert rrr.guard_catches(an, {"a": ["docs/"]}) == {"a": 0}
    assert rrr.guard_catches(an, {"unknown-check": ["src/"]}) == {}


def test_guard_catches_ignore_ledger_ceiling_and_allowlist_files() -> None:
    def an_with(files: list[str]) -> dict:
        return {"items": [{"check": "c", "run": 1, "derived": False}],
                "runs": {1: {"fix_commits": ["abc"]}}, "commits": {"abc": {"files": files}}}

    guards = {"c": ["tests/", "scripts/"]}
    assert rrr.guard_catches(an_with(["tests/ddl_ceiling.toml"]), guards) == {"c": 0}
    assert rrr.guard_catches(an_with(["scripts/file-size-exceptions.txt", "tests/x_allowlist.txt"]), guards) == {"c": 0}
    assert rrr.guard_catches(an_with(["tests/ddl_ceiling.toml", "tests/test_z.py"]), guards) == {"c": 1}


def test_red_hours_runs_from_red_completion_to_next_decided_run(extraction: list[dict]) -> None:
    hrs = rrr.red_hours(rrr.analyze(extraction))
    # r1 red -> r2 green is 1 minute (+0s) in the fixture; every stretch is one minute
    assert 0 < hrs["_total"] < 1 and hrs[rrr.PRODUCT] > 0


# --- MVV tolerance math -------------------------------------------------------------------


@pytest.mark.parametrize(("ours", "want", "rel", "floor", "ok"), [
    (614, 602, 0.02, 0, True), (615, 602, 0.02, 0, False),      # 2% of 602 is 12.04
    (14, 14, 0.02, 0, True), (15, 14, 0.02, 0, False),          # 2% of 14 is under one run
    (74, 68, 0.10, 3, True), (75, 68, 0.10, 3, False),          # 10% of 68 is 6.8
    (23, 20, 0.10, 3, True), (24, 20, 0.10, 3, False),          # the 3-run floor beats 10% of 20
    (7, 4, 0.10, 3, True), (8, 4, 0.10, 3, False)])
def test_within_tolerance_edges(ours: int, want: int, rel: float, floor: int, ok: bool) -> None:
    assert rrr.within(ours, want, rel, floor) is ok


def _fake(workflows: dict[str, int], cats: dict[str, int], checks: dict[str, int]) -> dict:
    wf = [w for w, n in workflows.items() for _ in range(n)]
    cat = [c for c, n in cats.items() for _ in range(n)]
    cat = (cat + [rrr.FLAKE] * len(wf))[:len(wf)]
    runs = {i: {"workflow": w, "category": c, "rules": ["accounting:job"]}
            for i, (w, c) in enumerate(zip(wf, cat, strict=True))}
    items = [{"check": k, "run": i, "derived": False}
             for k, n in checks.items() for i in range(n)]
    return {"runs": runs, "items": items, "meta": {"manifest": True, "failed_jobs": 1, "failed_jobs_with_log": 1}}


def _strand_b() -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    cats = {**rrr.STRAND_B_CATEGORIES, rrr.FLAKE: 118}      # strand B's six sum to 788 against 787 runs
    return dict(rrr.STRAND_B_WORKFLOWS), cats, dict(rrr.STRAND_B_CHECKS)


def test_mvv_passes_on_strand_b_figures() -> None:
    text, ok = rrr.render_mvv(_fake(*_strand_b()))
    assert ok, text
    assert text.count("PASS") == 1 + 5 + 7 + 6 + 1 and "FAIL" not in text and "PARTIAL" not in text


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


def test_mvv_marks_partial_data_and_unlabelled_runs() -> None:
    an = _fake(*_strand_b())
    an["meta"] = {"manifest": False, "failed_jobs": 10, "failed_jobs_with_log": 4}
    text, _ = rrr.render_mvv(an)
    assert text.startswith("PARTIAL: logs for 4 of 10 failed jobs, manifest absent")


def test_cli_extract_then_mvv_exit_code(snapshot: Path, history: tuple[Path, dict[str, str]],
                                        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "x.jsonl.gz"
    assert rrr.main(["extract", "--snapshot", str(snapshot), "--repo", str(history[0]), "--out", str(out),
                     "--since", "2026-10-01", "--until", "2026-10-01"]) == rrr.EXIT_OK
    assert rrr.main(["report", "--extraction", str(out)]) == rrr.EXIT_OK
    assert "PARTIAL DATA" in capsys.readouterr().out
    assert rrr.main(["mvv", "--extraction", str(out)]) == rrr.EXIT_FAIL   # 7 red runs is not 787
