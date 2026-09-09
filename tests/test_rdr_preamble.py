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

import shutil
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


@pytest.fixture(scope="session")
def _rdr_git_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A single real ``git init`` reused by every ``rdr_env`` instance.

    test-suite-compression P0 (nexus-test-cleanup, 2026-08-05): the fixture's
    behavioral requirement is a directory where ``git rev-parse
    --show-toplevel`` resolves cleanly — a truly empty ``.git/`` marker
    directory does NOT satisfy that (git requires a real repo structure), so
    the fix keeps ONE real ``git init`` and fans it out via
    ``shutil.copytree`` per test instead of re-spawning the subprocess ~74
    times (T1 scratch 47337851: this was the second-largest slow-test
    contributor after the storage-boundary-lint AST-scan cache).
    """
    template = tmp_path_factory.mktemp("rdr_git_template")
    subprocess.run(
        ["git", "init", str(template)],
        check=True, capture_output=True,
    )
    return template


@pytest.fixture()
def rdr_env(tmp_path: Path, monkeypatch, _rdr_git_template: Path):
    """Hermetic environment: tmp git repo, default T2 path, cwd set to repo root.

    Returns a namespace-like dict:
      rdr_dir     -- Path to tmp_path/docs/rdr (created)
      db_path     -- Path to tmp_path/t2.db
      db          -- live T2Database (open for seeding, auto-closed via yield)
      repo_root   -- tmp_path (the fake git root)
    """
    # Copy the session-template .git/ into this test's tmp_path instead of
    # spawning a fresh `git init` subprocess per test — cheap (shutil,
    # in-process) and behaviorally identical: `git rev-parse
    # --show-toplevel` resolves against the copied .git/ exactly as it would
    # against a freshly-initialized one.
    shutil.copytree(_rdr_git_template / ".git", tmp_path / ".git")
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

    def test_rdr_list_no_rdr_dir(self, tmp_path, monkeypatch, _rdr_git_template):
        """No docs/rdr directory: exits without error, reports directory missing."""
        shutil.copytree(_rdr_git_template / ".git", tmp_path / ".git")
        monkeypatch.chdir(tmp_path)
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

    def test_rdr_create_no_rdr_dir(self, tmp_path, monkeypatch, _rdr_git_template):
        """No docs/rdr directory: bootstrap message and first ID."""
        shutil.copytree(_rdr_git_template / ".git", tmp_path / ".git")
        monkeypatch.chdir(tmp_path)
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

    def test_rdr_accept_names_the_residual_disposition_rule(self, rdr_env):
        """nexus-g7zgw.2: residuals a round-3+ gate recorded instead of blocking
        are dispositioned at accept."""
        _write_rdr(
            rdr_env["rdr_dir"], "rdr-204-example.md",
            {"title": "Example", "status": "draft", "type": "Architecture", "priority": "medium"},
            body="## Problem\n\nText.\n",
        )
        result = _runner().invoke(rdr, ["preamble", "rdr-accept", "--", "204"])
        assert result.exit_code == 0, result.output
        assert "`residuals:`" in result.output
        assert "blocks accept" in result.output

    def test_rdr_accept_names_the_fix_check_a_sha_disposition_carries(self, rdr_env):
        """A residual dispositioned by a change to the RDR file gets a fix check
        on that change; a residual dispositioned by a bead id does not."""
        _write_rdr(
            rdr_env["rdr_dir"], "rdr-204-example.md",
            {"title": "Example", "status": "draft", "type": "Architecture", "priority": "medium"},
            body="## Problem\n\nText.\n",
        )
        result = _runner().invoke(rdr, ["preamble", "rdr-accept", "--", "204"])
        assert result.exit_code == 0, result.output
        out = result.output
        assert "204-fix-check-<sha>" in out, "the T2 title the disposition's check goes under"
        assert "docs/rdr/rdr-204-example.md" in out, "the range names the RDR file"
        assert "tip" in out, "the sha is the RDR file's tip after the disposition"
        assert "bead id" in out and "needs none" in out, "the bead-disposition exemption"

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

    def test_rdr_accept_counts_numbered_approach_items(self, rdr_env):
        """nexus convention: numbered list under ## Approach (no ###
        subheadings) -> step_count == number of top-level items."""
        body = (
            "## Problem Statement\n\nProblem.\n\n"
            "## Approach\n\n"
            "1. **First step.** Do thing one.\n"
            "2. **Second step.** Do thing two.\n"
            "3. **Third step.** Do thing three.\n"
            "   1. A nested sub-item that must NOT be counted.\n"
            "4. **Fourth step.** Do thing four.\n\n"
            "## Tradeoffs\n\nSome tradeoffs."
        )
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-numbered-approach.md",
            {"title": "Numbered", "status": "draft", "type": "decision", "priority": "P1"},
            body=body,
        )
        result = _runner().invoke(rdr, ["preamble", "rdr-accept", "--", "1"])
        assert result.exit_code == 0, result.output
        assert "Step count detected:** 4" in result.output
        assert "Has plan section:** yes" in result.output

    def test_rdr_accept_open_status_is_accepted_synonym(self, rdr_env):
        """GH #1409 (nexus-qsryj): projects whose RDR convention never uses
        `draft` (open -> accepted lifecycle, e.g. MarkupEditorApp's 22 RDRs)
        must be able to accept an `open` RDR — the gate-PASSED check is the
        real guard, not the pre-accept status word."""
        body = (
            "## Problem Statement\n\nProblem.\n\n"
            "## Approach\n\n### Phase 1: Implement\nWork.\n\n"
            "## Tradeoffs\n\nSome."
        )
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-open-convention.md",
            {"title": "Open Convention", "status": "open", "type": "decision", "priority": "P1"},
            body=body,
        )
        result = _runner().invoke(rdr, ["preamble", "rdr-accept", "--", "1"])
        assert result.exit_code == 0, result.output
        assert "BLOCKED" not in result.output
        assert "Planning Handoff" in result.output

    def test_rdr_accept_no_arg_lists_open_rdrs_too(self, rdr_env):
        """The no-id eligibility table includes `open` RDRs (GH #1409)."""
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-open-one.md",
            {"title": "Open One", "status": "open", "type": "decision", "priority": "P1"},
        )
        result = _runner().invoke(rdr, ["preamble", "rdr-accept"])
        assert result.exit_code == 0, result.output
        assert "Open One" in result.output

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


# RDR-201 P1.5 (nexus-j9z30.5) derivation-proof coverage for the
# accept/close preamble guards moved to tests/test_rdr_set_status.py — that
# module is hermetic (no T2Database/service substrate), matching this
# bead's instructed test surface; this file's ``rdr_env`` fixture
# constructs a live T2Database and is out of scope here.


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

    def test_rdr_research_t2_rows_matched_despite_listing_prefix(
        self, rdr_env, monkeypatch,
    ) -> None:
        """Regression (2026-07-22, caught on RDR-188): `nx memory list`
        rows are "[id] <project>/<title>  (…)" — the old ^-anchored
        title regex matched NOTHING, so every preamble reported "No
        research findings recorded" while T2 held them."""
        import subprocess as _sp

        import nexus.commands.rdr as rdr_mod

        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-hello-world.md",
            {"title": "Hello World", "status": "draft", "type": "decision", "priority": "P1"},
        )
        real_run = _sp.run

        def _fake_run(cmd, *a, **k):
            if cmd[:3] == ["nx", "memory", "list"]:
                return _sp.CompletedProcess(
                    cmd, 0,
                    stdout="[21044] nexus_rdr/1-research-1: canned finding  (-, 2026-07-22T00:00:00Z)\n"
                           "[21042] nexus_rdr/1  (-, 2026-07-22T00:00:00Z)\n",
                    stderr="",
                )
            return _sp.CompletedProcess(cmd, 1, stdout="", stderr="unavailable")

        monkeypatch.setattr(rdr_mod.subprocess, "run", _fake_run)
        result = _runner().invoke(rdr, ["preamble", "rdr-research", "--", "1"])
        assert result.exit_code == 0, result.output
        assert "1-research-1: canned finding" in result.output
        assert "No research findings recorded" not in result.output

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


