# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-258 test selection: ``scripts/select_affected_tests.py`` picks the test files a change can reach.

Each test builds a tiny repo tree, so the selection rules are pinned independent of today's corpus.
The last class runs the selector against this checkout, so the composition with
``scripts/meta_tests.py`` and ``scripts/select_docs_reader_tests.py`` is exercised once for real.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

import select_affected_tests as sat

#: Roots this file's subprocess reads, which scripts/meta_tests.py's trace cannot see.
META_SCANS = ("scripts/select_affected_tests.py", "scripts/meta_tests.py")

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "select_affected_tests.py"


def _write(root: pathlib.Path, rel: str, text: str = "") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


@pytest.fixture()
def tree(tmp_path: pathlib.Path) -> pathlib.Path:
    """src: a -> b -> c (a chain), d standalone; tests reach them at different depths."""
    _write(tmp_path, "src/nexus/__init__.py")
    _write(tmp_path, "src/nexus/c.py", "C = 1\n")
    _write(tmp_path, "src/nexus/b.py", "from nexus import c\n")
    _write(tmp_path, "src/nexus/a.py", "from nexus.b import C\n")
    _write(tmp_path, "src/nexus/d.py", "D = 1\n")
    _write(tmp_path, "src/nexus/pkg/__init__.py", "from .inner import X\n")
    _write(tmp_path, "src/nexus/pkg/inner.py", "X = 1\n")
    _write(tmp_path, "src/nexus/pkg/sibling.py", "from . import inner\n")
    _write(tmp_path, "src/nexus/lazy.py", "def f():\n    import nexus.d\n")
    _write(tmp_path, "tests/__init__.py")
    _write(tmp_path, "tests/conftest.py", "pytest_plugins = ['tests._plug']\n")
    _write(tmp_path, "tests/_plug.py", "X = 1\n")
    _write(tmp_path, "tests/_helper.py", "H = 1\n")
    _write(tmp_path, "tests/test_direct.py", "import nexus.a\n")
    _write(tmp_path, "tests/test_deep.py", "from nexus import a\n")
    _write(tmp_path, "tests/test_none.py", "import json\n")
    _write(tmp_path, "tests/test_pkg.py", "from nexus.pkg.sibling import inner\n")
    _write(tmp_path, "tests/test_lazy.py", "from nexus.lazy import f\n")
    _write(tmp_path, "tests/test_patchstr.py", "def test_x(monkeypatch):\n    monkeypatch.setattr('nexus.d.D', 2)\n")
    _write(tmp_path, "tests/test_uses_helper.py", "from tests._helper import H\n")
    _write(tmp_path, "tests/sub/__init__.py")
    _write(tmp_path, "tests/sub/conftest.py", "import nexus.d\n")
    _write(tmp_path, "tests/sub/test_in_sub.py", "def test_x():\n    pass\n")
    _write(tmp_path, "tests/test_outside_sub.py", "def test_x():\n    pass\n")
    return tmp_path


def _sel(repo: pathlib.Path, changed: list[str], **kw: object) -> sat.Selection:
    return sat.select(repo, changed, base=None, use_meta=False, use_docs=False, **kw)


class TestImportGraph:
    def test_direct_import_selects_the_test(self, tree: pathlib.Path) -> None:
        assert "tests/test_direct.py" in _sel(tree, ["src/nexus/a.py"]).files

    def test_unrelated_module_selects_nothing_from_that_test(self, tree: pathlib.Path) -> None:
        got = _sel(tree, ["src/nexus/d.py"]).files
        assert "tests/test_direct.py" not in got
        assert "tests/test_none.py" not in got

    def test_relative_and_from_package_imports_resolve(self, tree: pathlib.Path) -> None:
        # test_pkg imports nexus.pkg.sibling, which does `from . import inner`
        assert "tests/test_pkg.py" in _sel(tree, ["src/nexus/pkg/inner.py"]).files

    def test_function_local_import_counts(self, tree: pathlib.Path) -> None:
        assert "tests/test_lazy.py" in _sel(tree, ["src/nexus/d.py"]).files

    def test_dotted_string_naming_a_module_counts(self, tree: pathlib.Path) -> None:
        got = _sel(tree, ["src/nexus/d.py"])
        assert "tests/test_patchstr.py" in got.files

    def test_parent_package_init_is_an_edge(self, tree: pathlib.Path) -> None:
        # importing nexus.pkg.sibling executes nexus/pkg/__init__.py
        assert "tests/test_pkg.py" in _sel(tree, ["src/nexus/pkg/__init__.py"]).files


