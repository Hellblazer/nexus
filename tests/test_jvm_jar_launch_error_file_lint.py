# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every ``java ... -jar`` launch in src/, tests/ and scripts/ must redirect its hs_err report.

WHY (nexus-o5xyx.2). A JVM that crashes writes ``hs_err_pid<N>.log`` into its cwd
unless ``-XX:ErrorFile=`` says otherwise. The engine JVM does crash (an ONNX Runtime
SEGV when SIGTERM lands during model init, nexus-o5xyx), and from a test run the cwd is
the repo checkout, so crash reports piled up in the tree. The flag is a per-launch
argument, so the only way to keep it everywhere is to check every launch site.

WHAT IT CHECKS.
- Python: every list literal containing the string ``"-jar"`` must, in the same list,
  carry an element that is ``-XX:ErrorFile=...`` (a literal or f-string) or a call to
  ``jvm_error_file_arg()``. Route test launches through ``tests.db._service_fixture.jar_argv``
  or ``tests._engine_substrate.engine_argv`` rather than spelling ``"-jar"`` out.
- Shell (tests/ and src/): a non-comment logical line that runs ``java`` with ``-jar``
  must mention ``-XX:ErrorFile=``.

A native image is not a JVM and takes no such flag, so it has no launch site here.

"""

from __future__ import annotations

import ast
import re
import warnings
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ROOTS = (_REPO_ROOT / "src", _REPO_ROOT / "tests", _REPO_ROOT / "scripts")
_FLAG = "-XX:ErrorFile="
_HELPERS = frozenset({"jvm_error_file_arg"})

#: Files whose ``"-jar"`` is data, not a launch: this lint's own fixtures.
_SELF = Path(__file__).resolve()


def _files(suffix: str) -> list[Path]:
    out: list[Path] = []
    for root in _ROOTS:
        for p in root.rglob(f"*{suffix}"):
            if ".venv" in p.parts or "node_modules" in p.parts or "__pycache__" in p.parts:
                continue
            out.append(p)
    return sorted(out)


def _is_flag(node: ast.expr) -> bool:
    if isinstance(node, ast.Starred):
        node = node.value
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.startswith(_FLAG)
    if isinstance(node, ast.JoinedStr):
        first = node.values[0] if node.values else None
        return (
            isinstance(first, ast.Constant)
            and isinstance(first.value, str)
            and first.value.startswith(_FLAG)
        )
    if isinstance(node, ast.Call):
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
        return name in _HELPERS
    return False


#: ``java`` in COMMAND position: at the start of a command (line start or after
#: ``; & | (``), optionally after ``env``, env assignments and ``exec``. A word "java" inside a
#: message or comment is prose, not a launch.
_SHELL_JAVA_CMD = re.compile(
    r"(?:^|[;&|(])\s*(?:env\s+)?(?:\w+=(?:\"[^\"]*\"|'[^']*'|\S*)\s+)*(?:exec\s+)?java\s"
)


def python_violations(source: str, label: str) -> list[str]:
    """Each list literal with a ``"-jar"`` element and no ErrorFile element."""
    out: list[str] = []
    with warnings.catch_warnings():
        # Scanning other files' source: their escape-sequence warnings are not ours.
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.List):
            continue
        has_jar = any(
            isinstance(e, ast.Constant) and e.value == "-jar" for e in node.elts
        )
        if has_jar and not any(_is_flag(e) for e in node.elts):
            out.append(f"{label}:{node.lineno}")
    return out


def shell_violations(text: str, label: str) -> list[str]:
    """Each logical shell line that runs ``java`` with ``-jar`` and no ErrorFile."""
    out: list[str] = []
    joined = re.sub(r"\\\n", " ", text)
    # Same joining for line numbers: report the first physical line of each logical line.
    for lineno, line in enumerate(joined.splitlines(), start=1):
        body = line.strip()
        if body.startswith("#"):
            continue
        if _SHELL_JAVA_CMD.search(body) and re.search(r"\s-jar\b", body):
            if _FLAG not in body:
                out.append(f"{label}:{lineno}")
    return out


def _scan_python() -> list[str]:
    found: list[str] = []
    for p in _files(".py"):
        if p.resolve() == _SELF:
            continue
        found += python_violations(p.read_text(encoding="utf-8"), str(p.relative_to(_REPO_ROOT)))
    return found


def _scan_shell() -> list[str]:
    found: list[str] = []
    for p in _files(".sh"):
        found += shell_violations(p.read_text(encoding="utf-8"), str(p.relative_to(_REPO_ROOT)))
    return found


def test_every_python_jar_launch_redirects_hs_err() -> None:
    bad = _scan_python()
    assert not bad, (
        "a `-jar` argv without -XX:ErrorFile lets a crashing JVM drop hs_err_pid*.log "
        "into the cwd (the repo). Use jar_argv() / engine_argv() / jvm_error_file_arg():\n  "
        + "\n  ".join(bad)
    )


def test_every_shell_jar_launch_redirects_hs_err() -> None:
    bad = _scan_shell()
    assert not bad, (
        "a shell `java ... -jar` line without -XX:ErrorFile:\n  " + "\n  ".join(bad)
    )


# ── Non-vacuity: the scanners can see a violation and can be satisfied ───────


def test_scanner_is_not_vacuous() -> None:
    """A lint that scanned nothing would pass forever."""
    py = _files(".py")
    assert any(p.name == "storage_service_daemon.py" for p in py), "src/ not scanned"
    assert any(p.name == "_engine_substrate.py" for p in py), "tests/ not scanned"
    assert len(py) > 500, len(py)
    assert any(p.name == "check_release_workflow_shape.py" for p in py), "scripts/ not scanned"
    assert any(p.name == "up.sh" for p in _files(".sh")), "scripts/*.sh not scanned"


@pytest.mark.parametrize(
    "src, n",
    [
        ('x = ["java", "-jar", "a.jar"]', 1),
        ('x = [j, "-Duser.timezone=UTC", "-jar", str(p)]', 1),
        ('argv += ["-jar", str(p)]', 1),
        ('x = [j, "-XX:ErrorFile=/tmp/e_%p.log", "-jar", "a.jar"]', 0),
        ('x = [j, f"-XX:ErrorFile={d}/e_%p.log", "-jar", "a.jar"]', 0),
        ('x = [j, jvm_error_file_arg(), "-jar", "a.jar"]', 0),
        ('x = [j, t.jvm_error_file_arg(), "-jar", "a.jar"]', 0),
        ('x = ["-Dfoo"]', 0),
    ],
)
def test_python_scanner_verdicts(src: str, n: int) -> None:
    assert len(python_violations(src, "f")) == n


@pytest.mark.parametrize(
    "text, n",
    [
        ("java -jar app.jar\n", 1),
        ("env \\\n  A=1 \\\n  java -jar app.jar\n", 1),
        ("FOO=1 java -Duser.timezone=UTC -jar app.jar &\n", 1),
        ("exec java \"${jvm[@]}\" -jar x\n", 1),
        ("java -XX:ErrorFile=/tmp/e_%p.log -jar app.jar\n", 0),
        ("java \\\n  -XX:ErrorFile=/tmp/e_%p.log \\\n  -jar app.jar\n", 0),
        ("# java -jar app.jar\n", 0),
        ("echo no java jar here\n", 0),
    ],
)
def test_shell_scanner_verdicts(text: str, n: int) -> None:
    assert len(shell_violations(text, "f")) == n
