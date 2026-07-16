# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-185: the single upgrade ladder — every data transition is a rung.

``protocol`` defines the Rung seam (detect → converge → verify);
``registry`` holds rungs in dependency order (RQ2's five hard edges).
Completion records (P0.2) and the runner with the RDR-142
verify-before-record guard (P0.3) land in sibling modules.
"""
from nexus.upgrade_ladder.completion import CompletionRecord, CompletionStore
from nexus.upgrade_ladder.protocol import (
    ConvergeOutcome,
    ConvergeResult,
    ProgressReporter,
    Rung,
    RungStatus,
)
from nexus.upgrade_ladder.registry import (
    ALL_RUNGS,
    CO_RESIDENT_AXES,
    HARD_EDGES,
    PRECONDITION_ENGINE,
    PRECONDITION_PACKAGE,
    PRECONDITION_PROCESS,
    RUNG_ORDER,
    RUNG_SUBSTRATE_ETL,
    RUNG_T2_SCHEMA,
    LadderOrderError,
    LadderRegistry,
    default_registry,
    expand_edges,
    validate_hard_edges,
)

__all__ = [
    "ALL_RUNGS",
    "CO_RESIDENT_AXES",
    "HARD_EDGES",
    "PRECONDITION_ENGINE",
    "PRECONDITION_PACKAGE",
    "PRECONDITION_PROCESS",
    "RUNG_ORDER",
    "RUNG_SUBSTRATE_ETL",
    "RUNG_T2_SCHEMA",
    "CompletionRecord",
    "CompletionStore",
    "ConvergeOutcome",
    "ConvergeResult",
    "LadderOrderError",
    "LadderRegistry",
    "ProgressReporter",
    "Rung",
    "RungStatus",
    "default_registry",
    "expand_edges",
    "validate_hard_edges",
]
