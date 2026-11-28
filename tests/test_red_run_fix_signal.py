# SPDX-License-Identifier: AGPL-3.0-or-later
"""The red-run report's fix pick skips docstring-only and self-declaring commits (nexus-2jomg.1).

The Phase 0 step 3 hand check found two of nine readings tied to a commit that could not have fixed the red:
one changed a single docstring line of the test, the other only declared its own new file in an allowlist.
These tests build a small git history and run ``_fix_signal`` over it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests._red_run_fixtures import _git

import red_run_fix_signal as fs
import red_run_report as rrr

PY_TEST = "tests/test_x.py"
JAVA_TEST = "service/src/test/java/dev/RawGateTest.java"

BASE_PY = '''"""Checks the thing.

(``old_name``) always goes through the engine
and a last line.
"""

ALLOW = {
    "a.py": 1,
}


def test_x():
    assert compute() == 1  # trailing comment
'''

BASE_JAVA = '''class RawGateTest {
    static final Map<String, Object> ALLOWED = Map.ofEntries(
        Map.entry("Old.java", Map.of(
            "run", 1)),
        Map.entry("Zed.java", Map.of(
            "run", 1)));
}
'''


def _commit_files(repo: Path, subject: str, files: dict[str, str]) -> str:
    """Write each file whole, commit, return the sha."""
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", *files)
    _git(repo, "commit", "-q", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "develop")
    base = _commit_files(r, "chore: base", {PY_TEST: BASE_PY, JAVA_TEST: BASE_JAVA})
    return r, base


def _pick(repo: Path, base: str, item: dict) -> dict:
    hist = rrr.History(repo)
    rng = hist.range(base, "HEAD")
    hist.load(rng)
    it = dict(item)
    rrr._fix_signal(it, rng, {"head_sha": "green"}, hist)
    return it


def _docstring_edit(repo: Path) -> str:
    text = (repo / PY_TEST).read_text().replace("(``old_name``)", "(``new_name``)")
    return _commit_files(repo, "refactor: rename in prose", {PY_TEST: text})


def _self_declaring_edit(repo: Path) -> str:
    text = (repo / PY_TEST).read_text().replace('    "a.py": 1,\n', '    "a.py": 1,\n    "newmod.py": 2,\n')
    return _commit_files(repo, "feat: a new module", {PY_TEST: text, "src/nexus/newmod.py": "X = 1\n"})


def _real_edit(repo: Path) -> str:
    text = (repo / PY_TEST).read_text().replace("== 1  # trailing", "== 2  # trailing")
    return _commit_files(repo, "fix: the expected value", {PY_TEST: text})


def test_a_docstring_only_commit_is_not_the_fix_and_the_next_qualifying_commit_is(repo: tuple[Path, str]) -> None:
    r, base = repo
    _docstring_edit(r)
    real = _real_edit(r)
    it = _pick(r, base, {"kind": "test", "file": PY_TEST})
    assert (it["fix_commit"], it["fix_distance"], it["fix_pick"]) == (real[:12], 2, "doc_only_skipped")
    assert it["fix_is_fix"] is True


def test_a_commit_that_only_declares_its_own_new_file_is_not_the_fix(repo: tuple[Path, str]) -> None:
    r, base = repo
    _self_declaring_edit(r)
    real = _real_edit(r)
    it = _pick(r, base, {"kind": "test", "file": PY_TEST})
    assert (it["fix_commit"], it["fix_distance"], it["fix_pick"]) == (real[:12], 2, "qualified")


def test_a_java_entry_whose_continuation_lines_do_not_name_the_file_is_still_self_declaring(
        repo: tuple[Path, str]) -> None:
    r, base = repo
    text = (r / JAVA_TEST).read_text().replace(
        '        Map.entry("Zed.java"',
        '        Map.entry("Admin.java", Map.of(\n'
        '            // SANCTIONED RAW: no typed form.\n'
        '            "runDdl", Map.of(".execute(sql)", 1))),\n'
        '        Map.entry("Zed.java"')
    _commit_files(r, "feat: a sweep", {JAVA_TEST: text, "service/src/main/java/dev/Admin.java": "class Admin {}\n"})
    real = _commit_files(r, "fix: typed DSL", {JAVA_TEST: text.replace('"runDdl"', '"runDdl2"')})
    it = _pick(r, base, {"kind": "java", "file": "RawGateTest"})
    assert (it["fix_commit"], it["fix_distance"], it["fix_pick"]) == (real[:12], 2, "qualified")


def test_an_entry_for_a_file_the_commit_does_not_add_is_a_real_change(repo: tuple[Path, str]) -> None:
    r, base = repo
    text = (r / PY_TEST).read_text().replace('    "a.py": 1,\n', '    "a.py": 1,\n    "other.py": 2,\n')
    sha = _commit_files(r, "fix: allow other", {PY_TEST: text})
    it = _pick(r, base, {"kind": "test", "file": PY_TEST})
    assert (it["fix_commit"], it["fix_pick"], it["fix_table_only"]) == (sha[:12], "qualified", True)


def test_when_no_commit_qualifies_the_first_touching_commit_is_kept_and_recorded(repo: tuple[Path, str]) -> None:
    r, base = repo
    first = _docstring_edit(r)
    _self_declaring_edit(r)
    it = _pick(r, base, {"kind": "test", "file": PY_TEST})
    assert (it["fix_commit"], it["fix_distance"], it["fix_pick"]) == (first[:12], 1, "fallback")


def test_a_run_with_no_touching_commit_has_no_pick(repo: tuple[Path, str]) -> None:
    r, base = repo
    _commit_files(r, "chore: elsewhere", {"README.md": "x\n"})
    it = _pick(r, base, {"kind": "test", "file": PY_TEST})
    assert (it["fix_commit"], it["fix_distance"], it["fix_pick"], it["fix_table_only"]) == (None, None, None, None)


def _prose_edit(repo: Path, extra_row: bool = False) -> str:
    """Edits the docstring line ``and a last line.``, which is no table line on its own, and optionally adds a row."""
    text = (repo / PY_TEST).read_text().replace("and a last line.", "and the last line")
    if extra_row:
        text = text.replace('    "a.py": 1,\n', '    "a.py": 1,\n    "b.py": 2,\n')
    return _commit_files(repo, "fix: prose" + (" and entry" if extra_row else ""), {PY_TEST: text})


def test_a_docstring_line_is_not_a_table_edit(repo: tuple[Path, str]) -> None:
    r, base = repo
    doc = _prose_edit(r)
    hist = rrr.History(r)
    assert hist.table_only(doc, PY_TEST) is None          # read as code, ``and the last line`` is not a row
    real = _real_edit(r)           # an assertion line is logic, whatever else changed
    assert hist.table_only(real, PY_TEST) is False


def test_a_string_holding_a_line_of_code_is_not_a_table_entry() -> None:
    # 74e6ab64dd added this probe line inside a subprocess script string, beside a docstring
    assert not rrr.is_table_line('        "import nexus, nexus.hooks\\n"')
    for entry in ('        "src/nexus/x.py",', '    "newmod.py": 2,', "    'b',", '        "nexus-exact",',
                  '        ("tests/e2e/x.sh", \'tr "\\\\n" " "\'),',    # a tuple entry that merely quotes a newline escape
                  # the vw594 pin-table row shapes (b672f8e29d11): a \n string that ends in a comma is a row
                  '     "            _stamp_last()\\n            _hook(md_path, base_path=base_path)\\n",',
                  '     "            _stamp_last()\\n            _hook(md_path, base_path=base_path)\\n"),'):
        assert rrr.is_table_line(entry), entry
    for code in ('        "import nexus, nexus.hooks\\n"', '        "import os, signal, subprocess, sys, time\\n"'):
        assert not rrr.is_table_line(code), code


def test_a_docstring_line_beside_a_table_entry_does_not_change_the_reading(repo: tuple[Path, str]) -> None:
    r, _ = repo
    sha = _prose_edit(r, extra_row=True)
    assert rrr.History(r).table_only(sha, PY_TEST) is True    # False if the docstring line counted as code


@pytest.mark.parametrize(("source", "want"), [
    ('"""Doc\nover\nlines."""\nx = 1\n', {1, 2, 3}),
    ('# lead\nx = 1  # trailing\ndef f():\n    """one."""\n    return 1\n', {1, 4}),
    ('x = 1\ny = """not a\nstatement"""\n', set()),
    ('def broken(:\n# comment\n', {2}),
    ('SCRIPT = """\n#!/bin/sh\necho hi\n"""\n# real comment\n', {5}),      # a # line inside a string is no comment
])
def test_python_doc_lines(source: str, want: set[int]) -> None:
    assert fs.python_doc_lines(source) == want


