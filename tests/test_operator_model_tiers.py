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

import ast

import pytest


def _model_tiers_import_lines(source: str, filename: str = "<scratch>") -> list[int]:
    """AST-based scan for ANY reference to ``nexus.operators.model_tiers``
    — import, from-import (any imported NAME, any alias, any case), or a
    fully-qualified attribute chain ending in ``.model_tiers`` written
    inline without an import. Returns offending line numbers.

    Coordinator addendum (sibling .20, 2026-08-21): the prior regex
    ``(model_tiers|\\w+.*model_tiers)`` only matched a LOWERCASE
    ``model_tiers`` substring in the imported name, so
    ``from nexus.operators.model_tiers import OPERATOR_MODEL_TIER`` (the
    table's own real uppercase export) sailed through undetected — the
    regex checked the imported NAME string for the module's own name,
    not what module the import came FROM. AST inspection checks the
    ``ImportFrom.module`` string instead, which is case-exact and
    independent of what gets imported or how it is aliased.
    """
    offenders: list[int] = []
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return offenders
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "nexus.operators.model_tiers":
                    offenders.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "nexus.operators.model_tiers":
                offenders.append(node.lineno)
            elif node.module == "nexus.operators" and any(
                alias.name == "model_tiers" for alias in node.names
            ):
                offenders.append(node.lineno)
        elif isinstance(node, ast.Attribute):
            if node.attr == "model_tiers":
                offenders.append(node.lineno)
    return sorted(set(offenders))


def _resolve_model_call_lines(source: str, filename: str = "<scratch>") -> list[int]:
    """AST-based scan for a call to ``resolve_model_for_tier`` /
    ``resolve_model_for_operator`` under any name/attribute/alias."""
    offenders: list[int] = []
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return offenders
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else None
        )
        if name in ("resolve_model_for_tier", "resolve_model_for_operator"):
            offenders.append(node.lineno)
    return sorted(set(offenders))


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

# RDR-196 §Approach's ORIGINAL proposed split (lines 239-240 of
# docs/rdr/rdr-196-cost-aware-nx-answer.md) named aggregate/summarize
# "cheap" too. CORRECTED (review fix, nexus-nyry9.16 round-2, T2 [23144]
# Significant #6): pinned to "strong" instead, since no quality proxy
# exists for either (196-R4) -- "flip-eligible" requires evidence, not
# just the RDR's original textual proposal. See
# model_tiers.OPERATOR_MODEL_TIER's own docstring comment for the full
# rationale.
_EXPECTED_CHEAP = frozenset({
    "operator_extract", "operator_filter", "operator_groupby",
    "operator_rank",
})
_EXPECTED_STRONG = frozenset({
    "operator_generate", "operator_check", "operator_verify",
    "operator_compare", "operator_aggregate", "operator_summarize",
})


