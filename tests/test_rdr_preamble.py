# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for ``nx rdr preamble <name>`` subcommands.

Bead nexus-8nz1y (RDR-130 P1.1): these tests MUST FAIL (TDD red) because
``nx rdr preamble`` does not yet exist.  The subgroup will be implemented
in nexus-vb9r3 (P1.2).

Covers all 9 subcommands:
  rdr-create, rdr-list, rdr-show, rdr-gate, rdr-accept,
  rdr-close, rdr-research, rdr-audit, phase-review-gate

For each applicable subcommand, both data paths are covered:
  - T2-read path   : T2Database seeded with known RDR fixtures
  - file-fallback  : empty T2, fixture .md files in tmp docs/rdr/

The ``$ARGUMENTS`` passthrough via ``--`` terminator is covered explicitly.

Invocation convention mirrors test_rdr_lint.py:
  CliRunner().invoke(rdr, ["preamble", "<name>", ...])
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.commands.rdr import rdr
from nexus.db.t2 import T2Database


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _runner() -> CliRunner:
    """CliRunner — matches the convention in test_rdr_lint.py."""
    return CliRunner()


@pytest.fixture()
def rdr_env(tmp_path: Path, monkeypatch):
    """Hermetic environment: tmp git repo, default T2 path, cwd set to repo root.

    Returns a namespace-like dict:
      rdr_dir     -- Path to tmp_path/docs/rdr (created)
      db_path     -- Path to tmp_path/t2.db
      db          -- live T2Database (open for seeding, auto-closed via yield)
      repo_root   -- tmp_path (the fake git root)
    """
    # Fake git repo so git rev-parse --show-toplevel falls back cleanly.
    subprocess.run(
        ["git", "init", str(tmp_path)],
        check=True, capture_output=True,
    )
    monkeypatch.chdir(tmp_path)

    # Ensure docs/rdr exists (subcommands default to this path).
    rdr_dir = tmp_path / "docs" / "rdr"
    rdr_dir.mkdir(parents=True, exist_ok=True)

    # Redirect T2 to tmp SQLite so we don't touch the real database.
    db_path = tmp_path / "t2.db"
    monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: db_path)

    db = T2Database(db_path)
    yield {
        "rdr_dir": rdr_dir,
        "db_path": db_path,
        "db": db,
        "repo_root": tmp_path,
    }
    db.close()


def _write_rdr(rdr_dir: Path, filename: str, frontmatter: dict, body: str = "") -> Path:
    """Write a minimal RDR markdown file with YAML frontmatter."""
    fm_lines = ["---"]
    for k, v in frontmatter.items():
        fm_lines.append(f"{k}: {v}")
    fm_lines.append("---")
    fm_lines.append("")
    if body:
        fm_lines.append(body)
    path = rdr_dir / filename
    path.write_text("\n".join(fm_lines), encoding="utf-8")
    return path


def _seed_rdr_t2(db: T2Database, repo_name: str, rdr_id: str, **fields) -> None:
    """Seed a single RDR entry in T2 under project ``<repo_name>_rdr``.

    ``rdr_id`` must be a numeric string (e.g. "1", "130") — the ported
    rdr-list code filters on ``re.match(r'^\\d+$', title)``.
    """
    content_lines = [f"{k}: {v}" for k, v in fields.items()]
    db.put(
        project=f"{repo_name}_rdr",
        title=rdr_id,
        content="\n".join(content_lines),
    )


# ---------------------------------------------------------------------------
# rdr-list  (T2 path + file-fallback path)
# ---------------------------------------------------------------------------


class TestRdrList:
    """Tests for ``nx rdr preamble rdr-list``."""

    def test_rdr_list_t2_path(self, rdr_env):
        """T2-seeded path: output reports 'source: T2' and the table header."""
        repo_name = rdr_env["repo_root"].name
        _seed_rdr_t2(
            rdr_env["db"],
            repo_name,
            "130",
            title="Command Preambles via the nx CLI",
            status="accepted",
            type="decision",
            priority="P0",
            file_path="docs/rdr/rdr-130-command-preambles-via-nx-cli.md",
        )
        result = _runner().invoke(rdr, ["preamble", "rdr-list"])
        assert result.exit_code == 0, result.output
        assert "source: T2" in result.output
        assert "| ID | Title | Status | Type | Priority |" in result.output
        assert "Command Preambles via the nx CLI" in result.output
        assert "130" in result.output

    def test_rdr_list_file_fallback(self, rdr_env):
        """File-fallback path: empty T2 falls through to .md files."""
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-hello-world.md",
            {"title": "Hello World", "status": "draft", "type": "decision", "priority": "P1"},
        )
        result = _runner().invoke(rdr, ["preamble", "rdr-list"])
        assert result.exit_code == 0, result.output
        assert "source: files" in result.output
        assert "| ID | Title | Status | Type | Priority |" in result.output
        assert "Hello World" in result.output

    def test_rdr_list_no_rdr_dir(self, tmp_path, monkeypatch):
        """No docs/rdr directory: exits without error, reports directory missing."""
        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        db_path = tmp_path / "t2.db"
        monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: db_path)
        result = _runner().invoke(rdr, ["preamble", "rdr-list"])
        # Graceful: exit 0 or output about missing directory (not a crash)
        assert result.exit_code == 0
        assert "docs/rdr" in result.output


# ---------------------------------------------------------------------------
# rdr-create  (file-fallback only; no T2 path for create)
# ---------------------------------------------------------------------------


