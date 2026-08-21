# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-pfuns round 2, item 4: ``nexus.db.minilm_direct``'s cache-path
resolution, tested in isolation from ``tests/db/test_minilm_direct.py``'s
module-scoped ``_provision_artifact`` autouse fixture (which does a REAL
network download of the ONNX artifact if the shared chroma cache is cold
-- pure path-arithmetic has no business paying that cost or depending on
network access, so this lives in its own file rather than that module).

Same import-time-frozen-default bug class as the 3 writers nexus-pfuns
named (``gc_purge_marker.py`` / ``src/nexus/commands/t3.py`` /
``routing/_lib.py``) -- ``DOWNLOAD_PATH``/``ARTIFACT_DIR`` used to be
module-level ``Path.home()`` constants, frozen at import. This is a
defensive fix (no test currently redirects this path in anger -- it is
deliberately the same real, shared cache location for every install/test,
never redirected via ``NEXUS_CONFIG_DIR``), applied so a future test that
DOES need to patch ``Path.home()`` for isolation is not blocked by a
frozen module constant.
"""
from __future__ import annotations

import pathlib

from nexus.db import minilm_direct


def test_download_path_resolves_home_at_call_time(tmp_path: pathlib.Path, monkeypatch) -> None:
    new_home = tmp_path / "new-home"
    new_home.mkdir()
    monkeypatch.setattr(pathlib.Path, "home", lambda: new_home)

    resolved = minilm_direct.download_path()

    assert resolved == new_home / ".cache" / "chroma" / "onnx_models" / minilm_direct.MODEL_NAME


def test_artifact_dir_resolves_home_at_call_time(tmp_path: pathlib.Path, monkeypatch) -> None:
    new_home = tmp_path / "new-home"
    new_home.mkdir()
    monkeypatch.setattr(pathlib.Path, "home", lambda: new_home)

    resolved = minilm_direct.artifact_dir()

    expected = (
        new_home / ".cache" / "chroma" / "onnx_models" / minilm_direct.MODEL_NAME / "onnx"
    )
    assert resolved == expected


def test_artifact_dir_is_onnx_subdir_of_download_path(monkeypatch) -> None:
    """Structural invariant preserved across the refactor: artifact_dir()
    is always download_path()/"onnx", regardless of what home resolves
    to."""
    assert minilm_direct.artifact_dir() == minilm_direct.download_path() / "onnx"
