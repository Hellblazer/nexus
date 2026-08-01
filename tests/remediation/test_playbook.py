# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-182 P1.3 (nexus-ykzbj.7): shared (topic, store_state) -> Playbook emitter.

The Playbook is the single source of truth for remediation guidance text,
consumed by BOTH the CLI gate (`_emit_chash_poison_gate` in
src/nexus/commands/daemon.py) and, in Phase 3, the MCP `forensics`/`remediate`
tools. The chash-poison topic is LOCKED to the nexus-o513u ladder-first
contract (GH #1414, 2026-07-21): the nexus-joima verification proved the
pre-rewrite "a new engine would crash-loop on boot" premise FALSE for
v0.1.48+ engines (octet-width CHECKs land NOT VALID; no boot-time VALIDATE
exists), so the playbook steers the upgrade-ladder heal FIRST and reserves
rollback for the will-not-boot class. Every expected string below pins that
contract; changing it is a deliberate contract change, not a refactor.
"""
from __future__ import annotations

import pytest

_URL = "https://github.com/Hellblazer/nexus/blob/main/docs/migration-runbook.md"

_DETAIL = "3 non-32-char chash row(s) in pgvector (worst: 'abc')."

# The ready-to-paste agent prompt (nexus-o513u ladder-first contract).
_EXPECTED_PROMPT = (
    "My conexus/nexus store has width-non-conformant chash rows in "
    "pgvector (octet_length <> 32 — legacy pre-RDR-108 ids; the GH #1414 "
    "class). The serving engine tolerates them, but they are unhealed "
    "debt the upgrade ladder should converge. "
    f"Walk me through the recovery in {_URL} section 8.1: "
    "resolve the affected legacy-id collections to their repos "
    "(nx catalog owners list) and re-index the file-backed ones "
    "(nx index repo <path> — additive, per-collection), "
    "run nx upgrade — the chash-rekey rung recomputes conformant ids "
    "from stored chunk text (store_put-only notes included), "
    "re-run nx doctor and confirm the 'Chunk chash conformance' "
    "warning has cleared, and only then let me upgrade the engine. Do NOT drop "
    "the chash length constraints."
)

# The refusal block (nexus-o513u ladder-first contract).
_EXPECTED_TERMINAL = (
    "\nRefusing to install (nexus-pnwu0 / GH #1414): this store has "
    "width-non-conformant chash rows — heal them via the upgrade ladder "
    "before swapping engine binaries.\n"
    f"  {_DETAIL}\n\n"
    "Remediate first — full recovery playbook (clickable):\n"
    f"  {_URL} §8.1\n\n"
    "Or paste this to your Claude to be walked through it:\n"
    "  ----------------------------------------------------------------\n"
    f"  {_EXPECTED_PROMPT}\n"
    "  ----------------------------------------------------------------\n\n"
    "Do NOT drop the chash length constraints to force it through — that "
    "is the exact action that caused GH #1390. Re-run with --force ONLY "
    "after you have remediated."
)

# The --force override warning (nexus-o513u ladder-first contract).
_EXPECTED_FORCE = (
    "WARNING (nexus-pnwu0 / GH #1414): --force overrides the chash-poison "
    f"gate. {_DETAIL} The rows stay unhealed debt: the chash-rekey rung's "
    "VALIDATE will keep failing until they converge, and a pre-v0.1.48 "
    "char-era engine can still crash-loop on boot. "
    "Recovery: " + _URL + " §8.1."
)


@pytest.fixture()
def chash_playbook():
    from nexus.remediation import StoreState, emit_playbook

    return emit_playbook("chash-poison", StoreState(detail=_DETAIL))


def test_agent_prompt_byte_equivalent_to_shipped_gate(chash_playbook):
    assert chash_playbook.agent_prompt() == _EXPECTED_PROMPT


def test_terminal_block_byte_equivalent_to_shipped_gate(chash_playbook):
    assert chash_playbook.terminal_block() == _EXPECTED_TERMINAL


def test_force_override_warning_byte_equivalent_to_shipped_gate(chash_playbook):
    assert chash_playbook.force_override_warning() == _EXPECTED_FORCE


def test_runbook_url_is_https_and_pinned_to_main(chash_playbook):
    assert chash_playbook.runbook_url == _URL
    assert chash_playbook.runbook_url.startswith("https://")
    assert "/blob/main/" in chash_playbook.runbook_url


def test_structured_fields_carry_the_rdr_contract(chash_playbook):
    """RDR-182 Technical Design: Playbook carries context, hard do-NOT
    constraints, ordered steps, a structured-deliverable schema, the pinned
    URL, and the environment-gone escape."""
    pb = chash_playbook
    assert pb.topic == "chash-poison"
    assert _DETAIL == pb.store_detail
    assert pb.context.startswith("My conexus/nexus store has width-non-conformant")
    assert pb.constraints and all(c for c in pb.constraints)
    assert any("Do NOT drop the chash length constraints" in c for c in pb.constraints)
    assert len(pb.steps) == 3
    # nexus-o513u: ladder-first — the steps NEVER prescribe rollback; the
    # rollback branch lives in the escape, conditioned on will-not-boot.
    assert pb.steps[0].startswith("resolve the affected legacy-id collections")
    assert all("--rollback" not in s for s in pb.steps)
    # RDR-155 P4b: the rollback tooling died with the migration machinery;
    # the will-not-boot escape now routes through the pinned release.
    assert "LAST_MIGRATION_CAPABLE" in pb.escape
    assert "will NOT BOOT" in pb.escape
    assert pb.deliverable  # structured-deliverable instruction is non-empty
    assert "gone" in pb.escape  # environment-gone escape present


def test_tool_return_contains_all_layers_and_no_terminal_chrome(chash_playbook):
    """The MCP rendering (Phase 3 consumes) must carry context, constraints,
    steps, deliverable, escape, and the clickable URL — and must NOT contain
    the CLI-only chrome (the paste-dashes box or the --force advice)."""
    text = chash_playbook.tool_return()
    assert _DETAIL in text
    assert _URL in text
    for step in chash_playbook.steps:
        assert step in text
    assert chash_playbook.deliverable in text
    assert chash_playbook.escape in text
    assert "----------------------------------------------------------------" not in text
    assert "--force" not in text


def test_describe_shape_and_step_withholding(chash_playbook):
    """(review-p3 L2) Direct contract test for the pre-consent rendering:
    every layer-2 element present, the ordered recovery steps absent (from
    the TOOL's rendering — the public runbook URL stays, by design), and the
    step-count wording matches the actual step count."""
    pb = chash_playbook
    text = pb.describe()
    assert f"[{pb.topic}]" in text
    assert pb.store_detail in text
    for c in pb.constraints:
        assert c in text
    assert pb.deliverable in text
    assert pb.escape in text
    assert pb.runbook_url in text
    assert "confirm=true" in text
    assert f"({len(pb.steps)} ordered steps)" in text
    assert "no consent has been recorded" in text.lower()
    for step in pb.steps:
        assert step not in text  # the tool's own steps are not rendered


def test_consent_scope_builder_contract():
    """(review-p3 final note) The validated scope builder: happy path plus
    both fail-loud unhappy paths (unknown verb / unregistered topic)."""
    from nexus.remediation import consent_scope

    assert consent_scope("remediate", "chash-poison") == "remediate:chash-poison"
    assert consent_scope("forensics", "chash-poison") == "forensics:chash-poison"
    with pytest.raises(ValueError, match="unknown consent verb"):
        consent_scope("execute", "chash-poison")
    with pytest.raises(ValueError, match="unknown consent topic"):
        consent_scope("remediate", "typo-topic")


def test_unknown_topic_fails_loud():
    from nexus.remediation import StoreState, emit_playbook

    with pytest.raises(KeyError) as exc:
        emit_playbook("nope-such-topic", StoreState(detail="x"))
    assert "nope-such-topic" in str(exc.value)
    assert "chash-poison" in str(exc.value)  # names the known topics


# ── nexus-4s19o: migration-legacy-ids topic (runbook §8) ─────────────────────


def test_legacy_ids_remediate_topic_registered_and_emits():
    from nexus.remediation.playbook import StoreState, emit_playbook

    pb = emit_playbook(
        "migration-legacy-ids",
        StoreState(detail="5 collections pre-gate-blocked (legacy 16-char ids)"),
    )
    assert pb.topic == "migration-legacy-ids"
    # The GH #1390 rule is a HARD constraint, and salvage-before-delete is
    # load-bearing (note-shaped text has no other copy).
    joined = " ".join(pb.constraints)
    assert "NEVER drop or weaken the chash length CHECK constraints" in joined
    # RDR-155 P4b: the salvage choreography moved to the pinned release —
    # this version's playbook is the two-hop redirect.
    assert "pinned release" in joined
    # Ordered pinned-release arc: back up -> install pin -> migrate there ->
    # verify + return.
    assert len(pb.steps) == 4
    assert "back up" in pb.steps[0]
    assert "LAST_MIGRATION_" in pb.steps[1]
    assert pb.runbook_section == "8"
    assert pb.diagnostic_sql == ()  # chroma-side classification, no SQL leg
    # Both renderings carry the store detail verbatim.
    assert "5 collections pre-gate-blocked" in pb.tool_return()
    assert "5 collections pre-gate-blocked" in pb.terminal_block()


def test_legacy_ids_forensics_topic_is_read_only_shaped():
    from nexus.remediation.playbook import StoreState, emit_forensics_playbook

    pb = emit_forensics_playbook(
        "migration-legacy-ids", StoreState(detail="probe detail"),
    )
    assert pb.topic == "migration-legacy-ids"
    assert pb.constraints[0].startswith("READ-ONLY")
    assert "store_put" in pb.constraints[0]  # mutation examples named
    assert pb.force_risk == ""  # diagnostic topics carry no force framing
    assert pb.diagnostic_sql == ()


def test_topic_registries_expose_legacy_ids_on_both_verbs():
    from nexus.remediation.playbook import forensics_topics, remediate_topics

    assert "migration-legacy-ids" in remediate_topics()
    assert "migration-legacy-ids" in forensics_topics()
