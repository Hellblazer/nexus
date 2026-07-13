# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-182 P1.3: the shared ``(topic, store_state) -> Playbook`` emitter.

Single source of truth for remediation guidance text, consumed by BOTH the
CLI gate (``nx daemon service install-binary``'s chash-poison refusal,
``src/nexus/commands/daemon.py``) and — in RDR-182 Phase 3 — the MCP
``forensics``/``remediate`` tools, whose return string is the payload channel
to a Desktop-resident agent.

A :class:`Playbook` carries the RDR-182 Technical Design contract: context,
hard READ-ONLY/do-NOT constraints, ordered recovery steps, a
structured-deliverable instruction, the full clickable https runbook URL
pinned to ``main``, and the "environment gone" escape. It renders to two
targets: :meth:`Playbook.terminal_block` (the CLI refusal, byte-locked to the
pre-hoist gate text by ``tests/remediation/test_playbook.py``) and
:meth:`Playbook.tool_return` (the MCP payload — no terminal chrome, no
``--force`` advice: an agent cannot type ``--force``, a human at the CLI can).

This module is dependency-light BY DESIGN (stdlib only — no click, no
structlog): the MCP server, the CLI, and tests all import it without pulling
command-layer weight.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: Full clickable https URL, pinned to ``main`` (releases promote develop ->
#: main, so an operator on a released build finds the sections on main).
#: Hoisted from ``commands/daemon.py`` (nexus-ykzbj.7); daemon.py now imports
#: it from here.
MIGRATION_RUNBOOK_URL = (
    "https://github.com/Hellblazer/nexus/blob/main/docs/migration-runbook.md"
)

_PASTE_RULE = "  " + "-" * 64


@dataclass(frozen=True)
class StoreState:
    """What the read-only probe found — the dynamic half of a playbook.

    ``detail`` is the probe's finding text (e.g. the chash-conformance
    failure detail from ``nexus.health._check_migration_state``), rendered
    verbatim into the playbook so the operator/agent sees the store's ACTUAL
    state, not a generic description.
    """

    detail: str


@dataclass(frozen=True)
class Playbook:
    """A self-contained remediation playbook (RDR-182 Technical Design).

    Static per-topic fields compose with the probe's :class:`StoreState` at
    emit time. The renderers are the ONLY places layout lives — consumers
    never assemble guidance text themselves, so the CLI and MCP surfaces
    cannot drift apart.
    """

    topic: str
    #: One-sentence first-person problem statement (leads the agent prompt).
    context: str
    #: Hard do-NOTs / READ-ONLY rules. constraints[0] is the PRIMARY one and
    #: closes the agent prompt.
    constraints: tuple[str, ...]
    #: Ordered recovery steps, joined into the agent prompt and enumerated in
    #: the tool rendering.
    steps: tuple[str, ...]
    #: What the enlisted agent should report back (structured deliverable).
    deliverable: str
    #: The "environment gone" escape hatch — when the recovery's inputs no
    #: longer exist, stop rather than improvise.
    escape: str
    #: The end-state the steps build to ("let me upgrade the engine").
    goal: str
    #: Incident anchor rendered in warnings (bead / GH refs).
    incident_ref: str
    #: Topic-specific refusal lead line (terminal rendering).
    refusal_lead: str
    #: Topic-specific closing warning (terminal rendering; may reference
    #: --force, which only exists at the CLI).
    closing_warning: str
    #: Risk sentence rendered in the --force override warning.
    force_risk: str
    #: Runbook section number, e.g. "8.1" (rendered as "section 8.1" in the
    #: agent prompt and "§8.1" in terminal/tool text).
    runbook_section: str
    #: What the probe actually found (dynamic, from StoreState).
    store_detail: str
    runbook_url: str = field(default=MIGRATION_RUNBOOK_URL)
    #: SQL a DIAGNOSTIC (forensics) topic wants the agent to run. Linted
    #: read-only + metadata-scoped at emission (RDR-182 P2.2, nexus-ykzbj.9)
    #: — a mutating diagnostic is impossible-by-construction. Empty for
    #: topics whose guidance is CLI-command-based (like chash-poison).
    diagnostic_sql: tuple[str, ...] = ()

    def agent_prompt(self) -> str:
        """The ready-to-paste prompt an operator hands their own agent."""
        return (
            f"{self.context} Walk me through the recovery in "
            f"{self.runbook_url} section {self.runbook_section}: "
            f"{', '.join(self.steps)}, and only then {self.goal}. "
            f"{self.constraints[0]}"
        )

    def terminal_block(self) -> str:
        """The CLI refusal body (byte-locked to the pre-hoist gate text)."""
        return (
            f"\n{self.refusal_lead}\n"
            f"  {self.store_detail}\n\n"
            "Remediate first — full recovery playbook (clickable):\n"
            f"  {self.runbook_url} §{self.runbook_section}\n\n"
            "Or paste this to your Claude to be walked through it:\n"
            f"{_PASTE_RULE}\n"
            f"  {self.agent_prompt()}\n"
            f"{_PASTE_RULE}\n\n"
            f"{self.closing_warning}"
        )

    def force_override_warning(self) -> str:
        """The one-line warning when the operator overrides the gate."""
        return (
            f"WARNING ({self.incident_ref}): --force overrides the "
            f"{self.topic} gate. {self.store_detail} {self.force_risk} "
            f"Recovery: {self.runbook_url} §{self.runbook_section}."
        )

    def tool_return(self) -> str:
        """The MCP/Desktop rendering — the tool RETURN STRING is the payload.

        Carries every contract layer (context, constraints, ordered steps,
        deliverable, escape, clickable URL) and NONE of the CLI chrome: no
        paste-box (the agent already has the text) and no ``--force`` advice
        (an autonomously-invocable surface must never be handed the override
        a human consent gesture is supposed to guard).
        """
        constraint_lines = "\n".join(f"- {c}" for c in self.constraints)
        step_lines = "\n".join(
            f"{i}. {s}" for i, s in enumerate(self.steps, start=1)
        )
        # diagnostic_sql renders ONLY here (review-foundations Medium: linted
        # SQL the agent never sees is a silent gap) — the MCP surface is the
        # one an enlisted agent reads; the CLI renderings stay prose-only.
        sql_block = ""
        if self.diagnostic_sql:
            sql_lines = "\n".join(self.diagnostic_sql)
            sql_block = (
                "\n\nRead-only diagnostic SQL (lint-verified; run via the "
                f"nexus_diag path only):\n{sql_lines}"
            )
        return (
            f"[{self.topic}] {self.context}\n\n"
            f"Store state: {self.store_detail}\n\n"
            f"HARD CONSTRAINTS (read-only posture):\n{constraint_lines}\n\n"
            f"Recovery steps (in order):\n{step_lines}"
            f"{sql_block}\n\n"
            f"Deliverable: {self.deliverable}\n\n"
            f"If the environment is gone: {self.escape}\n\n"
            f"Full runbook (clickable): {self.runbook_url} "
            f"§{self.runbook_section}"
        )


def _chash_poison(store_state: StoreState) -> Playbook:
    """GH #1390 / nexus-pnwu0: non-32-char chash rows poison the pgvector
    target — a new engine crash-loops on Liquibase VALIDATE at boot."""
    return Playbook(
        topic="chash-poison",
        context=(
            "My conexus/nexus store has non-32-char chash rows in pgvector "
            "(GH #1390 / nexus-pnwu0) and a new engine would crash-loop on "
            "boot."
        ),
        constraints=(
            "Do NOT drop the chash length constraints.",
            "Diagnostics are read-only — no DML against pgvector outside the "
            "documented rollback command.",
        ),
        steps=(
            "roll back the poisoned pgvector target (nx storage migrate "
            "vectors --rollback)",
            "re-index the affected legacy-id collections from source",
            "re-run nx guided-upgrade",
        ),
        deliverable=(
            "Report back: the rollback command's verdict line, the re-indexed "
            "collection names with their row counts, and the final "
            "'Migration VERIFIED and unlocked' line from nx guided-upgrade."
        ),
        escape=(
            "If the source content for the affected collections is gone "
            "(nothing left to re-index from), STOP — do not improvise DML "
            "against pgvector; runbook §8 covers the rebuild-from-source "
            "options and when rollback alone is the terminal state."
        ),
        goal="let me upgrade the engine",
        incident_ref="nexus-pnwu0",
        refusal_lead=(
            "Refusing to install (nexus-pnwu0 / GH #1390): booting a new "
            "engine on this store would crash-loop."
        ),
        closing_warning=(
            "Do NOT drop the chash length constraints to force it through — "
            "that is the exact action that caused GH #1390. Re-run with "
            "--force ONLY after you have remediated."
        ),
        force_risk=(
            "The new engine may crash-loop on boot unless you have already "
            "remediated."
        ),
        runbook_section="8.1",
        store_detail=store_state.detail,
    )


#: topic -> builder. Phase 3 adds the remaining upgrade-edge topics here.
_TOPICS = {
    "chash-poison": _chash_poison,
}


def emit_playbook(topic: str, store_state: StoreState) -> Playbook:
    """Build the :class:`Playbook` for *topic* against *store_state*.

    Unknown topics fail LOUD with the known-topic list — a typo'd topic
    silently emitting nothing would defeat the gate it feeds.
    """
    try:
        builder = _TOPICS[topic]
    except KeyError:
        raise KeyError(
            f"unknown playbook topic {topic!r} — known topics: "
            f"{sorted(_TOPICS)}"
        ) from None
    playbook = builder(store_state)
    if playbook.diagnostic_sql:
        # RDR-182 P2.2 (nexus-ykzbj.9): pre-emission read-only lint — no
        # playbook carrying mutating or content-reading diagnostic SQL can
        # ever be emitted, on any surface.
        from nexus.remediation.sql_lint import assert_read_only_diagnostics  # noqa: PLC0415 — avoid import cycle at module load

        assert_read_only_diagnostics(playbook.diagnostic_sql)
    return playbook
