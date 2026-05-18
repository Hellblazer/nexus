# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the /nx:phase-review-gate preamble (nexus-j327).

The gate has two enforcement paths:

  Pass 1: enumerate §Approach items in scope for the named phase;
          emit them and request re-invocation with --evidence.

  Pass 2: validate that every enumerated item has an evidence pointer
          (bead-id or text description) supplied via --evidence; block
          on any missing or empty pointer.

Regression test: the actual RDR-112 Phase 1 closeout (nexus-52lb)
would have been blocked because §Approach §2 (T3 service) was
silently dropped from the closing-bead set.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
COMMAND_FILE = REPO_ROOT / "nx" / "commands" / "phase-review-gate.md"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_preamble(args: str, *, rdr_dir: Path | None = None) -> str:
    """Execute the preamble embedded in phase-review-gate.md.

    Extracts the Python block between ``!{`` / ``PYEOF`` and runs it
    in a subprocess so the same isolation level as the real slash command
    applies.  Returns combined stdout+stderr.
    """
    text = COMMAND_FILE.read_text()
    m = re.search(r"python3\s+<<\s+'PYEOF'\n(.*?)PYEOF", text, re.DOTALL)
    assert m, "No PYEOF block found in phase-review-gate.md"
    script = m.group(1)

    env = os.environ.copy()
    env["NEXUS_RDR_ARGS"] = args
    if rdr_dir is not None:
        env["NEXUS_RDR_DIR_OVERRIDE"] = str(rdr_dir)

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, env=env,
        timeout=15,
    )
    return (result.stdout + result.stderr).strip()


def _make_minimal_rdr(tmp_path: Path, rdr_id: int, approach_items: list[str],
                      status: str = "accepted") -> Path:
    """Write a minimal RDR markdown with numbered §Approach items."""
    rdr_dir = tmp_path / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    items_lines = "\n".join(
        f"{i + 1}. **Item {i + 1}**: {item}" for i, item in enumerate(approach_items)
    )
    # Build content without textwrap.dedent so indentation stays consistent
    content = (
        f"---\n"
        f"id: {rdr_id:03d}\n"
        f"title: Test RDR\n"
        f"status: {status}\n"
        f"type: implementation\n"
        f"---\n"
        f"\n"
        f"# Test RDR\n"
        f"\n"
        f"## Problem Statement\n"
        f"\n"
        f"#### Gap 1: test gap\n"
        f"A gap for testing.\n"
        f"\n"
        f"## Proposed Solution\n"
        f"\n"
        f"### Approach\n"
        f"\n"
        f"{items_lines}\n"
        f"\n"
        f"## Implementation Plan\n"
        f"\n"
        f"### Phase 1: Implementation\n"
        f"\n"
        f"Phase 1 description.\n"
        f"\n"
        f"## Finalization Gate\n"
        f"\n"
        f"Responses here.\n"
    )
    rdr_file = rdr_dir / f"{rdr_id:03d}-test-rdr.md"
    rdr_file.write_text(content)
    return rdr_dir


# ---------------------------------------------------------------------------
# Structural tests
# ---------------------------------------------------------------------------

class TestCommandFileStructure:
    """The command file must exist and have the required preamble shape."""

    def test_command_file_exists(self) -> None:
        assert COMMAND_FILE.exists(), f"Missing: {COMMAND_FILE}"

    def test_command_has_preamble_block(self) -> None:
        text = COMMAND_FILE.read_text()
        assert "python3 << 'PYEOF'" in text, "No Python preamble block found"
        assert "PYEOF" in text, "Preamble block not closed"

    def test_command_has_nexus_rdr_args_env(self) -> None:
        text = COMMAND_FILE.read_text()
        assert "NEXUS_RDR_ARGS" in text, "Preamble must read args via NEXUS_RDR_ARGS"

    def test_skill_file_exists(self) -> None:
        skill_file = REPO_ROOT / "nx" / "skills" / "phase-review-gate" / "SKILL.md"
        assert skill_file.exists(), f"Missing: {skill_file}"


# ---------------------------------------------------------------------------
# Pass 1 tests — enumerate approach items
# ---------------------------------------------------------------------------