def test_java_doc_lines_see_line_block_and_javadoc_comments_but_not_code_or_strings() -> None:
    source = (
        "/** Javadoc\n"        # 1
        " * continues. */\n"   # 2
        "class A {\n"          # 3
        "    // note\n"        # 4
        '    String s = "// not a comment";\n'    # 5
        "    int x; /* inline */ int y;\n"        # 6
        '    String t = """\n'                    # 7
        "        // inside a text block\n"        # 8
        '        """;\n'                          # 9
        "}\n")
    assert fs.java_doc_lines(source) == {1, 2, 4}


def test_parse_hunks_numbers_removed_and_added_lines() -> None:
    diff = ("diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -3,2 +3 @@\n-old one\n-old two\n+new\n"
            "@@ -9,0 +9,2 @@\n+add a\n+add b\n\\ No newline at end of file\n")
    assert fs.parse_hunks(diff) == [([(3, "old one"), (4, "old two")], [(3, "new")]),
                                    ([], [(9, "add a"), (10, "add b")])]


# --- fix-typed src commits, # in strings, row shapes ---------------------------------------------------------


def _comment_edit(repo: Path, subject: str, extra: dict[str, str]) -> str:
    text = (repo / PY_TEST).read_text().replace("def test_x():\n", "def test_x():\n    # now measured\n")
    return _commit_files(repo, subject, {PY_TEST: text, **extra})


