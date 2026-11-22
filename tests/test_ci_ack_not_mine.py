# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx ci ack --not-mine <sha>`` (RDR-252 post-close audit S1, bead nexus-igea9): the owner's "this red is not mine".

It posts a row of kind ``disclaim`` on the same ``--owner`` subspace an ack goes to, with the same sha handling and the
same identity stamping. The engine acts on it in a later engine cut (a disclaim from the current owner hands the red on
at once); until then the row is recorded only. All against a fake transport, as tests/test_ci_ack_identity.py is.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from nexus.commands import ci_cmd
from nexus.db.t2.http_tuple_store import HttpTupleStore

OWNER_SUBSPACE = "board/ci/nexus-develop--owner"
TOPIC_KEY = {"topic": "nexus-develop--owner"}
SHA = "ae619ef56" + "0" * 31
ME = "i5"
EP = "2026-11-20T14:00:00Z"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(HttpTupleStore, "_rejected_sessions", {})
    monkeypatch.setenv("NX_SERVICE_TOKEN", "data-token")
    monkeypatch.delenv("NX_T1_SESSION", raising=False)
    monkeypatch.setattr("nexus.ci_ack.identity_view", lambda who: {"identity": who, "kind": "seat"}, raising=False)


@pytest.fixture
def identity(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    held = [ME]
    monkeypatch.setattr("nexus.ci_ack.own_identity", lambda: held[0] if held else "")
    return held


class _Wire:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.rows: list[dict[str, Any]] = []
        self.store = HttpTupleStore(base_url="http://mock.test")
        self.store._client = httpx.Client(transport=httpx.MockTransport(self._handle))
        self.store._invalidate_and_reresolve = lambda: None  # type: ignore[method-assign]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rd"):
            return httpx.Response(200, json={"tuples": self.rows, "order": "desc", "limit": 300})
        self.sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "d" * 64})


@pytest.fixture
def wire() -> _Wire:
    return _Wire()


def _owner_row(sha: str, at: str, owner: str | None = ME, **body: Any) -> dict[str, Any]:
    fields = {"owner": owner, "episode_at": EP, **body}
    return {"id": f"owner-{at}".ljust(64, "0"), "subspace": OWNER_SUBSPACE, "keys": TOPIC_KEY, "created_at": at,
            "dims": {"kind": "owner", "from": "engine", "sha": sha}, "body": json.dumps(fields)}


def _open_red(wire: _Wire, owner: str | None = ME) -> None:
    wire.rows = [_owner_row(SHA, "2026-11-20T14:10:00Z", owner=owner, owner_alias="tamsin")]


def _run(args: list[str], wire: _Wire, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(ci_cmd, "_store", lambda: wire.store)
    return CliRunner().invoke(ci_cmd.ci_group, ["ack", *args])


def test_not_mine_posts_a_disclaim_row_with_the_full_sha_and_the_callers_identity(
    wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _open_red(wire)
    result = _run(["--not-mine", SHA[:7]], wire, monkeypatch)
    assert result.exit_code == 0, result.output
    [sent] = wire.sent
    assert sent["subspace"] == OWNER_SUBSPACE and sent["keys"] == TOPIC_KEY
    assert sent["dims"] == {"kind": "disclaim", "from": ME, "sha": SHA}, "kind disclaim, the prefix spelled out in full"
    assert sent["nonce"], "a fresh nonce: the template is keys+nonce"
    assert "disclaimed" in result.output and SHA[:7] in result.output and ME in result.output
    assert "recorded" in result.output, "the output says the engine does not act on it yet"


def test_a_plain_ack_is_unchanged_by_the_new_flag(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    _open_red(wire)
    assert _run([SHA[:7]], wire, monkeypatch).exit_code == 0
    assert wire.sent[0]["dims"] == {"kind": "ack", "from": ME, "sha": SHA}


def test_the_note_rides_in_the_body_and_is_optional(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    _open_red(wire)
    assert _run(["--not-mine", SHA[:7], "--note", "marginalia is fixing it"], wire, monkeypatch).exit_code == 0
    assert wire.sent[-1]["body"] == "marginalia is fixing it"
    assert _run(["--not-mine", SHA[:7]], wire, monkeypatch).exit_code == 0
    assert not wire.sent[-1].get("body")


@pytest.mark.parametrize("note", ["two\nlines", "x" * 201, "bell\x07"])
def test_a_note_with_a_control_character_or_over_200_characters_is_refused(
    wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch, note: str,
) -> None:
    _open_red(wire)
    result = _run(["--not-mine", SHA[:7], "--note", note], wire, monkeypatch)
    assert result.exit_code != 0 and "note" in result.output and wire.sent == []


def test_a_note_without_not_mine_is_refused(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    _open_red(wire)
    result = _run([SHA[:7], "--note", "hi"], wire, monkeypatch)
    assert result.exit_code != 0 and "--not-mine" in result.output and wire.sent == []


@pytest.mark.parametrize("arg", ["abc", "not-hex", "d" * 40, "1234567"])
def test_a_sha_that_is_no_tip_of_the_open_red_is_refused_as_an_ack_does(
    wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch, arg: str,
) -> None:
    _open_red(wire)
    result = _run(["--not-mine", arg], wire, monkeypatch)
    assert result.exit_code != 0, result.output
    assert wire.sent == []


def test_a_caller_who_is_not_the_owner_is_refused_and_told_the_engine_would_ignore_it(
    wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _open_red(wire, owner="i9")
    result = _run(["--not-mine", SHA[:7]], wire, monkeypatch)
    assert result.exit_code != 0 and wire.sent == []
    assert "owned by i9" in result.output and "disclaim" in result.output and "ignore" in result.output


def test_no_open_red_and_no_identity_are_refused(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    wire.rows = []
    result = _run(["--not-mine", SHA[:7]], wire, monkeypatch)
    assert result.exit_code != 0 and "no open red" in result.output and wire.sent == []
    _open_red(wire)
    identity[:] = []
    result = _run(["--not-mine", SHA[:7]], wire, monkeypatch)
    assert result.exit_code != 0 and "nx identity show" in result.output and wire.sent == []


def test_a_hand_typed_alias_in_a_disclaim_is_restamped_with_the_callers_identity(wire: _Wire, identity: list[str]) -> None:
    """The store stamps a disclaim as it stamps an ack: ``from`` is the poster's identity id, never a name; and a caller
    with no identity is refused rather than written under a name."""
    wire.store.out(OWNER_SUBSPACE, TOPIC_KEY, {"kind": "disclaim", "from": "marginalia", "sha": SHA}, nonce="n1")
    assert wire.sent[0]["dims"]["from"] == ME
    identity[:] = []
    with pytest.raises(ValueError, match="identity"):
        wire.store.out(OWNER_SUBSPACE, TOPIC_KEY, {"kind": "disclaim", "from": "marginalia", "sha": SHA}, nonce="n2")
    assert len(wire.sent) == 1


def test_a_disclaim_on_another_subspace_is_not_restamped(wire: _Wire, identity: list[str]) -> None:
    identity[:] = []
    wire.store.out("board/ci/nexus-develop--done", TOPIC_KEY, {"kind": "disclaim", "from": "marginalia"}, nonce="n1")
    assert wire.sent[0]["dims"]["from"] == "marginalia"


def test_the_help_says_what_it_does_now_and_after_the_engine_cut() -> None:
    result = CliRunner().invoke(ci_cmd.ci_group, ["ack", "--help"])
    text = " ".join(result.output.split())
    assert "--not-mine" in text and "--note" in text
    assert "recorded only" in text and "hands the red on at once" in text
