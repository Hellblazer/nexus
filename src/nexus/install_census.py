# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Which live processes are still running from which generation.

nexus-utpuw.10. THE LOGIC IS NOT HERE. It is in
:mod:`nexus._install.census_core`, a stdlib-only module that imports nothing
from nexus, and this module re-exports it under the name the installed world
already imports (``upgrade_finish.py``, ``health.py``).

``src/nexus/_install/census.sh`` used to be a second implementation of the same
rules, kept in step by ``tests/test_install_census_twins_agree.py``. It now
dispatches to the core, for the reason the layout twin collapsed first: the
constraint on the shell callers was importing NEXUS, never running Python.
"""
from __future__ import annotations

from nexus._install.census_core import (
    PS_COMMAND,
    census_report,
    generation_holder_pids,
    generation_match_pairs,
    generation_match_prefixes,
    legacy_tree_candidates,
    ps_snapshot,
)

__all__ = [
    "PS_COMMAND",
    "census_report",
    "generation_holder_pids",
    "generation_match_pairs",
    "generation_match_prefixes",
    "legacy_tree_candidates",
    "ps_snapshot",
]
