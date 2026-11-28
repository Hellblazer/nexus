# SPDX-License-Identifier: AGPL-3.0-or-later
"""The candidates step's test-inventory check: a row naming a test that is gone at a rev is set apart (nexus-2jomg.1)."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from tests._red_run_fixtures import _commit, _extract, _git, history, inventory_repo, snapshot  # noqa: F401

import red_run_report as rrr
from red_run_inventory import Inventory

PY = """\
import pytest


class TestWorker:
    def test_drain(self):
        pass

    async def test_async_drain(self):
        pass

    test_attr = test_drain


@pytest.mark.parametrize("x", [1])
def test_plain(x):
    pass


def _mk():
    def inner():
        pass
    return inner


test_dyn = _mk()
test_ann: object = _mk()
"""
BASES = """\
class Base:
    def test_inherited(self):
        pass

    test_by_assign = None


class Mid(Base):
    pass


class Chain0:
    def test_deep(self):
        pass
""" + "".join(f"\n\nclass Chain{i}(Chain{i - 1}):\n    pass\n" for i in range(1, 8))
DERIVED = """\
import unittest
from base import Base, Mid
from thirdparty import Thing


class TestDerived(Mid):
    pass


class TestSame(TestDerived):
    pass


class TestUnresolved(Thing):
    pass


class TestStd(unittest.TestCase):
    def test_s(self):
        pass


class TestCycleA(TestCycleB):
    pass


class TestCycleB(TestCycleA):
    pass


class TestA:
    def test_x(self):
        pass


class TestB:
    def test_y(self):
        pass


class Outer:
    class Inner:
        def test_n(self):
            pass
"""
JAVA = """\
package dev;

