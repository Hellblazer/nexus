# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-252 (bead nexus-igea9): the ack of a develop red carries the poster's IDENTITY ID in ``from``, never a name.

The engine counts an ack by the row's sender-session stamp, mapped through the session index to the owner's identity;
it never reads ``from``. ``from`` is the label ``nx group ack-times`` and people read. A row posted by hand with
``from=marginalia`` (the session alias, tuple b18af4e1, 2026-11-20) still stopped the hand-off, and only ack-times
counted it as no ack. Every client path that writes a ``kind=ack`` row to a ``board/ci/<topic>--owner`` subspace
therefore stamps ``from`` with the caller's own identity (the token record's), whatever the caller typed, and refuses
a caller with no identity rather than write a name.

All against a fake transport: the stamp is client-side, so no engine and no tenant are needed. Most tests patch the
identity at ``nexus.ci_ack.own_identity``; ``TestTheRealResolver`` drives the real one through token-file fixtures.
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


#: What the identity service answers for an identity (``IdentityClient.show``); an identity not named here is a seat.
VIEWS: dict[str, dict[str, Any]] = {}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(HttpTupleStore, "_rejected_sessions", {})
    monkeypatch.setenv("NX_SERVICE_TOKEN", "data-token")
    monkeypatch.delenv("NX_T1_SESSION", raising=False)
    VIEWS.clear()
    monkeypatch.setattr("nexus.ci_ack.identity_view", lambda who: VIEWS.get(who, {"identity": who, "kind": "seat"}), raising=False)


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
        if request.url.path.endswith("/ack"):
            return httpx.Response(200, json={"reply_id": "c" * 64})
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



# ── an ack that rides on a claim's reply is written by the store's ack(), not out() ────────────────────────


def _reply(subspace: str, dims: dict[str, str]) -> Any:
    from nexus.db.t2.records import ReplySpec  # noqa: PLC0415

    return ReplySpec(subspace=subspace, keys=TOPIC_KEY, dims=dims, body="x")


def test_an_ack_reply_to_an_owner_subspace_is_restamped_like_an_out(wire: _Wire, identity: list[str]) -> None:
    wire.store.ack("claim-1", "i5", _reply(OWNER_SUBSPACE, _ack_dims(**{"from": "marginalia"})))
    assert wire.sent[0]["reply"]["dims"] == _ack_dims(), "the reply is an out to the engine, so it is stamped as one"
    identity[:] = []
    with pytest.raises(ValueError, match="identity"):
        wire.store.ack("claim-2", "i5", _reply(OWNER_SUBSPACE, _ack_dims()))
    assert len(wire.sent) == 1, "a refused reply sends nothing"


def test_an_ack_with_no_reply_or_a_reply_elsewhere_needs_no_identity(wire: _Wire, identity: list[str]) -> None:
    identity[:] = []
    wire.store.ack("claim-1", "i5")
    wire.store.ack("claim-2", "i5", _reply("mailbox/i9", {"kind": "ack", "from": "marginalia"}))
    assert wire.sent[1]["reply"]["dims"] == {"kind": "ack", "from": "marginalia"}


# ── the real resolver: the token record, and nothing else ─────────────────────────────────────────────────


