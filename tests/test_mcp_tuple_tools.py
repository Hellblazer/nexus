# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The thirteen RDR-205/RDR-206/RDR-211 tuple-space MCP tools (beads
nexus-em75s.10, nexus-h61dl.9, nexus-rplay.9, nexus-rplay.11), plus
mailbox_send (RDR-208 Phase 2 Step 2, bead nexus-galkv.10), against the
real engine substrate (``t2_service_env``).

Uses the ``mailbox/<address>`` template loaded at engine boot (keys
``[to]``, dims ``{from, kind, correlation_id, address_kind}``,
``take.enabled=true``) and the ``directory/<name>`` template (keys
``[name]``, dims ``{session_id}``) — see ``tests/db/test_http_tuple_store.py``'s
module docstring for the full template shapes.

``TestTupleSubscriptions`` (nexus-rplay.11) is the first class in this
file to touch T1, and its own ``_isolated_t1_session`` fixture resets both
``nexus.mcp_infra``'s process-lifetime T1 singleton and
``nexus.mcp.subscriptions``'s process-lifetime cache before AND after each
test — ``mcp_infra.get_t1()`` caches across tests in one worker process,
so without the reset a later test's fresh ``NX_T1_SESSION_ID`` (minted by
the suite-wide autouse ``_isolate_t1_sessions`` fixture) would never take
effect, and any instance-mailbox lease thread a test starts would outlive
it.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime

import pytest

from nexus.db.t2.http_tuple_store import HttpTupleStore, ReplyNotWrittenError

from nexus.mcp.core import (
    mailbox_send,
    tuple_ack,
    tuple_in,
    tuple_list,
    tuple_nack,
    tuple_out,
    tuple_rd,
    tuple_registry,
    tuple_release,
    tuple_renew,
    tuple_stats,
    tuple_subscribe,
    tuple_subscriptions,
    tuple_unsubscribe,
)


def _uniq(label: str) -> str:
    return f"{label}-{uuid.uuid4().hex[:10]}"


class TestTupleOutRd:
    def test_out_then_rd_round_trips(self, t2_service_env) -> None:
        addr = _uniq("addr")
        tuple_id = tuple_out(
            f"mailbox/{addr}", {"to": addr}, {"from": "sender-a"}, "hello",
            nonce=_uniq("nonce"),
        )
        # nexus-em75s.12 review fix: the docstring promises "the tuple id,
        # lowercase hex" -- the return value IS the id, not a sentence.
        assert isinstance(tuple_id, str)
        assert "Wrote" not in tuple_id
        assert tuple_id == tuple_id.lower()
        assert all(c in "0123456789abcdef" for c in tuple_id)

        rows = tuple_rd(f"mailbox/{addr}", {"to": addr})
        assert isinstance(rows, list) and len(rows) == 1
        row = rows[0]
        assert isinstance(row, dict)
        assert row["id"] == tuple_id
        assert row["subspace"] == f"mailbox/{addr}"
        assert row["keys"] == {"to": addr}
        assert row["body"] == "hello"
        assert row["claim_state"] is None
        assert "error" not in row

    def test_rd_against_unknown_subspace_returns_error_row(self, t2_service_env) -> None:
        rows = tuple_rd("no-such-subspace/xyz", {})
        assert isinstance(rows, list) and len(rows) == 1
        assert "error" in rows[0]


