# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where the client finds the VC++ runtime DLLs its extension modules need
(nexus-lqjll, RDR-224).

onnxruntime, pymupdf, torch and fasttext link the system ``msvcp140.dll`` and
``msvcp140_1.dll`` and fail to import on a Windows machine without Microsoft's
redistributable. Three directories can hold an app-local copy: the engine
directory and the PostgreSQL bundle's ``bin`` (both ship the DLLs, RDR-224
Step 0.6) and ``<config>/vcrt``, which ``nx init`` / ``nx upgrade --auto`` fill
on a cloud-mode install that has neither.

Stdlib only and import-cheap: ``nexus/__init__`` imports this on every ``nx``
start, and the doctor row and the provisioner read the same answer from here.
"""
from __future__ import annotations

import os
import sys

#: The DLLs the failing extension modules need (measured: msvcp140 alone is not
#: enough for onnxruntime; uv's CPython supplies vcruntime140*).
VC_RUNTIME_DLLS: tuple[str, ...] = ("msvcp140.dll", "msvcp140_1.dll")

#: Subdirectory of the config dir the provisioner fills.
VCRT_SUBDIR = "vcrt"

NOT_APPLICABLE = "not_applicable"
SYSTEM = "system"
APP_LOCAL = "app_local"
MISSING = "missing"


def is_windows(platform: str | None = None) -> bool:
    return (platform if platform is not None else sys.platform) == "win32"


def system_dir() -> str:
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")


def default_config_dir() -> str:
    """Mirrors ``nexus.config.nexus_config_dir`` without importing it."""
    return os.environ.get("NEXUS_CONFIG_DIR", "").strip() or os.path.join(
        os.path.expanduser("~"), ".config", "nexus")


def has_runtime(directory: str) -> bool:
    """True when *directory* holds BOTH runtime DLLs (one is not enough)."""
    return all(os.path.isfile(os.path.join(directory, n)) for n in VC_RUNTIME_DLLS)


def app_local_dirs(config_dir: str) -> tuple[str, str, str]:
    """The candidate app-local directories, in search order."""
    return (
        os.path.join(config_dir, "service"),
        os.path.join(config_dir, "pg-bundle", "bundle", "bin"),
        os.path.join(config_dir, VCRT_SUBDIR),
    )


def runtime_source(
    platform: str | None = None,
    config_dir: str | None = None,
    system: str | None = None,
) -> tuple[str, str | None]:
    """``(kind, directory)``: ``not_applicable`` off Windows, ``system`` when
    System32 has both DLLs, ``app_local`` (with the first directory that has
    both) otherwise, ``missing`` when nothing does."""
    if not is_windows(platform):
        return NOT_APPLICABLE, None
    sysdir = system if system is not None else system_dir()
    if has_runtime(sysdir):
        return SYSTEM, sysdir
    cfg = config_dir if config_dir is not None else default_config_dir()
    for d in app_local_dirs(cfg):
        if has_runtime(d):
            return APP_LOCAL, d
    return MISSING, None
