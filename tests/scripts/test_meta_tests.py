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

READS = {"opened": ["pyproject.toml"]}
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
        tests[test] = {"sha256": mt.surface_hash(repo, test), "opened": [], "statted": [], "listed": [],
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
        ({"opened": ["pyproject.toml"]}, "docs/a.md"),
    ],
)
def test_a_path_outside_the_surface_selects_nothing(repo: Path, entry: dict, changed: str) -> None:
    sel = mt.select(repo, [changed], "base", _manifest(repo, test_scan=entry))
    assert "tests/test_scan.py" not in sel.selected
    assert "tests/test_scan.py" not in sel.always


def test_a_module_the_test_imports_directly_selects_it(repo: Path) -> None:
    sel = mt.select(repo, ["src/nexus/mod.py"], "base", _manifest(repo, test_imports=READS))
    assert sel.selected["tests/test_imports.py"] == "imports src/nexus/mod.py"
    sel = mt.select(repo, ["src/nexus/__init__.py"], "base", _manifest(repo, test_imports=READS))
    assert sel.selected["tests/test_imports.py"] == "imports src/nexus/__init__.py"


def test_the_test_file_itself_changing_selects_it(repo: Path) -> None:
    manifest = _manifest(repo, test_scan=READS)
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
    manifest = _manifest(repo, test_scan=READS, test_census=READS, test_imports=READS)
    assert "tests/test_guard.py" not in mt.select(repo, ["docs/a.md"], "base", manifest).selected
    sel = mt.select(repo, ["tests/test_plain.py"], "base", manifest)
    assert "1 changed test file" in sel.selected["tests/test_guard.py"]


def test_an_engine_source_change_selects_the_java_walkers_and_a_docs_change_does_not(repo: Path) -> None:
    manifest = _manifest(repo, test_scan=READS, test_census=READS, test_imports=READS)
    sel = mt.select(repo, ["service/src/main/java/x/Main.java"], "base", manifest)
    assert set(sel.java) == {"service/src/test/java/x/WalkGateTest.java"}
    assert "-Dtest=WalkGateTest" in (mt.java_command(sel) or "")
    assert mt.select(repo, ["docs/a.md"], "base", manifest).java == {}


def test_the_report_counts_every_kind_and_names_each_pick(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = _manifest(repo, test_scan={"opened": ["docs/a.md"]}, test_imports=READS)
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
            "    subprocess.run(['git', 'ls-files', '--', 'service', 'src/**.py', ':!docs'], check=True, capture_output=True)\n"
            "    subprocess.run(['ps', '-o', 'pid='], capture_output=True)\n"
            "def test_opaque():\n"
            "    subprocess.run([os.sys.executable, '-c', 'pass'], check=True)\n"
        ),
    })
    out = tmp_path / "trace"
    env = {**os.environ, "NX_TEST_T2_SUBSTRATE": "none", "NX_SURFACE_TRACE_OUT": str(out),
           "PYTHONPATH": str(repo / "src") + os.pathsep + str(repo)}
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
    assert got["prefixes"] == ["service", "src"]
    assert [o for o in got["opaque"] if o.startswith(sys.executable)], got["opaque"]
    assert not any(o.startswith("ps ") for o in got["opaque"])
    assert not any(p.endswith(".pyc") or p == "tests/_surface_trace.py" for p in got["opened"])


@pytest.mark.parametrize(
    ("spec", "root"),
    [
        ("src/nexus", "src/nexus"),
        ("src/nexus/", "src/nexus"),
        ("src/nexus/**.py", "src/nexus"),
        ("src/nexus/a*.py", "src/nexus"),
        ("*.py", ""),
        ("docs/rdr/rdr-[0-9]*.md", "docs/rdr"),
        (":(glob)tests/**/test_*.py", "tests"),
        (":!docs", None),
        (":^docs", None),
        (":(exclude)docs", None),
        (":(top,exclude)docs", None),
        (":(top)scripts", "scripts"),
    ],
)
def test_a_pathspec_is_read_as_the_directory_it_can_match_under(spec: str, root: str | None) -> None:
    sys.path.insert(0, str(REPO_ROOT))
    from tests._surface_trace import _pathspec_root

    assert _pathspec_root(spec) == root