class TestPass1Enumerate:
    """Pass 1: no --evidence supplied. Preamble enumerates approach items."""

    def test_no_args_shows_usage(self, tmp_path: pytest.fixture) -> None:
        """Invocation with no RDR ID prints usage and exits cleanly."""
        rdr_dir = _make_minimal_rdr(tmp_path, 99, ["T2 service", "T3 service"])
        out = _run_preamble("", rdr_dir=rdr_dir)
        assert "Usage" in out or "phase-review-gate" in out.lower()

    def test_pass1_lists_approach_items(self, tmp_path: pytest.fixture) -> None:
        """Pass 1 enumerates numbered §Approach items for the named phase."""
        _make_minimal_rdr(tmp_path, 99, ["T2 service", "T3 service", "CatalogDB collapse"])
        out = _run_preamble("99 --phase 1", rdr_dir=tmp_path / "docs" / "rdr")
        # Should list at least the approach items by number
        assert "1." in out or "Item 1" in out
        assert "2." in out or "Item 2" in out

    def test_pass1_instructs_reinvoke_with_evidence(self, tmp_path: pytest.fixture) -> None:
        """Pass 1 must tell the user to re-invoke with --evidence."""
        _make_minimal_rdr(tmp_path, 99, ["T2 service", "T3 service"])
        out = _run_preamble("99 --phase 1", rdr_dir=tmp_path / "docs" / "rdr")
        assert "--evidence" in out

    def test_pass1_exits_before_skill_body(self, tmp_path: pytest.fixture) -> None:
        """Pass 1 must exit cleanly; it must NOT emit 'validation passed'."""
        _make_minimal_rdr(tmp_path, 99, ["T2 service", "T3 service"])
        out = _run_preamble("99 --phase 1", rdr_dir=tmp_path / "docs" / "rdr")
        assert "validation passed" not in out.lower()


# ---------------------------------------------------------------------------
# Pass 2 tests — validate evidence pointers
# ---------------------------------------------------------------------------

class TestPass2Validate:
    """Pass 2: --evidence supplied. Preamble validates coverage."""

    def test_pass2_all_covered_emits_passed(self, tmp_path: pytest.fixture) -> None:
        """When all approach items have evidence, emit validation passed."""
        _make_minimal_rdr(tmp_path, 99, ["T2 service", "T3 service"])
        evidence = "Item1=nexus-abc1,Item2=nexus-def2"
        out = _run_preamble(
            f"99 --phase 1 --evidence '{evidence}'",
            rdr_dir=tmp_path / "docs" / "rdr",
        )
        assert "validation passed" in out.lower() or "PASSED" in out

    def test_pass2_missing_item_blocks(self, tmp_path: pytest.fixture) -> None:
        """When an approach item has no evidence, emit BLOCKED."""
        _make_minimal_rdr(tmp_path, 99, ["T2 service", "T3 service"])
        evidence = "Item1=nexus-abc1"  # Item2 missing
        out = _run_preamble(
            f"99 --phase 1 --evidence '{evidence}'",
            rdr_dir=tmp_path / "docs" / "rdr",
        )
        assert "BLOCKED" in out or "blocked" in out.lower() or "ERROR" in out

    def test_pass2_empty_evidence_value_blocks(self, tmp_path: pytest.fixture) -> None:
        """An empty string evidence value must be rejected."""
        _make_minimal_rdr(tmp_path, 99, ["T2 service", "T3 service"])
        evidence = "Item1=nexus-abc1,Item2="
        out = _run_preamble(
            f"99 --phase 1 --evidence '{evidence}'",
            rdr_dir=tmp_path / "docs" / "rdr",
        )
        assert "BLOCKED" in out or "ERROR" in out or "empty" in out.lower()

    def test_pass2_names_the_missing_item(self, tmp_path: pytest.fixture) -> None:
        """Blocked output must name which approach item is missing evidence."""
        _make_minimal_rdr(tmp_path, 99, ["T2 service", "T3 service"])
        evidence = "Item1=nexus-abc1"
        out = _run_preamble(
            f"99 --phase 1 --evidence '{evidence}'",
            rdr_dir=tmp_path / "docs" / "rdr",
        )
        # Must mention Item2 by number
        assert "Item2" in out or "item 2" in out.lower() or "2" in out


# ---------------------------------------------------------------------------
# Regression test — RDR-112 Phase 1 / nexus-52lb silent T3 drop
# ---------------------------------------------------------------------------

