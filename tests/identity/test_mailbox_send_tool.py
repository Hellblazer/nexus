# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-253 Step 5: the ``mailbox_send`` TOOL consults the alias route and reports what it found.

``tests/identity/test_mail_addressing.py`` pins ``resolve_send``; this pins the wiring in ``nexus.mcp.core``:
the alias lookup is built and passed through, a retired alias's notice reaches the result under ``notice``, a 410
becomes the tool's refusal with nothing written, and an unnoticed send has no ``notice`` key. Engine-free: the
tuple store is a recorder and the alias route a stand-in; reverting ``mailbox_send`` to ``resolve_send_address``
fails every test here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from nexus.identity.bind_client import IdentityApiError
from nexus.mcp import core

OLD_SESSION = "11111111-2222-4333-8444-555555555555"
SENDER = "22222222-2222-4333-8444-555555555555"


@dataclass
class _Tuples:
    holders: dict[str, list[str]] = field(default_factory=dict)
    written: list[tuple[str, dict[str, str], dict[str, str], str]] = field(default_factory=list)

    def rd(self, subspace: str, keys: dict[str, str], n: int, since: Any = None) -> list[Any]:
        rows = [type("R", (), {"dims": {"session_id": s}, "created_at": "t", "id": "r"})() for s in self.holders.get(keys["name"], [])]
        return rows

    def out(self, subspace: str, keys: dict[str, str], dims: dict[str, str], body: str, nonce: str) -> str:
        self.written.append((subspace, keys, dims, body))
        return f"tuple-{len(self.written)}"


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    tuples = _Tuples()
    asked: list[str] = []
    answers: dict[str, Any] = {}
    bound: dict[str, tuple[str, str]] = {}

    def alias(name: str) -> dict[str, Any]:
        asked.append(name)
        answer = answers[name]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(core, "identity_alias_lookup", lambda: alias)
    monkeypatch.setattr(core, "identity_holder_lookup", lambda: lambda session: bound.get(session))
    monkeypatch.setattr(core, "resolve_default_from", lambda: SENDER)
    monkeypatch.setattr(core, "_t2_index_write", lambda fn, op="": fn(type("Db", (), {"tuples": tuples})()))
    return tuples, asked, answers, bound


def test_a_name_goes_through_the_alias_route_and_a_plain_send_has_no_notice_key(wired) -> None:
    tuples, asked, answers, _bound = wired
    answers["marginalia"] = {"identity": "i12", "identity_state": "live", "state": "live"}
    result = core.mailbox_send("marginalia", body="hi")
    assert asked == ["marginalia"], "the tool consulted alias/<name>"
    assert result == {"tuple_id": "tuple-1", "to": "i12", "address_kind": "identity", "from": SENDER}
    assert tuples.written[0][0] == "mailbox/i12"


def test_a_retired_alias_in_its_grace_delivers_and_the_notice_reaches_the_result(wired) -> None:
    tuples, _asked, answers, _bound = wired
    notice = "renamed to lindy; this name resolves until 2026-11-22"
    answers["linda"] = {"identity": "i9", "identity_state": "live", "state": "retired", "notice": notice}
    result = core.mailbox_send("linda", body="hi")
    assert result["notice"] == notice and result["to"] == "i9" and "error" not in result
    assert tuples.written and tuples.written[0][0] == "mailbox/i9"


def test_a_gone_alias_is_the_tools_refusal_and_writes_nothing(wired) -> None:
    tuples, _asked, answers, _bound = wired
    answers["linda"] = IdentityApiError(410, "renamed", "renamed to lindy on 2026-11-15")
    result = core.mailbox_send("linda", body="hi")
    assert "renamed to lindy on 2026-11-15" in result["error"]
    assert tuples.written == []


def test_a_live_seat_with_a_directory_holder_is_mailed_in_the_holders_session_mailbox(wired) -> None:
    tuples, _asked, answers, _bound = wired
    tuples.holders["marginalia"] = [OLD_SESSION]
    answers["marginalia"] = {"identity": "i12", "identity_state": "live", "state": "live"}
    result = core.mailbox_send("marginalia", body="hi")
    assert (result["to"], result["address_kind"]) == (OLD_SESSION, "session")


def test_a_display_name_that_only_an_ephemeral_holds_is_refused_by_the_tool_and_writes_nothing(wired) -> None:
    tuples, _asked, answers, bound = wired
    tuples.holders["scratch42"] = [OLD_SESSION]
    answers["scratch42"] = IdentityApiError(404, "unknown_name", "no seat has been named scratch42")
    bound[OLD_SESSION] = ("i31", "ephemeral")
    result = core.mailbox_send("scratch42", body="hi")
    assert "reached by id only" in result["error"] and "send to i31" in result["error"]
    assert tuples.written == []
    assert core.mailbox_send("i31", body="hi")["to"] == "i31"