class TestTupleInAckNack:
    def test_in_then_ack_removes_the_row(self, t2_service_env) -> None:
        addr = _uniq("addr")
        tuple_out(f"mailbox/{addr}", {"to": addr}, {"from": "sender-b"}, "payload", nonce=_uniq("nonce"))

        claimant = _uniq("claimant")
        claim = tuple_in(
            f"mailbox/{addr}", {"to": addr}, claimant=claimant, lease_s=30,
        )
        assert isinstance(claim, dict)
        assert "error" not in claim
        assert claim["tuple"]["body"] == "payload"
        claim_id = claim["claim_id"]
        assert claim_id

        msg = tuple_ack(claim_id, claimant)
        assert "Acked" in msg

        # Consumed -- gone from a fresh rd/in against the same subspace.
        rows = tuple_rd(f"mailbox/{addr}", {"to": addr})
        assert rows == []

    def test_in_probe_miss_returns_none(self, t2_service_env) -> None:
        addr = _uniq("addr")
        result = tuple_in(
            f"mailbox/{addr}", {"to": addr}, claimant=_uniq("claimant"), lease_s=30,
        )
        assert result is None

    def test_nack_releases_the_claim_for_a_retake(self, t2_service_env) -> None:
        addr = _uniq("addr")
        tuple_out(f"mailbox/{addr}", {"to": addr}, {"from": "sender-c"}, "retry-me", nonce=_uniq("nonce"))
        claimant = _uniq("claimant")
        claim = tuple_in(f"mailbox/{addr}", {"to": addr}, claimant=claimant, lease_s=30)
        assert claim is not None
        msg = tuple_nack(claim["claim_id"], claimant)
        assert "Nacked" in msg

        # released back to available -- a second claimant can take it.
        claim2 = tuple_in(
            f"mailbox/{addr}", {"to": addr}, claimant=_uniq("claimant2"), lease_s=30,
        )
        assert claim2 is not None
        assert claim2["tuple"]["body"] == "retry-me"

    def test_ack_unknown_claim_returns_error(self, t2_service_env) -> None:
        msg = tuple_ack("0" * 64, "nobody")
        assert "Error" in msg

    def test_ack_without_reply_args_is_unchanged(self, t2_service_env) -> None:
        """RDR-206: the default (no ``reply``) plain ack must
        keep behaving exactly as before the reply argument existed."""
        addr = _uniq("addr")
        tuple_out(f"mailbox/{addr}", {"to": addr}, {"from": "sender-plain"}, "plain", nonce=_uniq("nonce"))
        claimant = _uniq("claimant")
        claim = tuple_in(f"mailbox/{addr}", {"to": addr}, claimant=claimant, lease_s=30)
        assert claim is not None
        msg = tuple_ack(claim["claim_id"], claimant)
        assert msg == f"Acked claim {claim['claim_id']}"
        assert "reply" not in msg.lower()

    def test_ack_with_reply_writes_reply_in_one_transaction(self, t2_service_env) -> None:
        """RDR-206: ``tuple_ack(reply=...)`` consumes the request
        and writes the reply together, and the tool reports the reply id."""
        req_addr = _uniq("req")
        reply_addr = _uniq("reply")
        tuple_out(
            f"mailbox/{req_addr}", {"to": req_addr}, {"from": "requester"},
            "please answer", nonce=_uniq("nonce"),
        )
        claimant = _uniq("responder")
        claim = tuple_in(f"mailbox/{req_addr}", {"to": req_addr}, claimant=claimant, lease_s=30)
        assert claim is not None

        msg = tuple_ack(
            claim["claim_id"], claimant,
            reply={
                "subspace": f"mailbox/{reply_addr}",
                "keys": {"to": reply_addr},
                "dims": {"from": claimant},
                "body": "the answer",
            },
        )
        assert "Acked" in msg
        assert "with reply" in msg

        # The request is gone (consumed) ...
        assert tuple_rd(f"mailbox/{req_addr}", {"to": req_addr}) == []
        # ... and the reply landed in the SAME call, visible on a fresh read.
        reply_rows = tuple_rd(f"mailbox/{reply_addr}", {"to": reply_addr}, n=2)  # n=2: n=1 would hide a duplicate
        assert len(reply_rows) == 1
        assert reply_rows[0]["body"] == "the answer"

    def test_ack_reply_to_keys_only_subspace_leaves_request_claimed(self, t2_service_env) -> None:
        """RDR-206: a reply target that resolves to a ``keys``-only
        template (the ledger) is a ``SchemaViolation`` raised BEFORE the
        ack's transaction opens, so the request is neither consumed nor
        answered, and the same claimant can still ack it plainly."""
        addr = _uniq("addr")
        tuple_out(f"mailbox/{addr}", {"to": addr}, {"from": "sender-e"}, "still-here", nonce=_uniq("nonce"))
        claimant = _uniq("claimant")
        claim = tuple_in(f"mailbox/{addr}", {"to": addr}, claimant=claimant, lease_s=30)
        assert claim is not None

        session_id = _uniq("session")
        msg = tuple_ack(
            claim["claim_id"], claimant,
            reply={"subspace": f"ledger/{session_id}", "keys": {"agent_id": "a", "kind": "start"}},
        )
        assert "Error" in msg

        # Request still claimed by the same claimant -- a plain ack
        # succeeds afterward, proving nothing was consumed by the refusal.
        plain_msg = tuple_ack(claim["claim_id"], claimant)
        assert "Acked" in plain_msg

    @pytest.mark.parametrize(
        ("reply", "refusal"),
        [
            ({"subspace": "mailbox/x", "keys": {"to": "x"}, "nonce": "mine"}, "must not carry a nonce"),
            ({"subspace": "mailbox/x", "keys": {"to": "x"}, "bdy": "typo"}, "unknown field(s) ['bdy']"),
            ({"keys": {"to": "x"}, "body": "no target"}, "reply.subspace is required"),
            ({"subspace": "mailbox/x", "body": "no keys"}, "reply.keys is required"),
        ],
        ids=["nonce", "unknown-key", "no-subspace", "no-keys"],
    )
    def test_malformed_reply_is_refused_and_leaves_request_claimed(
        self, t2_service_env, reply, refusal,
    ) -> None:
        """RDR-206: a malformed ``reply`` object is refused, never dropped.
        A misspelt field or a missing target that were silently ignored
        would ack the request as a plain ack and lose the answer; a
        caller-supplied nonce would let the caller believe it chose the
        reply's identity. Each refusal happens before the ack is sent, so
        the same claimant can still ack the request afterwards."""
        addr = _uniq("addr")
        tuple_out(f"mailbox/{addr}", {"to": addr}, {"from": "sender-m"}, "keep-me", nonce=_uniq("nonce"))
        claimant = _uniq("claimant")
        claim = tuple_in(f"mailbox/{addr}", {"to": addr}, claimant=claimant, lease_s=30)
        assert claim is not None

        msg = tuple_ack(claim["claim_id"], claimant, reply=reply)
        assert "Error" in msg
        assert refusal in msg

        plain_msg = tuple_ack(claim["claim_id"], claimant)
        assert plain_msg == f"Acked claim {claim['claim_id']}"

    def test_ack_reply_not_written_surfaces_as_tool_error_not_raised(
        self, t2_service_env, monkeypatch,
    ) -> None:
        """RDR-206 (bead nexus-h61dl.9 residual): ``ReplyNotWrittenError``
        is deliberately NOT a ``TupleError`` (by design, so a caller's
        ``except TupleError`` does not swallow it). The MCP tool's own
        broad ``except Exception`` must still turn it into
        ``_mcp_tool_error`` text -- never let it raise through the wire --
        and the text must say the request was consumed.
        """
        def _fake_ack(self, claim_id, claimant, reply=None):
            raise ReplyNotWrittenError(
                "the engine acked without writing the reply: the request "
                f"claimed by {claimant!r} HAS BEEN CONSUMED and the reply "
                "was NOT written."
            )

        monkeypatch.setattr(HttpTupleStore, "ack", _fake_ack)

        msg = tuple_ack(
            "some-claim-id", "some-claimant",
            reply={"subspace": "mailbox/whoever", "keys": {"to": "whoever"}},
        )
        assert "HAS BEEN CONSUMED" in msg
        assert "Error" in msg


