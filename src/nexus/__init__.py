# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus package root.

On Windows, TLS is verified through the operating system (nexus-f9bgu.52). uv's
standalone CPython verifies with OpenSSL against the Windows ROOT store, and a
fresh Windows holds only ~17 roots, fetching the rest on demand through the OS's
own verifier. So on a new machine every download nx makes (PG bundle, engine,
models) failed ``CERTIFICATE_VERIFY_FAILED: unable to get local issuer
certificate`` until something else happened to prime the store. ``truststore``
routes ``ssl`` verification (urllib, httpx) through CryptoAPI, which fetches
missing roots and honours enterprise roots. Done here, once, because every nx
entry point imports this package. POSIX is untouched.

Also on Windows, the client's own extension modules find the VC++ runtime
(nexus-lqjll). onnxruntime, pymupdf, torch and fasttext link the system
``msvcp140.dll``/``msvcp140_1.dll`` and fail to import on a machine without the
VC++ redistributable (measured on the RDR-224 clean guest; numpy, pandas and
scikit-learn carry their own copy, and uv's CPython ships ``vcruntime140*``).
The engine and PostgreSQL bundle assets already ship those DLLs app-local
(RDR-224 Step 0.6, Sam's signed-off reading), so when the system lacks them the
installed engine directory and the bundle's ``bin`` join the DLL search path.
"""
from __future__ import annotations

import sys


def _use_os_trust_store(platform: str | None = None) -> bool:
    """Route ``ssl`` verification through the OS on Windows. True when it did."""
    if (platform if platform is not None else sys.platform) != "win32":
        return False
    try:
        import truststore  # noqa: PLC0415 -- Windows-only dependency
    except ImportError:  # a dev environment without the dependency: OpenSSL as before
        return False
    truststore.inject_into_ssl()
    return True


_use_os_trust_store()

#: The VC++ runtime DLLs the failing extension modules need (measured: msvcp140
#: alone is not enough for onnxruntime; uv's CPython supplies vcruntime140*).
_VC_RUNTIME_DLLS: tuple[str, ...] = ("msvcp140.dll", "msvcp140_1.dll")

#: Handles from os.add_dll_directory; a directory leaves the search path when its
#: handle is closed, so they are kept for the life of the process.
_VC_DLL_DIR_HANDLES: list[object] = []


def _add_vc_runtime_dirs(
    platform: str | None = None,
    config_dir: str | None = None,
    system_dir: str | None = None,
    add=None,  # noqa: ANN001 -- os.add_dll_directory, injectable for tests
) -> list[str]:
    """On Windows without a system VC++ runtime, put nx's app-local copies on the
    DLL search path. Returns the directories added (empty elsewhere)."""
    import os  # noqa: PLC0415 -- keep the package import light

    if (platform if platform is not None else sys.platform) != "win32":
        return []
    system = system_dir or os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    if all(os.path.isfile(os.path.join(system, d)) for d in _VC_RUNTIME_DLLS):
        return []
    # Mirrors nexus.config.nexus_config_dir without importing it here.
    cfg = config_dir or os.environ.get("NEXUS_CONFIG_DIR", "").strip() or os.path.join(
        os.path.expanduser("~"), ".config", "nexus")
    adder = add if add is not None else getattr(os, "add_dll_directory", None)
    if adder is None:
        return []
    added: list[str] = []
    for d in (os.path.join(cfg, "service"), os.path.join(cfg, "pg-bundle", "bundle", "bin")):
        if all(os.path.isfile(os.path.join(d, n)) for n in _VC_RUNTIME_DLLS):
            try:
                _VC_DLL_DIR_HANDLES.append(adder(d))
                added.append(d)
            except OSError:
                continue
    return added


_add_vc_runtime_dirs()