class TestTheRealResolver:
    """No patching of ``own_identity``: the token file the process names, read the way ``nx group`` reads it."""

    @pytest.fixture(autouse=True)
    def _token_state(self) -> Any:
        from nexus.identity import token_header  # noqa: PLC0415

        token_header.reset_for_tests()
        yield
        token_header.reset_for_tests()

    @staticmethod
    def _token(tmp_path: Any, monkeypatch: pytest.MonkeyPatch, **over: Any) -> None:
        record = {"identity": "i5", "generation": 1, "pid": 1, "started_at": 1, "token": "t-secret",
                  "session_id": "3d4bd7aa-1691-4bc1-b59b-0af9dccfd153", **over}
        path = tmp_path / "1.1.token"
        path.write_text(json.dumps(record), encoding="utf-8")
        monkeypatch.setenv("NX_IDENTITY_TOKEN", str(path))

    def test_a_usable_token_file_names_the_identity_and_the_store_stamps_it(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, wire: _Wire,
    ) -> None:
        self._token(tmp_path, monkeypatch)
        wire.store.out(OWNER_SUBSPACE, TOPIC_KEY, _ack_dims(**{"from": "marginalia"}), nonce="n1")
        assert wire.sent[0]["dims"]["from"] == "i5"

    def test_a_token_file_with_no_token_is_no_identity(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, wire: _Wire,
    ) -> None:
        self._token(tmp_path, monkeypatch, token=None)
        with pytest.raises(ValueError, match="identity"):
            wire.store.out(OWNER_SUBSPACE, TOPIC_KEY, _ack_dims(), nonce="n1")

    def test_a_session_bound_identity_is_not_a_fallback(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, wire: _Wire,
    ) -> None:
        """A shared-T1 child with no token of its own must not land on its parent's session-bound identity."""
        monkeypatch.setenv("NX_IDENTITY_TOKEN", str(tmp_path / "absent.token"))
        monkeypatch.setattr("nexus.tuple_directory._own_identity", lambda _sd, _pid: "i9")
        with pytest.raises(ValueError, match="identity"):
            wire.store.out(OWNER_SUBSPACE, TOPIC_KEY, _ack_dims(), nonce="n1")
        assert wire.sent == []

    def test_a_resolver_failure_is_raised_not_read_as_no_identity(
        self, monkeypatch: pytest.MonkeyPatch, wire: _Wire,
    ) -> None:
        def broken() -> Any:
            raise RuntimeError("the token reader drifted")

        monkeypatch.setattr("nexus.identity.token_header.current", broken)
        with pytest.raises(RuntimeError, match="drifted"):
            wire.store.out(OWNER_SUBSPACE, TOPIC_KEY, _ack_dims(), nonce="n1")


# ── the helper the aura text names: nx ci ack <sha prefix> ────────────────────────────────────────────────

SHA_NEW = "bb" * 20
EP1, EP2 = "2026-11-20T10:00:00Z", "2026-11-20T14:00:00Z"


def _owner_row(sha: str, at: str, *, episode_at: str | None = None, kind: str = "owner", owner: str | None = ME,
               **body: Any) -> dict[str, Any]:
    fields = {"owner": owner, "episode_at": episode_at or at, **body}
    return {"id": f"{kind}-{at}".ljust(64, "0"), "subspace": OWNER_SUBSPACE, "keys": TOPIC_KEY, "created_at": at,
            "dims": {"kind": kind, "from": "engine", "sha": sha}, "body": json.dumps(fields)}


def _open_red(wire: _Wire) -> None:
    """Newest first: the open red's refresh row (a second tip of it), its first row, a clear, an older red."""
    wire.rows = [
        _owner_row("f" * 40, "2026-11-20T14:20:00Z", episode_at=EP2, refresh=True, suspects=["9" * 40]),
        _owner_row(SHA, "2026-11-20T14:10:00Z", episode_at=EP2, refresh=False),
        _owner_row("e" * 40, "2026-11-20T10:30:00Z", kind="clear"),
        _owner_row("d" * 40, "2026-11-20T10:00:00Z", owner="i9"),
    ]


def _ack(args: list[str], wire: _Wire, monkeypatch: pytest.MonkeyPatch) -> Any:
    """`nx ci ack ARGS` on the develop board, named with --base: no board is a default (the origin-derived board is
    tests/test_ci_ack_not_mine.py's)."""
    monkeypatch.setattr(ci_cmd, "_store", lambda: wire.store)
    named = "--base" in args or "--topic" in args
    return CliRunner().invoke(ci_cmd.ci_group, ["ack", *args] if named else ["ack", *args, "--base", "nexus-develop"])


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
    assert wire.sent[-1]["dims"]["sha"] == "f" * 40, "the sha of a refresh row is a tip of its red"


