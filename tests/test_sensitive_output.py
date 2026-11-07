# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 3 Step 3.1 (nexus-rfvyy.8.2): the pieces under the MCP output posture.

``tests/mcp/test_mcp_output_withhold.py`` drives the tools. These pin what they stand on: the stub renderer,
tumbler order, the config key (global only, fail closed), the owners search attaches, and the engine's per-row flag
reaching the tool through the HTTP client.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.context_annotations import READER_INSTRUCTION, SENSITIVE_READER_CLAUSE, reader_instruction
from nexus.sensitive_output import (
    first_sensitive_owner,
    hit_facts,
    hit_is_sensitive,
    mcp_output_posture,
    present_document_rows,
    stub_line,
    tumbler_order_key,
    withheld_edges,
    withheld_note,
)
from nexus.types import SearchResult


def test_stub_line_is_the_one_line_form():
    assert stub_line("1.2.7") == "[sensitive] 1.2.7: content withheld"
    assert stub_line("") == "[sensitive]: content withheld"
    assert stub_line() == "[sensitive]: content withheld"


def test_withheld_note_counts_and_pluralises():
    assert withheld_note(1) == "1 sensitive hit withheld"
    assert withheld_note(3) == "3 sensitive hits withheld"


def test_tumbler_order_is_numeric_not_textual():
    assert sorted(["1.10.1", "1.9.4", "1.2.7"], key=tumbler_order_key) == ["1.2.7", "1.9.4", "1.10.1"]
    assert first_sensitive_owner(["1.10.1", "1.9.4"]) == "1.9.4"
    assert first_sensitive_owner(["1.9.4", "1.9.4"]) == "1.9.4"
    assert first_sensitive_owner([]) == ""
    assert first_sensitive_owner(["", ""]) == ""
    # A malformed segment orders after every numeric one rather than raising.
    assert first_sensitive_owner(["x.y", "1.2"]) == "1.2"


def test_hit_is_sensitive_needs_the_engine_flag_true_or_an_owner():
    assert hit_is_sensitive(True)
    assert hit_is_sensitive(False, ["1.2.7"])
    assert not hit_is_sensitive(False)
    assert not hit_is_sensitive(None)
    assert not hit_is_sensitive("true")  # exactly True, not truthy
    assert hit_facts({"sensitive": True, "sensitive_owners": ["1.2.7", "1.9.4"]}) == (True, ("1.2.7", "1.9.4"))
    assert hit_facts({"sensitive": False}) == (False, ())
    assert hit_facts({"sensitive_owners": ["1.2.7"]}) == (True, ("1.2.7",))


def test_reader_instruction_is_unchanged_unless_a_sensitive_hit_is_shown():
    assert reader_instruction() == READER_INSTRUCTION
    assert reader_instruction(sensitive_shown=False) == READER_INSTRUCTION
    assert reader_instruction(sensitive_shown=True) == f"{READER_INSTRUCTION} {SENSITIVE_READER_CLAUSE}"


# ── the config key ────────────────────────────────────────────────────────────


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    return tmp_path / "config.yml"


def test_the_default_posture_is_withhold(cfg):
    assert mcp_output_posture() == "withhold"  # no config file at all
    cfg.write_text("sensitivity:\n  mcp_output: withhold\n")
    assert mcp_output_posture() == "withhold"


def test_flag_is_the_only_other_value(cfg):
    cfg.write_text("sensitivity:\n  mcp_output: flag\n")
    assert mcp_output_posture() == "flag"
    for bad in ("true", "FLAG!", "show", "1", "[flag]"):
        cfg.write_text(f"sensitivity:\n  mcp_output: '{bad}'\n")
        assert mcp_output_posture() == "withhold", bad
    cfg.write_text("sensitivity:\n  mcp_output: 7\n")
    assert mcp_output_posture() == "withhold"
    cfg.write_text("sensitivity: flag\n")  # a section that is not a mapping
    assert mcp_output_posture() == "withhold"


