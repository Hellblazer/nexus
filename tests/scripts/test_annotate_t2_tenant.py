# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-j23ni: ``scripts/annotate_t2_tenant.py`` turns a storage-touch census into
``pytest.mark.t2_tenant`` marks.

The cases build their own tiny test trees and census records, so they pin the
rule (what needs a tenant, where the mark goes, that a rerun adds nothing) and
not today's corpus. The last case collects the marked file with real pytest, to
show the marks land on the tests the census named and on no others.
"""
from __future__ import annotations

import json
import pathlib

import pytest

import annotate_t2_tenant as ann


def _rec(nodeid: str, *, outcome: str = "passed", touched: bool = False, provisioned: bool = False,
         exempt: bool = False, peek: bool = False) -> dict:
    return {
        "k": "test", "nodeid": nodeid, "outcome": outcome, "provisioned": provisioned, "exempt": exempt,
        "stale": False, "unprov_events": 1 if touched else 0, "events": 1 if touched else 0,
        "soft_peeks": [{"kind": "discover_lease"}] if peek else [],
    }


def _census(records: list[dict], **kw) -> ann.Census:
    return ann.classify(records, **kw)


def _write(root: pathlib.Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


# ── what needs a tenant ──────────────────────────────────────────────────────


def test_classification_follows_the_evidence() -> None:
    c = _census([
        _rec("t/a.py::test_touched", touched=True),
        _rec("t/a.py::test_failed_without_a_trace", outcome="failed"),
        _rec("t/a.py::test_clean"),
        _rec("t/a.py::test_peeks", peek=True),
        _rec("t/a.py::test_provisioned", provisioned=True),
        _rec("t/a.py::test_exempt", touched=True, exempt=True),
        _rec("t/a.py::test_skipped", outcome="skipped"),
        _rec("t/a.py::test_xfailed_after_a_touch", outcome="xfailed", touched=True),
        _rec("t/a.py::test_skipped_after_a_touch", outcome="skipped", touched=True),
        _rec("t/a.py::test_dirty", touched=True, provisioned=True),
        {"k": "late_thread", "nodeid": "t/a.py::test_leaker", "n": 1},
    ])
    assert c.need == {
        "t/a.py::test_touched", "t/a.py::test_failed_without_a_trace",
        "t/a.py::test_xfailed_after_a_touch", "t/a.py::test_skipped_after_a_touch",
    }
    assert c.failed_blind == ["t/a.py::test_failed_without_a_trace"]
    assert c.peek == {"t/a.py::test_peeks"}
    assert c.mint["t/a.py"] == {
        "t/a.py::test_touched", "t/a.py::test_failed_without_a_trace", "t/a.py::test_clean", "t/a.py::test_peeks",
        "t/a.py::test_xfailed_after_a_touch", "t/a.py::test_skipped_after_a_touch",
    }
    assert c.provisioned_dirty == ["t/a.py::test_dirty"] and c.late_owners == ["t/a.py::test_leaker"]
    assert c.total == 10
    assert _census([_rec("t/a.py::test_peeks", peek=True)], include_peeks=True).need == {"t/a.py::test_peeks"}


def test_a_directory_with_two_runs_must_say_which(tmp_path: pathlib.Path) -> None:
    for run in ("aaaa1111", "bbbb2222"):
        (tmp_path / f"nx-storage-touch-tests-{run}-1-x.jsonl").write_text(json.dumps(_rec(f"t/{run}.py::test_x")) + "\n")
    with pytest.raises(SystemExit, match="2 runs"):
        ann.load_records(tmp_path)
    assert [r["nodeid"] for r in ann.load_records(tmp_path, "bbbb2222")] == ["t/bbbb2222.py::test_x"]
    with pytest.raises(SystemExit, match="no nx-storage-touch-tests"):
        ann.load_records(tmp_path / "empty")


def test_a_line_cut_short_by_a_kill_is_skipped(tmp_path: pathlib.Path) -> None:
    (tmp_path / "nx-storage-touch-tests-aaaa1111-1-x.jsonl").write_text(
        json.dumps(_rec("t/a.py::test_x")) + "\n" + '{"k": "test", "nodeid": "t/a.py::test_y", "outc'
    )
    assert [r["nodeid"] for r in ann.load_records(tmp_path)] == ["t/a.py::test_x"]


# ── where the mark goes ──────────────────────────────────────────────────────

_MODULE = '''"""Docstring."""
from __future__ import annotations

import os

import pytest


def helper():
    return 1


def test_a():
    pass


def test_b():
    pass


@pytest.mark.parametrize("x", [1, 2])
def test_p(x):
    pass


class TestAll:
    def test_one(self):
        pass

    def test_two(self):
        pass


class TestSome:
    def test_needs(self):
        pass

    def test_does_not(self):
        pass
'''


def _plan(tmp_path: pathlib.Path, records: list[dict], source: str = _MODULE, **kw):
    _write(tmp_path, "t/test_m.py", source)
    census = _census(records)
    plan = ann.plan_file("t/test_m.py", census, tmp_path, threshold=kw.pop("threshold", 0.9))
    return census, plan


def _all(touched: set[str], names=("test_a", "test_b", "test_p[1]", "test_p[2]", "TestAll::test_one",
                                   "TestAll::test_two", "TestSome::test_needs", "TestSome::test_does_not")):
    return [_rec(f"t/test_m.py::{n}", touched=n in touched) for n in names]


def test_a_module_most_of_whose_tests_need_a_tenant_gets_one_module_mark(tmp_path: pathlib.Path) -> None:
    touched = {n.split("t/test_m.py::")[1] for n in ()} | {
        "test_a", "test_b", "test_p[1]", "test_p[2]", "TestAll::test_one", "TestAll::test_two", "TestSome::test_needs",
    }
    _, plan = _plan(tmp_path, _all(touched), threshold=0.8)
    assert plan.module_mark and not plan.class_marks and not plan.func_marks
    assert (plan.covered_need, plan.covered_extra) == (7, 1)
    assert ann.apply_plan(plan, tmp_path) is None
    text = (tmp_path / "t/test_m.py").read_text()
    # after the import block, with pytest already imported, before the first definition
    assert "import pytest\n\npytestmark = pytest.mark.t2_tenant\n\n\ndef helper" in text


def test_below_the_threshold_a_class_whose_every_test_needs_one_is_marked_once(tmp_path: pathlib.Path) -> None:
    _, plan = _plan(tmp_path, _all({"test_a", "TestAll::test_one", "TestAll::test_two", "test_p[1]", "TestSome::test_needs"}))
    assert not plan.module_mark
    assert plan.class_marks == [("TestAll",)]
    assert sorted(plan.func_marks) == [("TestSome", "test_needs"), ("test_a",), ("test_p",)]
    assert ann.apply_plan(plan, tmp_path) is None
    text = (tmp_path / "t/test_m.py").read_text()
    assert "@pytest.mark.t2_tenant\nclass TestAll:" in text
    assert "@pytest.mark.t2_tenant\ndef test_a():" in text
    # above the parametrize decorator, not between it and the def
    assert '@pytest.mark.t2_tenant\n@pytest.mark.parametrize("x", [1, 2])\ndef test_p' in text
    assert "    @pytest.mark.t2_tenant\n    def test_needs(self):" in text
    assert "test_b():" in text and "@pytest.mark.t2_tenant\ndef test_b" not in text
    assert "test_does_not" in text and "@pytest.mark.t2_tenant\n    def test_does_not" not in text


def test_a_rerun_adds_nothing(tmp_path: pathlib.Path) -> None:
    records = _all({"test_a", "TestAll::test_one", "TestAll::test_two", "test_p[1]", "TestSome::test_needs"})
    _, plan = _plan(tmp_path, records)
    assert ann.apply_plan(plan, tmp_path) is None
    once = (tmp_path / "t/test_m.py").read_text()
    census = _census(records)  # the same census against the already-marked tree
    again = ann.plan_file("t/test_m.py", census, tmp_path, threshold=0.9)
    assert not (again.module_mark or again.class_marks or again.func_marks) and again.already == 5
    assert (tmp_path / "t/test_m.py").read_text() == once
    # and a module already marked is skipped whole
    _write(tmp_path, "t/test_whole.py", "import pytest\npytestmark = pytest.mark.t2_tenant\n\ndef test_x():\n    pass\n")
    whole = ann.plan_file("t/test_whole.py", _census([_rec("t/test_whole.py::test_x", touched=True)]), tmp_path, threshold=0.9)
    assert not whole.module_mark and whole.already == 1


def test_a_test_that_asks_by_parameter_but_was_seen_unprovisioned_still_gets_the_mark(tmp_path: pathlib.Path) -> None:
    """``def test_x(db, t2_service_env)``: ``db`` touches storage before the tenant
    exists, so the parameter is not enough; the mark makes the autouse pin run first."""
    source = "def test_x(db, t2_service_env):\n    pass\n\ndef test_y():\n    pass\n"
    _write(tmp_path, "t/test_m.py", source)
    census = _census([_rec("t/test_m.py::test_x", touched=True), _rec("t/test_m.py::test_y")])
    plan = ann.plan_file("t/test_m.py", census, tmp_path, threshold=0.9)
    assert plan.func_marks == [("test_x",)]
    assert ann.apply_plan(plan, tmp_path) is None
    text = (tmp_path / "t/test_m.py").read_text()
    assert text.startswith("import pytest\n\n@pytest.mark.t2_tenant\ndef test_x(db, t2_service_env):")


@pytest.mark.parametrize("existing, expected", [
    ("pytestmark = pytest.mark.integration\n", "pytestmark = [pytest.mark.integration, pytest.mark.t2_tenant]"),
    ("pytestmark = [pytest.mark.integration, pytest.mark.slow]\n",
     "pytestmark = [pytest.mark.integration, pytest.mark.slow]\npytestmark = [*pytestmark, pytest.mark.t2_tenant]"),
    ("pytestmark = (\n    pytest.mark.skipif(\n        True,\n        reason='x',\n    )\n)\n", None),
])
def test_an_existing_pytestmark_is_extended_not_replaced(tmp_path: pathlib.Path, existing: str, expected: str | None) -> None:
    source = f"import pytest\n\n{existing}\n\ndef test_a():\n    pass\n"
    _write(tmp_path, "t/test_m.py", source)
    plan = ann.plan_file("t/test_m.py", _census([_rec("t/test_m.py::test_a", touched=True)]), tmp_path, threshold=0.9)
    assert plan.module_mark
    assert ann.apply_plan(plan, tmp_path) is None
    text = (tmp_path / "t/test_m.py").read_text()
    compile(text, "t/test_m.py", "exec")
    if expected:
        assert expected in text
    else:
        assert "pytest.mark.t2_tenant" in text and "reason='x'" in text


def test_pytest_is_imported_when_the_module_lacks_it(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "t/test_m.py", '"""Doc."""\nimport os\n\n\ndef test_a():\n    pass\n')
    plan = ann.plan_file("t/test_m.py", _census([_rec("t/test_m.py::test_a", touched=True)]), tmp_path, threshold=0.9)
    assert ann.apply_plan(plan, tmp_path) is None
    text = (tmp_path / "t/test_m.py").read_text()
    assert '"""Doc."""\nimport os\nimport pytest\n\npytestmark = pytest.mark.t2_tenant\n\n\ndef test_a' in text


def test_pytestmark_is_placed_after_a_late_pytest_import(tmp_path: pathlib.Path) -> None:
    """A mark above ``import pytest`` would be a NameError at import."""
    _write(tmp_path, "t/test_m.py", "import sys\nsys.path.insert(0, '.')\nimport pytest\n\n\ndef test_a():\n    pass\n")
    plan = ann.plan_file("t/test_m.py", _census([_rec("t/test_m.py::test_a", touched=True)]), tmp_path, threshold=0.9)
    assert ann.apply_plan(plan, tmp_path) is None
    text = (tmp_path / "t/test_m.py").read_text()
    assert text.index("import pytest") < text.index("pytestmark = pytest.mark.t2_tenant")
    compile(text, "t/test_m.py", "exec")


def test_an_inherited_test_marks_the_class_that_collects_it(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "t/test_m.py", (
        "import pytest\n\n\nclass Base:\n    def test_shared(self):\n        pass\n\n    def test_other(self):\n        pass\n\n\n"
        "class TestA(Base):\n    pass\n\n\nclass TestB(Base):\n    pass\n"
    ))
    census = _census([
        _rec("t/test_m.py::TestA::test_shared", touched=True), _rec("t/test_m.py::TestA::test_other"),
        _rec("t/test_m.py::TestB::test_shared"), _rec("t/test_m.py::TestB::test_other"),
    ])
    plan = ann.plan_file("t/test_m.py", census, tmp_path, threshold=0.9)
    assert plan.class_marks == [("TestA",)] and not plan.module_mark and not plan.unresolved


def test_a_test_that_cannot_be_found_is_reported_and_the_exit_code_says_so(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "t/test_m.py", "def test_a():\n    pass\n\ndef test_b():\n    pass\n")
    (tmp_path / "census").mkdir()
    records = [_rec("t/test_m.py::test_a"), _rec("t/test_m.py::test_b"), _rec("t/test_m.py::test_generated", touched=True)]
    (tmp_path / "census/nx-storage-touch-tests-aaaa1111-1-x.jsonl").write_text("\n".join(json.dumps(r) for r in records))
    assert ann.main([str(tmp_path / "census"), "--repo", str(tmp_path)]) == 1
    # a missing file is the same kind of failure
    (tmp_path / "census/nx-storage-touch-tests-aaaa1111-2-y.jsonl").write_text(json.dumps(_rec("t/gone.py::test_x", touched=True)))
    assert ann.main([str(tmp_path / "census"), "--repo", str(tmp_path)]) == 1


def test_the_default_is_a_dry_run(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(tmp_path, "t/test_m.py", "def test_a():\n    pass\n")
    (tmp_path / "census").mkdir()
    (tmp_path / "census/nx-storage-touch-tests-aaaa1111-1-x.jsonl").write_text(json.dumps(_rec("t/test_m.py::test_a", touched=True)))
    assert ann.main([str(tmp_path / "census"), "--repo", str(tmp_path)]) == 0
    assert (tmp_path / "t/test_m.py").read_text() == "def test_a():\n    pass\n"
    out = capsys.readouterr().out
    assert "module_marks: 1" in out and "need_a_tenant: 1" in out
    assert ann.main([str(tmp_path / "census"), "--repo", str(tmp_path), "--apply"]) == 0
    assert "pytest.mark.t2_tenant" in (tmp_path / "t/test_m.py").read_text()


def test_the_marks_land_on_the_tests_the_census_named(pytester: pytest.Pytester) -> None:
    """End to end with real pytest: collect by the mark and compare with the census."""
    pytester.makeini("[pytest]\nmarkers =\n    t2_tenant: x\n    integration: x\n")
    pytester.makepyfile(test_m=_MODULE + '''

class TestInherits(TestSome):
    pass
''')
    touched = {"test_a", "test_p[1]", "test_p[2]", "TestAll::test_one", "TestAll::test_two", "TestSome::test_needs"}
    names = [
        "test_a", "test_b", "test_p[1]", "test_p[2]", "TestAll::test_one", "TestAll::test_two",
        "TestSome::test_needs", "TestSome::test_does_not", "TestInherits::test_needs", "TestInherits::test_does_not",
    ]
    records = [_rec(f"test_m.py::{n}", touched=n in touched) for n in names]
    census = _census(records)
    plan = ann.plan_file("test_m.py", census, pytester.path, threshold=0.9)
    assert ann.apply_plan(plan, pytester.path) is None
    result = pytester.runpytest("--collect-only", "-q", "-m", "t2_tenant")
    collected = {line for line in result.outlines if "::" in line}
    expected = {f"test_m.py::{n}" for n in touched}
    assert expected <= collected
    # the marks may cover a neighbour (a class mark covers its whole class) but never a clean module's tests
    assert "test_m.py::TestSome::test_does_not" not in collected
    assert "test_m.py::test_b" not in collected
    assert "test_m.py::TestInherits::test_does_not" not in collected
