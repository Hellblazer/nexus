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
import os
import subprocess
import sys
import uuid
from pathlib import Path

from datetime import datetime, timezone

import pytest
from click.testing import CliRunner

from nexus.commands.tuple_cmd import tuple_group
from nexus.db.t2.http_tuple_store import (
    ClaimNotFoundError,
    ClaimOwnershipError,
    HttpTupleStore,
    LeaseTooLongError,
    ParkCapExceededError,
    ReplyNotWrittenError,
    SchemaViolationError,
)
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

    def test_release_ends_the_claim_without_counting_an_attempt(self, t2_service_env) -> None:
        addr = _uniq("addr")
        _invoke([
            "out", f"mailbox/{addr}", "--key", f"to={addr}",
            "--dim", "from=sender-d", "--body", "hand-back", "--nonce", _uniq("nonce"),
        ])
        claimant = _uniq("claimant")
        inr = _invoke([
            "in", f"mailbox/{addr}", "--pattern", f"to={addr}",
            "--claimant", claimant, "--lease-s", "30", "--json",
        ])
        claim_id = _last_json_line(inr.output)["claim_id"]

        release = _invoke(["release", claim_id, "--claimant", claimant])
        assert release.exit_code == 0, release.output
        assert "Released" in release.output

        inr2 = _invoke([
            "in", f"mailbox/{addr}", "--pattern", f"to={addr}",
            "--claimant", _uniq("claimant2"), "--lease-s", "30", "--json",
        ])
        assert inr2.exit_code == 0
        payload2 = _last_json_line(inr2.output)
        assert payload2["tuple"]["body"] == "hand-back"
        assert payload2["tuple"]["attempts"] == 0

    def test_release_unknown_claim_exits_nonzero(self, t2_service_env) -> None:
        release = _invoke(["release", "0" * 64, "--claimant", "nobody"])
        assert release.exit_code == 1
        assert "Error" in release.output

    def test_release_by_wrong_claimant_exits_nonzero(self, t2_service_env) -> None:
        addr = _uniq("addr")
        _invoke([
            "out", f"mailbox/{addr}", "--key", f"to={addr}",
            "--dim", "from=sender-d", "--body", "mine", "--nonce", _uniq("nonce"),
        ])
        claimant = _uniq("claimant")
        inr = _invoke([
            "in", f"mailbox/{addr}", "--pattern", f"to={addr}",
            "--claimant", claimant, "--lease-s", "30", "--json",
        ])
        claim_id = _last_json_line(inr.output)["claim_id"]

        release = _invoke(["release", claim_id, "--claimant", _uniq("impostor")])
        assert release.exit_code == 1
        assert "ClaimOwnershipError" in release.output


class TestTupleReleaseUnitPassThrough:
    """Fast, no engine: proves the CLI is a thin pass-through to the
    store's own ``release``, same shape as ``TestTupleRenewUnitPassThrough``."""

    def test_cli_calls_the_store_with_exactly_the_given_arguments(self, monkeypatch) -> None:
        captured: dict[str, tuple] = {}

        def _fake_release(self, claim_id, claimant):
            captured["args"] = (claim_id, claimant)

        monkeypatch.setattr(HttpTupleStore, "release", _fake_release)
        res = _invoke(["release", "claim-1", "--claimant", "me"])
        assert res.exit_code == 0, res.output
        assert captured["args"] == ("claim-1", "me")
        assert res.output.strip() == "Released claim claim-1"

    def test_typed_errors_print_the_typed_message_not_a_traceback(self, monkeypatch) -> None:
        for exc_cls, code in (
            (ClaimNotFoundError, "ClaimNotFound"),
            (ClaimOwnershipError, "ClaimOwnership"),
        ):
            def _fake_release(self, claim_id, claimant, _exc=exc_cls, _code=code):
                raise _exc(_code)

            monkeypatch.setattr(HttpTupleStore, "release", _fake_release)
            res = _invoke(["release", "c", "--claimant", "m"])
            assert res.exit_code == 1
            assert exc_cls.__name__ in res.output
            assert "Traceback" not in res.output

    def test_missing_required_claimant_is_a_usage_error(self, t2_service_env) -> None:
        res = _invoke(["release", "x"])  # no --claimant
        assert res.exit_code != 0
        assert "claimant" in res.output.lower()