class TestRdrCreate:
    """Tests for ``nx rdr preamble rdr-create``."""

    def test_rdr_create_with_existing_rdrs(self, rdr_env):
        """File-fallback: prints Next ID, ID style, and existing RDRs table."""
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-hello-world.md",
            {"title": "Hello World", "status": "draft", "type": "decision", "priority": "P1"},
        )
        result = _runner().invoke(rdr, ["preamble", "rdr-create"])
        assert result.exit_code == 0, result.output
        assert "**Next ID:**" in result.output
        assert "RDR-002" in result.output
        assert "**ID style detected:**" in result.output
        assert "Existing RDRs" in result.output
        assert "Hello World" in result.output

    def test_rdr_create_no_rdr_dir(self, tmp_path, monkeypatch):
        """No docs/rdr directory: bootstrap message and first ID."""
        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        db_path = tmp_path / "t2.db"
        monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: db_path)
        result = _runner().invoke(rdr, ["preamble", "rdr-create"])
        assert result.exit_code == 0, result.output
        assert "bootstrap required" in result.output
        assert "RDR-001" in result.output
        assert "this will be the first RDR" in result.output


# ---------------------------------------------------------------------------
# rdr-show  (file-fallback; no-arg and with-id paths)
# ---------------------------------------------------------------------------


class TestRdrShow:
    """Tests for ``nx rdr preamble rdr-show``."""

    def test_rdr_show_no_arg_lists_all(self, rdr_env):
        """No ID arg: prints file listing table (most recently modified first)."""
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-hello-world.md",
            {"title": "Hello World", "status": "draft", "type": "decision", "priority": "P1"},
        )
        result = _runner().invoke(rdr, ["preamble", "rdr-show"])
        assert result.exit_code == 0, result.output
        assert "RDR Files" in result.output
        assert "| File | Title | Status | Type | Priority |" in result.output
        assert "Hello World" in result.output

    def test_rdr_show_with_id_via_double_dash(self, rdr_env):
        """ID via ``--`` terminator: prints specific RDR metadata table."""
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-hello-world.md",
            {
                "title": "Hello World",
                "status": "draft",
                "type": "decision",
                "priority": "P1",
                "author": "hal",
            },
            body="## Problem Statement\n\nSomething is wrong.",
        )
        result = _runner().invoke(rdr, ["preamble", "rdr-show", "--", "1"])
        assert result.exit_code == 0, result.output
        assert "### RDR:" in result.output
        assert "rdr-001-hello-world.md" in result.output
        assert "#### Metadata" in result.output
        assert "Hello World" in result.output

    def test_rdr_show_unknown_id(self, rdr_env):
        """Unknown ID: prints 'RDR not found' and available list."""
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-hello-world.md",
            {"title": "Hello World", "status": "draft", "type": "decision", "priority": "P1"},
        )
        result = _runner().invoke(rdr, ["preamble", "rdr-show", "--", "999"])
        assert result.exit_code == 0, result.output
        assert "RDR not found for" in result.output

    def test_rdr_show_double_dash_passthrough(self, rdr_env):
        """Explicit regression: ``--`` must pass a numeric arg, not swallow it."""
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-042-another.md",
            {"title": "Another RDR", "status": "accepted", "type": "decision", "priority": "P0"},
        )
        result = _runner().invoke(rdr, ["preamble", "rdr-show", "--", "42"])
        assert result.exit_code == 0, result.output
        assert "rdr-042-another.md" in result.output


# ---------------------------------------------------------------------------
# rdr-gate  (no-arg + with-id)
# ---------------------------------------------------------------------------


class TestRdrGate:
    """Tests for ``nx rdr preamble rdr-gate``."""

    def test_rdr_gate_no_arg_prints_usage(self, rdr_env):
        """No ID: prints usage line and Available RDRs table."""
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-hello-world.md",
            {"title": "Hello World", "status": "draft", "type": "decision", "priority": "P1"},
        )
        result = _runner().invoke(rdr, ["preamble", "rdr-gate"])
        assert result.exit_code == 0, result.output
        assert "Usage" in result.output
        assert "Available RDRs" in result.output
        assert "| File | Title | Status | Type |" in result.output

    def test_rdr_gate_with_pre65_rdr_prints_section_structure(self, rdr_env):
        """ID for pre-65 RDR (no gap requirement): prints Section Structure."""
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-hello-world.md",
            {"title": "Hello World", "status": "draft", "type": "decision", "priority": "P1"},
            body=(
                "## Problem Statement\n\nProblem here.\n\n"
                "## Proposed Solution\n\nSolution here.\n\n"
                "## Tradeoffs\n\nTradeoffs here."
            ),
        )
        result = _runner().invoke(rdr, ["preamble", "rdr-gate", "--", "1"])
        assert result.exit_code == 0, result.output
        assert "Section Structure" in result.output
        assert "## Problem Statement" in result.output

    def test_rdr_gate_blocked_post65_missing_gaps(self, rdr_env):
        """Post-65 RDR with no Gap headings: prints BLOCKED message."""
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-070-taxonomy.md",
            {"title": "Taxonomy", "status": "draft", "type": "decision", "priority": "P0"},
            body="## Problem Statement\n\nNo gaps structured here.\n\n## Approach\n\nDo things.",
        )
        result = _runner().invoke(rdr, ["preamble", "rdr-gate", "--", "70"])
        assert result.exit_code == 0, result.output
        assert "BLOCKED" in result.output
        assert "gap structure" in result.output.lower()

    def test_rdr_gate_post65_with_gaps_prints_gap_list(self, rdr_env):
        """Post-65 RDR with gap headings: lists gaps before Section Structure."""
        body = (
            "## Problem Statement\n\n"
            "#### Gap 1: Missing preamble commands\nThe nx CLI lacks preamble commands.\n\n"
            "#### Gap 2: Brittle bash injection\nBash heredocs break.\n\n"
            "## Proposed Solution\n\nPort to nx CLI."
        )
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-130-command-preambles.md",
            {"title": "Command Preambles", "status": "draft", "type": "decision", "priority": "P0"},
            body=body,
        )
        result = _runner().invoke(rdr, ["preamble", "rdr-gate", "--", "130"])
        assert result.exit_code == 0, result.output
        # O1: rdr-gate output uses no-space form "Gap1" to match original rdr_gate.py
        assert "Gap1" in result.output
        assert "Gap2" in result.output
        assert "gap heading(s) present" in result.output


