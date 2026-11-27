# SPDX-License-Identifier: AGPL-3.0-or-later
"""The plan-library rename keeps the old names for one client release (nexus-sby49.4).

Sam, 2026-11-25: MCP ``plan_save`` / ``plan_search`` / ``plan_delete`` became ``answer_plan_save`` /
``answer_plan_search`` / ``answer_plan_delete``, and ``nx plan`` became ``nx answer-plan``, so an
implementation plan cannot be mistaken for what the library holds. Each old name still works and says it
is deprecated, naming the new one. These tests are tenant-free: the refusal and the stubbed library are
enough to pin the alias wiring; the round trip itself is pinned by the answer_plan_* tests.
"""
from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.commands.plan import answer_plan, plan
from nexus.mcp import core

_PAIRS = (
    ("plan_save", "answer_plan_save"),
    ("plan_search", "answer_plan_search"),
    ("plan_delete", "answer_plan_delete"),
)


def _tools() -> dict:
    return {t.name: t for t in core.mcp._tool_manager.list_tools()}


@pytest.mark.parametrize(("old", "new"), _PAIRS)
def test_both_names_are_registered_and_the_old_one_says_it_is_deprecated(old: str, new: str) -> None:
    tools = _tools()
    assert old in tools and new in tools
    assert tools[old].description.startswith(f"Deprecated: `{old}` is now `{new}`")
    assert "Deprecated" not in tools[new].description


@pytest.mark.parametrize(("old", "new"), _PAIRS)
def test_the_alias_takes_the_same_parameters_as_the_new_tool(old: str, new: str) -> None:
    tools = _tools()
    # Only the generated schema title carries the tool's own name.
    drop = lambda schema: {k: v for k, v in schema.items() if k != "title"}  # noqa: E731
    assert drop(tools[old].parameters) == drop(tools[new].parameters)


def test_plan_save_still_works_and_names_its_replacement() -> None:
    """A refused save is the one path that touches no storage; it must stay a marked failure."""
    from nexus.mcp._tool_failure import is_failure

    old = core.plan_save(query="q", plan_json="{}", verb="")
    new = core.answer_plan_save(query="q", plan_json="{}", verb="")
    assert is_failure(old) and is_failure(new)
    assert old.startswith(str(new))
    assert "Deprecated: `plan_save` is now `answer_plan_save`" in old
    assert "Deprecated" not in new


class _StubLibrary:
    """Just enough of the plan library for the search and delete tools to run without storage."""

    plans = SimpleNamespace(
        get_plan=lambda plan_id: {"id": plan_id, "query": "a stub plan", "name": None},
        delete_plan=lambda plan_id: True,
    )

    @staticmethod
    def search_plans(query, **kwargs):
        return []


@pytest.fixture()
def stub_library(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "_t2_ctx", lambda: contextlib.nullcontext(_StubLibrary))


def test_plan_search_still_works_and_names_its_replacement(stub_library: None) -> None:
    out = core.plan_search(query="anything")
    assert out.startswith("No matching plans.")
    assert "Deprecated: `plan_search` is now `answer_plan_search`" in out
    assert "Deprecated" not in core.answer_plan_search(query="anything")


def test_plan_delete_still_works_and_names_its_replacement(stub_library: None) -> None:
    out = core.plan_delete(plan_id=7)
    assert out.startswith("Deleted plan id=7: a stub plan")
    assert "Deprecated: `plan_delete` is now `answer_plan_delete`" in out


def test_the_old_cli_group_has_every_subcommand_of_the_new_one() -> None:
    assert plan.name == "plan" and answer_plan.name == "answer-plan"
    assert set(plan.commands) == set(answer_plan.commands) and plan.commands


def test_nx_plan_still_runs_and_says_it_is_deprecated() -> None:
    result = CliRunner().invoke(main, ["plan", "list", "--help"])
    assert result.exit_code == 0
    assert "Deprecated: `nx plan` is now `nx answer-plan`" in result.output
    assert "Tabulate plans" in result.output


def test_nx_answer_plan_runs_without_a_notice() -> None:
    result = CliRunner().invoke(main, ["answer-plan", "list", "--help"])
    assert result.exit_code == 0
    assert "Deprecated" not in result.output
    assert "Tabulate plans" in result.output


def test_the_old_group_is_hidden_from_the_command_list_and_the_new_one_is_listed() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert "answer-plan" in result.output
    assert not any(line.split()[:1] == ["plan"] for line in result.output.splitlines())