class _FakeT2ResearchClient:
    """In-memory T2 double for ``rdr-research add`` — matches the
    ``get_all(project=...)`` / ``get(project=, title=)`` / ``put(project=,
    title=, content=, ...)`` contract ``T2Database`` exposes (RDR-201 P1.4
    convention, see ``_FakeT2CensusClient`` in test_rdr_audit_vocabulary.py).

    ``hidden_from_get_all`` lets a test simulate a stale scan: a title
    present in ``get()`` (so a collision check still finds it) but absent
    from ``get_all()`` (so the seq-scan doesn't see it) — the exact race
    window nexus-zu1q0 exploited.
    """

    def __init__(
        self,
        entries: dict[str, str] | None = None,
        hidden_from_get_all: frozenset[str] = frozenset(),
    ) -> None:
        self._store: dict[str, str] = dict(entries or {})
        self._hidden = hidden_from_get_all
        self.put_calls: list[tuple[str, str]] = []

    def __enter__(self) -> "_FakeT2ResearchClient":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def get_all(self, project: str | None = None) -> list[dict]:
        return [
            {"title": t, "content": c}
            for t, c in self._store.items()
            if t not in self._hidden
        ]

    def get(self, project: str | None = None, title: str | None = None, id: int | None = None):
        content = self._store.get(title)
        return None if content is None else {"title": title, "content": content}

    def put(
        self,
        project: str,
        title: str,
        content: str,
        tags: str = "",
        ttl: int | None = 30,
        agent: str | None = None,
        session: str | None = None,
    ) -> int:
        self.put_calls.append((title, content))
        self._store[title] = content
        return len(self._store)


