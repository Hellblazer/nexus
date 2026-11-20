# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-252 (bead nexus-igea9): the ack of a develop red carries the poster's IDENTITY ID in ``from``, never a name.

``nx group ack-times`` and the engine's count match an ack on the identity that owns the red. A row posted by hand
with ``from=marginalia`` (the session alias, tuple b18af4e1, 2026-11-20) was counted as no ack. Every client path
that writes a ``kind=ack`` row to a ``board/ci/<topic>--owner`` subspace therefore stamps ``from`` with the caller's
own identity, whatever the caller typed, and refuses a caller with no identity rather than write a name.

All against a fake transport: the stamp is client-side, so no engine and no tenant are needed. The identity is
patched at ``nexus.ci_ack.own_identity``, the one place the client resolves it for this rule.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
import structlog
from click.testing import CliRunner
from structlog.testing import capture_logs

from nexus.commands import ci_cmd, tuple_cmd
from nexus.db.t2.http_tuple_store import HttpTupleStore
from nexus.mcp import core as mcp_core

OWNER_SUBSPACE = "board/ci/nexus-develop--owner"
TOPIC_KEY = {"topic": "nexus-develop--owner"}
SHA = "ae619ef56" + "0" * 31
ME = "i5"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(HttpTupleStore, "_rejected_sessions", {})
    monkeypatch.setenv("NX_SERVICE_TOKEN", "data-token")
    monkeypatch.delenv("NX_T1_SESSION", raising=False)


