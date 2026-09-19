# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Port of ``conexus/hooks/scripts/auto-approve-nx-mcp.sh`` (RDR-215 bead nexus-q02nx.4).

MVV port A: the first real hook module on the tool tier. Registered as
``hook_auto_approve`` in ``nexus.mcp.hooks.HOOK_TOOLS``.

**Byte-for-byte contract** (T2 ``nexus_rdr/215-hook-contract-map`` row 2). Reads
``tool_name`` and ``hook_event_name`` from the payload. On an allowlist match:
``PreToolUse`` renders
``{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision":
"allow", "permissionDecisionReason": "plugin allowlist (auto-approve)"}}``; any
other (or absent) ``hook_event_name`` -- the bash script's own default -- renders
the ``PermissionRequest`` form
``{"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision":
{"behavior": "allow"}}}``. No match is **silent** -- ``HookResult()``, not an
empty envelope. Always exit 0 (``HookResult``'s own default).

**One deliberate addition, not a bash behaviour carried forward.** The bash
script's case list is 62 explicit literal names, reproduced verbatim in
:data:`_ALLOWED_TOOLS` below -- "no wildcards" per the script's own header
comment, and the contract map records it the same way. This port ADDS a
second, generic check: any ``mcp__plugin_conexus_nexus__hook_*`` name is also
allowed. MCP has no way to hide a tool from the model's tool list (RDR-215
Technical Design, "Hook tools in the model's tool list"), and this hook is
itself the first tool registered under that prefix -- the RDR names the
auto-approve matcher as part of that mitigation ("the ``hook_`` prefix, the
description, and the auto-approve matcher are the mitigation") and bead
nexus-q02nx.4 states it as a requirement. It is also mechanically forced: the
live drift guard (``tests/hooks/test_permission_request_hooks.py::
test_every_registered_conexus_tool_is_auto_approved``) parametrizes over every
tool the conexus MCP servers actually register, which now includes
``hook_auto_approve`` itself the moment it is registered -- without this
carve-out the hook would fail to approve its own tool name. The frozen bash
script, unedited by this bead (its ``hooks.json`` re-declaration and deletion
are beads nexus-q02nx.21/.22), has no equivalent: it predates every ``hook_``
tool and cannot know about them without editing its case list, which is
exactly the manual-maintenance burden this generic check avoids repeating on
every future port.

**AUDIT RESIDUAL (bead nexus-q02nx.4, round 1, Moderate): hook-chain re-entry
is measured by bead nexus-q02nx.6, not resolved here.** Auto-approving
``hook_*`` tool calls does not by itself prove a model-initiated call to one
terminates rather than re-entering the PreToolUse chain against its own
matcher; that measurement, and any consequent exclusion or short-circuit, is
that bead's job.
"""
from __future__ import annotations

from nexus.hooks._io import HookResult, permission_decision, permission_request

#: Explicit full tool names, no wildcards -- carried byte-for-byte from
#: auto-approve-nx-mcp.sh's case statement (T2 nexus_rdr/215-hook-contract-map
#: row 2). ``mcp__plugin_conexus_nexus__daemon_uninstall`` is deliberately
#: ABSENT: it can tear down the storage-service OS autostart unit and,
#: with ``remove_data=true``, irreversibly delete the entire nexus config
#: directory, and its own ``confirm=true`` parameter is a trivial self-gate
#: an agent satisfies with one more tool call, not a human-in-the-loop
#: check (nexus-cnzei.5, preserved from the bash script's own header
#: comment). Do not add it here without a fresh review of that reasoning.
_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "mcp__plugin_conexus_nexus__search",
        "mcp__plugin_conexus_nexus__query",
        "mcp__plugin_conexus_nexus__search_metadata_scoped",
        "mcp__plugin_conexus_nexus__search_topic_scoped",
        "mcp__plugin_conexus_nexus__search_graph_hop",
        "mcp__plugin_conexus_nexus__search_aspect_scoped",
        "mcp__plugin_conexus_nexus__store_put",
        "mcp__plugin_conexus_nexus__store_get",
        "mcp__plugin_conexus_nexus__store_get_many",
        "mcp__plugin_conexus_nexus__store_list",
        "mcp__plugin_conexus_nexus__memory_put",
        "mcp__plugin_conexus_nexus__memory_get",
        "mcp__plugin_conexus_nexus__memory_search",
        "mcp__plugin_conexus_nexus__memory_delete",
        "mcp__plugin_conexus_nexus__memory_consolidate",
        "mcp__plugin_conexus_nexus__scratch",
        "mcp__plugin_conexus_nexus__scratch_manage",
        "mcp__plugin_conexus_nexus__collection_list",
        "mcp__plugin_conexus_nexus__plan_save",
        "mcp__plugin_conexus_nexus__plan_search",
        "mcp__plugin_conexus_nexus__plan_delete",
        "mcp__plugin_conexus_nexus__traverse",
        "mcp__plugin_conexus_nexus__nx_answer",
        "mcp__plugin_conexus_nexus__nx_answer_report",
        "mcp__plugin_conexus_nexus__nx_tidy",
        "mcp__plugin_conexus_nexus__nx_enrich_beads",
        "mcp__plugin_conexus_nexus__nx_plan_audit",
        "mcp__plugin_conexus_nexus__operator_summarize",
        "mcp__plugin_conexus_nexus__operator_extract",
        "mcp__plugin_conexus_nexus__operator_rank",
        "mcp__plugin_conexus_nexus__operator_compare",
        "mcp__plugin_conexus_nexus__operator_generate",
        "mcp__plugin_conexus_nexus__operator_filter",
        "mcp__plugin_conexus_nexus__operator_check",
        "mcp__plugin_conexus_nexus__operator_verify",
        "mcp__plugin_conexus_nexus__operator_groupby",
        "mcp__plugin_conexus_nexus__operator_aggregate",
        "mcp__plugin_conexus_nexus-catalog__search",
        "mcp__plugin_conexus_nexus-catalog__show",
        "mcp__plugin_conexus_nexus-catalog__list",
        "mcp__plugin_conexus_nexus-catalog__register",
        "mcp__plugin_conexus_nexus-catalog__update",
        "mcp__plugin_conexus_nexus-catalog__link",
        "mcp__plugin_conexus_nexus-catalog__links",
        "mcp__plugin_conexus_nexus-catalog__link_query",
        "mcp__plugin_conexus_nexus-catalog__resolve",
        "mcp__plugin_conexus_nexus-catalog__stats",
        "mcp__plugin_conexus_nexus__tuple_out",
        "mcp__plugin_conexus_nexus__tuple_rd",
        "mcp__plugin_conexus_nexus__tuple_in",
        "mcp__plugin_conexus_nexus__tuple_ack",
        "mcp__plugin_conexus_nexus__tuple_nack",
        "mcp__plugin_conexus_nexus__tuple_renew",
        "mcp__plugin_conexus_nexus__tuple_release",
        "mcp__plugin_conexus_nexus__tuple_registry",
        "mcp__plugin_conexus_nexus__tuple_list",
        "mcp__plugin_conexus_nexus__tuple_stats",
        "mcp__plugin_conexus_nexus__tuple_subscribe",
        "mcp__plugin_conexus_nexus__tuple_unsubscribe",
        "mcp__plugin_conexus_nexus__tuple_subscriptions",
        "mcp__plugin_conexus_nexus__mailbox_send",
        "mcp__plugin_conexus_sequential-thinking__sequentialthinking",
    }
)

#: See the module docstring's "One deliberate addition" section.
_HOOK_TOOL_PREFIX = "mcp__plugin_conexus_nexus__hook_"


def _is_allowed(tool_name: str) -> bool:
    return tool_name in _ALLOWED_TOOLS or tool_name.startswith(_HOOK_TOOL_PREFIX)


def run(payload: dict | None) -> HookResult:
    """Decide whether *payload*'s ``tool_name`` is auto-approved.

    Mirrors ``auto-approve-nx-mcp.sh`` exactly: a non-match returns
    ``HookResult()`` (silent -- no stdout at all, not an empty envelope);
    a match renders the event-appropriate envelope via ``_io``'s shared
    writers, which reproduce the bash layer's key order byte for byte.
    """
    data = payload or {}
    tool_name = data.get("tool_name", "")
    if not _is_allowed(tool_name):
        return HookResult()

    event = data.get("hook_event_name", "PermissionRequest")
    if event == "PreToolUse":
        stdout = permission_decision(
            "PreToolUse",
            "allow",
            permission_decision_reason="plugin allowlist (auto-approve)",
        )
    else:
        stdout = permission_request("allow")
    return HookResult(stdout=stdout)
