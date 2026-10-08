# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every ``java ... -jar`` launch in src/, tests/, scripts/ and service/*.sh must
redirect its hs_err report, and the flag must come BEFORE ``-jar``.

WHY (nexus-o5xyx.2). A JVM that crashes writes ``hs_err_pid<N>.log`` into its cwd
unless ``-XX:ErrorFile=`` says otherwise. The engine JVM does crash (an ONNX Runtime
SEGV when SIGTERM lands during model init, nexus-o5xyx), and from a test run the cwd is
the repo checkout, so crash reports piled up in the tree. The flag is a per-launch
argument, so the only way to keep it everywhere is to check every launch site. After
``-jar`` the JVM hands the flag to the program as an argument and ignores it, hence the
ordering check.

WHAT IT CHECKS.
- Python (src/, tests/, scripts/): every list or tuple literal containing the string
  ``"-jar"`` must carry, BEFORE it, an element that is ``-XX:ErrorFile=...`` (a literal
  or f-string) or a call to ``jvm_error_file_arg()``. A ``"...".split()`` or
  ``shlex.split("...")`` literal is held to the same rule on its tokens. A list passed
  as the command to ``spawn_service`` is exempt: that shared launcher injects the flag
  itself (``tests/db/_service_fixture.py::with_error_file``, unit-tested below).
  Direct-Popen sites use ``jar_argv()``, ``engine_argv()`` or ``jvm_error_file_arg()``.
- Shell (src/, tests/, scripts/, service/): a non-comment logical line (backslash
  continuations joined) that runs ``java`` in command position, alone or behind
  ``env``, ``nohup``, ``exec``, ``timeout N`` or ``NAME=value`` prefixes, and quoted,
  ``$VAR`` or absolute-path forms included, must mention ``-XX:ErrorFile=`` before
  ``-jar``. Violations are reported at the PHYSICAL line where ``java`` appears.

