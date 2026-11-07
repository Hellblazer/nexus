# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/tenant_delta.py (nexus-g37hn): the tenant-minting tests a branch adds, counted at HEAD and
at the merge base, against a throwaway git repo with its own pytest config."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from tests._module_seam import setattr_in

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "tenant_delta.py"

_PYPROJECT = """[tool.pytest.ini_options]
markers = ["t2_tenant: mints a tenant"]
"""

_CONFTEST = """import pytest

@pytest.fixture
def t2_tenant():
    return "tenant"

@pytest.fixture
def t2_service_env(t2_tenant):
    return t2_tenant
"""

_CEILING = """[python_unit]
provisioned_tests = 10
tolerance_tests = 0
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _commit(repo: Path, files: dict[str, str], message: str) -> None:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "develop")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _commit(repo, {
        "pyproject.toml": _PYPROJECT,
        "tests/__init__.py": "",
        "tests/conftest.py": _CONFTEST,
        "tests/storage_touch_ceiling.toml": _CEILING,
        "tests/test_a.py": "import pytest\n\n@pytest.mark.t2_tenant\ndef test_one():\n    pass\n\n"
                           "@pytest.mark.t2_tenant\ndef test_two():\n    pass\n",
        "tests/test_b.py": "def test_free():\n    pass\n",
    }, "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    return repo


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, NX_NO_TELEMETRY="1")
    return subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), "--base", "develop", *args],
                          capture_output=True, text=True, env=env, timeout=300)


def test_counts_a_marker_and_the_alias_fixture_and_nets_a_removal(repo: Path) -> None:
    _commit(repo, {
        # test_a loses test_two (-1); test_b gains one by marker and one through t2_service_env (+2).
        "tests/test_a.py": "import pytest\n\n@pytest.mark.t2_tenant\ndef test_one():\n    pass\n",
        "tests/test_b.py": "import pytest\n\ndef test_free():\n    pass\n\n"
                           "@pytest.mark.t2_tenant\ndef test_marked():\n    pass\n\n"
                           "def test_alias(t2_service_env):\n    pass\n",
    }, "head")
    proc = _run(repo)
    assert proc.returncode == 0, proc.stderr
    assert "tests/test_a.py: 2 -> 1 (-1)" in proc.stdout
    assert "tests/test_b.py: 0 -> 2 (+2)" in proc.stdout
    assert "tenant delta: +1 test(s)" in proc.stdout and "ceiling 10" in proc.stdout
    assert _run(repo, "--strict").returncode == 1


def test_a_new_module_counts_from_zero_and_a_deleted_one_to_zero(repo: Path) -> None:
    (repo / "tests" / "test_a.py").unlink()
    _commit(repo, {"tests/test_c.py": "import pytest\n\n@pytest.mark.t2_tenant\ndef test_c():\n    pass\n"},
            "head")
    proc = _run(repo)
    assert "tests/test_a.py: 2 -> 0 (-2)" in proc.stdout
    assert "tests/test_c.py: 0 -> 1 (+1)" in proc.stdout
    assert "tenant delta: -1 test(s)" in proc.stdout
    assert _run(repo, "--strict").returncode == 0


def test_a_branch_that_touches_no_test_module_says_so_or_stays_quiet(repo: Path) -> None:
    _commit(repo, {"README.md": "x\n"}, "head")
    assert "no test module changed" in _run(repo).stdout
    quiet = _run(repo, "--quiet-if-zero")
    assert quiet.returncode == 0 and quiet.stdout == ""


def test_a_tenant_free_change_is_quiet_under_quiet_if_zero(repo: Path) -> None:
    _commit(repo, {"tests/test_b.py": "def test_free():\n    pass\n\ndef test_more():\n    pass\n"}, "head")
    quiet = _run(repo, "--quiet-if-zero")
    assert quiet.returncode == 0 and quiet.stdout == ""


def test_a_module_that_fails_to_collect_is_refused_not_counted_as_zero(repo: Path) -> None:
    """A collection error must not read as a module with no tenant tests."""
    _commit(repo, {"tests/test_b.py": "import no_such_module_anywhere\n\ndef test_free():\n    pass\n"},
            "head")
    proc = _run(repo)
    assert proc.returncode == 2
    assert "could not count" in proc.stderr


def test_the_base_side_is_collected_against_the_base_tree_not_head_s(repo: Path) -> None:
    """nexus-l35bq: the base's tests run against the base's own src/ and see every file of the base tree,
    so a test that imports base code head has changed, or reads a non-test file while collecting, counts."""
    _git(repo, "checkout", "-q", "develop")
    _commit(repo, {
        "src/pkgx/__init__.py": "def old():\n    return 1\n",
        "data/golden.yaml": "x: 1\n",
        "tests/test_d.py": "from pathlib import Path\nimport pytest\nfrom pkgx import old\n\n"
                           "GOLDEN = (Path(__file__).parents[1] / 'data' / 'golden.yaml').read_text()\n\n"
                           "@pytest.mark.t2_tenant\ndef test_d():\n    assert old() == 1\n",
    }, "base with src and data")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "rebase", "-q", "develop")
    _commit(repo, {
        "src/pkgx/__init__.py": "def new():\n    return 2\n",
        "tests/test_d.py": "from pathlib import Path\nimport pytest\nfrom pkgx import new\n\n"
                           "GOLDEN = (Path(__file__).parents[1] / 'data' / 'golden.yaml').read_text()\n\n"
                           "@pytest.mark.t2_tenant\ndef test_d():\n    assert new() == 2\n\n"
                           "@pytest.mark.t2_tenant\ndef test_e():\n    pass\n",
    }, "head renames old to new")
    proc = _run(repo)
    assert proc.returncode == 0, proc.stderr
    assert "tests/test_d.py: 1 -> 2 (+1)" in proc.stdout


def test_a_module_the_base_cannot_collect_is_named_and_the_rest_still_count(repo: Path) -> None:
    """A base module that will not collect even against its own tree (a dependency head changed, say)
    is named with its head count and the reason; the other modules are counted, and the run is advisory."""
    _git(repo, "checkout", "-q", "develop")
    _commit(repo, {"tests/test_b.py": "import no_such_module_at_base\n\ndef test_free():\n    pass\n"},
            "a base module that cannot be collected")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "rebase", "-q", "develop")
    _commit(repo, {
        "tests/test_a.py": "import pytest\n\n@pytest.mark.t2_tenant\ndef test_one():\n    pass\n",
        "tests/test_b.py": "import pytest\n\n@pytest.mark.t2_tenant\ndef test_marked():\n    pass\n",
    }, "head")
    proc = _run(repo)
    assert proc.returncode == 0, proc.stderr
    assert "tests/test_a.py: 2 -> 1 (-1)" in proc.stdout
    assert "WARNING: tests/test_b.py: not counted at the merge base, 1 at head" in proc.stdout
    assert "(ModuleNotFoundError: No module named 'no_such_module_at_base')" in proc.stdout, proc.stdout
    assert _run(repo, "--strict").returncode == 1, "a rise cannot be ruled out where the base was not counted"


def test_a_base_unpacked_under_a_symlinked_temp_dir_attributes_a_failing_module(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On macOS the temp dir sits under /var, a symlink to /private/var. Unpacked there unresolved, pytest could not
    relate a failing module to the rootdir and reported the failure against the whole session, so every module fell
    back to a collection of its own (nexus-l35bq: 30 s and over the push budget). Resolved, the one pass names it."""
    spec = importlib.util.spec_from_file_location("tenant_delta_under_test", SCRIPT)
    td = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(td)
    real = tmp_path / "real-tmp"
    real.mkdir()
    link = tmp_path / "link-tmp"
    link.symlink_to(real)
    monkeypatch.setattr(tempfile, "tempdir", str(link))
    monkeypatch.setenv("NX_NO_TELEMETRY", "1")

    def no_fallback(*_a, **_k):
        raise AssertionError("the whole pass failed, so every module was collected alone")

    monkeypatch.setattr(td, "_one_by_one", no_fallback)
    _git(repo, "checkout", "-q", "develop")
    _commit(repo, {"tests/test_b.py": "import no_such_module_at_base\n\ndef test_free():\n    pass\n"}, "bad base")
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    counts, failed, _skipped = td.base_census(repo, base, ["tests/test_a.py", "tests/test_b.py"],
                                    deadline=time.monotonic() + 120)
    assert counts == {"tests/test_a.py": 2}
    assert list(failed) == ["tests/test_b.py"] and "no_such_module_at_base" in failed["tests/test_b.py"]


