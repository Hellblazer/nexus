# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-b0ka: ``nx doctor --check-post-store-hooks`` enumerates registered hooks.

RDR-095 § Day 2 Operations referenced ``nx doctor --check=hooks`` as the
way to enumerate registered post-store hook names. The actual
``--check-hooks`` flag (RDR-094 nexus-ntbg) reports slow Claude-Code
PostToolUse hook telemetry — a different concept. This bead adds a
sibling ``--check-post-store-hooks`` flag that imports
``nexus.mcp.core`` (triggers registration) and prints the registered
hook names per chain.

Use cases:
  * Confirm aspect_extraction_enqueue_hook (RDR-089) registered after
    install.
  * Detect drift if a hook silently fails to register due to import-
    order bugs.
  * Smoke after upgrade: are the expected hooks still registered?
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


# ── --check-post-store-hooks ───────────────────────────────────────────────


class TestCheckPostStoreHooks:
    def test_lists_three_chains(self, runner: CliRunner):
        """The output names all three chain sections so the operator can
        see where each registered hook lives.
        """
        result = runner.invoke(main, ["doctor", "--check-post-store-hooks"])
        assert result.exit_code == 0, result.output
        out = result.output.lower()
        assert "single-doc chain" in out
        assert "batch chain" in out
        assert "document-grain chain" in out or "document chain" in out

    def test_lists_known_registered_hooks(self, runner: CliRunner):
        """Importing nexus.mcp.core triggers the static registrations:
        chash_dual_write_batch_hook + taxonomy_assign_batch_hook on the
        batch chain, aspect_extraction_enqueue_hook on the document
        chain. The output must enumerate them.
        """
        result = runner.invoke(main, ["doctor", "--check-post-store-hooks"])
        assert result.exit_code == 0, result.output
        out = result.output
        assert "chash_dual_write_batch_hook" in out
        assert "taxonomy_assign_batch_hook" in out
        assert "aspect_extraction_enqueue_hook" in out

    def test_empty_chain_renders_explicitly(self, runner: CliRunner):
        """Chains with zero registrations render '(none)' rather than
        skipping the section so an absent hook is a visible signal,
        not silent.
        """
        result = runner.invoke(main, ["doctor", "--check-post-store-hooks"])
        assert result.exit_code == 0, result.output
        # The single-doc chain currently has no static registrations;
        # the output must still mark it explicitly.
        out = result.output
        # Either "(none)" or "0 hook" is acceptable; pin one form so
        # the contract is stable.
        assert "(none)" in out

    def test_reports_total_count(self, runner: CliRunner):
        """A summary line at the bottom counts total registered hooks
        across all chains. Useful for upgrade-smoke ('I expected 3,
        I got 2 — something didn't register')."""
        result = runner.invoke(main, ["doctor", "--check-post-store-hooks"])
        assert result.exit_code == 0, result.output
        out = result.output.lower()
        assert "total" in out or "hooks registered" in out