class TestTransitive:
    def test_two_hops_reach_the_test(self, tree: pathlib.Path) -> None:
        # test_direct -> a -> b -> c
        got = _sel(tree, ["src/nexus/c.py"])
        assert "tests/test_direct.py" in got.files
        assert "tests/test_deep.py" in got.files

    def test_the_reason_names_the_chain(self, tree: pathlib.Path) -> None:
        why = " ".join(_sel(tree, ["src/nexus/c.py"]).files["tests/test_direct.py"])
        assert "nexus.c" in why and "nexus.a" in why

    def test_max_depth_bounds_the_closure(self, tree: pathlib.Path) -> None:
        # test_direct -> a -> b -> c: two hops from c
        assert "tests/test_direct.py" not in _sel(tree, ["src/nexus/c.py"], max_depth=1).files
        assert "tests/test_direct.py" in _sel(tree, ["src/nexus/c.py"], max_depth=3).files

    def test_a_cli_registrar_does_not_drag_in_every_test_that_imports_it(self, tree: pathlib.Path) -> None:
        adds = "".join(f"main.add_command(c{i})\n" for i in range(10))
        imports = "".join(f"from nexus.commands.m{i} import c{i}\n" for i in range(10))
        _write(tree, "src/nexus/commands/__init__.py")
        for i in range(10):
            _write(tree, f"src/nexus/commands/m{i}.py", f"c{i} = 1\n")
        _write(tree, "src/nexus/cli.py", imports + "main = object()\n" + adds)
        _write(tree, "tests/test_cli_search.py", "from nexus.cli import main\nARGS = ['m3', 'x']\n")
        _write(tree, "tests/test_cli_other.py", "from nexus.cli import main\nARGS = ['zzz']\n")
        got = _sel(tree, ["src/nexus/commands/m3.py"]).files
        assert "tests/test_cli_search.py" in got
        assert "tests/test_cli_other.py" not in got
        # the registrar's own change reaches every importer
        assert "tests/test_cli_other.py" in _sel(tree, ["src/nexus/cli.py"]).files

    def test_a_test_helper_reaches_its_importers(self, tree: pathlib.Path) -> None:
        # not imported by any conftest, so the import closure decides: no run-everything
        got = _sel(tree, ["tests/_helper.py"])
        assert got.everything is None
        assert "tests/test_uses_helper.py" in got.files
        assert "tests/test_none.py" not in got.files

    def test_a_changed_test_file_selects_itself(self, tree: pathlib.Path) -> None:
        assert "tests/test_none.py" in _sel(tree, ["tests/test_none.py"]).files

    def test_a_deleted_module_still_selects_its_importers(self, tree: pathlib.Path) -> None:
        (tree / "src/nexus/d.py").unlink()
        assert "tests/test_lazy.py" in _sel(tree, ["src/nexus/d.py"]).files


