# SPDX-License-Identifier: AGPL-3.0-or-later
"""Engine and PostgreSQL children in tests start and stop through ``tests/_child_process.py``.

Three spellings broke the engine path on native Windows (T2
``nexus_rdr/224-win-batch-final-windows``), each copied into about twenty fixtures:

* ``preexec_fn=os.setsid`` at spawn: ``preexec_fn`` raises on Windows;
* ``os.killpg`` at stop: absent on Windows;
* ``Path(JAVA_HOME) / "bin" / "java"``: the launcher there is ``java.exe``, so every
  java-gated suite skipped with Java installed.

``tests/_child_process.py`` owns all three (``popen_in_group`` / ``stop_group`` /
``kill_group``, ``java_executable``). This sweep finds each spelling by AST anywhere under
``tests/`` and fails on any site outside the helper and the exemptions below. Comments,
docstrings and strings never match. Exemptions are keyed on (file, enclosing top-level def)
with an exact count, so a moved or removed site fails as loudly as a new one.
"""
from __future__ import annotations

import ast
import sys
import warnings
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO = Path(__file__).parent.parent
TESTS = REPO / "tests"

#: The helper itself: its POSIX arms.
HELPER = "tests/_child_process.py"

#: Sites that exercise the product's OWN process-group code, or the substrate's stale-cluster
#: sweep, on purpose. Each is a POSIX test of a POSIX mechanism (the product's Windows arms
#: are tested separately), not an engine or PG fixture.
EXEMPT: dict[tuple[str, str], int] = {
    # The stale-cluster sweep reaches a recorded engine pid's group (its Windows arm is safe_killpg).
    ("tests/_engine_substrate.py", "_kill_engine_leg"): 1,
    # Pins that _kill_engine_leg signals the group: spawns a real group, wraps os.killpg.
    ("tests/test_engine_substrate_sweep.py", "TestKillEngineLegUsesProcessGroup"): 2,
    # nexus.bounded_subprocess's own group kill, checked against a real group.
    ("tests/test_bounded_subprocess.py", "test_kill_reaches_the_group_not_just_the_child"): 1,
    # The POSIX process-group contract the semaphore-leak root cause rests on.
    ("tests/test_semaphore_leak_rootcause.py", "test_process_group_spawn_and_killpg_contract"): 2,
    # The posix_spawn adapter's census of which group/session requests keep the fast path.
    ("tests/test_posix_spawn_adapter.py", "test_session_and_group_requests_hold_and_take_posix_spawn"): 1,
}

#: Non-vacuity: the sweep must read at least this many files under tests/.
MIN_FILES = 1000


def _is_os_attr(node: ast.AST, attr: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attr
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _is_bin_java(node: ast.AST) -> bool:
    """``<x> / "bin" / "java"``: a Div whose right is ``"java"`` and whose left ends ``/ "bin"``."""
    return (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Div)
        and isinstance(node.right, ast.Constant)
        and node.right.value == "java"
        and isinstance(node.left, ast.BinOp)
        and isinstance(node.left.op, ast.Div)
        and isinstance(node.left.right, ast.Constant)
        and node.left.right.value == "bin"
    )


def sites(source: str) -> list[tuple[int, str, str]]:
    """(line, enclosing top-level def/class or ``<module>``, kind) of every banned spelling."""
    with warnings.catch_warnings():
        # A test file's own invalid escape is not this sweep's business.
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(source)
    found: list[tuple[int, str, str]] = []

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if scope == "<module>" and isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ):
                inner = child.name
            if isinstance(child, ast.keyword) and child.arg == "preexec_fn" and _is_os_attr(child.value, "setsid"):
                found.append((child.value.lineno, inner, "preexec_fn=os.setsid"))
            elif isinstance(child, ast.Dict):
                # The kwargs-dict form: {"preexec_fn": os.setsid}.
                for k, v in zip(child.keys, child.values, strict=True):
                    if isinstance(k, ast.Constant) and k.value == "preexec_fn" and _is_os_attr(v, "setsid"):
                        found.append((v.lineno, inner, "preexec_fn=os.setsid"))
            elif _is_os_attr(child, "killpg"):
                found.append((child.lineno, inner, "os.killpg"))
            elif _is_bin_java(child):
                found.append((child.lineno, inner, '"bin" / "java"'))
            visit(child, inner)

    visit(tree, "<module>")
    return found


