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
installed engine directory and the bundle's ``bin`` join the DLL search path, and
so does ``<config>/vcrt``, where a cloud-mode install (no engine, no bundle) gets
the two DLLs from ``nx init`` or ``nx upgrade --auto``.
"""
from __future__ import annotations

import sys


def _windows_version(winver=None) -> str:  # noqa: ANN001 -- sys.getwindowsversion()'s result, injectable for tests
    """The OS version WMI would report, as ``major.minor.build``, without WMI.

    ``getwindowsversion()[:3]`` is the real build when the interpreter declares
    Windows 10 support (uv's CPython does), while ``platform_version`` is
    kernel32.dll's file version, which lags on a build delivered as an
    enablement package: measured 2026-10-14 on Windows 11 25H2, WMI, the
    registry and ``ver`` all said 10.0.26200, ``[:3]`` said 26200 and
    ``platform_version`` said 26100. An interpreter without that manifest gets
    6.2 from ``[:3]``, so the larger of the two is taken.
    """
    w = winver if winver is not None else sys.getwindowsversion()  # type: ignore[attr-defined]  # Windows-only
    best = max(tuple(w[:3]), tuple(getattr(w, "platform_version", None) or w[:3]))
    return "{}.{}.{}".format(*best)


def _prime_platform_without_wmi(platform: str | None = None, platform_module=None, winver=None) -> bool:  # noqa: ANN001 -- the stdlib platform module and sys.getwindowsversion()'s result, injectable for tests
    """On Windows, fill ``platform``'s ``uname()`` cache without a WMI query.

    truststore's import calls ``platform.system()``, which on Windows asks WMI
    through ``_wmi.exec_query``. CPython 3.12's ``_wmi`` gives up after 1000 ms
    (COM start) or 100 ms (connect) and returns while its query thread still
    holds a pointer into the returned stack frame; that thread then closes
    handles through it and the process dies with 0xC000070A, no traceback.
    Under a session start's concurrent hook launches this crashed nx-hook on
    every Home guest run (nexus-f9bgu.55). 3.13 copies the data into the
    thread; 3.12 is security-only.

    ``uname()`` runs once here with the WMI query turned off and the ``ver``
    command answered by :func:`_windows_version` instead of a ``cmd`` child,
    so it takes its documented fallback: that version, ``PROCESSOR_ARCHITECTURE``
    for the machine, and ``platform``'s own release mapping. The cache it leaves is what
    ``system()``, ``release()`` and ``machine()`` read for the rest of the
    process. Both module functions are restored. True when the cache holds a
    value afterwards; False, with nothing changed, when this ``platform`` lacks
    the names relied on or ``uname()`` fails.
    """
    if (platform if platform is not None else sys.platform) != "win32":
        return False
    if platform_module is None:
        import platform as platform_module  # noqa: PLC0415 -- only Windows pays for it
    # The names are private to platform, hence getattr/setattr by name.
    mod = platform_module
    if not all(hasattr(mod, n) for n in ("_wmi_query", "_syscmd_ver", "_uname_cache", "uname")):
        return False
    if getattr(mod, "_uname_cache") is not None:
        return True
    try:
        version = _windows_version(winver)
    except Exception:  # noqa: BLE001 -- no version to offer: leave platform alone
        return False

    def _no_wmi(*_args: object, **_kwargs: object) -> object:
        raise OSError("WMI skipped at nexus import (nexus-f9bgu.55)")

    def _no_ver(system: str = "", release: str = "", _version: str = "", *_args: object, **_kwargs: object) -> tuple[str, str, str]:
        return system, release, version

    saved = (getattr(mod, "_wmi_query"), getattr(mod, "_syscmd_ver"))
    setattr(mod, "_wmi_query", _no_wmi)
    setattr(mod, "_syscmd_ver", _no_ver)
    try:
        mod.uname()
    except Exception:  # noqa: BLE001 -- leave platform to compute uname its own way
        setattr(mod, "_uname_cache", None)
        return False
    finally:
        setattr(mod, "_wmi_query", saved[0])
        setattr(mod, "_syscmd_ver", saved[1])
    return getattr(mod, "_uname_cache") is not None


def _use_os_trust_store(platform: str | None = None) -> bool:
    """Route ``ssl`` verification through the OS on Windows. True when it did."""
    if (platform if platform is not None else sys.platform) != "win32":
        return False
    _prime_platform_without_wmi(platform="win32")
    try:
        import truststore  # noqa: PLC0415 -- Windows-only dependency
    except ImportError:  # a dev environment without the dependency: OpenSSL as before
        return False
    truststore.inject_into_ssl()
    return True


_use_os_trust_store()

from nexus import _vcrt  # noqa: E402 -- stdlib-only, import-cheap

#: The VC++ runtime DLLs the failing extension modules need (see _vcrt).
_VC_RUNTIME_DLLS: tuple[str, ...] = _vcrt.VC_RUNTIME_DLLS

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
    DLL search path: the engine dir, the PG bundle's bin, and ``<config>/vcrt``
    (which ``nx init`` / ``nx upgrade --auto`` fill on a cloud-mode install).

    When System32 has the base pair but not ``msvcp140_atomic_wait.dll`` (an old
    redistributable; torch 2.13 imports it, nexus-vyy65), the directories that carry the
    WHOLE PDF set are added too, and only those: a base-only directory would shadow
    System32's own ``msvcp140`` for nothing. Returns the directories added (empty
    elsewhere)."""
    import os  # noqa: PLC0415 -- keep the package import light

    if not _vcrt.is_windows(platform):
        return []
    sysdir = system_dir or _vcrt.system_dir()
    if _vcrt.has_runtime(sysdir):
        if _vcrt.has_runtime(sysdir, _vcrt.PDF_STACK_DLLS):
            return []
        needed = _vcrt.PDF_STACK_DLLS
    else:
        needed = _vcrt.VC_RUNTIME_DLLS
    adder = add if add is not None else getattr(os, "add_dll_directory", None)
    if adder is None:
        return []
    added: list[str] = []
    for d in _vcrt.app_local_dirs(config_dir or _vcrt.default_config_dir()):
        if _vcrt.has_runtime(d, needed):
            try:
                _VC_DLL_DIR_HANDLES.append(adder(d))
                added.append(d)
            except OSError:
                continue
    return added


_add_vc_runtime_dirs()
