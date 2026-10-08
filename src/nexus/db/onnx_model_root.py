# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Shared onnx_models root for the service model provisioners (nexus-ogccs).

The Java engine resolved its model paths from ``System.getProperty("user.home")``
— the passwd entry — while the Python provisioners write under ``Path.home()``
(the HOME env var). Any process tree where the two differ (containers with a
custom HOME, a sandbox HOME, CI runners) got a green ``nx init``
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


def nexus_cache_root() -> Path:
    """``<home>/.cache/nexus``: the directory nexus owns for model and scratch caches.

    Holds the default ``onnx_models`` root and MinerU's fallback output dir
    (``nexus._mineru_spawn``). ``nx uninstall --yes --remove-data`` removes this
    directory (RDR-224, nexus-f9bgu). On Windows HOME is normally unset and
    there is no ``pwd``, so it is ``%USERPROFILE%\\.cache\\nexus``.
    """
    return _home_base() / ".cache" / "nexus"


def service_onnx_models_root() -> Path:
    """The root directory the per-model ``<model>/onnx/`` dirs live under."""
    env = os.environ.get(ENV_MODEL_DIR, "").strip()
    if env:
        return Path(env)
    return nexus_cache_root() / "onnx_models"


# ── DJL tokenizer-library cache (RDR-224 guest walk) ─────────────────────────
#
# The engine tokenizes with DJL's HuggingFace tokenizers, which extract their
# native library into a cache directory on first use. DJL resolves that
# directory (``ai.djl.util.Utils.getEngineCacheDir``, djl 0.30.0) as
# ``ENGINE_CACHE_DIR``, else ``DJL_CACHE_DIR``, else ``<user.home>/.djl.ai``,
# each read as an environment variable or a system property. Left alone it
# lands in ``~/.djl.ai/tokenizers``, a directory no nexus uninstall knows
# about and one other DJL programs share. The supervisor points the engine at
# a directory under the nexus cache instead, so ``nx uninstall --remove-data``
# removes it with the rest of ``~/.cache/nexus``.

#: DJL's cache-root variable and its engine-specific override.
ENV_DJL_CACHE_DIR = "DJL_CACHE_DIR"
ENV_DJL_ENGINE_CACHE_DIR = "ENGINE_CACHE_DIR"


def djl_cache_root() -> Path:
    """``<nexus cache>/djl``: where the engine's DJL native libraries extract."""
    return nexus_cache_root() / "djl"


def apply_djl_cache_env(env: dict[str, str]) -> None:
    """Point the engine's DJL cache under the nexus cache, in the spawn env *env*.

    An operator's own ``DJL_CACHE_DIR`` or ``ENGINE_CACHE_DIR`` (non-blank) is
    a directory they chose and wins, exactly as DJL itself would honour it.
    """
    if env.get(ENV_DJL_CACHE_DIR, "").strip() or env.get(ENV_DJL_ENGINE_CACHE_DIR, "").strip():
        return
    env[ENV_DJL_CACHE_DIR] = str(djl_cache_root())


def legacy_djl_tokenizer_cache() -> Path:
    """``~/.djl.ai/tokenizers``, where engines before this change extracted.

    DJL's default root is shared by every DJL program on the machine, so
    nothing in it can be shown to be nexus's; uninstall reports it and never
    removes it. Resolved from the home directory DJL reads (``user.home``).
    """
    return Path.home() / ".djl.ai" / "tokenizers"
