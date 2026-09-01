# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lint guard: no ``operator_*`` MCP tool builds its prompt/schema
inline at its dispatch site — RDR-200 Phase 0 (nexus-5mft0.1).

Structural AST check, modeled on
``tests/test_operator_model_tiers.py::TestNotConsultedRepoWide`` — a
substring/text check would be fooled by, e.g., a docstring mentioning
"prompt ="; this walks the real AST of each of the TEN ``operator_*``
functions and looks for a single-target ``Assign`` OR an annotated
``AnnAssign`` to a bare name ``prompt`` or ``schema`` (both historical
inline-construction shapes: 7/10 operators built ``schema: dict =
{...}`` annotated — an ``Assign``-only scan misses that form, the
nexus-5mft0.2 code-review finding).
After RDR-200 Phase 0's hoist, every one of the ten calls a
``build_<op>_request`` in ``nexus.mcp.operator_requests`` and unpacks
the result as ``prompt, schema = build_..._request(...)`` — a TUPLE
target, not a bare-name target — so this guard passes on the current
shape and fails the moment any of the ten reverts to constructing its
prompt/schema locally again.

SCOPE FENCE (deliberate, per the bead's audit residual): this guard
covers EXACTLY the ten ``operator_*`` tool functions named below.
``core.py`` has FIVE other ``claude_dispatch`` call sites that
legitimately build a prompt inline and must NOT be swept in by a
repo-wide version of this check — ``_generalize_grown_match_description``,
``_nx_answer_plan_miss``, ``nx_enrich_beads``, ``nx_tidy``, and
``nx_plan_audit`` (count corrected from "six" by the nexus-5mft0.2
critic's direct enumeration). Scoping via explicit
``getattr(mcp_core, name)`` rather than an AST walk of the whole file
is what keeps those five out of scope.
"""
from __future__ import annotations

import ast
import inspect

import pytest

from nexus.mcp import core as mcp_core

pytestmark = pytest.mark.lint

#: The exact ten operator_* MCP tools in scope for this guard (RDR-200
#: Phase 0). Deliberately NOT derived from a dynamic scan of core.py —
#: an explicit, reviewable list so a future eleventh operator lands in
#: this guard only when someone deliberately adds it here.
_OPERATOR_TOOL_NAMES: tuple[str, ...] = (
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
)

#: The five sites explicitly NOT covered (scope-fence documentation only
#: — not consulted by the guard, which scopes via _OPERATOR_TOOL_NAMES
#: alone; listed here so a reviewer can check the fence by eye).
_DELIBERATELY_OUT_OF_SCOPE: tuple[str, ...] = (
    "_generalize_grown_match_description",
    "_nx_answer_plan_miss",
    "nx_enrich_beads",
    "nx_tidy",
    "nx_plan_audit",
)


def _bare_name_assign_targets(fn) -> list[tuple[str, int]]:
    """Return ``(name, lineno)`` for every single-target ``Assign`` OR
    annotated ``AnnAssign`` in *fn* whose target is a bare ``ast.Name``
    (not part of a tuple/list unpack) named ``prompt`` or ``schema``.

    ``AnnAssign`` is load-bearing: 7/10 operators' historical schema
    construction was the annotated ``schema: dict = {...}`` form, which
    an ``Assign``-only scan cannot see — a schema-only revert to that
    exact syntax would have slipped this guard (nexus-5mft0.2
    code-review finding, folded)."""
    source = inspect.getsource(fn)
    tree = ast.parse(source)
    fn_node = tree.body[0]
    offenders = []
    for node in ast.walk(fn_node):
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1:
                continue
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        else:
            continue
        if isinstance(target, ast.Name) and target.id in ("prompt", "schema"):
            offenders.append((target.id, node.lineno))
    return offenders


class TestOperatorPromptsAreHoisted:
    def test_scope_list_is_non_vacuous(self) -> None:
        """All ten in-scope operators must actually be checked — a
        shrunk list would silently reduce coverage without any other
        test noticing (nexus-moht0 vacuous-gate doctrine)."""
        assert len(_OPERATOR_TOOL_NAMES) == 10

    def test_every_scoped_name_resolves_on_mcp_core(self) -> None:
        """Guard against a stale/typo'd name in the allowlist silently
        being skipped rather than failing loud."""
        for name in _OPERATOR_TOOL_NAMES:
            assert hasattr(mcp_core, name), f"nexus.mcp.core has no attribute {name!r}"

    def test_no_operator_tool_builds_prompt_or_schema_inline(self) -> None:
        offenders: list[str] = []
        for name in _OPERATOR_TOOL_NAMES:
            fn = getattr(mcp_core, name)
            for target_name, lineno in _bare_name_assign_targets(fn):
                offenders.append(f"{name}:{lineno} (`{target_name} = ...`)")
        assert offenders == [], (
            "operator tool(s) construct prompt/schema inline instead of via "
            f"a build_<op>_request builder in nexus.mcp.operator_requests: {offenders}"
        )

    def test_out_of_scope_sites_still_exist(self) -> None:
        """The scope-fence list documents real sites, not stale names —
        catches drift if one of the five is renamed/removed without this
        doc comment being updated."""
        for name in _DELIBERATELY_OUT_OF_SCOPE:
            assert hasattr(mcp_core, name), (
                f"scope-fence doc names {name!r}, which no longer exists on "
                f"nexus.mcp.core — update this test's documentation"
            )
