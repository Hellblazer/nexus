# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""scripts/plugin_ledger.py owns the ledger and Phase-2 helpers (nexus-rbmja.2).

The drift-ledger test imports these from a module of their own; the retired
anchored-tag cut script no longer holds them (tests/test_plugin_channel_retired.py
pins that it is gone).
"""

from __future__ import annotations

import pytest

LEDGER_TEST_SYMBOLS = (
    "deferred_paths",
    "phase2_deferral_check",
    "phase2_in_play",
    "phase2_guarded_paths",
    "PHASE2_GUARDED",
    "PHASE2_SAFE_ALONE",
)


@pytest.mark.parametrize("name", LEDGER_TEST_SYMBOLS)
def test_symbol_imports_from_plugin_ledger(name: str) -> None:
    import plugin_ledger

    assert hasattr(plugin_ledger, name), f"scripts/plugin_ledger.py lacks {name}"