class TestOperatorModelTierTable:
    def test_table_covers_exactly_the_ten_real_operators(self) -> None:
        """Non-vacuity: the table's keys must equal the real operator
        names read off core.py -- not a subset (missing coverage) and
        not a superset (stale/invented entries)."""
        from nexus.operators.model_tiers import OPERATOR_MODEL_TIER

        assert set(OPERATOR_MODEL_TIER) == _REAL_OPERATOR_NAMES

    def test_table_matches_corrected_split(self) -> None:
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

    def test_every_cheap_entry_has_a_registered_proxy_metric(self) -> None:
        """RDR-196 .p2a built a tier-agreement quality proxy for exactly
        6 operators (filter/groupby/rank/extract/check/verify) --
        ``scripts/bench/operator_proxy_metrics.THRESHOLDS``. A "cheap"
        entry in this table asserts "safe to route to a cheaper model";
        that assertion is unfounded for an operator with no proxy to
        measure the tier delta against. This mechanically ties the
        table to the proxy's registered set so a future addition to
        OPERATOR_MODEL_TIER (e.g. re-flipping aggregate/summarize once
        a proxy exists for them) can't silently skip re-adding the
        proxy coverage first."""
        import sys as _sys
        from pathlib import Path as _Path

        bench_parent = _Path(__file__).resolve().parent.parent / "scripts"
        if str(bench_parent) not in _sys.path:
            _sys.path.insert(0, str(bench_parent))
        from bench.operator_proxy_metrics import THRESHOLDS

        from nexus.operators.model_tiers import OPERATOR_MODEL_TIER

        cheap = {op for op, tier in OPERATOR_MODEL_TIER.items() if tier == "cheap"}
        unproxied_cheap = cheap - set(THRESHOLDS)
        assert unproxied_cheap == set(), (
            f"cheap-tier operator(s) with no registered .p2a proxy "
            f"metric: {sorted(unproxied_cheap)} -- pin to 'strong' "
            f"until scripts/bench/operator_proxy_metrics.THRESHOLDS "
            f"gains an entry for them"
        )


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

    #: RDR-196 .p2c (nexus-nyry9.16): the ONE measurement-only opt-in call
    #: site this bead adds, gated behind ``NX_OPERATOR_MODEL_TIERING=1``
    #: (see ``plans/runner.py::_default_dispatcher`` and
    #: ``mcp/core.py::_nx_answer_plan_miss``) — unset (every caller other
    #: than the .p2c A/B harness) leaves both files' default behavior
    #: byte-identical to pre-.p2c. This is NOT .p2d's default-tier flip;
    #: that bead will update this allowlist deliberately when it wires
    #: the table in for real (unconditionally, no env gate).
    #:
    #: RELATIVE PATH from ``src/nexus/``, not a bare filename (code-review
    #: fix, T2 [23143]): a bare-filename allowlist also silently exempted
    #: ``src/nexus/mcp_client/core.py`` and
    #: ``src/nexus/upgrade_ladder/runner.py`` — two unrelated modules that
    #: happen to share a basename with the two real opt-in sites — from
    #: the "not consulted" guard entirely.
    _P2C_MEASUREMENT_ONLY_OPT_IN_FILES = frozenset({
        "plans/runner.py",
        "mcp/core.py",
    })

    @staticmethod
    def _p2c_relpath(path) -> str:
        import pathlib

        src_root = pathlib.Path(__file__).resolve().parent.parent / "src" / "nexus"
        return path.relative_to(src_root).as_posix()

    def test_model_tiers_imported_by_zero_production_modules(self) -> None:
        import pathlib

        src_root = pathlib.Path(__file__).resolve().parent.parent / "src" / "nexus"
        offenders = []
        for path in src_root.rglob("*.py"):
            if path.name == "model_tiers.py":
                continue
            if self._p2c_relpath(path) in self._P2C_MEASUREMENT_ONLY_OPT_IN_FILES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if _model_tiers_import_lines(text, filename=str(path)):
                offenders.append(str(path))
        assert offenders == [], (
            f"model_tiers must not be imported/referenced by any "
            f"production module (outside the .p2c measurement-only "
            f"opt-in) until .p2d's deliberate default-tier flip: "
            f"{offenders}"
        )

    def test_model_tiers_import_scan_catches_uppercase_symbol_import(self) -> None:
        """Coordinator addendum (sibling .20, 2026-08-21): the regex this
        replaced only matched a lowercase ``model_tiers`` substring in
        the imported NAME, so importing the table's own real uppercase
        export (``OPERATOR_MODEL_TIER``) sailed through undetected. Prove
        the AST helper catches it, directly, without touching a real
        src/nexus file."""
        scratch_uppercase_symbol = (
            "from nexus.operators.model_tiers import OPERATOR_MODEL_TIER\n"
        )
        assert _model_tiers_import_lines(scratch_uppercase_symbol) == [1]

        scratch_aliased_call = (
            "from nexus.operators.model_tiers import "
            "resolve_model_for_operator as rmfo\n"
            "rmfo('operator_extract')\n"
        )
        assert _model_tiers_import_lines(scratch_aliased_call) == [1]

        scratch_bare_import = "import nexus.operators.model_tiers\n"
        assert _model_tiers_import_lines(scratch_bare_import) == [1]

        scratch_submodule_import = "from nexus.operators import model_tiers\n"
        assert _model_tiers_import_lines(scratch_submodule_import) == [1]

        scratch_inline_attribute = (
            "import nexus.operators\n"
            "t = nexus.operators.model_tiers.OPERATOR_MODEL_TIER\n"
        )
        assert _model_tiers_import_lines(scratch_inline_attribute) == [2]

        scratch_clean = "from nexus.operators import aspect_sql\n"
        assert _model_tiers_import_lines(scratch_clean) == []

    def test_no_production_module_calls_resolve_model_functions(self) -> None:
        import pathlib

        src_root = pathlib.Path(__file__).resolve().parent.parent / "src" / "nexus"
        offenders = []
        for path in src_root.rglob("*.py"):
            if path.name == "model_tiers.py":
                continue
            if self._p2c_relpath(path) in self._P2C_MEASUREMENT_ONLY_OPT_IN_FILES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if _resolve_model_call_lines(text, filename=str(path)):
                offenders.append(str(path))
        assert offenders == [], (
            f"no production module may call resolve_model_for_tier/"
            f"resolve_model_for_operator (outside the .p2c measurement-"
            f"only opt-in) until .p2d's deliberate default-tier flip: "
            f"{offenders}"
        )

    def test_p2c_opt_in_call_sites_are_env_gated(self) -> None:
        """The two allowlisted files' calls to the resolver must be
        STRUCTURALLY guarded behind ``NX_OPERATOR_MODEL_TIERING`` — code-
        review fix (T2 [23143]): a whole-file substring check ("the env
        name appears SOMEWHERE in the file") would pass even if the
        resolver call and the env check were in unrelated functions. This
        walks the AST and requires the ``resolve_model_for_*`` Call node
        to be lexically NESTED inside an ``if`` (or ``while``) block whose
        test expression's source mentions ``NX_OPERATOR_MODEL_TIERING`` —
        the mechanical half of "exactly one call site, gated on the env"
        the .p2c bead DO instructs.
        """
        import ast
        import pathlib

        src_root = pathlib.Path(__file__).resolve().parent.parent / "src" / "nexus"
        found_any = False
        for path in src_root.rglob("*.py"):
            if self._p2c_relpath(path) not in self._P2C_MEASUREMENT_ONLY_OPT_IN_FILES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "resolve_model_for_" not in text:
                continue
            tree = ast.parse(text, filename=str(path))

            # Map every node to its parent so a resolver Call can walk
            # up to find an enclosing If/While.
            parent_of: dict[ast.AST, ast.AST] = {}
            for node in ast.walk(tree):
                for child in ast.iter_child_nodes(node):
                    parent_of[child] = node

            resolver_calls = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr.startswith("resolve_model_for_")
                or (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id.startswith("resolve_model_for_")
                )
            ]
            if not resolver_calls:
                continue
            found_any = True
            for call in resolver_calls:
                node: ast.AST | None = call
                gated = False
                while node is not None:
                    node = parent_of.get(node)
                    if isinstance(node, (ast.If, ast.While)):
                        test_src = ast.get_source_segment(text, node.test) or ""
                        if "NX_OPERATOR_MODEL_TIERING" in test_src:
                            gated = True
                            break
                assert gated, (
                    f"{path}: a resolve_model_for_* call at line "
                    f"{call.lineno} is not lexically nested inside an "
                    f"if/while testing NX_OPERATOR_MODEL_TIERING"
                )
        assert found_any, (
            "expected at least one of the allowlisted files to actually "
            "call a model_tiers resolver — if neither does, the "
            "allowlist entries are stale and should be removed"
        )