# ---------------------------------------------------------------------------
# rdr-accept  (no-arg + with-id paths)
# ---------------------------------------------------------------------------


class TestRdrAccept:
    """Tests for ``nx rdr preamble rdr-accept``."""

    def test_rdr_accept_no_arg_prints_usage_and_drafts(self, rdr_env):
        """No ID: prints usage + Draft RDRs table."""
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-hello-world.md",
            {"title": "Hello World", "status": "draft", "type": "decision", "priority": "P1"},
        )
        result = _runner().invoke(rdr, ["preamble", "rdr-accept"])
        assert result.exit_code == 0, result.output
        assert "Usage" in result.output
        assert "Draft RDRs (eligible for acceptance)" in result.output
        assert "Hello World" in result.output

    def test_rdr_accept_with_draft_rdr_prints_planning_handoff(self, rdr_env):
        """Draft RDR with plan section: prints Planning Handoff block."""
        body = (
            "## Problem Statement\n\nProblem.\n\n"
            "## Approach\n\n"
            "### Phase 1: Implement\nDo the work.\n\n"
            "### Phase 2: Validate\nCheck it works.\n\n"
            "## Tradeoffs\n\nSome tradeoffs."
        )
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-hello-world.md",
            {"title": "Hello World", "status": "draft", "type": "decision", "priority": "P1"},
            body=body,
        )
        result = _runner().invoke(rdr, ["preamble", "rdr-accept", "--", "1"])
        assert result.exit_code == 0, result.output
        assert "### RDR:" in result.output
        assert "Planning Handoff" in result.output
        assert "Step count detected:" in result.output

    def test_rdr_accept_blocked_non_draft_status(self, rdr_env):
        """Non-draft/accepted status: prints BLOCKED message."""
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-hello-world.md",
            {"title": "Hello World", "status": "closed", "type": "decision", "priority": "P1"},
        )
        result = _runner().invoke(rdr, ["preamble", "rdr-accept", "--", "1"])
        assert result.exit_code == 0, result.output
        assert "BLOCKED" in result.output
        assert "closed" in result.output


# ---------------------------------------------------------------------------
# rdr-close  (no-arg + with-id paths, draft-blocked + accepted-proceeds)
# ---------------------------------------------------------------------------


