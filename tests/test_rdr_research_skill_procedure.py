# SPDX-License-Identifier: AGPL-3.0-or-later
"""The rdr-research skill builds a research record with the ``research_add``
procedure (RDR-240 Phase 2 Step 1, Cross-Cutting Concerns: Upgrade;
nexus-qo7ie.33).

The order is: the package ships the procedure, this plugin change moves the
skill onto ``procedure_run``, and a later package release refuses the
bare-argument ``nx rdr preamble rdr-research -- add``. These pins hold the
middle step: the skill passes every returned field, reads the title back
before it writes, spells the vocabulary as the table does, and no plugin file
still sends a caller to the form the refusal will remove.
"""
from __future__ import annotations

import re
from pathlib import Path

from nexus.tables.rdr_research_vocab import CLASSIFICATIONS, METHODS

_PLUGIN = Path(__file__).parent.parent / "conexus"
_SKILL = _PLUGIN / "skills" / "rdr-research" / "SKILL.md"
_BARE_ADD = re.compile(r"rdr-research\s+--\s+add")


def _add_section() -> str:
    text = _SKILL.read_text()
    start = text.index("### `/conexus:rdr-research add <id>`")
    end = text.index("### `/conexus:rdr-research status <id>`")
    return text[start:end]


def test_add_runs_the_research_add_procedure_and_names_no_cli() -> None:
    add = _add_section()
    assert 'mcp__plugin_conexus_nexus__procedure_run(name="research_add"' in add
    for arg in ('"rdr"', '"classification"', '"method"', '"finding"'):
        assert arg in add, f"the procedure call lacks {arg}"
    assert '"repo"' not in add, "repo is filled by the host and refused if passed"


def test_add_reads_the_title_back_before_it_writes_all_four_fields() -> None:
    add = _add_section()
    get = add.index("mcp__plugin_conexus_nexus__memory_get(project=<project>, title=<title>)")
    put = add.index(
        "mcp__plugin_conexus_nexus__memory_put(project=<project>, title=<title>, "
        "content=<content>, tags=<tags>)"
    )
    assert get < put, "the read-back must come before the write"
    assert "never write over" in add


def test_add_spells_the_vocabulary_as_the_table_does() -> None:
    add = _add_section()
    for value in (*CLASSIFICATIONS, *METHODS):
        assert f"`{value}`" in add, f"{value} from rdr-research-vocab.toml is not in the add step"


def test_no_plugin_file_sends_a_caller_to_the_bare_argument_add() -> None:
    stale = [
        str(p.relative_to(_PLUGIN))
        for p in sorted(_PLUGIN.rglob("*.md"))
        if p.name != "CHANGELOG.md" and _BARE_ADD.search(p.read_text())
    ]
    assert not stale, f"still naming `nx rdr preamble rdr-research -- add`: {stale}"
    assert sum(1 for _ in _PLUGIN.rglob("*.md")) > 50, "the sweep found almost nothing to check"
