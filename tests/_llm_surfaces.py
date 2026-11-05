# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The LLM-surface registry as the RDR-243 tests read it (nexus-rfvyy.6.2).

``src/nexus/tables/llm-surfaces.toml`` lands at nexus-rfvyy.6.16 with a typed
loader ``nexus.tables.llm_surfaces``. Until then the canary
(``tests/test_sensitivity_canary.py``) and the registry lint
(``tests/test_llm_surface_registry_lint.py``) read it through this module, which
is the ONE place that encodes the assumed row schema. When the typed loader
exists, reconcile :func:`surface_of` with it (or replace this module's reader by
the loader) in the same change.

A missing table is a clear failure naming the table, never an import crash.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / "src" / "nexus" / "tables" / "llm-surfaces.toml"


class Surface(NamedTuple):
    """One registry row.

    ASSUMED SCHEMA (RDR-243 Technical Design item 7, shaped as an RDR-201
    decision table): ``row.id`` is the surface name; the ``emit`` table carries
    ``target`` (``module:function``), ``endpoint`` (one of ``anthropic``,
    ``openai-compat``, ``voyage``, ``s2``, ``openalex``, ``crossref-via-dt``,
    ``mineru-url``, ``cloud-tenant``, ``session-model``, ``none``),
    ``behaviour`` (``refuse``, ``skip``, ``exclude``, ``withhold``,
    ``disclose``, ``n/a-not-a-document``, ``n/a-no-content``) and ``phase`` (the
    phase that enforces the row, an integer).
    """

    name: str
    target: str
    endpoint: str
    behaviour: str
    phase: int


def surface_of(row: Any) -> Surface:
    emit = row.outcome
    assert isinstance(emit, dict), f"registry row {row.id!r} has no emit table: {emit!r}"
    return Surface(
        name=str(row.id),
        target=str(emit["target"]),
        endpoint=str(emit["endpoint"]),
        behaviour=str(emit["behaviour"]),
        phase=int(str(emit["phase"])),
    )


def registry_missing_message() -> str:
    return (
        f"the LLM-surface registry {REGISTRY_PATH.relative_to(REPO_ROOT)} does not exist. "
        "RDR-243 Technical Design item 7 puts it in src/nexus/tables/ with a typed loader "
        "(nexus.tables.llm_surfaces, nexus-rfvyy.6.16); the canary and the registry lint are "
        "driven by its rows, so with no table they have nothing to judge and cannot pass."
    )


def load_surfaces() -> list[Surface] | None:
    """Every row of the registry, or ``None`` when the file is absent."""
    if not REGISTRY_PATH.is_file():
        return None
    from nexus.tables.load import load_table  # noqa: PLC0415 - only needed once the table exists

    return [surface_of(row) for row in load_table(REGISTRY_PATH).rows]
