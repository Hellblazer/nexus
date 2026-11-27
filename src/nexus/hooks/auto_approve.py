# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Port of ``conexus/hooks/scripts/auto-approve-nx-mcp.sh`` (RDR-215 bead nexus-q02nx.4).

MVV port A: the first real hook module on the tool tier. Registered as
``hook_auto_approve`` in ``nexus.mcp.hooks.HOOK_TOOLS``.

**Byte-for-byte contract** (T2 ``nexus_rdr/215-hook-contract-map`` row 2). Reads
``tool_name`` and ``hook_event_name`` from the payload. On an allowlist match:
``PreToolUse`` renders
``{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision":
"allow", "permissionDecisionReason": "plugin allowlist (auto-approve)"}}``.
**Any other ``hook_event_name``, absent or ``PermissionRequest`` included, is
silent** (RDR-259 S4, nexus-sby49.6): the bash script defaulted an absent event
to ``PermissionRequest`` and answered it, and an answer on that event overrides
a user's own ``ask`` rule for the tool, so the verb answers ``PreToolUse`` and
nothing else. No match is **silent** too -- ``HookResult()``, not an empty
envelope. Always exit 0 (``HookResult``'s own default).

**One deliberate addition, not a bash behaviour carried forward.** The bash
script's case list is 62 explicit literal names, reproduced verbatim in
:data:`_ALLOWED_TOOLS` below -- "no wildcards" per the script's own header
comment, and the contract map records it the same way. This port adds the
registered ``hook_<name>`` tools, in :data:`_ALLOWED_HOOK_TOOLS`, by the same
rule: one explicit name each. MCP has no way to hide a tool from the model's
tool list (RDR-215 Technical Design), so a hook tool is callable by the model
and the RDR names the auto-approve matcher as part of the mitigation; the live
drift guard (``tests/hooks/test_permission_request_hooks.py::
test_every_registered_conexus_tool_is_auto_approved``) parametrizes over every
tool the servers actually register, so a registered hook tool that is not
listed here fails that test.

**Why a name and not a ``hook_`` prefix match.** The first draft of this port
matched the prefix, reasoning that the drift guard forced it and that ~14 more
ports would otherwise each add a line. Both standing reviewers rejected that
independently, and they were right: the guard forces exactly the names that are
registered, which today is one, and a prefix grants auto-approval to every
future ``hook_``-named tool at the moment it is written -- including tools
nobody has reviewed, and including any later decision to register something
under that prefix that should have prompted. A one-way door with no one at it.
The line-per-port cost is the point, not the price: it is what makes each grant
visible in a diff, and it is what the bash script already paid 62 times.

**Every user.** The seven destructive or other-party tools in the list (``memory_delete``, ``answer_plan_delete`` and its old name
``plan_delete``, ``memory_consolidate``, ``nx_tidy``, ``tuple_in``, ``mailbox_send``) are auto-approved for everyone: the plugin
asks nothing about who the user is. A user who wants a prompt for one adds an ``ask`` or ``deny`` rule in their own Claude Code
settings, which beats a hook's allow.

**AUDIT RESIDUAL (bead nexus-q02nx.4, round 1, Moderate): hook-chain re-entry
is measured by bead nexus-q02nx.6, not resolved here.** Auto-approving
a hook tool's own name does not by itself prove a model-initiated call to one
terminates rather than re-entering the PreToolUse chain against its own
matcher; that measurement, and any consequent exclusion or short-circuit, is
that bead's job.
"""
from __future__ import annotations

from nexus._hook_runtime._io import HookResult, permission_decision

#: Explicit full tool names, no wildcards -- carried byte-for-byte from
#: auto-approve-nx-mcp.sh's case statement (T2 nexus_rdr/215-hook-contract-map
#: row 2). ``mcp__plugin_conexus_nexus__daemon_uninstall`` is deliberately
#: ABSENT: it can tear down the storage-service OS autostart unit and,
#: with ``remove_data=true``, irreversibly delete the entire nexus config
#: directory, and its own ``confirm=true`` parameter is a trivial self-gate
#: an agent satisfies with one more tool call, not a human-in-the-loop
#: check (nexus-cnzei.5, preserved from the bash script's own header
#: comment). Do not add it here without a fresh review of that reasoning.
#: ``mcp__plugin_conexus_nexus__setup`` and ``..._setup_status`` (RDR-228 Phase 2)
#: are ABSENT on purpose too: ``setup`` installs software, and the application's
#: prompt before it runs is the second check on consent after the model has asked
#: the user (CA-8). The record holds ``setup_status`` to the same prompt.
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
        "mcp__plugin_conexus_nexus__answer_plan_save",
        "mcp__plugin_conexus_nexus__answer_plan_search",
        "mcp__plugin_conexus_nexus__answer_plan_delete",
        # The old names stay for one client release (nexus-sby49.4); remove these three with the aliases.
        "mcp__plugin_conexus_nexus__plan_save",
        "mcp__plugin_conexus_nexus__plan_search",
        "mcp__plugin_conexus_nexus__plan_delete",
        "mcp__plugin_conexus_nexus__procedure_run",  # Sam 2026-11-08, nexus-qo7ie.31
        "mcp__plugin_conexus_nexus__brief",  # read-only on-demand SessionStart context (gate ruling)
        "mcp__plugin_conexus_nexus__traverse",
        "mcp__plugin_conexus_nexus__nx_answer",
        "mcp__plugin_conexus_nexus__nx_answer_report",
        "mcp__plugin_conexus_nexus__nx_tidy",
        "mcp__plugin_conexus_nexus__nx_enrich_beads",
        "mcp__plugin_conexus_nexus__nx_plan_audit",
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

#: Registered ``hook_<name>`` tools, listed one by one for the same reason the
#: 62 names above are: a grant a reviewer cannot see is a grant nobody reviewed.
#: RDR-215 registers roughly fourteen more of these across Phases 2 and 3, and
#: each one adds a line here as part of its own port. That is the cost of
#: keeping every auto-approval explicit, and it is the cost the bash script
#: already paid 62 times.
_ALLOWED_HOOK_TOOLS: frozenset[str] = frozenset(
    {
        "mcp__plugin_conexus_nexus__hook_auto_approve",
        "mcp__plugin_conexus_nexus__hook_subagent_start",
    }
)


def _is_allowed(tool_name: str) -> bool:
    return tool_name in _ALLOWED_TOOLS or tool_name in _ALLOWED_HOOK_TOOLS


def run(payload: dict | None) -> HookResult:
    """Decide whether *payload*'s ``tool_name`` is auto-approved.

    Answers only an event that is exactly ``PreToolUse`` (RDR-259 S4,
    nexus-sby49.6). A non-match, an absent event and a ``PermissionRequest``
    event all return ``HookResult()`` (silent -- no stdout at all, not an
    empty envelope); a ``PreToolUse`` match renders the envelope via
    ``_io``'s shared writer, which reproduces the bash layer's key order byte
    for byte.
    """
    data = payload or {}
    tool_name = data.get("tool_name", "")
    if not _is_allowed(tool_name):
        return HookResult()

    if data.get("hook_event_name") != "PreToolUse":
        return HookResult()
    return HookResult(
        stdout=permission_decision(
            "PreToolUse",
            "allow",
            permission_decision_reason="plugin allowlist (auto-approve)",
        )
    )