def test_compaction_drops_only_what_selects_nothing_more() -> None:
    got = mt.compact({
        "prefixes": ["src/nexus", "src/nexus/pkg", "tests"],
        "listed": ["src/nexus/x", "docs"],
        "opened": ["src/nexus/a.py", "docs/a.md", "docs/sub/b.md", "README.md"],
        "statted": ["docs/sub/b.md", "docs/c.md", "scripts/s.py"],
        "opaque": ["bash x.sh"],
    })
    assert got == {
        "prefixes": ["src/nexus", "tests"],
        "listed": ["docs"],
        "opened": ["README.md", "docs/a.md", "docs/sub/b.md"],
        "statted": ["docs/c.md", "scripts/s.py"],
        "opaque": ["bash x.sh"],
    }


def test_a_meta_test_whose_trace_recorded_nothing_or_did_not_finish_runs_whatever_the_diff(repo: Path) -> None:
    manifest = _manifest(repo, test_scan={}, test_census={"opened": ["docs/a.md"], "incomplete": "pytest exit 1: x"})
    sel = mt.select(repo, ["src/nexus/pkg/a.py"], "base", manifest)
    assert "recorded no read" in sel.always["tests/test_scan.py"]
    assert sel.always["tests/test_census.py"].endswith("pytest exit 1: x")


def test_a_changed_conftest_or_imported_tests_helper_makes_the_surface_stale(repo: Path) -> None:
    _write(repo, {"tests/_helper.py": "ROOT = 'docs'\n", "tests/test_scan.py": LINT + "from tests._helper import ROOT\n"})
    manifest = _manifest(repo, test_scan={"opened": ["docs/a.md"]})
    assert "tests/_helper.py" in mt.tests_closure(repo, "tests/test_scan.py")
    assert "tests/conftest.py" in mt.tests_closure(repo, "tests/test_scan.py")
    assert "tests/test_scan.py" not in mt.select(repo, ["src/nexus/pkg/a.py"], "base", manifest).always
    (repo / "tests/_helper.py").write_text("ROOT = 'src'\n")
    assert "changed since the manifest" in mt.select(repo, ["src/nexus/pkg/a.py"], "base", manifest).always["tests/test_scan.py"]
    _write(repo, {"tests/_helper.py": "ROOT = 'docs'\n"})
    (repo / "tests/conftest.py").write_text(CONFTEST + "# edited\n")
    assert "changed since the manifest" in mt.select(repo, ["src/nexus/pkg/a.py"], "base", manifest).always["tests/test_scan.py"]


@pytest.fixture
def uv_is_pytest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `uv` on PATH that runs `uv run pytest ARGS` as this interpreter's pytest, so run and
    build execute for real over a throwaway repo."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(f"#!/bin/sh\nshift 2\nexec {sys.executable} -m pytest -p no:cacheprovider \"$@\"\n")
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def _with_tracer(repo: Path) -> None:
    _write(repo, {
        "tests/__init__.py": "",
        "tests/_surface_trace.py": (REPO_ROOT / "tests/_surface_trace.py").read_text(),
        "tests/test_scan.py": LINT.replace("pass", "from pathlib import Path; Path('docs/a.md').read_text()"),
        "tests/test_census.py": "import pytest\nfrom pathlib import Path\n\n@pytest.mark.census\ndef test_c():\n    sorted(Path('src/nexus/pkg').iterdir())\n",
        "tests/test_imports.py": "import pytest\npytestmark = pytest.mark.lint\n\ndef test_fails():\n    assert False\n",
    })
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "tracer")
    _git(repo, "tag", "-f", "base")


