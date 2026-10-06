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


def test_a_set_std_handle_that_returns_false_is_a_failure() -> None:
    # subprocess takes a child's default stdin from STD_INPUT_HANDLE, so a
    # handle left unset is not isolation even though fd 0 is NUL.
    fake = _Fake()
    ops = fake.ops()
    ops = StdinOps(dup=ops.dup, rebind=ops.rebind, open_nul=ops.open_nul, dup2=ops.dup2, close=ops.close,
                   set_std_input=lambda fd: False)
    r = isolate_stdin(platform="win32", ops=ops)
    assert (r.isolated, r.step) == (False, "std-handle")
    assert fake.stdin_fd == 7


def test_the_sdk_stdio_server_reads_the_rebound_sys_stdin() -> None:
    # The fix moves the protocol by rebinding sys.stdin; that only works while
    # the installed MCP SDK's stdio_server reads sys.stdin.buffer when it is
    # called rather than fd 0. Drive the real stdio_server through a swapped
    # sys.stdin on every host, so an SDK change that breaks it fails here and
    # not only on a Windows box at server start.
    import io
    import os
    import sys

    import anyio
    from mcp.server.stdio import stdio_server

    r, w = os.pipe()
    os.write(w, b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n')
    os.close(w)
    swapped = io.TextIOWrapper(io.BufferedReader(io.FileIO(r, "rb", closefd=True)), encoding="utf-8")
    saved = sys.stdin
    sink = io.StringIO()

    async def first_message() -> object:
        with anyio.fail_after(10):
            async with stdio_server(stdout=anyio.wrap_file(sink)) as (read_stream, write_stream):
                msg = await read_stream.receive()
                await write_stream.aclose()  # lets the transport's writer task finish
                return msg

    try:
        sys.stdin = swapped
        msg = anyio.run(first_message)
    finally:
        sys.stdin = saved
        swapped.close()
    assert getattr(getattr(msg, "message", None), "root", None) is not None, msg
    assert msg.message.root.method == "ping"  # type: ignore[union-attr]
