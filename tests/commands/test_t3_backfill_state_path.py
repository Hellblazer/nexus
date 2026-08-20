# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-pfuns: ``nexus.commands.t3._backfill_state_path()`` resolution.

T2 nexus/gc-purge-marker-xdist-leak-2026-08-20: 14 of 19
``t3 backfill-manifest --no-dry-run`` invocations in
``tests/test_rdr108_phase1_remediation.py`` overwrote Sam's real
``~/.config/nexus/backfill_state.json`` because they forgot the
``NEXUS_BACKFILL_STATE_FILE`` override AND the fallback itself
(``_BACKFILL_STATE_DEFAULT = os.path.expanduser("~/.config/nexus/
backfill_state.json")``) was a module-level constant, frozen at import,
blind to ``NEXUS_CONFIG_DIR`` entirely -- the same import-time-default
class already fixed once for ``gc_purge_marker.py``. These are narrow,
filesystem-free unit tests of the resolution function itself; the
end-to-end test-isolation fix lives in the autouse fixture in
``tests/test_rdr108_phase1_remediation.py``.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from nexus import config as nexus_config_mod
from nexus.commands import t3 as t3_mod


def test_backfill_state_path_honors_nexus_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Absent an explicit ``NEXUS_BACKFILL_STATE_FILE``, the fallback must
    honor ``NEXUS_CONFIG_DIR`` (the suite-wide isolation knob every other
    subsystem already respects), not a hardcoded ``~/.config/nexus``."""
    monkeypatch.delenv("NEXUS_BACKFILL_STATE_FILE", raising=False)
    cfg_dir = tmp_path / "cfgdir"
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))

    assert t3_mod._backfill_state_path() == cfg_dir / "backfill_state.json"


def test_backfill_state_path_uses_config_module_attr_not_frozen_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same import-time-capture bug class as ``gc_purge_marker.py`` (T2
    nexus/gc-purge-marker-xdist-leak-2026-08-20): ``_backfill_state_path()``
    must read ``nexus.config.nexus_config_dir`` via module-attribute
    access so a patch applied to ``nexus.config.nexus_config_dir`` AFTER
    ``nexus.commands.t3`` is already imported still takes effect -- not a
    ``from nexus.config import nexus_config_dir`` binding captured once at
    t3.py's own import time (which this test would catch: the captured
    original function would ignore the patch below and resolve the real
    home instead of ``fake_dir``)."""
    monkeypatch.delenv("NEXUS_BACKFILL_STATE_FILE", raising=False)

    fake_dir = tmp_path / "patched-config-dir"
    monkeypatch.setattr(nexus_config_mod, "nexus_config_dir", lambda: fake_dir)

    assert t3_mod._backfill_state_path() == fake_dir / "backfill_state.json"


def test_backfill_state_path_explicit_override_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The explicit env override still takes precedence over both the
    NEXUS_CONFIG_DIR fallback and any nexus_config_dir() patch."""
    override = tmp_path / "explicit" / "state.json"
    monkeypatch.setenv("NEXUS_BACKFILL_STATE_FILE", str(override))
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfgdir"))

    assert t3_mod._backfill_state_path() == override