class TestRdrResearchAdd:
    """Tests for ``nx rdr preamble rdr-research -- add <id> <text>``
    (nexus-zu1q0): the next sequence number must be derived from existing
    ``<id>-research-*`` titles, and an add must never silently upsert over
    an existing title."""

    def test_add_writes_seq_1_when_no_prior_findings(self, monkeypatch):
        import nexus.commands.rdr as rdr_mod

        fake = _FakeT2ResearchClient()
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)

        result = _runner().invoke(
            rdr, ["preamble", "rdr-research", "--", "add", "201", "first", "finding"]
        )
        assert result.exit_code == 0, result.output
        assert "201-research-1" in result.output
        assert fake.put_calls == [("201-research-1", fake._store["201-research-1"])]
        assert "first finding" in fake._store["201-research-1"]

    def test_add_with_unpadded_id_finds_zero_padded_legacy_titles(self, monkeypatch):
        """Live titles for RDRs below 100 are zero-padded (``097-research-9``).
        An unpadded ``97`` must join that sequence, never fork a bare
        ``97-research-1`` namespace beside it (critique of nexus-zu1q0)."""
        import nexus.commands.rdr as rdr_mod

        fake = _FakeT2ResearchClient(entries={"097-research-9": "finding: ninth\n"})
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)

        result = _runner().invoke(
            rdr, ["preamble", "rdr-research", "--", "add", "97", "tenth", "finding"]
        )
        assert result.exit_code == 0, result.output
        assert "097-research-10" in result.output
        assert "97-research-1" not in fake._store
        assert "tenth finding" in fake._store["097-research-10"]

    def test_next_seq_sees_titles_with_a_summary_suffix(self):
        """Live T2 titles read "204-research-16: <summary>"; the scan must
        count them, or the next add overwrites nothing and restarts at 1."""
        from nexus.commands.rdr import _rdr_research_next_seq

        rows = [{"title": "204-research-16: the Key Discoveries bullet"}, {"title": "204-research-3"}]
        assert _rdr_research_next_seq(rows, "204") == 17

    def test_add_advances_past_existing_seq(self, monkeypatch):
        """A prior 201-research-1 entry means the next add lands on seq 2 —
        the second finding's content is never lost by upserting seq 1 again."""
        import nexus.commands.rdr as rdr_mod

        fake = _FakeT2ResearchClient(entries={"201-research-1": "finding: first\n"})
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)

        result = _runner().invoke(
            rdr, ["preamble", "rdr-research", "--", "add", "201", "second", "finding"]
        )
        assert result.exit_code == 0, result.output
        assert "201-research-2" in result.output
        assert "first" in fake._store["201-research-1"]
        assert "second finding" in fake._store["201-research-2"]

    def test_two_consecutive_invocations_never_collide(self, monkeypatch):
        """The exact repro shape from nexus-zu1q0: two back-to-back `add`
        calls against the same RDR each claim a distinct sequence number,
        sharing one client across both invocations (as two consecutive
        real CLI calls would)."""
        import nexus.commands.rdr as rdr_mod

        fake = _FakeT2ResearchClient()
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)

        r1 = _runner().invoke(
            rdr, ["preamble", "rdr-research", "--", "add", "201", "finding", "one"]
        )
        r2 = _runner().invoke(
            rdr, ["preamble", "rdr-research", "--", "add", "201", "finding", "two"]
        )
        assert r1.exit_code == 0, r1.output
        assert r2.exit_code == 0, r2.output
        assert "201-research-1" in r1.output
        assert "201-research-2" in r2.output
        assert "finding one" in fake._store["201-research-1"]
        assert "finding two" in fake._store["201-research-2"]

    def test_never_overwrites_when_computed_title_already_exists(self, monkeypatch):
        """A stale seq-scan that misses a concurrently-created seq-2 must not
        cause the write to upsert over it — the command advances to seq 3."""
        import nexus.commands.rdr as rdr_mod

        fake = _FakeT2ResearchClient(
            entries={
                "201-research-1": "finding: first\n",
                "201-research-2": "finding: concurrent\n",
            },
            hidden_from_get_all=frozenset({"201-research-2"}),
        )
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)

        result = _runner().invoke(
            rdr, ["preamble", "rdr-research", "--", "add", "201", "third", "finding"]
        )
        assert result.exit_code == 0, result.output
        assert "201-research-3" in result.output
        assert "concurrent" in fake._store["201-research-2"], (
            "existing seq-2 entry must not be overwritten"
        )
        assert "third finding" in fake._store["201-research-3"]

    def test_add_with_only_id_falls_through_to_context(
        self, tmp_path, monkeypatch, _rdr_git_template,
    ):
        """`add <id>` with no finding text is unchanged: still prints RDR
        context rather than attempting a T2 write (regression guard for
        test_rdr_research_double_dash_passthrough_with_subcommand_word).

        Deliberately avoids ``rdr_env``: this path never constructs a T2
        client in-process (only shells out to ``nx memory list``, caught
        and reported on failure), so it needs no engine substrate — same
        no-T2-construction shape as ``test_rdr_list_no_rdr_dir`` above.
        """
        shutil.copytree(_rdr_git_template / ".git", tmp_path / ".git")
        monkeypatch.chdir(tmp_path)
        rdr_dir = tmp_path / "docs" / "rdr"
        rdr_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path", lambda: tmp_path / "t2.db"
        )
        _write_rdr(
            rdr_dir,
            "rdr-001-hello-world.md",
            {"title": "Hello World", "status": "draft", "type": "decision", "priority": "P1"},
        )
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

    def test_phase_review_gate_refuses_a_subset_when_an_item_start_fails_to_parse(self, rdr_env):
        """GH #1443: a `5a.` item used to be absorbed into item 5 and the gate
        enumerated 2 of 3 items; now it refuses with the offending line."""
        body = (
            "## Problem Statement\n\nProblem.\n\n"
            "### Approach\n\n"
            "1. **T2 read**: Read from T2 database.\n"
            "1a. **T2 lease**: added after drafting.\n"
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
            ["preamble", "phase-review-gate", "--", "130", "--phase", "1", "--evidence", "Item1=nexus-aaaa,Item2=nexus-bbbb"],
        )
        assert result.exit_code == 0, result.output
        assert "ERROR" in result.output
        assert "GH #1443" in result.output
        assert "1a. **T2 lease**" in result.output
        assert "CROSS-WALK PASSED" not in result.output
        assert "| # | Label | Evidence needed |" not in result.output

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

    def test_implementation_plan_heading_pass1_enumerates(self, rdr_env):
        """RDRs that structure phased work under '## Implementation Plan'
        (e.g. conexus RDR-001) must be recognised, not error out (nexus-2pw1x)."""
        body = (
            "## Problem Statement\n\nProblem.\n\n"
            "## Implementation Plan\n\n"
            "1. **Schema slice**: Add the retention column.\n"
            "2. **ETL passthrough**: Relax the null-doc skip.\n\n"
            "## Tradeoffs\n\nSome tradeoffs."
        )
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-multitenant-cloud.md",
            {"title": "Multitenant Cloud", "status": "accepted",
             "type": "architecture", "priority": "P1"},
            body=body,
        )
        result = _runner().invoke(
            rdr,
            ["preamble", "phase-review-gate", "--", "1", "--phase", "1"],
        )
        assert result.exit_code == 0, result.output
        assert "ERROR" not in result.output
        assert "§Approach Cross-Walk" in result.output
        assert "Item1" in result.output
        assert "Schema slice" in result.output
        assert "Item2" in result.output
        assert "ETL passthrough" in result.output

    def test_implementation_plan_heading_pass2_validates_evidence(self, rdr_env):
        """Pass 2 cross-walk works for '## Implementation Plan' RDRs (nexus-2pw1x)."""
        body = (
            "## Problem Statement\n\nProblem.\n\n"
            "## Implementation Plan\n\n"
            "1. **Schema slice**: Add the retention column.\n"
            "2. **ETL passthrough**: Relax the null-doc skip.\n\n"
            "## Tradeoffs\n\nSome tradeoffs."
        )
        _write_rdr(
            rdr_env["rdr_dir"],
            "rdr-001-multitenant-cloud.md",
            {"title": "Multitenant Cloud", "status": "accepted",
             "type": "architecture", "priority": "P1"},
            body=body,
        )
        result = _runner().invoke(
            rdr,
            [
                "preamble", "phase-review-gate", "--", "1", "--phase", "1",
                "--evidence", "Item1=nexus-abc1,Item2=nexus-xyz2",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "APPROACH CROSS-WALK PASSED" in result.output
        assert "nexus-abc1" in result.output
        assert "nexus-xyz2" in result.output


class TestApproachSectionExtractor:
    """Unit tests for _prg_extract_approach_section synonym recognition (nexus-2pw1x)."""

    def test_extracts_implementation_plan_heading(self):
        from nexus.commands.rdr import _prg_extract_approach_section
        text = "## Intro\n\nx.\n\n## Implementation Plan\n\nbody line.\n\n## Next\n\ny."
        assert _prg_extract_approach_section(text).strip() == "body line."

    def test_extracts_phases_heading(self):
        from nexus.commands.rdr import _prg_extract_approach_section
        text = "## Intro\n\nx.\n\n### Phases\n\nbody line.\n\n### Next\n\ny."
        assert _prg_extract_approach_section(text).strip() == "body line."

    def test_extracts_plain_plan_heading_case_insensitive(self):
        from nexus.commands.rdr import _prg_extract_approach_section
        text = "## Intro\n\nx.\n\n## plan\n\nbody line.\n\n## Next\n\ny."
        assert _prg_extract_approach_section(text).strip() == "body line."

    def test_approach_still_extracted(self):
        from nexus.commands.rdr import _prg_extract_approach_section
        text = "## Intro\n\nx.\n\n### Approach\n\nbody line.\n\n## Next\n\ny."
        assert _prg_extract_approach_section(text).strip() == "body line."

    def test_no_recognised_heading_returns_empty(self):
        from nexus.commands.rdr import _prg_extract_approach_section
        text = "## Intro\n\nx.\n\n## Tradeoffs\n\ny."
        assert _prg_extract_approach_section(text) == ""

    def test_plan_prefix_words_do_not_match(self):
        """Bare 'Plan' must not match 'Planned'/'Planning'/'Planner' (prefix)."""
        from nexus.commands.rdr import _prg_extract_approach_section
        for heading in ("## Planned Work", "## Planning Notes", "### Planner Design"):
            text = f"## Intro\n\nx.\n\n{heading}\n\nbody.\n\n## Next\n\ny."
            assert _prg_extract_approach_section(text) == "", heading

    def test_plan_with_extra_words_does_not_match(self):
        """'## Plan Optimization' is a differently-scoped section, not the
        phase plan — bare 'Plan' matches only as the whole heading name."""
        from nexus.commands.rdr import _prg_extract_approach_section
        text = "## Intro\n\nx.\n\n## Plan Optimization\n\nbody.\n\n## Next\n\ny."
        assert _prg_extract_approach_section(text) == ""

    def test_approach_with_trailing_text_still_matches(self):
        """Suffix tolerance preserved for Approach/Implementation Plan/Phases."""
        from nexus.commands.rdr import _prg_extract_approach_section
        text = "## Intro\n\nx.\n\n### Approach (two tracks)\n\nbody line.\n\n## Next\n\ny."
        assert _prg_extract_approach_section(text).strip() == "body line."

    def test_extracts_proposed_approach_heading(self):
        """'## Proposed Approach' is the most common phrasing (RDR-176) and must
        be recognised — the 'Proposed' prefix previously defeated the matcher."""
        from nexus.commands.rdr import _prg_extract_approach_section
        text = (
            "## Intro\n\nx.\n\n## Proposed Approach (pillars)\n\n"
            "body line.\n\n## Next\n\ny."
        )
        assert _prg_extract_approach_section(text).strip() == "body line."

    def test_extracts_proposed_plan_heading(self):
        from nexus.commands.rdr import _prg_extract_approach_section
        text = "## Intro\n\nx.\n\n### Proposed Plan\n\nbody line.\n\n## Next\n\ny."
        assert _prg_extract_approach_section(text).strip() == "body line."

    def test_proposed_solution_does_not_match(self):
        """'Proposed' must only license Approach/Plan synonyms, not 'Proposed
        Solution' (a differently-scoped section in many RDRs)."""
        from nexus.commands.rdr import _prg_extract_approach_section
        text = "## Intro\n\nx.\n\n## Proposed Solution\n\nbody.\n\n## Next\n\ny."
        assert _prg_extract_approach_section(text) == ""


class TestPrgUnparsedItemStarts:
    """GH #1443: the three §Approach shapes the item regex misses must be
    reported, never absorbed into the previous item."""

    def test_clean_numbered_list_has_no_unparsed_lines(self):
        from nexus.commands.rdr import _prg_find_unparsed_item_starts  # noqa: PLC0415 — deferred, matches the file's other in-test imports
        text = (
            "1. **T2 read**: read from T2.\n"
            "   continuation prose of item one\n"
            "2. **File fallback**: fall back to files.\n"
            "- a sub bullet\n"
        )
        assert _prg_find_unparsed_item_starts(text) == []

    def test_non_integer_item_number_is_reported(self):
        from nexus.commands.rdr import _prg_find_unparsed_item_starts, _prg_parse_approach_items  # noqa: PLC0415 — deferred, matches the file's other in-test imports
        text = (
            "5. **Daemon**: stand it up.\n"
            "5a. **Daemon lease**: added after drafting.\n"
            "6. **Routes**: wire reads.\n"
        )
        assert [n for n, _, _ in _prg_parse_approach_items(text)] == [5, 6]
        assert _prg_find_unparsed_item_starts(text) == ["5a. **Daemon lease**: added after drafting."]

    def test_wrapped_bold_label_is_reported(self):
        from nexus.commands.rdr import _prg_find_unparsed_item_starts  # noqa: PLC0415 — deferred, matches the file's other in-test imports
        text = (
            "1. **Short**: fine.\n"
            "2. **A label long enough that the author wrapped it\n"
            "   onto the next line**: description.\n"
        )
        assert _prg_find_unparsed_item_starts(text) == [
            "2. **A label long enough that the author wrapped it",
        ]

    def test_label_on_the_following_line_is_reported(self):
        from nexus.commands.rdr import _prg_find_unparsed_item_starts  # noqa: PLC0415 — deferred, matches the file's other in-test imports
        text = "1. **First**: fine.\n2.\n**Second**: label below its number.\n"
        assert _prg_find_unparsed_item_starts(text) == ["2."]

    def test_phase_block_headers_are_not_item_starts(self):
        from nexus.commands.rdr import _prg_find_unparsed_item_starts  # noqa: PLC0415 — deferred, matches the file's other in-test imports
        text = "**Phase 0: Scaffolding**\n- bullet\n**Phase 1: Core**\n- bullet\n"
        assert _prg_find_unparsed_item_starts(text) == []


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


class TestRdrGateRegateBlock:
    """nexus-7vdf9: after a BLOCKED round, ``rdr-gate`` leads with the prior
    critique's findings and the survivor-sweep instruction; a first gate or a
    re-gate after a pass prints nothing extra; an unreachable T2 is a named
    note, never silence."""

    _BODY = (
        "## Problem Statement\n\n#### Gap 1: a gap\nText.\n\n"
        "## Proposed Solution\n\nSix parse sites.\n"
    )

    def _write(self, rdr_env):
        _write_rdr(
            rdr_env["rdr_dir"], "rdr-204-example.md",
            {"title": "Example", "status": "draft", "type": "Architecture", "priority": "medium"},
            body=self._BODY,
        )

    def test_blocked_prior_gate_prints_findings_and_layer_zero(self, rdr_env, monkeypatch):
        import nexus.commands.rdr as rdr_mod

        self._write(rdr_env)
        fake = _FakeT2ResearchClient({
            "204-gate-latest": (
                "outcome: \"BLOCKED\"\ndate: \"2026-09-07\"\ncritical_count: 2\n"
                "summary: \"two survivors\"\ncritique: nexus_rdr/204-gate-critique-2026-09-07c [24809]\n"
            ),
            "204-gate-critique-2026-09-07c": (
                "# Critique\n\n**Critical 1**: ghost sweep omits topic_assignments.\n"
                "- Significant: Phase 1 item 4 still says nx config set reminds the user.\n"
                "NEW CRITICAL\n\nIssue: the sweep is narrower than collectionIsEmpty.\n"
                "Observation: fine.\n"
            ),
        })
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        result = _runner().invoke(rdr, ["preamble", "rdr-gate", "--", "204"])
        assert result.exit_code == 0, result.output
        out = result.output
        assert "Re-gate: the previous gate was BLOCKED" in out
        assert "204-gate-critique-2026-09-07c" in out
        assert "ghost sweep omits topic_assignments" in out
        assert "nx config set reminds the user" in out
        assert "NEW CRITICAL" in out and "narrower than collectionIsEmpty" in out
        assert "Prior findings" in out
        assert "Observation: fine" not in out, "observations are not survivors to sweep"
        assert "Layer 0 (survivor sweep" in out
        assert out.index("Re-gate:") < out.index("Section Structure")

    def test_absent_prior_gate_prints_nothing_extra(self, rdr_env, monkeypatch):
        import nexus.commands.rdr as rdr_mod

        self._write(rdr_env)
        fake = _FakeT2ResearchClient({})
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        result = _runner().invoke(rdr, ["preamble", "rdr-gate", "--", "204"])
        assert result.exit_code == 0, result.output
        assert "Re-gate" not in result.output
        assert "Fix check" not in result.output

    def test_passed_prior_gate_still_prints_the_block(self, rdr_env, monkeypatch):
        """nexus-g7zgw.4: Layer 0 fires after a PASSED gate too. Both RDR-204
        rounds that introduced new Criticals were fixes authored against a
        PASSED gate's Significants, with the sweep structurally off."""
        import nexus.commands.rdr as rdr_mod

        self._write(rdr_env)
        fake = _FakeT2ResearchClient({
            "204-gate-latest": (
                "outcome: \"PASSED\"\ndate: \"2026-09-07\"\nsignificant_count: 2\n"
                "critique: nexus_rdr/204-gate-critique-2026-09-07f\n"
            ),
            "204-gate-critique-2026-09-07f": "- Significant: the walk sentence overstates.\n",
        })
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        result = _runner().invoke(rdr, ["preamble", "rdr-gate", "--", "204"])
        assert result.exit_code == 0, result.output
        out = result.output
        assert "Re-gate: the previous gate was PASSED" in out
        assert "the walk sentence overstates" in out
        assert "Layer 0 (survivor sweep" in out

    def test_missing_critique_pointer_says_so(self, rdr_env, monkeypatch):
        import nexus.commands.rdr as rdr_mod

        self._write(rdr_env)
        fake = _FakeT2ResearchClient({"204-gate-latest": "outcome: \"BLOCKED\"\ndate: \"2026-09-07\"\n"})
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        result = _runner().invoke(rdr, ["preamble", "rdr-gate", "--", "204"])
        assert "No `critique:` pointer" in result.output
        assert "Layer 0 (survivor sweep" in result.output

    def test_missing_critique_record_is_named_not_mislabelled(self, rdr_env, monkeypatch):
        """A pointer to a record that does not exist must say so, never
        'loaded but nothing recognised' (critique [24815] Significant 1)."""
        import nexus.commands.rdr as rdr_mod

        self._write(rdr_env)
        fake = _FakeT2ResearchClient({"204-gate-latest": (
            "outcome: \"BLOCKED\"\ndate: \"2026-09-07\"\ncritique: nexus_rdr/204-gate-critique-missing\n")})
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        result = _runner().invoke(rdr, ["preamble", "rdr-gate", "--", "204"])
        assert "no such T2 record was found" in result.output
        assert "nothing recognised" not in result.output

    def test_bad_gated_commit_is_reported_not_rendered_as_no_changes(self, rdr_env, monkeypatch):
        import nexus.commands.rdr as rdr_mod

        self._write(rdr_env)
        fake = _FakeT2ResearchClient({"204-gate-latest": (
            "outcome: \"BLOCKED\"\ndate: \"2026-09-07\"\ncommit: deadbeef0\n")})
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        result = _runner().invoke(rdr, ["preamble", "rdr-gate", "--", "204"])
        assert "Changed since the gated commit `deadbeef0`: unknown" in result.output
        assert "no changes to the RDR file" not in result.output

    def test_unreachable_t2_is_a_named_note(self, rdr_env, monkeypatch):
        import nexus.commands.rdr as rdr_mod

        self._write(rdr_env)

        class _Boom:
            def __enter__(self):
                raise ConnectionError("engine down")

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: _Boom())
        result = _runner().invoke(rdr, ["preamble", "rdr-gate", "--", "204"])
        assert result.exit_code == 0, result.output
        assert "T2 unreachable" in result.output and "engine down" in result.output
        assert "Section Structure" in result.output, "the rest of the preamble still prints"


class TestRdrGateRoundAndFixCheck:
    """nexus-g7zgw.1 / .2: the re-gate block carries the gate round number,
    derived from the gate record's ``prior:`` chain, and a Fix check section
    naming the exact diff range whenever the RDR changed since the gated
    commit. The fix check is what stands between a fix commit and Layer 3."""

    _BODY = "## Problem Statement\n\n#### Gap 1: a gap\nText.\n\n## Proposed Solution\n\nSix sites.\n"

    def _commit(self, rdr_env, body: str, msg: str) -> str:
        path = _write_rdr(
            rdr_env["rdr_dir"], "rdr-204-example.md",
            {"title": "Example", "status": "draft", "type": "Architecture", "priority": "medium"},
            body=body,
        )
        root = str(rdr_env["repo_root"])
        subprocess.run(["git", "-C", root, "add", str(path)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg],
            check=True, capture_output=True,
        )
        return subprocess.run(
            ["git", "-C", root, "log", "-1", "--format=%h"], check=True, capture_output=True, text=True,
        ).stdout.strip()

    def _gate(self, rdr_env, monkeypatch, *, outcome: str, commit: str, prior: str | None):
        import nexus.commands.rdr as rdr_mod

        content = f"outcome: \"{outcome}\"\ndate: \"2026-09-07\"\ncommit: {commit}\n"
        if prior is not None:
            content += f"prior: {prior}\n"
        fake = _FakeT2ResearchClient({"204-gate-latest": content})
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        return _runner().invoke(rdr, ["preamble", "rdr-gate", "--", "204"])

    def test_round_number_counts_the_prior_chain(self, rdr_env, monkeypatch):
        sha = self._commit(rdr_env, self._BODY, "gated")
        prior = "[24847] (PASSED 0C 2S 2O), [24844] (BLOCKED 1C 2S 2O), [24841] (PASSED 0C 3S 4O)"
        result = self._gate(rdr_env, monkeypatch, outcome="BLOCKED", commit=sha, prior=prior)
        assert result.exit_code == 0, result.output
        assert "Gate round 5" in result.output, result.output
        assert "prior rounds: 4 (2 BLOCKED, 2 PASSED, 0 unlabelled" in result.output

    def test_round_number_counts_bare_and_wrapped_chains(self, rdr_env, monkeypatch):
        """deep critique [24873] Critical 1: entries without an outcome word
        and chains wrapped over lines must still count."""
        from nexus.commands.rdr import _gate_round_lines

        bare = "outcome: \"BLOCKED\"\nprior: [1], [2], [3], [4], [5], [6], [7], [8], [9]\n"
        assert "Gate round 11" in _gate_round_lines(bare)[0]
        assert "9 unlabelled" in _gate_round_lines(bare)[0]
        wrapped = ("outcome: \"PASSED\"\nprior: [1] (BLOCKED 1C), [2] (PASSED 0C),\n"
                   "  [3] (BLOCKED 2C), [4] (PASSED)\ncommit: abc1234\n")
        line = _gate_round_lines(wrapped)[0]
        assert "Gate round 6" in line and "2 BLOCKED, 3 PASSED, 0 unlabelled" in line

    def test_round_number_prefers_the_critique_record_count(self, rdr_env, monkeypatch):
        """A hand-retyped chain that lost entries cannot reset the cap: the
        critique records T2 holds are the count nobody retypes."""
        import nexus.commands.rdr as rdr_mod

        sha = self._commit(rdr_env, self._BODY, "gated")
        fake = _FakeT2ResearchClient({
            "204-gate-latest": f"outcome: \"BLOCKED\"\ndate: \"2026-09-07\"\ncommit: {sha}\n",
            **{f"204-gate-critique-2026-09-0{i}": "x" for i in range(1, 6)},
        })
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        out = _runner().invoke(rdr, ["preamble", "rdr-gate", "--", "204"]).output
        assert "Gate round 6" in out and "from the critique records" in out, out

    def test_regate_without_fix_check_field_is_flagged(self, rdr_env, monkeypatch):
        """deep critique [24873] Critical 3: omitting fix_check: on a re-gate
        is a skipped check, never a clean one."""
        import nexus.commands.rdr as rdr_mod

        sha = self._commit(rdr_env, self._BODY, "gated")
        for content, flagged in (
            (f"outcome: \"PASSED\"\ncommit: {sha}\nprior: [1] (BLOCKED 1C)\n", True),
            (f"outcome: \"PASSED\"\ncommit: {sha}\n", False),
            (f"outcome: \"PASSED\"\ncommit: {sha}\nprior: [1] (BLOCKED 1C)\nfix_check: none (no change since {sha})\n", False),
        ):
            fake = _FakeT2ResearchClient({"204-gate-latest": content})
            monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda fake=fake: fake)
            out = _runner().invoke(rdr, ["preamble", "rdr-gate", "--", "204"]).output
            assert ("Fix check missing" in out) is flagged, (content, out)

    def test_fix_check_pointer_to_absent_record_is_flagged(self, rdr_env, monkeypatch):
        import nexus.commands.rdr as rdr_mod

        sha = self._commit(rdr_env, self._BODY, "gated")
        rec = f"outcome: \"PASSED\"\ncommit: {sha}\nprior: [1] (BLOCKED 1C)\nfix_check: nexus_rdr/204-fix-check-{sha}\n"
        fake = _FakeT2ResearchClient({"204-gate-latest": rec})
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        assert "Fix check record missing" in _runner().invoke(rdr, ["preamble", "rdr-gate", "--", "204"]).output
        fake = _FakeT2ResearchClient({"204-gate-latest": rec, f"204-fix-check-{sha}": "verdict: CLEAN\n"})
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        out = _runner().invoke(rdr, ["preamble", "rdr-gate", "--", "204"]).output
        assert "Fix check record missing" not in out and "Fix check" in out

    def test_round_number_without_prior_field_is_two(self, rdr_env, monkeypatch):
        sha = self._commit(rdr_env, self._BODY, "gated")
        result = self._gate(rdr_env, monkeypatch, outcome="BLOCKED", commit=sha, prior=None)
        assert "Gate round 2" in result.output, result.output

    def test_round_three_or_later_names_the_residual_rule(self, rdr_env, monkeypatch):
        sha = self._commit(rdr_env, self._BODY, "gated")
        result = self._gate(rdr_env, monkeypatch, outcome="BLOCKED", commit=sha, prior="[1] (BLOCKED 1C)")
        assert "Gate round 3" in result.output
        assert "only a ship-blocker blocks" in result.output
        early = self._gate(rdr_env, monkeypatch, outcome="BLOCKED", commit=sha, prior=None)
        assert "only a ship-blocker blocks" not in early.output

    def test_fix_check_names_the_diff_range_when_the_file_changed(self, rdr_env, monkeypatch):
        gated = self._commit(rdr_env, self._BODY, "gated")
        fixed = self._commit(rdr_env, self._BODY + "\nFive registration sites, not one.\n", "fix")
        result = self._gate(rdr_env, monkeypatch, outcome="PASSED", commit=gated, prior=None)
        out = result.output
        assert "### Fix check (required before Layer 1)" in out, out
        assert f"git diff {gated}..HEAD -- docs/rdr/rdr-204-example.md" in out
        assert f"fix" in out and fixed in out, "the fix commits are listed"
        assert f"204-fix-check-{fixed}" in out, "the T2 title carries the tip sha"
        assert "enumeration" in out and "universal" in out
        assert "Do not enter Layer 1 or Layer 3" in out

    def test_no_regate_fix_check_past_the_gate(self, rdr_env, monkeypatch):
        """Past the gate there is no re-gate to gate, and the branch says so —
        but a residual dispositioned by a change to the RDR file still carries a
        fix check on that change, and this surface names it."""
        gated = self._commit(rdr_env, self._BODY, "gated")
        path = _write_rdr(
            rdr_env["rdr_dir"], "rdr-204-example.md",
            {"title": "Example", "status": "accepted", "type": "Architecture", "priority": "medium"},
            body=self._BODY,
        )
        root = str(rdr_env["repo_root"])
        subprocess.run(["git", "-C", root, "add", str(path)], check=True, capture_output=True)
        subprocess.run(["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "accept"], check=True, capture_output=True)
        out = self._gate(rdr_env, monkeypatch, outcome="PASSED", commit=gated, prior=None).output
        assert "past the gate" in out and "`accepted`" in out, out
        assert "### Fix check (required" not in out, "no re-gate fix check past the gate"
        assert "204-fix-check-<sha>" in out, "the disposition's own fix check is named"
        assert f"git diff {gated}..HEAD -- docs/rdr/rdr-204-example.md" in out, out
        assert "bead id" in out and "needs none" in out, "the bead-disposition exemption"

    def test_fix_check_not_required_when_nothing_changed(self, rdr_env, monkeypatch):
        gated = self._commit(rdr_env, self._BODY, "gated")
        result = self._gate(rdr_env, monkeypatch, outcome="PASSED", commit=gated, prior=None)
        out = result.output
        assert "Fix check: not required" in out, out
        assert f"no change to the RDR file since `{gated}`" in out
        assert "### Fix check (required" not in out

    def test_fix_check_pointer_mismatch_is_flagged(self, rdr_env, monkeypatch):
        """critique [24865] Critical 1: `fix_check:` must name `commit:`'s sha."""
        import nexus.commands.rdr as rdr_mod

        gated = self._commit(rdr_env, self._BODY, "gated")
        for field, flagged in (
            (f"nexus_rdr/204-fix-check-df91f4072 (CLEAN)", True),
            (f"nexus_rdr/204-fix-check-{gated}", False),
            (gated, False),
        ):
            fake = _FakeT2ResearchClient({"204-gate-latest": (
                f"outcome: \"PASSED\"\ndate: \"2026-09-07\"\ncommit: {gated}\nfix_check: {field}\n")})
            monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda fake=fake: fake)
            out = _runner().invoke(rdr, ["preamble", "rdr-gate", "--", "204"]).output
            assert ("Fix check pointer mismatch" in out) is flagged, (field, out)

    def test_fix_check_git_log_failure_never_yields_a_fake_sha(self, rdr_env, monkeypatch):
        """code review [24866] Important 3: a failed `git log` must not print
        `HEAD` as the tip sha the T2 title is keyed on."""
        from nexus.commands.rdr import _fix_check_lines

        root = str(rdr_env["repo_root"])
        lines = _fix_check_lines(
            repo_root=root, t2_key="204", rel="docs/rdr/nope.md",
            gated_commit="0000000", changed=True,
        )
        joined = "\n".join(lines)
        assert "fix-check-HEAD" not in joined
        assert "could not be read" in joined

    def test_regate_block_carries_the_critics_sites_lines(self, rdr_env, monkeypatch):
        """code review [24866] Important 1: the Sites: line is what Layer 0 sweeps."""
        import nexus.commands.rdr as rdr_mod

        sha = self._commit(rdr_env, self._BODY, "gated")
        fake = _FakeT2ResearchClient({
            "204-gate-latest": (
                f"outcome: \"BLOCKED\"\ndate: \"2026-09-07\"\ncommit: {sha}\n"
                "critique: nexus_rdr/204-gate-critique-2026-09-07h\n"
            ),
            "204-gate-critique-2026-09-07h": (
                "## Critical Issues\n\n### Issue: model_version never parsed\n"
                "- **Location**: L471\n- **Sites**: L471, L254-257, L612\n"
                "- **Recommendation**: cite indexer.py:790\n"
            ),
        })
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        out = _runner().invoke(rdr, ["preamble", "rdr-gate", "--", "204"]).output
        assert "Sites: L471, L254-257, L612" in out, out

    def test_fix_check_with_unresolvable_commit_is_reported(self, rdr_env, monkeypatch):
        self._commit(rdr_env, self._BODY, "gated")
        result = self._gate(rdr_env, monkeypatch, outcome="PASSED", commit="deadbeef0", prior=None)
        assert "Fix check: the gated commit `deadbeef0` does not resolve" in result.output


class TestRdrFixPreamble:
    """nexus-zbdm0: the fix step's own surface. ``nx rdr preamble rdr-fix <id>``
    prints the latest gate's findings with their Sites, the diff and fix
    commits since the gated commit, the pre-edit research title, and the
    fix rules, at the moment an author sits down to fix."""

    _BODY = "## Problem Statement\n\n#### Gap 1: a gap\nText.\n\n## Proposed Solution\n\nSix sites.\n"

    def _commit(self, rdr_env, body: str, msg: str) -> str:
        path = _write_rdr(
            rdr_env["rdr_dir"], "rdr-204-example.md",
            {"title": "Example", "status": "draft", "type": "Architecture", "priority": "medium"},
            body=body,
        )
        root = str(rdr_env["repo_root"])
        subprocess.run(["git", "-C", root, "add", str(path)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg],
            check=True, capture_output=True,
        )
        return subprocess.run(
            ["git", "-C", root, "log", "-1", "--format=%h"], check=True, capture_output=True, text=True,
        ).stdout.strip()

    def test_no_gate_record_says_nothing_to_fix(self, rdr_env, monkeypatch):
        import nexus.commands.rdr as rdr_mod

        self._commit(rdr_env, self._BODY, "draft")
        fake = _FakeT2ResearchClient({})
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        result = _runner().invoke(rdr, ["preamble", "rdr-fix", "--", "204"])
        assert result.exit_code == 0, result.output
        assert "no gate record" in result.output.lower()
        assert "rdr-research" in result.output

    def test_prints_findings_sites_diff_research_title_and_rules(self, rdr_env, monkeypatch):
        import nexus.commands.rdr as rdr_mod

        gated = self._commit(rdr_env, self._BODY, "gated")
        fixed = self._commit(rdr_env, self._BODY + "\nFive sites.\n", "fix one")
        fake = _FakeT2ResearchClient({
            "204-gate-latest": (
                f"outcome: \"BLOCKED\"\ndate: \"2026-09-07\"\ncommit: {gated}\n"
                "critique: nexus_rdr/204-gate-critique-2026-09-07h\nprior: [1] (PASSED 0C 2S)\n"
            ),
            "204-gate-critique-2026-09-07h": (
                "## Critical Issues\n\n### Issue: model_version never parsed\n"
                "- **Location**: L471\n- **Sites**: L471, L254-257, L612\n"
                "- **Recommendation**: cite indexer.py:790\n\n## Observations\n- fine\n"
            ),
            "204-research-3": "finding: earlier\n",
        })
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        result = _runner().invoke(rdr, ["preamble", "rdr-fix", "--", "204"])
        assert result.exit_code == 0, result.output
        out = result.output
        assert "### Fix RDR-204" in out
        assert "Gate round 3" in out
        assert "model_version never parsed" in out and "Sites: L471, L254-257, L612" in out
        assert "- fine" not in out
        assert f"git diff {gated}..HEAD -- docs/rdr/rdr-204-example.md" in out
        assert fixed in out and "fix one" in out
        assert "204-research-4" in out, "the next research seq is the pre-edit entry's title"
        assert "nx rdr preamble rdr-research -- add 204" in out
        assert "nothing else" in out and "inferred, not read" in out and "census" in out
        assert f"204-fix-check-{fixed}" in out
        assert "no fix-check record yet" in out.lower()

    def test_existing_fix_check_record_is_reported(self, rdr_env, monkeypatch):
        import nexus.commands.rdr as rdr_mod

        gated = self._commit(rdr_env, self._BODY, "gated")
        fixed = self._commit(rdr_env, self._BODY + "\nFive sites.\n", "fix one")
        fake = _FakeT2ResearchClient({
            "204-gate-latest": f"outcome: \"BLOCKED\"\ncommit: {gated}\n",
            f"204-fix-check-{fixed}": "verdict: CLEAN\n",
        })
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        out = _runner().invoke(rdr, ["preamble", "rdr-fix", "--", "204"]).output
        assert f"Fix-check record `204-fix-check-{fixed}` exists" in out

    def test_past_the_gate_is_named(self, rdr_env, monkeypatch):
        import nexus.commands.rdr as rdr_mod

        gated = self._commit(rdr_env, self._BODY, "gated")
        path = _write_rdr(
            rdr_env["rdr_dir"], "rdr-204-example.md",
            {"title": "Example", "status": "accepted", "type": "Architecture", "priority": "medium"},
            body=self._BODY,
        )
        root = str(rdr_env["repo_root"])
        subprocess.run(["git", "-C", root, "add", str(path)], check=True, capture_output=True)
        subprocess.run(["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "accept"], check=True, capture_output=True)
        fake = _FakeT2ResearchClient({"204-gate-latest": f"outcome: \"PASSED\"\ncommit: {gated}\n"})
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        out = _runner().invoke(rdr, ["preamble", "rdr-fix", "--", "204"]).output
        assert "past the gate" in out and "accepted" in out
        assert "#### Before the edit" not in out, "past the gate, the instructions do not print"
        assert "204-fix-check-<sha>" in out, "the pointer at rdr-accept names the check it carries"
        assert "bead id" in out and "needs none" in out, "the bead-disposition exemption"

    def test_unreachable_t2_is_named(self, rdr_env, monkeypatch):
        import nexus.commands.rdr as rdr_mod

        self._commit(rdr_env, self._BODY, "draft")

        class _Boom:
            def __enter__(self):
                raise ConnectionError("engine down")

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: _Boom())
        result = _runner().invoke(rdr, ["preamble", "rdr-fix", "--", "204"])
        assert result.exit_code == 0
        assert "T2 unreachable" in result.output and "engine down" in result.output


class TestRdrAuditGateLoopHealth:
    """nexus-zbdm0 (G2): the bound ships with its counter-metric. The audit
    preamble prints, per gated RDR, the rounds, the findings-per-round
    series read from the prior chain, and the residual count, and flags a
    loop the cap did not end."""

    def test_health_block_lists_gated_rdrs(self, rdr_env, monkeypatch):
        import nexus.commands.rdr as rdr_mod

        fake = _FakeT2ResearchClient({
            "204-gate-latest": (
                "outcome: \"PASSED\"\ncritical_count: 0\nsignificant_count: 1\n"
                "residuals:\n  - Finalization Gate count is stale; disposition at accept: pointer, no number\n"
                "  - a second residual\n"
                "prior: [9] (BLOCKED 3C), [8] (PASSED 0C 2S), [7] (BLOCKED 1C 2S), [6] (PASSED 0C 3S), [5] (1C)\n"
            ),
            "150-gate-latest": "outcome: \"PASSED\"\ncritical_count: 0\n",
            "150": "status: accepted\n",
        })
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        result = _runner().invoke(rdr, ["preamble", "rdr-audit"])
        assert result.exit_code == 0, result.output
        out = result.output
        assert "### Gate loop health" in out
        assert "RDR-204: 6 rounds" in out, out
        assert "Criticals per round: 1, 0, 1, 0, 3, 0" in out
        assert "residuals: 2" in out
        assert "cap did not end the loop" in out
        assert "RDR-150: 1 round" in out

    def test_bound_test_flags_both_falling(self, rdr_env, monkeypatch):
        """The doctrine's signature: rounds per RDR fell AND findings per
        round fell since the cap shipped."""
        import nexus.commands.rdr as rdr_mod

        fake = _FakeT2ResearchClient({
            "100-gate-latest": "outcome: \"PASSED\"\ndate: \"2026-08-01\"\ncritical_count: 0\nprior: [1] (BLOCKED 3C), [2] (BLOCKED 2C)\n",
            "101-gate-latest": "outcome: \"PASSED\"\ndate: \"2026-08-02\"\ncritical_count: 1\nprior: [3] (BLOCKED 2C)\n",
            "300-gate-latest": "outcome: \"PASSED\"\ndate: \"2026-09-08\"\ncritical_count: 0\n",
        })
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        out = _runner().invoke(rdr, ["preamble", "rdr-audit"]).output
        assert "Bound test (RDRs gated before 2026-09-07: 2; since: 1)" in out, out
        assert "BOTH FELL" in out

    def test_bound_test_needs_both_sides(self, rdr_env, monkeypatch):
        import nexus.commands.rdr as rdr_mod

        fake = _FakeT2ResearchClient({
            "300-gate-latest": "outcome: \"PASSED\"\ndate: \"2026-09-08\"\ncritical_count: 0\n",
        })
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        out = _runner().invoke(rdr, ["preamble", "rdr-audit"]).output
        assert "not yet measurable (0 RDRs gated before" in out

    def test_round_count_prefers_critique_records(self, rdr_env, monkeypatch):
        import nexus.commands.rdr as rdr_mod

        fake = _FakeT2ResearchClient({
            "150-gate-latest": "outcome: \"PASSED\"\ncritical_count: 0\n",
            **{f"150-gate-critique-2026-09-0{i}": "x" for i in range(1, 5)},
        })
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        out = _runner().invoke(rdr, ["preamble", "rdr-audit"]).output
        assert "RDR-150: 4 rounds" in out and "cap did not end the loop" in out

    def test_residual_count_is_by_bullet_not_punctuation(self):
        from nexus.commands.rdr import _residual_count

        assert _residual_count("residuals:\n  - one; with a semicolon\ncommit: abc\n") == 1
        assert _residual_count("residuals: inline one\n") == 1
        assert _residual_count("outcome: PASSED\n") == 0

    def test_unreachable_t2_is_named_not_silent(self, rdr_env, monkeypatch):
        import nexus.commands.rdr as rdr_mod

        class _Boom:
            def __enter__(self):
                raise ConnectionError("engine down")

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: _Boom())
        out = _runner().invoke(rdr, ["preamble", "rdr-audit"]).output
        assert "Gate loop health: T2 unreachable" in out


FIXTURES = Path(__file__).parent / "fixtures" / "rdr_gate_critiques"


class TestRdrVerdictPreamble:
    """nexus-yxo2l: the gate outcome is computed in code from the critique
    and the round, never in the gating model's head (audit_rounds design
    decision 1). Real RDR-204 critiques are the fixtures."""

    _BODY = "## Problem Statement\n\n#### Gap 1: a gap\nText.\n\n## Proposed Solution\n\nSix sites.\n"

    def _commit(self, rdr_env) -> str:
        path = _write_rdr(
            rdr_env["rdr_dir"], "rdr-204-example.md",
            {"title": "Example", "status": "draft", "type": "Architecture", "priority": "medium"},
            body=self._BODY,
        )
        root = str(rdr_env["repo_root"])
        subprocess.run(["git", "-C", root, "add", str(path)], check=True, capture_output=True)
        subprocess.run(["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "gated"], check=True, capture_output=True)
        return subprocess.run(["git", "-C", root, "log", "-1", "--format=%h"], check=True, capture_output=True, text=True).stdout.strip()

    def _run(self, rdr_env, monkeypatch, store: dict[str, str], critique: str):
        import nexus.commands.rdr as rdr_mod

        fake = _FakeT2ResearchClient(store)
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        return _runner().invoke(rdr, ["preamble", "rdr-verdict", "--", "204", critique])

    def test_round_one_blocks_on_any_critical(self, rdr_env, monkeypatch):
        """-07h: 3 Criticals, ship_blockers 0; a first gate blocks."""
        self._commit(rdr_env)
        crit = (FIXTURES / "204-gate-critique-2026-09-07h.md").read_text()
        out = self._run(rdr_env, monkeypatch, {"204-gate-critique-2026-09-07h": crit}, "204-gate-critique-2026-09-07h").output
        assert "Gate round 1" in out
        assert "critical_count: 3" in out and "ship_blockers: 0" in out
        assert "outcome: \"BLOCKED\"" in out, out
        assert "rule: any-critical" in out

    def test_round_three_passes_with_residuals_when_no_ship_blocker(self, rdr_env, monkeypatch):
        """The same critique at round 10 (204's real position): PASSED with
        three residuals, which is what would have ended the loop at pass 5."""
        sha = self._commit(rdr_env)
        crit = (FIXTURES / "204-gate-critique-2026-09-07h.md").read_text()
        store = {
            "204-gate-critique-2026-09-07h": crit,
            "204-gate-latest": (
                f"outcome: \"PASSED\"\ndate: \"2026-09-07\"\ncritical_count: 0\nsignificant_count: 2\ncommit: {sha}\n"
                "prior: [24844] (BLOCKED 1C 2S 2O), [24841] (PASSED 0C 3S 4O), [24812] (1C), [24809] (2C), [24806] (2C 2S), [24800] (1C 2S), [24789] (2C)\n"
            ),
        }
        out = self._run(rdr_env, monkeypatch, store, "204-gate-critique-2026-09-07h").output
        assert "Gate round 9" in out, out
        assert "rule: ship-blocker" in out
        assert "outcome: \"PASSED\"" in out
        assert out.count("residuals:") >= 1 and "model_version" in out
        assert "prior: [" in out and "[24844] (BLOCKED 1C 2S 2O)" in out, "the chain is pre-filled from the current record"
        assert f"commit: {sha}" in out

    def test_free_form_critique_with_a_ship_blocker_blocks_at_any_round(self, rdr_env, monkeypatch):
        """-07f: free-form layout, `Ship-blocker: yes` on the Critical and a
        `VERDICT: ... ship_blockers=1` line; round 7 in the real loop."""
        self._commit(rdr_env)
        crit = (FIXTURES / "204-gate-critique-2026-09-07f.md").read_text()
        store = {
            "204-gate-critique-2026-09-07f": crit,
            "204-gate-latest": "outcome: \"PASSED\"\nprior: [1] (1C), [2] (2C), [3] (2C), [4] (1C), [5] (0C)\n",
        }
        out = self._run(rdr_env, monkeypatch, store, "204-gate-critique-2026-09-07f").output
        assert "Gate round 7" in out, out
        assert "critical_count: 1" in out and "ship_blockers: 1" in out
        assert "outcome: \"BLOCKED\"" in out

    def test_self_report_below_the_count_is_overridden(self, rdr_env, monkeypatch):
        """Two issues marked Ship-blocker: yes but a Verdict saying 0: the
        counted value wins, and the discrepancy is named."""
        self._commit(rdr_env)
        crit = (
            "## Critical Issues\n\n### Issue: one\n- **Location**: L1\n- **Ship-blocker**: yes\n\n"
            "### Issue: two\n- **Location**: L2\n- **Ship-blocker**: yes\n\n## Significant Issues\nNone.\n\n"
            "## Verdict\n\n- **outcome**: not-justified\n- **critical_count**: 1\n- **significant_count**: 0\n- **ship_blockers**: 0\n"
        )
        store = {"c": crit, "204-gate-latest": "outcome: \"PASSED\"\nprior: [1] (1C), [2] (1C)\n"}
        out = self._run(rdr_env, monkeypatch, store, "c").output
        assert "critical_count: 2" in out and "ship_blockers: 2" in out
        assert "self-reported" in out and "counted" in out
        assert "outcome: \"BLOCKED\"" in out

    def test_missing_ship_blockers_line_reads_as_critical_count(self, rdr_env, monkeypatch):
        self._commit(rdr_env)
        crit = "## Critical Issues\n\n### Issue: one\n- **Location**: L1\n\n## Verdict\n\n- **outcome**: not-justified\n- **critical_count**: 1\n"
        store = {"c": crit, "204-gate-latest": "outcome: \"PASSED\"\nprior: [1] (1C), [2] (1C)\n"}
        out = self._run(rdr_env, monkeypatch, store, "c").output
        assert "ship_blockers: 1" in out and "outcome: \"BLOCKED\"" in out
        assert "no ship_blockers line" in out

    def test_unrecognised_shape_refuses_instead_of_passing(self, rdr_env, monkeypatch):
        """Critique [24898] Critical 1: zero counts from an unparsed critique
        must never print PASSED."""
        self._commit(rdr_env)
        crit = "Some free prose about the RDR with no sections, no markers, no verdict.\n"
        out = self._run(rdr_env, monkeypatch, {"c": crit}, "c").output
        assert "neither recognised shape" in out
        assert "Outcome:" not in out

    def test_already_gated_critique_is_named_as_a_recomputation(self, rdr_env, monkeypatch):
        """Critique [24898] Critical 2: the round is the next gate's; say so
        when the critique is already the recorded one."""
        sha = self._commit(rdr_env)
        crit = (FIXTURES / "204-gate-critique-2026-09-07i.md").read_text()
        store = {
            "204-gate-critique-2026-09-07i": crit,
            "204-gate-latest": f"outcome: \"PASSED\"\ncritique: nexus_rdr/204-gate-critique-2026-09-07i\ncommit: {sha}\nprior: [1] (1C)\n",
        }
        out = self._run(rdr_env, monkeypatch, store, "204-gate-critique-2026-09-07i").output
        assert "already the one `204-gate-latest` records" in out
        crit_new = (FIXTURES / "204-gate-critique-2026-09-07h.md").read_text()
        store["204-gate-critique-2026-09-07h"] = crit_new
        out = self._run(rdr_env, monkeypatch, store, "204-gate-critique-2026-09-07h").output
        assert "assumes this critique is the new" in out

    def test_tally_is_not_poisoned_by_earlier_verdict_shaped_text(self):
        """Code review [24900] 1-3: a fenced example verdict, a duplicate
        Ship-blocker line, and a CRITICAL aside inside OBSERVATIONS."""
        from nexus.commands.rdr import _critique_tally

        canonical = (
            "Example of the block:\n```\n## Verdict\n- **critical_count**: 9\n- **ship_blockers**: 9\n```\n"
            "## Critical Issues\n\n### Issue: one\n- **Ship-blocker**: yes\n- **Ship-blocker**: yes\n\n"
            "## Observations\n- CRITICAL — historically this recurred, no action\n\n"
            "## Verdict\n\n- **critical_count**: 1\n- **significant_count**: 0\n- **ship_blockers**: 1\n"
        )
        t = _critique_tally(canonical)
        assert (t.reported_critical, t.reported_ship_blockers) == (1, 1)
        assert t.criticals == ["one"] and t.ship_blocker_titles == ["one"]
        free = (
            "CRITICAL — a real one.\nShip-blocker: yes\n\nOBSERVATIONS\n\n"
            "CRITICAL — historically this recurred, no action.\nShip-blocker: yes\n\n"
            "VERDICT: not-justified. critical_count=1, ship_blockers=1.\n"
        )
        t = _critique_tally(free)
        assert t.criticals == ["a real one."] and t.ship_blocker_titles == ["a real one."]
        assert t.reported_critical == 1

    def test_missing_critique_is_named(self, rdr_env, monkeypatch):
        self._commit(rdr_env)
        out = self._run(rdr_env, monkeypatch, {}, "204-gate-critique-nope").output
        assert "no such T2 record" in out


class TestCritiqueFindings:
    """nexus-7vdf9 (critique [24815] Critical 2): the extractor must read the
    substantive-critic's canonical format, a free-form critique, and the shape
    the real RDR-204 fourth-gate critique used, dropping Observations."""

    CANONICAL = (
        "## Critique Summary\nFine overall.\n\n"
        "## Critical Issues\n\n"
        "### Issue: Ghost sweep narrower than collectionIsEmpty\n"
        "- **Location**: Technical Design step 3\n"
        "- **Problem**: audit-only tables are not FK-constrained\n"
        "- **Recommendation**: reuse COLLECTION_SCOPED_TABLES\n"
        "- **Ship-blocker**: no\n\n"
        "## Significant Issues\n\n"
        "### Issue: Phase 1 item 4 stale sentence\n"
        "- **Location**: Implementation Plan\n\n"
        "## Observations\n\n### Issue: bare bead ids\n- **Location**: everywhere\n\n"
        "## Verification Performed\ngrepped.\n"
    )

    def test_canonical_format_yields_titles_locations_and_recommendations(self) -> None:
        from nexus.commands.rdr import _critique_findings

        f = _critique_findings(self.CANONICAL)
        assert "Issue: Ghost sweep narrower than collectionIsEmpty" in f
        assert "  Location: Technical Design step 3" in f
        assert "  Recommendation: reuse COLLECTION_SCOPED_TABLES" in f
        assert "Issue: Phase 1 item 4 stale sentence" in f
        assert not any("bare bead ids" in x for x in f), "observations are not findings"
        assert not any("Ship-blocker" in x for x in f)

    def test_none_sections_yield_nothing(self) -> None:
        from nexus.commands.rdr import _critique_findings

        assert _critique_findings("## Critical Issues\nNone.\n\n## Significant Issues\nNone.\n") == []

    def test_free_form_critique(self) -> None:
        from nexus.commands.rdr import _critique_findings

        text = ("Prior gate ...\n\nNEW CRITICAL\n\nIssue: the sweep is narrower.\n"
                "Location: step 3\n\n**Critical 1**: ghost sweep omits topic_assignments.\n"
                "- Significant: Phase 1 item 4 stale.\nObservation: fine.\n")
        f = _critique_findings(text)
        assert any("NEW CRITICAL" in x for x in f)
        assert any("the sweep is narrower" in x for x in f)
        assert any("omits topic_assignments" in x for x in f)
        assert any("Phase 1 item 4 stale" in x for x in f)
        assert not any("Observation: fine" in x for x in f)

    def test_empty(self) -> None:
        from nexus.commands.rdr import _critique_findings

        assert _critique_findings("") == []