@pytest.fixture
def identity(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The caller's identity, mutable: ``identity[:] = []`` is a caller with none."""
    held = [ME]
    monkeypatch.setattr("nexus.ci_ack.own_identity", lambda: held[0] if held else "")
    return held


@pytest.fixture
def info_logs():
    old = structlog.get_config()["wrapper_class"]
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    try:
        yield
    finally:
        structlog.configure(wrapper_class=old)


class _Wire:
    """A tuple store over a fake transport that records every payload it was sent."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        #: The rows a descending read of the owner subspace answers, newest first.
        self.rows: list[dict[str, Any]] = []
        self.store = HttpTupleStore(base_url="http://mock.test")
        self.store._client = httpx.Client(transport=httpx.MockTransport(self._handle))
        self.store._invalidate_and_reresolve = lambda: None  # type: ignore[method-assign]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rd"):
            return httpx.Response(200, json={"tuples": self.rows, "order": "desc", "limit": 300})
        self.sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "b" * 64})


@pytest.fixture
def wire() -> _Wire:
    return _Wire()


def _ack_dims(**over: str) -> dict[str, str]:
    return {"kind": "ack", "from": ME, "sha": SHA, **over}


# ── A. the store: the one place every client write passes through ─────────────────────────────────────────


def test_a_hand_typed_alias_in_an_ack_is_restamped_with_the_callers_identity(wire: _Wire, identity: list[str]) -> None:
    wire.store.out(OWNER_SUBSPACE, TOPIC_KEY, _ack_dims(**{"from": "marginalia"}), nonce="n1")
    assert wire.sent[0]["dims"] == _ack_dims(), "the alias was replaced; every other dim is as typed"
    assert wire.sent[0]["subspace"] == OWNER_SUBSPACE and wire.sent[0]["keys"] == TOPIC_KEY


def test_a_restamp_logs_one_info_line_and_an_own_id_logs_nothing(wire: _Wire, identity: list[str], info_logs: None) -> None:
    with capture_logs() as logs:
        wire.store.out(OWNER_SUBSPACE, TOPIC_KEY, _ack_dims(**{"from": "marginalia"}), nonce="n1")
        wire.store.out(OWNER_SUBSPACE, TOPIC_KEY, _ack_dims(), nonce="n2")
    restamps = [e for e in logs if e["log_level"] == "info" and "ack" in e["event"]]
    assert len(restamps) == 1, logs
    assert restamps[0]["typed"] == "marginalia" and restamps[0]["stamped"] == ME


def test_an_ack_with_no_from_gets_the_identity(wire: _Wire, identity: list[str]) -> None:
    dims = {"kind": "ack", "sha": SHA}
    wire.store.out(OWNER_SUBSPACE, TOPIC_KEY, dims, nonce="n1")
    assert wire.sent[0]["dims"]["from"] == ME
    assert "from" not in dims, "the caller's own dict is not mutated"


def test_a_caller_with_no_identity_is_refused_and_nothing_is_written(wire: _Wire, identity: list[str]) -> None:
    identity[:] = []
    with pytest.raises(ValueError, match="identity"):
        wire.store.out(OWNER_SUBSPACE, TOPIC_KEY, _ack_dims(**{"from": "marginalia"}), nonce="n1")
    assert wire.sent == []


def test_out_replace_is_stamped_too(wire: _Wire, identity: list[str]) -> None:
    wire.store._post = lambda path, payload, **kw: (wire.sent.append(payload) or {"id": "b" * 64, "outcome": "created"})  # type: ignore[method-assign]
    wire.store.out_replace(OWNER_SUBSPACE, TOPIC_KEY, _ack_dims(**{"from": "marginalia"}), nonce="n1")
    assert wire.sent[0]["dims"]["from"] == ME


@pytest.mark.parametrize(
    ("subspace", "dims"),
    [
        pytest.param(OWNER_SUBSPACE, {"kind": "owner", "from": "engine", "sha": SHA}, id="the-engines-own-owner-row"),
        pytest.param(OWNER_SUBSPACE, {"kind": "clear", "from": "engine", "sha": SHA}, id="a-clear-row"),
        pytest.param("board/ci/nexus-develop--done", {"kind": "ack", "from": "marginalia"}, id="the-done-topic"),
        pytest.param("board/some-topic", {"kind": "ack", "from": "marginalia"}, id="a-plain-board-topic"),
        pytest.param("mailbox/i9", {"kind": "ack", "from": "marginalia"}, id="a-mailbox-reply"),
    ],
)
def test_only_an_ack_on_an_owner_subspace_is_restamped(wire: _Wire, identity: list[str], subspace: str, dims: dict[str, str]) -> None:
    identity[:] = []  # a stamp would refuse: a row that is not the rule's subject must pass untouched
    wire.store.out(subspace, TOPIC_KEY, dict(dims), nonce="n1")
    assert wire.sent[0]["dims"] == dims


# ── A. the CLI and the MCP tool reach the same store ──────────────────────────────────────────────────────


def test_nx_tuple_out_restamps_a_hand_typed_ack(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tuple_cmd, "_store", lambda: wire.store)
    result = CliRunner().invoke(tuple_cmd.tuple_group, [
        "out", OWNER_SUBSPACE, "--key", "topic=nexus-develop--owner", "--dim", "kind=ack", "--dim", "from=marginalia",
        "--dim", f"sha={SHA}", "--nonce", "n1", "--body", "ack"])
    assert result.exit_code == 0, result.output
    assert wire.sent[0]["dims"]["from"] == ME


def test_nx_tuple_out_refuses_an_ack_when_the_caller_has_no_identity(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    identity[:] = []
    monkeypatch.setattr(tuple_cmd, "_store", lambda: wire.store)
    result = CliRunner().invoke(tuple_cmd.tuple_group, [
        "out", OWNER_SUBSPACE, "--key", "topic=nexus-develop--owner", "--dim", "kind=ack", "--dim", "from=marginalia",
        "--dim", f"sha={SHA}", "--nonce", "n1"])
    assert result.exit_code == 1 and "identity" in result.output, result.output
    assert wire.sent == []


def test_the_mcp_tuple_out_restamps_and_refuses_the_same_way(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    class _Db:
        tuples = wire.store

    monkeypatch.setattr(mcp_core, "_t2_index_write", lambda fn, *, op: fn(_Db()))
    out = mcp_core.tuple_out(OWNER_SUBSPACE, TOPIC_KEY, _ack_dims(**{"from": "marginalia"}), nonce="n1")
    assert out == "b" * 64 and wire.sent[0]["dims"]["from"] == ME
    identity[:] = []
    refused = mcp_core.tuple_out(OWNER_SUBSPACE, TOPIC_KEY, _ack_dims(**{"from": "marginalia"}), nonce="n2")
    assert "identity" in refused and len(wire.sent) == 1, refused


# ── the helper the aura text names: nx ci ack <sha prefix> ────────────────────────────────────────────────


def _owner_row(sha: str, at: str, kind: str = "owner", **body: Any) -> dict[str, Any]:
    return {"id": f"{kind}-{at}".ljust(64, "0"), "subspace": OWNER_SUBSPACE, "keys": TOPIC_KEY, "created_at": at,
            "dims": {"kind": kind, "from": "engine", "sha": sha}, "body": json.dumps(body)}


def _open_red(wire: _Wire) -> None:
    """Newest first: a refresh row of the open red naming a second tip, its first row, and an older red's clear."""
    wire.rows = [
        _owner_row("f" * 40, "2026-11-20T14:20:00Z", owner=ME, refresh=True, suspects=[SHA]),
        _owner_row(SHA, "2026-11-20T14:10:00Z", owner=ME, refresh=False),
        _owner_row("e" * 40, "2026-11-20T10:30:00Z", kind="clear"),
        _owner_row("d" * 40, "2026-11-20T10:00:00Z", owner="i9"),
    ]


def _ack(args: list[str], wire: _Wire, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(ci_cmd, "_store", lambda: wire.store)
    return CliRunner().invoke(ci_cmd.ci_group, ["ack", *args])


def test_nx_ci_ack_writes_the_ack_row_from_the_callers_identity_id(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    _open_red(wire)
    result = _ack([SHA], wire, monkeypatch)
    assert result.exit_code == 0, result.output
    sent = wire.sent[0]
    assert sent["subspace"] == OWNER_SUBSPACE and sent["keys"] == TOPIC_KEY
    assert sent["dims"] == {"kind": "ack", "from": ME, "sha": SHA}
    assert sent["nonce"], "a fresh nonce: the template is keys+nonce"
    assert ME in result.output and SHA[:7] in result.output


def test_nx_ci_ack_resolves_a_sha_prefix_against_the_open_reds_tips(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    _open_red(wire)
    assert _ack([SHA[:7]], wire, monkeypatch).exit_code == 0
    assert wire.sent[-1]["dims"]["sha"] == SHA, "the prefix is spelled out in full before it is posted"
    assert _ack(["f" * 7], wire, monkeypatch).exit_code == 0
    assert wire.sent[-1]["dims"]["sha"] == "f" * 40, "a tip named by a refresh row counts as one"


@pytest.mark.parametrize("arg", ["abc", "not-hex", "d" * 40, "d" * 7, "1234567"])
def test_nx_ci_ack_refuses_a_sha_that_is_no_tip_of_the_open_red(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch, arg: str) -> None:
    """Too short, not hex, the sha of a red a clear row already ended, and one that was never a red: nothing is posted."""
    _open_red(wire)
    result = _ack([arg], wire, monkeypatch)
    assert result.exit_code != 0, result.output
    assert wire.sent == []


def test_nx_ci_ack_says_so_when_no_red_is_open(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    wire.rows = [_owner_row("e" * 40, "2026-11-20T10:30:00Z", kind="clear")]
    result = _ack([SHA[:7]], wire, monkeypatch)
    assert result.exit_code != 0 and "no open red" in result.output, result.output
    assert wire.sent == []


def test_nx_ci_ack_refuses_without_an_identity_and_says_how_to_check(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    _open_red(wire)
    identity[:] = []
    result = _ack([SHA], wire, monkeypatch)
    assert result.exit_code != 0 and "nx identity show" in result.output, result.output
    assert wire.sent == []


def test_nx_ci_ack_takes_a_topic_and_keeps_the_develop_default(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    _open_red(wire)
    assert _ack([SHA, "--topic", "halcyon-main--owner"], wire, monkeypatch).exit_code == 0
    assert wire.sent[-1]["subspace"] == "board/ci/halcyon-main--owner"
    assert wire.sent[-1]["keys"] == {"topic": "halcyon-main--owner"}
    assert _ack([SHA, "--topic", "nexus-develop"], wire, monkeypatch).exit_code != 0, "a topic names its --owner post"
