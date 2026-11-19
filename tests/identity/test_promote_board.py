# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-253 Step 5 open items (nexus-hczdo.1.7's DISCOVER notes): the board title rule, and ``nx identity promote``
moving the entry's title to the new alias.

The engine's ``promote`` does not retitle the board entry (only ``rename`` does, in its own transaction; a change
there is an engine release), so the client does it after the promote, through ``put_replacing`` under the fence:
the owning session's write lands, an operator's shell is refused (``403 identity_token_required``) and the command
says so while still reporting the promote.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from nexus.commands.identity_cmd import identity_group
from nexus.identity.board import board_title, identity_of, retitle_entry
from nexus.identity.ops import CallerFacts, IdentityEnv
from tests.identity.fake_engine import FakeEngine
from tests.identity.fakes import HOST, FakeProcesses

ENTRY = "session: 11111111-2222-4333-8444-555555555555\nidentity: i1\n## Work\nstate: waiting\n"


class _Memory:
    """The slice of ``HttpMemoryStore`` the retitle uses; ``refuse`` stands for the engine's fence."""

    def __init__(self, entries: dict[str, str], refuse: int | None = None) -> None:
        self.entries = dict(entries)
        self.refuse = refuse
        self.quarantined: list[str] = []

    def search(self, query: str, project: str | None = None, access: str = "track") -> list[dict[str, Any]]:
        return [{"title": t} for t, c in self.entries.items() if "identity" in c]

    def get(self, project: str | None = None, title: str | None = None, id: int | None = None) -> dict[str, Any] | None:
        return {"title": title, "content": self.entries[title], "tags": "board"} if title in self.entries else None

    def put_replacing(self, project: str, title: str, content: str, replaces: str, tags: str = "", **_kw: Any) -> Any:
        if self.refuse:
            raise httpx.HTTPStatusError("refused", request=httpx.Request("POST", "http://engine/v1/memory/put"),
                                        response=httpx.Response(self.refuse))
        self.entries[title] = f"{content}\nreplaces: {replaces}"
        self.quarantined.append(replaces)
        del self.entries[replaces]


class _Machine:
    def __init__(self, tmp_path: Path, memory: _Memory | None) -> None:
        self.engine = FakeEngine()
        self.memory = memory
        self.procs = FakeProcesses(tmp_path / "claude-sessions", host=HOST)
        self.tmp_path = tmp_path

    def env(self) -> IdentityEnv:
        @contextmanager
        def board():
            yield self.memory

        return IdentityEnv(client=self.engine, procs=self.procs, identity_dir=self.tmp_path / "identity", environ={},
                           caller=lambda: CallerFacts(claude_pid=0, ancestors=()), clock=lambda: 1_000.0,
                           client_version="9.9.9-test", board_memory=board if self.memory is not None else None)

    def run(self, *args: str):
        return CliRunner().invoke(identity_group, list(args), obj={"identity_env": self.env()})


def _ephemeral(machine: _Machine, display_name: str | None = "scratch42") -> str:
    return machine.engine.stage(kind="ephemeral", state="live", generation=1, display_name=display_name)


# ── the title rule (open item a) ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("alias", "display", "title"),
    [
        ("marginalia", None, "marginalia"),
        ("marginalia", "something else", "marginalia"),
        (None, "scratch42", "scratch42 (i12)"),
        (None, None, "ephemeral (i12)"),
        (None, "   ", "ephemeral (i12)"),
        ("", "", "ephemeral (i12)"),
    ],
)
def test_the_board_title_is_the_alias_else_the_display_name_else_the_word_ephemeral(
    alias: str | None, display: str | None, title: str
) -> None:
    assert board_title(identity="i12", alias=alias, display_name=display) == title


def test_nx_identity_show_prints_the_title_a_session_gives_its_entry(tmp_path: Path) -> None:
    machine = _Machine(tmp_path, None)
    nameless = _ephemeral(machine, None)
    named = _ephemeral(machine)
    seat = machine.engine.stage(kind="seat", name="wren", state="live", generation=1)
    assert f"board title ephemeral ({nameless})" in machine.run("show", nameless).output
    assert f"board title scratch42 ({named})" in machine.run("show", named).output
    assert "board title wren\n" in machine.run("show", seat).output


# ── promote retitles the entry (open item b) ─────────────────────────────────


def test_promote_moves_the_entry_from_the_ephemeral_title_to_the_alias(tmp_path: Path) -> None:
    memory = _Memory({"scratch42 (i1)": ENTRY})
    machine = _Machine(tmp_path, memory)
    eph = _ephemeral(machine)
    assert eph == "i1"
    result = machine.run("promote", eph, "gamma")
    assert result.exit_code == 0, result.output
    assert "i1 is now the seat gamma" in result.output
    assert "board entry retitled 'scratch42 (i1)' to 'gamma'" in result.output
    assert list(memory.entries) == ["gamma"] and memory.quarantined == ["scratch42 (i1)"]
    assert identity_of(memory.entries["gamma"]) == "i1", "the identity: line travels with the entry"


def test_promote_run_outside_the_session_reports_the_fence_and_still_promotes(tmp_path: Path) -> None:
    """An operator's shell holds no token for the entry's owner: the engine answers 403, the seat is made anyway."""
    memory = _Memory({"scratch42 (i1)": ENTRY}, refuse=403)
    machine = _Machine(tmp_path, memory)
    eph = _ephemeral(machine)
    result = machine.run("promote", eph, "gamma")
    assert result.exit_code == 0, result.output
    assert machine.engine.bound(eph)["kind"] == "seat"
    assert "board entry keeps its title 'scratch42 (i1)': only the owning session may write its entry" in result.output
    assert list(memory.entries) == ["scratch42 (i1)"]


def test_promote_never_overwrites_another_entry_already_titled_with_the_alias(tmp_path: Path) -> None:
    memory = _Memory({"scratch42 (i1)": ENTRY, "gamma": "## someone else's notes\n"})
    machine = _Machine(tmp_path, memory)
    result = machine.run("promote", _ephemeral(machine), "gamma")
    assert result.exit_code == 0 and "an entry titled 'gamma' already exists" in result.output
    assert memory.entries["gamma"] == "## someone else's notes\n" and "scratch42 (i1)" in memory.entries


def test_promote_with_no_board_entry_says_nothing_about_the_board(tmp_path: Path) -> None:
    machine = _Machine(tmp_path, _Memory({"unrelated": "## nothing here\n"}))
    result = machine.run("promote", _ephemeral(machine), "gamma")
    assert result.exit_code == 0 and "board" not in result.output


def test_retitle_entry_ignores_an_entry_that_names_another_identity() -> None:
    memory = _Memory({"other (i2)": ENTRY.replace("i1", "i2")})
    assert retitle_entry(memory, "i1", "gamma") == "" and list(memory.entries) == ["other (i2)"]