@pytest.mark.parametrize("arg", ["abc", "not-hex", "d" * 40, "d" * 7, "1234567", "9" * 7])
def test_nx_ci_ack_refuses_a_sha_that_is_no_tip_of_the_open_red(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch, arg: str) -> None:
    """Too short, not hex, the sha of a red a clear row already ended, one never a red, and a SUSPECT a row names
    (the engine counts a red tip only, and a suspect may be untested or green): nothing is posted."""
    _open_red(wire)
    result = _ack([arg], wire, monkeypatch)
    assert result.exit_code != 0, result.output
    assert wire.sent == []


def test_nx_ci_ack_acks_only_the_current_red_when_two_reds_have_no_clear_between(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """A new episode_at on a row that is neither a refresh nor a hand-on starts a new red (ack-times' own split), clear
    or no clear: the older red's sha is no longer one to acknowledge."""
    wire.rows = [
        _owner_row(SHA_NEW, "2026-11-20T14:10:00Z", episode_at=EP2),
        _owner_row(SHA, "2026-11-20T10:05:00Z", episode_at=EP1, refresh=True),
        _owner_row("a" * 40, "2026-11-20T10:00:00Z", episode_at=EP1),
    ]
    refused = _ack([SHA[:7]], wire, monkeypatch)
    assert refused.exit_code != 0 and wire.sent == [], refused.output
    assert _ack([SHA_NEW[:7]], wire, monkeypatch).exit_code == 0
    assert wire.sent[-1]["dims"]["sha"] == SHA_NEW


def test_nx_ci_ack_keeps_a_hand_off_rows_sha_in_the_open_red(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine stamps a hand-off with a NEW episode_at; reason=handoff is what ties it to the open red."""
    wire.rows = [
        _owner_row(SHA_NEW, "2026-11-20T14:40:00Z", episode_at="2026-11-20T14:40:00Z", reason="handoff"),
        _owner_row(SHA, "2026-11-20T14:10:00Z", episode_at=EP2, owner="i9"),
    ]
    assert _ack([SHA[:7]], wire, monkeypatch).exit_code == 0, "the first row's tip is still a tip of the red"


def test_nx_ci_ack_refuses_an_ambiguous_prefix_and_names_the_tips(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    wire.rows = [
        _owner_row("abcdef0" + "1" * 33, "2026-11-20T14:20:00Z", episode_at=EP2, refresh=True),
        _owner_row("abcdef0" + "2" * 33, "2026-11-20T14:10:00Z", episode_at=EP2),
    ]
    result = _ack(["abcdef0"], wire, monkeypatch)
    assert result.exit_code != 0 and "ambiguous" in result.output, result.output
    assert wire.sent == []
    assert _ack(["abcdef01"], wire, monkeypatch).exit_code == 0, "a longer prefix settles it"


def test_nx_ci_ack_says_so_when_no_red_is_open(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    wire.rows = [_owner_row("e" * 40, "2026-11-20T10:30:00Z", kind="clear")]
    result = _ack([SHA[:7]], wire, monkeypatch)
    assert result.exit_code != 0 and "no open red" in result.output, result.output
    assert wire.sent == []


def test_nx_ci_ack_refuses_a_caller_who_is_not_the_reds_owner_and_names_the_owner(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """An ack from next up, a stale seat or a delegated child posts cleanly and counts for nobody: the silent
    no-count the verb exists to remove."""
    wire.rows = [_owner_row(SHA, "2026-11-20T14:10:00Z", episode_at=EP2, owner="i9", owner_alias="tamsin", next=ME)]
    result = _ack([SHA[:7]], wire, monkeypatch)
    assert result.exit_code != 0 and wire.sent == []
    assert "owned by i9 (tamsin)" in result.output and f"an ack from {ME} counts for nobody" in result.output, result.output


def test_nx_ci_ack_refuses_a_red_that_has_no_owner(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    wire.rows = [_owner_row(SHA, "2026-11-20T14:10:00Z", episode_at=EP2, owner=None, reason="escalated")]
    result = _ack([SHA[:7]], wire, monkeypatch)
    assert result.exit_code != 0 and "no owner" in result.output and wire.sent == [], result.output


def test_nx_ci_ack_follows_the_latest_owner_row_after_a_hand_off(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    wire.rows = [
        _owner_row(SHA, "2026-11-20T14:40:00Z", episode_at="2026-11-20T14:40:00Z", reason="handoff", owner=ME),
        _owner_row(SHA, "2026-11-20T14:10:00Z", episode_at=EP2, owner="i9"),
    ]
    assert _ack([SHA[:7]], wire, monkeypatch).exit_code == 0, "handed to the caller: it owns the red now"


def test_nx_ci_ack_refuses_the_first_owner_once_the_red_was_handed_away(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    wire.rows = [
        _owner_row(SHA, "2026-11-20T14:40:00Z", episode_at="2026-11-20T14:40:00Z", reason="handoff", owner="i9"),
        _owner_row(SHA, "2026-11-20T14:10:00Z", episode_at=EP2, owner=ME),
    ]
    result = _ack([SHA[:7]], wire, monkeypatch)
    assert result.exit_code != 0 and "owned by i9" in result.output and wire.sent == [], result.output


def test_nx_ci_ack_refuses_without_an_identity_and_says_how_to_check(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    _open_red(wire)
    identity[:] = []
    result = _ack([SHA], wire, monkeypatch)
    assert result.exit_code != 0 and "nx identity show" in result.output, result.output
    assert wire.sent == []


def test_nx_ci_ack_takes_a_topic_named_whole(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    _open_red(wire)
    assert _ack([SHA, "--topic", "halcyon-main--owner"], wire, monkeypatch).exit_code == 0
    assert wire.sent[-1]["subspace"] == "board/ci/halcyon-main--owner"
    assert wire.sent[-1]["keys"] == {"topic": "halcyon-main--owner"}
    assert _ack([SHA, "--topic", "nexus-develop"], wire, monkeypatch).exit_code != 0, "a topic names its --owner post"


# ── what the code and the docs say about who counts an ack ─────────────────────────────────────────────────


def test_the_sites_that_describe_the_stamp_say_the_engine_counts_the_sender_session_not_from() -> None:
    """Review r1 (2026-11-20): the code and docs said the engine matches `from`. It does not: it maps the row's
    sender-session stamp through the session index. `from` is the label ack-times and people read."""
    from pathlib import Path  # noqa: PLC0415

    from nexus import ci_ack  # noqa: PLC0415

    root = Path(__file__).resolve().parent.parent
    assert "never reads" in (ci_ack.__doc__ or "") and "_out_raw" in (ci_ack.__doc__ or "")
    assert "sender-session" in (root / "src/nexus/procedures/ci_red_notice.lua").read_text(encoding="utf-8")
    for doc in ("docs/auras.md", "docs/cli-reference.md"):
        text = (root / doc).read_text(encoding="utf-8")
        assert "a name in `from` counts as no ack" not in text, doc
        assert "an ack carrying a name was counted as no ack" not in text, doc
        assert "the identity id the engine records from the token" not in text, doc
    # Each anchor sits in the one place that makes the claim, so removing it from that place fails here.
    auras = (root / "docs/auras.md").read_text(encoding="utf-8")
    assert "The engine never reads the row's `from`" in auras
    reference = (root / "docs/cli-reference.md").read_text(encoding="utf-8")
    ack_section = reference.split("### nx ci ack")[1].split("\n## ")[0]
    assert "The engine counts the row's sender-session stamp, never `from`" in ack_section
    times_section = reference.split("### nx group ack-times")[1].split("\n## ")[0]
    assert "sender-session stamp is mapped through the identity service's session index" in times_section
    assert "an `ack` whose reply is such a row" in reference.split("### nx tuple out")[1].split("### nx tuple rd")[0]
    assert "identity" in (mcp_core.tuple_out.__doc__ or "") and "--owner" in (mcp_core.tuple_out.__doc__ or "")
    assert "--owner" in (root / "docs/tuple-space.md").read_text(encoding="utf-8").split("`board/ci/<topic>` (RDR-220)")[1][:3000]


# ── round 2: a delegated child acks as its parent, and the board's state rows are the engine's alone ──────

CHILD = "i7"


def test_a_delegated_childs_ack_is_stamped_with_its_parents_id(wire: _Wire, identity: list[str]) -> None:
    """BoardReads.identities maps a delegated identity to its parent, so the ack counts for the parent: its `from`
    is the parent's id, not the child's."""
    identity[:] = [CHILD]
    VIEWS[CHILD] = {"identity": CHILD, "kind": "delegated", "parent": ME}
    wire.store.out(OWNER_SUBSPACE, TOPIC_KEY, _ack_dims(**{"from": CHILD}), nonce="n1")
    assert wire.sent[0]["dims"]["from"] == ME


def test_a_delegated_child_whose_parent_cannot_be_read_is_refused_not_stamped_with_its_own_id(wire: _Wire, identity: list[str]) -> None:
    identity[:] = [CHILD]
    VIEWS[CHILD] = {"identity": CHILD, "kind": "delegated"}
    with pytest.raises(ValueError, match="parent"):
        wire.store.out(OWNER_SUBSPACE, TOPIC_KEY, _ack_dims(), nonce="n1")
    assert wire.sent == []


def test_nx_ci_ack_accepts_a_delegated_child_of_the_owner_and_posts_as_the_parent(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    _open_red(wire)
    identity[:] = [CHILD]
    VIEWS[CHILD] = {"identity": CHILD, "kind": "delegated", "parent": ME}
    result = _ack([SHA[:7]], wire, monkeypatch)
    assert result.exit_code == 0, result.output
    assert wire.sent[0]["dims"] == {"kind": "ack", "from": ME, "sha": SHA}


def test_nx_ci_ack_refuses_a_delegated_child_of_someone_else(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    _open_red(wire)
    identity[:] = [CHILD]
    VIEWS[CHILD] = {"identity": CHILD, "kind": "delegated", "parent": "i9"}
    result = _ack([SHA[:7]], wire, monkeypatch)
    assert result.exit_code != 0 and "an ack from i9 counts for nobody" in result.output and wire.sent == [], result.output


def test_nx_ci_ack_ignores_owner_and_clear_rows_a_session_wrote(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine's isState: from=engine, a null sender stamp. A session can post kind=owner or kind=clear, and the
    engine ignores both; a stray clear must not read as 'no open red', nor a stray owner row as a new red."""
    _open_red(wire)
    session = "3d4bd7aa-1691-4bc1-b59b-0af9dccfd153"
    stray_clear = _owner_row("e" * 40, "2026-11-20T14:30:00Z", kind="clear")
    stray_owner = _owner_row("c" * 40, "2026-11-20T14:35:00Z", episode_at="2026-11-20T14:35:00Z", owner="i9")
    for stray in (stray_clear, stray_owner):
        stray["dims"]["from"] = "i9"
        stray["sender_session"] = session
    wire.rows = [stray_owner, stray_clear, *wire.rows]
    result = _ack([SHA[:7]], wire, monkeypatch)
    assert result.exit_code == 0, result.output
    assert wire.sent[-1]["dims"]["sha"] == SHA
    assert _ack(["c" * 7], wire, monkeypatch).exit_code != 0, "the stray owner row's sha is no tip"