def test_build_traces_each_file_for_real_and_marks_a_failed_run_incomplete(
    repo: Path, uv_is_pytest: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_tracer(repo)
    assert mt.main(["build", "--repo", str(repo), "--jobs", "2"]) == 0
    tests = mt.load_manifest(repo / mt.MANIFEST)["tests"]
    assert set(tests) == {"tests/test_scan.py", "tests/test_census.py", "tests/test_imports.py"}
    assert "docs/a.md" in tests["tests/test_scan.py"]["opened"]
    assert tests["tests/test_census.py"]["listed"] == ["src/nexus/pkg"]
    assert tests["tests/test_imports.py"]["incomplete"].startswith("pytest exit 1")
    assert tests["tests/test_scan.py"]["sha256"] == mt.surface_hash(repo, "tests/test_scan.py")
    assert "1 with an incomplete trace" in capsys.readouterr().out


def test_run_selects_from_the_built_manifest_runs_the_pick_and_says_what_it_does_not_cover(
    repo: Path, uv_is_pytest: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_tracer(repo)
    mt.main(["build", "--repo", str(repo), "--jobs", "2"])
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "manifest")
    _git(repo, "tag", "-f", "base")
    (repo / "docs/a.md").write_text("changed\n")
    rc = mt.main(["run", "--repo", str(repo), "--base", "base", "--", "--maxfail=50"])
    out = capsys.readouterr().out
    assert "tests/test_scan.py: reads docs/a.md" in out
    assert "tests/test_census.py" not in out.split("run whatever the diff")[0]
    assert "tests/test_imports.py: its traced run did not complete cleanly" in out
    assert "meta-tests ran in" in out
    assert "--maxfail=50 tests/" in out  # pytest arguments after -- reach the meta-test run
    assert out.rstrip().endswith(mt.NOT_COVERED)
    assert rc == 1  # test_imports fails, and it ran


def test_a_listed_directory_selects_a_path_added_or_deleted_in_it_and_not_an_edit(repo: Path) -> None:
    manifest = _manifest(repo, test_scan={"listed": ["docs"]})
    _write(repo, {"docs/new.md": "n\n", "docs/fresh/deeper/new.md": "n\n"})
    sel = mt.select(repo, ["docs/new.md"], "base", manifest)
    assert sel.selected["tests/test_scan.py"] == "lists the directory gaining docs/new.md"
    # A new directory: its first ancestor that exists at the base was listed.
    sel = mt.select(repo, ["docs/fresh/deeper/new.md"], "base", manifest)
    assert sel.selected["tests/test_scan.py"] == "lists the directory gaining docs/fresh/deeper/new.md"
    (repo / "docs/a.md").write_text("edited\n")
    assert "tests/test_scan.py" not in mt.select(repo, ["docs/a.md"], "base", manifest).selected
    (repo / "docs/a.md").unlink()
    sel = mt.select(repo, ["docs/a.md"], "base", manifest)
    assert sel.selected["tests/test_scan.py"] == "lists the directory losing docs/a.md"


def test_the_manifest_round_trips_through_its_inverted_gzip_form_byte_for_byte(tmp_path: Path) -> None:
    manifest = {"built_from": "abc", "tests": {
        "tests/test_a.py": {"sha256": "1", "opened": ["docs/a.md", "src/x.py"], "statted": [], "listed": ["docs"],
                            "prefixes": ["scripts"], "opaque": []},
        "tests/test_b.py": {"sha256": "2", "opened": ["src/x.py"], "statted": ["README.md"], "listed": ["docs"],
                            "prefixes": [], "opaque": ["bash x.sh"], "incomplete": "a skip"},
    }}
    first, second = tmp_path / "1.gz", tmp_path / "2.gz"
    mt.write_manifest(first, manifest)
    assert mt.load_manifest(first) == manifest
    mt.write_manifest(second, mt.load_manifest(first))
    assert first.read_bytes() == second.read_bytes()


# ── replays of develop's real reds (T2 nexus/observability-lookback-2026-11-17, item 1) ──

REPLAYS = json.loads(__import__("gzip").decompress((Path(__file__).parent / "meta_tests_replays.json.gz").read_bytes()))


def test_every_repeat_offender_has_a_replay() -> None:
    """Nine offenders in the T2 record (seven Python, two Java), one replay each. The names stay in
    the fixture: one is a verb's name that a census keeps out of every other file."""
    assert len(REPLAYS) == len({r["offender"] for r in REPLAYS}) == 9
    assert sum(r["file"].endswith(".java") for r in REPLAYS) == 2
    assert all((REPO_ROOT / r["file"]).is_file() for r in REPLAYS)


@pytest.mark.parametrize("replay", REPLAYS, ids=[r["offender"] for r in REPLAYS])
def test_the_diff_of_a_push_that_reddened_develop_selects_the_meta_test_it_broke(replay: dict) -> None:
    """Each replay is a develop push (its CI run is named) whose own change turned the offender
    red, checked to fail at the red commit and pass at its base. Selection runs today's script
    and committed manifest over that push's changed paths, with the push's added files absent
    from the base tree, as they were."""
    now = set(mt.git(REPO_ROOT, "ls-files").split("\n")) - {""}
    added, deleted = set(replay["added"]), set(replay["deleted"])
    files = (now - added) | deleted
    tree = mt.BaseTree(dirs={""} | {d for f in files for d in mt._ancestors(f)}, files=files,
                       now=(now | added) - deleted)
    sel = mt.select(REPO_ROOT, replay["changed"], "HEAD", tree=tree)
    picked = {**sel.selected, **sel.always, **sel.java}
    assert replay["file"] in picked, f"{replay['offender']} (CI run {replay['ci_run']}) not selected"


def test_a_declared_meta_scans_root_stands_in_for_the_opaque_subprocess(repo: Path) -> None:
    _write(repo, {"tests/test_scan.py": LINT + 'META_SCANS = ("src/nexus/pkg", "docs/")\n'})
    manifest = _manifest(repo, test_scan={"opened": ["pyproject.toml"], "opaque": ["bash run.sh"]})
    sel = mt.select(repo, ["src/nexus/pkg/a.py"], "base", manifest)
    assert sel.selected["tests/test_scan.py"] == "enumerates a subtree holding src/nexus/pkg/a.py"
    assert "tests/test_scan.py" not in mt.select(repo, ["src/nexus/mod.py"], "base", manifest).always


def test_every_meta_scans_root_declared_in_the_repo_exists() -> None:
    declared = {t: mt.declared_scans(REPO_ROOT, t) for t in mt.python_meta_tests(REPO_ROOT)}
    missing = {t: [p for p in roots if not (REPO_ROOT / p).exists()] for t, roots in declared.items()}
    assert {t: m for t, m in missing.items() if m} == {}
    assert sum(1 for roots in declared.values() if roots) >= 5


# ── review fixes (code review of 41b5575aa) ──────────────────────────────────


def _built_and_committed(repo: Path, conftest_extra: str = "") -> None:
    _with_tracer(repo)
    _write(repo, {
        "tests/test_imports.py": LINT,
        "tests/conftest.py": CONFTEST + conftest_extra,
    })
    mt.main(["build", "--repo", str(repo), "--jobs", "2"])
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "manifest")
    _git(repo, "tag", "-f", "base")


