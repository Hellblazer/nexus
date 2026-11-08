# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 3 fix round r1 (nexus-rfvyy.8), item 3: one rule for edge ``meta``.

An edge touching a withheld document loses its ``meta`` (free text a linker may have written about the document),
whichever side the withheld document is on and whichever surface returns the edge: MCP ``catalog_show``
(neighbours as well as the shown document), ``catalog_links``, ``catalog_link_query``, and the CLI ``show`` and
``links``. In a terminal (CLI) or under ``sensitivity.mcp_output: flag`` nothing is hidden.

A fake catalog stands in for the engine's: what is under test is how an edge is rendered.
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.mcp_server import _reset_singletons, catalog_link_query, catalog_links, catalog_show

from tests.commands.test_catalog_cmd_sensitivity import (  # type: ignore[import-not-found]
    CANARY, CLEAN_A, CLEAN_B, SECRET, SECRET_LINK_NOTE, _FakeCatalog,
)


class _GraphCatalog(_FakeCatalog):
    def graph(self, t, *, depth=1, direction="both", link_types=None, link_type=""):
        edges = self.links_from(t) + self.links_to(t)
        ends = {str(t)} | {str(e.from_tumbler) for e in edges} | {str(e.to_tumbler) for e in edges}
        return {"nodes": [e for e in self.entries if str(e.tumbler) in ends], "edges": edges}

    def link_query(self, **_kw):
        return list(self.links)


@pytest.fixture
def world(tmp_path, monkeypatch: pytest.MonkeyPatch):
    cfg = tmp_path / "nexus-config"
    cfg.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    _reset_singletons()
    fake = _GraphCatalog()
    monkeypatch.setattr("nexus.mcp.catalog._require_catalog", lambda: (fake, None))
    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: fake)
    fake.cfg = cfg
    yield fake
    _reset_singletons()


def _metas(edges: list[dict]) -> dict[tuple[str, str], dict]:
    return {(e["from"], e["to"]): e["meta"] for e in edges}


# ── MCP ──────────────────────────────────────────────────────────────────────


def test_mcp_show_of_a_clean_document_strips_meta_from_an_edge_to_a_withheld_neighbour(world) -> None:
    d = catalog_show(tumbler=CLEAN_A)
    assert d["title"] == "Gardening"
    assert _metas(d["links_from"])[(CLEAN_A, SECRET)] == {}
    assert CANARY not in json.dumps(d, default=str)


def test_mcp_show_keeps_meta_on_an_edge_between_clean_documents(world) -> None:
    import dataclasses

    world.links[1] = dataclasses.replace(world.links[1], meta={"note": "plain"})
    d = catalog_show(tumbler=CLEAN_A)
    assert _metas(d["links_from"])[(CLEAN_A, CLEAN_B)] == {"note": "plain"}


def test_mcp_links_strips_meta_from_every_edge_touching_a_withheld_document(world) -> None:
    out = catalog_links(tumbler=CLEAN_A)
    assert _metas(out["edges"])[(CLEAN_A, SECRET)] == {}
    assert CANARY not in json.dumps(out, default=str)


def test_mcp_link_query_strips_meta_and_the_rest_is_raw(world) -> None:
    rows = catalog_link_query()
    assert _metas(rows)[(CLEAN_A, SECRET)] == {}
    assert CANARY not in json.dumps(rows, default=str)
    assert {(r["from"], r["to"], r["type"]) for r in rows} == {(CLEAN_A, SECRET, "cites"), (CLEAN_A, CLEAN_B, "relates")}


def test_mcp_flag_posture_shows_the_meta(world) -> None:
    (world.cfg / "config.yml").write_text("sensitivity:\n  mcp_output: flag\n")
    assert SECRET_LINK_NOTE in json.dumps(catalog_link_query(), default=str)


def test_mcp_link_query_fails_closed_when_the_flag_cannot_be_read(world, monkeypatch) -> None:
    def down(_t):
        raise RuntimeError("catalog down")

    monkeypatch.setattr(world, "resolve_many", down)
    monkeypatch.setattr(world, "by_doc_id", down)
    rows = catalog_link_query()
    assert all(r["meta"] == {} for r in rows if "meta" in r)


# ── CLI ──────────────────────────────────────────────────────────────────────


def _cli(*args: str) -> str:
    result = CliRunner().invoke(main, ["catalog", *args])
    assert result.exit_code == 0, result.output
    return result.stdout


def test_cli_links_under_claude_code_withholds_nodes_and_edge_meta(world, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    out = _cli("links", CLEAN_A, "--json")
    d = json.loads(out)
    assert CANARY not in out
    assert _metas(d["edges"])[(CLEAN_A, SECRET)] == {}
    secret_node = next(n for n in d["nodes"] if n["tumbler"] == SECRET)
    assert secret_node["sensitive"] is True and "title" not in secret_node


def test_cli_links_flat_query_under_claude_code_strips_meta(world, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    out = _cli("links", "--type", "cites", "--json")
    assert CANARY not in out and all(r["meta"] == {} for r in json.loads(out) if r["to"] == SECRET)


def test_cli_links_resolve_prints_no_title_for_a_withheld_endpoint(world, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    out = _cli("links", CLEAN_A, "--resolve")
    assert CANARY not in out
    assert f"{SECRET} " in out or f"{SECRET}\n" in out or f"→ {SECRET} " in out
    assert "Gardening" in out, "a clean endpoint keeps its title"


def test_cli_links_in_a_terminal_shows_everything(world) -> None:
    out = _cli("links", CLEAN_A, "--json")
    assert SECRET_LINK_NOTE in out
    assert _cli("links", CLEAN_A, "--resolve").count(CANARY) == 1, "the title, in a terminal"
