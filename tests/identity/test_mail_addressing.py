# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-253 Phase 2: mail is addressed by identity. A session id and the ``directory/<name>`` lease are no longer addresses.

Engine-free: the alias lookup is a stand-in for ``IdentityClient.alias`` that answers the way the engine's
``GET /v1/identity/alias/<name>`` does (``IdentityRepository.resolveAlias``: a live name; a retired one inside its
7-day grace with a notice; past it a 410 "renamed to <new> on <date>"), on a fixed clock the stand-in owns.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from nexus.identity.bind_client import IdentityApiError
from nexus.tuple_directory import (
    DirectoryResolutionError,
    SendTarget,
    check_from_address_is_own,
    looks_like_identity_id,
    resolve_send,
)
from nexus.tuple_wait import build_specs

OLD_SESSION = "11111111-2222-4333-8444-555555555555"


@dataclass
class _Engine:
    """The alias route on a fixed clock: names -> (identity, identity_state, retired_at, renamed_to)."""

    now: datetime
    names: dict[str, tuple[str, str, datetime | None, str]] = field(default_factory=dict)

    def alias(self, name: str) -> dict[str, Any]:
        if name not in self.names:
            raise IdentityApiError(404, "unknown_name", f"no seat has been named {name}")
        identity, state, retired_at, renamed_to = self.names[name]
        out: dict[str, Any] = {"identity": identity, "name": name, "identity_state": state, "generation": 3}
        if retired_at is not None:
            until = retired_at + timedelta(days=7)
            if self.now >= until:
                raise IdentityApiError(410, "renamed", f"renamed to {renamed_to} on {retired_at.date().isoformat()}")
            out.update(state="retired", renamed_to=renamed_to,
                       notice=f"renamed to {renamed_to}; this name resolves until {until.date().isoformat()}")
        else:
            out["state"] = "live"
        return out


NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def test_a_name_resolves_through_its_alias_to_the_identity_mailbox_whoever_holds_a_lease() -> None:
    """A live seat's mail waits in ``mailbox/<id>``: the seat reads it when it binds. No directory row is read, and
    none could change the address."""
    engine = _Engine(NOW, {"marginalia": ("i12", "live", None, "")})
    assert resolve_send("marginalia", engine.alias) == SendTarget("i12", "identity")
    lapsed = _Engine(NOW, {"wren": ("i4", "lapsed", None, "")})
    assert resolve_send("wren", lapsed.alias) == SendTarget("i4", "identity")


def test_an_identity_id_goes_straight_to_its_mailbox() -> None:
    assert resolve_send("i7") == SendTarget("i7", "identity")
    assert looks_like_identity_id("i7") and not looks_like_identity_id("I7") and not looks_like_identity_id("i")


def test_an_agent_id_goes_to_its_own_mailbox_without_a_lookup() -> None:
    def boom(_name: str) -> dict[str, Any]:
        raise AssertionError("an agent id is never looked up")

    assert resolve_send("a0123456789abcdef", boom) == SendTarget("a0123456789abcdef", "agent")


def test_a_session_id_is_refused_naming_the_identity_the_session_is_bound_to() -> None:
    asked: list[str] = []

    def session(sid: str) -> dict[str, Any]:
        asked.append(sid)
        return {"identity": "i12"}

    with pytest.raises(DirectoryResolutionError) as raised:
        resolve_send(OLD_SESSION, None, session)
    text = str(raised.value)
    assert "is a session id" in text and "bound to i12" in text and "mailbox/i12" in text
    assert asked == [OLD_SESSION]


@pytest.mark.parametrize("lookup", ["none", "unknown", "down"])
def test_a_session_id_the_index_cannot_place_is_refused_naming_the_identity_shape(lookup: str) -> None:
    def unknown(_sid: str) -> dict[str, Any]:
        raise IdentityApiError(404, "unknown_session", "no such session")

    def down(_sid: str) -> dict[str, Any]:
        raise ConnectionError("engine down")

    fn = {"none": None, "unknown": unknown, "down": down}[lookup]
    with pytest.raises(DirectoryResolutionError, match=r"session id.*identity id \(an \"i\" and digits\) or its seat's name"):
        resolve_send(OLD_SESSION, None, fn)