def test_a_fix_commit_that_changes_src_and_comments_the_test_is_never_skipped_as_doc_only(
        repo: tuple[Path, str]) -> None:
    r, base = repo
    first = _comment_edit(r, "fix(a): repair the measure", {"src/nexus/a.py": "A = 2\n"})
    _real_edit(r)
    it = _pick(r, base, {"kind": "test", "file": PY_TEST})
    assert (it["fix_commit"], it["fix_distance"], it["fix_pick"]) == (first[:12], 1, "qualified")
    assert it["fix_touched_product"] is True


def test_a_refactor_that_changes_src_and_comments_the_test_is_still_skipped_as_doc_only(repo: tuple[Path, str]) -> None:
    r, base = repo
    _comment_edit(r, "refactor(a): move the measure", {"src/nexus/a.py": "A = 2\n"})
    real = _real_edit(r)
    it = _pick(r, base, {"kind": "test", "file": PY_TEST})
    assert (it["fix_commit"], it["fix_pick"]) == (real[:12], "doc_only_skipped")


def test_a_hash_line_inside_a_string_is_a_code_change_not_a_comment(repo: tuple[Path, str]) -> None:
    r, _ = repo
    script = 'SCRIPT = """\n#!/bin/sh\necho hi\n"""\n\n\ndef test_s():\n    assert SCRIPT\n'
    _commit_files(r, "feat: a script", {"tests/test_s.py": script})
    sha = _commit_files(r, "fix: the shebang", {"tests/test_s.py": script.replace("#!/bin/sh", "#!/bin/bash")})
    change = rrr.History(r).changes.analyse(sha, "tests/test_s.py")
    assert change.qualifies and not change.doc_only


def test_a_row_with_a_trailing_comment_is_a_row_in_both_readings() -> None:
    row = '    "a\\n",  # why this one'
    assert rrr.is_table_line(row) and rrr.is_table_line('    "a\\n"),  # why')          # the regex fallback
    assert not rrr.is_table_line('    "import os\\n"') and not rrr.is_table_line('    "x\\n"  # no comma')
    rows = fs.python_table_lines(f"ROWS = [\n{row}\n    'b',\n]\n")
    assert rows is not None and {2, 3} <= rows                                       # the ast reading