class TestTupleRenew:
    def test_renew_extends_the_lease(self, t2_service_env) -> None:
        addr = _uniq("addr")
        tuple_out(f"mailbox/{addr}", {"to": addr}, {"from": "sender-f"}, "renew-me", nonce=_uniq("nonce"))
        claimant = _uniq("claimant")
        claim = tuple_in(f"mailbox/{addr}", {"to": addr}, claimant=claimant, lease_s=5)
        assert claim is not None
        original_lease_until = claim["tuple"]["lease_until"]

        result = tuple_renew(claim["claim_id"], claimant, 300)
        assert isinstance(result, dict)
        assert "error" not in result
        assert "lease_until" in result
        # Must be the engine's own timestamp -- parseable, and it moved
        # forward from the original 5 s lease.
        new_lease_until = datetime.fromisoformat(result["lease_until"])
        assert new_lease_until > datetime.fromisoformat(original_lease_until)

        # The claim is still held and ackable afterward -- a renew is not
        # a delivery given back.
        msg = tuple_ack(claim["claim_id"], claimant)
        assert "Acked" in msg

    def test_renew_on_unknown_claim_returns_error_dict(self, t2_service_env) -> None:
        result = tuple_renew("0" * 64, "nobody", 30)
        assert isinstance(result, dict)
        assert "error" in result
        assert "Error" in result["error"]

    def test_renew_by_a_different_claimant_returns_error_dict(self, t2_service_env) -> None:
        addr = _uniq("addr")
        tuple_out(f"mailbox/{addr}", {"to": addr}, {"from": "sender-g"}, "mine", nonce=_uniq("nonce"))
        claimant = _uniq("claimant")
        claim = tuple_in(f"mailbox/{addr}", {"to": addr}, claimant=claimant, lease_s=30)
        assert claim is not None

        result = tuple_renew(claim["claim_id"], _uniq("impostor"), 60)
        assert "error" in result


