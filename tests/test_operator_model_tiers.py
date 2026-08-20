# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for the operator model-tier table + resolver (RDR-196 .p2b,
nexus-nyry9.15).

Design contract:
  * ``nexus.operators.model_tiers.OPERATOR_MODEL_TIER`` maps each of the
    10 real MCP operator names to a cost tier ("cheap" / "strong").
  * ``resolve_model_for_tier(tier)`` maps a tier to the CLI ``--model``
    alias to pass to ``claude_dispatch`` -- an ALIAS ("haiku"/"sonnet"),
    never a pinned model id (RDR-196 R3: DispatchUsage.model already
    records the canonical resolved id from the stream-json envelope, so
    an alias re-point stays observable in telemetry rather than
    silently invalidating every measurement keyed on this table).
  * ``resolve_model_for_operator(operator)`` composes the table + the
    tier resolver for callers that key on operator name directly.
  * NOTHING in this bead applies this table anywhere by default --
    ``claude_dispatch`` never imports this module (see
    ``tests/test_operator_dispatch.py::TestModelKwarg`` for the
    argv-observable half of that contract). The default-tier flip is a
    later bead (.p2d), on .p2c's measured evidence.
"""
from __future__ import annotations

import pytest


# The 10 real MCP operator names, as registered via @mcp.tool in
# src/nexus/mcp/core.py (operator_extract .. operator_aggregate).
# Read directly off core.py -- not invented -- so this test is a real
# non-vacuity check against the production call sites, not a tautology
# against whatever OPERATOR_MODEL_TIER happens to contain.
_REAL_OPERATOR_NAMES = frozenset({
    "operator_extract",
    "operator_rank",
    "operator_compare",
    "operator_summarize",
    "operator_generate",
    "operator_filter",
    "operator_check",
    "operator_verify",
    "operator_groupby",
    "operator_aggregate",
})

# RDR-196 §Approach's proposed split (lines 239-240 of
# docs/rdr/rdr-196-cost-aware-nx-answer.md) -- this bead ships the table
# with this content, but .p2c/.p2d are what actually apply it anywhere.
_EXPECTED_CHEAP = frozenset({
    "operator_extract", "operator_filter", "operator_groupby",
    "operator_aggregate", "operator_rank", "operator_summarize",
})
_EXPECTED_STRONG = frozenset({
    "operator_generate", "operator_check", "operator_verify",
    "operator_compare",
})


class TestOperatorModelTierTable:
    def test_table_covers_exactly_the_ten_real_operators(self) -> None:
        """Non-vacuity: the table's keys must equal the real operator
        names read off core.py -- not a subset (missing coverage) and
        not a superset (stale/invented entries)."""
        from nexus.operators.model_tiers import OPERATOR_MODEL_TIER

        assert set(OPERATOR_MODEL_TIER) == _REAL_OPERATOR_NAMES

    def test_table_matches_rdr_proposed_split(self) -> None:
        from nexus.operators.model_tiers import OPERATOR_MODEL_TIER

        cheap = {op for op, tier in OPERATOR_MODEL_TIER.items() if tier == "cheap"}
        strong = {op for op, tier in OPERATOR_MODEL_TIER.items() if tier == "strong"}
        assert cheap == _EXPECTED_CHEAP
        assert strong == _EXPECTED_STRONG

    def test_every_tier_value_is_a_known_tier(self) -> None:
        from nexus.operators.model_tiers import OPERATOR_MODEL_TIER, resolve_model_for_tier

        for operator, tier in OPERATOR_MODEL_TIER.items():
            # Must not raise -- every table entry names a tier the
            # resolver actually knows about.
            resolve_model_for_tier(tier)


class TestResolveModelForTier:
    def test_cheap_resolves_to_haiku_alias(self) -> None:
        from nexus.operators.model_tiers import resolve_model_for_tier

        assert resolve_model_for_tier("cheap") == "haiku"

    def test_strong_resolves_to_sonnet_alias(self) -> None:
        from nexus.operators.model_tiers import resolve_model_for_tier

        assert resolve_model_for_tier("strong") == "sonnet"

    def test_unknown_tier_raises_naming_the_tier(self) -> None:
        from nexus.operators.model_tiers import resolve_model_for_tier, UnknownTierError

        with pytest.raises(UnknownTierError, match="bogus-tier"):
            resolve_model_for_tier("bogus-tier")


class TestResolveModelForOperator:
    def test_composes_table_and_tier_resolver(self) -> None:
        from nexus.operators.model_tiers import resolve_model_for_operator

        assert resolve_model_for_operator("operator_extract") == "haiku"
        assert resolve_model_for_operator("operator_compare") == "sonnet"

    def test_unknown_operator_raises_naming_the_operator(self) -> None:
        from nexus.operators.model_tiers import resolve_model_for_operator, UnknownTierError

        with pytest.raises(UnknownTierError, match="operator_does_not_exist"):
            resolve_model_for_operator("operator_does_not_exist")


class TestSingleMappingLocation:
    """RDR-196 .p2b VERIFICATION: grep shows the tier -> model-id mapping
    exists in exactly ONE place. Locked here as an automated non-vacuity
    check rather than a purely manual grep, so a future accidental second
    mapping table fails CI instead of drifting silently."""

    def test_no_other_module_defines_a_tier_to_model_alias_mapping(self) -> None:
        import pathlib
        import re

        src_root = pathlib.Path(__file__).resolve().parent.parent / "src" / "nexus"
        offenders = []
        for path in src_root.rglob("*.py"):
            if path.name == "model_tiers.py":
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if re.search(r'["\']cheap["\']\s*:\s*["\']haiku["\']', text):
                offenders.append(str(path))
        assert offenders == [], (
            f"a tier->model alias mapping exists outside model_tiers.py: {offenders}"
        )


class TestNotConsultedRepoWide:
    """Review fix (nexus-nyry9.15, substantive-critic [23034] Significant
    #2): ``TestModelAndOperatorKwargs::test_default_path_never_consults_the_tier_table``
    (tests/test_operator_dispatch.py) only proves ``dispatch.py`` itself
    has no import of this module -- it says nothing about, e.g., a future
    eager prototype wiring ``resolve_model_for_operator`` into
    ``plans/runner.py``'s default call path, which would leave that
    file-scoped test passing while the table WAS being consulted by
    default elsewhere. This is the repo-wide counterpart, same pattern
    as ``TestSingleMappingLocation`` above: it walks every non-test
    source file and asserts none of them reach into this module. The
    default-tier flip is bead .p2d, which will deliberately update this
    test at the same time it wires the table in for real."""

    def test_model_tiers_imported_by_zero_production_modules(self) -> None:
        import pathlib
        import re

        src_root = pathlib.Path(__file__).resolve().parent.parent / "src" / "nexus"
        import_pattern = re.compile(
            r"^\s*(import\s+nexus\.operators\.model_tiers"
            r"|from\s+nexus\.operators(\.model_tiers)?\s+import\s+"
            r"(model_tiers|\w+.*model_tiers))",
            re.MULTILINE,
        )
        offenders = []
        for path in src_root.rglob("*.py"):
            if path.name == "model_tiers.py":
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if import_pattern.search(text):
                offenders.append(str(path))
        assert offenders == [], (
            f"model_tiers must not be imported by any production module "
            f"until .p2d's deliberate default-tier flip: {offenders}"
        )

    def test_no_production_module_calls_resolve_model_functions(self) -> None:
        import pathlib
        import re

        src_root = pathlib.Path(__file__).resolve().parent.parent / "src" / "nexus"
        call_pattern = re.compile(r"resolve_model_for_(tier|operator)\s*\(")
        offenders = []
        for path in src_root.rglob("*.py"):
            if path.name == "model_tiers.py":
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if call_pattern.search(text):
                offenders.append(str(path))
        assert offenders == [], (
            f"no production module may call resolve_model_for_tier/"
            f"resolve_model_for_operator until .p2d's deliberate "
            f"default-tier flip: {offenders}"
        )