def test_a_collection_cut_short_before_the_census_is_refused_not_read_as_zero(repo: Path) -> None:
    """nexus-l35bq review r2: when an earlier modifyitems hook raises, pytest skips the census hook. The census is
    then missing, never an empty one that would read every module as having no tenant tests."""
    _commit(repo, {
        "tests/conftest.py": _CONFTEST + "\n\ndef pytest_collection_modifyitems(items):\n    raise RuntimeError('boom')\n",
        "tests/test_a.py": "import pytest\n\n@pytest.mark.t2_tenant\ndef test_one():\n    pass\n",
    }, "head with a raising hook")
    proc = _run(repo)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "WARNING: tenant delta: could not count" in proc.stderr


@pytest.mark.parametrize("written", [
    {"tests/test_a.py": 2},  # the old plugin's bare mapping
    {"complete": False, "counts": {}, "failed": {}},
    {"counts": {}, "failed": {}},
])
def test_a_census_that_is_not_complete_is_refused(repo: Path, monkeypatch: pytest.MonkeyPatch, written: dict) -> None:
    """The reader's own guard, apart from the missing-file case: an incomplete or old-format census is refused."""
    spec = importlib.util.spec_from_file_location("tenant_delta_guard", SCRIPT)
    td = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(td)
    real_run = td.subprocess.run

    def run(argv, **kw):
        proc = real_run(argv, **kw)
        if "--collect-only" in argv:
            Path(kw["env"]["TENANT_CENSUS_OUT"]).write_text(json.dumps(written), encoding="utf-8")
        return proc

    setattr_in(monkeypatch, td, "subprocess.run", run)
    with pytest.raises(td.CollectionFailed):
        td.census(repo, ["tests/test_a.py"], deadline=time.monotonic() + 60)


