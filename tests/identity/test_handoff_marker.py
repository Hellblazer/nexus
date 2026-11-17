# SPDX-License-Identifier: AGPL-3.0-or-later
"""The T1 handoff marker's identity extension (RDR-253 "Handover to the MCP server"): on ``clear``, ``resume`` and
``fork`` the marker also carries the identity, the generation, the session id and the token file's path, never the
token. Tenant-free."""
from __future__ import annotations

import json
from pathlib import Path

from nexus.daemon.t1_handoff import (
    handoff_marker_path, read_handoff_marker, write_handoff_marker, write_handoff_marker_if_absent,
)
from nexus.hooks import _identity_marker_fields
from nexus.hooks.identity_bind import IdentityResult
from nexus.identity.binder import BindOutcome


def test_the_marker_carries_identity_generation_session_id_and_the_token_file_path_never_the_token(tmp_path: Path) -> None:
    token_file = tmp_path / "identity" / "4242.4242000.token"
    write_handoff_marker(77, new_session_id="sid-2", claude_pid=4242, config_dir=tmp_path, identity="i3", generation=2,
                         token_file=str(token_file))
    marker = read_handoff_marker(77, tmp_path)
    assert (marker.new_session_id, marker.claude_pid, marker.identity, marker.generation, marker.token_file) == (
        "sid-2", 4242, "i3", 2, str(token_file))
    raw = handoff_marker_path(77, tmp_path).read_text()
    assert "token_file" in raw and set(json.loads(raw)) == {"new_session_id", "claude_pid", "written_at", "identity", "generation", "token_file"}


def test_a_marker_without_an_identity_reads_as_before(tmp_path: Path) -> None:
    write_handoff_marker(77, new_session_id="sid-2", claude_pid=4242, config_dir=tmp_path)
    marker = read_handoff_marker(77, tmp_path)
    assert (marker.identity, marker.generation, marker.token_file) == (None, None, None)
    assert "identity" not in json.loads(handoff_marker_path(77, tmp_path).read_text())


def test_a_reinstated_marker_keeps_the_identity_fields(tmp_path: Path) -> None:
    assert write_handoff_marker_if_absent(78, new_session_id="sid-3", claude_pid=4242, config_dir=tmp_path, identity="i4",
                                          generation=1, token_file="/x/y.token")
    assert read_handoff_marker(78, tmp_path).identity == "i4"


def test_the_hook_passes_the_fields_only_for_the_sources_that_hand_off(tmp_path: Path) -> None:
    outcome = BindOutcome("bound", "x", identity="i3", generation=2, event="resume", token_file=tmp_path / "t.token")
    result = IdentityResult(outcome=outcome)
    expected = {"identity": "i3", "generation": 2, "token_file": str(tmp_path / "t.token")}
    assert _identity_marker_fields(result, "resume") == expected
    assert _identity_marker_fields(result, "clear") == expected and _identity_marker_fields(result, "fork") == expected
    assert _identity_marker_fields(result, "startup") == {} and _identity_marker_fields(result, "compact") == {}
    assert _identity_marker_fields(None, "clear") == {}
    assert _identity_marker_fields(IdentityResult(outcome=BindOutcome("unbound", "x")), "clear") == {}