class TestFileReferences:
    def test_basename_in_a_test_source_selects_it(self, tree: pathlib.Path) -> None:
        _write(tree, "tests/test_reads_ledger.py", "def test_x():\n    open('ledger.toml')\n")
        _write(tree, "tests/ledger.toml", "x = 1\n")
        got = _sel(tree, ["tests/ledger.toml"])
        assert "tests/test_reads_ledger.py" in got.files
        assert "tests/test_none.py" not in got.files

    def test_a_pathlib_chain_is_joined(self, tree: pathlib.Path) -> None:
        _write(tree, "tests/test_chain.py", "import pathlib\nR = pathlib.Path('.')\nP = R / 'conexus' / 'skills' / 'x.md'\n")
        assert "tests/test_chain.py" in _sel(tree, ["conexus/skills/x.md"]).files

    def test_a_directory_literal_selects_for_a_file_under_it(self, tree: pathlib.Path) -> None:
        _write(tree, "tests/test_dir.py", "import pathlib\nP = pathlib.Path('.') / '.github' / 'workflows'\n")
        assert "tests/test_dir.py" in _sel(tree, [".github/workflows/ci.yml"]).files

    def test_a_glob_selects_for_a_matching_file(self, tree: pathlib.Path) -> None:
        _write(tree, "tests/test_glob.py", "import pathlib\nL = list(pathlib.Path('.').glob('rdr-*.md'))\n")
        assert "tests/test_glob.py" in _sel(tree, ["docs/rdr/rdr-258.md"]).files

    def test_an_unreferenced_file_selects_nothing(self, tree: pathlib.Path) -> None:
        assert _sel(tree, ["docs/unrelated-notes.md"]).files == {}

    def test_a_referenced_source_data_file_selects_through_its_module(self, tree: pathlib.Path) -> None:
        _write(tree, "src/nexus/reader.py", "NAME = 'schema.sql'\n")
        _write(tree, "tests/test_reader.py", "import nexus.reader\n")
        _write(tree, "src/nexus/schema.sql", "select 1;\n")
        got = _sel(tree, ["src/nexus/schema.sql"])
        assert "tests/test_reader.py" in got.files

    def test_package_data_selects_the_modules_beside_it(self, tree: pathlib.Path) -> None:
        # a loader that scans its directory never names the file it loads
        _write(tree, "src/nexus/tables/__init__.py")
        _write(tree, "src/nexus/tables/load.py", "import pathlib\nROWS = sorted(pathlib.Path(__file__).parent.glob('*.toml'))\n")
        _write(tree, "src/nexus/tables/sites.toml", "x = 1\n")
        _write(tree, "tests/test_tables.py", "from nexus.tables import load\n")
        assert "tests/test_tables.py" in _sel(tree, ["src/nexus/tables/sites.toml"]).files

    def test_a_directory_scan_in_a_source_module_does_not_seed_its_closure(self, tree: pathlib.Path) -> None:
        # a bare README.md named by a src module must not pull in everything that imports the module
        _write(tree, "src/nexus/idx.py", "NAME = 'README.md'\n")
        _write(tree, "tests/test_idx.py", "import nexus.idx\n")
        assert "tests/test_idx.py" not in _sel(tree, ["docs/README.md"]).files

    def test_a_script_run_by_basename_is_selected(self, tree: pathlib.Path) -> None:
        _write(tree, "scripts/tool.py", "print('x')\n")
        _write(tree, "tests/test_tool.py", "import subprocess\nsubprocess.run(['python', 'scripts/tool.py'])\n")
        assert "tests/test_tool.py" in _sel(tree, ["scripts/tool.py"]).files

    def test_a_script_imported_bare_is_selected(self, tree: pathlib.Path) -> None:
        _write(tree, "scripts/tool2.py", "X = 1\n")
        _write(tree, "tests/test_tool2.py", "import tool2\n")
        assert "tests/test_tool2.py" in _sel(tree, ["scripts/tool2.py"]).files


class TestSelectsEverything:
    @pytest.mark.parametrize("changed", ["tests/conftest.py", "pyproject.toml", "uv.lock"])
    def test_these_select_everything(self, tree: pathlib.Path, changed: str) -> None:
        got = _sel(tree, [changed])
        assert got.everything
        assert changed in got.everything

    def test_a_helper_a_conftest_pulls_in_selects_everything(self, tree: pathlib.Path) -> None:
        got = _sel(tree, ["tests/_plug.py"])
        assert got.everything and "tests/_plug.py" in got.everything

    def test_a_nested_conftest_selects_its_directory_only(self, tree: pathlib.Path) -> None:
        got = _sel(tree, ["tests/sub/conftest.py"])
        assert got.everything is None
        assert "tests/sub/test_in_sub.py" in got.files
        assert "tests/test_outside_sub.py" not in got.files

    def test_the_cli_prints_the_tests_dir_and_says_why(self, tree: pathlib.Path) -> None:
        out = subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(tree), "--no-meta", "--no-docs", "tests/conftest.py"],
            capture_output=True, text=True, check=True,
        )
        assert out.stdout.split() == ["tests"]
        assert "everything" in out.stderr.lower()