TABLES = '''import pytest

ROWS = [
    "one\\n",
    "two",
]
CAP = 134
PROBE = (
    "import os\\n"
    "print(1)\\n"
)
PINS = [
    ("k", "a\\n"
          "b\\n"),
    ("k2", "x\\n"),
]
CONCAT = ["a" +
          "b", "c"]
SQL = {"q": ("select 1 "
             "from t"),
       "last": "x\\ny"}
ALLOWED = frozenset({"p.py", "q.py"})


class C:
    N = 3
    S = {"x", "y"}


@pytest.mark.parametrize(("a", "b"), [
    (1, "x\\n"),
    (2, "y"),
])
def test_p(a, b):
    local = ["not", "a", "table"]
    mapping = {
        "ci_cmd": "ci",
    }
    probe = (
        "import sys\\n"
        "print(2)\\n"
    )
    run("import os\\n"
        "print(3)\\n")
    call(metadata={"title": "x"})
    assert a
'''


def _line(substr: str) -> int:
    return next(n for n, ln in enumerate(TABLES.splitlines(), 1) if substr in ln)


def test_table_lines_are_the_rows_of_every_bound_collection_literal_and_integer_constants() -> None:
    rows = fs.python_table_lines(TABLES)
    assert rows is not None
    for yes in ('"one', '"two"', "CAP = 134", '("k", "a', '"b\\n"', '("k2"', '"b", "c"', "select 1", "from t", '"last"',
                '"p.py"', "    N = 3", '    S = {', "(1,", "(2,", "local =", '"ci_cmd"'):
        assert _line(yes) in rows, yes
    # a string not held by a collection literal is code, and so is a keyword argument of a call
    for no in ("PROBE = (", "import sys", "print(2)", "run(", "print(3)", "call(metadata",
               "assert a"):
        assert _line(no) not in rows, no
    assert _line('"import os\\n"') not in rows and _line('"print(1)') not in rows
    assert fs.python_table_lines("def broken(:\n") is None


def test_a_vw594_pin_row_is_a_table_edit_and_a_probe_script_line_is_code(repo: tuple[Path, str]) -> None:
    r, _ = repo
    pins = 'PINS = [\n    ("k", "a\\nb\\n"),\n    ("j", "c"),\n]\n\n\ndef test_pins():\n    assert PINS\n'
    probe = ('def test_probe():\n    probe = (\n        "import os\\n"\n        "print(1)\\n"\n    )\n'
             '    assert probe\n')
    _commit_files(r, "feat: pins", {"tests/test_pins.py": pins, "tests/test_probe.py": probe})
    row = _commit_files(r, "fix: a row", {"tests/test_pins.py": pins.replace('"c"', '"c\\nd\\n"')})
    code = _commit_files(r, "fix: the probe", {"tests/test_probe.py": probe.replace("print(1)", "print(2)")})
    hist = rrr.History(r)
    assert hist.table_only(row, "tests/test_pins.py") is True
    assert hist.table_only(code, "tests/test_probe.py") is False


# --- self-declaring, tightened -------------------------------------------------------------------------------------


def test_a_hunk_with_a_logic_line_beside_the_declaring_line_is_a_real_change(repo: tuple[Path, str]) -> None:
    r, _ = repo
    text = (r / PY_TEST).read_text().replace(
        "def test_x():\n", 'def test_x():\n    assert "newmod.py" in ALLOW\n    assert compute() == 2\n')
    sha = _commit_files(r, "feat: a new module", {PY_TEST: text, "src/nexus/newmod.py": "X = 1\n"})
    assert rrr.History(r).changes.analyse(sha, PY_TEST).qualifies


def test_the_analysed_file_is_not_an_added_name(repo: tuple[Path, str]) -> None:
    r, _ = repo
    sha = _commit_files(r, "feat: a test that lists itself", {"tests/test_new.py": 'ALLOW = {\n    "test_new.py": 1,\n}\n'})
    assert rrr.History(r).changes.analyse(sha, "tests/test_new.py").qualifies


