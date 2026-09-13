# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-6konb.9 (MM-3.1): the SessionStart mailbox-watch arm instruction.

Unit-tests :mod:`nexus.mailbox_arm` directly, with no network and no
engine substrate -- every probe call is monkeypatched. The
session_start()-integration scenarios (four SessionStart sources, real
end-to-end absence-when-unavailable, and the timing measurement with the
engine genuinely absent) live in ``tests/test_hooks.py``.

Defect fix (nexus-6konb.9.1): the instance name exists only in the
model's own knowledge, never in this module's environment or any file it
could read, so ``mailbox_arm_instruction`` no longer takes an *instance*
parameter and this module no longer reads any registry to guess one --
see ``TestNoRegistryGuessing`` below, and the module docstring's
"The instance-name mailbox" section.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import nexus.mailbox_arm as mailbox_arm_module
from nexus.mailbox_arm import (
    ARM_MARKER,
    arm_block,
    mailbox_arm_instruction,
    tuple_surface_available,
)


# ── mailbox_arm_instruction: pure text builder ──────────────────────────────


class TestMailboxArmInstructionText:
    def test_contains_the_monitor_call_shape(self) -> None:
        text = mailbox_arm_instruction("sess-abc")
        assert "Monitor({" in text
        assert "persistent: true" in text
        assert "timeout_ms: 3600000" in text
        assert '"nx tuple watch --instance' in text

    def test_never_uses_a_bare_positional_address(self) -> None:
        """The old design embedded the session id (and instance, when
        guessed) as a bare positional in the command. That form suppresses
        `nx tuple watch`'s own session-id default outright, so it must
        never appear -- the rendered command is always `--instance NAME`
        or no arguments at all."""
        text = mailbox_arm_instruction("sess-abc")
        assert 'command: "nx tuple watch sess-abc"' not in text
        assert 'command: "nx tuple watch sess-abc nexus-19"' not in text
        assert '"nx tuple watch --instance' in text

    def test_states_omit_instance_when_name_not_known(self) -> None:
        text = mailbox_arm_instruction("sess-abc")
        assert "omit --instance" in text

    def test_states_armed_once(self) -> None:
        text = mailbox_arm_instruction("sess-abc")
        assert "ONCE" in text or "once" in text
        assert "twice" in text  # the redundant-arm-is-harmless sentence

    def test_states_ping_carries_no_body_and_watcher_never_claims(self) -> None:
        text = mailbox_arm_instruction("sess-abc")
        assert "never the message" in text
        assert "never claims" in text

    def test_states_redundant_arm_is_harmless(self) -> None:
        text = mailbox_arm_instruction("sess-abc")
        assert "harmless" in text

    def test_carries_the_arm_marker(self) -> None:
        assert ARM_MARKER in mailbox_arm_instruction("sess-abc")

    def test_names_full_mcp_tool_names_for_the_drain(self) -> None:
        """nexus-6konb.9 defect fix: the drain instruction must name the
        full MCP tools, including the ack (and nack), not just a bare
        `nx tuple in` CLI fragment with no mention of ack at all -- an
        unacked claim lapses and is eventually dead-lettered."""
        text = mailbox_arm_instruction("sess-abc")
        assert "mcp__plugin_conexus_nexus__tuple_in" in text
        assert "mcp__plugin_conexus_nexus__tuple_ack" in text
        assert "mcp__plugin_conexus_nexus__tuple_nack" in text

    def test_states_reply_argument_for_a_request(self) -> None:
        text = mailbox_arm_instruction("sess-abc")
        assert "reply" in text

    def test_states_unacked_claims_lapse_and_dead_letter(self) -> None:
        text = mailbox_arm_instruction("sess-abc")
        assert "lapses" in text
        assert "dead-lettered" in text


# ── arm_block: the orchestrator hooks.session_start() calls ────────────────


