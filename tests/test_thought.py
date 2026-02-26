# SPDX-License-Identifier: AGPL-3.0-or-later
"""Comprehensive tests for nx thought — session-scoped sequential thinking chains.

Five test categories × five tests each = 25 tests verifying behavioural
equivalence with the sequential-thinking MCP server plus nx-specific guarantees.

MCP server reference (from lib.ts processThought):
  - Appends thought to thoughtHistory[]
  - Auto-adjusts totalThoughts when thoughtNumber > totalThoughts
  - Tracks branches{} by branchId
  - Returns: thoughtNumber, totalThoughts, nextThoughtNeeded, branches[], thoughtHistoryLength
  - nx thought additionally returns the full accumulated thought text (MCP does not)
"""

import os
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.commands.thought import (
    _extract_branches,
    _parse_thoughts,
    thought_group,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def db(tmp_path):
    """Isolated T2 database with fixed repo name and session GID."""
    db_path = tmp_path / "memory.db"
    with (
        patch("nexus.commands.thought.default_db_path", return_value=db_path),
        patch("nexus.commands.thought._repo_name", return_value="testrepo"),
        patch("os.getsid", return_value=42),
    ):
        yield db_path


def add(runner, *thoughts, chain=None):
    """Helper: invoke `nx thought add` for each thought string."""
    results = []
    for t in thoughts:
        args = ["add", t]
        if chain:
            args += ["--chain", chain]
        results.append(runner.invoke(thought_group, args))
    return results[-1] if results else None


# ── Category 1: Accumulation semantics ────────────────────────────────────────
# Every add returns the full accumulated chain — the core MCP equivalence property.

def test_acc_single_thought_present_in_output(runner, db):
    result = add(runner, "**Thought 1 of ~4**\nFrame: problem\nnextThoughtNeeded: true")
    assert result.exit_code == 0
    assert "**Thought 1 of ~4**" in result.output
    assert "Frame: problem" in result.output


def test_acc_second_add_includes_first_thought(runner, db):
    add(runner, "**Thought 1 of ~3**\nA\nnextThoughtNeeded: true")
    result = add(runner, "**Thought 2 of ~3**\nB\nnextThoughtNeeded: true")
    assert "**Thought 1 of ~3**" in result.output
    assert "**Thought 2 of ~3**" in result.output


def test_acc_all_prior_thoughts_present_after_many_adds(runner, db):
    """Core compaction-resilience property: full chain always in tool result."""
    for i in range(1, 6):
        next_needed = "true" if i < 5 else "false"
        add(runner, f"**Thought {i} of ~5**\nContent {i}\nnextThoughtNeeded: {next_needed}")
    result = add(runner, "**Thought 5 of ~5**\nFinal\nnextThoughtNeeded: false")
    for i in range(1, 6):
        assert f"**Thought {i} of ~5**" in result.output


def test_acc_thoughts_separated_by_blank_line(runner, db):
    add(runner, "**Thought 1 of ~2**\nFirst\nnextThoughtNeeded: true")
    result = add(runner, "**Thought 2 of ~2**\nSecond\nnextThoughtNeeded: false")
    # Blank line separator between thoughts
    assert "\n\n" in result.output


def test_acc_show_returns_same_chain_as_add(runner, db):
    add(runner, "**Thought 1 of ~2**\nA\nnextThoughtNeeded: true")
    add_result = add(runner, "**Thought 2 of ~2**\nB\nnextThoughtNeeded: false")
    show_result = runner.invoke(thought_group, ["show"])
    # Both should contain all thoughts
    assert "**Thought 1 of ~2**" in show_result.output
    assert "**Thought 2 of ~2**" in show_result.output
    assert show_result.exit_code == 0


# ── Category 2: MCP semantic equivalence ──────────────────────────────────────
# Output fields must match the MCP server's processThought response schema.

def test_mcp_thoughtHistoryLength_increments(runner, db):
    for i in range(1, 4):
        result = add(runner, f"**Thought {i} of ~5**\nContent\nnextThoughtNeeded: true")
    assert "thoughtHistoryLength: 3" in result.output


def test_mcp_totalThoughts_auto_adjusts_when_number_exceeds_estimate(runner, db):
    """MCP server: if thoughtNumber > totalThoughts, set totalThoughts = thoughtNumber."""
    add(runner, "**Thought 1 of ~2**\nA\nnextThoughtNeeded: true")
    add(runner, "**Thought 2 of ~2**\nB\nnextThoughtNeeded: true")
    # Thought 3 exceeds initial estimate of 2
    result = add(runner, "**Thought 3 of ~2**\nC — more needed\nnextThoughtNeeded: true")
    assert result.exit_code == 0
    # totalThoughts should be auto-adjusted to at least 3
    assert "totalThoughts: 3" in result.output


def test_mcp_nextThoughtNeeded_false_reported_in_metadata(runner, db):
    result = add(runner, "**Thought 1 of ~1**\nConclusion\nnextThoughtNeeded: false")
    assert "nextThoughtNeeded: false" in result.output


def test_mcp_nextThoughtNeeded_true_reported_in_metadata(runner, db):
    result = add(runner, "**Thought 1 of ~3**\nFrame\nnextThoughtNeeded: true")
    assert "nextThoughtNeeded: true" in result.output


def test_mcp_branches_field_present_in_output(runner, db):
    """branches field always present, matching MCP response schema."""
    result = add(runner, "**Thought 1 of ~3**\nFrame\nnextThoughtNeeded: true")
    assert "branches:" in result.output


# ── Category 3: Revision and branching ────────────────────────────────────────
# Annotations must be preserved and branch tracking must work.

def test_branch_annotation_tracked_in_branches_list(runner, db):
    add(runner, "**Thought 1 of ~4**\nFrame\nnextThoughtNeeded: true")
    add(runner, "**Thought 2 of ~4**\nHypothesis\nnextThoughtNeeded: true")
    result = add(runner,
        "**Thought 3 of ~4** [BRANCH from Thought 2 — alt-approach]\n"
        "Alternative path\nnextThoughtNeeded: true"
    )
    assert "alt-approach" in result.output
    assert "branches: ['alt-approach']" in result.output


def test_revision_annotation_preserved_in_chain_text(runner, db):
    add(runner, "**Thought 1 of ~3**\nOriginal hypothesis\nnextThoughtNeeded: true")
    add(runner, "**Thought 2 of ~3**\nEvidence refutes it\nnextThoughtNeeded: true")
    result = add(runner,
        "**Thought 3 of ~4** [REVISION of Thought 1]\n"
        "Revised hypothesis\nnextThoughtNeeded: true"
    )
    assert "[REVISION of Thought 1]" in result.output
    assert "Revised hypothesis" in result.output
    assert "Original hypothesis" in result.output


def test_multiple_branches_all_tracked(runner, db):
    add(runner, "**Thought 1 of ~6**\nFrame\nnextThoughtNeeded: true")
    add(runner, "**Thought 2 of ~6** [BRANCH from Thought 1 — approach-a]\nApproach A\nnextThoughtNeeded: true")
    result = add(runner, "**Thought 3 of ~6** [BRANCH from Thought 1 — approach-b]\nApproach B\nnextThoughtNeeded: true")
    assert "approach-a" in result.output
    assert "approach-b" in result.output


def test_needsMoreThoughts_annotation_preserved(runner, db):
    add(runner, "**Thought 1 of ~2**\nInitial\nnextThoughtNeeded: true")
    result = add(runner,
        "**Thought 2 of ~2** [needsMoreThoughts]\n"
        "Reached estimate but need more\nnextThoughtNeeded: true"
    )
    assert "[needsMoreThoughts]" in result.output
    assert "nextThoughtNeeded: true" in result.output


def test_parse_thoughts_returns_correct_metadata():
    """Unit test _parse_thoughts extracts all header fields correctly."""
    content = (
        "**Thought 1 of ~3**\nFrame: problem\nnextThoughtNeeded: true\n\n"
        "**Thought 2 of ~3** [REVISION of Thought 1]\nRevised\nnextThoughtNeeded: true\n\n"
        "**Thought 3 of ~4** [BRANCH from Thought 2 — alt]\nBranch\nnextThoughtNeeded: false"
    )
    thoughts = _parse_thoughts(content)
    assert len(thoughts) == 3
    assert thoughts[0]['number'] == 1 and thoughts[0]['total'] == 3
    assert thoughts[1]['nextThoughtNeeded'] is True
    assert thoughts[2]['nextThoughtNeeded'] is False
    assert thoughts[2]['branchId'] == 'alt'
    branches = _extract_branches(thoughts)
    assert branches == ['alt']


# ── Category 4: Session isolation ─────────────────────────────────────────────
# Different session GIDs must be fully isolated — same guarantee as separate
# MCP server instances.

def test_session_different_gids_fully_isolated(runner, db):
    with patch("os.getsid", return_value=1111):
        add(runner, "**Thought 1 of ~2**\nSession A\nnextThoughtNeeded: true")
    with patch("os.getsid", return_value=2222):
        result = runner.invoke(thought_group, ["show"])
    assert "No active thought chain" in result.output


def test_session_same_gid_shares_state(runner, db):
    with patch("os.getsid", return_value=9999):
        add(runner, "**Thought 1 of ~2**\nShared\nnextThoughtNeeded: true")
    with patch("os.getsid", return_value=9999):
        result = runner.invoke(thought_group, ["show"])
    assert "**Thought 1 of ~2**" in result.output


def test_session_explicit_chain_id_bypasses_current_pointer(runner, db):
    add(runner, "**Thought 1 of ~2**\nDefault chain\nnextThoughtNeeded: true")
    add(runner, "**Thought 1 of ~2**\nNamed chain\nnextThoughtNeeded: true", chain="mychain")
    result = runner.invoke(thought_group, ["show", "--chain", "mychain"])
    assert "Named chain" in result.output
    # Default chain thoughts should NOT be in the named chain
    assert "Default chain" not in result.output


def test_session_multiple_chains_coexist_in_same_session(runner, db):
    add(runner, "**Thought 1 of ~1**\nChain Alpha\nnextThoughtNeeded: false", chain="alpha")
    add(runner, "**Thought 1 of ~1**\nChain Beta\nnextThoughtNeeded: false", chain="beta")
    list_result = runner.invoke(thought_group, ["list"])
    assert "alpha" in list_result.output
    assert "beta" in list_result.output


def test_session_project_namespace_includes_gid():
    """Project namespace must embed the session GID to guarantee isolation."""
    from nexus.commands.thought import _session_project
    with patch("os.getsid", return_value=12345):
        project = _session_project("myrepo")
    assert "12345" in project
    assert "myrepo" in project


def test_session_nexus_session_id_overrides_getsid():
    """NEXUS_SESSION_ID env var must override os.getsid for cross-process sharing."""
    from nexus.commands.thought import _session_project
    with (
        patch("os.getsid", return_value=99999),
        patch.dict(os.environ, {"NEXUS_SESSION_ID": "e2e-test-12345"}),
    ):
        project = _session_project("myrepo")
    assert "e2e-test-12345" in project
    assert "99999" not in project
    assert "myrepo" in project


def test_session_nexus_session_id_cross_process_sharing(runner, db):
    """Two processes with different GIDs but same NEXUS_SESSION_ID share the chain."""
    # "Process A" (gid 1111) writes a thought with shared session ID
    with (
        patch("os.getsid", return_value=1111),
        patch.dict(os.environ, {"NEXUS_SESSION_ID": "shared-e2e-key"}),
    ):
        add(runner, "**Thought 1 of ~2**\nFrom process A\nnextThoughtNeeded: true")

    # "Process B" (gid 2222, different GID) reads via same session ID
    with (
        patch("os.getsid", return_value=2222),
        patch.dict(os.environ, {"NEXUS_SESSION_ID": "shared-e2e-key"}),
    ):
        result = runner.invoke(thought_group, ["show"])

    assert "From process A" in result.output


# ── Category 5 (MCP-mirrored): Direct port of lib.test.ts ─────────────────────
# Each test below maps 1:1 to a test in the MCP server's own test suite.
# Where the MCP returns JSON metadata, nx thought emits text fields — we
# verify the same semantic properties hold.

def test_mcp_mirror_basic_thought_accepted_and_metadata_returned(runner, db):
    """MCP: 'should accept valid basic thought' — verify all response fields."""
    result = add(runner,
        "**Thought 1 of ~3**\nThis is my first thought\nnextThoughtNeeded: true"
    )
    assert result.exit_code == 0
    assert "thoughtNumber: 1" in result.output
    assert "totalThoughts: 3" in result.output
    assert "nextThoughtNeeded: true" in result.output
    assert "thoughtHistoryLength: 1" in result.output
    assert "branches: []" in result.output


def test_mcp_mirror_optional_fields_accepted(runner, db):
    """MCP: 'should accept thought with optional fields' (isRevision, revisesThought, needsMoreThoughts)."""
    add(runner, "**Thought 1 of ~3**\nFirst\nnextThoughtNeeded: true")
    result = add(runner,
        "**Thought 2 of ~3** [REVISION of Thought 1]\n"
        "Revising my earlier idea\n"
        "needsMoreThoughts: false\n"
        "nextThoughtNeeded: true"
    )
    assert result.exit_code == 0
    assert "thoughtHistoryLength: 2" in result.output
    assert "[REVISION of Thought 1]" in result.output


def test_mcp_mirror_multiple_thoughts_tracked_in_history(runner, db):
    """MCP: 'should track multiple thoughts in history' — thoughtHistoryLength = 3, nextThoughtNeeded = false."""
    add(runner, "**Thought 1 of ~3**\nFirst thought\nnextThoughtNeeded: true")
    add(runner, "**Thought 2 of ~3**\nSecond thought\nnextThoughtNeeded: true")
    result = add(runner, "**Thought 3 of ~3**\nFinal thought\nnextThoughtNeeded: false")
    assert "thoughtHistoryLength: 3" in result.output
    assert "nextThoughtNeeded: false" in result.output


def test_mcp_mirror_auto_adjust_totalThoughts_when_number_exceeds(runner, db):
    """MCP: 'should auto-adjust totalThoughts if thoughtNumber exceeds it' — exact MCP scenario."""
    # MCP test: thoughtNumber=5, totalThoughts=3 → response totalThoughts should be 5
    result = add(runner,
        "**Thought 5 of ~3**\nThought 5 exceeding estimate\nnextThoughtNeeded: true"
    )
    assert "totalThoughts: 5" in result.output


def test_mcp_mirror_branches_tracked_correctly(runner, db):
    """MCP: 'should track branches correctly' — both branch IDs in branches list."""
    add(runner, "**Thought 1 of ~3**\nMain thought\nnextThoughtNeeded: true")
    add(runner,
        "**Thought 2 of ~3** [BRANCH from Thought 1 — branch-a]\n"
        "Branch A thought\nnextThoughtNeeded: true"
    )
    result = add(runner,
        "**Thought 2 of ~3** [BRANCH from Thought 1 — branch-b]\n"
        "Branch B thought\nnextThoughtNeeded: false"
    )
    assert "branch-a" in result.output
    assert "branch-b" in result.output


def test_mcp_mirror_same_branch_id_deduplicated(runner, db):
    """MCP: 'should allow multiple thoughts in same branch' — branch ID appears only once."""
    add(runner,
        "**Thought 1 of ~2** [BRANCH from Thought 1 — branch-a]\n"
        "Branch thought 1\nnextThoughtNeeded: true"
    )
    result = add(runner,
        "**Thought 2 of ~2** [BRANCH from Thought 1 — branch-a]\n"
        "Branch thought 2\nnextThoughtNeeded: false"
    )
    # branch-a should appear exactly once in the branches list
    branches_line = next(
        line for line in result.output.splitlines() if line.startswith("branches:")
    )
    assert branches_line.count("branch-a") == 1


def test_mcp_mirror_very_long_thought_string(runner, db):
    """MCP: 'should handle very long thought strings' — no error, metadata correct."""
    long_content = "a" * 10_000
    result = add(runner,
        f"**Thought 1 of ~1**\n{long_content}\nnextThoughtNeeded: false"
    )
    assert result.exit_code == 0
    assert "thoughtHistoryLength: 1" in result.output
    assert "nextThoughtNeeded: false" in result.output


def test_mcp_mirror_single_thought_chain(runner, db):
    """MCP: 'should handle thoughtNumber = 1, totalThoughts = 1'."""
    result = add(runner, "**Thought 1 of ~1**\nOnly thought\nnextThoughtNeeded: false")
    assert result.exit_code == 0
    assert "thoughtNumber: 1" in result.output
    assert "totalThoughts: 1" in result.output


def test_mcp_mirror_response_has_all_required_fields(runner, db):
    """MCP: 'should return correct response structure on success' — all fields present."""
    result = add(runner, "**Thought 1 of ~1**\nTest thought\nnextThoughtNeeded: false")
    for field in ["thoughtNumber:", "totalThoughts:", "nextThoughtNeeded:", "thoughtHistoryLength:", "branches:"]:
        assert field in result.output, f"Missing field: {field}"


# ── Category 5: Lifecycle ──────────────────────────────────────────────────────
# Start → add → close matches expected state transitions.

def test_lifecycle_show_with_no_chain_gives_helpful_message(runner, db):
    result = runner.invoke(thought_group, ["show"])
    assert result.exit_code == 0
    assert "No active thought chain" in result.output


def test_lifecycle_close_clears_current_pointer(runner, db):
    add(runner, "**Thought 1 of ~1**\nDone\nnextThoughtNeeded: false")
    runner.invoke(thought_group, ["close"])
    result = runner.invoke(thought_group, ["show"])
    assert "No active thought chain" in result.output


def test_lifecycle_close_preserves_chain_data(runner, db):
    """Closing a chain clears the pointer but keeps the data accessible by ID."""
    add(runner, "**Thought 1 of ~1**\nPreserved\nnextThoughtNeeded: false", chain="saved")
    runner.invoke(thought_group, ["close", "--chain", "saved"])
    result = runner.invoke(thought_group, ["show", "--chain", "saved"])
    assert "Preserved" in result.output


def test_lifecycle_list_marks_active_chain(runner, db):
    add(runner, "**Thought 1 of ~2**\nActive\nnextThoughtNeeded: true")
    result = runner.invoke(thought_group, ["list"])
    assert "← active" in result.output


def test_lifecycle_next_add_after_close_starts_new_chain(runner, db):
    add(runner, "**Thought 1 of ~1**\nOld chain\nnextThoughtNeeded: false")
    runner.invoke(thought_group, ["close"])
    result = add(runner, "**Thought 1 of ~3**\nNew chain\nnextThoughtNeeded: true")
    # New chain should NOT contain old chain's thoughts
    assert "Old chain" not in result.output
    assert "New chain" in result.output