class TestServiceChange:
    def test_a_service_change_prints_the_mvnw_line_and_selects_route_readers(self, tree: pathlib.Path) -> None:
        _write(tree, "service/src/main/java/dev/nexus/service/http/WidgetHandler.java",
               'class WidgetHandler { static final String R = "/v1/widgets/{id}"; }\n')
        _write(tree, "service/src/test/java/dev/nexus/service/http/WidgetHandlerTest.java", "class WidgetHandlerTest {}\n")
        _write(tree, "src/nexus/widget_client.py", "URL = '/v1/widgets'\n")
        _write(tree, "tests/test_widget_client.py", "import nexus.widget_client\n")
        _write(tree, "tests/test_widget_wire.py", "def test_x():\n    assert '/v1/widgets/abc'\n")
        got = _sel(tree, ["service/src/main/java/dev/nexus/service/http/WidgetHandler.java"])
        assert "tests/test_widget_client.py" in got.files
        assert "tests/test_widget_wire.py" in got.files
        assert any("mvnw-leased" in n and "WidgetHandlerTest" in n for n in got.notes)

    def test_service_substrate_flag_selects_the_engine_tests(self, tree: pathlib.Path) -> None:
        _write(tree, "service/src/main/java/dev/nexus/service/Util.java", "class Util {}\n")
        _write(tree, "tests/test_boots_engine.py", "import pytest\npytestmark = pytest.mark.t2_tenant\n")
        changed = ["service/src/main/java/dev/nexus/service/Util.java"]
        assert "tests/test_boots_engine.py" not in _sel(tree, changed).files
        got = _sel(tree, changed, service_substrate=True)
        assert "tests/test_boots_engine.py" in got.files

    def test_a_service_change_with_no_route_says_so(self, tree: pathlib.Path) -> None:
        _write(tree, "service/src/main/java/dev/nexus/service/Util.java", "class Util {}\n")
        got = _sel(tree, ["service/src/main/java/dev/nexus/service/Util.java"])
        assert any("no wire route" in n for n in got.notes)


class TestIndexCache:
    def test_a_second_build_reuses_unchanged_files(self, tree: pathlib.Path) -> None:
        sat.build_index(tree)
        stats = sat.build_index(tree).stats
        assert stats["parsed"] == 0 and stats["cached"] > 0

    def test_an_edited_file_is_reparsed(self, tree: pathlib.Path) -> None:
        sat.build_index(tree)
        _write(tree, "src/nexus/d.py", "D = 2  # edited, longer\n")
        assert sat.build_index(tree).stats["parsed"] == 1


class TestExplainAndRange:
    def test_explain_prints_the_reason_per_file(self, tree: pathlib.Path) -> None:
        out = subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(tree), "--no-meta", "--no-docs", "--explain", "src/nexus/c.py"],
            capture_output=True, text=True, check=True,
        )
        assert "tests/test_direct.py" in out.stdout
        assert "nexus.c" in out.stdout

    def test_files_from_stdin(self, tree: pathlib.Path) -> None:
        out = subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(tree), "--no-meta", "--no-docs", "--files-from", "-"],
            input="src/nexus/d.py\n", capture_output=True, text=True, check=True,
        )
        assert "tests/test_lazy.py" in out.stdout.split()

    def test_a_git_range_names_the_changed_files(self, tree: pathlib.Path) -> None:
        def git(*a: str) -> None:
            subprocess.run(["git", "-C", str(tree), *a], check=True, capture_output=True)

        git("init", "-q")
        git("-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
        _write(tree, "src/nexus/d.py", "D = 3\n")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "change")
        out = subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(tree), "--no-meta", "--no-docs", "--range", "HEAD~1..HEAD"],
            capture_output=True, text=True, check=True,
        )
        assert "tests/test_lazy.py" in out.stdout.split()


class TestAgainstThisCheckout:
    """Composition with meta_tests.py and select_docs_reader_tests.py, on the real tree."""

    def test_a_script_change_selects_its_own_test(self) -> None:
        got = sat.select(REPO_ROOT, ["scripts/file_size_ratchet.py"], base="HEAD")
        assert any("file_size_ratchet" in f for f in got.files), sorted(got.files)

    def test_a_docs_change_selects_the_docs_readers(self) -> None:
        got = sat.select(REPO_ROOT, ["docs/rdr/AGENTS.md"], base="HEAD", use_meta=False)
        docs = {r for r, why in got.files.items() if any("docs reader" in w for w in why)}
        assert len(docs) >= 20, sorted(got.files)

    def test_a_src_change_reaches_a_test_by_import(self) -> None:
        got = sat.select(REPO_ROOT, ["src/nexus/catalog/tumbler.py"], base="HEAD", use_meta=False, use_docs=False)
        assert got.files and got.everything is None
