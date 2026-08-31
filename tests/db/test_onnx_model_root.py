# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Shared onnx_models root resolution (nexus-ogccs).

The engine resolved model paths from ``user.home`` (the passwd entry) while
the Python provisioners write under ``Path.home()`` ($HOME) — any HOME
override got a green provision and an engine crash. Both sides now resolve
one root, rung for rung; the cross-language tests here pin the Java mirror
(``OnnxModelPaths.java``) to the Python module so the rungs cannot drift.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from nexus.db import onnx_model_root as omr

_JAVA_PATHS = (
    Path(__file__).resolve().parents[2]
    / "service/src/main/java/dev/nexus/service/vectors/OnnxModelPaths.java"
)


def test_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv(omr.ENV_MODEL_DIR, str(tmp_path / "custom-root"))
    assert omr.service_onnx_models_root() == tmp_path / "custom-root"


def test_default_is_home_cache(monkeypatch):
    monkeypatch.delenv(omr.ENV_MODEL_DIR, raising=False)
    assert (
        omr.service_onnx_models_root()
        == Path.home() / ".cache" / "nexus" / "onnx_models"
    )


def test_blank_env_is_absence(monkeypatch):
    monkeypatch.setenv(omr.ENV_MODEL_DIR, "   ")
    assert (
        omr.service_onnx_models_root()
        == Path.home() / ".cache" / "nexus" / "onnx_models"
    )


def test_home_env_wins(tmp_path, monkeypatch):
    monkeypatch.delenv(omr.ENV_MODEL_DIR, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert (
        omr.service_onnx_models_root()
        == tmp_path / ".cache" / "nexus" / "onnx_models"
    )


def test_blank_home_falls_to_passwd_entry(monkeypatch):
    """Rung parity for HOME="" (code-review-expert Important, 2026-08-30):
    Java blank-checks HOME and falls to user.home; bare Path.home() is
    presence-only and resolves HOME="" to ``/``. Both sides must land on the
    passwd entry."""
    import pwd

    monkeypatch.delenv(omr.ENV_MODEL_DIR, raising=False)
    monkeypatch.setenv("HOME", "")
    expected = (
        Path(pwd.getpwuid(os.getuid()).pw_dir) / ".cache" / "nexus" / "onnx_models"
    )
    assert omr.service_onnx_models_root() == expected


# ── Cross-language rung parity with OnnxModelPaths.java ──────────────────────


def test_java_env_name_matches_python():
    """One env var, both sides: the supervisor writes ``ENV_MODEL_DIR`` into
    the spawn env, and the engine must read the SAME name."""
    src = _JAVA_PATHS.read_text()
    m = re.search(r'MODEL_DIR_ENV\s*=\s*"([^"]+)"', src)
    assert m is not None, "Java MODEL_DIR_ENV literal not found"
    assert m.group(1) == omr.ENV_MODEL_DIR


def test_java_home_suffix_matches_python_default():
    """The Java HOME/user.home rungs append HOME_SUFFIX; the Python default is
    ``Path.home()`` + the same relative segments."""
    src = _JAVA_PATHS.read_text()
    m = re.search(r'HOME_SUFFIX\s*=\s*"([^"]+)"', src)
    assert m is not None, "Java HOME_SUFFIX literal not found"
    py_rel = "/" + "/".join((".cache", "nexus", "onnx_models"))
    assert m.group(1) == py_rel


def test_java_resolver_prefers_env_then_home():
    """Rung ORDER parity: the Java resolver must check MODEL_DIR_ENV before
    HOME before user.home — same order as the Python module documents."""
    src = _JAVA_PATHS.read_text()
    env_pos = src.index("MODEL_DIR_ENV);")
    home_pos = src.index('env.apply("HOME")')
    userhome_pos = src.index("userHome + HOME_SUFFIX")
    assert env_pos < home_pos < userhome_pos