@pytest.mark.parametrize(("entry", "declares"), [
    ('    "newmod.py": 2,\n', True), ('    "src/nexus/newmod.py": 2,\n', True), ('    "nexus/newmod.py": 2,\n', True),
    ('    "bignewmod.py": 2,\n', False), ('    "newmod.py.bak": 2,\n', False), ('    "other/newmod": 2,\n', False),
])
def test_a_declaring_entry_names_the_added_file_by_whole_path_components(
        repo: tuple[Path, str], entry: str, declares: bool) -> None:
    r, _ = repo
    text = (r / PY_TEST).read_text().replace('    "a.py": 1,\n', '    "a.py": 1,\n' + entry)
    sha = _commit_files(r, "feat: a new module", {PY_TEST: text, "src/nexus/newmod.py": "X = 1\n"})
    assert rrr.History(r).changes.analyse(sha, PY_TEST).qualifies is (not declares)


def test_java_comment_lines_are_filtered_through_analyse(repo: tuple[Path, str]) -> None:
    r, _ = repo
    text = (r / JAVA_TEST).read_text().replace(
        'Map.entry("Zed.java"', '// a line comment\n        /* a block\n           comment */\n        Map.entry("Zed.java"')
    sha = _commit_files(r, "docs: comments", {JAVA_TEST: text})
    change = rrr.History(r).changes.analyse(sha, JAVA_TEST)
    assert change.doc_only and not change.qualifies and change.table_only is None


# --- the review round 2 shapes: where a row is, and what stays code --------------------------------------------


def _table_only_after(repo: Path, path: str, before: str, old: str, new: str) -> bool | None:
    _commit_files(repo, "feat: base", {path: before})
    sha = _commit_files(repo, "fix: edit", {path: before.replace(old, new)})
    return rrr.History(repo).table_only(sha, path)


def test_a_row_of_a_function_local_dict_is_a_table_edit(repo: tuple[Path, str]) -> None:
    """test_cli_registration (0c5c9df824a7): ``"ci_cmd": "ci",`` added to ``_MODULE_TO_CLI`` inside a test."""
    src = ('def test_reg():\n    _MODULE_TO_CLI = {\n        "agents_cmd": "agents",\n'
           '        "command_context": "command-context",\n    }\n    assert _MODULE_TO_CLI\n')
    assert _table_only_after(repo[0], "tests/test_reg.py", src, '        "command_context"',
                             '        "ci_cmd": "ci",\n        "command_context"') is True


def test_a_row_whose_value_is_a_multi_line_parenthesised_string_is_a_table_edit(repo: tuple[Path, str]) -> None:
    """test_collection_scoped_tables_schema_parity (d18e898c3767): a documented exclusion with a prose value."""
    src = '_EXCL: dict[tuple[str, str], str] = {\n    ("a", "b"): (\n        "first "\n        "second"\n    ),\n}\n'
    assert _table_only_after(repo[0], "tests/test_excl.py", src, '    ("a", "b")',
                             '    ("c", "d"): (\n        "RDR note: x "\n        "more text."\n    ),\n    ("a", "b")') is True


def test_a_keyword_argument_of_an_ordinary_call_is_code(repo: tuple[Path, str]) -> None:
    """test_reference_only_null_content_safety (2ad8c621a6c0)."""
    src = 'def test_r():\n    store(\n        collection="code__refonly", metadata={"title": "shared-doc"},\n    )\n'
    assert _table_only_after(repo[0], "tests/test_ref.py", src, '{"title": "shared-doc"}',
                             '{"doc_id": "1.1.7", "title": "shared-doc"}') is False


