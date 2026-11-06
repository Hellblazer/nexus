# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The LLM-surface registry table and its typed loader (RDR-243 Phase 1 Step 5a, nexus-rfvyy.6.16).

``src/nexus/tables/llm-surfaces.toml`` holds one row per census row of RDR-243
(Key Discoveries, 25 rows). The loader closes the vocabularies (endpoint class,
behaviour, phase) and refuses a row outside them. The pins below hold the
table to the census: the census rows in order, each row's behaviour and
enforcing phase as the RDR gives them, and every ``module:function`` to a
scope that exists in the source.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from nexus.tables import llm_surfaces as ls
from nexus.tables.check import BLOCKING_CODES, check_table
from nexus.tables.load import load_table

REPO_ROOT = Path(__file__).resolve().parents[2]
TABLE_PATH = REPO_ROOT / "src" / "nexus" / "tables" / "llm-surfaces.toml"

#: RDR-243 census (Key Discoveries), row number -> (behaviour, enforcing phase).
#: Behaviours are the census's "Proposed behaviour" column read into the closed
#: vocabulary; phases are the RDR's Phase 1 Step 3-5, Phase 2 and Phase 3 scopes.
CENSUS: dict[int, tuple[str, int]] = {
    1: ("skip", 1), 2: ("skip", 1), 3: ("refuse", 1), 4: ("refuse", 1), 5: ("skip", 1),
    6: ("skip", 1), 7: ("skip", 1), 8: ("skip", 1), 9: ("skip", 1), 10: ("skip", 1),
    11: ("exclude", 3), 12: ("exclude", 2), 13: ("exclude", 2), 14: ("exclude", 2),
    15: ("exclude", 2), 16: ("exclude", 2), 17: ("exclude", 2),
    18: ("n/a-not-a-document", 1), 19: ("disclose", 1), 20: ("exclude", 3),
    21: ("skip", 1), 22: ("disclose", 1), 23: ("withhold", 3), 24: ("withhold", 3),
    25: ("n/a-no-content", 1),
}


def _text() -> str:
    return TABLE_PATH.read_text(encoding="utf-8")


def _load_text(tmp_path: Path, text: str) -> ls.Table:
    p = tmp_path / "llm-surfaces.toml"
    p.write_text(text, encoding="utf-8")
    return load_table(p)


def _first_row_block(text: str) -> tuple[int, int]:
    """Start and end offsets of the first ``[[row]]`` block."""
    start = text.index("[[row]]")
    end = text.index("[[row]]", start + 1)
    return start, end


def _with_first_row_emit(text: str, old: str, new: str) -> str:
    start, end = _first_row_block(text)
    block = text[start:end]
    assert old in block, f"the first row block has no {old!r} to replace"
    return text[:start] + block.replace(old, new, 1) + text[end:]


# --------------------------------------------------------------------------- #
# The table against the census.
# --------------------------------------------------------------------------- #


def test_the_table_has_one_row_per_census_row_in_census_order() -> None:
    rows = ls.rows()
    assert [r.census_row for r in rows] == list(range(1, 26)), "census_row 1..25, unique, in census order"
    assert len(rows) == ls.CENSUS_ROW_COUNT == 25
    assert len({r.name for r in rows}) == 25, "surface names are unique"


@pytest.mark.parametrize("census_row", sorted(CENSUS))
def test_each_row_carries_the_census_behaviour_and_the_rdr_phase(census_row: int) -> None:
    row = next(r for r in ls.rows() if r.census_row == census_row)
    assert (row.behaviour, row.phase) == CENSUS[census_row], (
        f"census row {census_row} ({row.name}): the RDR gives {CENSUS[census_row]}"
    )


def test_the_table_passes_the_rdr_201_checker() -> None:
    findings = [f for f in check_table(ls.load_table()) if f.code in BLOCKING_CODES]
    assert not findings, findings


def test_dropping_a_row_is_caught_by_the_checker_and_the_count(tmp_path: Path) -> None:
    """Non-vacuity: a 24-row table fails totality (the dimension still names 25 surfaces)
    and the census count, so deleting a row cannot go unnoticed."""
    text = _text()
    start, end = _first_row_block(text)
    table = _load_text(tmp_path, text[:start] + text[end:])
    assert len(table.rows) == ls.CENSUS_ROW_COUNT - 1
    assert [f for f in check_table(table) if f.code in BLOCKING_CODES], "totality names the missing surface"


def test_primary_targets_are_distinct_and_every_row_has_one() -> None:
    primaries = [r.target for r in ls.rows()]
    assert all(primaries)
    assert len(set(primaries)) == len(primaries), "the canary keys its drivers by the primary target"


