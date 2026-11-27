# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 0 probe (nexus-sby49.1): does the Windows profile API ignore env?

The plugin-portability plan (section 1.1, invariant 4) resolves the OS-account
home on Windows with ``SHGetKnownFolderPath(FOLDERID_Profile)`` through ctypes,
so no env a repository sets (``USERPROFILE``, ``HOME``) can hide the maintainer
flag. This measures that claim on native Windows, in-process and in a child
whose environment was set at creation. Stdlib only. Skips off Windows: it is
a measurement for ``scripts/windows_hand_test.py``, not a Phase 1 test, and the
Phase 1 ``_audience`` tests supersede it.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows profile API probe")

_FOLDERID_PROFILE = "{5E6C858F-0E22-4760-9AFE-EA3317B67173}"
_FAKE = r"C:\nx-fake-profile"

_PROBE = r'''
import ctypes, os, sys, uuid
from ctypes import wintypes

class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

def profile_home():
    g = uuid.UUID("%s")
    guid = GUID(g.time_low, g.time_mid, g.time_hi_version,
                (ctypes.c_ubyte * 8)(*g.bytes[8:]))
    path = ctypes.c_wchar_p()
    fn = ctypes.windll.shell32.SHGetKnownFolderPath
    fn.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                   ctypes.POINTER(ctypes.c_wchar_p)]
    hr = fn(ctypes.byref(guid), 0, None, ctypes.byref(path))
    if hr != 0:
        raise OSError("SHGetKnownFolderPath hr=%%#x" %% (hr & 0xFFFFFFFF))
    try:
        return path.value
    finally:
        ctypes.windll.ole32.CoTaskMemFree(path)

print(profile_home())
print(os.environ.get("USERPROFILE"))
print(os.path.expanduser("~"))
''' % _FOLDERID_PROFILE


def _run_child(env_overrides: dict[str, str]) -> list[str]:
    env = {**os.environ, **env_overrides}
    out = subprocess.run([sys.executable, "-c", _PROBE], env=env, capture_output=True,
                         text=True, encoding="utf-8", check=True, timeout=60).stdout
    return out.splitlines()


def test_profile_api_ignores_changed_userprofile_and_home() -> None:
    base_api, base_up, base_expand = _run_child({})
    changed_api, changed_up, changed_expand = _run_child(
        {"USERPROFILE": _FAKE, "HOME": _FAKE})
    print(f"PROBE baseline: api={base_api!r} USERPROFILE={base_up!r} expanduser={base_expand!r}")
    print(f"PROBE changed:  api={changed_api!r} USERPROFILE={changed_up!r} expanduser={changed_expand!r}")
    assert base_api and os.path.isdir(base_api)
    assert changed_up == _FAKE, "the child did not see the changed USERPROFILE"
    assert changed_expand == _FAKE, "expanduser should follow the env (the control)"
    assert changed_api == base_api, "profile API followed USERPROFILE/HOME"
    assert changed_api != _FAKE
