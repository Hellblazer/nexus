# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the per-operator dispatch router.

The router is pure-logic + env-introspection; no mocking required
beyond ``monkeypatch`` for env vars. Tests cover:

  * Default mode (no env): always Claude
  * NEXUS_DISPATCH_BACKEND=qwen / claude / auto
  * Per-operator pins (NEXUS_DISPATCH_{QWEN,CLAUDE}_OPERATORS) win in
    every mode
  * operator_X / X equivalence (the optional ``operator_`` prefix)
  * Bundle-level conservative routing: any-claude → all-claude
"""
from __future__ import annotations

import pytest

from nexus.operators.dispatch_router import (
    CLAUDE_OPERATORS_PINNED,
    QWEN_OPERATORS_DEFAULT,
    pick_dispatcher,
    pick_dispatcher_for_bundle,
)


# ── Fixture: clean env per test ───────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "NEXUS_DISPATCH_BACKEND",
        "NEXUS_DISPATCH_QWEN_OPERATORS",
        "NEXUS_DISPATCH_CLAUDE_OPERATORS",
    ):
        monkeypatch.delenv(var, raising=False)


# ── Default mode: preserves prior behavior ────────────────────────────────


class TestDefaultMode:
    def test_no_env_routes_everything_to_claude(self) -> None:
        for op in QWEN_OPERATORS_DEFAULT | CLAUDE_OPERATORS_PINNED:
            assert pick_dispatcher(op) == "claude", f"unexpected route for {op}"

    def test_unknown_operator_defaults_to_claude(self) -> None:
        assert pick_dispatcher("nonsense_operator") == "claude"

    def test_operator_prefix_doesnt_change_default(self) -> None:
        assert pick_dispatcher("operator_summarize") == "claude"
        assert pick_dispatcher("operator_extract") == "claude"


# ── NEXUS_DISPATCH_BACKEND global override ────────────────────────────────


class TestGlobalOverride:
    def test_backend_qwen_routes_everything_to_qwen(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NEXUS_DISPATCH_BACKEND", "qwen")
        for op in QWEN_OPERATORS_DEFAULT | CLAUDE_OPERATORS_PINNED:
            assert pick_dispatcher(op) == "qwen", f"unexpected route for {op}"

    def test_backend_claude_routes_everything_to_claude(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NEXUS_DISPATCH_BACKEND", "claude")
        for op in QWEN_OPERATORS_DEFAULT | CLAUDE_OPERATORS_PINNED:
            assert pick_dispatcher(op) == "claude"

    def test_backend_value_is_case_insensitive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NEXUS_DISPATCH_BACKEND", "QwEn")
        assert pick_dispatcher("summarize") == "qwen"

    def test_unrecognized_value_falls_back_to_claude(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NEXUS_DISPATCH_BACKEND", "gibberish")
        assert pick_dispatcher("summarize") == "claude"


# ── auto mode: bench-grounded routing table ───────────────────────────────


class TestAutoMode:
    @pytest.fixture(autouse=True)
    def _auto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_DISPATCH_BACKEND", "auto")

    @pytest.mark.parametrize("op", sorted(QWEN_OPERATORS_DEFAULT))
    def test_qwen_default_operators_route_to_qwen(self, op: str) -> None:
        assert pick_dispatcher(op) == "qwen"

    @pytest.mark.skipif(
        not CLAUDE_OPERATORS_PINNED,
        reason="CLAUDE_OPERATORS_PINNED is empty after extract was promoted to "
        "qwen-default; placeholder remains for future bench-driven pins",
    )
    @pytest.mark.parametrize("op", sorted(CLAUDE_OPERATORS_PINNED))
    def test_claude_pinned_operators_route_to_claude(self, op: str) -> None:
        assert pick_dispatcher(op) == "claude"

    def test_operator_prefix_treated_equivalently(self) -> None:
        for op in QWEN_OPERATORS_DEFAULT:
            assert pick_dispatcher(f"operator_{op}") == "qwen"
        for op in CLAUDE_OPERATORS_PINNED:
            assert pick_dispatcher(f"operator_{op}") == "claude"

    def test_unknown_operator_routes_to_claude(self) -> None:
        # Conservative — never silently route an unknown operator to
        # qwen even in auto mode.
        assert pick_dispatcher("brand_new_op") == "claude"


# ── Per-operator env pins win in every mode ───────────────────────────────


class TestPerOperatorPins:
    def test_qwen_pin_wins_over_default_claude(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Default mode = all-claude. Per-op pin to qwen overrides.
        monkeypatch.setenv("NEXUS_DISPATCH_QWEN_OPERATORS", "summarize,filter")
        assert pick_dispatcher("summarize") == "qwen"
        assert pick_dispatcher("filter") == "qwen"
        # Unspecified operator stays on the default.
        assert pick_dispatcher("compare") == "claude"

    def test_claude_pin_wins_over_qwen_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force-all-qwen mode. Per-op pin pulls one back to Claude.
        monkeypatch.setenv("NEXUS_DISPATCH_BACKEND", "qwen")
        monkeypatch.setenv("NEXUS_DISPATCH_CLAUDE_OPERATORS", "summarize")
        assert pick_dispatcher("summarize") == "claude"
        assert pick_dispatcher("compare") == "qwen"

    def test_claude_pin_wins_over_auto_mode_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NEXUS_DISPATCH_BACKEND", "auto")
        monkeypatch.setenv("NEXUS_DISPATCH_CLAUDE_OPERATORS", "summarize")
        assert pick_dispatcher("summarize") == "claude"
        # Other QWEN_OPERATORS_DEFAULT entries still go to qwen.
        assert pick_dispatcher("compare") == "qwen"

    def test_claude_pin_takes_priority_over_qwen_pin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Documented order: Claude pin checked first → Claude wins
        # when an operator appears in both env sets (operator error,
        # but be predictable).
        monkeypatch.setenv("NEXUS_DISPATCH_QWEN_OPERATORS", "compare")
        monkeypatch.setenv("NEXUS_DISPATCH_CLAUDE_OPERATORS", "compare")
        assert pick_dispatcher("compare") == "claude"

    def test_pin_handles_whitespace_and_empty_segments(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "NEXUS_DISPATCH_QWEN_OPERATORS", " summarize , , filter ,"
        )
        assert pick_dispatcher("summarize") == "qwen"
        assert pick_dispatcher("filter") == "qwen"


# ── Bundle-level routing ──────────────────────────────────────────────────


class TestBundleRouting:
    def test_all_qwen_steps_routes_bundle_to_qwen(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NEXUS_DISPATCH_BACKEND", "auto")
        assert (
            pick_dispatcher_for_bundle(["summarize", "compare", "rank"])
            == "qwen"
        )

    def test_any_claude_step_pulls_whole_bundle_to_claude(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NEXUS_DISPATCH_BACKEND", "auto")
        # Pin one operator to Claude via env. The bundle then contains
        # one Claude-routed step; conservative routing pulls the whole
        # bundle to Claude so it stays a single subprocess.
        monkeypatch.setenv("NEXUS_DISPATCH_CLAUDE_OPERATORS", "verify")
        assert (
            pick_dispatcher_for_bundle(["verify", "rank", "summarize"])
            == "claude"
        )

    def test_default_mode_bundles_route_to_claude(self) -> None:
        # No env set → all-claude default → bundle goes to Claude.
        assert (
            pick_dispatcher_for_bundle(["summarize", "compare"]) == "claude"
        )

    def test_force_qwen_mode_routes_bundle_to_qwen(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NEXUS_DISPATCH_BACKEND", "qwen")
        assert (
            pick_dispatcher_for_bundle(["summarize", "compare"]) == "qwen"
        )

    def test_empty_bundle_defaults_to_claude(self) -> None:
        assert pick_dispatcher_for_bundle([]) == "claude"

    def test_per_op_pin_in_bundle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # In default mode, pin one operator to qwen — the bundle still
        # contains other (default-claude) operators so it goes to claude.
        monkeypatch.setenv("NEXUS_DISPATCH_QWEN_OPERATORS", "summarize")
        assert (
            pick_dispatcher_for_bundle(["summarize", "compare"]) == "claude"
        )
