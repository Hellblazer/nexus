# SPDX-License-Identifier: AGPL-3.0-or-later
"""The RDR research vocabulary table (RDR-240 Technical Design 7, nexus-qo7ie.33)."""
from __future__ import annotations

import ast
from pathlib import Path

import click
import pytest

import nexus.tables.rdr_research_vocab as vocab
from nexus.commands import rdr as rdr_cmd
from nexus.tables.check import check_table
from nexus.tables.load import load_packaged_table
from nexus.tables.rdr_research_vocab import CLASSIFICATIONS, METHODS

RDR_PY = Path(rdr_cmd.__file__)


def test_the_table_lints_clean_and_holds_the_vocabulary() -> None:
    table = load_packaged_table("rdr-research-vocab.toml")
    assert check_table(table) == []
    assert CLASSIFICATIONS == ("verified", "documented", "assumed")
    assert METHODS == ("source_search", "spike", "docs_only")


def test_nx_rdr_keeps_no_copy_of_the_vocabulary() -> None:
    # The two constants moved to the table; a literal list of the values must not come back.
    text = RDR_PY.read_text(encoding="utf-8")
    assert "_RESEARCH_CLASSIFICATIONS" not in text and "_RESEARCH_METHODS" not in text
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            values = {e.value for e in node.elts if isinstance(e, ast.Constant)}
            assert not (values >= set(CLASSIFICATIONS)), f"line {node.lineno} copies the classifications"
            assert not (values >= set(METHODS)), f"line {node.lineno} copies the methods"
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # A string that spells the whole set (a usage hint) is a copy too.
            assert not all(v in node.value for v in CLASSIFICATIONS), f"line {node.lineno} spells the classifications"
            assert not all(v in node.value for v in METHODS), f"line {node.lineno} spells the methods"


@pytest.mark.parametrize("flag,value", [("--classification", "guessed"), ("--method", "intuition")])
def test_nx_rdr_refuses_a_value_outside_the_table(flag: str, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rdr_cmd, "_preamble_resolve_repo", lambda: (str(Path.cwd()), "nexus"))
    with pytest.raises(click.ClickException) as e:
        rdr_cmd.preamble_rdr_research.callback(("add", "240", flag, value, "a", "finding"))
    allowed = CLASSIFICATIONS if flag == "--classification" else METHODS
    assert all(v in e.value.message for v in allowed)


def test_nx_rdr_reads_the_table_not_a_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    # Swap the table's domain: the CLI must follow it.
    monkeypatch.setattr(vocab, "CLASSIFICATIONS", ("probe_only",))
    monkeypatch.setattr(rdr_cmd, "_preamble_resolve_repo", lambda: (str(Path.cwd()), "nexus"))
    with pytest.raises(click.ClickException) as e:
        rdr_cmd.preamble_rdr_research.callback(("add", "240", "--classification", "verified", "x"))
    assert "probe_only" in e.value.message
