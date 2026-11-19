# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""scripts/claude-restart-sessions: seats for the sessions it relaunches under a name the user set (RDR-253 Step 4,
nexus-hczdo.1.5; the RDR Test Plan has no line for this, so these tests are it).

"A name the user set" is the registry ``nameSource`` being ``user`` (``-n`` or ``/rename``), not one Claude Code
generated. A session relaunched under such a name gets a seat when no seat has the name: its ephemeral identity is
PROMOTED (keeping its scratch and board entry), else a seat is created with ``created_by=restart``. A seat that already
has the name is left alone. ``nx identity`` is replaced by a stand-in that keeps the engine's state in a dict and
answers in the CLI's own wording; the wording the script reads (``unknown_name``, ``unknown_session``) is pinned
against the real command in tests/test_identity_cmd.py.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests._module_seam import setattr_in

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "claude-restart-sessions"


@pytest.fixture(scope="module")
def crs():
    loader = importlib.machinery.SourceFileLoader("claude_restart_sessions_seats", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeNx:
    """``nx identity`` against a dict: seats by name, the identity each session id belongs to."""

    def __init__(self) -> None:
        self.seats: dict[str, str] = {}                           # seat name -> id
        self.identities: dict[str, dict[str, Any]] = {}           # id -> record
        self.sessions: dict[str, str] = {}                        # session id -> id
        self.calls: list[tuple[str, ...]] = []
        self.down = False
        self.show_returns_garbage = False

    def ephemeral(self, session_id: str, *, state: str = "live", kind: str = "ephemeral", name: str | None = None) -> str:
        ident = f"i{len(self.identities) + 1}"
        self.identities[ident] = {"id": ident, "kind": kind, "state": state, "name": name}
        self.sessions[session_id] = ident
        if name:
            self.seats[name] = ident
        return ident

    def __call__(self, *args: str) -> subprocess.CompletedProcess:
        self.calls.append(args)
        out = lambda code, text="": subprocess.CompletedProcess(args, code, text if code == 0 else "", text if code else "")  # noqa: E731
        if self.down:
            return out(1, "Error: the engine is not answering (ConnectError)")
        verb, rest = args[0], list(args[1:])
        if "--" in rest:                                          # click reads what follows `--` as arguments, never options
            before, after = rest[:rest.index("--")], rest[rest.index("--") + 1:]
            rest = before + after
        elif verb in ("promote", "create") or (verb == "show" and rest[0] != "--session"):
            raise AssertionError(f"a name goes after `--`, so a leading '-' is not an option: {args}")
        if verb == "show" and rest[0] == "--session":
            if self.show_returns_garbage:
                return out(0, "not json")
            ident = self.sessions.get(rest[1])
            return out(0, json.dumps(self.identities[ident])) if ident else out(1, "Error: unknown_session: no identity has held session")
        if verb == "show":
            return out(0, f"{self.seats[rest[0]]} {rest[0]}") if rest[0] in self.seats else out(1, f"Error: unknown_name: no seat has been named {rest[0]}")
        if verb == "promote":
            ident, name = rest
            self.identities[ident].update(kind="seat", name=name)
            self.seats[name] = ident
            return out(0, f"{ident} is now the seat {name}")
        if verb == "create":
            assert rest[:2] == ["--created-by", "restart"], rest
            name = rest[2]
            ident = f"i{len(self.identities) + 1}"
            self.identities[ident] = {"id": ident, "kind": "seat", "state": "unheld", "name": name, "created_by": "restart"}
            self.seats[name] = ident
            return out(0, f"created seat {name} ({ident}), created by restart")
        raise AssertionError(args)


@pytest.fixture
def nx(crs, monkeypatch) -> FakeNx:
    fake = FakeNx()
    monkeypatch.setattr(crs, "_nx_identity", fake)
    return fake


def session(**kw: Any) -> dict[str, Any]:
    return {"sessionId": "sid-1", "name": "tamsin", "nameSource": "user", "pid": 111, "pane": "%1", "cwd": "/w", **kw}


def verbs(nx: FakeNx) -> list[str]:
    return [c[0] for c in nx.calls]


def test_a_user_named_session_with_no_seat_and_no_identity_gets_a_seat_created_by_restart(crs, nx) -> None:
    assert crs.ensure_seat(session()) == "created seat tamsin"
    assert nx.identities["i1"] | {} == {"id": "i1", "kind": "seat", "state": "unheld", "name": "tamsin", "created_by": "restart"}
    assert verbs(nx) == ["show", "show", "create"] and nx.calls[0][1] == "--session", "the session's own identity is asked first"


def test_a_session_whose_name_is_already_a_seat_gets_no_second_one(crs, nx) -> None:
    nx.ephemeral("other", name="tamsin", kind="seat")
    assert crs.ensure_seat(session()) == "seat tamsin exists"
    assert "create" not in verbs(nx) and "promote" not in verbs(nx), "nothing created, nothing promoted"
    assert len(nx.identities) == 1


def test_a_session_holding_an_ephemeral_identity_has_it_promoted_not_replaced(crs, nx) -> None:
    eph = nx.ephemeral("sid-1")
    assert crs.ensure_seat(session()) == f"promoted {eph} to seat tamsin"
    assert nx.identities[eph]["kind"] == "seat" and nx.seats == {"tamsin": eph}
    assert "create" not in verbs(nx), "a new seat would orphan the ephemeral's scratch and board entry"


@pytest.mark.parametrize("source", [None, "generated", "auto"], ids=["absent", "generated", "other"])
def test_a_name_the_user_did_not_set_gets_no_seat(crs, nx, source) -> None:
    s = session()
    if source is None:
        del s["nameSource"]
    else:
        s["nameSource"] = source
    assert crs.ensure_seat(s) == "" and verbs(nx) == ["show"], "only the session's own identity is looked up"
    assert crs.ensure_seat(session(name="")) == "" and "create" not in verbs(nx), "no name at all"


def test_a_name_with_a_leading_dash_reaches_nx_as_an_argument_not_an_option(crs, nx) -> None:
    nx.ephemeral("sid-1")
    assert crs.ensure_seat(session(name="-rf")) == "promoted i1 to seat -rf"
    for call in nx.calls:
        if "-rf" in call:
            assert call[call.index("-rf") - 1] in ("--", "i1"), call
    assert ("show", "--", "-rf") in nx.calls and ("promote", "--", "i1", "-rf") in nx.calls
    nx.identities.clear(), nx.seats.clear(), nx.sessions.clear()
    assert crs.ensure_seat(session(name="-x")) == "created seat -x"
    assert ("create", "--created-by", "restart", "--", "-x") in nx.calls


@pytest.mark.parametrize(("source", "kind", "seat", "passed"),
                         [("user", "user", True, True), ("generated", "derived", False, False),
                          (None, "unknown", False, True), ("user", "none", False, False)],
                         ids=["user", "derived", "unknown-older-claude", "no-name"])
def test_the_name_source_rule_is_stated_once_and_both_decisions_follow_it(crs, source, kind, seat, passed) -> None:
    """Seats only for a user-set name (an unknown source is not taken for one); `-n` is passed back for a user-set name
    and for an unknown source (older Claude behaviour), never for a derived one."""
    s = session(name="" if kind == "none" else "tamsin")
    if source is None:
        del s["nameSource"]
    else:
        s["nameSource"] = source
    assert crs.name_kind(s) == kind
    assert (crs.user_named(s), crs.passes_name(s)) == (seat, passed)
    assert (" -n tamsin" in crs.relaunch_command({**s, "argv": "claude", "cwd": "/w"})) is passed


def test_a_session_holding_another_seat_makes_no_seat_for_its_display_name_and_the_relaunch_carries_nx_seat(crs, nx) -> None:
    """Seat A under display name B: no seat B, and the relaunch is `NX_SEAT=A claude -n B` (Step 3 binds a seat named
    by NX_SEAT; a `-n` that names no seat would give the relaunch an ephemeral identity and drop the seat)."""
    nx.ephemeral("sid-1", kind="seat", name="elsewhere", state="live")
    s = session(argv="claude --model x -n tamsin")
    out = crs.ensure_seat(s)
    assert "holds seat elsewhere" in out and "tamsin" not in nx.seats and "create" not in verbs(nx)
    assert s["nx_seat"] == "elsewhere"
    command = crs.relaunch_command(s)
    assert command.startswith("cd /w && NX_SEAT=elsewhere ") and " -n tamsin " in command + " "


def test_a_derived_name_session_that_holds_a_seat_still_carries_it_but_makes_no_seat(crs, nx) -> None:
    nx.ephemeral("sid-1", kind="seat", name="elsewhere", state="live")
    s = session(nameSource="generated")
    assert crs.ensure_seat(s).startswith("holds seat elsewhere") and s["nx_seat"] == "elsewhere"
    assert verbs(nx) == ["show"]


def test_a_session_with_no_seat_gets_no_nx_seat_in_its_relaunch(crs, nx) -> None:
    s = session()
    crs.ensure_seat(s)
    assert "nx_seat" not in s and "NX_SEAT" not in crs.relaunch_command(s)


@pytest.mark.parametrize(("source", "passed"), [("user", True), ("generated", False), ("derived", False), (None, True)],
                         ids=["user", "generated", "derived", "absent-older-claude"])
def test_relaunch_passes_dash_n_only_for_a_name_the_user_set(crs, source, passed) -> None:
    """`-n` makes the name a user-set one: a name Claude derived must not become one by being passed back, or the next
    restart would make a seat from it. An older Claude writes no nameSource: its sessions relaunch as before."""
    s = session(argv="claude -n old", sessionId="sid-1")
    if source is None:
        del s["nameSource"]
    else:
        s["nameSource"] = source
    args = shlex.split(crs.relaunch_command(s).split(" && ", 1)[1])
    assert ("-n" in args) is passed and (args[args.index("-n") + 1] == "tamsin" if passed else "old" not in args)


def test_an_ephemeral_that_has_ended_is_not_promoted_a_seat_is_created(crs, nx) -> None:
    nx.ephemeral("sid-1", state="ended")
    assert crs.ensure_seat(session()) == "created seat tamsin"
    assert "promote" not in verbs(nx)


def test_an_engine_that_is_down_makes_no_seat_and_does_not_raise(crs, nx) -> None:
    nx.down = True
    out = crs.ensure_seat(session())
    assert out.startswith("no seat made") and verbs(nx) == ["show"]
    assert crs.ensure_seat(session(nameSource="generated")) == "", "a name the user did not set says nothing"


def test_whatever_goes_wrong_ensure_seat_returns_and_never_raises(crs, nx) -> None:
    assert crs.ensure_seat({"name": "tamsin", "nameSource": "user"}).startswith("no seat made for tamsin"), "a KeyError"
    nx.show_returns_garbage = True
    assert crs.ensure_seat(session()) == "created seat tamsin", "a non-JSON answer is read as no identity"


def test_a_missing_nx_binary_is_reported_not_raised(crs, monkeypatch) -> None:
    def boom(*args: str) -> Any:
        raise FileNotFoundError("nx")

    monkeypatch.setattr(crs, "_nx_identity", boom)
    assert crs.ensure_seat(session()).startswith("no seat made for tamsin"), "an OSError"


def test_restart_makes_the_seat_before_it_relaunches_under_the_name(crs, nx, monkeypatch, tmp_path) -> None:
    """restart() of a user-named session: the seat exists by the time the launch line is typed; the log says so."""
    (tmp_path / "sessions").mkdir()
    monkeypatch.setattr(crs, "SESSIONS", tmp_path / "sessions")          # nothing registers: the relaunch stalls
    monkeypatch.setattr(crs, "LOG", tmp_path / "restart.log")
    monkeypatch.setattr(crs, "MANAGED_SETTINGS", (tmp_path / "managed" / "managed-settings.json",))
    monkeypatch.setattr(crs, "USER_SETTINGS", tmp_path / "user-settings.json")
    monkeypatch.setattr(crs, "NO_SLASH", True)
    handoff = tmp_path / "h.md"
    handoff.write_text("# a\n# b\n", encoding="utf-8")
    eph = nx.ephemeral("old-sid")
    typed: list[tuple] = []
    monkeypatch.setattr(crs, "tmux", lambda *a: typed.append((a, list(nx.seats))))
    monkeypatch.setattr(crs, "save_draft", lambda *_a: ("", ""))
    monkeypatch.setattr(crs, "alive", lambda _pid: False)
    setattr_in(monkeypatch, crs, "time.sleep", lambda _s: None)
    s = session(sessionId="old-sid", argv="claude -n tamsin")
    with pytest.raises(crs.RelaunchStalled):
        crs.restart(s, str(handoff))
    launch = [seats for a, seats in typed if "-l" in a]
    assert launch == [["tamsin"]], "the seat was there when the launch line was typed"
    assert nx.identities[eph]["kind"] == "seat"
    assert f"tamsin: promoted {eph} to seat tamsin" in (tmp_path / "restart.log").read_text(encoding="utf-8")


def _restart_stand_ins(crs, monkeypatch, tmp_path) -> None:
    (tmp_path / "sessions").mkdir(exist_ok=True)
    monkeypatch.setattr(crs, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(crs, "LOG", tmp_path / "restart.log")
    monkeypatch.setattr(crs, "MANAGED_SETTINGS", (tmp_path / "managed" / "managed-settings.json",))
    monkeypatch.setattr(crs, "USER_SETTINGS", tmp_path / "user-settings.json")
    monkeypatch.setattr(crs, "save_draft", lambda *_a: ("", ""))
    setattr_in(monkeypatch, crs, "time.sleep", lambda _s: None)


def test_the_in_place_slash_restart_path_makes_the_seat_too(crs, nx, monkeypatch, tmp_path) -> None:
    """/restart keeps the process and rebinds the same identity, so promoting its ephemeral first is what lets the
    seat survive the default restart path: the seat call comes before slash_restart returns."""
    _restart_stand_ins(crs, monkeypatch, tmp_path)
    eph = nx.ephemeral("old-sid")
    monkeypatch.setattr(crs, "slash_restart", lambda s: (True, ""))
    assert crs.restart(session(sessionId="old-sid", argv="claude -n tamsin")) is True
    assert nx.identities[eph]["kind"] == "seat" and nx.seats == {"tamsin": eph}
    assert f"promoted {eph} to seat tamsin" in (tmp_path / "restart.log").read_text(encoding="utf-8")


def test_a_failing_seat_call_never_stops_a_restart(crs, nx, monkeypatch, tmp_path) -> None:
    _restart_stand_ins(crs, monkeypatch, tmp_path)
    nx.down = True
    monkeypatch.setattr(crs, "slash_restart", lambda s: (True, ""))
    assert crs.restart(session(sessionId="old-sid", argv="claude -n tamsin")) is True
