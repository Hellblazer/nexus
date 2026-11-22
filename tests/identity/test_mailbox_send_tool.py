# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-253 Step 5: the ``mailbox_send`` TOOL consults the alias route and reports what it found.

``tests/identity/test_mail_addressing.py`` pins ``resolve_send``; this pins the wiring in ``nexus.mcp.core``:
the alias and session-index lookups are built and passed through, a retired alias's notice reaches the result under
``notice``, a 410 becomes the tool's refusal with nothing written, an unnoticed send has no ``notice`` key, and a send
to a session id is refused naming the identity (RDR-253 Phase 2). Engine-free: the tuple store is a recorder and the
alias route a stand-in.
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
    written: list[tuple[str, dict[str, str], dict[str, str], str]] = field(default_factory=list)

    def out(self, subspace: str, keys: dict[str, str], dims: dict[str, str], body: str, nonce: str) -> str:
        self.written.append((subspace, keys, dims, body))
        return f"tuple-{len(self.written)}"


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    tuples = _Tuples()
    asked: list[str] = []
    answers: dict[str, Any] = {}
    sessions: dict[str, str] = {}

    def alias(name: str) -> dict[str, Any]:
        asked.append(name)
        answer = answers[name]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def session(sid: str) -> dict[str, Any]:
        if sid not in sessions:
            raise IdentityApiError(404, "unknown_session", "no such session")
        return {"identity": sessions[sid]}

    monkeypatch.setattr(core, "identity_alias_lookup", lambda: alias)
    monkeypatch.setattr(core, "identity_session_lookup", lambda: session)
    monkeypatch.setattr(core, "resolve_default_from", lambda: SENDER)
    monkeypatch.setattr(core, "_t2_index_write", lambda fn, op="": fn(type("Db", (), {"tuples": tuples})()))
    return tuples, asked, answers, sessions


def test_a_name_goes_through_the_alias_route_and_a_plain_send_has_no_notice_key(wired) -> None:
    tuples, asked, answers, _sessions = wired
    answers["marginalia"] = {"identity": "i12", "identity_state": "live", "state": "live"}
    result = core.mailbox_send("marginalia", body="hi")
    assert asked == ["marginalia"], "the tool consulted alias/<name>"
    assert result == {"tuple_id": "tuple-1", "to": "i12", "address_kind": "identity", "from": SENDER}
    assert tuples.written[0][0] == "mailbox/i12"


def test_a_retired_alias_in_its_grace_delivers_and_the_notice_reaches_the_result(wired) -> None:
    tuples, _asked, answers, _sessions = wired
    notice = "renamed to lindy; this name resolves until 2026-11-22"
    answers["linda"] = {"identity": "i9", "identity_state": "live", "state": "retired", "notice": notice}
    result = core.mailbox_send("linda", body="hi")
    assert result["notice"] == notice and result["to"] == "i9" and "error" not in result
    assert tuples.written and tuples.written[0][0] == "mailbox/i9"


def test_a_gone_alias_is_the_tools_refusal_and_writes_nothing(wired) -> None:
    tuples, _asked, answers, _sessions = wired
    answers["linda"] = IdentityApiError(410, "renamed", "renamed to lindy on 2026-11-15")
    result = core.mailbox_send("linda", body="hi")
    assert "renamed to lindy on 2026-11-15" in result["error"]
    assert tuples.written == []


def test_a_session_id_is_refused_naming_the_identity_and_writes_nothing(wired) -> None:
    tuples, _asked, _answers, sessions = wired
    sessions[OLD_SESSION] = "i12"
    result = core.mailbox_send(OLD_SESSION, body="hi")
    assert "is a session id" in result["error"] and "send to i12" in result["error"] and "mailbox/i12" in result["error"]
    assert tuples.written == []


def test_a_session_id_the_index_cannot_place_is_refused_and_writes_nothing(wired) -> None:
    tuples, *_ = wired
    result = core.mailbox_send(OLD_SESSION, body="hi")
    assert "is a session id" in result["error"] and "identity id" in result["error"]
    assert tuples.written == []


def test_a_name_no_seat_holds_is_refused_and_writes_nothing(wired) -> None:
    """The directory is gone: a display name only an ephemeral holds reaches nobody, by id alone."""
    tuples, _asked, answers, _sessions = wired
    answers["scratch42"] = IdentityApiError(404, "unknown_name", "no seat has been named scratch42")
    result = core.mailbox_send("scratch42", body="hi")
    assert "no seat is named 'scratch42'" in result["error"] and "ephemeral identity by its id only" in result["error"]
    assert tuples.written == []
    assert core.mailbox_send("i31", body="hi")["to"] == "i31"


def test_an_agent_id_is_mailed_in_its_own_mailbox(wired) -> None:
    tuples, *_ = wired
    result = core.mailbox_send("a0123456789abcdef", body="hi")
    assert (result["to"], result["address_kind"]) == ("a0123456789abcdef", "agent")
    assert tuples.written[0][0] == "mailbox/a0123456789abcdef"
