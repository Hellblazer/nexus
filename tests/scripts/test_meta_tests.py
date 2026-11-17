# SPDX-License-Identifier: AGPL-3.0-only
"""scripts/meta_tests.py: which census and lint meta-tests a diff selects, and why.

The selection runs over a throwaway git repo with a hand-made manifest; the tracer
runs for real over a throwaway pytest project, so what a test reads is recorded,
not assumed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import meta_tests as mt

REPO_ROOT = Path(__file__).resolve().parents[2]

LINT = "import pytest\npytestmark = pytest.mark.lint\n\ndef test_it():\n    pass\n"
CONFTEST = '_SESSION_ITEMS_CENSUS_TEST_NODEIDS: frozenset[str] = frozenset({"tests/test_guard.py::test_guard"})\n'


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def _write(repo: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    _write(r, {
        "tests/conftest.py": CONFTEST,
        "tests/test_guard.py": "def test_guard():\n    pass\n",
        "tests/test_scan.py": LINT,
        "tests/test_census.py": "import pytest\n\n@pytest.mark.census\ndef test_c():\n    pass\n",
        "tests/test_plain.py": "def test_p():\n    pass\n",
        "tests/test_imports.py": "import pytest\nfrom nexus.mod import X\npytestmark = pytest.mark.lint\n",
        "src/nexus/__init__.py": "",
        "src/nexus/mod.py": "X = 1\n",
        "src/nexus/pkg/a.py": "a = 1\n",
        "docs/a.md": "a\n",
        "service/src/test/java/x/WalkGateTest.java": "class WalkGateTest { void t() { Files.walk(root); } }\n",
        "service/src/test/java/x/WalkIntegrationTest.java": "class WalkIntegrationTest { void t() { Files.walk(root); } }\n",
        "service/src/test/java/x/PlainTest.java": "class PlainTest {}\n",
        "service/src/main/java/x/Main.java": "class Main {}\n",
    })
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    _git(r, "tag", "base")
    return r


def _manifest(repo: Path, **surfaces: dict) -> dict:
    tests = {}
    for name, entry in surfaces.items():
        test = f"tests/{name}.py"
        tests[test] = {"sha256": mt.sha256(repo / test), "opened": [], "statted": [], "listed": [],
                       "prefixes": [], "opaque": [], **entry}
    return {"tests": tests}


def test_discovery_finds_lint_census_and_session_items_files_and_no_plain_test(repo: Path) -> None:
    known = mt.python_meta_tests(repo)
    assert known == {
        "tests/test_scan.py": {"lint"},
        "tests/test_census.py": {"census"},
        "tests/test_imports.py": {"lint"},
        "tests/test_guard.py": {"session-items"},
    }


def test_java_discovery_takes_source_walkers_and_leaves_out_integration_tests(repo: Path) -> None:
    assert mt.java_meta_tests(repo) == ["service/src/test/java/x/WalkGateTest.java"]


def test_a_conftest_without_the_session_items_registry_stops_the_script(repo: Path) -> None:
    (repo / "tests/conftest.py").write_text("x = 1\n")
    with pytest.raises(SystemExit, match="_SESSION_ITEMS_CENSUS_TEST_NODEIDS"):
        mt.python_meta_tests(repo)


@pytest.mark.parametrize(
    ("entry", "changed", "reason"),
    [
        ({"opened": ["docs/a.md"]}, "docs/a.md", "reads docs/a.md"),
        ({"statted": ["docs/gone.md"]}, "docs/gone.md", "reads docs/gone.md"),
        ({"listed": ["docs"]}, "docs/new.md", "lists the directory of docs/new.md"),
        # A new directory: its first ancestor that exists at the base was listed.
        ({"listed": ["docs"]}, "docs/fresh/deeper/new.md", "lists the directory of docs/fresh/deeper/new.md"),
        ({"prefixes": ["src/nexus"]}, "src/nexus/pkg/a.py", "enumerates a subtree holding src/nexus/pkg/a.py"),
        ({"prefixes": [""]}, "anything/at/all.txt", "enumerates a subtree holding anything/at/all.txt"),
    ],
)
def test_a_path_in_the_traced_surface_selects_the_test(repo: Path, entry: dict, changed: str, reason: str) -> None:
    sel = mt.select(repo, [changed], "base", _manifest(repo, test_scan=entry))
    assert sel.selected == {"tests/test_scan.py": reason}


@pytest.mark.parametrize(
    ("entry", "changed"),
    [
        ({"opened": ["docs/a.md"]}, "docs/b.md"),
        # A listed directory selects its own entries, not a deeper existing directory's.
        ({"listed": ["src/nexus"]}, "src/nexus/pkg/a.py"),
        ({"prefixes": ["src/nexus"]}, "src/nexusx/a.py"),
        ({}, "docs/a.md"),
    ],
)
def test_a_path_outside_the_surface_selects_nothing(repo: Path, entry: dict, changed: str) -> None:
    sel = mt.select(repo, [changed], "base", _manifest(repo, test_scan=entry))
    assert "tests/test_scan.py" not in sel.selected
    assert "tests/test_scan.py" not in sel.always


def test_a_module_the_test_imports_directly_selects_it(repo: Path) -> None:
    sel = mt.select(repo, ["src/nexus/mod.py"], "base", _manifest(repo, test_imports={}))
    assert sel.selected["tests/test_imports.py"] == "imports src/nexus/mod.py"
    sel = mt.select(repo, ["src/nexus/__init__.py"], "base", _manifest(repo, test_imports={}))
    assert sel.selected["tests/test_imports.py"] == "imports src/nexus/__init__.py"


def test_the_test_file_itself_changing_selects_it(repo: Path) -> None:
    manifest = _manifest(repo, test_scan={})
    sel = mt.select(repo, ["tests/test_scan.py"], "base", manifest)
    assert sel.selected["tests/test_scan.py"] == "the test file itself changed"


def test_a_meta_test_the_manifest_lacks_runs_whatever_the_diff_and_says_why(repo: Path) -> None:
    sel = mt.select(repo, ["docs/a.md"], "base", {"tests": {}})
    assert set(sel.always) == {"tests/test_scan.py", "tests/test_census.py", "tests/test_imports.py"}
    assert all("not in the manifest" in why for why in sel.always.values())


def test_a_meta_test_edited_since_the_build_runs_whatever_the_diff(repo: Path) -> None:
    manifest = _manifest(repo, test_scan={"opened": ["docs/a.md"]})
    (repo / "tests/test_scan.py").write_text(LINT + "\n# edited\n")
    sel = mt.select(repo, ["src/nexus/pkg/a.py"], "base", manifest)
    assert "changed since the manifest" in sel.always["tests/test_scan.py"]


def test_a_meta_test_reading_through_an_opaque_subprocess_runs_whatever_the_diff(repo: Path) -> None:
    manifest = _manifest(repo, test_scan={"opaque": ["uv run python scripts/x.py"]})
    sel = mt.select(repo, ["docs/a.md"], "base", manifest)
    assert sel.always["tests/test_scan.py"].endswith("uv run python scripts/x.py")


def test_the_session_items_guard_runs_only_with_changed_test_files(repo: Path) -> None:
    manifest = _manifest(repo, test_scan={}, test_census={}, test_imports={})
    assert "tests/test_guard.py" not in mt.select(repo, ["docs/a.md"], "base", manifest).selected
    sel = mt.select(repo, ["tests/test_plain.py"], "base", manifest)
    assert "1 changed test file" in sel.selected["tests/test_guard.py"]


def test_an_engine_source_change_selects_the_java_walkers_and_a_docs_change_does_not(repo: Path) -> None:
    manifest = _manifest(repo, test_scan={}, test_census={}, test_imports={})
    sel = mt.select(repo, ["service/src/main/java/x/Main.java"], "base", manifest)
    assert set(sel.java) == {"service/src/test/java/x/WalkGateTest.java"}
    assert "-Dtest=WalkGateTest" in (mt.java_command(sel) or "")
    assert mt.select(repo, ["docs/a.md"], "base", manifest).java == {}


def test_the_report_counts_every_kind_and_names_each_pick(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = _manifest(repo, test_scan={"opened": ["docs/a.md"]}, test_imports={})
    mt.report(mt.select(repo, ["docs/a.md"], "base", manifest))
    out = capsys.readouterr().out
    assert "meta-tests known: 4 Python (2 lint, 1 census, 1 session-items), 1 Java" in out
    assert "tests/test_scan.py: reads docs/a.md" in out
    assert "tests/test_census.py: not in the manifest" in out


def test_changed_paths_take_committed_uncommitted_and_untracked_files(repo: Path) -> None:
    _write(repo, {"docs/committed.md": "c\n"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "c")
    _write(repo, {"docs/a.md": "edited\n", "docs/untracked.md": "u\n"})
    assert mt.changed_paths(repo, "base") == ["docs/a.md", "docs/committed.md", "docs/untracked.md"]


# ── the tracer, for real ─────────────────────────────────────────────────────


def test_the_tracer_records_reads_listings_stats_and_git_enumerations_and_skips_imports(
    repo: Path, tmp_path: Path
) -> None:
    _write(repo, {
        "tests/__init__.py": "",
        "tests/_surface_trace.py": (REPO_ROOT / "tests/_surface_trace.py").read_text(),
        "tests/test_traced.py": (
            "import os, subprocess\nfrom pathlib import Path\nimport pytest\n"
            "pytestmark = pytest.mark.lint\n"
            "LISTED = sorted(p.name for p in Path('src/nexus/pkg').iterdir())\n"
            "def test_reads():\n"
            "    Path('docs/a.md').read_text()\n"
            "    assert not os.path.exists('docs/absent.md')\n"
            "    subprocess.run(['git', 'ls-files', '--', 'service'], check=True, capture_output=True)\n"
            "    subprocess.run(['ps', '-o', 'pid='], capture_output=True)\n"
            "def test_opaque():\n"
            "    subprocess.run([os.sys.executable, '-c', 'pass'], check=True)\n"
        ),
    })
    out = tmp_path / "trace"
    env = {**os.environ, "NX_SURFACE_TRACE_OUT": str(out), "PYTHONPATH": str(repo / "src") + os.pathsep + str(repo)}
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "-p", "no:cacheprovider", "-p", "tests._surface_trace",
         "--rootdir", str(repo), "-c", os.devnull, "tests/test_traced.py"],
        cwd=repo, env=env, capture_output=True, text=True,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    [frag] = list(out.glob("trace-*.json"))
    got = json.loads(frag.read_text())["tests/test_traced.py"]
    assert "docs/a.md" in got["opened"]
    assert "docs/absent.md" in got["statted"]
    assert "src/nexus/pkg" in got["listed"]  # module level: recorded during collection
    assert got["prefixes"] == ["service"]
    assert [o for o in got["opaque"] if o.startswith(sys.executable)], got["opaque"]
    assert not any(o.startswith("ps ") for o in got["opaque"])
    assert not any(p.endswith(".pyc") or p == "tests/_surface_trace.py" for p in got["opened"])
