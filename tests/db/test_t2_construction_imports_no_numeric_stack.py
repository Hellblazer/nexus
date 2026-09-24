# SPDX-License-Identifier: AGPL-3.0-or-later
"""Constructing a ``T2Database`` loads no numpy, scipy or sklearn (nexus-fd3zf).

``T2Database.__init__`` imports every T2 store module. Two of them,
``http_taxonomy_store`` and ``taxonomy_compute``, imported numpy and
``sklearn.feature_extraction.text`` at module scope, so every process's first
T2 handle paid for the whole numeric stack (1.74s cold, measured 2026-09-24)
whether or not it touched taxonomy. It paid on the error path as well: with
no endpoint configured, construction imported all three BEFORE raising
``ServiceEndpointUnresolvableError``. That numpy import is where the MCP
server blocked indefinitely on native Windows, so ``tuple_registry`` hung
instead of returning its endpoint error.

The subprocess is required. The test process has already imported numpy, so
an in-process check would always find numpy in ``sys.modules``, whatever
construction does. The environment is scrubbed so that no ambient lease or
``NX_SERVICE_*`` value decides which path runs.

Positive control: run this against 2dfa8f0e9 (before the fix). Both cases
fail, listing all three modules.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from nexus._lazy_module import LazyModule, lazy_module

_HEAVY = ("numpy", "scipy", "sklearn")

_PROBE = """
import json, sys, pathlib
from nexus.db.t2 import T2Database
outcome = "constructed"
try:
    T2Database(pathlib.Path(sys.argv[1]) / "memory.db").close()
except Exception as exc:
    outcome = type(exc).__name__
print(json.dumps({"outcome": outcome,
                  "loaded": [m for m in %r if m in sys.modules]}))
""" % (_HEAVY,)


def _probe(tmp_path: Path, extra_env: dict[str, str]) -> dict:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "home"),
        "NEXUS_CONFIG_DIR": str(tmp_path / "config"),
        **extra_env,
    }
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, str(tmp_path)],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_construction_against_an_endpoint_loads_no_numeric_stack(tmp_path: Path) -> None:
    # Port 9 (discard): construction must not dial, so nothing listens there.
    result = _probe(tmp_path, {
        "NX_SERVICE_URL": "http://127.0.0.1:9",
        "NX_SERVICE_TOKEN": "fd3zf-test",
    })
    # Non-vacuity: the construction really completed, so every store module
    # the facade imports really was imported.
    assert result["outcome"] == "constructed", result
    assert result["loaded"] == [], result


def test_unresolvable_endpoint_fails_before_any_numeric_import(tmp_path: Path) -> None:
    # This is the native-Windows shape: no NX_SERVICE_* and no lease.
    result = _probe(tmp_path, {})
    assert result["outcome"] == "ServiceEndpointUnresolvableError", result
    assert result["loaded"] == [], result


def test_lazy_module_imports_on_first_attribute_access() -> None:
    proxy = lazy_module("json")
    assert isinstance(proxy, LazyModule)
    assert "not loaded" in repr(proxy)
    assert proxy.dumps([1]) == "[1]"
    assert "(loaded)" in repr(proxy)


def test_lazy_module_surfaces_a_missing_module_at_use() -> None:
    proxy = lazy_module("nexus_fd3zf_no_such_module")
    with pytest.raises(ModuleNotFoundError):
        proxy.anything  # noqa: B018 — attribute access is the trigger under test