class TestArmBlock:
    def test_absent_when_no_session_id(self, tmp_path: Path) -> None:
        assert arm_block(None, config_dir=tmp_path) == ""
        assert arm_block("", config_dir=tmp_path) == ""
        assert arm_block("unknown", config_dir=tmp_path) == ""

    def test_absent_when_tuple_surface_unavailable(self, tmp_path: Path) -> None:
        with patch("nexus.mailbox_arm.tuple_surface_available", return_value=False):
            assert arm_block("sess-1", config_dir=tmp_path) == ""

    def test_present_when_available(self, tmp_path: Path) -> None:
        with patch("nexus.mailbox_arm.tuple_surface_available", return_value=True):
            text = arm_block("sess-1", config_dir=tmp_path)
        assert ARM_MARKER in text
        assert "sess-1" in text
        assert '"nx tuple watch --instance' in text


class TestNoRegistryGuessing:
    """nexus-6konb.9 defect fix: the module must not read ANY file to guess
    an instance name -- the old machine-wide `<config>/tuple-watch/addresses`
    registry is never consulted, present or absent, junk or clean."""

    def test_a_populated_machine_wide_registry_does_not_change_the_instruction(
        self, tmp_path: Path,
    ) -> None:
        registry = tmp_path / "tuple-watch" / "addresses"
        registry.parent.mkdir(parents=True)
        registry.write_text("nexus-registered-instance\n", encoding="utf-8")
        with patch("nexus.mailbox_arm.tuple_surface_available", return_value=True):
            text = arm_block("sess-1", config_dir=tmp_path)
        assert "nexus-registered-instance" not in text

    def test_module_exposes_no_registry_reader(self) -> None:
        assert not hasattr(mailbox_arm_module, "known_instance_name")
        assert not hasattr(mailbox_arm_module, "_read_registry")


# ── tuple_surface_available: bounded probe + cache ──────────────────────────


class TestTupleSurfaceAvailable:
    def test_probes_when_no_cache(self, tmp_path: Path) -> None:
        with patch("nexus.mailbox_arm._probe_tuple_surface", return_value=True) as m:
            assert tuple_surface_available(tmp_path, now=100.0) is True
        m.assert_called_once()

    def test_cache_hit_within_ttl_does_not_reprobe(self, tmp_path: Path) -> None:
        with patch("nexus.mailbox_arm._probe_tuple_surface", return_value=True) as m:
            tuple_surface_available(tmp_path, now=100.0, cache_ttl_s=60.0)
            result = tuple_surface_available(tmp_path, now=110.0, cache_ttl_s=60.0)
        assert result is True
        m.assert_called_once()

    def test_cache_expiry_reprobes(self, tmp_path: Path) -> None:
        with patch(
            "nexus.mailbox_arm._probe_tuple_surface", side_effect=[True, False],
        ) as m:
            first = tuple_surface_available(tmp_path, now=100.0, cache_ttl_s=60.0)
            second = tuple_surface_available(tmp_path, now=200.0, cache_ttl_s=60.0)
        assert first is True
        assert second is False
        assert m.call_count == 2

    def test_negative_probe_is_cached_too(self, tmp_path: Path) -> None:
        """A cached "unavailable" must not be silently retried every call --
        that would defeat the point of caching for a genuinely down engine."""
        with patch("nexus.mailbox_arm._probe_tuple_surface", return_value=False) as m:
            tuple_surface_available(tmp_path, now=100.0, cache_ttl_s=60.0)
            tuple_surface_available(tmp_path, now=101.0, cache_ttl_s=60.0)
        m.assert_called_once()

    def test_corrupt_cache_file_reprobes_rather_than_raising(
        self, tmp_path: Path,
    ) -> None:
        cache = tmp_path / "tuple-watch" / "arm-probe-cache.json"
        cache.parent.mkdir(parents=True)
        cache.write_text("not json", encoding="utf-8")
        with patch("nexus.mailbox_arm._probe_tuple_surface", return_value=True):
            assert tuple_surface_available(tmp_path, now=100.0) is True

    def test_probe_bounded_timeout_is_forwarded(self, tmp_path: Path) -> None:
        with patch("nexus.mailbox_arm._probe_tuple_surface", return_value=True) as m:
            tuple_surface_available(tmp_path, now=100.0, probe_timeout_s=0.5)
        m.assert_called_once_with(0.5)