class TestTupleTemplatesListStats:
    def test_templates_lists_the_registered_templates(self, t2_service_env) -> None:
        out = _invoke(["templates", "--json"])
        assert out.exit_code == 0, out.output
        reg = _last_json_line(out.output)
        names = {t.get("name") for t in reg["templates"]}
        assert "mailbox/<address>" in names
        assert "ledger/<session_id>" in names
        assert "directory/<name>" in names

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

    def test_list_with_limit_pages_via_after(self, t2_service_env) -> None:
        prefix = f"mailbox/{_uniq('page')}-"
        addrs = [f"{prefix}{i}" for i in range(3)]
        for addr in addrs:
            _invoke([
                "out", addr, "--key", f"to={addr}",
                "--dim", "from=sender", "--nonce", _uniq("nonce"),
            ])

        page1 = _invoke(["list", "--prefix", prefix, "--limit", "2", "--json"])
        assert page1.exit_code == 0, page1.output
        rows1 = _last_json_line(page1.output)
        assert len(rows1) == 3  # 2 census rows + 1 {"next_cursor": ...} tail entry
        cursor = rows1[-1]["next_cursor"]
        assert cursor

        page2 = _invoke(["list", "--prefix", prefix, "--limit", "2", "--after", cursor, "--json"])
        assert page2.exit_code == 0, page2.output
        rows2 = _last_json_line(page2.output)
        assert len(rows2) == 1
        assert "next_cursor" not in rows2[-1]

    def test_list_with_no_limit_is_unpaged(self, t2_service_env) -> None:
        addr = _uniq("addr")
        _invoke([
            "out", f"mailbox/{addr}", "--key", f"to={addr}",
            "--dim", "from=sender", "--nonce", _uniq("nonce"),
        ])
        lst = _invoke(["list", "--prefix", f"mailbox/{addr}", "--json"])
        assert lst.exit_code == 0, lst.output
        rows = _last_json_line(lst.output)
        assert len(rows) == 1
        assert "next_cursor" not in rows[0]