def test_the_key_is_in_the_defaults_and_set_validates_it(cfg):
    from nexus.config import _DEFAULTS, set_config_value

    assert _DEFAULTS["sensitivity"] == {"mcp_output": "withhold"}
    with pytest.raises(ValueError, match="withhold.*flag"):
        set_config_value("sensitivity.mcp_output", "show")
    assert not cfg.exists()
    set_config_value("sensitivity.mcp_output", "flag")
    assert mcp_output_posture() == "flag"


def test_nx_config_set_reaches_the_key(cfg):
    from click.testing import CliRunner

    from nexus.commands.config_cmd import config_group

    runner = CliRunner()
    bad = runner.invoke(config_group, ["set", "sensitivity.mcp_output=show"])
    assert bad.exit_code != 0 and "withhold" in bad.output
    ok = runner.invoke(config_group, ["set", "sensitivity.mcp_output=flag"])
    assert ok.exit_code == 0, ok.output
    assert mcp_output_posture() == "flag"


# ── the owners search attaches ────────────────────────────────────────────────

CHASH = "c" * 64


def _positions(*rows: tuple[str, bool]):
    return [{"chash": CHASH, "doc_id": d, "position": 0, "chunk_count": 1, "collection": "code__x", "sensitive": s}
            for d, s in rows]


class _Catalog:
    def __init__(self, rows):
        self._rows = rows

    def chash_positions(self, chashes):
        return self._rows


def _result() -> SearchResult:
    return SearchResult(id=CHASH, content="x", distance=0.1, collection="code__x",
                        metadata={"chunk_text_hash": CHASH})


def test_search_attaches_every_sensitive_owner_not_just_the_chosen_ones_flag():
    from nexus.search_engine import _attach_from_chash_positions

    r = _result()
    # 1.1.1 is a plain document and the chosen owner (lowest doc_id); 1.9.4 and 1.10.1 are sensitive co-owners.
    rows = _positions(("1.1.1", False), ("1.10.1", True), ("1.9.4", True))
    assert _attach_from_chash_positions([r], [CHASH], _Catalog(rows)) is True
    assert r.metadata["sensitive_owners"] == ["1.9.4", "1.10.1"]  # tumbler order, numeric


def test_search_attaches_nothing_for_a_clean_chash():
    from nexus.search_engine import _attach_from_chash_positions

    r = _result()
    assert _attach_from_chash_positions([r], [CHASH], _Catalog(_positions(("1.1.1", False), ("1.2.1", False)))) is True
    assert "sensitive_owners" not in r.metadata


def test_search_attaches_nothing_when_the_engine_sends_no_flag():
    from nexus.search_engine import _attach_from_chash_positions

    r = _result()
    rows = [{k: v for k, v in row.items() if k != "sensitive"} for row in _positions(("1.1.1", False))]
    assert _attach_from_chash_positions([r], [CHASH], _Catalog(rows)) is True
    assert "sensitive_owners" not in r.metadata


# ── the engine's per-row flag through the HTTP client ─────────────────────────


def test_collection_get_carries_the_engines_sensitives(monkeypatch):
    from nexus.db.http_vector_client import _ServiceCollectionStub

    monkeypatch.setattr(
        "nexus.db.http_vector_client._post",
        lambda path, body, **kw: {"ids": ["a", "b"], "documents": ["x", "y"], "metadatas": [{}, {}],
                                  "sensitives": [True, False], "count": 2},
    )
    out = _ServiceCollectionStub("col__test__m__v1").get(ids=["a", "b"], sensitivity="include")
    assert out["sensitives"] == [True, False]


def test_get_by_id_carries_the_flag_after_the_metadata_and_only_when_sent(monkeypatch):
    from nexus.db.http_vector_client import HttpVectorClient

    me = SimpleNamespace(_stamp_sensitivity=lambda body, s: None, _tenant=None)
    monkeypatch.setattr(
        "nexus.db.http_vector_client._post",
        lambda path, body, **kw: {"ids": ["a"], "documents": ["x"], "metadatas": [{"sensitive": False}],
                                  "sensitives": [True]},
    )
    assert HttpVectorClient.get_by_id(me, "col__test__m__v1", "a", sensitivity="include")["sensitive"] is True
    monkeypatch.setattr(
        "nexus.db.http_vector_client._post",
        lambda path, body, **kw: {"ids": ["a"], "documents": ["x"], "metadatas": [{}]},
    )
    assert "sensitive" not in HttpVectorClient.get_by_id(me, "col__test__m__v1", "a", sensitivity="include")


