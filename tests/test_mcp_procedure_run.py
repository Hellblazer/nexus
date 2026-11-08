# SPDX-License-Identifier: AGPL-3.0-or-later
"""The MCP tool ``procedure_run`` (RDR-240 Technical Design 3; nexus-qo7ie.31).

It calls ``run_procedure`` and nothing else: the runtime is replaced here by a fake
``run_script``, which proves the name and the arguments arrive untouched and every error
comes back as its kind and detail. The shipped procedures' summaries are in its description.
"""
from __future__ import annotations

import json

import pytest

import nexus.lua_runtime as rt
from nexus.lua_runtime.errors import ScriptBusyError, ScriptError, ScriptLimitError, ScriptWorkerError
from nexus.lua_runtime.library import LibraryError, procedures
from nexus.mcp import core


def _install(monkeypatch: pytest.MonkeyPatch, fn: object) -> None:
    monkeypatch.setattr(rt, "run_script", fn, raising=False)


def test_the_tool_is_registered_with_the_documented_parameters() -> None:
    tool = core.mcp._tool_manager._tools["procedure_run"]
    props = tool.parameters["properties"]
    assert set(props) == {"name", "args"}
    assert props["name"]["type"] == "string"
    assert props["args"]["type"] == "object"
    assert tool.parameters["required"] == ["name"]
    assert props["name"]["description"] and props["args"]["description"]


def test_the_tool_is_a_sync_read_only_tool() -> None:
    tool = core.mcp._tool_manager._tools["procedure_run"]
    assert not tool.is_async
    assert tool.annotations is not None and tool.annotations.read_only_hint is True


def test_the_description_lists_every_shipped_procedure_with_its_summary() -> None:
    desc = core.mcp._tool_manager._tools["procedure_run"].description
    shipped = [p for p in procedures(cwd=None) if p.root == "shipped"]
    assert {p.name for p in shipped} >= {"research_add", "gate_record"}
    for p in shipped:
        assert f"- {p.name}: {p.summary}" in desc, p.name
    assert "nx run --list" in desc  # what runs from a checkout's own .nexus/procedures is listed there
    assert "NX_PROCEDURES_REPO_ROOT=1" in desc
    assert len(desc) <= 2048


def test_the_result_comes_back_as_json_text(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, dict]] = []

    def fake(name: str, args: dict) -> object:
        seen.append((name, args))
        return {"title": "é", "n": [1, 2.5, None]}

    _install(monkeypatch, fake)
    out = core.procedure_run(name="research_add", args={"rdr": 240})
    assert json.loads(out) == {"title": "é", "n": [1, 2.5, None]}
    assert "é" in out  # not escaped to é
    assert seen == [("research_add", {"rdr": 240})]


def test_args_default_to_an_empty_object(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict] = []
    _install(monkeypatch, lambda name, args: seen.append(args))
    core.procedure_run(name="x")
    assert seen == [{}]


@pytest.mark.parametrize(
    ("exc", "text"),
    [
        (ScriptError("args", "rdr: missing required argument"), "args: rdr: missing required argument"),
        (ScriptError("static_check", "verb-ungranted: memory.get (line 4)"), "static_check: verb-ungranted: memory.get (line 4)"),
        (ScriptLimitError("wall_time", 60.0), "limit: wall_time (60.0) reached"),
        (ScriptWorkerError("worker ended", returncode=3), "worker: worker ended (exit 3)"),
        (ScriptBusyError("4 workers run"), "busy: 4 workers run"),
        (LibraryError("not_found", "no procedure x"), "library: not_found: no procedure x"),
    ],
)
def test_an_error_comes_back_as_its_kind_and_detail(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, text: str
) -> None:
    def fake(name: str, args: dict) -> object:
        raise exc

    _install(monkeypatch, fake)
    assert core.procedure_run(name="x", args={}) == text


def test_the_tool_without_a_runtime_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(rt, "run_script", raising=False)
    out = core.procedure_run(name="x")
    assert out.startswith("unavailable: ")
    assert "run_script" in out


def test_an_unexpected_error_is_logged_and_returned_as_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(name: str, args: dict) -> object:
        raise ValueError("boom")

    _install(monkeypatch, fake)
    out = core.procedure_run(name="x")
    assert "boom" in out
    assert out.startswith("Error")


def test_a_result_that_is_not_json_is_an_error_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, lambda name, args: {"x": object()})
    assert core.procedure_run(name="x").startswith("Error")


def test_the_wire_call_goes_through_the_tool_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    _install(monkeypatch, lambda name, args: {"echo": [name, args]})

    async def call() -> object:
        return await core.mcp.call_tool("procedure_run", {"name": "gate_record", "args": {"bead": "nexus-x"}})

    result = asyncio.run(call())
    text = result[0].text if isinstance(result, (list, tuple)) else result.content[0].text
    assert json.loads(text) == {"echo": ["gate_record", {"bead": "nexus-x"}]}