class TestTupleDirectoryCmd:
    """RDR-208 Phase 2 Step 4 (bead nexus-galkv.11): ``nx tuple directory
    NAME`` -- shows who holds a name, reading ``directory/NAME`` exactly
    once and classifying that one list through the SAME pure classifier
    ``mailbox_send`` (nexus-galkv.10) uses, so the verb and the tool cannot
    disagree about whether a name is safely addressable (gate audit round B
    item 1, code review T2 ``nexus/rdr-208-phase2b-cre-2026-09-14``)."""

    def _arm(self, name: str, session_id: str) -> None:
        res = _invoke([
            "out", f"directory/{name}", "--key", f"name={name}",
            "--dim", f"session_id={session_id}", "--nonce", _uniq("nonce"),
        ])
        assert res.exit_code == 0, res.output

    def test_no_entry_reports_nothing_live(self, t2_service_env) -> None:
        name = _uniq("name")
        res = _invoke(["directory", name])
        assert res.exit_code == 0, res.output
        assert "No live directory entry" in res.output

    def test_one_holder_prints_the_entry_and_resolves(self, t2_service_env) -> None:
        name = _uniq("name")
        sid = str(uuid.uuid4())
        self._arm(name, sid)

        res = _invoke(["directory", name])
        assert res.exit_code == 0, res.output
        assert sid in res.output
        assert "resolves to session" in res.output

    def test_two_holders_are_flagged(self, t2_service_env) -> None:
        name = _uniq("name")
        sid1, sid2 = str(uuid.uuid4()), str(uuid.uuid4())
        self._arm(name, sid1)
        self._arm(name, sid2)

        res = _invoke(["directory", name])
        assert res.exit_code == 0, res.output
        assert sid1 in res.output
        assert sid2 in res.output
        assert "held by" in res.output
        assert "mailbox_send" in res.output

    def test_json_shape_single_holder(self, t2_service_env) -> None:
        name = _uniq("name")
        sid = str(uuid.uuid4())
        self._arm(name, sid)

        res = _invoke(["directory", name, "--json"])
        assert res.exit_code == 0, res.output
        payload = _last_json_line(res.output)
        assert payload["name"] == name
        assert payload["holders"] == [sid]
        assert payload["ambiguous"] is False
        assert payload["resolved_session_id"] == sid
        assert len(payload["entries"]) == 1
        entry = payload["entries"][0]
        assert entry["session_id"] == sid
        assert "created_at" in entry
        assert "expires_at" in entry

    def test_json_shape_ambiguous(self, t2_service_env) -> None:
        name = _uniq("name")
        sid1, sid2 = str(uuid.uuid4()), str(uuid.uuid4())
        self._arm(name, sid1)
        self._arm(name, sid2)

        res = _invoke(["directory", name, "--json"])
        assert res.exit_code == 0, res.output
        payload = _last_json_line(res.output)
        assert sorted(payload["holders"]) == sorted([sid1, sid2])
        assert payload["ambiguous"] is True
        assert payload["resolved_session_id"] is None
        assert len(payload["entries"]) == 2

    def test_reads_directory_exactly_once_and_stays_self_consistent(
        self, monkeypatch,
    ) -> None:
        """The bug this guards against: the verb used to read
        ``directory/NAME`` once for the printed list and a SECOND time
        inside the resolver's own classification -- a lapse or a re-nonce
        landing between those two reads would make the printed entries and
        the ambiguous/resolved verdict describe two different moments. A
        fake store whose second call would see a SECOND holder that never
        existed at the first read proves both that the verb reads exactly
        once (the assert on ``fake.calls``) and that its output is the
        SAME single read throughout (one entry, not ambiguous, resolved to
        the one real holder -- never a spurious two-holder verdict)."""
        import types

        import nexus.commands.tuple_cmd as tuple_cmd_mod

        name = _uniq("name")
        sid1, sid2 = str(uuid.uuid4()), str(uuid.uuid4())

        def _row(session_id: str, row_id: str) -> object:
            return types.SimpleNamespace(
                id=row_id, subspace=f"directory/{name}", template="directory/<name>",
                keys={"name": name}, dims={"session_id": session_id}, body=None,
                claim_state=None, claimant=None, lease_until=None, attempts=0,
                consumed_at=None, consumed_by=None,
                expires_at="2026-01-01T00:05:00Z", created_at="2026-01-01T00:00:00Z",
            )

        class _FakeStore:
            def __init__(self) -> None:
                self.calls = 0

            def rd(self, subspace, keys_pattern=None, *, n=1, since=None, timeout_s=0):
                self.calls += 1
                # A second call (the bug this test catches) would see a
                # world with a SECOND holder that just armed -- if the verb
                # ever reads twice, this makes the divergence visible.
                rows = [_row(sid1, "a")]
                if self.calls > 1:
                    rows.append(_row(sid2, "b"))
                return rows

        fake = _FakeStore()
        monkeypatch.setattr(tuple_cmd_mod, "_store", lambda: fake)

        res = _invoke(["directory", name, "--json"])
        assert res.exit_code == 0, res.output
        payload = _last_json_line(res.output)

        assert fake.calls == 1, (
            f"directory/{name} was read {fake.calls} times, expected exactly 1"
        )
        assert len(payload["entries"]) == 1
        assert payload["holders"] == [sid1]
        assert payload["ambiguous"] is False
        assert payload["resolved_session_id"] == sid1


class TestKvParsing:
    def test_out_rejects_malformed_key(self, t2_service_env) -> None:
        out = _invoke(["out", "mailbox/x", "--key", "no-equals-sign"])
        assert out.exit_code != 0


# ── nx tuple renew and nx tuple ack --reply-* (RDR-206 Phase 2, nexus-h61dl.10) ──
#
# HttpTupleStore.renew and ack(reply=) already landed (nexus-h61dl.8); these
# tests cover only the CLI's own wiring: flag parsing, pass-through to the
# client, and rendering the result. The client's own contract (the two
# ceilings, the transaction atomicity, the reply nonce) is pinned in
# tests/db/test_http_tuple_store.py and is not re-pinned here.


def _claimed_mailbox(claimant: str = "c1", lease_s: int = 60) -> tuple[str, str]:
    """Write and claim a mailbox request through the real store; returns
    (address, claim_id)."""
    addr = _uniq("addr")
    store = HttpTupleStore()
    store.out(f"mailbox/{addr}", {"to": addr}, {"from": "sender"}, "hi", nonce=_uniq("nonce"))
    _row, claim_id = store.in_(f"mailbox/{addr}", {"to": addr}, claimant=claimant, lease_s=lease_s)
    return addr, claim_id