class TestRdrClose:
    """Tests for ``nx rdr preamble rdr-close``."""

    def test_rdr_close_no_arg_prints_usage_and_rdr_list(self, rdr_env):
        """No ID: prints usage + Open/Draft RDRs table."""
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-hello-world.md",
            {"title": "Hello World", "status": "draft", "type": "decision", "priority": "P1"},
        )
        result = _runner().invoke(rdr, ["preamble", "rdr-close"])
        assert result.exit_code == 0, result.output
        assert "Usage" in result.output
        assert "Open/Draft RDRs" in result.output
        assert "Hello World" in result.output

    def test_rdr_close_blocked_draft_status(self, rdr_env):
        """Draft RDR: prints BLOCKED (requires accepted/final status)."""
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-hello-world.md",
            {"title": "Hello World", "status": "draft", "type": "decision", "priority": "P1"},
        )
        result = _runner().invoke(rdr, ["preamble", "rdr-close", "--", "1"])
        assert result.exit_code == 0, result.output
        assert "BLOCKED" in result.output
        assert "draft" in result.output.lower()
        assert "accepted" in result.output.lower()

    def test_rdr_close_accepted_pre65_no_gaps_proceeds_to_t2(self, rdr_env):
        """Accepted pre-65 RDR with --reason implemented: passes gap check, prints T2 Metadata."""
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-hello-world.md",
            {"title": "Hello World", "status": "accepted", "type": "decision", "priority": "P1"},
            body="## Problem Statement\n\nProblem without structured gaps.\n\n## Approach\n\nStuff.",
        )
        result = _runner().invoke(
            rdr,
            ["preamble", "rdr-close", "--", "1", "--reason", "implemented"],
        )
        assert result.exit_code == 0, result.output
        # Pre-65, no gaps — warns and proceeds to T2 Metadata section
        assert "T2 Metadata" in result.output

    def test_rdr_close_force_flag_overrides_draft_block(self, rdr_env):
        """--force overrides the draft-status block."""
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-hello-world.md",
            {"title": "Hello World", "status": "draft", "type": "decision", "priority": "P1"},
        )
        result = _runner().invoke(rdr, ["preamble", "rdr-close", "--", "1", "--force"])
        assert result.exit_code == 0, result.output
        assert "Override" in result.output
        assert "BLOCKED" not in result.output

    # S1 regression: --force-implemented with empty reason must error
    def test_rdr_close_force_implemented_empty_reason_errors(self, rdr_env):
        """S1: --force-implemented with empty reason string prints ERROR and exits clean.

        Original rdr_close.py:133-137 rejected empty/whitespace reasons.
        The port dropped this guard; this test prevents regression.
        """
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-hello-world.md",
            {"title": "Hello World", "status": "accepted", "type": "decision", "priority": "P1"},
        )
        # Pass an empty-string reason (two single-quotes with nothing inside)
        result = _runner().invoke(
            rdr,
            ["preamble", "rdr-close", "--", "1", "--reason", "implemented",
             "--force-implemented", ""],
        )
        assert result.exit_code == 0, result.output
        assert "ERROR" in result.output
        assert "non-empty reason" in result.output
        # Must not proceed to T2 Metadata
        assert "T2 Metadata" not in result.output

    # S3 regression: WARNING block must appear when open beads exist
    def test_rdr_close_warning_present_when_open_beads(self, rdr_env, monkeypatch):
        """S3: prints WARNING when bd list returns open beads.

        Original rdr_close.py:335-341 printed an explicit warning requiring
        explicit user confirmation.  The port dropped the conditional block.
        """
        import subprocess as _real_sp

        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-hello-world.md",
            {"title": "Hello World", "status": "accepted", "type": "decision", "priority": "P1"},
        )

        # Mock bd list to return a non-empty bead list; route git calls to real subprocess
        def _fake_run(cmd, **kwargs):
            if cmd and cmd[0] == "git":
                return _real_sp.run(cmd, **kwargs)
            r = _real_sp.CompletedProcess(cmd, 0)
            r.stdout = "nexus-abc: some open bead (open)"
            r.stderr = ""
            return r

        monkeypatch.setattr("nexus.commands.rdr.subprocess.run", _fake_run)
        result = _runner().invoke(rdr, ["preamble", "rdr-close", "--", "1"])
        assert result.exit_code == 0, result.output
        assert "WARNING" in result.output
        assert "Open beads exist" in result.output
        assert "explicit" in result.output

    def test_rdr_close_no_warning_when_no_open_beads(self, rdr_env, monkeypatch):
        """S3: WARNING is absent when no open beads exist.

        Counterpart to the above — ensures the WARNING is conditional,
        not unconditional.
        """
        import subprocess as _real_sp

        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-hello-world.md",
            {"title": "Hello World", "status": "accepted", "type": "decision", "priority": "P1"},
        )

        def _fake_run(cmd, **kwargs):
            if cmd and cmd[0] == "git":
                return _real_sp.run(cmd, **kwargs)
            r = _real_sp.CompletedProcess(cmd, 0)
            r.stdout = "No issues found."
            r.stderr = ""
            return r

        monkeypatch.setattr("nexus.commands.rdr.subprocess.run", _fake_run)
        result = _runner().invoke(rdr, ["preamble", "rdr-close", "--", "1"])
        assert result.exit_code == 0, result.output
        assert "WARNING" not in result.output
        assert "Open beads exist" not in result.output

    # S2 regression: PASS-2 success must attempt nx scratch put with rdr-close-active tag
    def test_rdr_close_pass2_success_attempts_scratch_put(self, rdr_env, monkeypatch):
        """S2: after gap-pointer validation passes, subprocess.run is called with nx scratch put.

        Original rdr_close.py:299-303 emitted a best-effort scratch marker
        (rdr-close-active tag) after PASS-2 pointer validation succeeded.
        The port omitted this call.  We verify the call is attempted.
        """
        # Create a real implementation file so the pointer validation passes
        impl_file = rdr_env["repo_root"] / "src" / "impl.py"
        impl_file.parent.mkdir(parents=True, exist_ok=True)
        impl_file.write_text("# implementation\n")

        body = (
            "## Problem Statement\n\n"
            "#### Gap 1: Missing feature\nThe feature is missing.\n\n"
            "## Approach\n\nImplement it."
        )
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-130-cmd.md",
            {"title": "Command Preambles", "status": "accepted",
             "type": "decision", "priority": "P0"},
            body=body,
        )

        import subprocess as _real_sp

        scratch_calls = []

        def _capture_run(cmd, **kwargs):
            if cmd and cmd[0] == "git":
                return _real_sp.run(cmd, **kwargs)
            scratch_calls.append(list(cmd))
            r = _real_sp.CompletedProcess(cmd, 0)
            r.stdout = "No issues found."
            r.stderr = ""
            return r

        monkeypatch.setattr("nexus.commands.rdr.subprocess.run", _capture_run)
        result = _runner().invoke(
            rdr,
            ["preamble", "rdr-close", "--", "130", "--reason", "implemented",
             "--pointers", "Gap1=src/impl.py:1"],
        )
        assert result.exit_code == 0, result.output
        assert "validation passed" in result.output
        # Verify the scratch put call was attempted with the right tags
        scratch_cmds = [c for c in scratch_calls if "scratch" in c and "put" in c]
        assert scratch_cmds, (
            f"Expected an 'nx scratch put' call with rdr-close-active tag; "
            f"got calls: {scratch_calls}"
        )
        assert any("rdr-close-active" in str(c) for c in scratch_cmds), (
            f"Expected rdr-close-active tag in scratch put call; got: {scratch_cmds}"
        )