def _scan() -> tuple[int, dict[tuple[str, str], list[tuple[int, str]]]]:
    files = 0
    by_scope: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for path in sorted(TESTS.rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        if rel == HELPER:
            continue
        files += 1
        for line, scope, kind in sites(path.read_text(encoding="utf-8")):
            by_scope.setdefault((rel, scope), []).append((line, kind))
    return files, by_scope


def test_banned_spellings_only_in_the_helper_and_exemptions() -> None:
    files, by_scope = _scan()
    assert files >= MIN_FILES, f"sweep read {files} files under tests/, expected >= {MIN_FILES}"
    problems: list[str] = []
    for key, hits in sorted(by_scope.items()):
        allowed = EXEMPT.get(key)
        if allowed is None:
            for line, kind in hits:
                problems.append(
                    f"{key[0]}:{line} ({key[1]}): {kind} -- use tests._child_process "
                    "(popen_in_group / stop_group / kill_group / java_executable)"
                )
        elif len(hits) != allowed:
            problems.append(f"{key[0]} ({key[1]}): {len(hits)} sites, exemption says {allowed}")
    for key, allowed in EXEMPT.items():
        if key not in by_scope and allowed:
            problems.append(f"{key[0]} ({key[1]}): exemption for {allowed} sites, found none")
    assert not problems, "\n".join(problems)


def test_the_helper_still_holds_each_spelling() -> None:
    """Non-vacuity on the other side: the helper is where the POSIX arms live."""
    kinds = {kind for _line, _scope, kind in sites((REPO / HELPER).read_text(encoding="utf-8"))}
    assert kinds == {"preexec_fn=os.setsid", "os.killpg"}, kinds


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("subprocess.Popen(argv, preexec_fn=os.setsid)\n", ["preexec_fn=os.setsid"]),
        ("os.killpg(os.getpgid(p.pid), signal.SIGTERM)\n", ["os.killpg"]),
        ("real = os.killpg\n", ["os.killpg"]),
        ('kw = {"preexec_fn": os.setsid}\n', ["preexec_fn=os.setsid"]),
        ('J = Path(home) / "bin" / "java"\n', ['"bin" / "java"']),
        ('J = (Path(h) / "bin" / "java").exists()\n', ['"bin" / "java"']),
        # Not code: a docstring, a comment, a string naming it.
        ('"""spawned with preexec_fn=os.setsid and os.killpg"""\n# os.killpg\n', []),
        ('setattr_in(mp, mod, "os.killpg", f)\n', []),
        # The helper-shaped forms are fine.
        ('J = Path(home) / "bin" / exe_name("java")\n', []),
        ("subprocess.Popen(argv, start_new_session=True)\n", []),
        ("subprocess.Popen(argv, preexec_fn=other)\n", []),
    ],
)
def test_the_detector_on_samples(source: str, expected: list[str]) -> None:
    assert [kind for _line, _scope, kind in sites(source)] == expected


def test_a_planted_violation_fails_the_sweep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-vacuity of the whole sweep: a fixture copy written into a scanned tree is reported."""
    fake_tests = tmp_path / "tests"
    fake_tests.mkdir()
    for i in range(MIN_FILES):
        (fake_tests / f"t{i}.py").write_text("x = 1\n")
    (fake_tests / "test_planted.py").write_text(
        "def fx():\n    p = subprocess.Popen(a, preexec_fn=os.setsid)\n"
        "    os.killpg(os.getpgid(p.pid), 9)\n",
    )
    mod = sys.modules[__name__]
    monkeypatch.setattr(mod, "REPO", tmp_path)
    monkeypatch.setattr(mod, "TESTS", fake_tests)
    with pytest.raises(AssertionError, match=r"test_planted\.py:2 \(fx\): preexec_fn=os\.setsid"):
        test_banned_spellings_only_in_the_helper_and_exemptions()
