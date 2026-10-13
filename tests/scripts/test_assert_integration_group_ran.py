# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-gx2l1: ``scripts/assert_integration_group_ran.py`` must not pass a group
that executed nothing.

Two defects, both found by the 7.67.0 evidence table: the checker reported
``EmbedParityTest`` as "ok 3 test(s)" when all three were skipped (no Voyage
key), and it expected ``RdrO8dil7GlobalManifestAntiJoinTest`` because that class
quotes the tag in javadoc. The report fixtures below mirror a real surefire
``TEST-*.xml`` (``<testcase>`` children, a ``<skipped message=.../>`` child on a
skipped one, ``tests``/``skipped`` attributes on the suite).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent.parent
_SCRIPT = _REPO / "scripts" / "assert_integration_group_ran.py"

spec = importlib.util.spec_from_file_location("assert_integration_group_ran", _SCRIPT)
_mod = importlib.util.module_from_spec(spec)
sys.modules["assert_integration_group_ran"] = _mod
spec.loader.exec_module(_mod)


# ---------------------------------------------------------------- stripper


@pytest.mark.parametrize(
    "src, tagged",
    [
        # a real annotation on the class
        ('package a;\n@Tag("integration")\nclass T {}\n', True),
        # a real annotation on a method only
        ('package a;\nclass T {\n  @Test\n  @Tag("integration")\n  void m() {}\n}\n', True),
        # qualified, with value=
        ('package a;\n@org.junit.jupiter.api.Tag(value = "integration")\nclass T {}\n', True),
        # the same text in a line comment
        ('package a;\n// @Tag("integration")\nclass T {}\n', False),
        # block comment
        ('package a;\n/* @Tag("integration") */\nclass T {}\n', False),
        # javadoc, the RdrO8dil7 shape
        (
            'package a;\n/**\n * excludes {@code @Tag("integration")} from mvn test\n */\nclass T {}\n',
            False,
        ),
        # inside a string literal
        ('package a;\nclass T { String s = "@Tag(\\"integration\\")"; }\n', False),
        ('package a;\nclass T { String s = "x @Tag(\\"integration\\") y"; }\n', False),
        # inside a text block
        ('package a;\nclass T { String s = """\n  @Tag("integration")\n  """; }\n', False),
        # a different tag value is not the integration tag
        ('package a;\n@Tag("slow")\nclass T {}\n', False),
        # a comment opener inside a string must not swallow the real annotation after it
        ('package a;\nclass T { String u = "http://x/*"; }\n@Tag("integration")\nclass U {}\n', True),
        # a char literal holding a quote must not open a string
        ("package a;\nclass T { char q = '\"'; }\n@Tag(\"integration\")\nclass U {}\n", True),
    ],
    ids=[
        "class-annotation",
        "method-annotation",
        "qualified-value",
        "line-comment",
        "block-comment",
        "javadoc",
        "string-escaped",
        "string-embedded",
        "text-block",
        "other-tag",
        "slash-star-in-string",
        "quote-char-literal",
    ],
)
def test_only_a_real_annotation_counts(src, tagged):
    assert _mod.is_integration_tagged(src) is tagged


def test_tagged_classes_derives_fqcn_from_code_not_comments(tmp_path):
    root = tmp_path / "src" / "test" / "java" / "a" / "b"
    root.mkdir(parents=True)
    (root / "Real.java").write_text('package a.b;\n@Tag("integration")\nclass Real {}\n')
    (root / "Doc.java").write_text(
        'package a.b;\n/** runs under {@code @Tag("integration")} elsewhere */\nclass Doc {}\n'
    )
    assert _mod.tagged_classes(str(tmp_path / "src" / "test" / "java")) == ["a.b.Real"]


def test_the_real_tree_expects_what_it_should():
    """The checked-in tree: javadoc-only RdrO8dil7 is out, every allowlist entry is
    a real tagged class, and the set is not vacuously small."""
    classes = _mod.tagged_classes(str(_REPO / "service" / "src" / "test" / "java"))
    assert "dev.nexus.service.RdrO8dil7GlobalManifestAntiJoinTest" not in classes
    assert len(classes) >= 10
    assert set(_mod.ALLOWED_SKIPS) <= set(classes)
    assert all(reason.strip() for reason in _mod.ALLOWED_SKIPS.values())


# ---------------------------------------------------------------- main()


def _service(tmp_path: Path, classes: list[str]) -> Path:
    svc = tmp_path / "service"
    for fqcn in classes:
        pkg, _, name = fqcn.rpartition(".")
        d = svc / "src" / "test" / "java" / pkg.replace(".", "/")
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.java").write_text(f'package {pkg};\n@Tag("integration")\nclass {name} {{}}\n')
    (svc / "target" / "surefire-reports").mkdir(parents=True)
    return svc


def _report(svc: Path, fqcn: str, ran: int, skipped: int) -> None:
    cases = "".join(f'<testcase name="r{i}" classname="{fqcn}" time="0.1"/>' for i in range(ran))
    cases += "".join(
        f'<testcase name="s{i}" classname="{fqcn}" time="0.0"><skipped message="no key"/></testcase>'
        for i in range(skipped)
    )
    (svc / "target" / "surefire-reports" / f"TEST-{fqcn}.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuite name="{fqcn}" tests="{ran + skipped}" errors="0" skipped="{skipped}" '
        f'failures="0">{cases}</testsuite>\n'
    )