class RouterIT {
    @Test void zeroRoutesExact() {}
    void helper() { zeroRoutesExactly(); this.callSite(); }
    // void commented() {}
    String s = "void quoted() {}";
}
"""
JAVA_BASE = """\
abstract class AbsBase {
    @Test void inheritedT() {}
}
"""
JAVA_MID = """\
class Mid<T extends Number> extends AbsBase {
}
"""
JAVA_DER = """\
class Der extends Mid<Integer> {
    @Test
    public
    void
    multi
    () throws Exception {}
}
"""
JAVA_UNRES = """\
class Unres extends ThirdPartyBase {
    @Test <T extends Number> java.util.List<T> gen() { return null; }
}
"""
BINDINGS = {
    "tests/test_imp.py": (
        "from helpers import test_imported\nimport helpers as test_modalias\n"
        "try:\n    from helpers import test_try\nexcept ImportError:\n    pass\n\n\n"
        "class TestImp:\n    from helpers import test_cls_imp\n"),
    "tests/test_star.py": "from helpers import *\n\n\nclass TestS:\n    pass\n",
    "tests/test_fac.py": (
        "def make():\n    return type('T', (), {})\n\n\nTestFac = make()\nTestFac2: type = make()\n\n\n"
        "class TestReal:\n    pass\n"),
    "tests/test_loop.py": (
        "import sys\n\nfor n in range(3):\n    def test_loop():\n        pass\n\n"
        "for m in range(3):\n    class TestLoopCls:\n        def test_x(self):\n            pass\n\n"
        "while False:\n    def test_while():\n        pass\n\n"
        "match sys.platform:\n    case 'linux':\n        def test_match():\n            pass\n"),
    "tests/test_plat.py": (
        "import sys\n\nif sys.platform == 'win32':\n    class TestPlat:\n        def test_win(self):\n            pass\n"
        "else:\n    class TestPlat:\n        def test_posix(self):\n            pass\n"),
}
JIFACE = "interface Iface {\n    default void ifaceDefault() {}\n}\n"
JIMPL = {
    "Impl": "class Impl implements Iface {}\n",
    "Both": "class Both extends AbsBase implements Iface {}\n",
    "Impl2": "class Impl2 implements ThirdIface {}\n",
    "Impl3": "import java.io.Serializable;\nclass Impl3 implements Runnable, Serializable {}\n",
    "SubIface": "interface SubIface extends Iface {}\n",
    "ViaSub": "class ViaSub implements SubIface {}\n",
}
JDIR = "service/src/test/java/dev/"


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "inv"
    root.mkdir()
    _git(root, "init", "-q", "-b", "develop")
    sha = _commit(root, "feat: tests", {
        "tests/test_w.py": PY, "tests/base.py": BASES, "tests/test_d.py": DERIVED,
        "tests/sp ace/test_s.py": "def test_s():\n    pass\n",
        "tests/test_new.py": "def test_moved():\n    pass\n",
        **BINDINGS, JDIR + "Iface.java": JIFACE, **{JDIR + k + ".java": v for k, v in JIMPL.items()},
        "tests/test_cmp.py": "class TestCmp:\n    if True:\n        def test_cond(self):\n            pass\n",
        JDIR + "RouterIT.java": JAVA, JDIR + "AbsBase.java": JAVA_BASE, JDIR + "Mid.java": JAVA_MID,
        JDIR + "Der.java": JAVA_DER, JDIR + "Unres.java": JAVA_UNRES,
        JDIR + "OldIT.java": "class OldIT { @Test void zeroNeverRoutesExact_onTheWalk() {} }\n",
        JDIR + "Other.java": "class Other { @Test void zeroNeverRoutesExact() {} }\n"})
    return root, sha


def _row(nodeid: str, kind: str = "test", **kw: object) -> dict:
    return {"nodeid": nodeid, "kind": kind, "path_source": "summary", "red_runs": 1, **kw}


T = "tests/test_w.py::"
D = "tests/test_d.py::"


@pytest.mark.parametrize("nodeid,kind,absent,present_reason", [
    (T + "test_plain", "test", None, None),
    (T + "TestWorker::test_drain", "test", None, None),
    (T + "TestWorker::test_async_drain", "test", None, None),
    (T + "TestWorker::test_attr", "test", None, None),                  # defined by assignment in the class
    (T + "test_dyn", "test", None, None),                               # by assignment at module level
    (T + "test_ann", "test", None, None),                               # by annotated assignment
    (T + "test_plain[1-x]", "test", None, None),                        # a parametrize id is stripped
    (T + "test_plain[a::b.c]", "test", None, None),                     # whatever it holds
    ("tests/gone.py::test_plain", "test", "file gone", None),
    (T + "test_removed", "test", "test gone", None),
    (T + "TestWorker::test_plain", "test", "test gone", None),          # a module test is not in the class
    (T + "TestGone::test_drain", "test", "test gone", None),
    (T + "test_drai", "test", "test gone", None),                       # a prefix is not the name
    (D + "TestA::test_x", "test", None, None),
    (D + "TestA::test_y", "test", "test gone", None),                   # beside, not inside
    (D + "Outer::Inner::test_n", "test", None, None),
    (D + "Outer::test_n", "test", "test gone", None),
    ("tests/sp ace/test_s.py::test_s", "test", None, None),
    ("tests/test_cmp.py::TestCmp::test_cond", "test", None, None),      # under an if in the class body
    (D + "TestDerived::test_inherited", "test", None, None),            # def in a base in another file, via Mid
    (D + "TestSame::test_inherited", "test", None, None),               # two levels of derived
    (D + "TestDerived::test_by_assign", "test", None, None),            # an inherited assignment
    (D + "TestDerived::test_removed", "test", "test gone", None),       # every base resolved, none has it
    (D + "TestStd::test_s", "test", None, None),
    (D + "TestStd::test_gone", "test", "test gone", None),              # a stdlib base is not unresolved
    (D + "TestUnresolved::test_anything", "test", None, "unresolved base Thing"),
    (D + "TestCycleA::test_nothing", "test", "test gone", None),        # a cycle ends the walk
    ("tests/base.py::Chain5::test_deep", "test", None, None),           # five bases up is within the cap
    ("tests/base.py::Chain7::test_deep", "test", None, "base chain deeper than 5 at Chain2"),
    ("RouterIT::zeroRoutesExact", "java", None, None),
    ("RouterIT::zeroNeverRoutesExact", "java", "test gone", None),      # renamed away
    ("RouterIT::zeroRoutes", "java", "test gone", None),                # a word, not a prefix
    ("RouterIT::Exact", "java", "test gone", None),                     # nor a suffix: the leading boundary
    ("RouterIT::zeroRoutesExactly", "java", "test gone", None),         # a call site is not a declaration
    ("RouterIT::callSite", "java", "test gone", None),
    ("RouterIT::commented", "java", "test gone", None),
    ("RouterIT::quoted", "java", "test gone", None),
    ("RouterIT::helper", "java", None, None),
    ("GoneIT::zeroRoutesExact", "java", "file gone", None),
    ("Der::inheritedT", "java", None, None),                            # AbsBase via Mid<T extends Number>
    ("Der::multi", "java", None, None),                                 # a declaration spread over lines
    ("Der::missing", "java", "test gone", None),                        # AbsBase resolved, no such method
    ("Unres::gen", "java", None, None),                                 # a generic method
    ("Unres::missing", "java", None, "unresolved base ThirdPartyBase"),
    ("Impl::ifaceDefault", "java", None, None),                         # a default method of an implemented interface
    ("Both::inheritedT", "java", None, None),                           # extends and implements are both followed
    ("Both::ifaceDefault", "java", None, None),
    ("Impl::missing", "java", "test gone", None),
    ("Impl2::missing", "java", None, "unresolved base ThirdIface"),
    ("Impl3::missing", "java", "test gone", None),                      # Runnable and an imported java.io type
    ("ViaSub::ifaceDefault", "java", None, None),                       # an interface that extends the interface
    ("tests/test_imp.py::test_imported", "test", None, "unresolved binding import test_imported"),
    ("tests/test_imp.py::test_modalias", "test", None, "unresolved binding import test_modalias"),
    ("tests/test_imp.py::test_try", "test", None, "unresolved binding import test_try"),
    ("tests/test_imp.py::TestImp::test_cls_imp", "test", None, "unresolved binding import test_cls_imp"),
    ("tests/test_imp.py::test_other", "test", "test gone", None),
    ("tests/test_star.py::test_anything", "test", None, "unresolved binding star-import test_anything"),
    ("tests/test_star.py::TestHidden::test_x", "test", None, "unresolved binding star-import TestHidden"),
    ("tests/test_star.py::TestS::test_x", "test", "test gone", None),     # a class body has no star import
    ("tests/test_fac.py::TestFac::test_a", "test", None, "unresolved binding assignment TestFac"),
    ("tests/test_fac.py::TestFac2::test_a", "test", None, "unresolved binding assignment TestFac2"),
    ("tests/test_fac.py::TestReal::test_a", "test", "test gone", None),
    ("tests/test_loop.py::test_loop", "test", None, "unresolved binding loop test_loop"),
    ("tests/test_loop.py::test_while", "test", None, "unresolved binding loop test_while"),
    ("tests/test_loop.py::test_match", "test", None, "unresolved binding loop test_match"),
    ("tests/test_loop.py::TestLoopCls::test_x", "test", None, "unresolved binding loop TestLoopCls"),
    ("tests/test_loop.py::test_nowhere", "test", "test gone", None),
    ("tests/test_plat.py::TestPlat::test_win", "test", None, None),     # every definition of the class is checked
    ("tests/test_plat.py::TestPlat::test_posix", "test", None, None),
    ("tests/test_plat.py::TestPlat::test_neither", "test", "test gone", None),
])
def test_a_row_is_judged_by_following_the_definition_chain_at_the_rev(
        repo: tuple[Path, str], nodeid: str, kind: str, absent: str | None, present_reason: str | None) -> None:
    assert Inventory(repo[0]).judge(_row(nodeid, kind)) == (absent, present_reason)


def test_a_moved_test_stays_absent_at_its_old_nodeid_and_the_new_place_is_only_a_hint(
        repo: tuple[Path, str]) -> None:
    inv = Inventory(repo[0])
    row = _row("tests/test_old.py::test_moved")
    assert inv.judge(row) == ("file gone", None)
    assert inv.same_name_at_rev(row) == ["tests/test_new.py::test_moved"]
    assert inv.same_name_at_rev(_row("tests/test_old.py::TestK::test_nowhere")) == []
    # a class-qualified hit names its class; assignment definitions count
    assert inv.same_name_at_rev(_row("t.py::test_drain")) == ["tests/test_w.py::TestWorker::test_drain"]
    assert inv.same_name_at_rev(_row("t.py::test_dyn")) == ["tests/test_w.py::test_dyn"]
    java = inv.same_name_at_rev(_row("OldIT::zeroNeverRoutesExact", "java"))
    assert java == ["OldIT::zeroNeverRoutesExact_onTheWalk", "Other::zeroNeverRoutesExact"]


def test_a_bare_name_is_followed_the_same_way_and_scoped_to_its_class(repo: tuple[Path, str]) -> None:
    inv = Inventory(repo[0])
    assert inv.judge(_row("test_plain", path_source="none")) == (None, None)
    assert inv.judge(_row("test_dyn", path_source="none")) == (None, None)
    assert inv.judge(_row("TestWorker.test_drain[p]", path_source="none")) == (None, None)
    assert inv.judge(_row("TestWorker.test_plain", path_source="none")) == ("test gone", None)
    assert inv.judge(_row("test_drain", path_source="none")) == ("test gone", None)   # a method, not a module test
    assert inv.judge(_row("TestDerived.test_inherited", path_source="none")) == (None, None)
    assert inv.judge(_row("Outer.Inner.test_n", path_source="none")) == (None, None)
    assert inv.judge(_row("TestUnresolved.test_q", path_source="none")) == (None, "unresolved base Thing")
    assert inv.judge(_row("test_removed", path_source="none")) == ("test gone", None)
    assert inv.judge(_row("TestGone.test_drain", path_source="none")) == ("test gone", None)
    assert inv.judge(_row("TestFac.test_a", path_source="none")) == (None, "unresolved binding assignment TestFac")
    assert inv.judge(_row("TestPlat.test_posix", path_source="none")) == (None, None)


def test_the_rev_is_resolved_to_a_full_sha_and_decides_what_exists(repo: tuple[Path, str]) -> None:
    root, first = repo
    _git(root, "rm", "-q", "tests/test_w.py")
    _git(root, "commit", "-q", "-m", "test: drop")
    now, then = Inventory(root), Inventory(root, "HEAD~1")
    assert then.rev == first and len(now.rev) == 40 and now.rev != first
    row = _row("tests/test_w.py::test_plain")
    assert now.judge(row)[0] == "file gone" and then.judge(row)[0] is None


def test_a_repo_with_no_files_or_a_bad_repo_or_rev_is_refused(tmp_path: Path, repo: tuple[Path, str]) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    _git(root, "init", "-q", "-b", "develop")
    _git(root, "commit", "-q", "--allow-empty", "-m", "empty")
    with pytest.raises(RuntimeError, match="no files"):
        Inventory(root)
    with pytest.raises(RuntimeError):
        Inventory(tmp_path / "not-a-repo")
    for rev in ("nope", "-x", "--help"):                  # a rev that reads as an option is a rev, not a flag
        with pytest.raises(RuntimeError):
            Inventory(repo[0], rev)


def test_a_listed_path_that_cannot_be_read_raises_instead_of_reading_as_a_gone_test(repo: tuple[Path, str]) -> None:
    root, _ = repo
    _git(root, "update-index", "--add", "--cacheinfo", "160000," + "1" * 40 + ",tests/sub_mod.py")   # an object not here
    _git(root, "commit", "-q", "-m", "chore: a gitlink named like a test file")
    inv = Inventory(root)
    assert "tests/sub_mod.py" in inv.files
    with pytest.raises(RuntimeError, match="sub_mod.py"):
        inv.judge(_row("tests/sub_mod.py::test_x"))


def _an(rows: list[tuple[str, str, str]]) -> dict:
    """rows: (category, file, item) of python tests, one red run."""
    items = [{"kind": "test", "file": f, "item": i, "run": 1, "derived": False, "category": c,
              "rule": "r", "venue": "hosted", "path_source": "summary"} for c, f, i in rows]
    return {"runs": {1: {"created": "2026-10-01T00:00:00Z"}}, "items": items, "meta": {}}


def test_absent_rows_leave_the_active_lists_carry_a_hint_and_the_header_counts_both(
        repo: tuple[Path, str]) -> None:
    an = _an([(rrr.FLAKE, "tests/test_w.py", "test_plain"),
              (rrr.FLAKE, "tests/test_w.py", "test_removed"),
              (rrr.FLAKE, "tests/test_old.py", "test_moved"),
              (rrr.UNATTRIBUTED, "tests/test_w.py", "TestWorker::test_drain"),
              (rrr.UNATTRIBUTED, "tests/test_d.py", "TestUnresolved::test_q"),
              (rrr.UNATTRIBUTED, "tests/test_w.py", "TestGone::test_drain")])
    report = rrr.candidates_report(an, {"archive_sha256": "abc"}, Inventory(repo[0]))
    assert [r["nodeid"] for r in report["candidates"]] == ["tests/test_w.py::test_plain"]
    assert [r["nodeid"] for r in report["unattributed"]] == [
        "tests/test_d.py::TestUnresolved::test_q", "tests/test_w.py::TestWorker::test_drain"]
    by_id = {r["nodeid"]: r for r in report["unattributed"]}
    assert by_id["tests/test_d.py::TestUnresolved::test_q"]["present_reason"] == "unresolved base Thing"
    assert "present_reason" not in by_id["tests/test_w.py::TestWorker::test_drain"]
    assert all(r["present"] is True and "absent_reason" not in r for r in report["candidates"] + report["unattributed"])
    absent = report["absent"]
    assert [(r["nodeid"], r["present"], r["absent_reason"], r["same_name_at_rev"]) for r in absent["candidates"]] == [
        ("tests/test_old.py::test_moved", False, "file gone", ["tests/test_new.py::test_moved"]),
        ("tests/test_w.py::test_removed", False, "test gone", [])]
    assert [(r["nodeid"], r["absent_reason"]) for r in absent["unattributed"]] == [
        ("tests/test_w.py::TestGone::test_drain", "test gone")]
    h = report["header"]
    assert h["inventory_rev"] == repo[1] and len(h["inventory_rev"]) == 40
    assert h["inventory"] == {"candidates": {"present": 1, "absent": 2},
                              "unattributed": {"present": 2, "absent": 1}}
    assert (h["candidate_tests"], h["unattributed_tests"]) == (1, 2)             # the active lists
    assert h["path_sources"] == {"candidates": {"summary": 1}, "unattributed": {"summary": 2}}


def test_without_an_inventory_the_report_is_unchanged_and_has_no_absent_key() -> None:
    report = rrr.candidates_report(_an([(rrr.FLAKE, "tests/x.py", "test_a")]), {})
    assert set(report) == {"header", "candidates", "unattributed"}
    assert "present" not in report["candidates"][0] and "inventory_rev" not in report["header"]


def _cli(snapshot: Path, history: tuple[Path, dict[str, str]], tmp_path: Path, repo: Path, *extra: str,
         expect: int = rrr.EXIT_OK) -> Path:
    extraction_file, out = tmp_path / "x.jsonl.gz", tmp_path / "c.json.gz"
    rrr.write_extraction(_extract(snapshot, history[0]), extraction_file)
    assert rrr.main(["candidates", "--extraction", str(extraction_file), "--out", str(out),
                     "--repo", str(repo), *extra]) == expect
    return out


def test_cli_candidates_checks_the_inventory_at_head_or_at_a_named_rev_deterministically(
        snapshot: Path, history: tuple[Path, dict[str, str]], tmp_path: Path, inventory_repo: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    c1 = _git(inventory_repo, "rev-parse", "HEAD")
    _git(inventory_repo, "rm", "-q", "tests/test_r.py")
    _git(inventory_repo, "commit", "-q", "-m", "test: gone")
    head = _git(inventory_repo, "rev-parse", "HEAD")
    out = _cli(snapshot, history, tmp_path, inventory_repo)
    with gzip.open(out, "rt", encoding="utf-8") as fh:
        got = json.load(fh)
    assert got["header"]["inventory_rev"] == head
    assert [r["nodeid"] for r in got["candidates"]] == ["tests/test_t.py::test_t"]
    assert [(r["nodeid"], r["absent_reason"]) for r in got["absent"]["candidates"]] == [
        ("tests/test_r.py::test_r", "file gone")]
    assert "1 flake tests, 3 unattributed tests (absent at " + head[:12] in capsys.readouterr().out
    again = tmp_path / "again"
    again.mkdir()
    assert _cli(snapshot, history, again, inventory_repo).read_bytes() == out.read_bytes()
    older = tmp_path / "older"
    older.mkdir()
    old = _cli(snapshot, history, older, inventory_repo, "--rev", c1)
    with gzip.open(old, "rt", encoding="utf-8") as fh:
        assert json.load(fh)["header"]["inventory_rev"] == c1


def test_cli_candidates_refuses_when_more_than_half_the_rows_are_absent(
        snapshot: Path, history: tuple[Path, dict[str, str]], tmp_path: Path, inventory_repo: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    # 5 rows in all (2 flake, 3 unattributed). Two gone is 2 of 5: written. Three gone is 3 of 5: refused.
    _git(inventory_repo, "rm", "-q", "tests/test_r.py", "tests/test_b.py")
    _git(inventory_repo, "commit", "-q", "-m", "test: two gone")
    assert _cli(snapshot, history, tmp_path, inventory_repo).exists()
    capsys.readouterr()
    _git(inventory_repo, "rm", "-q", "tests/test_t.py")
    _git(inventory_repo, "commit", "-q", "-m", "test: a third gone")
    again = tmp_path / "again"
    again.mkdir()
    out = _cli(snapshot, history, again, inventory_repo, expect=rrr.EXIT_FAIL)
    err = capsys.readouterr().err
    assert not out.exists() and "3 of 5 rows are absent" in err and "--repo" in err


def test_cli_candidates_refuses_a_wrong_repo_and_a_bad_rev_without_a_traceback(
        snapshot: Path, history: tuple[Path, dict[str, str]], tmp_path: Path, inventory_repo: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    out = _cli(snapshot, history, tmp_path, history[0], expect=rrr.EXIT_FAIL)    # none of the named tests are there
    assert not out.exists() and "5 of 5 rows are absent" in capsys.readouterr().err
    bad = tmp_path / "bad"
    bad.mkdir()
    out = _cli(snapshot, history, bad, inventory_repo, "--rev", "no-such-rev", expect=rrr.EXIT_FAIL)
    err = capsys.readouterr().err
    assert not out.exists() and "no-such-rev" in err and "Traceback" not in err
    worse = tmp_path / "worse"
    worse.mkdir()
    _cli(snapshot, history, worse, tmp_path / "not-a-repo", expect=rrr.EXIT_FAIL)
    assert "Traceback" not in capsys.readouterr().err


def test_cli_candidates_refuses_an_extraction_with_no_rows_and_writes_nothing(
        snapshot: Path, history: tuple[Path, dict[str, str]], tmp_path: Path, inventory_repo: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    records = [r for r in _extract(snapshot, history[0]) if r.get("t") != "item"]
    assert records
    extraction_file, out = tmp_path / "x.jsonl.gz", tmp_path / "c.json"
    rrr.write_extraction(records, extraction_file)
    assert rrr.main(["candidates", "--extraction", str(extraction_file), "--out", str(out),
                     "--repo", str(inventory_repo)]) == rrr.EXIT_FAIL
    err = capsys.readouterr().err
    assert not out.exists() and "0 flake and 0 unattributed rows" in err and "Traceback" not in err


def _warn_of(snapshot: Path, history: tuple[Path, dict[str, str]], tmp_path: Path, repo: Path, *extra: str,
             capsys: pytest.CaptureFixture[str]) -> tuple[str, Path]:
    capsys.readouterr()
    out = _cli(snapshot, history, tmp_path, repo, *extra)
    return capsys.readouterr().err, out


def test_cli_candidates_warns_and_still_writes_when_the_rev_is_not_on_develop(
        snapshot: Path, history: tuple[Path, dict[str, str]], tmp_path: Path, inventory_repo: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    base = _git(inventory_repo, "rev-parse", "HEAD")
    for name in ("a", "b", "c", "d", "e"):
        (tmp_path / name).mkdir()
    # on develop with no origin: HEAD is develop, so no warning
    err, out = _warn_of(snapshot, history, tmp_path / "a", inventory_repo, capsys=capsys)
    assert "warning" not in err and out.exists()
    # origin/develop is behind HEAD: the rev is not its ancestor
    _commit(inventory_repo, "chore: ahead of origin", {"tests/test_extra.py": "x = 1\n"})
    _git(inventory_repo, "update-ref", "refs/remotes/origin/develop", base)
    err, out = _warn_of(snapshot, history, tmp_path / "b", inventory_repo, capsys=capsys)
    assert out.exists() and err.count("\n") == 1
    assert "warning: inventory rev " in err and "is not an ancestor of origin/develop" in err
    err, out = _warn_of(snapshot, history, tmp_path / "c", inventory_repo, "--rev", base, capsys=capsys)
    assert "warning" not in err and out.exists()
    # no origin: the check falls back to develop
    _git(inventory_repo, "update-ref", "-d", "refs/remotes/origin/develop")
    _git(inventory_repo, "switch", "-q", "-c", "side", base)
    side = _commit(inventory_repo, "chore: off develop", {"tests/test_side.py": "x = 1\n"})
    err, out = _warn_of(snapshot, history, tmp_path / "d", inventory_repo, "--rev", side, capsys=capsys)
    assert out.exists() and "is not an ancestor of develop" in err and "origin/develop" not in err
    # neither ref exists
    _git(inventory_repo, "branch", "-m", "develop", "trunk")
    err, out = _warn_of(snapshot, history, tmp_path / "e", inventory_repo, capsys=capsys)
    assert out.exists() and "neither origin/develop nor develop exists" in err
