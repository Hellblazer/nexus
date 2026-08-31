# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Shared onnx_models root for the service model provisioners (nexus-ogccs).

The Java engine resolved its model paths from ``System.getProperty("user.home")``
— the passwd entry — while the Python provisioners write under ``Path.home()``
(the HOME env var). Any process tree where the two differ (containers with a
custom HOME, the release-sandbox HOME, CI runners) got a green ``nx init``
("model ready at $HOME/...") and then an engine crash ("model not found at
<passwd-home>/..."). The container leg of the plugin-cut rehearsal measured
exactly this on engine-service-v0.1.91 (2026-08-30).

Both sides now resolve the SAME root, rung for rung:

1. ``NX_ONNX_MODEL_DIR`` — the onnx_models ROOT (not a per-model dir). The
   storage-service supervisor passes this explicitly in the engine's spawn env
   so supervisor and engine agree by construction.
2. ``$HOME/.cache/nexus/onnx_models`` — the pre-existing default. Blank-aware
   on BOTH sides (review finding, 2026-08-30): the Java rung blank-checks HOME
   and a present-but-EMPTY ``HOME=""`` must fall through here too — bare
   ``Path.home()`` is presence-only and resolves ``HOME=""`` to ``/``.
3. The passwd entry (Java: ``user.home``) — last resort when HOME is absent
   or blank.

Java mirror: ``service/src/main/java/dev/nexus/service/vectors/OnnxModelPaths.java``;
``tests/db/test_onnx_model_root.py`` pins the two against each other. No
XDG_CACHE_HOME rung, deliberately — a rung only one side reads re-creates the
divergence this module exists to end.
"""
from __future__ import annotations

import os
from pathlib import Path

#: Spawn-env override naming the onnx_models ROOT. Mirrors
#: ``OnnxModelPaths.MODEL_DIR_ENV`` on the Java side.
ENV_MODEL_DIR = "NX_ONNX_MODEL_DIR"


def _home_base() -> Path:
    """HOME when set and non-blank, else the passwd entry — the Java rungs."""
    home = os.environ.get("HOME", "").strip()
    if home:
        return Path(home)
    try:
        import pwd  # noqa: PLC0415 — POSIX-only; the ImportError arm below IS the non-POSIX branch

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError):  # non-POSIX / no passwd row: best effort
        return Path.home()


def service_onnx_models_root() -> Path:
    """The root directory the per-model ``<model>/onnx/`` dirs live under."""
    env = os.environ.get(ENV_MODEL_DIR, "").strip()
    if env:
        return Path(env)
    return _home_base() / ".cache" / "nexus" / "onnx_models"
