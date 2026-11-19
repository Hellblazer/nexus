# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-253 Step 5: mail addressed by identity, in a mixed fleet of old and new clients.

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
class _Row:
    dims: dict[str, str]
    created_at: str = "2026-11-18T00:00:00Z"
    id: str = "r1"


@dataclass
class _Tuples:
    """``directory/<name>`` rows, as the lease side writes them."""

    holders: dict[str, list[str]] = field(default_factory=dict)
    reads: list[str] = field(default_factory=list)

    def rd(self, subspace: str, keys: dict[str, str], n: int, since: Any = None) -> list[_Row]:
        self.reads.append(subspace)
        return [_Row({"session_id": s}) for s in self.holders.get(keys["name"], [])]


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


def test_an_alias_whose_seat_a_new_client_holds_resolves_to_the_identity_mailbox() -> None:
    engine = _Engine(NOW, {"marginalia": ("i12", "live", None, "")})
    tuples = _Tuples({"marginalia": [OLD_SESSION]})
    assert resolve_send("marginalia", tuples, engine.alias) == SendTarget("i12", "identity")
    assert tuples.reads == [], "a live seat never needs the directory"


def test_an_identity_id_goes_straight_to_its_mailbox() -> None:
    assert resolve_send("i7", _Tuples(), None) == SendTarget("i7", "identity")
    assert looks_like_identity_id("i7") and not looks_like_identity_id("I7") and not looks_like_identity_id("i")


def test_a_seat_with_no_new_client_binding_falls_back_to_the_directory_and_the_session_mailbox() -> None:
    """New-client sender, old-client receiver: the seat is lapsed (nobody holds its lease), the old client holds
    ``directory/<name>``, so the mail goes to that session's mailbox."""
    engine = _Engine(NOW, {"wren": ("i4", "lapsed", None, "")})
    tuples = _Tuples({"wren": [OLD_SESSION]})
    assert resolve_send("wren", tuples, engine.alias) == SendTarget(OLD_SESSION, "session")
    # nobody holds the name either: the mail waits in the seat's mailbox for its next bind
    assert resolve_send("wren", _Tuples(), engine.alias) == SendTarget("i4", "identity")


def test_an_old_client_sender_reaches_a_new_client_receiver_through_the_session_mailbox() -> None:
    """Old-client sender (no alias lookup): the directory names the new client's session, and a new client
    subscribes its session mailbox too (dual subscription), so the mail is delivered."""
    tuples = _Tuples({"marginalia": [OLD_SESSION]})
    assert resolve_send("marginalia", tuples, None) == SendTarget(OLD_SESSION, "session")
    specs = build_specs(OLD_SESSION, [{"subspace": f"mailbox/{OLD_SESSION}"}, {"subspace": "mailbox/i12"}],
                        waiter_token="w")
    assert [s.subspace for s in specs] == [f"mailbox/{OLD_SESSION}", "mailbox/i12"]


def test_an_unknown_name_or_an_engine_without_identity_routes_is_the_directorys() -> None:
    tuples = _Tuples({"bob": [OLD_SESSION]})
    assert resolve_send("bob", tuples, _Engine(NOW).alias) == SendTarget(OLD_SESSION, "session")

    def down(_name: str) -> dict[str, Any]:
        raise ConnectionError("no identity routes")

    assert resolve_send("bob", tuples, down) == SendTarget(OLD_SESSION, "session")


def test_a_retired_alias_delivers_with_a_notice_for_seven_days_and_is_refused_after() -> None:
    retired = NOW - timedelta(days=3)
    engine = _Engine(NOW, {"linda": ("i9", "live", retired, "lindy")})
    got = resolve_send("linda", _Tuples(), engine.alias)
    assert (got.address, got.address_kind) == ("i9", "identity")
    assert got.notice == "renamed to lindy; this name resolves until 2026-11-22"
    engine.now = retired + timedelta(days=7)
    with pytest.raises(DirectoryResolutionError, match=r"^renamed to lindy on 2026-11-15$"):
        resolve_send("linda", _Tuples(), engine.alias)


def test_an_identity_shaped_from_that_is_not_the_senders_own_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.tuple_directory as td  # noqa: PLC0415

    monkeypatch.setattr(td, "_own_identity", lambda _sd, _pid: "i12")
    assert check_from_address_is_own("i12") == "i12"
    with pytest.raises(DirectoryResolutionError, match="is an identity but not this server's own"):
        check_from_address_is_own("i13")
    monkeypatch.setattr(td, "_own_identity", lambda _sd, _pid: "")
    with pytest.raises(DirectoryResolutionError, match="none resolved for this server"):
        check_from_address_is_own("i12")


def test_a_name_held_by_two_sessions_is_still_refused_when_the_seat_is_not_live() -> None:
    engine = _Engine(NOW, {"wren": ("i4", "lapsed", None, "")})
    tuples = _Tuples({"wren": [OLD_SESSION, "22222222-2222-4333-8444-555555555555"]})
    with pytest.raises(DirectoryResolutionError, match="held by more than one session"):
        resolve_send("wren", tuples, engine.alias)
