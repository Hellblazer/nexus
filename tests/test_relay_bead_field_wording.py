# SPDX-License-Identifier: AGPL-3.0-or-later
"""The relay's Bead field takes a tracking id as well as a bead id (nexus-sby49.3, D10), and says so in every copy.

The field's definition lives in a dozen places: SubagentStart's relay table, ``RELAY_TEMPLATE.md``, the shared context
protocol, the ten agents' Relay Reception checklists, the README's example and two command templates. A user with no beads
reads whichever copy they meet first, so the wording must agree. This pins it: each copy carries "a tracking id", none
carries the old "bead-id ... or 'none'" form, and the ten checklists are one line.
"""
from __future__ import annotations

from pathlib import Path

from nexus.hooks import subagent_start

PLUGIN = Path(__file__).resolve().parent.parent / "conexus"
RECEPTION = "2. [ ] **Bead** field present (ID with status, a tracking id, or 'none')"
TEMPLATE_LINE = "**Bead**: [ID] (status: [status]), [tracking id], or 'none'"


def _read(rel: str) -> str:
    return (PLUGIN / rel).read_text(encoding="utf-8")


def test_the_ten_agents_share_one_reception_line() -> None:
    agents = sorted((PLUGIN / "agents").glob("*.md"))
    assert len(agents) == 10, "the plan's ten agents"
    for path in agents:
        assert RECEPTION in path.read_text(encoding="utf-8").splitlines(), path.name


def test_the_relay_template_and_its_checklist_carry_the_wording() -> None:
    text = _read("resources/agent-shared/RELAY_TEMPLATE.md")
    assert text.splitlines().count(TEMPLATE_LINE) == 2
    assert RECEPTION in text.splitlines()
    assert "| **Bead** | Bead ID with status, a tracking id, or 'none' |" in text


def test_the_shared_context_protocol_carries_the_wording() -> None:
    text = _read("resources/agent-shared/CONTEXT_PROTOCOL.md")
    assert "- [ ] Bead ID(s) with current status, a tracking id, or 'none'" in text.splitlines()
    assert TEMPLATE_LINE in text.splitlines()


def test_the_readme_example_and_the_command_templates_carry_the_wording() -> None:
    assert TEMPLATE_LINE in _read("README.md").splitlines()
    for rel in ("commands/debug.md", "commands/review-code.md"):
        line = next(ln for ln in _read(rel).splitlines() if ln.startswith("**Bead**:"))
        assert "tracking id" in line, rel


def test_the_subagent_start_relay_table_carries_the_wording() -> None:
    assert "| Bead | Bead ID with status, a tracking id, or 'none' |" in subagent_start._RELAY


def test_no_copy_keeps_the_old_bead_id_only_form() -> None:
    for rel in ("README.md", "resources/agent-shared/CONTEXT_PROTOCOL.md", "resources/agent-shared/RELAY_TEMPLATE.md"):
        text = _read(rel)
        assert "**Bead**: [bead-id]" not in text, rel
        assert "(ID with status, or 'none')" not in text, rel
