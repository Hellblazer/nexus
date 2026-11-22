# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Unit tests for `nexus.tuple_directory`'s sender side: `from` validation and the default sender (RDR-208, RDR-253
Phase 2). The send side (`resolve_send`) is pinned by `tests/identity/test_mail_addressing.py`."""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from nexus.session_marker import write_session_marker
from nexus.tuple_directory import (
    DirectoryResolutionError,
    check_from_address_is_own,
    from_address_not_own_message,
    resolve_default_from,
    resolve_send,
    validate_from_address,
)


class TestValidateFromAddress:
    def test_session_id_shape_is_accepted(self) -> None:
        sid = str(uuid.uuid4())
        assert validate_from_address(sid) == sid

    def test_agent_id_shape_is_accepted(self) -> None:
        agent_id = "a" + "0" * 16
        assert validate_from_address(agent_id) == agent_id

    def test_neither_shape_is_refused(self) -> None:
        with pytest.raises(DirectoryResolutionError, match="neither a session id, an identity nor an agent id"):
            validate_from_address("not-a-valid-shape")


class TestResolveDefaultFrom:
    """`resolve_default_from`'s own unit coverage (RDR-211 nexus-rplay.24):
    previously exercised only indirectly, through `mailbox_send`'s
    engine-substrate tests in `tests/test_mcp_tuple_tools.py`. Pins the
    reader through the REHOMED contract (`nexus.session_marker`) directly,
    with no engine and no process-table read: *state_dir*/*claude_pid* are
    both overridable for exactly this.
    """

    def test_resolves_through_the_rehomed_marker_reader(self, tmp_path) -> None:
        write_session_marker(tmp_path, 4242, "marker-session-id")
        assert resolve_default_from(state_dir=tmp_path, claude_pid=4242) == "marker-session-id"

    def test_falls_back_to_env_when_no_marker_present(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("NX_T1_SESSION_ID", "0b6f1d3e-2c4a-4e8b-9f7d-5a1c3e9b2d40")
        assert resolve_default_from(state_dir=tmp_path, claude_pid=4242) == "0b6f1d3e-2c4a-4e8b-9f7d-5a1c3e9b2d40"

    def test_marker_wins_over_a_stale_env_var(self, tmp_path, monkeypatch) -> None:
        write_session_marker(tmp_path, 4242, "marker-session-id")
        monkeypatch.setenv("NX_T1_SESSION_ID", "7d2e9a41-6b3c-4f0e-8a5d-1c9b4e7f2a63")
        assert resolve_default_from(state_dir=tmp_path, claude_pid=4242) == "marker-session-id"

    def test_refuses_when_neither_marker_nor_env_is_present(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        with pytest.raises(DirectoryResolutionError, match="no resolvable session id"):
            resolve_default_from(state_dir=tmp_path, claude_pid=4242)


class TestCheckFromAddressIsOwn:
    """F3 step 1 (nexus-2mv7y, JDR-002): a session-shaped `from_address`
    must be one of the calling server's own session ids. Agent ids pass,
    and with no own id at all (Claude Desktop, Cowork) nothing is compared."""

    MARKER = "1d4e8a2c-7b3f-4c9e-a6d1-2f8b5e0c9a71"
    T1 = "5a9c3e1f-2d7b-4e6a-8c0d-9b1f4a7e3c52"
    ENV = "9e2b6d4a-1c8f-4a3e-b7d0-6f5c2a9e1b84"
    OTHER = "3c7f1a9e-5b2d-4e8c-a1f6-0d9b7e3a5c28"

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch) -> None:
        monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)
        monkeypatch.delenv("NX_SESSION_ID", raising=False)

    def test_own_marker_id_passes_beside_a_different_t1_id(self, tmp_path, monkeypatch) -> None:
        # The env tier is set, so dropping the marker tier leaves an own set
        # without MARKER and the call is refused.
        write_session_marker(tmp_path, 4242, self.MARKER)
        monkeypatch.setenv("NX_T1_SESSION_ID", self.T1)
        assert check_from_address_is_own(self.MARKER, state_dir=tmp_path, claude_pid=4242) == self.MARKER

    def test_own_t1_session_id_passes_beside_a_marker(self, tmp_path, monkeypatch) -> None:
        write_session_marker(tmp_path, 4242, self.MARKER)
        monkeypatch.setenv("NX_T1_SESSION_ID", self.T1)
        assert check_from_address_is_own(self.T1, state_dir=tmp_path, claude_pid=4242) == self.T1

    def test_nx_session_id_is_own_when_t1_is_unset(self, tmp_path, monkeypatch) -> None:
        write_session_marker(tmp_path, 4242, self.MARKER)
        monkeypatch.setenv("NX_SESSION_ID", self.ENV)
        assert check_from_address_is_own(self.ENV, state_dir=tmp_path, claude_pid=4242) == self.ENV

    def test_nx_session_id_is_not_own_when_t1_is_set(self, tmp_path, monkeypatch) -> None:
        """A claude -p child gets its own NX_T1_SESSION_ID and its parent's id
        in NX_SESSION_ID; it must not send as the parent."""
        monkeypatch.setenv("NX_T1_SESSION_ID", self.T1)
        monkeypatch.setenv("NX_SESSION_ID", self.ENV)
        with pytest.raises(DirectoryResolutionError) as exc:
            check_from_address_is_own(self.ENV, state_dir=tmp_path, claude_pid=4242)
        assert str(exc.value) == from_address_not_own_message(self.ENV, [self.T1])

    def test_a_different_session_id_is_refused_with_the_pinned_text(self, tmp_path, monkeypatch) -> None:
        write_session_marker(tmp_path, 4242, self.MARKER)
        monkeypatch.setenv("NX_T1_SESSION_ID", self.T1)
        with pytest.raises(DirectoryResolutionError) as exc:
            check_from_address_is_own(self.OTHER, state_dir=tmp_path, claude_pid=4242)
        assert str(exc.value) == from_address_not_own_message(self.OTHER, [self.MARKER, self.T1])

    def test_the_refusal_text(self) -> None:
        assert from_address_not_own_message(self.OTHER, [self.MARKER]) == (
            f"from_address {self.OTHER!r} is a session id but not this server's own "
            f"({self.MARKER}): `from` names the sender, so a session id there must be the "
            "sending session's own. The usual slip is putting the recipient's id here; "
            "it belongs in `to`. Omit from_address to send as this session; a subagent "
            "passes its own agent id"
        )

    def test_own_id_in_another_spelling_passes_as_the_own_spelling(self, tmp_path) -> None:
        write_session_marker(tmp_path, 4242, self.MARKER)
        assert check_from_address_is_own(
            self.MARKER.upper(), state_dir=tmp_path, claude_pid=4242,
        ) == self.MARKER

    def test_an_agent_id_passes_regardless(self, tmp_path) -> None:
        write_session_marker(tmp_path, 4242, self.MARKER)
        agent = "a0123456789abcdef"
        assert check_from_address_is_own(agent, state_dir=tmp_path, claude_pid=4242) == agent

    def test_no_own_id_resolvable_lets_a_session_id_pass(self, tmp_path) -> None:
        assert check_from_address_is_own(self.OTHER, state_dir=tmp_path, claude_pid=4242) == self.OTHER

    def test_no_own_id_pass_through_is_canonical(self, tmp_path) -> None:
        assert check_from_address_is_own(
            self.OTHER.upper(), state_dir=tmp_path, claude_pid=4242,
        ) == self.OTHER

    def test_a_non_uuid_env_value_is_refused_not_treated_as_no_session(self, tmp_path, monkeypatch) -> None:
        """Fail closed: with no marker, an unusable env id must not read as
        the Desktop case, where any session id passes."""
        monkeypatch.setenv("NX_SESSION_ID", "not-a-uuid")
        with pytest.raises(DirectoryResolutionError, match="is not a UUID"):
            check_from_address_is_own(self.OTHER, state_dir=tmp_path, claude_pid=4242)

    def test_the_shape_check_still_runs_first(self, tmp_path) -> None:
        with pytest.raises(DirectoryResolutionError, match="neither a session id, an identity nor an agent id"):
            check_from_address_is_own("not-a-valid-shape", state_dir=tmp_path, claude_pid=4242)


class TestANameNoSeatCanHold:
    """nexus-3ceda.7: a `to` that no seat can be named is refused before any lookup. The probe battery found the
    engine's old error ("no template registered for subspace ...") sent agents to tools that do not exist on every
    model tried (8 of 9 runs)."""

    @pytest.mark.parametrize("to", ["Seminar seat: Bullock", "two words", "a/b", "-leading-dash", "ünïcode"])
    def test_refused_naming_the_rule_and_the_identity_route(self, to: str) -> None:
        def never(_name: str) -> dict[str, Any]:
            raise AssertionError("the alias route was asked for a name no seat can hold")

        with pytest.raises(DirectoryResolutionError) as exc_info:
            resolve_send(to, never)
        msg = str(exc_info.value)
        assert repr(to) in msg and "identity id" in msg and "letters, digits" in msg, msg
        assert "template" not in msg

    @pytest.mark.parametrize("to", ["tamsin", "nexus_3dc", "kimi.nexus-edb", "a1"])
    def test_a_name_a_seat_can_hold_asks_the_alias_route(self, to: str) -> None:
        asked: list[str] = []

        def alias(name: str) -> dict[str, Any]:
            asked.append(name)
            return {"identity": "i4"}

        assert resolve_send(to, alias).address == "i4" and asked == [to]

    def test_the_rule_matches_the_engines_address_segment_pattern(self) -> None:
        """The engine refuses a mailbox address outside its ADDRESS_SEGMENT_PATTERN; keep the two equal."""
        import re  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        from nexus.tuple_directory import SAFE_NAME_RE  # noqa: PLC0415

        java = (Path(__file__).resolve().parents[1] / "service/src/main/java/dev/nexus/service/tuples/TemplateRegistry.java").read_text()
        m = re.search(r'ADDRESS_SEGMENT_PATTERN = Pattern\.compile\("([^"]+)"\)', java)
        assert m, "the engine's pattern moved: update this pin"
        assert SAFE_NAME_RE.pattern == f"^{m.group(1)}$"
