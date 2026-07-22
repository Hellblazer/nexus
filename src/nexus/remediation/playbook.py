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

from nexus.db.chash_tables import CHASH_BEARING_TABLES as _CHASH_TABLES
from nexus.db.chash_tables import chash_conformance_statements as _chash_statements
from nexus.db.chash_tables import debt_chash_conformance_statements as _debt_statements

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

    def describe(
        self,
        consent_hint: str = (
            "re-call this tool with confirm=true (the grant is audit-recorded)"
        ),
    ) -> str:
        """The PRE-CONSENT rendering (RDR-182 remediate layer 2): states what
        consent would authorize — context, hard do-NOTs, deliverable, escape,
        runbook pointer — without rendering the ordered recovery steps (the
        TOOL's guided, store-state-aware playbook releases only on the
        consented ``confirm=true`` call, audit-recorded).

        Threat-model honesty (review-p3 H1): this withholds the tool's OWN
        rendering, not the knowledge — the runbook URL included here is
        public documentation (Gap 2) containing equivalent recovery steps.
        The consent layer makes the safe guided path the audited path; it is
        not an information-access control.
        """
        constraint_lines = "\n".join(f"- {c}" for c in self.constraints)
        return (
            f"[{self.topic}] {self.context}\n\n"
            f"Store state: {self.store_detail}\n\n"
            f"This is the DESCRIBE stage — no consent has been recorded and "
            f"no recovery guidance has been released.\n\n"
            f"Consenting authorizes a guided recovery playbook "
            f"({len(self.steps)} ordered steps) that your agent executes "
            f"locally with your credentials, bound by these HARD "
            f"CONSTRAINTS:\n{constraint_lines}\n\n"
            f"Deliverable you should expect back: {self.deliverable}\n\n"
            f"If the environment is gone: {self.escape}\n\n"
            f"Full runbook (clickable): {self.runbook_url} "
            f"§{self.runbook_section}\n\n"
            f"To consent and receive the recovery playbook, {consent_hint}."
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
    """GH #1414 / nexus-pnwu0 class: width-non-conformant chash rows in the
    pgvector store — unhealed upgrade-ladder debt.

    nexus-o513u ladder-first rewrite (2026-07-21). The pre-rewrite premise
    ("a new engine would crash-loop on Liquibase VALIDATE at boot") was
    DISPROVEN for v0.1.48+ engines by the nexus-joima verification: the
    RDR-180 octet-width CHECKs land NOT VALID and are VALIDATEd by the
    client chash-rekey rung post-heal — no boot-time VALIDATE exists
    (rdr180-11's header says so explicitly). The ladder heals the rows in
    place; rollback is reserved for the will-not-boot class (pre-v0.1.48
    char-era engines, the closed GH #1390 shape) via the escape branch.
    Steering rollback-first on a serving store was observed live steering
    an agent toward reverting a healthy pgvector migration (GH #1414)."""
    return Playbook(
        topic="chash-poison",
        context=(
            "My conexus/nexus store has width-non-conformant chash rows in "
            "pgvector (octet_length <> 32 — legacy pre-RDR-108 ids; the "
            "GH #1414 class). The serving engine tolerates them, but they "
            "are unhealed debt the upgrade ladder should converge."
        ),
        constraints=(
            "Do NOT drop the chash length constraints.",
            "Do NOT roll back a serving store — rollback (nx storage "
            "migrate vectors --rollback) is reserved for the will-not-boot "
            "class in the escape below.",
            "Diagnostics are read-only — no DML against pgvector; the only "
            "mutating actions are the documented ladder commands in the "
            "steps.",
        ),
        steps=(
            "resolve the affected legacy-id collections to their repos "
            "(nx catalog owners list) and re-index the file-backed ones "
            "(nx index repo <path> — additive, per-collection)",
            "run nx upgrade — the substrate-etl rung converges the "
            "re-indexed collections and the chash-rekey rung recomputes "
            "conformant ids from stored chunk text for the rest "
            "(store_put-only notes included)",
            "re-run nx doctor and confirm the 'Chunk chash conformance' "
            "warning has cleared",
        ),
        deliverable=(
            "Report back: the re-indexed collection names with their row "
            "counts, nx upgrade's rung verdict lines (substrate-etl and "
            "chash-rekey), and the final nx doctor 'Chunk chash "
            "conformance' line showing the warning cleared."
        ),
        escape=(
            "If the service itself will NOT BOOT (Liquibase crash-loop at "
            "startup — the closed GH #1390 shape, seen on pre-v0.1.48 "
            "char-era engines), the ladder cannot run: roll back the "
            "poisoned target (nx storage migrate vectors --rollback), "
            "re-index, and re-run nx guided-upgrade per runbook §8.1. If "
            "a file-backed collection's source content is gone, skip its "
            "re-index — the chash-rekey rung recomputes ids from stored "
            "text; STOP and report only if the ladder itself refuses."
        ),
        goal="let me upgrade the engine",
        incident_ref="nexus-pnwu0 / GH #1414",
        refusal_lead=(
            "Refusing to install (nexus-pnwu0 / GH #1414): this store has "
            "width-non-conformant chash rows — heal them via the upgrade "
            "ladder before swapping engine binaries."
        ),
        closing_warning=(
            "Do NOT drop the chash length constraints to force it through — "
            "that is the exact action that caused GH #1390. Re-run with "
            "--force ONLY after you have remediated."
        ),
        force_risk=(
            "The rows stay unhealed debt: the chash-rekey rung's VALIDATE "
            "will keep failing until they converge, and a pre-v0.1.48 "
            "char-era engine can still crash-loop on boot."
        ),
        runbook_section="8.1",
        store_detail=store_state.detail,
    )


# The chash-bearing table set (_CHASH_TABLES) is imported at the top from the
# shared nexus.db.chash_tables — the SINGLE source of truth shared with the
# nx doctor / install-binary health probe (nexus-vounk), so the operator gate
# and this agent-facing topic cannot drift to checking different tables.


def _chash_poison_forensics(store_state: StoreState) -> Playbook:
    """The FORENSICS (read-only investigation) playbook for the GH #1390
    class — the first diagnostic-shaped topic (nexus-ykzbj.10): carries
    lint-verified aggregate SQL, mutates nothing, and exists to decide
    whether ``remediate:chash-poison`` is needed at all."""
    return Playbook(
        topic="chash-poison",
        context=(
            "My conexus/nexus store may hold width-non-conformant chash "
            "rows in pgvector (the GH #1414 / nexus-pnwu0 class) — help me "
            "diagnose the blast radius READ-ONLY before deciding on any "
            "remediation."
        ),
        constraints=(
            "READ-ONLY investigation: run nothing that mutates — no DML, no "
            "DDL, no constraint changes.",
            "Do NOT begin remediation from this playbook; that is the "
            "separately-consented remediate path.",
            "Do NOT drop or weaken the chash length constraints.",
        ),
        steps=(
            "run the diagnostic SQL below (each statement is lint-verified "
            "read-only; the nexus_diag path executes them in a read-only "
            "session)",
            "interpret: any non-zero count in the first four statements "
            "(chunks_384/768/1024, catalog_document_chunks) means width-"
            "non-conformant rows exist in that table — unhealed upgrade-"
            "ladder debt. v0.1.48+ engines tolerate these rows at boot "
            "(the octet-width CHECKs are NOT VALID until the chash-rekey "
            "rung heals and VALIDATEs them — nexus-joima); the remedy is "
            "the ladder (nx upgrade), not rollback. Only a pre-v0.1.48 "
            "char-era engine can still crash-loop on a boot VALIDATE (the "
            "closed GH #1390 shape). (The chash_index statement was "
            "retired by RDR-187 — the router table is dropped as of "
            "engine v0.1.51.)",
            "interpret: the next three statements (topic_assignments, "
            "frecency, relevance_log) are LEGACY DEBT — non-gating "
            "(no CHECK constraint exists there), but non-zero counts mean "
            "rows silently missing their chunk-table joins; an empty (NULL) "
            "result means the deployed view predates these entries — report "
            "it as unknown, not clean (nexus-z5j0t)",
            "check constraint state: unvalidated *_chash_*_check constraints indicate "
            "an earlier forced upgrade",
        ),
        deliverable=(
            "Report back: the per-table non-conformant counts, the "
            "chash width-constraint validation states, and a recommendation — "
            "clean (no action) or proceed to remediate:chash-poison."
        ),
        escape=(
            "If the Postgres cluster is down, the diagnostic credentials are "
            "gone (pre-P2.1 install), or the nexus.diag_chash_conformance "
            "counts view is absent (pre-A6 engine), STOP and report which "
            "prerequisite is missing — re-running `nx init --service` "
            "backfills the diagnostic role, credentials, and view."
        ),
        goal="decide whether remediation is needed",
        incident_ref="nexus-pnwu0",
        refusal_lead=(
            "Diagnostic playbook for the chash-poison class (read-only)."
        ),
        closing_warning=(
            "This playbook diagnoses only — remediation is the separately-"
            "consented remediate path."
        ),
        force_risk="",
        runbook_section="8.1",
        store_detail=store_state.detail,
        # Amendment A6 (nexus-9bufb): the counts VIEW, not direct table
        # counts — the diagnostic role's table SELECT is revoked in the view
        # era, and the view emits counts by construction. Same emitter the
        # health probe uses (nexus.db.chash_tables), so the two surfaces
        # cannot drift.
        diagnostic_sql=_chash_statements() + _debt_statements() + (
            "SELECT conname, convalidated FROM pg_constraint "
            # critic-180-foundation finding 3: the historical LIKE 'chk_%'
            # matched ZERO constraints (real names end in _check) — a dead
            # diagnostic since inception. Match both eras' real names.
            "WHERE conname LIKE '%\\_chash\\_%\\_check'",
        ),
    )


def _migration_legacy_ids(store_state: StoreState) -> Playbook:
    """nexus-4s19o / GH #1390-adjacent (nexus-sot7v): pre-RDR-108 Chroma
    collections carry 16/18-char chunk ids; the migration pre-gate blocks
    them LOUDLY before any write. Recovery = runbook §8: re-index
    file-backed collections from source, re-put note-shaped content so
    store_put recomputes the canonical chash, then re-run the migration."""
    return Playbook(
        topic="migration-legacy-ids",
        context=(
            "My conexus/nexus Chroma store has pre-RDR-108 collections with "
            "legacy 16/18-char chunk ids; nx migrate-to-service pre-gate-"
            "blocks them (nexus-sot7v / GH #1390 class) and I need to "
            "remediate so the migration can complete."
        ),
        constraints=(
            "NEVER drop or weaken the chash length CHECK constraints to "
            "force upserts through — that exact action caused GH #1390 "
            "(silently corrupted store, crash-looping engine).",
            "Copy, never move: take a backup of the Chroma source directory "
            "BEFORE touching any collection, and verify the salvage "
            "artifact exists and is non-empty BEFORE any delete.",
            "Note-shaped knowledge__ content (no source file) exists ONLY "
            "in Chroma — salvage it first; after RDR-155 P4b removes the "
            "Chroma read path it is unrecoverable.",
        ),
        steps=(
            "back up the Chroma source dir (copy-not-move), then run "
            "nx migrate-to-service --dry-run and list every collection the "
            "pre-gate classifies unsupported with the legacy-id diagnostic",
            "classify each blocked collection: file-backed (code__/docs__/"
            "rdr__, or knowledge__ indexed from a PDF/file with a "
            "source_uri) vs note-shaped (knowledge__ written via store_put "
            "with no source_uri) vs derived (taxonomy centroids — "
            "regenerated later, nothing to salvage)",
            "salvage every note-shaped collection NOW: read each note's "
            "text out (nx store get, or the Chroma documents field via a "
            "direct chromadb.PersistentClient — CLI verbs route to the "
            "service in service mode) into a local JSON artifact of "
            "(documents + metadatas); verify it is non-empty",
            "remove the blocked collections from the Chroma source (the "
            "backup from step 1 is the archive), and re-run "
            "nx migrate-to-service / nx guided-upgrade to completion",
            "rebuild after the migration VERIFIES: nx index repo (and "
            "nx index pdf) for the file-backed set, store_put each salvaged "
            "note (store_put recomputes the canonical 32-char chash from "
            "the text), then nx taxonomy discover",
        ),
        deliverable=(
            "Report back: the blocked-collection list with its per-"
            "collection classification, the salvage artifact path and note "
            "count, the final 'Migration VERIFIED and unlocked' line, and "
            "the rebuild commands run with their row counts."
        ),
        escape=(
            "If a note-shaped collection's text cannot be read out (Chroma "
            "dir corrupt or already deleted), STOP before removing anything "
            "— report which collections are salvageable and which are not; "
            "runbook §8 covers when rebuild-from-source is the only path."
        ),
        goal="clear the pre-gate block and complete the migration",
        incident_ref="nexus-sot7v",
        refusal_lead=(
            "Migration pre-gate block (nexus-sot7v / GH #1390 class): "
            "legacy 16/18-char chunk ids cannot migrate."
        ),
        closing_warning=(
            "Do NOT drop the chash length constraints to force it through — "
            "that is the exact action that caused GH #1390. Salvage note-"
            "shaped content BEFORE any delete."
        ),
        force_risk=(
            "Forcing past the pre-gate migrates nothing for the blocked "
            "collections and strands their content in Chroma."
        ),
        runbook_section="8",
        store_detail=store_state.detail,
    )


def _migration_legacy_ids_forensics(store_state: StoreState) -> Playbook:
    """READ-ONLY classification for the legacy-id class: enumerate what is
    blocked and what kind of content each blocked collection holds, without
    touching the store — decides whether remediate:migration-legacy-ids is
    needed and how much salvage work it implies."""
    return Playbook(
        topic="migration-legacy-ids",
        context=(
            "My conexus/nexus migration may be pre-gate-blocked on pre-"
            "RDR-108 legacy chunk ids — help me classify the blast radius "
            "READ-ONLY before deciding on remediation."
        ),
        constraints=(
            "READ-ONLY investigation: run nothing that mutates — no "
            "collection deletes, no re-index, no store_put.",
            "Do NOT begin remediation from this playbook; that is the "
            "separately-consented remediate path.",
            "Do NOT drop or weaken the chash length constraints.",
        ),
        steps=(
            "run nx migrate-to-service --dry-run and collect every "
            "collection the pre-gate classifies unsupported with the "
            "legacy-id diagnostic",
            "for each, record its name-derived kind (code__/docs__/rdr__ = "
            "file-backed; knowledge__ = check for source_uri metadata to "
            "split file-backed from note-shaped; taxonomy centroids = "
            "derived) and its row count",
            "interpret: note-shaped rows are the ONLY copy of their text — "
            "their count is the salvage workload; file-backed rows rebuild "
            "from source at zero content risk",
        ),
        deliverable=(
            "Report back: the blocked-collection list with classification "
            "and row counts, the note-shaped salvage workload, and a "
            "recommendation — clean (nothing blocked) or proceed to "
            "remediate:migration-legacy-ids."
        ),
        escape=(
            "If the Chroma source directory is gone entirely, STOP — "
            "nothing is salvageable and the remediate path does not apply; "
            "report that state."
        ),
        goal="decide whether legacy-id remediation is needed",
        incident_ref="nexus-sot7v",
        refusal_lead=(
            "Diagnostic playbook for the legacy-chunk-id class (read-only)."
        ),
        closing_warning=(
            "This playbook diagnoses only — remediation is the separately-"
            "consented remediate path."
        ),
        force_risk="",
        runbook_section="8",
        store_detail=store_state.detail,
    )


#: verb-shaped registries (RDR-182: forensics diagnoses, remediate recovers).
#: The REMEDIATE registry keeps the original name/shape — daemon.py's gate
#: and emit_playbook()'s public signature predate the split.
_TOPICS = {
    "chash-poison": _chash_poison,
    "migration-legacy-ids": _migration_legacy_ids,
}
_FORENSICS_TOPICS = {
    "chash-poison": _chash_poison_forensics,
    "migration-legacy-ids": _migration_legacy_ids_forensics,
}


def _emit(registry: dict, kind: str, topic: str, store_state: StoreState) -> Playbook:
    try:
        builder = registry[topic]
    except KeyError:
        raise KeyError(
            f"unknown {kind} playbook topic {topic!r} — known topics: "
            f"{sorted(registry)}"
        ) from None
    playbook = builder(store_state)
    if playbook.diagnostic_sql:
        # RDR-182 P2.2 (nexus-ykzbj.9): pre-emission read-only lint — no
        # playbook carrying mutating or content-reading diagnostic SQL can
        # ever be emitted, on any surface.
        from nexus.remediation.sql_lint import assert_read_only_diagnostics  # noqa: PLC0415 — avoid import cycle at module load

        assert_read_only_diagnostics(playbook.diagnostic_sql)
    return playbook


def emit_playbook(topic: str, store_state: StoreState) -> Playbook:
    """Build the REMEDIATE :class:`Playbook` for *topic* (recovery guidance).

    Unknown topics fail LOUD with the known-topic list — a typo'd topic
    silently emitting nothing would defeat the gate it feeds.
    """
    return _emit(_TOPICS, "remediate", topic, store_state)


def emit_forensics_playbook(topic: str, store_state: StoreState) -> Playbook:
    """Build the FORENSICS (read-only diagnostic) :class:`Playbook` for
    *topic*. Same loud-unknown and pre-emission-lint semantics as
    :func:`emit_playbook`; a distinct registry because the two verbs carry
    different content for the same subject (diagnose vs recover)."""
    return _emit(_FORENSICS_TOPICS, "forensics", topic, store_state)


def forensics_topics() -> tuple[str, ...]:
    """The registered forensics topic names (membership checks — callers
    branch on this instead of catching ``KeyError`` from the emitter, which
    would also swallow a builder bug)."""
    return tuple(sorted(_FORENSICS_TOPICS))


def remediate_topics() -> tuple[str, ...]:
    """The registered remediate topic names (same membership-check contract
    as :func:`forensics_topics`)."""
    return tuple(sorted(_TOPICS))


#: The two consent-audited verbs. Locked here (not free strings at call
#: sites) so audit-scope strings cannot fragment on typos (nexus-ykzbj.15
#: builder note; first enforced by P3.2).
_CONSENT_VERBS = ("forensics", "remediate")

#: Audit scope for the DURABLE flag itself (grants AND revokes of
#: ``claude_assisted_remediation.enabled`` via ``nx config set`` — the
#: revocation-write obligation, nexus-ykzbj.15). Distinct from the
#: per-invocation ``<verb>:<topic>`` scopes: the flag is not topic-shaped.
FLAG_CONSENT_SCOPE = "flag:claude_assisted_remediation"


def consent_scope(verb: str, topic: str) -> str:
    """The canonical consent-audit scope string: ``<verb>:<topic>``.

    Fail-loud on an unknown verb or unregistered topic — a typo'd scope
    silently fragmenting the ``claude_assisted_remediation_consents`` audit
    trail is exactly what a free-form f-string at each call site invites.
    """
    if verb not in _CONSENT_VERBS:
        raise ValueError(
            f"unknown consent verb {verb!r} — known: {list(_CONSENT_VERBS)}"
        )
    known = set(_TOPICS) | set(_FORENSICS_TOPICS)
    if topic not in known:
        raise ValueError(
            f"unknown consent topic {topic!r} — known: {sorted(known)}"
        )
    return f"{verb}:{topic}"
