# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The eight RDR-205 Phase 2 Step 2 tuple-space MCP tools (bead
nexus-em75s.10), against the real engine substrate (``t2_service_env``).

Uses the ``mailbox/<address>`` template loaded at engine boot (keys
``[to]``, dims ``{from, kind, correlation_id, address_kind}``,
``take.enabled=true`` — see ``tests/db/test_http_tuple_store.py``'s module
docstring for the full template shapes).
"""
from __future__ import annotations

import uuid

from nexus.mcp.core import (
    tuple_ack,
    tuple_in,
    tuple_list,
    tuple_nack,
    tuple_out,
    tuple_rd,
    tuple_registry,
    tuple_stats,
)


def _uniq(label: str) -> str:
    return f"{label}-{uuid.uuid4().hex[:10]}"


class TestTupleOutRd:
    def test_out_then_rd_round_trips(self, t2_service_env) -> None:
        addr = _uniq("addr")
        msg = tuple_out(
            f"mailbox/{addr}", {"to": addr}, {"from": "sender-a"}, "hello",
            nonce=_uniq("nonce"),
        )
        assert "Wrote tuple" in msg
        assert f"mailbox/{addr}" in msg

        rows = tuple_rd(f"mailbox/{addr}", {"to": addr})
        assert isinstance(rows, list) and len(rows) == 1
        row = rows[0]
        assert isinstance(row, dict)
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