def test_every_target_names_a_scope_in_the_source() -> None:
    """``module:Class.func`` resolves by AST over the source file, so the check imports nothing
    (``nexus.mcp.core`` and the hooks are heavy, and the hooks sit on every tool call)."""
    missing: list[str] = []
    for row in ls.rows():
        for target in row.targets:
            module, _, qual = target.partition(":")
            base = REPO_ROOT / "src" / Path(*module.split("."))
            file = base.with_suffix(".py") if base.with_suffix(".py").is_file() else base / "__init__.py"
            if not file.is_file():
                missing.append(f"{row.name}: {target}: module has no source file")
                continue
            scopes: set[str] = set()

            def walk(node: ast.AST, stack: list[str]) -> None:
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        scopes.add(".".join([*stack, child.name]))
                        walk(child, [*stack, child.name])
                    else:
                        walk(child, stack)

            walk(ast.parse(file.read_text(encoding="utf-8")), [])
            if qual not in scopes:
                missing.append(f"{row.name}: {target}: no such function or class in {file.relative_to(REPO_ROOT)}")
    assert not missing, "\n".join(missing)


def test_lookups_and_phase_selection() -> None:
    assert ls.row_for("nx_tidy").census_row == 15
    with pytest.raises(KeyError):
        ls.row_for("no-such-surface")
    assert {r.census_row for r in ls.enforced_through(1)} == {n for n, (_, p) in CENSUS.items() if p <= 1}
    assert {r.census_row for r in ls.enforced_through(2)} == {n for n, (_, p) in CENSUS.items() if p <= 2}
    assert ls.enforced_through(3) == ls.rows()
    with pytest.raises(ValueError):
        ls.enforced_through(4)


def test_all_targets_flattens_every_row() -> None:
    flat = ls.all_targets()
    assert set(flat) == {t for r in ls.rows() for t in r.targets}
    assert len(flat) == len(set(flat)), "a target is listed once"


def test_endpoint_defaults_are_the_first_listed() -> None:
    row = ls.row_for("enrich_bib")
    assert row.endpoints[:2] == ("s2", "openalex") and row.endpoint == "s2"


# --------------------------------------------------------------------------- #
# The loader refuses what the vocabularies do not hold.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        pytest.param('endpoints = ["anthropic"]', 'endpoints = ["gemini"]', "endpoint", id="unknown-endpoint"),
        pytest.param('behaviour = "skip"', 'behaviour = "ignore"', "behaviour", id="unknown-behaviour"),
        pytest.param("phase = 1\n", "", "phase", id="missing-phase"),
        pytest.param("phase = 1\n", "phase = 4\n", "phase", id="phase-out-of-range"),
        pytest.param("phase = 1\n", 'phase = "1"\n', "phase", id="phase-not-an-int"),
        pytest.param("census_row = 1\n", "", "census_row", id="missing-census-row"),
        pytest.param('endpoints = ["anthropic"]', "endpoints = []", "endpoint", id="no-endpoints"),
    ],
)
def test_the_loader_refuses_a_row_outside_the_closed_vocabularies(
    tmp_path: Path, old: str, new: str, message: str,
) -> None:
    text = _text()
    start, end = _first_row_block(text)
    block = text[start:end]
    assert old in block, f"the fixture needs {old!r} in the first row"
    table = _load_text(tmp_path, text[:start] + block.replace(old, new, 1) + text[end:])
    with pytest.raises(ValueError, match=message):
        ls.rows_of(table)


@pytest.mark.parametrize("bad", ['"nexus.aspect_worker"', '":func"', '"nexus.x:"', '"Nexus.X:f"', '"nexus.x:f g"'])
def test_the_loader_refuses_a_target_that_is_not_module_colon_function(tmp_path: Path, bad: str) -> None:
    text = _text()
    first = re.search(r'targets = \[\s*"([^"]+)"', text)
    assert first is not None
    table = _load_text(tmp_path, text.replace(f'"{first.group(1)}"', bad, 1))
    with pytest.raises(ValueError, match="target"):
        ls.rows_of(table)


def test_the_loader_refuses_a_duplicate_surface_name_in_the_census_numbering(tmp_path: Path) -> None:
    text = _text()
    table = _load_text(tmp_path, text.replace("census_row = 2\n", "census_row = 1\n", 1))
    with pytest.raises(ValueError, match="census_row"):
        ls.rows_of(table)


def test_the_vocabularies_are_the_rdr_item_7_sets() -> None:
    assert ls.ENDPOINT_CLASSES == (
        "anthropic", "openai-compat", "voyage", "s2", "openalex", "crossref-via-dt",
        "mineru-url", "cloud-tenant", "session-model", "none",
    )
    assert ls.BEHAVIOURS == (
        "refuse", "skip", "exclude", "withhold", "disclose", "n/a-not-a-document", "n/a-no-content",
    )
    assert ls.PHASES == (1, 2, 3)