class TestTupleRelease:
    def test_release_ends_the_claim_without_counting_an_attempt(self, t2_service_env) -> None:
        addr = _uniq("addr")
        tuple_out(f"mailbox/{addr}", {"to": addr}, {"from": "sender-h"}, "hand-back", nonce=_uniq("nonce"))
        claimant = _uniq("claimant")
        claim = tuple_in(f"mailbox/{addr}", {"to": addr}, claimant=claimant, lease_s=30)
        assert claim is not None
        assert claim["tuple"]["attempts"] == 0

        msg = tuple_release(claim["claim_id"], claimant)
        assert "Released" in msg

        # released back to available with attempts unchanged -- a second
        # claimant can take it, and it is not a dead-letter retry.
        claim2 = tuple_in(f"mailbox/{addr}", {"to": addr}, claimant=_uniq("claimant2"), lease_s=30)
        assert claim2 is not None
        assert claim2["tuple"]["body"] == "hand-back"
        assert claim2["tuple"]["attempts"] == 0

    def test_release_on_unknown_claim_returns_error_string(self, t2_service_env) -> None:
        msg = tuple_release("0" * 64, "nobody")
        assert "Error" in msg

    def test_release_by_a_different_claimant_returns_error_string(self, t2_service_env) -> None:
        addr = _uniq("addr")
        tuple_out(f"mailbox/{addr}", {"to": addr}, {"from": "sender-i"}, "mine", nonce=_uniq("nonce"))
        claimant = _uniq("claimant")
        claim = tuple_in(f"mailbox/{addr}", {"to": addr}, claimant=claimant, lease_s=30)
        assert claim is not None

        msg = tuple_release(claim["claim_id"], _uniq("impostor"))
        assert "Error" in msg


class TestTupleRegistryListStats:
    def test_registry_reports_digest_and_templates(self, t2_service_env) -> None:
        reg = tuple_registry()
        assert isinstance(reg, dict)
        assert "error" not in reg
        assert "digest" in reg
        assert "templates" in reg
        names = {t.get("name") for t in reg["templates"]}
        assert "mailbox/<address>" in names
        assert "ledger/<session_id>" in names
        assert "directory/<name>" in names

    def test_list_and_stats_reflect_a_written_tuple(self, t2_service_env) -> None:
        addr = _uniq("addr")
        tuple_out(f"mailbox/{addr}", {"to": addr}, {"from": "sender-d"}, "x", nonce=_uniq("nonce"))

        subspaces = tuple_list(f"mailbox/{addr}")
        assert isinstance(subspaces, list) and len(subspaces) == 1
        assert subspaces[0]["subspace"] == f"mailbox/{addr}"
        assert subspaces[0]["available"] == 1

        stats = tuple_stats(f"mailbox/{addr}")
        assert isinstance(stats, dict)
        assert "error" not in stats
        assert stats["total"] == 1
        assert stats["available"] == 1

    def test_stats_on_never_written_subspace_is_all_zero(self, t2_service_env) -> None:
        addr = _uniq("addr")
        stats = tuple_stats(f"mailbox/{addr}")
        assert stats["total"] == 0
        assert stats["available"] == 0

    def test_list_truncated_page_carries_a_pagination_footer_entry(self, t2_service_env) -> None:
        """nexus-xapt8: a truncated page appends a `_pagination` entry with
        `next_cursor` -- the catalog tools' own convention for a
        `list[dict]`-shaped paged result."""
        prefix = f"mailbox/{_uniq('page')}-"
        addrs = [f"{prefix}{i}" for i in range(3)]
        for addr in addrs:
            tuple_out(addr, {"to": addr}, {"from": "sender"}, "x", nonce=_uniq("nonce"))

        page1 = tuple_list(prefix, limit=2)
        assert len(page1) == 3  # 2 census rows + 1 pagination footer
        assert "_pagination" in page1[-1]
        cursor = page1[-1]["_pagination"]["next_cursor"]
        assert cursor

        page2 = tuple_list(prefix, limit=2, after=cursor)
        assert len(page2) == 1  # the remaining subspace, no footer (not truncated)
        assert "_pagination" not in page2[-1]