NOT COVERED, on purpose. A native image is not a JVM and takes no such flag, so it has
no launch site here. The jlink launcher configured in ``service/pom.xml`` is generated
at build time and is out of scope. Anything that builds ``-jar`` dynamically (a variable,
concatenation) is invisible to a static check.
"""

from __future__ import annotations

import ast
import re
import shlex
import tempfile
import warnings
from pathlib import Path

import pytest

from tests.db import _service_fixture as fx
from tests._module_seam import setattr_in

pytestmark = pytest.mark.lint

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ROOTS = (
    _REPO_ROOT / "src",
    _REPO_ROOT / "tests",
    _REPO_ROOT / "scripts",
    _REPO_ROOT / "service",
)
_SKIP_PARTS = frozenset({".venv", "node_modules", "__pycache__", "target"})
_FLAG = "-XX:ErrorFile="
_HELPERS = frozenset({"jvm_error_file_arg"})
_INJECTING_LAUNCHERS = frozenset({"spawn_service"})

#: This file's own parametrized snippets spell out violating launches on purpose.
_SELF = Path(__file__).resolve()


def _files(suffix: str) -> list[Path]:
    out: list[Path] = []
    for root in _ROOTS:
        for p in root.rglob(f"*{suffix}"):
            if _SKIP_PARTS.intersection(p.parts):
                continue
            out.append(p)
    return sorted(out)


# ── Python ────────────────────────────────────────────────────────────────────


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


def _call_name(call: ast.Call) -> str:
    fn = call.func
    return fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")


def _flag_precedes_jar(tokens: list[str]) -> bool:
    if "-jar" not in tokens:
        return True
    jar = tokens.index("-jar")
    return any(t.startswith(_FLAG) for t in tokens[:jar])


def python_violations(source: str, label: str) -> list[str]:
    """Each ``-jar`` argv literal with no ErrorFile element BEFORE the ``-jar``."""
    with warnings.catch_warnings():
        # Scanning other files' source: their escape-sequence warnings are not ours.
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    out: list[str] = []
    for node in ast.walk(tree):
        parent = parents.get(node)
        if isinstance(node, (ast.List, ast.Tuple)):
            jar = next(
                (i for i, e in enumerate(node.elts)
                 if isinstance(e, ast.Constant) and e.value == "-jar"),
                None,
            )
            if jar is None:
                continue
            if (
                isinstance(parent, ast.Call)
                and _call_name(parent) in _INJECTING_LAUNCHERS
                and node in parent.args
            ):
                continue
            if not any(_is_flag(e) for e in node.elts[:jar]):
                out.append(f"{label}:{node.lineno}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            tokens: list[str] | None = None
            grand = parents.get(parent) if parent is not None else None
            if (
                isinstance(parent, ast.Attribute)
                and parent.attr == "split"
                and parent.value is node
                and isinstance(grand, ast.Call)
                and grand.func is parent
            ):
                tokens = node.value.split()
            elif isinstance(parent, ast.Call) and _call_name(parent) == "split" and node in parent.args:
                try:
                    tokens = shlex.split(node.value)
                except ValueError:
                    tokens = node.value.split()
            if tokens is not None and not _flag_precedes_jar(tokens):
                out.append(f"{label}:{node.lineno}")
    return out


# ── Shell ─────────────────────────────────────────────────────────────────────

#: ``java`` in COMMAND position: at the start of a command (line start or after
#: ``; & | (``), behind any of the wrappers env / nohup / exec / command / time,
#: ``timeout [opts] N`` and ``NAME=value`` assignments, in bare, quoted, ``$VAR`` or
#: path form (any token that ENDS in "java", so "$JAVA", /usr/bin/java and
#: "$JH/bin/java" all count). A word "java" in a message or comment is prose.
_SHELL_JAVA_CMD = re.compile(
    r"""(?:^|[;&|(])\s*
        (?:(?:env|nohup|exec|command|time)\s+
          |timeout\s+(?:\S+\s+){1,3}?
          |\w+=(?:"[^"]*"|'[^']*'|\S*)\s+)*
        (?P<java>["']?[^\s"']*java["']?)(?=\s)""",
    re.VERBOSE | re.IGNORECASE,
)


def _strip_shell_comment(s: str) -> str:
    """Cut a trailing ``# ...`` comment, honouring quotes."""
    quote = ""
    for i, ch in enumerate(s):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or s[i - 1].isspace()):
            return s[:i]
    return s


def shell_violations(text: str, label: str) -> list[str]:
    """Each logical shell line that runs ``java -jar`` without an ErrorFile before
    the ``-jar``. The reported line is the physical line holding ``java``."""
    out: list[str] = []
    physical = text.split("\n")
    i = 0
    while i < len(physical):
        pieces: list[tuple[int, str]] = []
        while True:
            line = physical[i]
            i += 1
            stripped = line.rstrip()
            if stripped.endswith("\\") and i < len(physical):
                pieces.append((i, stripped[:-1]))
                continue
            pieces.append((i, line))
            break
        joined = ""
        starts: list[tuple[int, int]] = []
        for lineno, piece in pieces:
            starts.append((len(joined), lineno))
            joined += piece + " "
        body = _strip_shell_comment(joined)
        if body.lstrip().startswith("#"):
            continue
        m = _SHELL_JAVA_CMD.search(body)
        jar = re.search(r"\s-jar\b", body)
        if not m or not jar:
            continue
        flag = body.find(_FLAG)
        if flag == -1 or flag > jar.start():
            pos = m.start("java")
            lineno = next(ln for off, ln in reversed(starts) if off <= pos)
            out.append(f"{label}:{lineno}")
    return out


# ── Repo scans ────────────────────────────────────────────────────────────────


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
        "a `-jar` argv without -XX:ErrorFile BEFORE `-jar` lets a crashing JVM drop "
        "hs_err_pid*.log into the cwd (the repo). Use jar_argv() / engine_argv() / "
        "jvm_error_file_arg(), or pass the list to spawn_service():\n  " + "\n  ".join(bad)
    )


def test_every_shell_jar_launch_redirects_hs_err() -> None:
    bad = _scan_shell()
    assert not bad, (
        "a shell `java ... -jar` line without -XX:ErrorFile before `-jar`:\n  "
        + "\n  ".join(bad)
    )


# ── Non-vacuity: the scanners can see a violation and can be satisfied ───────


def test_scanner_is_not_vacuous() -> None:
    """A lint that scanned nothing would pass forever."""
    py = _files(".py")
    sh = _files(".sh")
    assert any(p.name == "storage_service_daemon.py" for p in py), "src/ not scanned"
    assert any(p.name == "_engine_substrate.py" for p in py), "tests/ not scanned"
    assert any(p.name == "check_engine_release_floor.py" for p in py), "scripts/ not scanned"
    assert len(py) > 500, len(py)
    assert any(p.name == "up.sh" for p in sh), "scripts/*.sh not scanned"
    assert any(p.name == "trace-native.sh" for p in sh), "service/*.sh not scanned"
    assert not any("target" in p.parts for p in sh), "build output scanned"


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
        # the flag must PRECEDE -jar
        ('x = [j, "-jar", "a.jar", jvm_error_file_arg()]', 1),
        ('x = [j, "-jar", "-XX:ErrorFile=/t/e_%p.log"]', 1),
        # tuple argv
        ('x = (j, "-jar", "a.jar")', 1),
        ('x = (j, jvm_error_file_arg(), "-jar", "a.jar")', 0),
        # .split() and shlex.split literals
        ('run("java -jar a.jar".split())', 1),
        ('run("java -XX:ErrorFile=/t/e_%p.log -jar a.jar".split())', 0),
        ('run(shlex.split("java -jar a.jar"))', 1),
        ('run(shlex.split("java -XX:ErrorFile=/t/e_%p.log -jar a.jar"))', 0),
        ('run(shlex.split("java -jar a.jar -XX:ErrorFile=/t/e_%p.log"))', 1),
        # prose is not a launch
        ('x = "java -jar a.jar"', 0),
        ('log("use java -jar".upper())', 0),
        # spawn_service injects the flag; any other callee does not
        ('spawn_service([j, "-jar", p], env)', 0),
        ('spawn_service(cmd=[j, "-jar", p], env=env)', 1),
        ('other([j, "-jar", p], env)', 1),
    ],
)
def test_python_scanner_verdicts(src: str, n: int) -> None:
    assert len(python_violations(src, "f")) == n