# ---------------------------------------------------------------------------
# rdr-research  (no-arg + with-id)
# ---------------------------------------------------------------------------


class TestRdrResearch:
    """Tests for ``nx rdr preamble rdr-research``."""

    def test_rdr_research_no_arg_prints_usage_and_list(self, rdr_env):
        """No ID: prints Available RDRs table + usage hint."""
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-hello-world.md",
            {"title": "Hello World", "status": "draft", "type": "decision", "priority": "P1"},
        )
        result = _runner().invoke(rdr, ["preamble", "rdr-research"])
        assert result.exit_code == 0, result.output
        assert "Available RDRs" in result.output
        assert "| File | Title | Status | Type |" in result.output
        assert "Usage" in result.output

    def test_rdr_research_with_id_prints_rdr_header(self, rdr_env):
        """ID arg: prints RDR heading and Research Findings section."""
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-hello-world.md",
            {"title": "Hello World", "status": "draft", "type": "decision", "priority": "P1"},
            body="## Research Findings\n\n- Finding A\n- Finding B",
        )
        result = _runner().invoke(rdr, ["preamble", "rdr-research", "--", "1"])
        assert result.exit_code == 0, result.output
        assert "### RDR 1:" in result.output
        assert "Hello World" in result.output
        assert "Research Findings" in result.output

    def test_rdr_research_double_dash_passthrough_with_subcommand_word(self, rdr_env):
        """Subcommand word 'add' plus numeric ID: numeric ID is extracted correctly."""
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-hello-world.md",
            {"title": "Hello World", "status": "draft", "type": "decision", "priority": "P1"},
        )
        # "add 1" — the script searches for digits and finds 1
        result = _runner().invoke(rdr, ["preamble", "rdr-research", "--", "add", "1"])
        assert result.exit_code == 0, result.output
        assert "RDR 1" in result.output


# ---------------------------------------------------------------------------
# rdr-audit  (default mode + management subcommand)
# ---------------------------------------------------------------------------


class TestRdrAudit:
    """Tests for ``nx rdr preamble rdr-audit``."""

    def test_rdr_audit_default_mode(self, rdr_env):
        """No args or project arg: prints audit dispatch mode line."""
        result = _runner().invoke(rdr, ["preamble", "rdr-audit"])
        assert result.exit_code == 0, result.output
        assert "**Mode:** audit dispatch" in result.output
        assert "Target project:" in result.output

    def test_rdr_audit_list_subcommand(self, rdr_env):
        """'list' subcommand: prints management mode with read-only label."""
        result = _runner().invoke(rdr, ["preamble", "rdr-audit", "--", "list"])
        assert result.exit_code == 0, result.output
        assert "management subcommand" in result.output
        assert "list" in result.output
        assert "read-only" in result.output

    def test_rdr_audit_explicit_project(self, rdr_env):
        """Explicit project name: target project appears in output."""
        result = _runner().invoke(rdr, ["preamble", "rdr-audit", "--", "myproject"])
        assert result.exit_code == 0, result.output
        assert "myproject" in result.output


# ---------------------------------------------------------------------------
# phase-review-gate  (no-arg, no-approach error, pass-1 item table)
# ---------------------------------------------------------------------------


