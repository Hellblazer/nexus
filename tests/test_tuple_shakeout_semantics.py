# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Tuple-space semantics found by the 2026-10-21 cross-surface shakeout
(beads nexus-jz3kh F7, nexus-zihq1 F8, nexus-fzteh F9, nexus-5fpkh F10,
nexus-o9gt2 F11): the client and tool-description halves that need no engine.
The engine halves are pinned in ``TupleRepositoryTest`` / ``TupleHandlerWiringTest``;
the engine-backed MCP round trips are in ``test_mcp_tuple_tools.py``.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nexus.db.t2.http_tuple_store import (
    NonceReuseConflictError,
    SchemaViolationError,
    _body_to_census,
    _since_payload,
)
from nexus.db.t2.records import TupleRow
from nexus.mcp import channel
from nexus.mcp.core import _tuple_row_to_dict, mcp, tuple_rd

_TEMPLATES = Path(__file__).resolve().parent.parent / "service/src/main/resources/tuples/templates"


def _description(tool: str) -> str:
    """The wire description with line wrapping folded to single spaces."""
    return " ".join(mcp._tool_manager._tools[tool].description.split())  # noqa: SLF001


def _param_description(tool: str, param: str) -> str:
    return mcp._tool_manager._tools[tool].parameters["properties"][param]["description"]  # noqa: SLF001


def _row(claim_state: str | None, lease_until: str | None) -> TupleRow:
    return TupleRow(
        id="a" * 64, subspace="queue/q", template="queue/<name>", keys={"name": "q"}, dims={},
        body="work", claim_state=claim_state, claimant="old-holder" if claim_state else None,
        lease_until=lease_until, attempts=1, consumed_at=None, consumed_by=None,
        expires_at=None, created_at="2026-10-21T00:00:00Z",
    )


# ── F10: a lapsed lease reads as lapsed ──────────────────────────────────────


class TestLapsedClaimState:
    NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)

    def test_a_claim_whose_lease_has_passed_is_lapsed_and_keeps_its_stale_holder(self) -> None:
        past = (self.NOW - timedelta(seconds=5)).isoformat()
        row = _row("claimed", past)
        assert row.effective_claim_state(self.NOW) == "lapsed"
        assert row.claim_state == "claimed", "the stored value is untouched: the lazy reclaim reads it"
        assert (row.claimant, row.lease_until) == ("old-holder", past)

    def test_a_live_claim_stays_claimed(self) -> None:
        future = (self.NOW + timedelta(seconds=60)).isoformat()
        assert _row("claimed", future).effective_claim_state(self.NOW) == "claimed"

    @pytest.mark.parametrize("state", [None, "dead"])
    def test_other_states_are_never_relabelled(self, state: str | None) -> None:
        past = (self.NOW - timedelta(days=1)).isoformat()
        assert _row(state, past).effective_claim_state(self.NOW) == state

    @pytest.mark.parametrize("lease", [None, "", "not-a-time"])
    def test_a_claim_with_no_readable_lease_stays_claimed(self, lease: str | None) -> None:
        assert _row("claimed", lease).effective_claim_state(self.NOW) == "claimed"

    def test_a_zulu_timestamp_parses(self) -> None:
        assert _row("claimed", "2020-01-01T00:00:00Z").effective_claim_state(self.NOW) == "lapsed"

    def test_the_mcp_row_dict_reports_lapsed(self) -> None:
        d = _tuple_row_to_dict(_row("claimed", "2020-01-01T00:00:00Z"))
        assert d["claim_state"] == "lapsed"
        assert d["claimant"] == "old-holder"
        assert d["lease_until"] == "2020-01-01T00:00:00Z"

    def test_census_parses_lapsed_and_defaults_to_zero_from_an_older_engine(self) -> None:
        base = {"subspace": "s", "total": 3, "available": 1, "claimed": 2, "dead": 0, "consumed": 0}
        assert _body_to_census({**base, "lapsed": 1}).lapsed == 1
        assert _body_to_census(base).lapsed == 0