class TestMailboxSend:
    """RDR-208 Phase 2 Step 2 (bead nexus-galkv.10): send-time `to`
    resolution and the default `from`. Every test sets NX_T1_SESSION_ID so
    the `from` fallback resolves without touching a real tuple-watch
    session marker on this box; the marker-vs-env tests below override
    that with an explicit marker file instead."""

    def _directory_entry(self, name: str, session_id: str, *, ttl_seconds: int | None = None) -> None:
        tuple_out(
            f"directory/{name}", {"name": name}, {"session_id": session_id},
            nonce=_uniq("nonce"), ttl_seconds=ttl_seconds,
        )

    def test_session_id_delivers_directly_with_address_kind_session(
        self, t2_service_env, monkeypatch,
    ) -> None:
        monkeypatch.setenv("NX_T1_SESSION_ID", str(uuid.uuid4()))
        dest = str(uuid.uuid4())
        result = mailbox_send(dest, body="hello", kind="ping", correlation_id="corr-1")
        assert isinstance(result, dict) and "error" not in result
        assert result["to"] == dest
        assert result["address_kind"] == "session"

        rows = tuple_rd(f"mailbox/{dest}", {"to": dest})
        assert len(rows) == 1
        assert rows[0]["body"] == "hello"
        assert rows[0]["dims"]["address_kind"] == "session"
        assert rows[0]["dims"]["kind"] == "ping"
        assert rows[0]["dims"]["correlation_id"] == "corr-1"
        assert rows[0]["dims"]["from"] == result["from"]

    def test_agent_id_delivers_directly_with_address_kind_agent(
        self, t2_service_env, monkeypatch,
    ) -> None:
        monkeypatch.setenv("NX_T1_SESSION_ID", str(uuid.uuid4()))
        dest = "a" + uuid.uuid4().hex[:16]
        result = mailbox_send(dest, body="hello")
        assert "error" not in result
        assert result["to"] == dest
        assert result["address_kind"] == "agent"

        rows = tuple_rd(f"mailbox/{dest}", {"to": dest})
        assert len(rows) == 1
        assert rows[0]["dims"]["address_kind"] == "agent"

    def test_name_with_one_live_holder_delivers_to_that_session(
        self, t2_service_env, monkeypatch,
    ) -> None:
        monkeypatch.setenv("NX_T1_SESSION_ID", str(uuid.uuid4()))
        name = _uniq("name")
        sid = str(uuid.uuid4())
        self._directory_entry(name, sid)

        result = mailbox_send(name, body="hey")
        assert "error" not in result
        assert result["to"] == sid
        assert result["address_kind"] == "session"

        rows = tuple_rd(f"mailbox/{sid}", {"to": sid})
        assert len(rows) == 1 and rows[0]["body"] == "hey"

    def test_unresolvable_name_errors_naming_the_name_and_writes_nothing(
        self, t2_service_env, monkeypatch,
    ) -> None:
        monkeypatch.setenv("NX_T1_SESSION_ID", str(uuid.uuid4()))
        name = _uniq("noholder")

        result = mailbox_send(name, body="x")
        assert "error" in result
        assert name in result["error"]

        # A defect that fell through and used the unresolved name literally
        # as the mailbox address would show up here as a live row.
        stats = tuple_stats(f"mailbox/{name}")
        assert stats["total"] == 0

    def test_two_live_holders_errors_listing_both_and_writes_nothing(
        self, t2_service_env, monkeypatch,
    ) -> None:
        monkeypatch.setenv("NX_T1_SESSION_ID", str(uuid.uuid4()))
        name = _uniq("name")
        sid1, sid2 = str(uuid.uuid4()), str(uuid.uuid4())
        self._directory_entry(name, sid1)
        self._directory_entry(name, sid2)

        result = mailbox_send(name, body="x")
        assert "error" in result
        assert sid1 in result["error"]
        assert sid2 in result["error"]

        assert tuple_rd(f"mailbox/{sid1}", {"to": sid1}) == []
        assert tuple_rd(f"mailbox/{sid2}", {"to": sid2}) == []

    def test_one_session_two_live_rows_is_one_holder_not_a_conflict(
        self, t2_service_env, monkeypatch,
    ) -> None:
        monkeypatch.setenv("NX_T1_SESSION_ID", str(uuid.uuid4()))
        name = _uniq("name")
        sid = str(uuid.uuid4())
        self._directory_entry(name, sid)  # the original watcher row
        self._directory_entry(name, sid)  # a re-armed watcher's new nonce

        result = mailbox_send(name, body="x")
        assert "error" not in result
        assert result["to"] == sid

    def test_lapsed_entry_is_refused(self, t2_service_env, monkeypatch) -> None:
        monkeypatch.setenv("NX_T1_SESSION_ID", str(uuid.uuid4()))
        name = _uniq("name")
        sid = str(uuid.uuid4())
        self._directory_entry(name, sid, ttl_seconds=1)
        time.sleep(1.5)

        result = mailbox_send(name, body="x")
        assert "error" in result
        assert name in result["error"]

    def test_significant_a_lapse_refused_then_session_send_then_rearm_resolves_again(
        self, t2_service_env, monkeypatch,
    ) -> None:
        """Gate Significant (a): a directory entry lapses while its session
        stays live -- the name refuses, mail BY SESSION ID still arrives,
        and a fresh entry with a new nonce (what a re-armed watcher writes)
        makes the name resolve again."""
        monkeypatch.setenv("NX_T1_SESSION_ID", str(uuid.uuid4()))
        name = _uniq("name")
        sid = str(uuid.uuid4())
        self._directory_entry(name, sid, ttl_seconds=1)
        time.sleep(1.5)

        refused = mailbox_send(name, body="x")
        assert "error" in refused and name in refused["error"]

        by_session = mailbox_send(sid, body="still-reachable")
        assert "error" not in by_session
        assert by_session["to"] == sid
        rows = tuple_rd(f"mailbox/{sid}", {"to": sid}, n=5)
        assert any(r["body"] == "still-reachable" for r in rows)

        self._directory_entry(name, sid)  # re-armed watcher, fresh nonce
        resolved_again = mailbox_send(name, body="resolved-again")
        assert "error" not in resolved_again
        assert resolved_again["to"] == sid

    def test_from_defaults_to_the_moved_session_marker_not_a_stale_env_var(
        self, t2_service_env, tmp_path, monkeypatch,
    ) -> None:
        """RDR-208 audit round-2 fix: the default `from` is the tuple-watch
        session marker for this MCP server's claude ancestor, not
        NX_T1_SESSION_ID -- the env var lags a `/clear` handoff while the
        marker is written synchronously by SessionStart. Moving only the
        env var, not the marker, would pass either way; this test moves
        the marker and leaves the env var stale, so only the fix (reading
        the marker first) can pass it."""
        import nexus.session as session_mod
        from nexus.session_marker import write_session_marker

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(session_mod, "find_immediate_claude_pid", lambda: 4242)
        moved_id = str(uuid.uuid4())
        write_session_marker(tmp_path, 4242, moved_id)
        monkeypatch.setenv("NX_T1_SESSION_ID", str(uuid.uuid4()))  # stale -- must not be used

        dest = str(uuid.uuid4())
        result = mailbox_send(dest, body="from-check")
        assert "error" not in result
        assert result["from"] == moved_id

        rows = tuple_rd(f"mailbox/{dest}", {"to": dest})
        assert rows[0]["dims"]["from"] == moved_id

    def test_from_falls_back_to_env_when_no_marker_present(
        self, t2_service_env, tmp_path, monkeypatch,
    ) -> None:
        import nexus.session as session_mod

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(session_mod, "find_immediate_claude_pid", lambda: 4242)
        env_id = str(uuid.uuid4())
        monkeypatch.setenv("NX_T1_SESSION_ID", env_id)

        dest = str(uuid.uuid4())
        result = mailbox_send(dest, body="x")
        assert "error" not in result
        assert result["from"] == env_id

    def test_from_refused_when_neither_marker_nor_env_present_and_nothing_written(
        self, t2_service_env, tmp_path, monkeypatch,
    ) -> None:
        import nexus.session as session_mod

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(session_mod, "find_immediate_claude_pid", lambda: 4242)
        monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)

        dest = str(uuid.uuid4())
        result = mailbox_send(dest, body="x")
        assert "error" in result

        assert tuple_rd(f"mailbox/{dest}", {"to": dest}) == []

    def test_a_failed_directory_read_errors_and_writes_nothing(
        self, t2_service_env, monkeypatch,
    ) -> None:
        monkeypatch.setenv("NX_T1_SESSION_ID", str(uuid.uuid4()))

        def _boom(self, *a, **kw):
            raise RuntimeError("directory read exploded")

        monkeypatch.setattr(HttpTupleStore, "rd", _boom)
        name = _uniq("name")

        result = mailbox_send(name, body="x")
        assert "error" in result
        assert "directory read exploded" in result["error"]

    def test_agent_id_from_address_stamps_from_with_that_agent_id(
        self, t2_service_env, monkeypatch,
    ) -> None:
        """from_address test validation gap 2 (T2 nexus_rdr/208-p2-test-
        validation-galkv16-2026-09-14): NX_T1_SESSION_ID is set so a bug
        that silently ignored from_address and fell back to the default
        would still produce SOME `from`, not a refusal -- the assertion on
        `result["from"]` is what catches the ignored override."""
        monkeypatch.setenv("NX_T1_SESSION_ID", str(uuid.uuid4()))
        dest = str(uuid.uuid4())
        agent_id = "a" + uuid.uuid4().hex[:16]

        result = mailbox_send(dest, body="x", from_address=agent_id)
        assert "error" not in result
        assert result["from"] == agent_id

        rows = tuple_rd(f"mailbox/{dest}", {"to": dest})
        assert len(rows) == 1
        assert rows[0]["dims"]["from"] == agent_id

    def test_session_id_from_address_overrides_the_session_marker(
        self, t2_service_env, tmp_path, monkeypatch,
    ) -> None:
        """A live, correctly-read session marker is armed here too, so this
        proves from_address WINS over it -- not merely that from_address
        works when nothing else is set."""
        import nexus.session as session_mod
        from nexus.session_marker import write_session_marker

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(session_mod, "find_immediate_claude_pid", lambda: 4242)
        marker_id = str(uuid.uuid4())
        write_session_marker(tmp_path, 4242, marker_id)

        override_id = str(uuid.uuid4())
        dest = str(uuid.uuid4())
        result = mailbox_send(dest, body="x", from_address=override_id)
        assert "error" not in result
        assert result["from"] == override_id
        assert result["from"] != marker_id

        rows = tuple_rd(f"mailbox/{dest}", {"to": dest})
        assert rows[0]["dims"]["from"] == override_id

    def test_invalid_from_address_shape_is_refused_and_writes_nothing(
        self, t2_service_env, monkeypatch,
    ) -> None:
        monkeypatch.setenv("NX_T1_SESSION_ID", str(uuid.uuid4()))
        dest = str(uuid.uuid4())

        result = mailbox_send(dest, body="x", from_address="not-a-valid-shape")
        assert "error" in result

        stats = tuple_stats(f"mailbox/{dest}")
        assert stats["total"] == 0