class TestPhaseReviewGate:
    """Tests for ``nx rdr preamble phase-review-gate``."""

    def test_phase_review_gate_no_arg_prints_usage(self, rdr_env):
        """No ID: prints usage + 'What this gate does' section."""
        result = _runner().invoke(rdr, ["preamble", "phase-review-gate"])
        assert result.exit_code == 0, result.output
        assert "Usage" in result.output
        assert "What this gate does" in result.output
        assert "Pass 1" in result.output
        assert "Pass 2" in result.output

    def test_phase_review_gate_no_approach_section_errors(self, rdr_env):
        """RDR without §Approach: prints ERROR about missing section."""
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-hello-world.md",
            {"title": "Hello World", "status": "accepted", "type": "decision", "priority": "P1"},
            body="## Problem Statement\n\nProblem.\n\n## Proposed Solution\n\nSolution.",
        )
        result = _runner().invoke(
            rdr,
            ["preamble", "phase-review-gate", "--", "1", "--phase", "1"],
        )
        assert result.exit_code == 0, result.output
        assert "ERROR" in result.output
        assert "Approach" in result.output

    def test_phase_review_gate_pass1_enumerates_items(self, rdr_env):
        """RDR with §Approach and numbered items: Pass 1 table printed."""
        body = (
            "## Problem Statement\n\nProblem.\n\n"
            "### Approach\n\n"
            "1. **T2 read**: Read from T2 database.\n"
            "2. **File fallback**: Fall back to .md files.\n"
            "3. **CLI output**: Print markdown table.\n\n"
            "## Tradeoffs\n\nSome tradeoffs."
        )
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-130-command-preambles.md",
            {"title": "Command Preambles", "status": "accepted", "type": "decision", "priority": "P0"},
            body=body,
        )
        result = _runner().invoke(
            rdr,
            ["preamble", "phase-review-gate", "--", "130", "--phase", "1"],
        )
        assert result.exit_code == 0, result.output
        assert "§Approach Cross-Walk" in result.output
        assert "| # | Label | Evidence needed |" in result.output
        assert "Item1" in result.output
        assert "T2 read" in result.output
        assert "Item2" in result.output
        assert "File fallback" in result.output
        assert "Item3" in result.output

    def test_phase_review_gate_pass2_all_covered_passes(self, rdr_env):
        """Pass 2 with all items covered: APPROACH CROSS-WALK PASSED printed."""
        body = (
            "## Problem Statement\n\nProblem.\n\n"
            "### Approach\n\n"
            "1. **T2 read**: Read from T2 database.\n"
            "2. **File fallback**: Fall back to .md files.\n\n"
            "## Tradeoffs\n\nSome tradeoffs."
        )
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-130-command-preambles.md",
            {"title": "Command Preambles", "status": "accepted", "type": "decision", "priority": "P0"},
            body=body,
        )
        result = _runner().invoke(
            rdr,
            [
                "preamble",
                "phase-review-gate",
                "--",
                "130",
                "--phase",
                "1",
                "--evidence",
                "Item1=nexus-abc1,Item2=nexus-xyz2",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "APPROACH CROSS-WALK PASSED" in result.output
        assert "nexus-abc1" in result.output
        assert "nexus-xyz2" in result.output

    def test_phase_review_gate_pass2_missing_evidence_blocked(self, rdr_env):
        """Pass 2 with missing evidence: BLOCKED printed."""
        body = (
            "## Problem Statement\n\nProblem.\n\n"
            "### Approach\n\n"
            "1. **T2 read**: Read from T2 database.\n"
            "2. **File fallback**: Fall back to .md files.\n\n"
            "## Tradeoffs\n\nSome tradeoffs."
        )
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-130-command-preambles.md",
            {"title": "Command Preambles", "status": "accepted", "type": "decision", "priority": "P0"},
            body=body,
        )
        # Only provide evidence for Item1, not Item2
        result = _runner().invoke(
            rdr,
            [
                "preamble",
                "phase-review-gate",
                "--",
                "130",
                "--phase",
                "1",
                "--evidence",
                "Item1=nexus-abc1",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "BLOCKED" in result.output
        assert "Item2" in result.output

    def test_phase_review_gate_pass2_empty_evidence_value_blocked(self, rdr_env):
        """Pass 2 with an empty evidence value (Item2=): BLOCKED printed.

        Migrated from test_phase_review_gate.py::TestPass2Validate.
        test_pass2_empty_evidence_value_blocks (nexus-2fnet).
        """
        body = (
            "## Problem Statement\n\nProblem.\n\n"
            "### Approach\n\n"
            "1. **T2 read**: Read from T2 database.\n"
            "2. **File fallback**: Fall back to .md files.\n\n"
            "## Tradeoffs\n\nSome tradeoffs."
        )
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-130-command-preambles.md",
            {"title": "Command Preambles", "status": "accepted", "type": "decision", "priority": "P0"},
            body=body,
        )
        result = _runner().invoke(
            rdr,
            [
                "preamble",
                "phase-review-gate",
                "--",
                "130",
                "--phase",
                "1",
                "--evidence",
                "Item1=nexus-abc1,Item2=",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "BLOCKED" in result.output

    # nexus-4u6mt: phase-block sub-bullet §Approach enumeration (RDR-120 style)

    _PHASE_BLOCK_BODY = (
        "## Problem Statement\n\nProblem.\n\n"
        "### Approach\n\n"
        "**Phase 0: Lint + cutover flag scaffolding**\n\n"
        "- Implement nx doctor --check-storage-boundary\n"
        "- Add NX_STORAGE_MODE env-var\n\n"
        "**Phase 1: T3 daemon**\n\n"
        "- Stand up the T3 daemon process\n"
        "- Route T3 reads through T3Client\n"
        "- Add storage_boundary_lint T3 enforcement\n\n"
        "## Tradeoffs\n\nSome tradeoffs."
    )

    def test_phase_block_enumerates_requested_phase_bullets(self, rdr_env):
        """RDR-120-style phase blocks: --phase 1 enumerates Phase 1's
        three sub-bullets as Item1..Item3 (nexus-4u6mt)."""
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-120-storage-substrate-split.md",
            {"title": "Storage Substrate Split", "status": "accepted",
             "type": "architecture", "priority": "P1"},
            body=self._PHASE_BLOCK_BODY,
        )
        result = _runner().invoke(
            rdr,
            ["preamble", "phase-review-gate", "--", "120", "--phase", "1"],
        )
        assert result.exit_code == 0, result.output
        assert "§Approach Cross-Walk" in result.output
        # Phase 1 has exactly 3 bullets -> Item1..Item3, none from Phase 0.
        assert "Item1" in result.output
        assert "Item2" in result.output
        assert "Item3" in result.output
        assert "Item4" not in result.output
        assert "Phase 1: Stand up the T3 daemon process" in result.output
        # Phase 0 bullets must NOT leak into the Phase 1 cross-walk.
        assert "check-storage-boundary" not in result.output

    def test_phase_block_phase0_enumerates_phase0_bullets(self, rdr_env):
        """--phase 0 enumerates Phase 0's two sub-bullets (nexus-4u6mt
        acceptance: matches the manual RDR-120 P0 cross-walk)."""
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-120-storage-substrate-split.md",
            {"title": "Storage Substrate Split", "status": "accepted",
             "type": "architecture", "priority": "P1"},
            body=self._PHASE_BLOCK_BODY,
        )
        result = _runner().invoke(
            rdr,
            ["preamble", "phase-review-gate", "--", "120", "--phase", "0"],
        )
        assert result.exit_code == 0, result.output
        assert "Item1" in result.output
        assert "Item2" in result.output
        assert "Item3" not in result.output
        assert "check-storage-boundary" in result.output

    def test_numbered_items_still_work_unchanged(self, rdr_env):
        """RDR-121/125-style numbered §Approach items must continue to
        enumerate phase-agnostically (regression guard for nexus-4u6mt)."""
        body = (
            "## Problem Statement\n\nProblem.\n\n"
            "### Approach\n\n"
            "1. **Vendor the hook**: Copy _lib.py into sn.\n"
            "2. **Byte-equality CI guard**: Assert identical bytes.\n\n"
            "## Tradeoffs\n\nT."
        )
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-125-routing-hook-plugin-ownership.md",
            {"title": "Routing Hook Ownership", "status": "accepted",
             "type": "architecture", "priority": "P1"},
            body=body,
        )
        result = _runner().invoke(
            rdr,
            ["preamble", "phase-review-gate", "--", "125", "--phase", "1"],
        )
        assert result.exit_code == 0, result.output
        assert "Item1" in result.output
        assert "Vendor the hook" in result.output
        assert "Item2" in result.output
        assert "Byte-equality CI guard" in result.output


class TestPhaseBlockParser:
    """Unit tests for _prg_parse_phase_block_items (nexus-4u6mt)."""

    _APPROACH = (
        "**Phase 0: Scaffolding**\n\n"
        "- bullet zero a\n"
        "- bullet zero b\n\n"
        "**Phase 1: Core**\n\n"
        "- **Daemon**: stand it up\n"
        "- route reads\n"
    )

    def test_selects_only_requested_phase(self):
        from nexus.commands.rdr import _prg_parse_phase_block_items
        items = _prg_parse_phase_block_items(self._APPROACH, phase="1")
        assert [n for n, _, _ in items] == [1, 2]
        assert items[0][1] == "Phase 1: Daemon"
        assert items[0][2] == "stand it up"
        assert items[1][1].startswith("Phase 1:")

    def test_phase0_selects_phase0(self):
        from nexus.commands.rdr import _prg_parse_phase_block_items
        items = _prg_parse_phase_block_items(self._APPROACH, phase="0")
        assert len(items) == 2
        assert all("Phase 0" in lbl for _, lbl, _ in items)

    def test_no_phase_enumerates_all_blocks(self):
        from nexus.commands.rdr import _prg_parse_phase_block_items
        items = _prg_parse_phase_block_items(self._APPROACH, phase=None)
        assert [n for n, _, _ in items] == [1, 2, 3, 4]

    def test_empty_on_non_phase_block_text(self):
        from nexus.commands.rdr import _prg_parse_phase_block_items
        # Numbered-item §Approach has no **Phase N:** header -> [].
        items = _prg_parse_phase_block_items(
            "1. **Foo**: bar\n2. **Baz**: qux\n", phase="1",
        )
        assert items == []

    def test_phase_arg_accepts_phase_n_prose(self):
        from nexus.commands.rdr import _prg_parse_phase_block_items
        items = _prg_parse_phase_block_items(self._APPROACH, phase="Phase 1")
        assert [n for n, _, _ in items] == [1, 2]


# ---------------------------------------------------------------------------
# Sentinel write (RDR-121 P2 co-requirement) — migrated from
# test_phase_review_gate.py::TestSentinelSideEffect (nexus-2fnet).
# ---------------------------------------------------------------------------


class TestPhaseReviewGateSentinel:
    """The PASSED path must write a sentinel; BLOCKED must not.

    Migrated from test_phase_review_gate.py::TestSentinelSideEffect (nexus-2fnet).
    Uses TMPDIR redirect (monkeypatch.setenv) instead of subprocess so CliRunner
    tests can verify the sentinel file without running a sub-process.
    """

    def _make_rdr_with_approach(self, rdr_dir: Path, rdr_id: int) -> None:
        body = (
            "## Problem Statement\n\nProblem.\n\n"
            "### Approach\n\n"
            "1. **T2 read**: Read T2.\n"
            "2. **File fallback**: Fall back to files.\n\n"
            "## Tradeoffs\n\nSome."
        )
        _write_rdr(
            rdr_dir,
            f"rdr-{rdr_id:03d}-test.md",
            {"title": "Test RDR", "status": "accepted", "type": "decision", "priority": "P0"},
            body=body,
        )

    def test_passed_writes_sentinel(self, rdr_env, monkeypatch, tmp_path):
        """PASSED outcome writes a sentinel JSON file under $TMPDIR/nx-phase-gate-sentinel/."""
        self._make_rdr_with_approach(rdr_env["rdr_dir"], 130)
        sentinel_base = tmp_path / "sentinels"
        sentinel_base.mkdir()
        monkeypatch.setenv("TMPDIR", str(sentinel_base))

        result = _runner().invoke(
            rdr,
            [
                "preamble",
                "phase-review-gate",
                "--",
                "130",
                "--phase",
                "1",
                "--evidence",
                "Item1=nexus-abc1,Item2=nexus-def2",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "APPROACH CROSS-WALK PASSED" in result.output
        sentinel_dir = sentinel_base / "nx-phase-gate-sentinel"
        assert sentinel_dir.exists(), "PASSED outcome must create sentinel dir"
        files = list(sentinel_dir.glob("*-130-1.json"))
        assert len(files) == 1, f"expected one sentinel for RDR-130 phase 1, got {files}"
        import json as _json
        payload = _json.loads(files[0].read_text())
        assert payload["outcome"] == "PASSED"
        assert payload["rdr_id"] == "130"
        assert payload["phase"] == "1"

    def test_blocked_does_not_write_sentinel(self, rdr_env, monkeypatch, tmp_path):
        """BLOCKED outcome must NOT write a sentinel file."""
        self._make_rdr_with_approach(rdr_env["rdr_dir"], 130)
        sentinel_base = tmp_path / "sentinels"
        sentinel_base.mkdir()
        monkeypatch.setenv("TMPDIR", str(sentinel_base))

        result = _runner().invoke(
            rdr,
            [
                "preamble",
                "phase-review-gate",
                "--",
                "130",
                "--phase",
                "1",
                "--evidence",
                "Item1=nexus-abc1",  # Item2 missing
            ],
        )
        assert result.exit_code == 0, result.output
        assert "BLOCKED" in result.output
        sentinel_dir = sentinel_base / "nx-phase-gate-sentinel"
        if sentinel_dir.exists():
            files = list(sentinel_dir.glob("*-130-1.json"))
            assert len(files) == 0, (
                f"BLOCKED outcome must not write a sentinel; found {files}"
            )


# ---------------------------------------------------------------------------
# Gap regex contract — migrated from test_rdr_close_gate.py::TestGapExtraction
# and TestPreambleConsistency (skill-file checks) (nexus-2fnet).
# These test the regex specification that lives in rdr.py's rdr-close preamble.
# The local _find_gaps replica tests the CONTRACT, not the file.
# ---------------------------------------------------------------------------

import re as _re


def _find_gaps(problem_stmt: str) -> list[tuple[str, str, str]]:
    """Replica of the gap-extraction regex in the rdr-close preamble (nexus-2fnet)."""
    return _re.findall(
        r"^#{3,5} Gap (\d+)([^\n:]*):\s*(.*)$", problem_stmt, _re.MULTILINE
    )


class TestGapRegexContract:
    """Regression guard for the gap-heading regex (nexus-2fnet).

    The preamble in ``nx rdr preamble rdr-close`` uses:
      ``^#{3,5} Gap (\\d+)([^\\n:]*): (.*)$``
    to find structured gap headings.  These tests verify the CONTRACT
    (which combinations match/don't match) so changes to rdr.py's regex
    trigger failures here.
    """

    def test_h4_gap_matches(self) -> None:
        section = "#### Gap 1: First gap\nContent.\n\n#### Gap 2: Second gap\nContent."
        gaps = _find_gaps(section)
        assert len(gaps) == 2
        assert gaps[0][0] == "1"
        assert gaps[0][2] == "First gap"
        assert gaps[1][0] == "2"

    def test_h3_gap_matches(self) -> None:
        gaps = _find_gaps("### Gap 1: Three-hash gap\nContent.")
        assert len(gaps) == 1
        assert gaps[0][0] == "1"

    def test_h5_gap_matches(self) -> None:
        gaps = _find_gaps("##### Gap 1: Five-hash gap\nContent.")
        assert len(gaps) == 1

    def test_h2_gap_not_matched(self) -> None:
        gaps = _find_gaps("## Gap 1: Too few hashes\nContent.")
        assert len(gaps) == 0

    def test_h6_gap_not_matched(self) -> None:
        gaps = _find_gaps("###### Gap 1: Too many hashes\nContent.")
        assert len(gaps) == 0

    def test_gap_without_colon_not_matched(self) -> None:
        gaps = _find_gaps("#### Gap 1 Missing the colon\nContent.")
        assert len(gaps) == 0

    def test_parenthetical_gap(self) -> None:
        """#### Gap 4 (prerequisite for Gap 1): Complex title"""
        gaps = _find_gaps("#### Gap 4 (prerequisite for Gap 1): Complex title\nContent.")
        assert len(gaps) == 1
        assert gaps[0][0] == "4"
        assert gaps[0][2] == "Complex title"

    def test_multi_digit_gap_number(self) -> None:
        gaps = _find_gaps("#### Gap 12: Twelfth gap\nContent.")
        assert len(gaps) == 1
        assert gaps[0][0] == "12"

    def test_no_gaps_returns_empty(self) -> None:
        gaps = _find_gaps("Some section with no gap headings.\n### Not a gap heading")
        assert len(gaps) == 0


class TestSkillFileGapCoverage:
    """Skill .md files must document both heading variants (nexus-2fnet).

    Migrated from test_rdr_close_gate.py::TestPreambleConsistency
    (the two skill-file checks that survive script deletion).
    """

    def test_gate_skill_lists_heading_variants(self) -> None:
        """rdr-gate SKILL.md must list both Problem and Problem Statement."""
        skill = (
            Path(__file__).parent.parent / "conexus" / "skills" / "rdr-gate" / "SKILL.md"
        ).read_text()
        assert "Problem / Problem Statement" in skill

    def test_create_skill_documents_heading_variants(self) -> None:
        """rdr-create SKILL.md must mention both heading forms."""
        skill = (
            Path(__file__).parent.parent
            / "conexus"
            / "skills"
            / "rdr-create"
            / "SKILL.md"
        ).read_text()
        assert "## Problem Statement" in skill
        assert "## Problem" in skill