class TestTupleRenewRoundTrip:
    def test_renew_prints_a_lease_until_later_than_the_original(self, t2_service_env) -> None:
        _addr, claim_id = _claimed_mailbox(lease_s=30)
        res = _invoke(["renew", "--claim-id", claim_id, "--claimant", "c1", "--lease-s", "300"])
        assert res.exit_code == 0, res.output
        printed = [ln for ln in res.output.splitlines() if ln.strip()][-1].strip()
        after = datetime.fromisoformat(printed)
        assert after.tzinfo is not None
        # the claim is still live and ackable afterwards -- a renew that had
        # somehow released or consumed it would fail this
        ack = _invoke(["ack", claim_id, "--claimant", "c1"])
        assert ack.exit_code == 0, ack.output

    def test_renew_of_an_unknown_claim_prints_claim_not_found_not_a_traceback(
        self, t2_service_env,
    ) -> None:
        res = _invoke(["renew", "--claim-id", uuid.uuid4().hex, "--claimant", "c1", "--lease-s", "60"])
        assert res.exit_code == 1
        assert "ClaimNotFoundError" in res.output
        assert "Traceback" not in res.output

    def test_renew_by_the_wrong_claimant_is_claim_ownership(self, t2_service_env) -> None:
        _addr, claim_id = _claimed_mailbox(claimant="c1")
        res = _invoke(["renew", "--claim-id", claim_id, "--claimant", "someone-else", "--lease-s", "60"])
        assert res.exit_code == 1
        assert "ClaimOwnershipError" in res.output

    def test_renew_above_the_template_cap_is_refused_not_capped(self, t2_service_env) -> None:
        _addr, claim_id = _claimed_mailbox()
        res = _invoke(["renew", "--claim-id", claim_id, "--claimant", "c1", "--lease-s", str(900 + 1)])
        assert res.exit_code == 1
        assert "LeaseTooLongError" in res.output

    def test_renew_missing_required_options_is_a_usage_error(self, t2_service_env) -> None:
        res = _invoke(["renew", "--claim-id", "x", "--lease-s", "60"])  # no --claimant
        assert res.exit_code != 0
        assert "claimant" in res.output.lower()


