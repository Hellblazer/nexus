# SPDX-License-Identifier: AGPL-3.0-or-later
"""`nx enrich aspects` refusals that need no service: they run before the collection's content type is read.

RDR-229 P2.9 made ``select_config`` read the collection's catalog row, which lists the tenant on a cold row
cache. A refusal that decides nothing from the collection must stay ahead of that read, so an operator's usage
error costs no round trip. This module takes no row fixture and no tenant on purpose: the storage-touch guard
(fail mode) turns a listing reached by one of these refusals into a failing test.

The backend gate is NOT such a refusal. Whether it applies depends on the collection's extractor (a
deterministic parser needs no LLM), so it follows ``select_config`` and the catalog read, as it always has.
"""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from nexus.commands.enrich import enrich


def test_one_path_for_both_validation_files_is_refused_before_any_storage_is_reached(tmp_path: Path) -> None:
    same = tmp_path / "validation.jsonl"
    result = CliRunner().invoke(
        enrich,
        ["aspects", "knowledge__delos", "--validation-out", str(same), "--validation-all-out", str(same)],
    )
    assert result.exit_code != 0, result.output
    assert "name the same file" in result.output, result.output
