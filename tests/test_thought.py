# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for nx thought — session-scoped sequential thinking chains."""

import os
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.commands.thought import thought_group


@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def tmp_db(tmp_path):
    """Patch default_db_path to use a temp file, and fix session GID."""
    db_path = tmp_path / "memory.db"
    with (
        patch("nexus.commands.thought.default_db_path", return_value=db_path),
        patch("nexus.commands.thought._repo_name", return_value="testrepo"),
        patch("os.getsid", return_value=12345),
    ):
        yield db_path


# ── add ───────────────────────────────────────────────────────────────────────

def test_add_creates_chain_and_returns_it(runner, tmp_db):
    result = runner.invoke(thought_group, ["add", "**Thought 1 of ~3**\nHypothesis: X\nnextThoughtNeeded: true"])
    assert result.exit_code == 0
    assert "**Thought 1 of ~3**" in result.output
    assert "Chain:" in result.output
    assert "1 thought" in result.output


def test_add_accumulates_thoughts(runner, tmp_db):
    runner.invoke(thought_group, ["add", "**Thought 1 of ~3**\nFrame: why\nnextThoughtNeeded: true"])
    result = runner.invoke(thought_group, ["add", "**Thought 2 of ~3**\nEvidence: X\nnextThoughtNeeded: true"])
    assert result.exit_code == 0
    assert "**Thought 1 of ~3**" in result.output
    assert "**Thought 2 of ~3**" in result.output
    assert "2 thoughts" in result.output


def test_add_returns_full_chain_after_simulated_compaction(runner, tmp_db):
    """Simulates compaction: thoughts written in one invocation, retrieved in another.

    This is the core property that makes nx thought equivalent to the MCP server:
    each add returns the FULL chain from T2 storage, regardless of conversation context.
    """
    runner.invoke(thought_group, ["add", "**Thought 1 of ~4**\nFrame: problem\nnextThoughtNeeded: true"])
    runner.invoke(thought_group, ["add", "**Thought 2 of ~4**\nHypothesis: A\nnextThoughtNeeded: true"])
    runner.invoke(thought_group, ["add", "**Thought 3 of ~4**\nEvidence: found B\nnextThoughtNeeded: true"])

    # Simulated compaction: in a fresh process, same session GID, same db
    result = runner.invoke(thought_group, ["add", "**Thought 4 of ~4**\nConclusion\nnextThoughtNeeded: false"])
    assert result.exit_code == 0
    # All four thoughts visible in tool result
    assert "**Thought 1 of ~4**" in result.output
    assert "**Thought 2 of ~4**" in result.output
    assert "**Thought 3 of ~4**" in result.output
    assert "**Thought 4 of ~4**" in result.output
    assert "4 thoughts" in result.output


def test_add_shows_next_thought_prompt(runner, tmp_db):
    result = runner.invoke(thought_group, ["add", "**Thought 1 of ~3**\nFrame\nnextThoughtNeeded: true"])
    assert "Next: nx thought add" in result.output
    assert "Thought 2" in result.output


def test_add_explicit_chain_id(runner, tmp_db):
    runner.invoke(thought_group, ["add", "--chain", "mychain", "**Thought 1 of ~2**\nA\nnextThoughtNeeded: true"])
    result = runner.invoke(thought_group, ["add", "--chain", "mychain", "**Thought 2 of ~2**\nB\nnextThoughtNeeded: false"])
    assert result.exit_code == 0
    assert "mychain" in result.output
    assert "**Thought 1 of ~2**" in result.output
    assert "**Thought 2 of ~2**" in result.output


# ── show ──────────────────────────────────────────────────────────────────────

def test_show_no_chain(runner, tmp_db):
    result = runner.invoke(thought_group, ["show"])
    assert result.exit_code == 0
    assert "No active thought chain" in result.output


def test_show_returns_chain(runner, tmp_db):
    runner.invoke(thought_group, ["add", "**Thought 1 of ~2**\nA\nnextThoughtNeeded: true"])
    result = runner.invoke(thought_group, ["show"])
    assert result.exit_code == 0
    assert "**Thought 1 of ~2**" in result.output


# ── close ─────────────────────────────────────────────────────────────────────

def test_close_clears_current_pointer(runner, tmp_db):
    runner.invoke(thought_group, ["add", "**Thought 1 of ~1**\nDone\nnextThoughtNeeded: false"])
    close_result = runner.invoke(thought_group, ["close"])
    assert close_result.exit_code == 0
    assert "closed" in close_result.output

    show_result = runner.invoke(thought_group, ["show"])
    assert "No active thought chain" in show_result.output


def test_close_no_chain(runner, tmp_db):
    result = runner.invoke(thought_group, ["close"])
    assert result.exit_code == 0
    assert "No active thought chain" in result.output


# ── list ──────────────────────────────────────────────────────────────────────

def test_list_empty(runner, tmp_db):
    result = runner.invoke(thought_group, ["list"])
    assert result.exit_code == 0
    assert "No thought chains" in result.output


def test_list_shows_active_chain(runner, tmp_db):
    runner.invoke(thought_group, ["add", "**Thought 1 of ~2**\nA\nnextThoughtNeeded: true"])
    result = runner.invoke(thought_group, ["list"])
    assert result.exit_code == 0
    assert "← active" in result.output


# ── session isolation ─────────────────────────────────────────────────────────

def test_different_sessions_are_isolated(runner, tmp_db):
    """Chains from different session GIDs do not bleed into each other."""
    with patch("os.getsid", return_value=11111):
        runner.invoke(thought_group, ["add", "**Thought 1 of ~2**\nSession A\nnextThoughtNeeded: true"])

    with patch("os.getsid", return_value=22222):
        result = runner.invoke(thought_group, ["show"])
        assert "No active thought chain" in result.output