class TestRDR112Regression:
    """Gate MUST block the actual nexus-52lb closing-bead set.

    nexus-52lb closed Phase 1 with T2-only beads (nexus-08i1, nexus-61x6,
    nexus-m4gm, nexus-qy0u, nexus-w0et, nexus-x98k).  RDR-112 §Approach
    item 2 explicitly specifies T3 service (nx daemon t3) but no bead
    covered that item.  The gate must detect this gap and BLOCK.
    """

    RDR112_FILE = (
        REPO_ROOT / "docs" / "rdr"
        / "rdr-112-storage-as-service-container-boundary.md"
    )

    # The six Phase 1 closing beads from nexus-52lb (all T2-focused).
    # Item2 (T3 service) is deliberately absent.
    NEXUS_52LB_EVIDENCE = (
        "Item1=nexus-61x6,"   # P1.1 — T2 daemon process scaffold
        "Item3=nexus-7ejx,"   # P2.1 CatalogDB — only partial Phase 1 mention
        "Item4=none,"         # T1 stays put — acknowledged no work needed
        "Item6=nexus-61x6,"   # Discovery — covered by P1.1 daemon scaffold
        "Item7=nexus-m4gm,"   # EventStream RPC
        "Item8=nexus-x98k,"   # Subspace registry
        "Item9=nexus-w0et,"   # Schema migration ownership
        "Item10=none"         # Lifecycle hooks — superseded by items 7-9
        # Item2 (T3 service) intentionally absent — this is the bug
        # Item5 (storage boundary lint) intentionally absent — separate concern
    )

    def test_rdr112_file_exists(self) -> None:
        assert self.RDR112_FILE.exists(), (
            f"RDR-112 file not found at {self.RDR112_FILE}. "
            "Regression test requires the actual RDR file."
        )

    def test_rdr112_approach_section_present(self) -> None:
        text = self.RDR112_FILE.read_text()
        assert "### Approach" in text, "RDR-112 §Approach section not found"

    def test_rdr112_approach_has_t3_item(self) -> None:
        text = self.RDR112_FILE.read_text()
        # Verify item 2 is the T3 service item that was silently dropped.
        assert "T3 service" in text or "chroma run" in text, (
            "RDR-112 should mention T3 service / chroma run in §Approach"
        )

    def test_gate_would_block_nexus_52lb_partial_evidence(self) -> None:
        """Gate blocks when T3 (Item2) and storage-boundary lint (Item5) have no evidence.

        This is the regression: Phase 1 shipped only T2 work; the gate
        should have caught that Item2 (T3 daemon) had no closing bead.
        """
        rdr_dir = self.RDR112_FILE.parent
        out = _run_preamble(
            f"112 --phase 1 --evidence '{self.NEXUS_52LB_EVIDENCE}'",
            rdr_dir=rdr_dir,
        )
        # The gate must block because Item2 is absent from the evidence
        assert "BLOCKED" in out or "ERROR" in out or "missing" in out.lower(), (
            f"Gate should have BLOCKED due to missing Item2 (T3 service). "
            f"Got: {out[:500]}"
        )

    def test_gate_passes_when_all_items_covered(self) -> None:
        """If all items have evidence (including T3), the gate passes."""
        rdr_dir = self.RDR112_FILE.parent
        full_evidence = (
            "Item1=nexus-61x6,"
            "Item2=nexus-t3xx,"   # T3 bead — would have prevented the bug
            "Item3=nexus-7ejx,"
            "Item4=none,"
            "Item5=nexus-lint1,"
            "Item6=nexus-61x6,"
            "Item7=nexus-m4gm,"
            "Item8=nexus-x98k,"
            "Item9=nexus-w0et,"
            "Item10=none"         # Lifecycle hooks superseded by items 7-9
        )
        out = _run_preamble(
            f"112 --phase 1 --evidence '{full_evidence}'",
            rdr_dir=rdr_dir,
        )
        assert "validation passed" in out.lower() or "PASSED" in out, (
            f"Gate should PASS when all 10 approach items have evidence. "
            f"Got: {out[:500]}"
        )

    def test_pass1_enumerates_all_10_approach_items(self) -> None:
        """Pass 1 on RDR-112 must enumerate all 10 §Approach sub-items."""
        rdr_dir = self.RDR112_FILE.parent
        out = _run_preamble("112 --phase 1", rdr_dir=rdr_dir)
        # Items are numbered 1-10 in §Approach
        for item_num in range(1, 11):
            assert f"Item{item_num}" in out or f"item {item_num}" in out.lower(), (
                f"Pass 1 output should enumerate Item{item_num}. "
                f"Got:\n{out[:600]}"
            )
