# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx tuple`` CLI (RDR-205 Phase 2 Step 2, bead nexus-em75s.10), against
the real engine substrate (``t2_service_env``).

Uses the ``mailbox/<address>`` template loaded at engine boot -- see
``tests/db/test_http_tuple_store.py``'s module docstring for the full
template shapes.
"""
from __future__ import annotations

import json
import uuid

from click.testing import CliRunner

from nexus.commands.tuple_cmd import tuple_group


def _uniq(label: str) -> str:
    return f"{label}-{uuid.uuid4().hex[:10]}"


def _invoke(argv: list[str]):
    return CliRunner().invoke(tuple_group, argv)


def _last_json_line(output: str):
    """Parse the LAST non-empty line of *output* as JSON.

    A structlog warning line (e.g. the guard_production_write opt-in
    notice) can land on stdout ahead of the command's own JSON line;
    the JSON payload is always the command's final ``click.echo``.
    """
    lines = [line for line in output.splitlines() if line.strip()]
    return json.loads(lines[-1])


class TestTupleOutRd:
    def test_out_then_rd_round_trips(self, t2_service_env) -> None:
        addr = _uniq("addr")
        out = _invoke([
            "out", f"mailbox/{addr}",
            "--key", f"to={addr}", "--dim", "from=sender-a", "--body", "hello",
            "--nonce", _uniq("nonce"),
        ])
        assert out.exit_code == 0, out.output
        tuple_id = [line for line in out.output.splitlines() if line.strip()][-1].strip()
        assert len(tuple_id) == 64

        rd = _invoke(["rd", f"mailbox/{addr}", "--pattern", f"to={addr}", "--json"])
        assert rd.exit_code == 0, rd.output
        rows = _last_json_line(rd.output)
        assert len(rows) == 1
        assert rows[0]["id"] == tuple_id
        assert rows[0]["body"] == "hello"
        assert rows[0]["claim_state"] is None

    def test_rd_no_match_reports_none(self, t2_service_env) -> None:
        addr = _uniq("addr")
        rd = _invoke(["rd", f"mailbox/{addr}", "--pattern", f"to={addr}"])
        assert rd.exit_code == 0, rd.output
        assert "No matching tuples" in rd.output

    def test_rd_against_unknown_subspace_exits_nonzero(self, t2_service_env) -> None:
        rd = _invoke(["rd", "no-such-subspace/xyz"])
        assert rd.exit_code == 1
        assert "Error" in rd.output


class TestTupleInAckNack:
    def test_in_then_ack(self, t2_service_env) -> None:
        addr = _uniq("addr")
        _invoke([
            "out", f"mailbox/{addr}", "--key", f"to={addr}",
            "--dim", "from=sender-b", "--body", "payload", "--nonce", _uniq("nonce"),
        ])
        claimant = _uniq("claimant")
        inr = _invoke([
            "in", f"mailbox/{addr}", "--pattern", f"to={addr}",
            "--claimant", claimant, "--lease-s", "30", "--json",
        ])
        assert inr.exit_code == 0, inr.output
        payload = _last_json_line(inr.output)
        claim_id = payload["claim_id"]
        assert payload["tuple"]["body"] == "payload"

        ack = _invoke(["ack", claim_id, "--claimant", claimant])
        assert ack.exit_code == 0, ack.output
        assert "Acked" in ack.output

    def test_in_probe_miss_exits_nonzero(self, t2_service_env) -> None:
        addr = _uniq("addr")
        inr = _invoke([
            "in", f"mailbox/{addr}", "--pattern", f"to={addr}",
            "--claimant", _uniq("claimant"), "--lease-s", "30",
        ])
        assert inr.exit_code == 1
        assert "No matching tuple" in inr.output

    def test_nack_releases_for_a_retake(self, t2_service_env) -> None:
        addr = _uniq("addr")
        _invoke([
            "out", f"mailbox/{addr}", "--key", f"to={addr}",
            "--dim", "from=sender-c", "--body", "retry-me", "--nonce", _uniq("nonce"),
        ])
        claimant = _uniq("claimant")
        inr = _invoke([
            "in", f"mailbox/{addr}", "--pattern", f"to={addr}",
            "--claimant", claimant, "--lease-s", "30", "--json",
        ])
        claim_id = _last_json_line(inr.output)["claim_id"]

        nack = _invoke(["nack", claim_id, "--claimant", claimant])
        assert nack.exit_code == 0, nack.output
        assert "Nacked" in nack.output

        inr2 = _invoke([
            "in", f"mailbox/{addr}", "--pattern", f"to={addr}",
            "--claimant", _uniq("claimant2"), "--lease-s", "30", "--json",
        ])
        assert inr2.exit_code == 0
        assert _last_json_line(inr2.output)["tuple"]["body"] == "retry-me"

    def test_ack_unknown_claim_exits_nonzero(self, t2_service_env) -> None:
        ack = _invoke(["ack", "0" * 64, "--claimant", "nobody"])
        assert ack.exit_code == 1
        assert "Error" in ack.output


class TestTupleTemplatesListStats:
    def test_templates_lists_the_registered_templates(self, t2_service_env) -> None:
        out = _invoke(["templates", "--json"])
        assert out.exit_code == 0, out.output
        reg = _last_json_line(out.output)
        names = {t.get("name") for t in reg["templates"]}
        assert "mailbox/<address>" in names
        assert "ledger/<session_id>" in names

    def test_list_and_stats_reflect_a_written_tuple(self, t2_service_env) -> None:
        addr = _uniq("addr")
        _invoke([
            "out", f"mailbox/{addr}", "--key", f"to={addr}",
            "--dim", "from=sender-d", "--body", "x", "--nonce", _uniq("nonce"),
        ])

        lst = _invoke(["list", "--prefix", f"mailbox/{addr}", "--json"])
        assert lst.exit_code == 0, lst.output
        rows = _last_json_line(lst.output)
        assert len(rows) == 1
        assert rows[0]["available"] == 1

        stats = _invoke(["stats", f"mailbox/{addr}", "--json"])
        assert stats.exit_code == 0, stats.output
        census = _last_json_line(stats.output)
        assert census["total"] == 1
        assert census["available"] == 1

    def test_stats_on_never_written_subspace_is_zero(self, t2_service_env) -> None:
        addr = _uniq("addr")
        stats = _invoke(["stats", f"mailbox/{addr}", "--json"])
        assert stats.exit_code == 0
        census = _last_json_line(stats.output)
        assert census["total"] == 0
        assert census["available"] == 0


class TestKvParsing:
    def test_out_rejects_malformed_key(self, t2_service_env) -> None:
        out = _invoke(["out", "mailbox/x", "--key", "no-equals-sign"])
        assert out.exit_code != 0
