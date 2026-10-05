# SPDX-License-Identifier: AGPL-3.0-or-later
"""Owner-only files on every platform (RDR-224, nexus-f9bgu.22).

A credential file is written ``0o600`` on POSIX. On Windows ``os.chmod`` sets
only the read-only attribute and ``os.stat`` reports ``0o666`` for everything,
so the same call restricts nothing and the same stat check refuses nothing.
This module is the one place that knows the difference:

* :func:`open_private` and :func:`restrict_to_owner` create or tighten a file
  so only the current user can reach it. POSIX does exactly what the call
  sites did before (``os.open(..., 0o600)`` / ``os.chmod(path, 0o600)``).
  Windows installs a PROTECTED DACL (no inheritance) with one ACE, full control
  for the current user's SID.
* :func:`owner_only_problem` is the reader's check: POSIX looks at the mode
  bits, Windows reads the DACL and names any trustee beyond the current user,
  SYSTEM and Administrators (the Windows counterparts of ``root``).
* :func:`ensure_owner_only` tightens a file that is already there.

The DACL goes on BEFORE any secret byte is written. A site that publishes with
``os.replace`` must apply it to the temp file: a rename keeps the SOURCE's
security descriptor, so an ACL set on the old destination would be replaced
along with it.

Stdlib only, ctypes imported where used: ``conexus/hooks/scripts/_endpoint_resolve.py``
cannot import nexus and carries a mirror of :func:`owner_only_problem` and
:func:`_windows_dacl_trustees`; ``tests/test_winsec.py`` pins the two together.
``platform`` and the three Windows lookups are seams, so the Windows branch
runs under test on every host.
"""
from __future__ import annotations

import contextlib
import os
import stat
from pathlib import Path
from typing import Callable

__all__ = [
    "ensure_owner_only",
    "grant_user_tree_access",
    "open_private",
    "owner_only_problem",
    "restrict_to_owner",
]

#: Well-known SIDs the reader tolerates beside the current user: the account
#: Windows services run as, the Administrators group, and OWNER RIGHTS (the
#: file's owner, which is the user again). SYSTEM and Administrators are what
#: ``root`` is to a POSIX ``0600`` file, and a default user-profile ACL carries
#: them, so refusing them would refuse every file this module did not write.
_SID_SYSTEM = "S-1-5-18"
_SID_ADMINISTRATORS = "S-1-5-32-544"
_SID_OWNER_RIGHTS = "S-1-3-4"

#: Full control for one SID, protected (``P``) so nothing is inherited.
_OWNER_ONLY_SDDL = "D:P(A;;FA;;;{sid})"

#: Full control for one SID, inheritable by files (OI) and directories (CI),
#: with inheritance from the parent left on (no ``P``).
_USER_TREE_SDDL = "D:(A;OICI;FA;;;{sid})"


def _is_windows(platform: str | None) -> bool:
    return (platform == "win32") if platform is not None else (os.name == "nt")