class TestTupleRenewUnitPassThrough:
    """Fast, no engine: proves the CLI prints exactly what the store returns
    and does not recompute a lease of its own -- the mutation this exists to
    catch is a CLI that prints ``now() + lease_s`` locally instead of the
    engine's (possibly clipped) answer."""

    def test_cli_prints_exactly_the_stores_return_value(self, monkeypatch) -> None:
        sentinel = datetime(2030, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        captured: dict[str, tuple] = {}

        def _fake_renew(self, claim_id, claimant, lease_s):
            captured["args"] = (claim_id, claimant, lease_s)
            return sentinel

        monkeypatch.setattr(HttpTupleStore, "renew", _fake_renew)
        res = _invoke(["renew", "--claim-id", "claim-1", "--claimant", "me", "--lease-s", "30"])
        assert res.exit_code == 0, res.output
        assert captured["args"] == ("claim-1", "me", 30)
        assert res.output.strip() == sentinel.isoformat()

    def test_typed_errors_print_the_typed_message_not_a_traceback(self, monkeypatch) -> None:
        for exc_cls, code in (
            (ClaimNotFoundError, "ClaimNotFound"),
            (ClaimOwnershipError, "ClaimOwnership"),
            (LeaseTooLongError, "LeaseTooLong"),
        ):
            def _fake_renew(self, claim_id, claimant, lease_s, _exc=exc_cls, _code=code):
                raise _exc(_code)

            monkeypatch.setattr(HttpTupleStore, "renew", _fake_renew)
            res = _invoke(["renew", "--claim-id", "c", "--claimant", "m", "--lease-s", "60"])
            assert res.exit_code == 1
            assert exc_cls.__name__ in res.output
            assert "Traceback" not in res.output


class TestTupleAckReplyUnitPassThrough:
    """Fast, no engine: the mutation this catches is a CLI that parses the
    --reply-* flags but never builds/forwards a ReplySpec, or forwards the
    wrong fields."""

    def test_full_reply_flag_set_builds_and_forwards_a_replyspec(self, monkeypatch) -> None:
        captured: dict[str, object] = {}

        def _fake_ack(self, claim_id, claimant, reply=None):
            captured["claim_id"] = claim_id
            captured["claimant"] = claimant
            captured["reply"] = reply
            return "ab" * 32

        monkeypatch.setattr(HttpTupleStore, "ack", _fake_ack)
        res = _invoke([
            "ack", "claim-1", "--claimant", "me",
            "--reply-subspace", "mailbox/x",
            "--reply-key", "to=x",
            "--reply-dim", "from=y",
            "--reply-body", "hello",
            "--reply-ttl-seconds", "60",
        ])
        assert res.exit_code == 0, res.output
        assert captured["claim_id"] == "claim-1"
        assert captured["claimant"] == "me"
        reply = captured["reply"]
        assert reply is not None
        assert reply.subspace == "mailbox/x"
        assert reply.keys == {"to": "x"}
        assert reply.dims == {"from": "y"}
        assert reply.body == "hello"
        assert reply.ttl_seconds == 60
        assert "ab" * 32 in res.output

    def test_no_reply_flags_passes_none_and_confirmation_is_unchanged(self, monkeypatch) -> None:
        captured: dict[str, object] = {}

        def _fake_ack(self, claim_id, claimant, reply=None):
            captured["reply"] = reply
            return None

        monkeypatch.setattr(HttpTupleStore, "ack", _fake_ack)
        res = _invoke(["ack", "claim-1", "--claimant", "me"])
        assert res.exit_code == 0, res.output
        assert captured["reply"] is None
        assert res.output.strip() == "Acked claim claim-1"

    def test_a_reply_flag_without_reply_subspace_is_a_usage_error(self, monkeypatch) -> None:
        called = []
        monkeypatch.setattr(HttpTupleStore, "ack", lambda *a, **kw: called.append(1))
        for argv in (
            ["ack", "claim-1", "--claimant", "me", "--reply-body", "hi"],
            ["ack", "claim-1", "--claimant", "me", "--reply-key", "to=x"],
            ["ack", "claim-1", "--claimant", "me", "--reply-dim", "from=y"],
            ["ack", "claim-1", "--claimant", "me", "--reply-ttl-seconds", "60"],
        ):
            res = _invoke(argv)
            assert res.exit_code != 0, res.output
            assert "--reply-subspace" in res.output
        assert called == [], "a usage error must never reach the store"

    def test_a_reply_key_without_equals_fails_like_out(self, monkeypatch) -> None:
        called = []
        monkeypatch.setattr(HttpTupleStore, "ack", lambda *a, **kw: called.append(1))
        res = _invoke([
            "ack", "claim-1", "--claimant", "me",
            "--reply-subspace", "mailbox/x", "--reply-key", "no-equals-sign",
        ])
        assert res.exit_code != 0
        assert called == []

    def test_reply_not_written_error_prints_a_clear_message_not_a_traceback(
        self, monkeypatch,
    ) -> None:
        def _fake_ack(self, claim_id, claimant, reply=None):
            raise ReplyNotWrittenError(
                "the engine acked without writing the reply: the request "
                "HAS BEEN CONSUMED and the reply was NOT written."
            )

        monkeypatch.setattr(HttpTupleStore, "ack", _fake_ack)
        res = _invoke([
            "ack", "claim-1", "--claimant", "me",
            "--reply-subspace", "mailbox/x", "--reply-key", "to=x",
        ])
        assert res.exit_code == 1
        assert "ReplyNotWrittenError" in res.output
        assert "HAS BEEN CONSUMED" in res.output
        assert "NOT written" in res.output
        assert "Traceback" not in res.output


class TestTupleAckReplyAgainstTheRealEngine:
    def test_ack_with_a_full_reply_flag_set_writes_the_reply_and_prints_its_id(
        self, t2_service_env,
    ) -> None:
        reply_addr = _uniq("replyto")
        _addr, claim_id = _claimed_mailbox()
        res = _invoke([
            "ack", claim_id, "--claimant", "c1",
            "--reply-subspace", f"mailbox/{reply_addr}",
            "--reply-key", f"to={reply_addr}",
            "--reply-dim", "from=worker",
            "--reply-body", "done",
        ])
        assert res.exit_code == 0, res.output
        assert "Acked claim" in res.output
        reply_line = [ln for ln in res.output.splitlines() if ln.startswith("reply_id=")]
        assert len(reply_line) == 1, res.output
        reply_id = reply_line[0].split("=", 1)[1]
        assert len(reply_id) == 64
        int(reply_id, 16)

        rows = HttpTupleStore().rd(f"mailbox/{reply_addr}", {"to": reply_addr}, n=2)  # n=2: n=1 would hide a duplicate
        assert [r.body for r in rows] == ["done"]
        assert rows[0].id == reply_id

    def test_ack_with_no_reply_flags_confirmation_line_is_unchanged(self, t2_service_env) -> None:
        _addr, claim_id = _claimed_mailbox()
        res = _invoke(["ack", claim_id, "--claimant", "c1"])
        assert res.exit_code == 0, res.output
        last_line = [ln for ln in res.output.splitlines() if ln.strip()][-1].strip()
        assert last_line == f"Acked claim {claim_id}"

    def test_ack_with_a_reply_to_a_keys_only_subspace_is_a_schema_violation(
        self, t2_service_env,
    ) -> None:
        session = _uniq("sess")
        store = HttpTupleStore()
        store.out(f"ledger/{session}", {"agent_id": "a1", "kind": "start"}, None, None)
        _addr, claim_id = _claimed_mailbox()

        res = _invoke([
            "ack", claim_id, "--claimant", "c1",
            "--reply-subspace", f"ledger/{session}",
            "--reply-key", "agent_id=a1",
            "--reply-key", "kind=done",
        ])
        assert res.exit_code == 1
        assert "SchemaViolationError" in res.output

        # the refusal happened before the transaction opened: the request is
        # still claimed and still ackable by the same claimant
        follow_up = _invoke(["ack", claim_id, "--claimant", "c1"])
        assert follow_up.exit_code == 0, follow_up.output


# ── mailbox delivery helpers ─────────────────────────────────────────────
#
# RDR-211 nexus-rplay.14 deleted the CLI ping-then-pull watcher these
# helpers used to drive; ``_watch_env``/``_out`` remain in reduced form for
# the mailbox drain hook's own tests below, which never depended on it.


def _watch_env(tmp_path):
    return HttpTupleStore(), tmp_path


def _out(store, addr, *, sender="sender-w", body="hi", kind="note"):
    return store.out(
        f"mailbox/{addr}", {"to": addr}, dims={"from": sender, "kind": kind},
        body=body, nonce=_uniq("nonce"),
    )


class TestMailboxDrainDoesNotStarveBehindALargeDeadBacklog:
    """nexus-1kvk3: the engine's ``rd`` orders by created_at ascending, never
    excludes claimed or dead-lettered rows, and caps a page at the hook's own
    PROBE_N (20, ``mailbox_drain.py``). A live row ranked behind more than
    that many dead-lettered rows must still reach this hook -- unlike the
    ``rd``-only watcher (the sibling starvation, nexus-qw386), this hook
    claims via ``/v1/tuples/in``, which is not windowed by any probe page."""

    HOOK = (
        Path(__file__).resolve().parent.parent
        / "conexus" / "hooks" / "scripts" / "mailbox_drain.py"
    )

    def _run_hook(self, addr: str, tmp_path) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["NEXUS_CONFIG_DIR"] = str(tmp_path / "hookcfg")
        env["XDG_STATE_HOME"] = str(tmp_path / "hookstate")
        return subprocess.run(
            [sys.executable, str(self.HOOK)],
            input=json.dumps({"session_id": addr, "hook_event_name": "UserPromptSubmit"}),
            capture_output=True, text=True, env=env, timeout=300,
        )

    def test_a_live_row_ranked_beyond_probe_n_dead_rows_is_still_drained(
        self, t2_service_env, tmp_path,
    ) -> None:
        store, _tmp = _watch_env(tmp_path)
        addr = _uniq("addr")
        sub = f"mailbox/{addr}"
        # PROBE_N is 20 in mailbox_drain.py; one more than that dead-lettered
        # ahead of the live row reproduces the starvation the bead names.
        for i in range(21):
            tid = _out(store, addr, sender="poison", body=f"dead-{i}")
            for _ in range(3):  # mailbox.yaml max_attempts=3: the third nack dead-letters it
                claimant = _uniq("c")
                claimed = store.in_(sub, {"to": addr}, claimant=claimant, lease_s=30)
                assert claimed is not None and claimed[0].id == tid
                store.nack(claimed[1], claimant)
        live_id = _out(store, addr, sender="alice", body="the live row ranked 22nd")

        res = self._run_hook(addr, tmp_path)

        assert res.returncode == 0, res.stderr
        assert "the live row ranked 22nd" in res.stdout, (
            "a live row ranked beyond the probe window was starved behind "
            "more than PROBE_N dead-lettered rows"
        )
        remaining = {r.id for r in store.rd(sub, {"to": addr}, n=50)}
        assert live_id not in remaining, (
            "the live row was never claimed, so it is still sitting in the mailbox"
        )
