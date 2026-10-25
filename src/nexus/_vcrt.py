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

#: What the PDF stack adds: torch 2.13 imports ``MSVCP140_ATOMIC_WAIT.dll`` and fails
#: to load (WinError 126) without it. An engine release whose Windows PG bundle carries the
#: DLL (nexus-vyy65) puts it app-local beside the base pair, and the provisioner copies it to
#: ``<config>/vcrt``; an older engine's bundle does not, so there only Microsoft's
#: redistributable satisfies this set. Both are true at once, which is why the gate reads the
#: DLLs on disk and never the engine version. Re-walk torch's PE imports on any torch bump.
PDF_ONLY_DLLS: tuple[str, ...] = ("msvcp140_atomic_wait.dll",)
PDF_STACK_DLLS: tuple[str, ...] = (*VC_RUNTIME_DLLS, *PDF_ONLY_DLLS)

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


def has_runtime(directory: str, dlls: tuple[str, ...] = VC_RUNTIME_DLLS) -> bool:
    """True when *directory* holds EVERY DLL of *dlls* (default: the base pair;
    one of the two is not enough)."""
    return all(os.path.isfile(os.path.join(directory, n)) for n in dlls)


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
    dlls: tuple[str, ...] = VC_RUNTIME_DLLS,
) -> tuple[str, str | None]:
    """``(kind, directory)``: ``not_applicable`` off Windows, ``system`` when
    System32 has every DLL of *dlls*, ``app_local`` (with the first directory
    that has them all) otherwise, ``missing`` when nothing does. *dlls* defaults
    to the base pair; the PDF stack passes :data:`PDF_STACK_DLLS`."""
    if not is_windows(platform):
        return NOT_APPLICABLE, None
    sysdir = system if system is not None else system_dir()
    if has_runtime(sysdir, dlls):
        return SYSTEM, sysdir
    cfg = config_dir if config_dir is not None else default_config_dir()
    for d in app_local_dirs(cfg):
        if has_runtime(d, dlls):
            return APP_LOCAL, d
    return MISSING, None
