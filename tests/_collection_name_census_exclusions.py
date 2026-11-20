# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Sites the raw collection-name scan flags and the census removes by
judgement (RDR-204 Phase 3 item 1's exclusion rule), split out of
``tests/test_collection_name_parse_census.py`` by RDR-229 P1.1
(nexus-p2m1x.1) so that file stays under its size ceiling.

Two false positives the routing census removed by reading
(T2 ``229-research-39``) need no entry any more, and are covered by negative
fixtures instead. ``commands/taxonomy_cmd.py``'s ``_find_by_prefix`` tests a
doc id against a parameter named ``prefix``: the widened scanner scopes loop
names per function, so the loop name in its parent no longer reaches it.
``mcp/core.py``'s ``_nx_answer_plan_miss`` hint says "bare prefixes like
'knowledge' or 'code' will miss": the widened scanner reads SQL ``LIKE`` in
upper case only. Re-checked on develop 970f73223: neither line is a hit.
"""
from __future__ import annotations

#: (relative-path, content-anchor, shape) -> one-line reason, for sites the raw
#: AST scan WOULD flag but which the RDR-204 Phase 3 item 1 exclusion rule
#: (``mcp__`` tool names, ``rdr-`` document ids) removes from the pin.
#:
#: SHAPE-SPECIFIC (RDR-229 P1.1 review): an entry removes the one shape
#: it names on that line. A different shape on the same line stays a hit.
#:
#: CONTENT-KEYED, not line-keyed (nexus-vkpr3): the anchor is the
#: exempted line's own stripped source text PLUS its nearest preceding
#: non-blank line (the mandatory two-line convention -- see
#: ``tests._lint_line_anchor``'s MANDATORY TWO-LINE MINIMUM section for
#: why a single line is not enough), resolved to its CURRENT line number
#: by ``tests._lint_line_anchor.resolve_anchor`` on every run. A
#: line-number key silently re-targets onto whatever different code now
#: sits at the stored number the moment anything is inserted above it;
#: this file's entries were themselves retargeted by line-shift
#: arithmetic several times before the conversion (7887 -> 7900 -> 7934
#: -> 8317 -> 8501 -> 8528 -> 8630 -> 8644 for the mcp/core.py entry
#: alone) — exactly the churn a content anchor makes structurally
#: impossible, since the anchor's own text does not move. Every entry
#: here is independently verified as a real raw match by
#: ``test_excluded_sites_are_real_matches_not_omissions`` — an anchor
#: that no longer resolves (STALE) or resolves ambiguously is a stale
#: judgement call, not a quieter gate.
EXCLUDED_SITES: dict[tuple[str, tuple[str, ...], str], str] = {
    (
        "src/nexus/mcp/core.py",
        (
            'raw_tool = step.get("tool", "")',
            'bare = raw_tool.rsplit("__", 1)[-1] if '
            'raw_tool.startswith("mcp__") else raw_tool',
        ),
        "rsplit",
    ): (
        "mcp__ tool name: `raw_tool.rsplit(\"__\", 1)[-1] if "
        'raw_tool.startswith("mcp__")` strips an MCP tool-name prefix for '
        "planner-step normalization, not a collection name."
    ),
    (
        "src/nexus/plans/answer_kind.py",
        ('if tool.startswith("mcp__"):', 'tool = tool.rsplit("__", 1)[-1]'),
        "rsplit",
    ): (
        "mcp__ tool name: `terminal_is_retrieval_only` strips the mcp__...__ "
        "prefix from a plan's last step the way the runner does (nexus-0vh80)."
    ),
    (
        "src/nexus/plans/bundle.py",
        ('if tool.startswith("mcp__"):', 'tool = tool.rsplit("__", 1)[-1]'),
        "rsplit",
    ): (
        "mcp__ tool name: `_extract_tool_name`'s own docstring says it "
        '"strips any mcp__...__ prefix" from a plan step\'s tool identifier.'
    ),
    (
        "src/nexus/plans/cost_estimate.py",
        ('if tool.startswith("mcp__"):', 'tool = tool.rsplit("__", 1)[-1]'),
        "rsplit",
    ): (
        "mcp__ tool name: `_extract_tool`, the documented local copy of "
        "bundle._extract_tool_name, strips the same mcp__...__ prefix."
    ),
    (
        "src/nexus/plans/runner.py",
        ('if t.startswith("mcp__"):', 't = t.rsplit("__", 1)[-1]'),
        "rsplit",
    ): (
        "mcp__ tool name: strips an mcp__...__ prefix from a resolved plan "
        "step's tool identifier before dispatch, guarded by the same "
        '`startswith("mcp__")` check as the other three sites.'
    ),
    (
        "src/nexus/mcp/hooks.py",
        ('*parents, leaf = flat_name.split("__")',),
        "split",
    ): (
        "not a collection name at all: RDR-215's tool-tier hook_<name> "
        'registration flattens a dotted payload field path (e.g. '
        '"tool_input.command") to a valid tool-parameter/JSON-Schema '
        "identifier by joining segments with `__` (`flatten_field_name`); "
        "`nest_payload` is the exact inverse, splitting that identifier "
        "back into the nested dict a ported hook's `run()` reads. No "
        "collection ever passes through this path."
    ),
}