@pytest.mark.parametrize("source", [
    'def test_h():\n    probe = (\n        "import nexus, nexus.hooks\\n"\n        "print(1)\\n"\n    )\n',    # hooks 74e6ab64dd
    '_INNER = (\n    "import os, signal\\n"\n    "print(1)\\n"\n)\n',                                    # xzorj 932041b76eed
    'def test_a():\n    script = (\n        "import os\\n"\n        "os.replace(a, b)\\n"\n    )\n',   # aspect_extractor 643c71dad193
    'def test_q(cur):\n    cur.execute("select a "\n                "from t")\n',                       # SQL 4bacce380dc4
])
def test_a_multi_line_string_that_is_not_a_collection_element_is_code(source: str) -> None:
    rows = fs.python_table_lines(source)
    assert rows is not None and not rows


# --- decorators on classes, fixtures, attribute and subscript targets, Java integer constants ----------------------

DECORATED = '''import pytest


@pytest.mark.parametrize("n", [
    1,
    2,
])
class TestA:
    def test_a(self, n):
        pass


@pytest.fixture(params=[
    "x",
    "y",
])
def fx(request):
    return request.param


class B:
    def __init__(self):
        self.rows = [
            "r1",
        ]
        self.cache["k"] = {
            "r2": 1,
        }

    def test_b(self):
        assert self.rows == {
            "c1": 1,
        }
        for k in (
            "i1",
        ):
            pass
'''


def _dline(substr: str) -> int:
    return next(n for n, ln in enumerate(DECORATED.splitlines(), 1) if substr in ln)


def test_class_parametrize_and_fixture_params_are_tables() -> None:
    rows = fs.python_table_lines(DECORATED)
    assert rows is not None
    for yes in ("    1,", "    2,", '    "x",', '    "y",'):
        assert _dline(yes) in rows, yes


def test_attribute_and_subscript_targets_are_tables_and_a_compared_or_iterated_literal_is_code() -> None:
    rows = fs.python_table_lines(DECORATED)
    assert rows is not None
    for yes in ('"r1"', '"r2"'):
        assert _dline(yes) in rows, yes
    for no in ('"c1"', '"i1"'):
        assert _dline(no) not in rows, no


JAVA_CONST = '''class JooqRecordTest {
    private static final int EXPECTED_RECORD_TYPES = 105;
    private static final String NAME = "a";

    void t() {
        int local = 105;
        assertEquals(EXPECTED_RECORD_TYPES, count());
    }
}
'''


@pytest.mark.parametrize(("old", "new", "want"), [
    ("EXPECTED_RECORD_TYPES = 105;", "EXPECTED_RECORD_TYPES = 106;", True),     # 8fe17ad48113
    ('NAME = "a";', 'NAME = "b";', False),                                       # a String constant is no integer pin
    ("int local = 105;", "int local = 106;", False),                             # a local variable is code
    ("assertEquals(EXPECTED_RECORD_TYPES, count());", "assertEquals(1, count());", False),
])
def test_a_java_static_final_integer_constant_is_a_table_line(repo: tuple[Path, str], old: str, new: str,
                                                              want: bool) -> None:
    r = repo[0]
    path = "service/src/test/java/dev/JooqRecordTest.java"
    _commit_files(r, "test: base", {path: JAVA_CONST})
    sha = _commit_files(r, "test: edit", {path: JAVA_CONST.replace(old, new)})
    assert rrr.History(r).table_only(sha, path) is want


def test_the_unattributed_caveat_counts_fallback_picks_in_every_category() -> None:
    def item(category: str, pick: str) -> dict:
        return {"category": category, "derived": False, "fix_pick": pick}

    run = {"category": rrr.UNATTRIBUTED, "rules": ["unattributed:fix-pick-fallback"], "fix_product_commit": False,
           "range_len": 3}
    an = {"runs": {1: run, 2: {**run, "category": rrr.STALE}},
          "items": [item(rrr.ACCOUNTING, "fallback"), item(rrr.ACCOUNTING, "fallback"), item(rrr.UNATTRIBUTED, "fallback"),
                    item(rrr.STALE, "qualified"), {**item(rrr.STALE, "fallback"), "derived": True}]}
    text = rrr.unattributed_caveat(an)
    assert f"{{'{rrr.ACCOUNTING}': 2, '{rrr.UNATTRIBUTED}': 1}}" in text
    assert "items with that fallback pick, all categories" in text
