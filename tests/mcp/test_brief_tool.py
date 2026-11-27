# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``brief`` MCP tool: the same text as ``nx brief <topic>`` (SessionStart progressive disclosure).

Design of record: T2 nexus/design-sessionstart-progressive-disclosure-2026-11-20. The tool is read-only, answers plain
text, fails through ``_fail`` so a client sees ``isError``, and is auto-approved like the other read-only tools (marginalia's
gate ruling): a prompt per first call would suppress exactly the lookups the progressive disclosure relies on.
"""
from __future__ import annotations

import asyncio
import json

from nexus import brief
from nexus.hooks import auto_approve, session_context
from nexus.mcp import core
from nexus.mcp._tool_failure import is_failure
from tests.hooks.test_permission_request_hooks import _parse_decision, _run_nx_hook


def _tool():
    return {t.name: t for t in core.mcp._tool_manager.list_tools()}["brief"]


def test_the_tool_is_registered_read_only_with_one_topic_parameter() -> None:
    tool = _tool()
    assert tool.annotations is not None and tool.annotations.read_only_hint is True
    props = tool.parameters["properties"]
    assert set(props) == {"topic"} and props["topic"].get("description")
    assert tool.parameters.get("required", []) == []  # no topic: the index


def test_the_description_names_every_topic() -> None:
    description = _tool().description
    for topic in brief.TOPICS:
        assert f"`{topic}`" in description, topic


def test_the_tool_answers_what_nx_brief_prints() -> None:
    assert core.brief("caps") == brief.render("caps", source="mcp").text
    assert core.brief("") == brief.index_text()
    assert core.brief() == brief.index_text()


def test_a_failing_topic_is_marked_so_the_client_sees_is_error(monkeypatch) -> None:
    def boom() -> list[str]:
        raise RuntimeError("cache exploded")

    monkeypatch.setattr(session_context, "_build_capabilities_block", boom)
    result = core.brief("caps")
    assert is_failure(result) and "brief caps failed" in result
    wire = asyncio.run(core.mcp.call_tool("brief", {"topic": "caps"}))
    assert wire.is_error is True and "brief caps failed" in wire.content[0].text


def test_an_unknown_topic_is_a_failure() -> None:
    assert is_failure(core.brief("nonsense"))


def test_the_tool_records_its_source_as_mcp(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    core.brief("caps")
    [line] = (tmp_path / "logs" / "brief.log").read_text(encoding="utf-8").splitlines()
    assert json.loads(line)["source"] == "mcp"


def test_the_tool_is_auto_approved_on_pretooluse_only() -> None:
    name = "mcp__plugin_conexus_nexus__brief"
    assert name in auto_approve._ALLOWED_TOOLS
    assert _parse_decision(_run_nx_hook(name)) == "allow"
    assert _run_nx_hook(name, hook_event_name="PermissionRequest") == ""


def test_the_topic_is_case_insensitive_over_the_wire_too() -> None:
    assert core.brief("CAPS") == core.brief("caps")
    assert core.brief("  Caps ") == core.brief("caps")