SKIP_IN_CONFTEST = (
    "import pytest\n"
    "def pytest_runtest_setup(item):\n"
    "    if item.name == 'test_guard':\n"
    "        pytest.skip('session.items shrank')\n"
)


@pytest.mark.parametrize(("conftest_extra", "rc"), [("", 0), (SKIP_IN_CONFTEST, 1)], ids=["passes", "skips-in-conftest"])
def test_the_guard_counts_only_when_it_passes_a_skip_reported_at_the_conftest_fails_the_run(
    repo: Path, uv_is_pytest: None, capsys: pytest.CaptureFixture[str], conftest_extra: str, rc: int
) -> None:
    _built_and_committed(repo, conftest_extra)
    # A sibling beside the census in the guard file: its pass is no evidence the census judged.
    (repo / "tests/test_guard.py").write_text("def test_guard():\n    pass\n\ndef test_sibling():\n    pass\n")
    (repo / "tests/test_plain.py").write_text("def test_p():\n    assert True\n")
    assert mt.main(["run", "--repo", str(repo), "--base", "base"]) == rc
    out = capsys.readouterr().out
    assert "session-items guard with the changed test files ran in" in out
    assert ("the session-items guard did not pass" in out) is bool(rc)


def test_a_failing_meta_test_is_named_in_the_short_summary(
    repo: Path, uv_is_pytest: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_tracer(repo)
    mt.main(["build", "--repo", str(repo), "--jobs", "2"])
    capsys.readouterr()
    (repo / "docs/a.md").write_text("changed\n")
    assert mt.main(["run", "--repo", str(repo), "--base", "base"]) == 1
    assert "FAILED tests/test_imports.py::test_fails" in capsys.readouterr().out


def test_picked_files_whose_meta_tests_are_all_deselected_do_not_redden_the_run(
    repo: Path, uv_is_pytest: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_tracer(repo)
    _write(repo, {"tests/test_imports.py": "import pytest\n\n@pytest.mark.lint\n@pytest.mark.integration\ndef test_i():\n    pass\n"})
    mt.main(["build", "--repo", str(repo), "--jobs", "2"])
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "manifest")
    _git(repo, "tag", "-f", "base")
    # Every pick (the edited files and the integration-only one) holds only deselected meta-tests.
    (repo / "tests/test_scan.py").write_text("import pytest\n\n@pytest.mark.lint\n@pytest.mark.slow\ndef test_s():\n    pass\n")
    (repo / "tests/test_census.py").write_text("import pytest\n\n@pytest.mark.census\n@pytest.mark.slow\ndef test_c():\n    pass\n")
    assert mt.main(["run", "--repo", str(repo), "--base", "base"]) == 0
    assert "no meta-test collected from the picked files" in capsys.readouterr().out


def test_a_changed_path_with_spaces_stays_one_path_and_selects_its_reader(repo: Path) -> None:
    _write(repo, {"dt/scripts/Index Selection in nx.applescript": "a\n"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "spaced")
    (repo / "dt/scripts/Index Selection in nx.applescript").write_text("b\n")
    changed = mt.changed_paths(repo, "base")
    assert changed == ["dt/scripts/Index Selection in nx.applescript"]
    manifest = _manifest(repo, test_scan={"opened": changed})
    assert mt.select(repo, changed, "base", manifest).selected["tests/test_scan.py"] == f"reads {changed[0]}"


def test_a_bare_import_resolves_through_scripts_and_tests_and_joins_the_staleness_hash(repo: Path) -> None:
    _write(repo, {
        "scripts/plugin_channel.py": "V = 1\n",
        "tests/_helper.py": "H = 1\n",
        "tests/test_bare.py": "import pytest\nimport _helper\nfrom plugin_channel import V\npytestmark = pytest.mark.lint\n",
    })
    assert mt.module_files(repo, "tests/test_bare.py") == {"scripts/plugin_channel.py", "tests/_helper.py"}
    manifest = _manifest(repo, test_bare=READS)
    sel = mt.select(repo, ["scripts/plugin_channel.py"], "base", manifest)
    assert sel.selected["tests/test_bare.py"] == "imports scripts/plugin_channel.py"
    assert "tests/_helper.py" in mt.tests_closure(repo, "tests/test_bare.py")
    (repo / "tests/_helper.py").write_text("H = 2\n")
    assert "changed since the manifest" in mt.select(repo, ["tests/_helper.py"], "base", manifest).always["tests/test_bare.py"]


def test_deleting_the_last_file_of_a_directory_reaches_a_test_that_listed_its_parent(repo: Path) -> None:
    _write(repo, {"docs/sub/only.md": "o\n"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "sub")
    _git(repo, "tag", "-f", "base")
    manifest = _manifest(repo, test_scan={"listed": ["docs"]})
    (repo / "docs/sub/only.md").unlink()
    (repo / "docs/sub").rmdir()
    assert mt.select(repo, ["docs/sub/only.md"], "base", manifest).selected["tests/test_scan.py"] == (
        "lists the directory losing docs/sub/only.md")
    _write(repo, {"docs/sub/other.md": "x\n"})  # the directory survives: its parent's listing is unchanged
    assert "tests/test_scan.py" not in mt.select(repo, ["docs/sub/only.md"], "base", manifest).selected


def test_the_tracer_sees_a_repo_command_run_from_a_temporary_cwd_and_other_spawn_calls(
    repo: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _write(repo, {
        "tests/__init__.py": "",
        "tests/_surface_trace.py": (REPO_ROOT / "tests/_surface_trace.py").read_text(),
        "scripts/tool.sh": "true\n",
        "tests/test_traced.py": (
            "import os, subprocess, sys\nimport pytest\n"
            "pytestmark = pytest.mark.lint\n"
            f"ELSEWHERE = {str(elsewhere)!r}\n"
            "REPO = os.getcwd()\n"
            "def test_from_elsewhere():\n"
            "    subprocess.run(['git', '-C', REPO, 'ls-files', '--', 'docs'], cwd=ELSEWHERE, check=True, capture_output=True)\n"
            "    subprocess.run(['sh', os.path.join(REPO, 'scripts/tool.sh')], cwd=ELSEWHERE, check=True)\n"
            "    subprocess.run(['true'], cwd=ELSEWHERE, check=True)\n"
            "    os.system('true')\n"
        ),
    })
    out = tmp_path / "trace"
    env = {**os.environ, "NX_TEST_T2_SUBSTRATE": "none", "NX_SURFACE_TRACE_OUT": str(out),
           "PYTHONPATH": str(repo / "src") + os.pathsep + str(repo)}
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "-p", "no:cacheprovider", "-p", "tests._surface_trace",
         "--rootdir", str(repo), "-c", os.devnull, "tests/test_traced.py"],
        cwd=repo, env=env, capture_output=True, text=True,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    [frag] = list(out.glob("trace-*.json"))
    got = json.loads(frag.read_text())["tests/test_traced.py"]
    assert got["prefixes"] == ["docs"]
    assert any("scripts/tool.sh" in o for o in got["opaque"]), got["opaque"]
    assert "sh -c true" in got["opaque"]  # os.system runs from the repo cwd
    assert not any(o == "true" for o in got["opaque"])  # nothing of the repo in it, run from elsewhere


def test_select_reads_changed_names_from_stdin_one_per_line_or_nul_separated(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io
    for data in ("docs/a b.md\ndocs/c.md\n", "docs/a b.md\0docs/c.md\0"):
        monkeypatch.setattr(sys, "stdin", io.StringIO(data))
        mt.main(["select", "--repo", str(repo), "--base", "base", "--names-from-stdin", "--json"])
        assert json.loads(capsys.readouterr().out)["changed"] == ["docs/a b.md", "docs/c.md"]


#: A stand-in for tests/conftest.py's NX_CENSUS_ONLY_JOB: skip-mark (never deselect) every item
#: outside the guard module, so the census still sees them collected.
CENSUS_ONLY_IN_CONFTEST = (
    "import os, pytest\n"
    "def pytest_collection_modifyitems(config, items):\n"
    "    if os.environ.get('NX_CENSUS_ONLY_JOB') == '1':\n"
    "        for item in items:\n"
    "            if not item.nodeid.startswith('tests/test_guard.py::'):\n"
    "                item.add_marker(pytest.mark.skip(reason='census-only'))\n"
)


def test_the_changed_test_files_are_collected_for_the_guard_and_not_run(
    repo: Path, uv_is_pytest: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _built_and_committed(repo, CENSUS_ONLY_IN_CONFTEST)
    (repo / "tests/test_plain.py").write_text("def test_p():\n    assert False, 'needs the engine'\n")
    assert mt.main(["run", "--repo", str(repo), "--base", "base"]) == 0
    assert "FAILED tests/test_plain.py" not in capsys.readouterr().out


def test_a_session_finish_guard_forcing_a_red_fails_the_run_when_the_census_passes(
    repo: Path, uv_is_pytest: None, capsys: pytest.CaptureFixture[str]
) -> None:
    # tests/conftest.py's sessionfinish guards set exitstatus 1 and name no node.
    forced = ("def pytest_sessionfinish(session):\n"
              "    print('FAIL: a sessionfinish guard')\n"
              "    session.exitstatus = 1\n")
    _built_and_committed(repo, CENSUS_ONLY_IN_CONFTEST)
    (repo / "tests/conftest.py").write_text(CONFTEST + CENSUS_ONLY_IN_CONFTEST + forced)
    (repo / "tests/test_plain.py").write_text("def test_p():\n    assert False\n")
    assert mt.main(["run", "--repo", str(repo), "--base", "base"]) == 1
    assert "FAIL: a sessionfinish guard" in capsys.readouterr().out


def test_a_scripts_module_imported_through_a_tests_helper_selects_the_test(repo: Path) -> None:
    _write(repo, {
        "scripts/checker.py": "C = 1\n",
        "tests/_helper.py": "from checker import C\n",
        "tests/test_bare.py": "import pytest\nimport _helper\npytestmark = pytest.mark.lint\n",
    })
    manifest = _manifest(repo, test_bare=READS)
    assert mt.select(repo, ["scripts/checker.py"], "base", manifest).selected["tests/test_bare.py"] == (
        "imports scripts/checker.py")


def test_a_bare_name_present_in_two_roots_yields_both_files(repo: Path) -> None:
    _write(repo, {"scripts/twin.py": "", "tests/twin.py": "", "tests/test_twin.py": "import twin\n"})
    assert mt.module_files(repo, "tests/test_twin.py") == {"scripts/twin.py", "tests/twin.py"}


@pytest.mark.parametrize("where", ["guard-sibling", "changed-guard-file"])
def test_a_failure_outside_the_changed_test_files_stays_red_when_the_census_passes(
    repo: Path, uv_is_pytest: None, capsys: pytest.CaptureFixture[str], where: str
) -> None:
    _built_and_committed(repo)
    if where == "guard-sibling":
        # A ratchet beside the census in the guard file: not engine-bound, and run nowhere else.
        (repo / "tests/test_guard.py").write_text("def test_guard():\n    pass\n\ndef test_ratchet():\n    assert False\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "ratchet")
        _git(repo, "tag", "-f", "base")
    (repo / "tests/test_plain.py").write_text("def test_p():\n    assert True\n")
    if where == "changed-guard-file":
        (repo / "tests/test_guard.py").write_text("def test_guard():\n    pass\n\ndef test_ratchet():\n    assert False\n")
    assert mt.main(["run", "--repo", str(repo), "--base", "base"]) == 1
    assert "advisory" not in capsys.readouterr().out


def test_a_relative_import_resolves_from_the_test_package(repo: Path) -> None:
    _write(repo, {"tests/auras/__init__.py": "", "tests/auras/_support.py": "S = 1\n",
                  "tests/auras/test_a.py": "from ._support import S\nfrom . import _support\n"})
    assert mt.module_files(repo, "tests/auras/test_a.py") == {"tests/auras/_support.py"}
    assert "tests/auras/_support.py" in mt.tests_closure(repo, "tests/auras/test_a.py")
    _write(repo, {"tests/auras/deep/__init__.py": "", "tests/auras/deep/test_b.py": "from .._support import S\nfrom ..pkg.mod import M\n",
                  "tests/auras/pkg/__init__.py": "", "tests/auras/pkg/mod.py": "M = 1\n"})
    assert mt.module_files(repo, "tests/auras/deep/test_b.py") == {
        "tests/auras/_support.py", "tests/auras/pkg/__init__.py", "tests/auras/pkg/mod.py"}
