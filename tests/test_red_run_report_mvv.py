# SPDX-License-Identifier: AGPL-3.0-or-later
"""The red-run report's MVV tolerance, report, CLI, candidates and route-scan tests (nexus-2jomg.1).

Split from ``test_red_run_report.py`` to stay under the test file cap; the fixtures and builders are in
``_red_run_fixtures.py``. Run with ``NX_TEST_T2_SUBSTRATE=none uv run pytest tests/test_red_run_report_mvv.py``.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib
import json
import subprocess
from pathlib import Path

import pytest
from tests._red_run_fixtures import (  # noqa: F401
    _extract, _run, _set_green, _shard, extraction, history, inventory_repo, snapshot,
)

import red_run_report as rrr


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
    assert {r["path_source"] for r in report["candidates"]} == {"summary"}
    assert report["header"]["path_sources"] == {"candidates": {"summary": 2},
                                                 "unattributed": {"java": 2, "summary": 1}}
    assert {r["nodeid"]: r["path_source"] for r in report["unattributed"]} == {
        "BarTest::m": "java", "FooIntegrationTest::bar": "java", "tests/test_b.py::test_flaky": "summary"}
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
        snapshot: Path, history: tuple[Path, dict[str, str]], tmp_path: Path, inventory_repo: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    extraction_file, out = tmp_path / "x.jsonl.gz", tmp_path / "c.json"
    rrr.write_extraction(_extract(snapshot, history[0]), extraction_file)
    repo_root = Path(__file__).resolve().parent.parent
    assert rrr.main(["candidates", "--extraction", str(extraction_file), "--out", str(out),
                     "--repo", str(inventory_repo)]) == rrr.EXIT_OK
    got = json.loads(out.read_text())
    header = got["header"]
    assert header["archive_sha256"] == rrr.PHASE0_ARCHIVE_SHA256 == (
        "520722bfbf0a9e75b566093f5ab19078ef3e3c41f39af4a356714d07b7f8a968")
    assert header["extraction_sha256"] == hashlib.sha256(extraction_file.read_bytes()).hexdigest()
    assert header["script_sha256"] == hashlib.sha256((repo_root / "scripts" / "red_run_report.py").read_bytes()).hexdigest()
    assert "script_commit" not in header and header["window"] == ["2026-10-01", "2026-10-01"]
    assert header["snapshot_final"] is False and set(got) == {"header", "candidates", "unattributed", "absent"}
    assert "2 flake tests, 3 unattributed tests" in capsys.readouterr().out
    assert out.read_text().endswith("\n") and json.loads(out.read_text()) == got


def test_the_ledger_regex_no_longer_takes_a_pending_name_for_a_ledger() -> None:
    assert not rrr.is_ledger_file("tests/test_pending_jobs.py")
    assert not rrr.is_ledger_file("docs/notes/pending-items.md")
    assert rrr.is_ledger_file("docs/wire-contract-pending.md") and rrr.is_ledger_file("tests/x_ceiling.toml")


def test_candidates_out_is_deterministic_gzip_when_the_path_ends_in_gz(
        snapshot: Path, history: tuple[Path, dict[str, str]], tmp_path: Path, inventory_repo: Path) -> None:
    extraction_file = tmp_path / "x.jsonl.gz"
    rrr.write_extraction(_extract(snapshot, history[0]), extraction_file)
    plain, one, two = tmp_path / "c.json", tmp_path / "one" / "c.json.gz", tmp_path / "two" / "c.json.gz"
    for out in (plain, one, two):
        out.parent.mkdir(exist_ok=True)
        assert rrr.main(["candidates", "--extraction", str(extraction_file), "--out", str(out),
                         "--repo", str(inventory_repo)]) == rrr.EXIT_OK
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
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, timeout=60)   # the scans read tracked files
    seen, _ = ttl_scan._scan(tmp_path)
    assert "scripts/elsewhere/names.txt" in seen and "scripts/red-run-data/names.txt" not in seen
    listed = {p.relative_to(tmp_path).as_posix() for p in retire_scan.TestNoAutomaticTrigger._files(tmp_path)}
    assert "scripts/elsewhere/names.txt" in listed and "scripts/red-run-data/names.txt" not in listed
    assert "red-run-data" in ttl_scan.SKIP_DIRS and "red-run-data" in retire_scan.TestNoAutomaticTrigger.SKIP_DIRS


# --- paths recovered from the traceback --------------------------------------------------------


def _section(name: str, *body: str) -> list[str]:
    return [f"_____ {name} _____", *body, "E   AssertionError: no", "==== short test summary info ===="]


def test_the_section_path_is_the_location_line_nearest_the_def_line() -> None:
    lines = _section("TestK.test_x[a.b]", "", "tmp = 1", "    def test_x(self, tmp):", "        helper()",
                     "> other()", "", "tests/test_z.py:7: AssertionError",
                     "src/nexus/mod.py:40: in other", "/usr/lib/python3.12/contextlib.py:81: in inner")
    assert rrr.parse_log(lines)["tests"][0]["tb_path"] == "tests/test_z.py"
    win = _section("test_w", "    def test_w():", "x",
                   "C:\\build\\runner-win\\_work\\nexus\\nexus\\tests\\daemon\\test_w.py:332: AssertionError")
    assert rrr.parse_log(win)["tests"][0]["tb_path"] == "tests/daemon/test_w.py"
    far_first = _section("test_n", "src/nexus/a.py:1: in a", "    def test_n():", "tests/test_n.py:5: Boom")
    assert rrr.parse_log(far_first)["tests"][0]["tb_path"] == "tests/test_n.py"        # nearest, not first
    no_def = _section("test_q", "src/nexus/a.py:1: in a", "tests/test_q.py:5: Boom")
    assert rrr.parse_log(no_def)["tests"][0]["tb_path"] == "src/nexus/a.py"            # first location line
    assert rrr.parse_log(_section("test_none", "no location here"))["tests"][0]["tb_path"] == ""


def test_a_location_after_the_summary_header_is_not_the_sections() -> None:
    lines = [*_section("test_x", "    def test_x():"), "tests/late.py:3: AssertionError"]
    assert rrr.parse_log(lines)["tests"][0]["tb_path"] == ""


def _mini(tmp_path: Path, log: str) -> Path:
    """One red CI run at c4 (job 101) and a green rerun, whose only failing item comes from ``log``."""
    snap = tmp_path / "mini"
    for d in ("jobs", "green_jobs", "logs/1"):
        (snap / d).mkdir(parents=True)
    runs = [_run(1, "CI", "{c4}", "failure", 1), _run(2, "CI", "{c4}", "success", 2)]
    (snap / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in runs))
    (snap / "jobs" / "1.json").write_text(json.dumps([_shard(101)]))
    (snap / "green_jobs" / "2.json").write_text(json.dumps([
        {"id": 201, "name": "pytest (Python 3.12, shard 1/6)", "conclusion": "success", "run_attempt": 1}]))
    (snap / "logs" / "1" / "101.log").write_text(log)
    return snap


def _recover(tmp_path: Path, history: tuple[Path, dict[str, str]], log: str) -> dict:
    snap = _mini(tmp_path, log)
    runs = (snap / "runs.jsonl").read_text().replace("{c4}", history[1]["c4"])
    (snap / "runs.jsonl").write_text(runs)
    items = [r for r in _extract(snap, history[0]) if r["t"] == "item"]
    assert len(items) == 1
    return items[0]


def test_a_bare_name_takes_the_traceback_path_when_that_file_defines_the_test(
        tmp_path: Path, history: tuple[Path, dict[str, str]]) -> None:
    item = _recover(tmp_path, history, "\n".join(_section(
        "TestK.test_o[p.q]", "    def test_o(self):", "tests/test_old.py:5: AssertionError")) + "\n")
    assert (item["file"], item["path_source"]) == ("tests/test_old.py", "traceback") and "tb_path" not in item
    assert rrr.pytest_nodeid(item) == "tests/test_old.py::TestK::test_o[p.q]"      # dots become ::, params kept
    assert item["touched_test"] is False                                          # the fix signal now works


@pytest.mark.parametrize("body", [
    ("    def test_o():", "tests/test_a.py:3: AssertionError"),          # the file exists but has no test_o
    ("    def test_o():", "tests/test_missing.py:3: AssertionError"),    # no such file at the sha
    ("    def test_o():", "src/nexus/a.py:2: AssertionError"),           # a repo file with no such def
    ("    def test_o():",)])                                              # no location line at all
def test_a_traceback_path_that_does_not_define_the_test_leaves_the_name_bare(
        tmp_path: Path, history: tuple[Path, dict[str, str]], body: tuple[str, ...]) -> None:
    item = _recover(tmp_path, history, "\n".join(_section("test_o", *body)) + "\n")
    assert (item["file"], item["path_source"]) == ("", "none")
    assert rrr.pytest_nodeid(item) == "test_o"


def test_history_defines_reads_the_file_at_the_sha(history: tuple[Path, dict[str, str]]) -> None:
    hist = rrr.History(history[0])
    c3 = history[1]["c3"]
    assert hist.defines(c3, "tests/test_old.py", "test_o")
    assert not hist.defines(c3, "tests/test_old.py", "test_nope") and not hist.defines(c3, "tests/none.py", "test_o")
    assert not hist.defines(history[1]["c2"], "tests/test_old.py", "test_o")       # not there yet at c2


def test_a_fixture_error_header_names_the_test_and_a_collection_error_names_none() -> None:
    got = rrr.parse_log(_section("ERROR at setup of TestK.test_o", "    def test_o(self):",
                                 "tests/test_old.py:5: AssertionError"))
    assert got["tests"][0]["name"] == "TestK.test_o" and got["tests"][0]["tb_path"] == "tests/test_old.py"
    item = {"kind": "test", "file": "", "item": "ERROR collecting gw0", "run": 1, "derived": False,
            "category": rrr.UNATTRIBUTED, "rule": "r", "venue": "hosted", "path_source": "none"}
    an = {"runs": {1: {"created": "2026-10-01T00:00:00Z"}}, "items": [item], "meta": {}}
    assert rrr.candidates_report(an, {})["unattributed"] == []