# ── F11: the read cursor is checked before it is sent ────────────────────────


class TestReadCursor:
    GOOD_ID = "ab" * 32

    @pytest.mark.parametrize("bad", ["0", "abc", "xyz", "ab" * 31, "ab" * 33])
    def test_a_made_up_since_id_is_refused_naming_the_field(self, bad: str) -> None:
        with pytest.raises(SchemaViolationError) as exc_info:
            _since_payload(("2026-10-21T00:00:00Z", bad))
        msg = str(exc_info.value)
        assert "since_id" in msg
        assert "64-hex" in msg
        assert "prior read" in msg
        assert "string length not even" not in msg

    def test_a_bad_created_at_is_refused_naming_the_field(self) -> None:
        with pytest.raises(SchemaViolationError, match="since_created_at.*prior read"):
            _since_payload(("yesterday", self.GOOD_ID))

    def test_a_real_cursor_and_the_subscriber_watermark_sentinel_pass(self) -> None:
        assert _since_payload(("2026-10-21T00:00:00.123456Z", self.GOOD_ID)) == {
            "created_at": "2026-10-21T00:00:00.123456Z", "id": self.GOOD_ID,
        }
        assert _since_payload(("2026-10-21T00:00:00+00:00", "")) == {"created_at": "2026-10-21T00:00:00+00:00"}

    @pytest.mark.parametrize(("created_at", "row_id"), [("2026-10-21T00:00:00Z", ""), ("", "ab" * 32)])
    def test_tuple_rd_refuses_half_a_cursor(self, created_at: str, row_id: str) -> None:
        out = tuple_rd("mailbox/x", since_created_at=created_at, since_id=row_id)
        assert len(out) == 1
        err = out[0]["error"]
        assert "since_created_at and since_id come as a pair" in err

    def test_the_tool_description_says_where_the_pair_comes_from(self) -> None:
        assert "of a prior read" in _param_description("tuple_rd", "since_created_at")
        assert "64 hex" in _param_description("tuple_rd", "since_id")
        assert "from a returned row" in _description("tuple_rd")
        assert "lapsed" in _description("tuple_rd")


# ── F9: the conflict maps to its own class ───────────────────────────────────


def test_nonce_reuse_conflict_has_its_own_client_class() -> None:
    assert NonceReuseConflictError.code == "NonceReuseConflict"
    assert issubclass(NonceReuseConflictError, Exception)
    assert "NonceReuseConflict" in _description("tuple_out")
    assert "fresh nonce" in _description("tuple_out")


# ── F7: the description names every keys+nonce template ─────────────────────


def _keys_nonce_templates() -> list[str]:
    names = []
    for path in sorted(_TEMPLATES.glob("*.yaml")):
        text = path.read_text()
        if re.search(r"^id_from:\s*keys\+nonce\s*$", text, re.M):
            names.append(re.search(r"^name:\s*(\S+)\s*$", text, re.M).group(1))
    return names


def test_tuple_out_nonce_description_names_every_keys_nonce_template_and_is_its_own_parameter() -> None:
    templates = _keys_nonce_templates()
    assert len(templates) >= 5, f"the census of keys+nonce templates found only {templates}; the glob is broken"
    desc = _param_description("tuple_out", "nonce")
    for name in templates:
        assert name in desc, f"tuple_out's nonce description omits the keys+nonce template {name}"
    assert "its own parameter" in desc
    assert "never a key or a dim" in desc
    assert "(the mailbox)" not in desc


# ── F8: tuple_subscribe states what the code does ────────────────────────────


def test_tuple_subscribe_description_states_the_real_start_position_and_batch_bound() -> None:
    desc = _description("tuple_subscribe")
    assert f"starts {int(channel.DEFAULT_BOARD_START_SKEW_S)} seconds before the subscribe call" in desc
    assert f"at most {channel.DEFAULT_BOARD_WAIT_ROWS} per wait" in desc
    assert "starts at now" not in desc
    assert "before the subscribe is delivered" in desc, "a mailbox delivers earlier mail; the doc must not say otherwise"