def _windows_user_sid() -> str:
    """The current process token's user SID as a string (``S-1-5-21-...``).

    Windows only; ctypes against advapi32. Any other platform raises OSError.
    """
    if os.name != "nt":
        raise OSError("the Windows user SID is only readable on Windows")
    import ctypes  # noqa: PLC0415 — Windows-only branch
    from ctypes import wintypes  # noqa: PLC0415 — Windows-only branch

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD()
        # TokenUser = 1. The sizing call fails by design (ERROR_INSUFFICIENT_BUFFER) and fills `needed`.
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if needed.value == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        buf = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(token, 1, buf, needed, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        # TOKEN_USER starts with SID_AND_ATTRIBUTES { PSID Sid; DWORD Attributes }: the first pointer is the SID.
        psid = ctypes.c_void_p.from_buffer(buf).value
        string_sid = ctypes.c_void_p()
        if not advapi32.ConvertSidToStringSidW(psid, ctypes.byref(string_sid)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.wstring_at(string_sid.value)
        finally:
            kernel32.LocalFree(string_sid)
    finally:
        kernel32.CloseHandle(token)


def _windows_set_owner_only_dacl(path: str, sid: str) -> None:
    """Replace *path*'s DACL with one ACE: full control for *sid*, protected.

    The SDDL string is turned into a security descriptor and its DACL handed to
    ``SetNamedSecurityInfoW`` with ``PROTECTED_DACL_SECURITY_INFORMATION``, the
    documented way to cut inheritance. Windows only; raises OSError on failure.
    """
    _windows_set_dacl(path, _OWNER_ONLY_SDDL.format(sid=sid), protected=True)


def _windows_grant_user_tree(path: str, sid: str) -> None:
    """Add an explicit, inheritable full-control ACE for *sid* to *path* and keep
    inheritance from the parent switched on. Windows only; raises OSError."""
    _windows_set_dacl(path, _USER_TREE_SDDL.format(sid=sid), protected=False)


def _windows_set_dacl(path: str, sddl: str, *, protected: bool) -> None:
    """Hand the DACL of *sddl* to ``SetNamedSecurityInfoW`` for *path*.

    *protected* True cuts inheritance (PROTECTED_DACL_SECURITY_INFORMATION);
    False turns it on and lets Windows merge the parent's inheritable ACEs back
    in (UNPROTECTED_DACL_SECURITY_INFORMATION). Windows only.
    """
    if os.name != "nt":
        raise OSError("a Windows DACL can only be set on Windows")
    import ctypes  # noqa: PLC0415 — Windows-only branch
    from ctypes import wintypes  # noqa: PLC0415 — Windows-only branch

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD

    descriptor = ctypes.c_void_p()
    # SDDL_REVISION_1 = 1
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), None,
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if not advapi32.GetSecurityDescriptorDacl(
            descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not present.value or not dacl.value:
            raise OSError("the security descriptor carries no DACL")
        # SE_FILE_OBJECT = 1; DACL_SECURITY_INFORMATION (4) with PROTECTED (0x80000000)
        # or UNPROTECTED (0x20000000)
        flags = 0x00000004 | (0x80000000 if protected else 0x20000000)
        error = advapi32.SetNamedSecurityInfoW(path, 1, flags, None, None, dacl, None)
        if error:
            raise ctypes.WinError(error)
    finally:
        kernel32.LocalFree(descriptor)


def _windows_dacl_trustees(path: str) -> list[str] | None:
    """The SID of every ACE in *path*'s DACL that GRANTS access, or ``None``
    when the file has no DACL at all (a NULL DACL grants everyone everything).

    Deny ACEs grant nothing and are skipped. An ACE type this walk cannot read
    (object ACEs, anything new) is reported as ``<ace-type-N>`` so the caller
    fails closed rather than counting it as harmless. Windows only; raises
    OSError when the descriptor cannot be read.
    """
    if os.name != "nt":
        raise OSError("a Windows DACL can only be read on Windows")
    import ctypes  # noqa: PLC0415 — Windows-only branch
    from ctypes import wintypes  # noqa: PLC0415 — Windows-only branch

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    class AclSizeInformation(ctypes.Structure):
        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    # SE_FILE_OBJECT = 1; DACL_SECURITY_INFORMATION = 4
    error = advapi32.GetNamedSecurityInfoW(
        path, 1, 0x00000004, None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor),
    )
    if error:
        raise ctypes.WinError(error)
    try:
        if not dacl.value:
            return None
        info = AclSizeInformation()
        # AclSizeInformation = 2
        if not advapi32.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2):
            raise ctypes.WinError(ctypes.get_last_error())
        trustees: list[str] = []
        for index in range(info.AceCount):
            ace = ctypes.c_void_p()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            ace_type = ctypes.string_at(ace.value, 1)[0]
            if ace_type in (1, 10):  # ACCESS_DENIED_ACE_TYPE, ACCESS_DENIED_CALLBACK_ACE_TYPE
                continue
            if ace_type not in (0, 9):  # ACCESS_ALLOWED_ACE_TYPE, ACCESS_ALLOWED_CALLBACK_ACE_TYPE
                trustees.append(f"<ace-type-{ace_type}>")
                continue
            # ACCESS_ALLOWED_ACE: ACE_HEADER (4 bytes), ACCESS_MASK (4 bytes), then the SID.
            string_sid = ctypes.c_void_p()
            if not advapi32.ConvertSidToStringSidW(ctypes.c_void_p(ace.value + 8), ctypes.byref(string_sid)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                trustees.append(ctypes.wstring_at(string_sid.value))
            finally:
                kernel32.LocalFree(string_sid)
        return trustees
    finally:
        kernel32.LocalFree(descriptor)


def owner_only_problem(
    path: str | os.PathLike[str],
    st_mode: int,
    *,
    platform: str | None = None,
    sid_lookup: Callable[[], str] | None = None,
    trustees_lookup: Callable[[str], list[str] | None] | None = None,
) -> str | None:
    """Why *path* is readable by someone besides its owner, or ``None`` when it is not.

    POSIX: any group or other mode bit (*st_mode* is the caller's own
    ``stat``, so the check runs on the file the caller opened). Windows
    ignores *st_mode*, which says ``0o666`` for every file, and reads the DACL:
    a file with no DACL, or one that grants anyone except the current user,
    SYSTEM, Administrators and OWNER RIGHTS, is refused, naming the extra SIDs.
    """
    if not _is_windows(platform):
        mode = stat.S_IMODE(st_mode)
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            return f"group/other-accessible (mode {oct(mode)})"
        return None
    try:
        user = (sid_lookup if sid_lookup is not None else _windows_user_sid)()
        trustees = (trustees_lookup if trustees_lookup is not None else _windows_dacl_trustees)(str(path))
    except (OSError, AttributeError, ValueError) as exc:
        return f"its access list cannot be read ({exc})"
    if trustees is None:
        return "has no access list, so every account can read it"
    foreign = sorted({t for t in trustees if t not in (user, _SID_SYSTEM, _SID_ADMINISTRATORS, _SID_OWNER_RIGHTS)})
    if foreign:
        return f"accessible to other accounts ({', '.join(foreign)})"
    return None


def restrict_to_owner(
    path: str | os.PathLike[str],
    *,
    platform: str | None = None,
    sid_lookup: Callable[[], str] | None = None,
    acl_apply: Callable[[str, str], None] | None = None,
) -> None:
    """Make *path* reachable by the current user only.

    POSIX: ``os.chmod(path, 0o600)``. Windows: a protected one-ACE DACL; a
    failure (an ACL-less filesystem, say) raises OSError, because a credential
    file left readable is worse than a credential that is not written.
    """
    if not _is_windows(platform):
        os.chmod(path, 0o600)
        return
    sid = (sid_lookup if sid_lookup is not None else _windows_user_sid)()
    (acl_apply if acl_apply is not None else _windows_set_owner_only_dacl)(str(path), sid)


def open_private(
    path: str | os.PathLike[str],
    flags: int,
    *,
    platform: str | None = None,
    sid_lookup: Callable[[], str] | None = None,
    acl_apply: Callable[[str, str], None] | None = None,
) -> int:
    """``os.open(path, flags, 0o600)`` that is owner-only on Windows too, before the caller writes.

    The ACL goes on while the file is still empty. When it cannot be applied
    the descriptor is closed, an empty file this call just made is removed
    (a non-empty one is somebody's data and stays), and the error propagates.
    """
    fd = os.open(str(path), flags, 0o600)
    if not _is_windows(platform):
        return fd
    try:
        restrict_to_owner(path, platform=platform, sid_lookup=sid_lookup, acl_apply=acl_apply)
    except BaseException:
        empty = os.fstat(fd).st_size == 0
        os.close(fd)
        if empty:
            with contextlib.suppress(OSError):
                Path(path).unlink()
        raise
    return fd


def ensure_owner_only(
    path: str | os.PathLike[str],
    *,
    platform: str | None = None,
    sid_lookup: Callable[[], str] | None = None,
    trustees_lookup: Callable[[str], list[str] | None] | None = None,
    acl_apply: Callable[[str, str], None] | None = None,
) -> None:
    """Tighten *path* when :func:`owner_only_problem` finds it open, leave it alone otherwise."""
    if owner_only_problem(
        path, Path(path).stat().st_mode,
        platform=platform, sid_lookup=sid_lookup, trustees_lookup=trustees_lookup,
    ) is not None:
        restrict_to_owner(path, platform=platform, sid_lookup=sid_lookup, acl_apply=acl_apply)


def grant_user_tree_access(
    path: str | os.PathLike[str],
    *,
    platform: str | None = None,
    sid_lookup: Callable[[], str] | None = None,
    acl_apply: Callable[[str, str], None] | None = None,
) -> None:
    """Give the current user an explicit, inheritable full-control ACE on the
    directory *path*, so every file created under it afterwards carries one.

    WHY (nexus-f9bgu.18, measured on Windows 11 / Python 3.12.13, elevated
    session). PostgreSQL's ``initdb`` and ``postgres`` re-run themselves under
    a restricted token that marks Administrators deny-only. A directory made
    with ``Path.mkdir(mode=0o700)`` (Python 3.12.4 and later apply an ACL for
    0o700 on Windows; nexus makes its config dir that way in several places)
    carries ACEs for SYSTEM, Administrators and OWNER RIGHTS only, and files an
    elevated process creates under it are owned by Administrators. Under the
    restricted token none of those ACEs grants anything, so ``initdb`` dies
    ``0xC0000135`` with no message. An explicit ACE for the user's own SID does
    grant, and it must be on BOTH the extracted bundle tree and the data
    directory (each alone fails). POSIX: nothing to do.
    """
    if not _is_windows(platform):
        return
    sid = (sid_lookup if sid_lookup is not None else _windows_user_sid)()
    (acl_apply if acl_apply is not None else _windows_grant_user_tree)(str(path), sid)
