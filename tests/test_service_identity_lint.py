# SPDX-License-Identifier: AGPL-3.0-or-later
"""``os.getuid()`` appears only inside the service-identity helper (RDR-224 Gap 3, nexus-f9bgu.16).

``os.getuid`` does not exist on Windows, so every call outside
``service_identity()`` is a site where the supervisor fails before it starts
anything. The helper (and its stdlib mirror in the conexus plugin script, which
cannot import nexus) is the one place allowed to call it.

The scan is by AST attribute NAME, not by ``os.getuid`` spelling: the repo
already aliases the module (``import os as _os`` in ``commands/daemon.py``), and
a lint keyed on the literal ``os.`` prefix is blind to that. It also flags
``from os import getuid`` and the string form (``hasattr(os, "getuid")``,
``getattr(os, "getuid")``), which is how ``mailbox_drain.py`` hid one. Comments
and docstrings are not code and never match.

Exemptions are keyed on (file, enclosing top-level function) with an exact
count, so a moved or removed exemption fails as loudly as a new violation.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO = Path(__file__).parent.parent
#: Code roots swept: the wheel and the plugin's hook scripts.
SCAN_ROOTS = ("src/nexus", "conexus/hooks/scripts")

#: The helper and its stdlib mirror: exactly one call each.
HELPERS: dict[tuple[str, str], int] = {
    ("src/nexus/daemon/service_registry.py", "service_identity"): 1,
    ("conexus/hooks/scripts/_endpoint_resolve.py", "service_identity"): 1,
}

#: Sites that need the raw POSIX uid on purpose, each with its reason.
EXEMPT: dict[tuple[str, str], int] = {
    # launchctl's gui/<uid> domain: macOS only, the number IS the POSIX uid, never a Windows identity.
    ("src/nexus/daemon/installer.py", "_activate_cmd"): 1,
    ("src/nexus/daemon/installer.py", "_deactivate_cmd"): 1,
    ("src/nexus/daemon/installer.py", "_activation_query_cmd"): 1,
    ("src/nexus/daemon/installer.py", "autostart_activation_state"): 1,
    ("src/nexus/daemon/installer.py", "_launchd_loaded_now"): 1,
    # pwd.getpwuid(os.getuid()): behind `import pwd`, whose ImportError arm IS the non-POSIX branch.
    ("src/nexus/db/onnx_model_root.py", "_home_base"): 1,
}

#: Non-vacuity floor: files the sweep must read (the wheel alone has several hundred).
MIN_FILES = 200


def _sites(source: str) -> list[tuple[int, str]]:
    """(line, enclosing top-level def or ``<module>``) of every getuid reference in *source*."""
    tree = ast.parse(source)
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if scope == "<module>" and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inner = child.name
            elif scope == "<module>" and isinstance(child, ast.ClassDef):
                inner = child.name
            hit = (
                (isinstance(child, ast.Attribute) and child.attr == "getuid")
                or (isinstance(child, ast.ImportFrom) and any(a.name == "getuid" for a in child.names))
                or (isinstance(child, ast.Constant) and child.value == "getuid")
            )
            if hit:
                found.append((child.lineno, inner))
            visit(child, inner)

    visit(tree, "<module>")
    return found


def _scan(root: Path) -> tuple[int, dict[tuple[str, str], list[int]]]:
    files = 0
    out: dict[tuple[str, str], list[int]] = {}
    for rel_root in SCAN_ROOTS:
        for path in sorted((root / rel_root).rglob("*.py")):
            files += 1
            rel = path.relative_to(root).as_posix()
            for line, scope in _sites(path.read_text(encoding="utf-8")):
                out.setdefault((rel, scope), []).append(line)
    return files, out


def _violations(found: dict[tuple[str, str], list[int]]) -> list[str]:
    allowed = {**HELPERS, **EXEMPT}
    bad = []
    for key, lines in sorted(found.items()):
        if len(lines) != allowed.get(key, 0):
            bad.append(f"{key[0]}:{key[1]} has getuid at lines {lines}, allowed {allowed.get(key, 0)}")
    for key, count in sorted(allowed.items()):
        if key not in found:
            bad.append(f"{key[0]}:{key[1]} is allowed {count} getuid call(s) but has none (stale exemption)")
    return bad


def test_getuid_is_only_in_the_helper_and_the_named_exemptions() -> None:
    files, found = _scan(REPO)
    assert files >= MIN_FILES, f"the sweep read only {files} files; the roots are wrong or the tree moved"
    # Non-vacuity: the scan sees the helper's own call, so a blind scan cannot pass this test.
    for key in HELPERS:
        assert found.get(key), f"the scan did not find the helper's own getuid call at {key}"
    assert not _violations(found), (
        "os.getuid() outside service_identity(): route it through "
        "nexus.daemon.service_registry.service_identity() (RDR-224 Gap 3).\n" + "\n".join(_violations(found))
    )


@pytest.mark.parametrize(
    "shape",
    [
        "import os\ndef f():\n    return os.getuid()\n",
        "import os as _os\ndef f():\n    return str(_os.getuid())\n",
        "from os import getuid\ndef f():\n    return getuid()\n",
        "import os\ndef f():\n    return hasattr(os, 'getuid')\n",
        "import os\nX = getattr(os, 'getuid')\n",
        "import os\ndef f():\n    g = os.getuid\n    return g()\n",
    ],
)
def test_the_scan_sees_each_way_a_call_could_be_spelled(shape: str) -> None:
    """Positive controls: aliasing, import-from, the string forms and a bare reference are all found."""
    assert _sites(shape)


def test_comments_and_docstrings_are_not_code() -> None:
    src = 'def f():\n    """Calls os.getuid() on POSIX."""\n    # os.getuid() again\n    return 1\n'
    assert _sites(src) == []


def test_a_planted_violation_fails_the_sweep(tmp_path: Path) -> None:
    """Plant a violation in a copy of the tree layout and require the sweep to name it, then to pass once removed."""
    pkg = tmp_path / "src" / "nexus"
    pkg.mkdir(parents=True)
    (tmp_path / "conexus" / "hooks" / "scripts").mkdir(parents=True)
    (pkg / "ok.py").write_text("def f():\n    return 1\n")
    _, clean = _scan(tmp_path)
    assert clean == {}
    (pkg / "planted.py").write_text("import os\n\ndef scope():\n    return str(os.getuid())\n")
    _, planted = _scan(tmp_path)
    assert planted == {("src/nexus/planted.py", "scope"): [4]}
    assert any("planted.py:scope" in v for v in _violations(planted)), "the planted site must be reported by name"


# ---------------------------------------------------------------------------
# Test-support modules (RDR-224, nexus-f9bgu.19).
#
# ``tests/conftest.py`` imports ``tests/_engine_substrate.py`` at collection
# start, and that module called ``os.getuid()`` at import, so a native Windows
# pytest run died before collecting a test. The sweep above covers the wheel and
# the plugin scripts, not the harness. Test FILES (``test_*.py``) are not swept:
# a ``getuid`` inside one test body fails that test on Windows, not the run.
# What must hold is that the modules every run imports carry none.
# ---------------------------------------------------------------------------

TEST_SUPPORT_GLOBS = ("tests/conftest.py", "tests/*/conftest.py", "tests/_*.py")

#: Raw ``getuid`` the test-support modules may still call, each with its reason.
TEST_SUPPORT_EXEMPT: dict[tuple[str, str], int] = {
    # Inside the `shutil.which("launchctl")` branch: the macOS service-manager snapshot,
    # where the number IS the POSIX uid. Never reached on Windows.
    ("tests/conftest.py", "_snapshot_manager_state"): 1,
}

#: The sweep must read at least this many helper modules (there are dozens).
MIN_TEST_SUPPORT_FILES = 10


def _scan_test_support(root: Path) -> tuple[int, dict[tuple[str, str], list[int]]]:
    paths = sorted({p for pattern in TEST_SUPPORT_GLOBS for p in root.glob(pattern)})
    out: dict[tuple[str, str], list[int]] = {}
    for path in paths:
        rel = path.relative_to(root).as_posix()
        for line, scope in _sites(path.read_text(encoding="utf-8")):
            out.setdefault((rel, scope), []).append(line)
    return len(paths), out


def test_test_support_modules_call_getuid_nowhere_but_the_named_exemptions() -> None:
    files, found = _scan_test_support(REPO)
    assert files >= MIN_TEST_SUPPORT_FILES, f"the sweep read only {files} test-support files"
    bad = []
    for key, lines in sorted(found.items()):
        if len(lines) != TEST_SUPPORT_EXEMPT.get(key, 0):
            bad.append(f"{key[0]}:{key[1]} has getuid at lines {lines}, allowed {TEST_SUPPORT_EXEMPT.get(key, 0)}")
    for key, count in sorted(TEST_SUPPORT_EXEMPT.items()):
        if key not in found:
            bad.append(f"{key[0]}:{key[1]} is allowed {count} getuid call(s) but has none (stale exemption)")
    assert not bad, (
        "os.getuid() in a module every pytest run imports kills a native Windows run before it "
        "collects a test: use service_identity().\n" + "\n".join(bad)
    )


def test_the_test_support_scan_finds_a_planted_import_time_call(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "conftest.py").write_text("import os\nUID = os.getuid()\n")
    for i in range(MIN_TEST_SUPPORT_FILES):
        (tmp_path / "tests" / f"_helper{i}.py").write_text("X = 1\n")
    files, found = _scan_test_support(tmp_path)
    assert files == MIN_TEST_SUPPORT_FILES + 1
    assert found == {("tests/conftest.py", "<module>"): [2]}