class TestTupleSubscriptions:
    """RDR-211 Phase 1 Step 3 (bead nexus-rplay.11): ``tuple_subscribe``,
    ``tuple_unsubscribe``, ``tuple_subscriptions`` against the real engine
    and a real T1 handle. See this module's own docstring for why the T1
    singleton and the subscriptions process cache are reset around every
    test in this class."""

    @pytest.fixture(autouse=True)
    def _isolated_t1_session(self):
        from nexus import mcp_infra
        from nexus.mcp import subscriptions as subs_mod

        mcp_infra.reset_t1_for_release()
        subs_mod.reset_cache()
        yield
        subs_mod.reset_cache()
        mcp_infra.reset_t1_for_release()

    def test_queue_is_refused_naming_in_and_the_list_is_unchanged(self, t2_service_env) -> None:
        msg = tuple_subscribe("queue/builds")
        assert "Error" in msg
        assert "`in`" in msg
        entries = tuple_subscriptions()
        assert len(entries) == 1  # only the session mailbox
        assert "error" not in entries[0]

    def test_lock_is_refused_naming_in(self, t2_service_env) -> None:
        msg = tuple_subscribe("lock/release-train")
        assert "Error" in msg
        assert "`in`" in msg

    def test_thirty_second_board_topic_accepted_thirty_third_refused(self, t2_service_env) -> None:
        for i in range(32):
            msg = tuple_subscribe(f"board/topic-{i}")
            assert "Error" not in msg
        entries = tuple_subscriptions()
        assert len(entries) == 33  # the session mailbox + 32 topics

        msg = tuple_subscribe("board/topic-33rd")
        assert "Error" in msg
        entries = tuple_subscriptions()
        assert len(entries) == 33  # unchanged by the refusal

    def test_subscribe_and_unsubscribe_a_board_topic(self, t2_service_env) -> None:
        msg = tuple_subscribe("board/release-notes")
        assert "Subscribed" in msg

        entries = tuple_subscriptions()
        subspaces = {e["subspace"] for e in entries}
        assert "board/release-notes" in subspaces
        assert all("cursor" in e for e in entries)

        msg = tuple_unsubscribe("board/release-notes")
        assert "Unsubscribed" in msg
        entries = tuple_subscriptions()
        assert "board/release-notes" not in {e["subspace"] for e in entries}

    def test_instance_mailbox_takeover_writes_registration_and_lease_refused_for_any_other_name(
        self, t2_service_env, tmp_path, monkeypatch,
    ) -> None:
        import os

        from nexus.mcp.subscriptions import registration_path

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        session_id = os.environ["NX_T1_SESSION_ID"]
        name = f"inst-{uuid.uuid4().hex[:8]}"

        msg = tuple_subscribe(f"mailbox/{name}")
        assert "Subscribed" in msg

        reg = registration_path(tmp_path, session_id)
        assert reg.read_text(encoding="utf-8") == f"{name}\n"

        rows = tuple_rd(f"directory/{name}", {"name": name})
        assert len(rows) == 1
        assert rows[0]["dims"]["session_id"] == session_id

        other = f"other-{uuid.uuid4().hex[:8]}"
        refusal = tuple_subscribe(f"mailbox/{other}")
        assert "Error" in refusal

        msg = tuple_unsubscribe(f"mailbox/{name}")
        assert "Unsubscribed" in msg
        entries = tuple_subscriptions()
        assert f"mailbox/{name}" not in {e["subspace"] for e in entries}

    def test_subscriptions_lists_entries_with_cursors(self, t2_service_env) -> None:
        entries = tuple_subscriptions()
        assert isinstance(entries, list)
        assert len(entries) == 1
        assert "cursor" in entries[0]
        assert entries[0]["cursor"] is None

    def test_a_change_fires_the_observer(self, t2_service_env) -> None:
        import os

        from nexus.mcp import subscriptions as subs_mod
        from nexus.mcp_infra import get_t1, t2_ctx

        session_id = os.environ["NX_T1_SESSION_ID"]
        t1, _ = get_t1()
        subs = subs_mod.get_or_load(t1, session_id, store_factory=t2_ctx)
        seen: list[int] = []
        subs.add_listener(lambda s: seen.append(s.version))

        # tuple_subscribe's own get_or_load resolves the SAME cached
        # object (keyed by this session id), so its mutation fires the
        # listener registered directly above -- the unit-level stand-in
        # for the not-yet-built waiter re-issuing its parked `wait`.
        msg = tuple_subscribe("board/observed-topic")
        assert "Subscribed" in msg
        assert seen == [1]
