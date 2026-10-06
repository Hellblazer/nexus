# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-jg99b: the stdio-isolation sequence, on every host, with the OS faked.

The real kernel behaviour (a pending read on the stdin pipe blocking
GetFileSizeEx and the OpenBLAS DLL load) is proven in
tests/test_mcp_win_stdin_real_windows.py; this file proves the order of the
steps, that every failure leaves a working stdin, that POSIX is untouched, and
that both MCP stdio entry points call it before anything else.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from nexus.mcp._win_stdin import StdinOps, isolate_stdin

SRC = Path(__file__).resolve().parents[1] / "src" / "nexus" / "mcp"


class _Fake:
    def __init__(self, fail: str = "") -> None:
        self.calls: list[str] = []
        self.fail = fail
        self.stdin_fd: int | None = None

    def _step(self, name: str) -> None:
        self.calls.append(name)
        if self.fail == name:
            raise OSError(f"{name} failed")

    def ops(self) -> StdinOps:
        def dup(fd: int) -> int:
            self._step(f"dup({fd})")
            return 7

        def rebind(fd: int) -> None:
            self._step(f"rebind({fd})")
            self.stdin_fd = fd

        def open_nul() -> int:
            self._step("open_nul")
            return 9

        def dup2(src: int, dst: int) -> None:
            self._step(f"dup2({src},{dst})")

        def close(fd: int) -> None:
            self.calls.append(f"close({fd})")

        def set_std_input(fd: int) -> bool:
            self._step(f"set_std_input({fd})")
            return True

        return StdinOps(dup=dup, rebind=rebind, open_nul=open_nul, dup2=dup2, close=close, set_std_input=set_std_input)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_posix_is_untouched(platform: str) -> None:
    fake = _Fake()
    r = isolate_stdin(platform=platform, ops=fake.ops())
    assert (r.isolated, r.step) == (False, "not-windows")
    assert fake.calls == []


def test_windows_rebinds_to_the_private_fd_before_replacing_fd0() -> None:
    fake = _Fake()
    r = isolate_stdin(platform="win32", ops=fake.ops())
    assert (r.isolated, r.step) == (True, "done")
    assert fake.calls == ["dup(0)", "rebind(7)", "open_nul", "dup2(9,0)", "close(9)", "set_std_input(0)"]
    assert fake.stdin_fd == 7


def test_a_failed_dup_changes_nothing() -> None:
    fake = _Fake(fail="dup(0)")
    assert isolate_stdin(platform="win32", ops=fake.ops()).isolated is False
    assert fake.calls == ["dup(0)"]
    assert fake.stdin_fd is None


def test_a_failed_rebind_closes_the_dup_and_leaves_fd0_alone() -> None:
    fake = _Fake(fail="rebind(7)")
    assert isolate_stdin(platform="win32", ops=fake.ops()).isolated is False
    assert fake.calls == ["dup(0)", "rebind(7)", "close(7)"]


@pytest.mark.parametrize("step", ["open_nul", "dup2(9,0)", "set_std_input(0)"])
def test_a_failure_after_the_rebind_keeps_the_protocol_on_the_private_fd(step: str) -> None:
    fake = _Fake(fail=step)
    assert isolate_stdin(platform="win32", ops=fake.ops()).isolated is False
    assert fake.stdin_fd == 7, "sys.stdin must still read the protocol pipe"
    if step == "dup2(9,0)":
        assert "close(9)" in fake.calls, "the NUL descriptor must not leak"


@pytest.mark.parametrize("module", ["core.py", "catalog.py"])
def test_both_stdio_entry_points_isolate_stdin_first(module: str) -> None:
    tree = ast.parse((SRC / module).read_text(encoding="utf-8"))
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    first_call = next(
        stmt for stmt in main.body if isinstance(stmt, (ast.Expr, ast.Assign)) and isinstance(stmt.value, ast.Call)
    )
    imports = [stmt for stmt in main.body[: main.body.index(first_call)]]
    assert all(isinstance(s, ast.ImportFrom) for s in imports), "nothing but its import may run before isolate_stdin"
    assert isinstance(first_call.value.func, ast.Name) and first_call.value.func.id == "isolate_stdin"


def test_the_helper_never_logs_because_stdout_is_the_protocol_pipe() -> None:
    # It runs before configure_logging, when structlog writes to stdout; a line
    # there reached the guest's MCP client as a malformed JSON-RPC message.
    tree = ast.parse((SRC / "_win_stdin.py").read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not imported & {"structlog", "logging"}, imported
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "print" not in names
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not attrs & {"stdout", "stderr"}, attrs


@pytest.mark.parametrize("module", ["core.py", "catalog.py"])
def test_the_result_is_logged_after_logging_is_configured(module: str) -> None:
    text = (SRC / module).read_text(encoding="utf-8")
    main = text[text.index("\ndef main():"):]
    assert main.index("configure_logging(") < main.index('"mcp_stdin_isolation"')