def test_a_failure_that_covers_no_module_asked_for_is_refused(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed collector the reader cannot place (a nodeid owning none of the modules asked for) would leave the
    modules it hid reading as zero; it is refused instead."""
    spec = importlib.util.spec_from_file_location("tenant_delta_unowned", SCRIPT)
    td = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(td)
    real_run = td.subprocess.run

    def run(argv, **kw):
        proc = real_run(argv, **kw)
        if "--collect-only" in argv:
            Path(kw["env"]["TENANT_CENSUS_OUT"]).write_text(json.dumps(
                {"complete": True, "counts": {}, "failed": {"tests/__init__.py": "E"}}), encoding="utf-8")
        return proc

    setattr_in(monkeypatch, td, "subprocess.run", run)
    with pytest.raises(td.CollectionFailed):
        td.census(repo, ["tests/test_a.py"], deadline=time.monotonic() + 60)


def test_a_failed_package_covers_only_the_modules_under_it() -> None:
    spec = importlib.util.spec_from_file_location("tenant_delta_owner", SCRIPT)
    td = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(td)
    failed = {"tests/hooks": "ImportError: x"}
    assert td._owner("tests/hooks/test_a.py", failed) == "ImportError: x"
    assert td._owner("tests/hooks_extra/test_b.py", failed) is None
    assert td._owner("tests/hooks", failed) == "ImportError: x"
    assert td._owner("tests/test_c.py", {"tests/test_c.py::TestX": "E"}) == "E"


def test_a_count_over_its_budget_is_refused_and_says_so(repo: Path) -> None:
    _commit(repo, {"tests/test_b.py": "import pytest\n\n@pytest.mark.t2_tenant\ndef test_m():\n    pass\n"},
            "head")
    proc = _run(repo, "--budget-s", "0.5")
    assert proc.returncode == 2
    assert "not counted" in proc.stderr or "could not count" in proc.stderr


def test_a_deselected_test_is_not_counted(repo: Path) -> None:
    """The census runs after -m deselection: a test the default selection drops is not counted."""
    _commit(repo, {
        "pyproject.toml": _PYPROJECT.replace(
            'markers = ["t2_tenant: mints a tenant"]',
            'markers = ["t2_tenant: mints a tenant", "integration: e2e"]\naddopts = "-m \'not integration\'"'),
        "tests/test_b.py": "import pytest\n\n@pytest.mark.integration\n@pytest.mark.t2_tenant\n"
                           "def test_e2e():\n    pass\n",
    }, "head")
    proc = _run(repo)
    assert proc.returncode == 0, proc.stderr
    assert "tests/test_b.py" not in proc.stdout


# --- nexus-xxuvz: a module skipped at collection is uncounted, on either side, and never refused -----------------------

_SKIPPED_AT_COLLECTION = "import pytest\n\npytest.importorskip('no_such_module_anywhere')\n\n"


def test_the_census_returns_a_collection_skipped_module_apart_from_the_failed_ones(repo: Path) -> None:
    td = _load("tenant_delta_skipped")
    _commit(repo, {"tests/test_b.py": _SKIPPED_AT_COLLECTION + "@pytest.mark.t2_tenant\ndef test_m():\n    pass\n"},
            "head")
    counts, failed, skipped = td.census(repo, ["tests/test_a.py", "tests/test_b.py"],
                                        deadline=time.monotonic() + 120)
    assert counts == {"tests/test_a.py": 2}
    assert failed == {}
    assert list(skipped) == ["tests/test_b.py"] and "no_such_module_anywhere" in skipped["tests/test_b.py"]


def test_a_module_skipped_at_head_with_a_base_count_is_a_warning_not_a_drop_to_zero(repo: Path) -> None:
    """test_a counts 2 at the base; skipped at collection at head it would read 2 -> 0 (-2). It is named as a
    WARNING instead (a count the head hides), left out of the delta, and --strict fails on it as on the base side."""
    _commit(repo, {"tests/test_a.py": _SKIPPED_AT_COLLECTION + "@pytest.mark.t2_tenant\ndef test_one():\n    pass\n"},
            "head")
    proc = _run(repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "tests/test_a.py: 2 -> 0" not in proc.stdout
    assert "WARNING: tests/test_a.py: skipped at collection at head, 2 at the merge base" in proc.stdout
    assert "no_such_module_anywhere" in proc.stdout
    assert "tenant delta: +0 test(s)" in proc.stdout and "1 skipped at collection" in proc.stdout
    assert _run(repo, "--strict").returncode == 1
    assert _run(repo, "--quiet-if-zero").stdout != "", "a hidden base count is not silent under --quiet-if-zero"


def test_a_module_skipped_at_head_with_no_base_count_is_a_note_and_not_strict(repo: Path) -> None:
    _commit(repo, {"tests/test_b.py": _SKIPPED_AT_COLLECTION + "@pytest.mark.t2_tenant\ndef test_m():\n    pass\n"},
            "head")
    proc = _run(repo, "--strict")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "NOTE: tests/test_b.py: skipped at collection at head" in proc.stdout
    assert "WARNING" not in proc.stdout


def test_a_module_skipped_at_the_base_with_tenant_tests_at_head_is_named_and_cannot_rule_out_a_rise(
        repo: Path) -> None:
    _git(repo, "checkout", "-q", "develop")
    _commit(repo, {"tests/test_b.py": _SKIPPED_AT_COLLECTION + "def test_free():\n    pass\n"}, "base skips test_b")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "rebase", "-q", "develop")
    _commit(repo, {"tests/test_b.py": "import pytest\n\n@pytest.mark.t2_tenant\ndef test_m():\n    pass\n"}, "head")
    proc = _run(repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "tests/test_b.py: 0 -> 1" not in proc.stdout
    assert "WARNING: tests/test_b.py: skipped at collection at the merge base, 1 at head" in proc.stdout
    assert "no_such_module_anywhere" in proc.stdout
    assert _run(repo, "--strict").returncode == 1


def test_a_module_skipped_on_both_sides_is_quiet_under_quiet_if_zero_and_never_refused(repo: Path) -> None:
    """A platform-conditional skip holds on every push: it must not say 'could not count' (exit 2) each time."""
    text = _SKIPPED_AT_COLLECTION + "def test_free():\n    pass\n"
    _git(repo, "checkout", "-q", "develop")
    _commit(repo, {"tests/test_b.py": text}, "base skips test_b")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "rebase", "-q", "develop")
    _commit(repo, {"tests/test_b.py": text + "\ndef test_more():\n    pass\n"}, "head")
    quiet = _run(repo, "--quiet-if-zero", "--strict")
    assert quiet.returncode == 0 and quiet.stdout == "" and quiet.stderr == "", quiet.stdout + quiet.stderr
    loud = _run(repo, "--strict")
    assert loud.returncode == 0 and "could not count" not in loud.stderr
    assert "skipped at collection" in loud.stdout
