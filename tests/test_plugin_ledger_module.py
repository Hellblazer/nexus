# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""scripts/plugin_ledger.py owns the ledger parser (nexus-rbmja.2).

The drift-ledger test imports it from a module of its own; the retired
anchored-tag cut script no longer holds it (tests/test_plugin_channel_retired.py
pins that it is gone).
"""

from __future__ import annotations

import pytest

LEDGER_TEST_SYMBOLS = ("deferred_paths",)


@pytest.mark.parametrize("name", LEDGER_TEST_SYMBOLS)
def test_symbol_imports_from_plugin_ledger(name: str) -> None:
    import plugin_ledger

    assert hasattr(plugin_ledger, name), f"scripts/plugin_ledger.py lacks {name}"
