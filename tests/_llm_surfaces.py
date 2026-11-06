# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The LLM-surface registry as the RDR-243 tests read it (nexus-rfvyy.6.2, reconciled at nexus-rfvyy.6.16).

``src/nexus/tables/llm-surfaces.toml`` is read through its typed loader,
``nexus.tables.llm_surfaces``; this module adapts a loader row to the
:class:`Surface` tuple the canary and the registry lint use. A row names
several ``module:function`` targets: ``target`` is the primary one (the
canary keys its driver by it), ``targets`` is all of them (the lint treats
each as registered).

A missing table is a clear failure naming the table, never an import crash.
"""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / "src" / "nexus" / "tables" / "llm-surfaces.toml"


class Surface(NamedTuple):
    """One registry row, as ``nexus.tables.llm_surfaces.SurfaceRow`` gives it."""

    name: str
    target: str
    targets: tuple[str, ...]
    endpoint: str
    behaviour: str
    phase: int


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
    from nexus.tables import llm_surfaces  # noqa: PLC0415 - only needed once the table exists

    return [
        Surface(name=r.name, target=r.target, targets=r.targets, endpoint=r.endpoint,
                behaviour=r.behaviour, phase=r.phase)
        for r in llm_surfaces.rows()
    ]
