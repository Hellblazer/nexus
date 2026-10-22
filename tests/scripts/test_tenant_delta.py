# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/tenant_delta.py (nexus-g37hn): the tenant-minting tests a branch adds, counted at HEAD and
at the merge base, against a throwaway git repo with its own pytest config."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

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
