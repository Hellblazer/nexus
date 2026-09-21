# SPDX-License-Identifier: AGPL-3.0-or-later
"""The SDK-patch status has to reach a handler, not just be emitted.

nexus-dgvsz, second round. The patch statuses were first logged at DEBUG
under an INFO log, so 3.5 MB of mcp.log carried zero occurrences and "is the
patch live in this process?" needed an audit. Raising them to INFO did NOT
fix it, and the live log after the next restart proved that: still zero
occurrences, because the apply runs at module IMPORT and `configure_logging`
does not run until `main()`. The level was never the problem. The timing was.

These tests pin the timing, since that is the thing that failed twice.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from nexus.mcp import _sdk_patches

SRC = pathlib.Path(_sdk_patches.__file__).parent


class _CapturingLog:
    """Minimal stand-in for a structlog logger."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def info(self, event: str, **kw: object) -> None:
        self.calls.append((event, dict(kw)))


def test_report_emits_one_line_per_patch_with_its_status() -> None:
    log = _CapturingLog()
    _sdk_patches.report_sdk_patches(
        log,
        {"cancellation_response": "applied", "sync_tool_offload": "already applied"},
        server="nx-mcp",
    )
    assert [e for e, _ in log.calls] == [
        "mcp_sdk_patch_applied",
        "mcp_sdk_patch_not_applied",
    ]
    assert log.calls[0][1] == {
        "patch": "cancellation_response",
        "status": "applied",
        "server": "nx-mcp",
    }
    assert log.calls[1][1]["status"] == "already applied"


def test_report_says_nothing_when_there_is_nothing_to_report() -> None:
    log = _CapturingLog()
    _sdk_patches.report_sdk_patches(log, {})
    assert log.calls == []


def _main_body(module: str) -> list[ast.stmt]:
    tree = ast.parse((SRC / module).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node.body
    raise AssertionError(f"{module} has no main()")


def _first_index(body: list[ast.stmt], predicate) -> int:
    for i, stmt in enumerate(body):
        for node in ast.walk(stmt):
            if predicate(node):
                return i
    return -1


def _is_call_to(name: str):
    def check(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == name
        )
    return check


@pytest.mark.parametrize("module", ["core.py", "catalog.py"])
def test_the_report_runs_after_logging_is_configured(module: str) -> None:
    """The defect exactly: a status emitted before configure_logging vanishes.

    Both entry points must call report_sdk_patches AFTER configure_logging.
    Asserted on the real source of both servers, because the catalog server
    is patched only transitively through nexus/mcp/__init__.py importing core
    and had no coverage of its own.
    """
    body = _main_body(module)
    configured_at = _first_index(body, _is_call_to("configure_logging"))
    reported_at = _first_index(body, _is_call_to("report_sdk_patches"))

    assert configured_at >= 0, f"{module}: main() never calls configure_logging"
    assert reported_at >= 0, f"{module}: main() never calls report_sdk_patches"
    assert reported_at > configured_at, (
        f"{module}: report_sdk_patches runs at statement {reported_at}, before "
        f"configure_logging at {configured_at} — the status would be emitted "
        "with no handler attached and dropped, which is the nexus-dgvsz defect"
    )


def test_apply_does_not_log_on_its_own() -> None:
    """Applying must stay silent, because it runs before logging exists.

    Non-vacuity for the ordering test above: if apply_sdk_patches logged by
    itself, the ordering would not matter and the test would be pinning a
    property with no consequence.
    """
    src = (SRC / "_sdk_patches.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "apply_sdk_patches":
            logged = [
                n for n in ast.walk(node)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr in {"info", "debug", "warning"}
            ]
            assert not logged, (
                "apply_sdk_patches logs directly; it runs at import time before "
                "configure_logging, so anything it emits is dropped. Report via "
                "report_sdk_patches from main() instead."
            )
            return
    raise AssertionError("apply_sdk_patches not found")