def test_all_executed_passes(tmp_path, capsys):
    svc = _service(tmp_path, ["a.A", "a.B"])
    _report(svc, "a.A", 3, 0)
    _report(svc, "a.B", 1, 0)
    assert _mod.main([str(svc)], allowed={}) == 0
    assert "ok a.A: 3 test(s) executed" in capsys.readouterr().out


def test_class_with_every_test_skipped_fails(tmp_path, capsys):
    """The EmbedParityTest shape: tests=3 skipped=3 used to print 'ok 3 test(s)'."""
    svc = _service(tmp_path, ["a.A", "a.EmbedLike"])
    _report(svc, "a.A", 2, 0)
    _report(svc, "a.EmbedLike", 0, 3)
    assert _mod.main([str(svc)], allowed={}) == 1
    err = capsys.readouterr().err
    assert "a.EmbedLike: all 3 test(s) SKIPPED" in err
    assert "a.A" not in err


def test_partial_skip_still_counts_the_executed_tests(tmp_path, capsys):
    svc = _service(tmp_path, ["a.A"])
    _report(svc, "a.A", 2, 1)
    assert _mod.main([str(svc)], allowed={}) == 0
    assert "2 test(s) executed, 1 skipped" in capsys.readouterr().out


def test_suite_attributes_are_the_fallback_when_no_testcases_are_listed(tmp_path):
    svc = _service(tmp_path, ["a.A"])
    (svc / "target" / "surefire-reports" / "TEST-a.A.xml").write_text(
        '<testsuite name="a.A" tests="3" skipped="3" errors="0" failures="0"/>'
    )
    assert _mod.main([str(svc)], allowed={}) == 1


def test_allowlisted_skip_passes_and_prints_its_reason(tmp_path, capsys):
    svc = _service(tmp_path, ["a.A", "a.NeedsKey"])
    _report(svc, "a.A", 2, 0)
    _report(svc, "a.NeedsKey", 0, 3)
    allowed = {"a.NeedsKey": "needs VOYAGE_API_KEY; CI has none"}
    assert _mod.main([str(svc)], allowed=allowed) == 0
    out = capsys.readouterr().out
    assert "allowed-skip a.NeedsKey" in out and "needs VOYAGE_API_KEY; CI has none" in out


def test_allowlisted_class_with_no_report_passes(tmp_path, capsys):
    """A class-level gate (@EnabledIfSystemProperty) may leave no report at all."""
    svc = _service(tmp_path, ["a.A", "a.Gated"])
    _report(svc, "a.A", 1, 0)
    assert _mod.main([str(svc)], allowed={"a.Gated": "property unset in CI"}) == 0
    assert "no report" in capsys.readouterr().out


def test_allowlisted_class_that_does_execute_is_reported_executed(tmp_path, capsys):
    svc = _service(tmp_path, ["a.A"])
    _report(svc, "a.A", 2, 0)
    assert _mod.main([str(svc)], allowed={"a.A": "reason"}) == 0
    out = capsys.readouterr().out
    assert "ok a.A: 2 test(s) executed" in out and "allowed-skip" not in out


def test_stale_allowlist_entry_fails(tmp_path, capsys):
    svc = _service(tmp_path, ["a.A"])
    _report(svc, "a.A", 1, 0)
    assert _mod.main([str(svc)], allowed={"a.Gone": "reason"}) == 1
    assert "a.Gone: STALE ALLOWLIST ENTRY" in capsys.readouterr().err


def test_javadoc_only_class_is_not_expected(tmp_path):
    """RdrO8dil7: mentions the tag in javadoc, has no report in the integration run."""
    svc = _service(tmp_path, ["a.A"])
    d = svc / "src" / "test" / "java" / "a"
    (d / "Doc.java").write_text(
        'package a;\n/** excludes {@code @Tag("integration")} from mvn test */\nclass Doc {}\n'
    )
    _report(svc, "a.A", 1, 0)
    assert _mod.main([str(svc)], allowed={}) == 0


def test_missing_report_and_zero_tests_fail(tmp_path, capsys):
    svc = _service(tmp_path, ["a.A", "a.B"])
    _report(svc, "a.B", 0, 0)
    assert _mod.main([str(svc)], allowed={}) == 1
    err = capsys.readouterr().err
    assert "a.A: NO REPORT" in err and "a.B: 0 tests" in err


def test_zero_expected_classes_is_a_failure_not_a_pass(tmp_path, capsys):
    """Non-vacuity: a tree with no real annotation (or only javadoc mentions)."""
    svc = tmp_path / "service"
    d = svc / "src" / "test" / "java" / "a"
    d.mkdir(parents=True)
    (d / "Doc.java").write_text('package a;\n/** @Tag("integration") */\nclass Doc {}\n')
    (svc / "target" / "surefire-reports").mkdir(parents=True)
    assert _mod.main([str(svc)], allowed={}) == 1
    assert "no class carries a real" in capsys.readouterr().err
