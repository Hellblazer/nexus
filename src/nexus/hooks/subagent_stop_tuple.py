# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The RDR-205 ledger REPORT projection, on the command tier (bead nexus-egm7p).

Sibling of :mod:`nexus.hooks.subagent_start_tuple` -- see that module's
docstring for why this calls :func:`~nexus.hooks.tuple_ledger_project.project`
directly and synchronously rather than reusing
:func:`nexus.hooks.tuple_projection.run_stop`'s daemon-thread detachment,
and why the command tier is the fix for the same MCP-disconnect hazard
nexus-5l8i8 fixed for the RDR-184 ledger's writers.

**This move also closes a SEPARATE, independently-confirmed defect for
free: the ``verify`` dim's transport.** The ``mcp_tool`` registration for
``hook_subagent_stop_tuple`` (``nexus.mcp.hooks.HOOK_TOOLS``) declares only
three ``fields``: ``session_id``, ``agent_id``, ``agent_type`` -- there was
never an ``agent_transcript_path`` in that tool's input, so every REPORT row
projected through the mcp_tool tier reached
:func:`~nexus.hooks.tuple_ledger_project.project` with an empty transcript
path, and ``_extract_verify_dims("")`` always returns
``{"verify": "absent"}``. Measured live: zero ``verify=present`` rows out
of 15,595 REPORT rows across every ledger this analysis sampled, including
the window (2026-09-14 through 2026-09-19) when the projection ran as a
plugin-resident bash script with the FULL SubagentStop payload on stdin --
so the field was never wired through at any point in this hook's history,
mcp_tool or otherwise.

A command-tier verb receives the ENTIRE stdin payload Claude Code sends for
the event -- there is no per-field allowlist to fall short of. SubagentStop's
real payload carries ``agent_transcript_path`` (confirmed by this hook's own
sibling, :mod:`nexus.hooks.subagent_stop`, which already reads that exact
key), so moving to the command tier delivers it to ``project()`` with no
further change needed here.

**A second, genuinely independent defect was found verifying this end to
end against real transcripts (not just the unit fixtures), per the task's
own instruction not to trust extraction until it is proven against a real
report.** :func:`nexus.hooks.tuple_ledger_project._last_send_message_text`'s
field-name map read ``{"SendMessage": "content", "SubagentHandback":
"message"}`` -- but a live ``SendMessage`` tool_use's own ``input`` carries
the report text under ``"message"``, never ``"content"``; ``"content"`` is
a separate, TRUNCATED preview field the harness also stores alongside it
(confirmed against several real transcripts: ~50 characters, always ending
in an ellipsis). So even with the transcript path now reaching ``project()``,
extraction would still have missed nearly every VERIFY block reported via
SendMessage -- the more common report shape -- because a VERIFY line
sitting past character ~50 of a real report was silently truncated out of
the text this hook searched. Fixed in that module directly (see its own
docstring); this module carries no code for it, but the fix is why THIS
move actually reaches ``verify=present`` end to end rather than trading one
missing-field bug for another.

**ACCEPTED RESIDUAL, not reopened by this bead (review round, nexus-egm7p):
``claude -p`` kills a still-running ``"async": true`` command hook at
session teardown, no grace** -- see
:mod:`nexus.hooks.subagent_start_tuple`'s own docstring for the full
citation (RDR-205, ``docs/rdr/rdr-205-linda-tuple-space-over-postgres.md``
line 231 and lines 1136-1137) and why this is a restored, already-researched
shape rather than a newly-introduced one. It matters MORE on this side
than on the start side: a SubagentStop firing as the last act of a
short-lived ``claude -p`` invocation is an ordinary shape for this
project's own automation (CCR, GitHub Actions, owned-mode subprocess
dispatch -- see this repo's ``AGENTS.md``), not an edge case, so this
REPORT projection is the one more likely to race the teardown kill. The
RDR-184 ``.expectations`` TSV ledger -- written synchronously by a
different hook (``nexus.hooks.subagent_stop``) on the same event -- stays
the authoritative record of whether an agent reported; this projection is
a best-effort secondary view for the RDR-205 tuple space, never the thing
anything correctness-sensitive (including ``scripts/check_agent_verify_claims.py``,
whose own docstring names this) reads as ground truth for "did the agent
report at all." Nothing in ``hooks.json`` bounds this entry's own runtime
with a manifest ``"timeout"`` -- see the sibling module's docstring for
why the key is absent rather than vestigial.
"""
from __future__ import annotations

from nexus._hook_runtime._io import HookResult
from nexus.hooks import tuple_ledger_project

__all__ = ["run"]


def run(payload: dict | None) -> HookResult:
    """Project this subagent's REPORT tuple, synchronously, in this process.

    *payload* is the full SubagentStop stdin payload -- carries
    ``agent_transcript_path`` straight through to
    :func:`~nexus.hooks.tuple_ledger_project.project`'s own VERIFY-dims
    extraction. Always returns a silent, exit-0 result; see
    :mod:`nexus.hooks.subagent_start_tuple` for why.
    """
    tuple_ledger_project.project("report", payload)
    return HookResult()
