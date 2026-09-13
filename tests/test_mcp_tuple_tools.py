# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The nine RDR-205/RDR-206 tuple-space MCP tools (beads nexus-em75s.10,
nexus-h61dl.9), against the real engine substrate (``t2_service_env``).

Uses the ``mailbox/<address>`` template loaded at engine boot (keys
``[to]``, dims ``{from, kind, correlation_id, address_kind}``,
``take.enabled=true`` — see ``tests/db/test_http_tuple_store.py``'s module
docstring for the full template shapes).
"""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest

from nexus.db.t2.http_tuple_store import HttpTupleStore, ReplyNotWrittenError

from nexus.mcp.core import (
    tuple_ack,
    tuple_in,
    tuple_list,
    tuple_nack,
    tuple_out,
    tuple_rd,
    tuple_registry,
    tuple_renew,
    tuple_stats,
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
        reply_rows = tuple_rd(f"mailbox/{reply_addr}", {"to": reply_addr})
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