# ── catalog document rows (nexus-rfvyy.1) ─────────────────────────────────────

_CLEAN = {"tumbler": "1.3.1", "title": "Gardening", "sensitive": False, "content_type": "knowledge"}
_SECRET = {
    "tumbler": "1.2.7", "title": "CANARY title", "author": "CANARY author", "file_path": "CANARY/p.md",
    "source_uri": "file:///CANARY/p.md", "meta": {"k": "CANARY"}, "bib_doi": "10.1/CANARY", "head_hash": "ab",
    "corpus": "CANARY-corpus", "alias_of": "", "content_type": "knowledge", "physical_collection": "knowledge__x",
    "chunk_count": 4, "indexed_at": "2026-11-01", "sensitive": True,
}


def test_a_page_with_no_sensitive_row_is_returned_whole_and_does_not_read_the_config(monkeypatch):
    monkeypatch.setattr("nexus.sensitive_output.mcp_output_posture", lambda: pytest.fail("config read for a clean page"))
    page = present_document_rows([_CLEAN, dict(_CLEAN, tumbler="1.3.2")])
    assert page.rows == [_CLEAN, dict(_CLEAN, tumbler="1.3.2")]
    assert page.trailer() == [] and page.keys() == {} and page.withheld == 0


def test_withhold_replaces_a_sensitive_row_by_its_stub_in_the_same_slot(cfg):
    page = present_document_rows([_CLEAN, _SECRET, dict(_CLEAN, tumbler="1.3.2")])
    assert [r["tumbler"] for r in page.rows] == ["1.3.1", "1.2.7", "1.3.2"]
    assert page.rows[1] == {
        "tumbler": "1.2.7", "content_type": "knowledge", "physical_collection": "knowledge__x", "chunk_count": 4,
        "indexed_at": "2026-11-01", "sensitive": True, "withheld": "[sensitive] 1.2.7: content withheld",
    }
    assert "CANARY" not in repr(page.rows[1])
    assert page.rows[0] == _CLEAN
    assert page.withheld == 1 and page.hidden == frozenset({"1.2.7"})
    assert page.trailer() == [{"_withheld": {"count": 1, "note": "1 sensitive hit withheld"}}]


def test_the_caller_can_name_extra_sensitive_tumblers_the_row_does_not_say(cfg):
    """An alias of a flagged canonical carries its own unset flag; the caller names it."""
    page = present_document_rows([dict(_CLEAN, alias_of="1.2.7")], extra_sensitive={"1.3.1"})
    assert page.withheld == 1 and "title" not in page.rows[0]


def test_flag_returns_the_row_whole_with_a_badge_and_asks_for_the_reader_clause(cfg):
    cfg.write_text("sensitivity:\n  mcp_output: flag\n")
    page = present_document_rows([_CLEAN, _SECRET])
    assert page.rows[1]["title"] == "CANARY title" and page.rows[1]["badge"] == "[sensitive]"
    assert "badge" not in page.rows[0]
    assert page.withheld == 0 and page.shown_sensitive
    assert page.trailer() == [{"_sensitive_notice": SENSITIVE_READER_CLAUSE}]


def test_withheld_edges_empties_the_meta_of_an_edge_touching_a_withheld_document_only():
    edges = [
        {"from": "1.3.1", "to": "1.2.7", "type": "relates", "from_span": "", "to_span": "", "meta": {"n": "CANARY"}},
        {"from": "1.3.1", "to": "1.3.2", "type": "relates", "from_span": "", "to_span": "", "meta": {"n": "plain"}},
    ]
    out = withheld_edges(edges, frozenset({"1.2.7"}))
    assert [(e["from"], e["to"], e["type"]) for e in out] == [("1.3.1", "1.2.7", "relates"), ("1.3.1", "1.3.2", "relates")]
    assert out[0]["meta"] == {} and out[1]["meta"] == {"n": "plain"}
    assert withheld_edges(edges, frozenset()) == edges