class TestP2COptInDispatcherBehavior:
    """RDR-196 .p2c (nexus-nyry9.16) DO 7: 'a test that with the env
    UNSET the argv has no --model and with it SET the argv has the
    tier's alias' — the behavioral half of the opt-in, exercised
    directly against ``plans/runner.py``'s ``_default_dispatcher``
    (isolated-step path) without spending a real dispatch."""

    @pytest.mark.asyncio
    async def test_env_unset_injects_no_model_kwarg(self, monkeypatch) -> None:
        monkeypatch.delenv("NX_OPERATOR_MODEL_TIERING", raising=False)
        from nexus.plans.runner import _default_dispatcher
        from nexus.mcp import core as mcp_core

        captured: dict = {}

        async def fake_operator_filter(items, criterion, timeout=300.0,
                                        source="auto", aspect_field="",
                                        model=None):
            captured["model"] = model
            return {"items": [], "rationale": []}

        monkeypatch.setattr(mcp_core, "operator_filter", fake_operator_filter)
        await _default_dispatcher("filter", {"items": "[]", "criterion": "x"})
        assert captured["model"] is None

    @pytest.mark.asyncio
    async def test_env_set_injects_tier_alias(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_OPERATOR_MODEL_TIERING", "1")
        from nexus.plans.runner import _default_dispatcher
        from nexus.mcp import core as mcp_core

        captured: dict = {}

        async def fake_operator_filter(items, criterion, timeout=300.0,
                                        source="auto", aspect_field="",
                                        model=None):
            captured["model"] = model
            return {"items": [], "rationale": []}

        monkeypatch.setattr(mcp_core, "operator_filter", fake_operator_filter)
        await _default_dispatcher("filter", {"items": "[]", "criterion": "x"})
        assert captured["model"] == "haiku"

    @pytest.mark.asyncio
    async def test_env_set_never_overrides_caller_supplied_model(self, monkeypatch) -> None:
        """A step whose args already name a ``model`` (an author's
        explicit override) wins — the opt-in only fills a gap."""
        monkeypatch.setenv("NX_OPERATOR_MODEL_TIERING", "1")
        from nexus.plans.runner import _default_dispatcher
        from nexus.mcp import core as mcp_core

        captured: dict = {}

        async def fake_operator_filter(items, criterion, timeout=300.0,
                                        source="auto", aspect_field="",
                                        model=None):
            captured["model"] = model
            return {"items": [], "rationale": []}

        monkeypatch.setattr(mcp_core, "operator_filter", fake_operator_filter)
        await _default_dispatcher(
            "filter", {"items": "[]", "criterion": "x", "model": "opus"},
        )
        assert captured["model"] == "opus"

    @pytest.mark.asyncio
    async def test_env_set_untiered_tool_unaffected(self, monkeypatch) -> None:
        """A non-operator tool (not in ``OPERATOR_MODEL_TIER`` at all)
        never gets a ``model`` kwarg injected, opt-in or not."""
        monkeypatch.setenv("NX_OPERATOR_MODEL_TIERING", "1")
        from nexus.plans.runner import _default_dispatcher
        from nexus.mcp import core as mcp_core

        captured: dict = {"called": False}

        def fake_search(query, **kwargs):
            captured["called"] = True
            assert "model" not in kwargs
            return {"ids": [], "tumblers": [], "distances": [], "collections": []}

        monkeypatch.setattr(mcp_core, "search", fake_search)
        await _default_dispatcher("search", {"query": "x", "structured": True})
        assert captured["called"] is True


class TestP2COptInPlannerBehavior:
    """RDR-196 .p2c (nexus-nyry9.16): the inline planner's own
    ``claude_dispatch`` call, gated on the SAME env var (planner tier
    is fixed to "cheap" when opt-in is on — see ``_nx_answer_plan_miss``'s
    DO-7 comment)."""

    @pytest.mark.asyncio
    async def test_env_unset_planner_dispatches_with_no_model(self, monkeypatch) -> None:
        monkeypatch.delenv("NX_OPERATOR_MODEL_TIERING", raising=False)
        import nexus.operators.dispatch as _dispatch_mod
        from nexus.mcp.core import _nx_answer_plan_miss

        captured: dict = {}

        async def fake_dispatch(prompt, schema, timeout=300.0, model=None):
            captured["model"] = model
            return {"steps": [{"tool": "search", "args": {"query": "$intent"}}]}

        monkeypatch.setattr(_dispatch_mod, "claude_dispatch", fake_dispatch)
        await _nx_answer_plan_miss("how does X work")
        assert captured["model"] is None

    @pytest.mark.asyncio
    async def test_env_set_planner_dispatches_at_cheap_tier(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_OPERATOR_MODEL_TIERING", "1")
        import nexus.operators.dispatch as _dispatch_mod
        from nexus.mcp.core import _nx_answer_plan_miss

        captured: dict = {}

        async def fake_dispatch(prompt, schema, timeout=300.0, model=None):
            captured["model"] = model
            return {"steps": [{"tool": "search", "args": {"query": "$intent"}}]}

        monkeypatch.setattr(_dispatch_mod, "claude_dispatch", fake_dispatch)
        await _nx_answer_plan_miss("how does X work")
        assert captured["model"] == "haiku"