@pytest.mark.parametrize(
    "text, n",
    [
        ("java -jar app.jar\n", 1),
        ("FOO=1 java -Duser.timezone=UTC -jar app.jar &\n", 1),
        ('exec java "${jvm[@]}" -jar x\n', 1),
        ("java -XX:ErrorFile=/tmp/e_%p.log -jar app.jar\n", 0),
        ("java \\\n  -XX:ErrorFile=/tmp/e_%p.log \\\n  -jar app.jar\n", 0),
        ("# java -jar app.jar\n", 0),
        ("echo no java jar here\n", 0),
        ('_die "the java -jar path is expunged"\n', 0),
        ("env \\\n  A=1 \\\n  java -jar app.jar\n", 1),
        # quoted, $VAR and absolute-path forms
        ('"$JAVA" -jar app.jar\n', 1),
        ("$JAVA -jar app.jar\n", 1),
        ("/usr/bin/java -jar app.jar\n", 1),
        ('"$JH/bin/java" -jar app.jar\n', 1),
        ('"$JH/bin/java" "-XX:ErrorFile=$T/e_%p.log" -jar app.jar\n', 0),
        # wrapper prefixes
        ("nohup java -jar app.jar &\n", 1),
        ("timeout 30 java -jar app.jar\n", 1),
        ("timeout -s KILL 30 java -jar app.jar\n", 1),
        ("nohup java -XX:ErrorFile=/t/e_%p.log -jar app.jar &\n", 0),
        # the flag must precede -jar, and a comment does not count
        ("java -jar app.jar -XX:ErrorFile=/t/e_%p.log\n", 1),
        ("java -jar app.jar # -XX:ErrorFile=/t/e_%p.log\n", 1),
    ],
)
def test_shell_scanner_verdicts(text: str, n: int) -> None:
    assert len(shell_violations(text, "f")) == n


def test_shell_violation_reports_the_physical_line_of_java() -> None:
    text = "echo hi\nenv A=1 \\\n  java \\\n  -jar x\necho done\n"
    assert shell_violations(text, "f") == ["f:3"]


def test_shell_line_numbers_after_a_continuation_are_physical() -> None:
    text = "a=1 \\\n  b=2\n\njava -jar y\n"
    assert shell_violations(text, "f") == ["f:4"]


# ── The shared launcher really injects the flag ──────────────────────────────


def test_with_error_file_injects_before_jar_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    flag = f"-XX:ErrorFile={tmp_path}/hs_err_%p.log"
    cmd = ["/usr/bin/java", "-Xmx1g", "-jar", "a.jar"]
    out = fx.with_error_file(cmd)
    assert out == ["/usr/bin/java", "-Xmx1g", flag, "-jar", "a.jar"]
    assert fx.with_error_file(out) == out, "must not add a second flag"
    assert cmd == ["/usr/bin/java", "-Xmx1g", "-jar", "a.jar"], "input must not be mutated"
    own = ["java", "-XX:ErrorFile=/elsewhere/e_%p.log", "-jar", "a.jar"]
    assert fx.with_error_file(own) == own, "a caller's own flag wins"
    native = ["/opt/nexus-service", "-Duser.timezone=UTC"]
    assert fx.with_error_file(native) == native, "a native launch has no -jar and no flag"


def test_spawn_service_hands_popen_the_injected_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    seen: dict = {}

    class _Popen:
        def __init__(self, argv, **kwargs) -> None:
            seen["argv"] = argv

    setattr_in(monkeypatch, "tests.db._service_fixture", "subprocess.Popen", _Popen)
    _proc, log = fx.spawn_service(["/usr/bin/java", "-jar", "a.jar"], {}, log_dir=tmp_path / "l")
    assert seen["argv"] == [
        "/usr/bin/java", f"-XX:ErrorFile={tmp_path}/hs_err_%p.log", "-jar", "a.jar",
    ]
    assert log.parent == tmp_path / "l"
