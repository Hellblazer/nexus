# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Where the Windows user PATH lives: the registry, or a file in a sandboxed run.

Split out of ``generation_core`` to keep it under its size cap (nexus-f9bgu.47).
``generation_core`` re-exports these names, and its ``ensure_user_path``,
``remove_user_path`` and ``inspect_user_path`` use them. Standard library only and
importing nothing from nexus, like its siblings.
"""
from __future__ import annotations

import os
from pathlib import Path

#: Registry value types.
REG_SZ = 1
REG_EXPAND_SZ = 2


class UserPathStore:
    """Where the persistent user PATH lives. The seam for the registry read,
    write and broadcast; subclasses below are the registry and a file."""

    kind = "abstract"

    def read(self) -> tuple[str, int]:
        """``(value, registry type)``; an absent value is ``("", REG_EXPAND_SZ)``."""
        raise NotImplementedError

    def write(self, value: str, reg_type: int) -> None:
        raise NotImplementedError

    def broadcast(self) -> None:
        """Tell running programs the environment changed. Best effort."""


class RegistryUserPath(UserPathStore):
    """``HKCU\\Environment`` ``Path``, preserving its registry type. Never HKLM."""

    kind = "HKCU\\Environment"

    def read(self) -> tuple[str, int]:
        import winreg  # noqa: PLC0415 -- exists only on Windows

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
            try:
                value, kind = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                return "", REG_EXPAND_SZ
        return str(value), int(kind)

    def write(self, value: str, reg_type: int) -> None:
        import winreg  # noqa: PLC0415 -- exists only on Windows

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, "Path", 0, reg_type, value)

    def broadcast(self) -> None:
        import ctypes  # noqa: PLC0415 -- Windows only
        from ctypes import wintypes  # noqa: PLC0415

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        send = user32.SendMessageTimeoutW
        send.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_wchar_p,
            wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
        ]
        send.restype = ctypes.c_ssize_t
        result = ctypes.c_size_t()
        # HWND_BROADCAST, WM_SETTINGCHANGE, "Environment", SMTO_ABORTIFHUNG, 5 s.
        send(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result))


class FileUserPath(UserPathStore):
    """The sandbox store: one line in a file. No registry, no broadcast."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.kind = str(self.path)

    def read(self) -> tuple[str, int]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return "", REG_EXPAND_SZ
        return text.split("\n", 1)[0].rstrip("\r"), REG_EXPAND_SZ

    def write(self, value: str, reg_type: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp.{os.getpid()}")
        tmp.write_text(value + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
