# SPDX-License-Identifier: AGPL-3.0-or-later
"""The candidates step's test-inventory check: a row naming a test that is gone at a rev is set apart (nexus-2jomg.1)."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from tests._red_run_fixtures import _commit, _extract, _git, history, snapshot  # noqa: F401

import red_run_report as rrr

PY = """\
import pytest


class TestWorker:
    def test_drain(self):
        pass

    async def test_async_drain(self):
        pass


@pytest.mark.parametrize("x", [1])
def test_plain(x):
    pass
"""
JAVA = """\
package dev;

class RouterIT {
    @Test void zeroRoutesExact() {}
    void helper() { zeroRoutesExactly(); }
}
"""


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "inv"
    root.mkdir()
    _git(root, "init", "-q", "-b", "develop")
    sha = _commit(root, "feat: tests", {"tests/test_w.py": PY,
                                        "service/src/test/java/dev/RouterIT.java": JAVA})
    return root, sha


def _row(nodeid: str, kind: str = "test", **kw: object) -> dict:
    return {"nodeid": nodeid, "kind": kind, "path_source": "summary", "red_runs": 1, **kw}


@pytest.mark.parametrize("nodeid,kind,reason", [
    ("tests/test_w.py::test_plain", "test", None),
    ("tests/test_w.py::TestWorker::test_drain", "test", None),
    ("tests/test_w.py::TestWorker::test_async_drain", "test", None),
    ("tests/test_w.py::test_plain[1-x]", "test", None),                 # a parametrize id is stripped
    ("tests/test_w.py::test_plain[a::b.c]", "test", None),              # whatever it holds
    ("tests/gone.py::test_plain", "test", "file gone"),
    ("tests/test_w.py::test_removed", "test", "test gone"),
    ("tests/test_w.py::TestWorker::test_plain", "test", None),          # a class is checked beside the name
    ("tests/test_w.py::TestGone::test_drain", "test", "test gone"),
    ("tests/test_w.py::test_drai", "test", "test gone"),                # a prefix is not the name
    ("RouterIT::zeroRoutesExact", "java", None),
    ("RouterIT::zeroNeverRoutesExact", "java", "test gone"),            # renamed away
    ("RouterIT::zeroRoutes", "java", "test gone"),                      # a word, not a substring
    ("RouterIT::helper", "java", None),
    ("GoneIT::zeroRoutesExact", "java", "file gone"),
])
def test_a_row_is_present_when_its_file_class_and_test_are_at_the_rev(
        repo: tuple[Path, str], nodeid: str, kind: str, reason: str | None) -> None:
    assert rrr.Inventory(repo[0]).absent_reason(_row(nodeid, kind)) == reason


def test_a_bare_name_is_present_when_a_test_file_defines_it_and_gone_when_none_does(
        repo: tuple[Path, str]) -> None:
    inv = rrr.Inventory(repo[0])
    assert inv.absent_reason(_row("test_plain", path_source="none")) is None
    assert inv.absent_reason(_row("TestWorker.test_drain[p]", path_source="none")) is None
    assert inv.absent_reason(_row("TestWorker.test_plain", path_source="none")) is None   # in a file with the class
    assert inv.absent_reason(_row("test_removed", path_source="none")) == "test gone"
    assert inv.absent_reason(_row("TestGone.test_drain", path_source="none")) == "test gone"


def test_the_rev_is_resolved_to_a_full_sha_and_decides_what_exists(repo: tuple[Path, str]) -> None:
    root, first = repo
    _git(root, "rm", "-q", "tests/test_w.py")
    _git(root, "commit", "-q", "-m", "test: drop")
    now, then = rrr.Inventory(root), rrr.Inventory(root, "HEAD~1")
    assert then.rev == first and len(now.rev) == 40 and now.rev != first
    row = _row("tests/test_w.py::test_plain")
    assert now.absent_reason(row) == "file gone" and then.absent_reason(row) is None


def test_a_repo_with_no_files_at_the_rev_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    _git(root, "init", "-q", "-b", "develop")
    _git(root, "commit", "-q", "--allow-empty", "-m", "empty")
    with pytest.raises(RuntimeError, match="no files"):
        rrr.Inventory(root)
    with pytest.raises(RuntimeError):
        rrr.Inventory(tmp_path / "not-a-repo")


def _an(rows: list[tuple[str, str, str]]) -> dict:
    """rows: (category, file, item) of python tests, one red run."""
    items = [{"kind": "test", "file": f, "item": i, "run": 1, "derived": False, "category": c,
              "rule": "r", "venue": "hosted", "path_source": "summary"} for c, f, i in rows]
    return {"runs": {1: {"created": "2026-10-01T00:00:00Z"}}, "items": items, "meta": {}}


def test_absent_rows_leave_the_active_lists_and_the_header_counts_both(repo: tuple[Path, str]) -> None:
    an = _an([(rrr.FLAKE, "tests/test_w.py", "test_plain"),
              (rrr.FLAKE, "tests/test_w.py", "test_removed"),
              (rrr.FLAKE, "tests/gone.py", "test_x"),
              (rrr.UNATTRIBUTED, "tests/test_w.py", "TestWorker::test_drain"),
              (rrr.UNATTRIBUTED, "tests/test_w.py", "TestGone::test_drain")])
    report = rrr.candidates_report(an, {"archive_sha256": "abc"}, rrr.Inventory(repo[0]))
    assert [r["nodeid"] for r in report["candidates"]] == ["tests/test_w.py::test_plain"]
    assert [r["nodeid"] for r in report["unattributed"]] == ["tests/test_w.py::TestWorker::test_drain"]
    assert all(r["present"] is True and "absent_reason" not in r
               for r in report["candidates"] + report["unattributed"])
    absent = report["absent"]
    assert [(r["nodeid"], r["present"], r["absent_reason"]) for r in absent["candidates"]] == [
        ("tests/gone.py::test_x", False, "file gone"), ("tests/test_w.py::test_removed", False, "test gone")]
    assert [(r["nodeid"], r["absent_reason"]) for r in absent["unattributed"]] == [
        ("tests/test_w.py::TestGone::test_drain", "test gone")]
    h = report["header"]
    assert h["inventory_rev"] == repo[1] and len(h["inventory_rev"]) == 40
    assert h["inventory"] == {"candidates": {"present": 1, "absent": 2},
                              "unattributed": {"present": 1, "absent": 1}}
    assert (h["candidate_tests"], h["unattributed_tests"]) == (1, 1)             # the active lists
    assert h["path_sources"] == {"candidates": {"summary": 1}, "unattributed": {"summary": 1}}


def test_without_an_inventory_the_report_is_unchanged_and_has_no_absent_key() -> None:
    report = rrr.candidates_report(_an([(rrr.FLAKE, "tests/x.py", "test_a")]), {})
    assert set(report) == {"header", "candidates", "unattributed"}
    assert "present" not in report["candidates"][0] and "inventory_rev" not in report["header"]


def _cli(snapshot: Path, history: tuple[Path, dict[str, str]], tmp_path: Path, repo: Path, *extra: str) -> Path:
    extraction_file, out = tmp_path / "x.jsonl.gz", tmp_path / "c.json.gz"
    rrr.write_extraction(_extract(snapshot, history[0]), extraction_file)
    code = rrr.main(["candidates", "--extraction", str(extraction_file), "--out", str(out),
                     "--repo", str(repo), *extra])
    assert code == rrr.EXIT_OK
    return out


def test_cli_candidates_checks_the_inventory_at_head_or_at_a_named_rev_deterministically(
        snapshot: Path, history: tuple[Path, dict[str, str]], tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "inv"
    root.mkdir()
    _git(root, "init", "-q", "-b", "develop")
    c1 = _commit(root, "feat: t", {"tests/test_t.py": "def test_t():\n    pass\n",
                                   "tests/test_r.py": "def test_r():\n    pass\n"})
    c2 = _commit(root, "test: drop r", {"tests/test_other.py": "x = 1\n"})
    _git(root, "rm", "-q", "tests/test_r.py")
    _git(root, "commit", "-q", "-m", "test: gone")
    head = _git(root, "rev-parse", "HEAD")
    out = _cli(snapshot, history, tmp_path, root)
    with gzip.open(out, "rt", encoding="utf-8") as fh:
        got = json.load(fh)
    assert got["header"]["inventory_rev"] == head
    assert [r["nodeid"] for r in got["candidates"]] == ["tests/test_t.py::test_t"]
    assert [(r["nodeid"], r["absent_reason"]) for r in got["absent"]["candidates"]] == [
        ("tests/test_r.py::test_r", "file gone")]
    assert "1 flake tests, 0 unattributed tests (absent at " + head[:12] in capsys.readouterr().out
    again = tmp_path / "again"
    again.mkdir()
    assert _cli(snapshot, history, again, root).read_bytes() == out.read_bytes()
    older = tmp_path / "older"
    older.mkdir()
    old = _cli(snapshot, history, older, root, "--rev", c1)
    with gzip.open(old, "rt", encoding="utf-8") as fh:
        assert json.load(fh)["header"]["inventory_rev"] == c1 != c2


def test_cli_candidates_refuses_when_no_row_is_present_at_the_rev(
        snapshot: Path, history: tuple[Path, dict[str, str]], tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    extraction_file, out = tmp_path / "x.jsonl.gz", tmp_path / "c.json"
    rrr.write_extraction(_extract(snapshot, history[0]), extraction_file)
    code = rrr.main(["candidates", "--extraction", str(extraction_file), "--out", str(out),
                     "--repo", str(history[0])])                 # the fixture repo has none of the named tests
    assert code == rrr.EXIT_FAIL and not out.exists()
    assert "no row is present" in capsys.readouterr().err