def test_no_name_falls_back_to_a_directory_or_a_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown name, an engine without identity routes and no lookup at all are each refused: there is nothing
    to fall back to."""
    with pytest.raises(DirectoryResolutionError, match=r"no seat is named 'bob'.*ephemeral identity by its id only"):
        resolve_send("bob", _Engine(NOW).alias)

    def down(_name: str) -> dict[str, Any]:
        raise ConnectionError("no identity routes")

    with pytest.raises(DirectoryResolutionError, match=r"cannot resolve the name 'bob': the alias lookup failed"):
        resolve_send("bob", down)
    with pytest.raises(DirectoryResolutionError, match="no identity alias lookup is available"):
        resolve_send("bob", None)

    def server_error(_name: str) -> dict[str, Any]:
        raise IdentityApiError(500, "boom", "engine error")

    with pytest.raises(DirectoryResolutionError, match="the alias lookup failed"):
        resolve_send("bob", server_error)


def test_a_display_name_never_reaches_an_ephemeral() -> None:
    """``-n scratch42`` makes an ephemeral that takes no alias: the name is refused, and the refusal says an ephemeral
    is reached by id."""
    with pytest.raises(DirectoryResolutionError, match="ephemeral identity by its id only"):
        resolve_send("scratch42", _Engine(NOW).alias)
    assert resolve_send("i31") == SendTarget("i31", "identity"), "by id it is reached"


def test_a_name_the_alias_answers_without_an_identity_is_refused() -> None:
    with pytest.raises(DirectoryResolutionError, match="named no identity"):
        resolve_send("odd", lambda _name: {"state": "live"})


def test_the_build_specs_identity_mailbox_is_the_only_mailbox_watched() -> None:
    """``nx tuple wait`` parks on ``mailbox/<identity>`` and the board topics; a mailbox entry in the subscription list is
    not a second address, and no session mailbox is watched."""
    specs = build_specs(
        OLD_SESSION, [{"subspace": f"mailbox/{OLD_SESSION}"}, {"subspace": "mailbox/i99"}, {"subspace": "board/ci"}],
        identity="i12", waiter_token="w",
    )
    assert [s.subspace for s in specs] == ["mailbox/i12", "board/ci"]
    assert {s.announce.subscriber for s in specs} == {f"wait:{OLD_SESSION}"}


def test_a_retired_alias_delivers_with_a_notice_for_seven_days_and_is_refused_after() -> None:
    retired = NOW - timedelta(days=3)
    engine = _Engine(NOW, {"linda": ("i9", "live", retired, "lindy")})
    got = resolve_send("linda", engine.alias)
    assert (got.address, got.address_kind) == ("i9", "identity")
    assert got.notice == "renamed to lindy; this name resolves until 2026-11-22"
    engine.now = retired + timedelta(days=7)
    with pytest.raises(DirectoryResolutionError, match=r"^renamed to lindy on 2026-11-15$"):
        resolve_send("linda", engine.alias)


@pytest.mark.parametrize("name", ["Seminar seat: Bullock", ".hidden", "a/b", "x\ny"])
def test_a_name_no_seat_can_hold_is_refused_before_any_lookup(name: str) -> None:
    def boom(_name: str) -> dict[str, Any]:
        raise AssertionError("no lookup for a name that cannot resolve")

    with pytest.raises(DirectoryResolutionError, match="cannot be a seat's name"):
        resolve_send(name, boom)


def test_an_identity_shaped_from_that_is_not_the_senders_own_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.tuple_directory as td  # noqa: PLC0415

    monkeypatch.setattr(td, "_own_identity", lambda _sd, _pid: "i12")
    assert check_from_address_is_own("i12") == "i12"
    with pytest.raises(DirectoryResolutionError, match="is an identity but not this server's own"):
        check_from_address_is_own("i13")
    monkeypatch.setattr(td, "_own_identity", lambda _sd, _pid: "")
    with pytest.raises(DirectoryResolutionError, match="none resolved for this server"):
        check_from_address_is_own("i12")


def test_own_identity_takes_the_seat_whose_current_session_it_is_not_one_that_only_held_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The ``from`` guard's own-identity read shares the token-file rule: after ``/clear`` plus a ``/resume`` under
    another seat, the old seat's (newer) file lists the id but the new seat's file holds it."""
    import json  # noqa: PLC0415
    import os  # noqa: PLC0415

    import nexus.identity.token_header as th  # noqa: PLC0415
    import nexus.tuple_directory as td  # noqa: PLC0415

    directory = tmp_path / "identity"
    directory.mkdir()
    for pid, identity, current, mtime in ((1, "i9", "other", 2000), (2, "i12", OLD_SESSION, 1000)):
        path = directory / f"{pid}.1.token"
        path.write_text(json.dumps({"identity": identity, "generation": 1, "pid": pid, "started_at": 1,
                                    "session_id": current, "session_ids": [OLD_SESSION]}), encoding="utf-8")
        os.utime(path, (mtime, mtime))
    monkeypatch.setattr(th, "current", lambda: None)
    monkeypatch.setattr(td, "_own_session_ids", lambda _sd, _pid: [OLD_SESSION])
    assert td._own_identity(tmp_path, 1) == "i12"


def test_the_alias_lookup_builds_no_client_for_an_identity_or_agent_id_and_closes_the_one_it_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.identity.bind_client import IdentityClient  # noqa: PLC0415
    from nexus.tuple_directory import identity_alias_lookup  # noqa: PLC0415

    log: list[str] = []

    class _Client:
        def alias(self, name: str) -> dict[str, Any]:
            log.append(f"alias {name}")
            if name == "gone":
                raise IdentityApiError(404, "unknown_name", "no seat")
            return {"identity": "i4", "identity_state": "lapsed"}

        def close(self) -> None:
            log.append("close")

    def build() -> _Client:
        log.append("build")
        return _Client()

    monkeypatch.setattr(IdentityClient, "from_config", staticmethod(build))
    lookup = identity_alias_lookup()
    assert resolve_send("i4", lookup) == SendTarget("i4", "identity")
    assert resolve_send("a0123456789abcdef", lookup).address_kind == "agent"
    assert log == [], "no client for an id that is never looked up"
    assert resolve_send("wren", lookup) == SendTarget("i4", "identity")
    assert log == ["build", "alias wren", "close"]
    log.clear()
    with pytest.raises(DirectoryResolutionError):
        resolve_send("gone", lookup)  # 404: no seat holds the name
    assert log == ["build", "alias gone", "close"], "closed on the failing path too"
