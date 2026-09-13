# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-6konb.9 (MM-3.1): the SessionStart mailbox-watch arm instruction.

Unit-tests :mod:`nexus.mailbox_arm` directly, with no network and no
engine substrate -- every probe call is monkeypatched. The
session_start()-integration scenarios (four SessionStart sources, real
end-to-end absence-when-unavailable, and the timing measurement with the
engine genuinely absent) live in ``tests/test_hooks.py``.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from nexus.mailbox_arm import (
    ARM_MARKER,
    arm_block,
    known_instance_name,
    mailbox_arm_instruction,
    tuple_surface_available,
)


# ── mailbox_arm_instruction: pure text builder ──────────────────────────────


class TestMailboxArmInstructionText:
    def test_contains_the_exact_monitor_call_with_session_id_only(self) -> None:
        text = mailbox_arm_instruction("sess-abc", "")
        assert "Monitor({" in text
        assert 'command: "nx tuple watch sess-abc"' in text
        assert "persistent: true" in text
        assert "timeout_ms: 3600000" in text

    def test_contains_both_addresses_when_instance_known(self) -> None:
        text = mailbox_arm_instruction("sess-abc", "nexus-19")
        assert 'command: "nx tuple watch sess-abc nexus-19"' in text
        assert "sess-abc" in text
        assert "nexus-19" in text

    def test_degrades_to_session_id_only_when_instance_not_known(self) -> None:
        text = mailbox_arm_instruction("sess-abc", "")
        assert "nexus-19" not in text
        # exactly one address token in the command, not two
        assert 'command: "nx tuple watch sess-abc"' in text

    def test_states_armed_once(self) -> None:
        text = mailbox_arm_instruction("sess-abc", "")
        assert "ONCE" in text or "once" in text
        assert "twice" in text  # the redundant-arm-is-harmless sentence

    def test_states_ping_carries_no_body_and_watcher_never_claims(self) -> None:
        text = mailbox_arm_instruction("sess-abc", "")
        assert "never the message" in text
        assert "tuple_in" in text or "nx tuple in" in text
        assert "never claims" in text

    def test_states_redundant_arm_is_harmless(self) -> None:
        text = mailbox_arm_instruction("sess-abc", "")
        assert "harmless" in text

    def test_carries_the_arm_marker(self) -> None:
        assert ARM_MARKER in mailbox_arm_instruction("sess-abc", "")


# ── known_instance_name: the shared, unscoped registry ──────────────────────


class TestKnownInstanceName:
    def test_no_registry_file_degrades_to_not_known(self, tmp_path: Path) -> None:
        assert known_instance_name("sess-1", tmp_path) == ""

    def test_empty_registry_degrades_to_not_known(self, tmp_path: Path) -> None:
        reg = tmp_path / "tuple-watch" / "addresses"
        reg.parent.mkdir(parents=True)
        reg.write_text("", encoding="utf-8")
        assert known_instance_name("sess-1", tmp_path) == ""

    def test_singleton_candidate_is_known(self, tmp_path: Path) -> None:
        reg = tmp_path / "tuple-watch" / "addresses"
        reg.parent.mkdir(parents=True)
        reg.write_text("nexus-19\n", encoding="utf-8")
        assert known_instance_name("sess-1", tmp_path) == "nexus-19"

    def test_two_candidates_degrades_to_not_known(self, tmp_path: Path) -> None:
        reg = tmp_path / "tuple-watch" / "addresses"
        reg.parent.mkdir(parents=True)
        reg.write_text("nexus-19\nnexus-20\n", encoding="utf-8")
        assert known_instance_name("sess-1", tmp_path) == ""

    def test_registry_entry_equal_to_own_session_id_is_excluded(
        self, tmp_path: Path,
    ) -> None:
        """A sole entry that is just this session's own id is not a
        distinct instance name -- excluding it must not leave a bogus
        singleton behind."""
        reg = tmp_path / "tuple-watch" / "addresses"
        reg.parent.mkdir(parents=True)
        reg.write_text("sess-1\n", encoding="utf-8")
        assert known_instance_name("sess-1", tmp_path) == ""

    def test_comments_and_blank_lines_ignored(self, tmp_path: Path) -> None:
        reg = tmp_path / "tuple-watch" / "addresses"
        reg.parent.mkdir(parents=True)
        reg.write_text("# a comment\n\nnexus-19\n", encoding="utf-8")
        assert known_instance_name("sess-1", tmp_path) == "nexus-19"

    def test_unsafe_line_is_dropped(self, tmp_path: Path) -> None:
        reg = tmp_path / "tuple-watch" / "addresses"
        reg.parent.mkdir(parents=True)
        reg.write_text("../../etc/passwd\n", encoding="utf-8")
        assert known_instance_name("sess-1", tmp_path) == ""

    def test_unsafe_line_alongside_a_safe_one_leaves_singleton(
        self, tmp_path: Path,
    ) -> None:
        reg = tmp_path / "tuple-watch" / "addresses"
        reg.parent.mkdir(parents=True)
        reg.write_text("rm -rf /\nnexus-19\n", encoding="utf-8")
        assert known_instance_name("sess-1", tmp_path) == "nexus-19"


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


# ── arm_block: the orchestrator hooks.session_start() calls ────────────────


class TestArmBlock:
    def test_absent_when_no_session_id(self, tmp_path: Path) -> None:
        assert arm_block(None, config_dir=tmp_path) == ""
        assert arm_block("", config_dir=tmp_path) == ""
        assert arm_block("unknown", config_dir=tmp_path) == ""

    def test_absent_when_tuple_surface_unavailable(self, tmp_path: Path) -> None:
        with patch("nexus.mailbox_arm.tuple_surface_available", return_value=False):
            assert arm_block("sess-1", config_dir=tmp_path) == ""

    def test_present_with_session_id_only_when_instance_unknown(
        self, tmp_path: Path,
    ) -> None:
        with patch("nexus.mailbox_arm.tuple_surface_available", return_value=True):
            text = arm_block("sess-1", config_dir=tmp_path)
        assert ARM_MARKER in text
        assert 'command: "nx tuple watch sess-1"' in text

    def test_present_with_both_addresses_when_instance_known(
        self, tmp_path: Path,
    ) -> None:
        reg = tmp_path / "tuple-watch" / "addresses"
        reg.parent.mkdir(parents=True)
        reg.write_text("nexus-19\n", encoding="utf-8")
        with patch("nexus.mailbox_arm.tuple_surface_available", return_value=True):
            text = arm_block("sess-1", config_dir=tmp_path)
        assert 'command: "nx tuple watch sess-1 nexus-19"' in text
