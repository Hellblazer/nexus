# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-h3e2: ``mode: broad`` plan-step authoring affordance.

The bug: per-corpus default thresholds (e.g. 0.65 for prose) were
tuned for narrow-target search and drop 100% of candidates for broad-
phrasing queries like "main themes in cognitive and neural mechanisms".
Plan authors writing abstract / community-summary plans had to know
the magic value ``threshold: 2.0`` (cosine distance maxes at 2.0,
effectively no filter) and override it. Surfaced via nexus-ldnp's
abstract-themes plan smoke test.

The fix: introduce a step-level ``mode`` field with values ``narrow``
(default; per-corpus threshold applies) and ``broad`` (threshold
overridden to 2.0 unless the step explicitly sets one). Plan authors
write ``mode: broad`` instead of memorising 2.0.

Implemented as a runner-side argument-shaping step, not a search-tool
kwarg, so the authoring concept stays out of the MCP tool API.
"""
from __future__ import annotations

import pytest


# ── Pure helper: _apply_mode_to_args ───────────────────────────────────────


class TestApplyModeToArgs:
    def test_broad_mode_sets_threshold_2_0_for_search(self):
        from nexus.plans.runner import _apply_mode_to_args

        out = _apply_mode_to_args(
            tool="search", args={"query": "themes", "mode": "broad"},
        )
        # ``mode`` is consumed; threshold is set; query passes through.
        assert out["threshold"] == 2.0
        assert "mode" not in out
        assert out["query"] == "themes"

    def test_explicit_threshold_overrides_broad(self):
        """A plan author who sets both wins explicitly — the runner
        must not stomp on a hand-set threshold even when mode=broad.
        """
        from nexus.plans.runner import _apply_mode_to_args

        out = _apply_mode_to_args(
            tool="search",
            args={"query": "themes", "mode": "broad", "threshold": 0.4},
        )
        assert out["threshold"] == 0.4
        assert "mode" not in out

    def test_narrow_mode_is_a_noop(self):
        """``mode: narrow`` is the default; presence does not change
        the args (the per-corpus default threshold applies as before).
        """
        from nexus.plans.runner import _apply_mode_to_args

        out = _apply_mode_to_args(
            tool="search", args={"query": "x", "mode": "narrow"},
        )
        assert "threshold" not in out
        assert "mode" not in out

    def test_unknown_mode_is_a_noop_with_warning(self, caplog):
        """A typo or unsupported mode name must not silently take effect."""
        import logging

        from nexus.plans.runner import _apply_mode_to_args

        with caplog.at_level(logging.WARNING):
            out = _apply_mode_to_args(
                tool="search", args={"query": "x", "mode": "bogus"},
            )
        assert "threshold" not in out
        assert "mode" not in out

    def test_no_mode_is_a_noop(self):
        from nexus.plans.runner import _apply_mode_to_args

        out = _apply_mode_to_args(
            tool="search", args={"query": "x", "limit": 30},
        )
        assert out == {"query": "x", "limit": 30}

    def test_mode_only_applies_to_retrieval_tools(self):
        """``mode`` on a non-retrieval tool is dropped without setting
        threshold (the threshold concept doesn't apply outside search/query).
        """
        from nexus.plans.runner import _apply_mode_to_args

        out = _apply_mode_to_args(
            tool="generate",
            args={"prompt": "x", "mode": "broad"},
        )
        assert "threshold" not in out
        assert "mode" not in out

    def test_query_tool_also_supports_broad(self):
        """``query`` (the document-level retrieval tool) shares the same
        threshold semantics as ``search``, so ``mode: broad`` should
        apply there too.
        """
        from nexus.plans.runner import _apply_mode_to_args

        out = _apply_mode_to_args(
            tool="query", args={"q": "themes", "mode": "broad"},
        )
        assert out["threshold"] == 2.0
        assert "mode" not in out


# ── abstract-themes.yml uses mode: broad ───────────────────────────────────


class TestAbstractThemesUsesMode:
    def test_abstract_themes_plan_uses_mode_broad(self):
        """The abstract-themes builtin should now express its broad
        retrieval intent via ``mode: broad`` instead of ``threshold: 2.0``.
        """
        import yaml
        from pathlib import Path

        path = Path("nx/plans/builtin/abstract-themes.yml")
        with path.open() as f:
            doc = yaml.safe_load(f)
        steps = doc.get("plan_json", {}).get("steps", [])
        search_step = next(
            (s for s in steps if s.get("tool") == "search"), None,
        )
        assert search_step is not None, "abstract-themes must have a search step"
        args = search_step.get("args", {})
        # The whole point: authors write the mode flag, not the magic value.
        assert args.get("mode") == "broad"
        # threshold should NOT be hard-coded any more; the runner
        # applies 2.0 from the mode at dispatch time.
        assert "threshold" not in args
