#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stdlib-only, no-``nexus``-import mirror of the client's nexus-service
endpoint-resolution primitives (nexus-vg6d4) -- the ONE shared module for
every hook script under ``conexus/hooks/scripts/`` that must resolve the
service base URL and read its on-disk lease/credential files without
importing the ``nexus`` package.

Factored out under nexus-aginu, after nexus-0zsmg: ``t2_prefix_scan.py``
carried the first stdlib mirror of this precedence, ``routing/_lib.py``
ported it "verbatim" (nexus-gjv9b), and a third plugin script (since
deleted) was a THIRD independent copy -- and the one that
fell behind: its mirror lacked the persisted ``config.yml`` ``service_url``
leg the other two already had. A drift between hand-maintained copies is
exactly how that happened. This module is the single source for the parts
that were actually drifting.

WHAT THIS MODULE OWNS: the raw building blocks -- config-dir resolution,
lease-file reads, ``config.yml`` credential parsing, data-token-lease
matching, and the full base-URL precedence mirroring
``nexus.db.service_endpoint.resolve_service_endpoint``:

  1. ``NX_SERVICE_URL`` env.
  2. The persisted ``config.yml`` ``credentials.service_url``
     (``nx config set service_url``).
  3. ``NX_SERVICE_HOST``/``NX_SERVICE_PORT`` env (host filled from a live
     local supervisor lease when PORT is set but HOST is not -- nexus-aginu,
     matching ``resolve_service_config``'s per-field lease merge).
  4. The local ``ServiceRegistry`` supervisor lease
     (``storage_service_addr.<identity>``).

WHAT THIS MODULE DOES NOT OWN: which CREDENTIAL a caller presents once the
base URL is known. Callers differ here BY DESIGN, not by drift:
``t2_prefix_scan.py``/``routing/_lib.py`` are synchronous GET/POST callers
that accept a persisted-config or env static ``service_token`` as a last
resort -- a real, documented managed-onboarding path (``nx config set
service_token``). :func:`resolve_base_url` reports which leg won
(``is_local_supervisor``) so a caller can implement either credential
policy without re-deriving the lease/config reads.
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any, Callable

if sys.version_info < (3, 12):
    sys.stderr.write(
        f"ERROR: conexus plugin hook requires Python 3.12+, got {sys.version.split()[0]}\n"
        f"  Resolved: {sys.executable}\n"
        f"  Install: brew install python@3.13 (macOS) | apt install python3.12 (Ubuntu) | uv python install 3.12\n"
    )
    sys.exit(1)

#: Mirrors ``nexus.daemon.service_registry``'s tier name for the shared
#: nexus-service engine -- the lease file is
#: ``<config_dir>/storage_service_addr.<uid>``.
STORAGE_SERVICE_TIER = "storage_service"

#: Mirrors ``nexus.db.data_token``'s cross-process DATA-token lease
#: filename prefix: ``<config_dir>/data_token_lease.<sha256(host[:port]
#: \x00tenant)>``.
DATA_TOKEN_LEASE_PREFIX = "data_token_lease."
DATA_TOKEN_LEASE_FORMAT_VERSION = 1

#: The tenant every ``Http*Store`` in this repo is constructed with.
#: Matches ``nexus.db.t2.http_tuple_store.DEFAULT_TENANT`` (and its
#: siblings' identical constant) -- never a non-default tenant anywhere
#: these hooks' reads/writes correspond to.
DEFAULT_TENANT = "default"


class EndpointUnresolvable(Exception):
    """No endpoint/credential combination could be resolved. The message
    names every leg that was checked and why, never a bare 'not found'."""


def default_config_dir() -> Path:
    """Stdlib-only counterpart of ``nexus.config.nexus_config_dir``, not a mirror: this also honours
    ``NX_CONFIG_DIR`` (after ``NEXUS_CONFIG_DIR``), which the CLI's function does not. Whether the CLI should
    is an open decision (nexus-sv2wx.22); ``nx doctor``'s "Plugin setup" row resolves its directory this
    way, and ``tests/test_doctor_setup_row.py`` compares it with ``nx_setup.setup_dir()``."""
    config_dir = os.environ.get("NEXUS_CONFIG_DIR") or os.environ.get("NX_CONFIG_DIR")
    if config_dir:
        return Path(config_dir)
    return Path.home() / ".config" / "nexus"


class ServiceIdentityError(RuntimeError):
    """Mirror of ``nexus.daemon.service_registry.ServiceIdentityError``."""


#: Mirror of ``service_registry._SID_PATTERN`` (a parity test compares the two).
_SID_PATTERN = re.compile(r"S-1-\d+(-\d+)+")


#: The current process token's user SID, read once per process (RDR-224,
#: nexus-f9bgu.33, review m11). A token's user cannot change, and the lease
#: writer asked for it on every heartbeat.
_SID_CACHE: list[str] = []

#: advapi32 and kernel32 with every ``argtypes`` / ``restype`` the calls below
#: need, bound once per process: a ``WinDLL`` load plus a dozen attribute
#: assignments on every lease write is pure overhead.
_WIN_LIBS: list[tuple[Any, Any]] = []


def _win_libs(*, loader: Callable[[str], Any] | None = None) -> tuple[Any, Any]:
    """``(advapi32, kernel32)``, bound once. *loader* is the test seam for
    ``ctypes.WinDLL``; without it, anything but Windows raises OSError."""
    if _WIN_LIBS:
        return _WIN_LIBS[0]
    if loader is None and os.name != "nt":
        raise OSError("the Windows security APIs are only reachable on Windows")
    import ctypes  # noqa: PLC0415 — Windows-only branch
    from ctypes import wintypes  # noqa: PLC0415 — Windows-only branch

    load = loader if loader is not None else (lambda name: ctypes.WinDLL(name, use_last_error=True))
    advapi32 = load("advapi32")
    kernel32 = load("kernel32")
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
    _WIN_LIBS.append((advapi32, kernel32))
    return _WIN_LIBS[0]


def _windows_user_sid() -> str:
    """The current process token's user SID as a string (``S-1-5-21-...``).

    Windows only; ctypes against advapi32. Any other platform raises OSError.
    """
    if _SID_CACHE:
        return _SID_CACHE[0]
    if os.name != "nt":
        raise OSError("the Windows user SID is only readable on Windows")
    import ctypes  # noqa: PLC0415 — Windows-only branch
    from ctypes import wintypes  # noqa: PLC0415 — Windows-only branch

    advapi32, kernel32 = _win_libs()

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
            sid = ctypes.wstring_at(string_sid.value)
        finally:
            kernel32.LocalFree(string_sid)
        _SID_CACHE.append(sid)
        return sid
    finally:
        kernel32.CloseHandle(token)


def service_identity(
    *,
    platform: str | None = None,
    getuid: Callable[[], int] | None = None,
    sid_lookup: Callable[[], str] | None = None,
) -> str:
    """Stdlib mirror of ``nexus.daemon.service_registry.service_identity``:
    ``str(os.getuid())`` on POSIX, the user SID on Windows. This file must not
    import ``nexus``, so the derivation is restated here and a parity test
    (``tests/daemon/test_service_identity.py``) pins the two together.
    """
    on_windows = (platform == "win32") if platform is not None else (os.name == "nt")
    if not on_windows:
        return str(getuid() if getuid is not None else os.getuid())
    try:
        sid = (sid_lookup if sid_lookup is not None else _windows_user_sid)()
    except (OSError, AttributeError, ValueError) as exc:
        raise ServiceIdentityError(f"cannot read the Windows user SID: {exc}") from exc
    if not _SID_PATTERN.fullmatch(sid):
        raise ServiceIdentityError(f"the Windows user identity is not a SID: {sid!r}")
    return sid


#: Mirror of ``nexus._winsec``'s tolerated SIDs (SYSTEM, Administrators: the
#: Windows counterparts of ``root`` on a POSIX ``0600`` file; OWNER RIGHTS: the
#: file's owner, the user again).
_SID_SYSTEM = "S-1-5-18"
_SID_ADMINISTRATORS = "S-1-5-32-544"
_SID_OWNER_RIGHTS = "S-1-3-4"


def _is_windows(platform: str | None) -> bool:
    return (platform == "win32") if platform is not None else (os.name == "nt")


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

    advapi32, kernel32 = _win_libs()

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


def storage_service_lease_path(config_dir: Path) -> Path:
    return config_dir / f"{STORAGE_SERVICE_TIER}_addr.{service_identity()}"


def _shown(lease_path: Path | None) -> str:
    return str(lease_path) if lease_path is not None else "<the Windows user identity cannot be read>"


def _lease_path_or_none(config_dir: Path) -> Path | None:
    """:func:`storage_service_lease_path`, or ``None`` when the Windows user SID
    cannot be read (:class:`ServiceIdentityError`). Every hook caller degrades
    "no lease" on a failure here, so an identity that cannot be read must never
    raise out of one (RDR-224, nexus-f9bgu.33, review m10)."""
    try:
        return storage_service_lease_path(config_dir)
    except ServiceIdentityError:
        return None


#: Mirror of ``service_registry._WINDOWS_SHARING_RETRY_*`` (a parity test
#: compares the constants and the pause sequence): how long a lease read keeps
#: retrying a Windows sharing violation, with a doubling pause between tries.
_WINDOWS_SHARING_RETRY_BUDGET_S = 2.0
_WINDOWS_SHARING_RETRY_FIRST_S = 0.002
_WINDOWS_SHARING_RETRY_MAX_S = 0.01


#: The longest the SESSION-START HOOK lets a lease read retry a Windows sharing
#: violation (RDR-228 Phase 2 Step 4, nexus-sv2wx.17; residual nexus-9i4d4 item 9).
#: The registry's own budget is two seconds, which suits a client that can wait; the
#: hook has 100 ms in all, so it retries for at most this long and then reads the
#: lease as absent. A miss then reports "no service" for that one session start,
#: which is the safe side of the same question the hook asks again next time.
HOOK_LEASE_RETRY_BUDGET_S = 0.03


def read_lease_text(
    path: Path,
    *,
    platform: str | None = None,
    read: Callable[[Path], str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    budget_s: float | None = None,
) -> str:
    """Read the supervisor lease file, retrying a Windows sharing violation.

    Stdlib mirror of ``ServiceRegistry._retry_sharing_violation`` around its
    lease read (RDR-224, nexus-f9bgu.44). Off Windows this is a plain read: a
    ``PermissionError`` there is a real permission problem and fails at once.
    On Windows the supervisor's ``os.replace`` of the lease makes a concurrent
    reader's open raise ``PermissionError`` for an instant, so the read is
    retried with a doubling pause up to the budget and then raised. The callers
    treat any ``OSError`` as "no lease", so a miss during a replace no longer
    reads as a stopped service.
    """
    do_read = read if read is not None else (lambda p: p.read_text(encoding="utf-8"))
    if not _is_windows(platform):
        return do_read(path)
    deadline = monotonic() + (_WINDOWS_SHARING_RETRY_BUDGET_S if budget_s is None else budget_s)
    pause = _WINDOWS_SHARING_RETRY_FIRST_S
    while True:
        try:
            return do_read(path)
        except PermissionError:
            now = monotonic()
            if now >= deadline:
                raise
            sleep(min(pause, deadline - now))
            pause = min(pause * 2, _WINDOWS_SHARING_RETRY_MAX_S)


#: Mirror of ``service_registry.STALE_LEASE_GRACE_MAX_TTL_MULTIPLE`` and
#: ``_GRACE_PROBE_LOOPBACK_HOSTS`` (a parity test compares them).
STALE_LEASE_GRACE_MAX_TTL_MULTIPLE = 10.0
_GRACE_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})

#: Mirror of ``service_registry``'s Windows process-liveness constants.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_ERROR_INVALID_PARAMETER = 87
_WAIT_OBJECT_0 = 0x0
_MAX_WINDOWS_PID = 0xFFFFFFFF


class _KernelProcessApi:
    """The three kernel32 calls the Windows liveness probe makes
    (``service_registry._CtypesWinProcessApi``, restated: this file cannot import it)."""

    def __init__(self) -> None:
        import ctypes  # noqa: PLC0415 -- Windows-only, built on first use
        from ctypes import wintypes  # noqa: PLC0415

        self._ctypes = ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        k32.CloseHandle.restype = wintypes.BOOL
        self._k32 = k32

    def open_process(self, pid: int) -> tuple[int | None, int]:
        handle = self._k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid)
        if not handle:
            return None, self._ctypes.get_last_error()  # type: ignore[attr-defined]
        return handle, 0

    def wait_zero(self, handle: int) -> int:
        return self._k32.WaitForSingleObject(handle, 0)

    def close(self, handle: int) -> None:
        self._k32.CloseHandle(handle)


def pid_alive(pid: int, *, platform: str | None = None, win_api: Any = None) -> bool:
    """True when *pid* names a process that has not exited: stdlib mirror of
    ``nexus.daemon.service_registry.pid_alive`` (a parity test runs the two over the same
    cases, the Windows arm through the same fake API). Ambiguity reads as alive, as there.

    POSIX signals 0. Windows must not (CPython sends ``CTRL_C_EVENT`` there): it opens the
    process and asks whether it has exited. No process is started and nothing is written.
    """
    if pid <= 0:
        return False
    if _is_windows(platform):
        if pid > _MAX_WINDOWS_PID:
            return False
        api = win_api if win_api is not None else _KernelProcessApi()
        handle, last_error = api.open_process(pid)
        if handle is None:
            return last_error != _ERROR_INVALID_PARAMETER
        try:
            return api.wait_zero(handle) != _WAIT_OBJECT_0
        finally:
            api.close(handle)
    import errno  # noqa: PLC0415 -- lazy: only a stale lease reaches this

    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):  # no such process; or a number no pid can be (a corrupt lease)
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


def _stale_owner_alive(data: dict[str, Any], age: float, ttl: float) -> bool:
    """Whether a lease past its TTL should still be read as live, as far as a reader that
    makes no network request can tell: ``ServiceRegistry._stale_lease_still_live`` minus its
    ``/health`` probe. The guards, each sufficient to deny: not more than ten TTLs stale; a
    recorded ``payload.supervisor_pid`` that names a live process; a loopback host and a
    positive port. (The caller has already required status ``live``.)"""
    if age > ttl * STALE_LEASE_GRACE_MAX_TTL_MULTIPLE:
        return False
    payload = data.get("payload")
    pid = payload.get("supervisor_pid") if isinstance(payload, dict) else None
    if not (isinstance(pid, int) and pid > 0):
        return False
    if not pid_alive(pid):
        return False
    endpoint = data["endpoint"]
    if str(endpoint.get("host", "")) not in _GRACE_LOOPBACK_HOSTS:
        return False
    try:
        return int(endpoint.get("port", 0)) > 0
    except (TypeError, ValueError):
        return False


def read_storage_service_lease(
    config_dir: Path, *, retry_budget_s: float | None = None, grace: bool = False,
) -> dict[str, Any] | None:
    """Best-effort read of the local supervisor's ``ServiceRegistry``
    lease: ``{"host", "port", "token"}``, or ``None``. Any failure --
    missing file, unreadable, malformed JSON, non-``live`` status, or a
    heartbeat older than its TTL -- resolves to ``None``. Never raises.

    *retry_budget_s* bounds the Windows sharing-violation retry (default: the
    registry's two seconds); the session-start hook passes what is left of
    :data:`HOOK_READ_BUDGET_S`.

    *grace* (only :func:`endpoint_usable` passes it) keeps a lease past its TTL when its
    recorded owner process is alive, as the CLI does (:func:`_stale_owner_alive`). The
    other callers read a stale lease as absent, because they present its token.
    """
    path = _lease_path_or_none(config_dir)
    if path is None:
        return None
    try:
        data = json.loads(read_lease_text(path, budget_s=retry_budget_s))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    try:
        if str(data.get("status", "live")) != "live":
            return None
        heartbeat_epoch = float(data["heartbeat_epoch"])
        ttl = float(data["ttl"])
        endpoint = data["endpoint"]
        host = str(endpoint.get("host", "127.0.0.1"))
        port = int(endpoint.get("port", 0))
        token = str(endpoint.get("token", ""))
    except (KeyError, TypeError, ValueError):
        return None
    if port <= 0:
        return None
    age = time.time() - heartbeat_epoch
    if age >= ttl and not (grace and _stale_owner_alive(data, age, ttl)):
        return None
    return {"host": host, "port": port, "token": token}


def read_local_supervisor_token(config_dir: Path) -> str:
    """The LOCAL SUPERVISOR's own static token, straight off the
    ``storage_service_addr.<uid>`` lease record -- or raise
    :class:`EndpointUnresolvable` naming why.

    Refuses (never trusts) a lease file that is not owner-only: the token
    it carries authorizes real engine writes, and a group/other-readable
    lease file means some other local account could have read it too.
    Re-verifies the same liveness checks :func:`read_storage_service_lease`
    already applies (rather than taking a pre-checked value as a parameter)
    so this function is a complete, independent audit trail for the one
    credential-bearing read in this module.
    """
    path = _lease_path_or_none(config_dir)
    if path is None:
        raise EndpointUnresolvable(
            "local supervisor lease unavailable: the Windows user identity cannot be read"
        )
    try:
        st_result = path.stat()
    except OSError as exc:
        raise EndpointUnresolvable(
            f"local supervisor lease unavailable: cannot stat {path}: {exc}"
        ) from exc
    problem = owner_only_problem(path, st_result.st_mode)
    if problem is not None:
        raise EndpointUnresolvable(
            f"local supervisor lease {path} is {problem}; "
            "refusing to use its token as a bearer"
        )
    try:
        data = json.loads(read_lease_text(path))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise EndpointUnresolvable(
            f"local supervisor lease {path} unreadable/malformed: {exc}"
        ) from exc
    try:
        if str(data.get("status", "live")) != "live":
            raise EndpointUnresolvable(f"local supervisor lease {path} is not live")
        heartbeat_epoch = float(data["heartbeat_epoch"])
        ttl = float(data["ttl"])
        token = str(data["endpoint"].get("token", "") or "")
    except (KeyError, TypeError, ValueError) as exc:
        raise EndpointUnresolvable(f"local supervisor lease {path} malformed: {exc}") from exc
    if (time.time() - heartbeat_epoch) >= ttl:
        raise EndpointUnresolvable(f"local supervisor lease {path} is stale (past ttl)")
    if not token:
        raise EndpointUnresolvable(f"local supervisor lease {path} carries no token")
    return token


def _strip_inline_comment(value: str) -> str:
    """Truncate *value* at an unquoted, comment-starting ``#`` -- YAML's
    inline-comment rule: a ``#`` that is the first character, or is
    immediately preceded by whitespace, starts a comment UNLESS it falls
    inside a quoted scalar (tracked from position 0 only -- this is a
    narrow line scanner, not a real YAML parser, so a quote character
    that is not the value's own opening/closing quote is not specially
    handled). Nothing here changes when *value* has no unquoted ``#`` --
    the common case -- beyond a trailing-whitespace ``rstrip()``.
    """
    if not value:
        return value
    quote: str | None = None
    for i, ch in enumerate(value):
        if quote is not None:
            if ch == quote:
                quote = None
            continue
        if i == 0 and ch in ("'", '"'):
            quote = ch
            continue
        if ch == "#" and (i == 0 or value[i - 1] in " \t"):
            return value[:i].rstrip()
    return value.rstrip()


def _unquote(value: str) -> str:
    """*value* without one pair of matching single or double quotes around it."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _flow_body(text: str) -> str | None:
    """What lies between the ``{`` that *text* starts with and its matching ``}``,
    skipping quotes and nested brackets; ``None`` when the mapping does not close."""
    quote: str | None = None
    depth = 0
    for i, ch in enumerate(text):
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return text[1:i] if ch == "}" else None
    return None


def _flow_items(body: str) -> list[str]:
    """The comma-separated items of a flow mapping's inside, split outside quotes and
    outside nested brackets."""
    items: list[str] = []
    current: list[str] = []
    quote: str | None = None
    depth = 0
    for ch in body:
        if quote is not None:
            current.append(ch)
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            current.append(ch)
        elif ch in "{[":
            depth += 1
            current.append(ch)
        elif ch in "}]":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            items.append("".join(current))
            current = []
        else:
            current.append(ch)
    items.append("".join(current))
    return items


def _flow_credentials(text: str, keys: tuple[str, ...]) -> dict[str, str]:
    """*keys* from a flow mapping's inside, ``service_url: https://x, service_token: t``.

    A flat mapping of scalars only, which is all ``credentials`` holds. Plain and
    single- or double-quoted values (a quoted value may hold a comma); a key is a plain
    or quoted word followed by ``:`` and a space, as YAML requires in a flow mapping.
    Anything else in an item (a nested mapping or list, an anchor, an alias, a tag, a
    block scalar) is skipped, so a value this scan does not understand reads as absent
    and never as a guess.
    """
    result: dict[str, str] = {}
    for item in _flow_items(text):
        name, sep, value = item.strip().partition(":")
        if not sep or not (value[:1].isspace() or not value):
            continue
        name = _unquote(name.strip())
        value = value.strip()
        if value[:1] in ("{", "[", "&", "*", "!", "|", ">"):
            continue
        value = _unquote(value)
        if name in keys and value:
            result[name] = value
    return result


#: The most lines a flow-style ``credentials`` mapping is followed over.
_FLOW_LINES_MAX = 40


def read_config_yml_credentials(
    config_dir: Path, keys: tuple[str, ...] = ("service_url", "service_token"),
) -> dict[str, str]:
    """Bounded, stdlib-only extraction of ``service_url``/``service_token``
    (or the other *keys* asked for) from the persisted ``config.yml``.

    NOT a general YAML parser -- this module cannot import ``nexus`` or a
    third-party YAML library (nexus-vg6d4). It is a line-oriented scan
    restricted to exactly the two keys these hooks need, under a
    top-level ``credentials:`` block, matching the EXACT shape
    ``nexus.config.set_credential`` writes (``yaml.dump({"credentials":
    {...}}, default_flow_style=False)`` -- two-space indented ``key:
    value`` lines, no flow-style ``{...}``). A value PyYAML quoted
    (single or double, no embedded escapes) is unwrapped; a trailing
    inline ``# comment`` is stripped (nexus-aginu -- real YAML/PyYAML
    strips it too; the three pre-consolidation mirrors this module
    replaces did not, so a hand-edited config.yml with an inline comment
    on a credential line fed the comment text into the resolved value).
    Last-key-wins for a duplicate key (mirrors ``yaml.safe_load``).

    A hand-edited flow-style mapping, ``credentials: {service_url: https://x,
    service_token: t}`` on one line or over several (up to ``_FLOW_LINES_MAX``), is
    read too, because the CLI reads it (PyYAML) and a plugin that did not would say "no
    service" on a machine where every tool works (RDR-228 Phase 2 Step 4,
    nexus-sv2wx.17; ``tests/hooks/test_usable_service_agreement.py`` holds the two
    together). THE LIMITS, stated: a flow mapping of plain or quoted scalars, with no
    comment inside it; ``credentials`` as a key of a document-wide flow mapping
    (``{credentials: {...}}``), anchors and aliases, and block scalars are not read.

    Anything this narrow scanner does not recognize -- an escaped quote, a
    value spanning multiple lines in block style, the shapes above -- is silently
    skipped (that key absent from the result), never guessed: a caller's normal
    env/lease fallback takes over exactly as if the credential were absent, never
    mis-resolving a base URL from a misparse.

    Returns ``{}`` when the file is absent, unreadable, or has no
    ``credentials:`` block.
    """
    path = config_dir / "config.yml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}

    result: dict[str, str] = {}
    in_credentials = False
    cred_indent = 0
    lines = text.splitlines()
    for number, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if not in_credentials:
            if stripped == "credentials:":
                in_credentials = True
                cred_indent = indent
            elif indent == 0 and stripped.startswith("credentials:") and stripped[len("credentials:"):].strip().startswith("{"):
                flow = stripped[len("credentials:"):].strip()
                inside = _flow_body(flow)
                for more in lines[number + 1: number + _FLOW_LINES_MAX]:
                    if inside is not None:
                        break
                    flow += " " + more.strip()
                    inside = _flow_body(flow)
                return _flow_credentials(inside, keys) if inside is not None else {}
            continue
        if indent <= cred_indent:
            # Dedented back out of the credentials block -- done scanning.
            break
        for key in keys:
            prefix = f"{key}:"
            if not stripped.startswith(prefix):
                continue
            value = stripped[len(prefix):].strip()
            value = _strip_inline_comment(value)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            if value:
                result[key] = value
    return result


def read_data_token_lease(
    config_dir: Path,
    base_url: str,
    *,
    tenant: str = DEFAULT_TENANT,
    near_expiry_threshold: float = 0.0,
) -> str | None:
    """The freshest data-token lease for *tenant* whose digest matches
    *base_url*'s host, with remaining TTL strictly above
    ``ttl_seconds * near_expiry_threshold`` -- or ``None``. Never raises.

    ``near_expiry_threshold=0.0`` (the default) means "not yet expired" --
    the historical behaviour for a synchronous GET/POST caller that can
    retry on a 401 (``t2_prefix_scan.py`` / ``routing/_lib.py``). A
    fire-and-forget caller with no retry and no reader may pass ``0.20``, the same margin
    ``nexus.db.data_token._REFRESH_THRESHOLD`` uses, so a lease that is
    about to expire is never presented on a write nothing will retry.

    Filters on *tenant* explicitly (nexus-em75s.12), not only on digest
    self-consistency: the digest is recomputed from the SAME lease file's
    own ``tenant`` field, so a lease for a different tenant on the same
    host still reproduces a matching digest and would otherwise be
    indistinguishable from a same-tenant lease by that check alone.
    """
    import hashlib  # noqa: PLC0415 -- lazy: the session-start hook's 100 ms; only this reader needs them
    import urllib.parse  # noqa: PLC0415

    host = urllib.parse.urlsplit(base_url).netloc or base_url
    now = time.time()
    best_token: str | None = None
    best_expiry = 0.0
    try:
        candidates = sorted(config_dir.glob(f"{DATA_TOKEN_LEASE_PREFIX}*"))
    except OSError:
        candidates = []
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("format_version") != DATA_TOKEN_LEASE_FORMAT_VERSION:
                continue
            lease_tenant = str(data["tenant"])
            if lease_tenant != tenant:
                continue
            digest = hashlib.sha256(f"{host}\x00{lease_tenant}".encode("utf-8")).hexdigest()
            if data.get("base_url_digest") != digest:
                continue
            token = str(data["token"])
            expires_at = float(data["expires_at"])
            ttl_seconds = float(data["ttl_seconds"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if not token:
            continue
        remaining = expires_at - now
        if remaining <= ttl_seconds * near_expiry_threshold:
            continue  # expired, or too close to worth presenting
        if expires_at > best_expiry:
            best_token, best_expiry = token, expires_at
    return best_token


def resolve_base_url(config_dir: Path) -> tuple[str, bool]:
    """``(base_url, is_local_supervisor)``, or raise
    :class:`EndpointUnresolvable`.

    Mirrors ``nexus.db.service_endpoint.resolve_service_endpoint``'s
    precedence: the managed-cloud ``service_url`` leg -- env
    ``NX_SERVICE_URL`` first, then the persisted ``config.yml`` credential
    a user set with ``nx config set service_url`` (RDR-166 nexus-v3p0x) --
    is checked BEFORE the local-supervisor legs, exactly like the real
    HTTP storage clients (nexus-0zsmg: a cloud-mode box with no
    ``NX_SERVICE_URL`` exported -- an all-persisted-config install, the
    common shape after ``nx init`` -- must still resolve).

    The ``NX_SERVICE_HOST``/``NX_SERVICE_PORT`` env leg fills EITHER
    missing field -- HOST from a live local supervisor lease when only
    PORT is set, or PORT from that same lease when only HOST is set --
    before HOST defaults to ``127.0.0.1`` (nexus-aginu fix round,
    completing the earlier nexus-aginu fix's PORT-set/HOST-missing
    direction with the HOST-set/PORT-missing mirror: matching
    ``resolve_service_config``'s per-field lease merge, which triggers
    whenever ANY of host/port/token is missing, not only when PORT
    happens to be the one present. The pre-fix version here gated the
    whole env leg on ``NX_SERVICE_PORT`` being set, so a HOST-only env
    (PORT unset) fell straight through to the pure-lease leg below and
    silently returned the LEASE's host instead of the env-set one.
    PORT itself has no such default -- unlike HOST, an unresolvable
    PORT (env unset, no lease, or a lease that doesn't supply one) is a
    loud failure, mirroring ``resolve_service_config``'s identical
    behavior for port there).

    ``is_local_supervisor`` is True ONLY for the last leg -- the endpoint
    was resolved by literally reading the ``storage_service_addr.<uid>``
    lease file for BOTH host and port. It is False for every other leg,
    the host/port-env leg included even when part or all of that leg's
    host/port came from the same lease file: naming EITHER field via env
    is an explicit pin, and a caller that wants to know whether a lease
    record specifically backed the credential should check the lease
    directly rather than infer it from this flag.
    """
    url = os.environ.get("NX_SERVICE_URL", "").strip().rstrip("/")
    if not url:
        url = read_config_yml_credentials(config_dir).get("service_url", "").strip().rstrip("/")
    if url:
        return url, False

    host_str = os.environ.get("NX_SERVICE_HOST", "").strip()
    port_str = os.environ.get("NX_SERVICE_PORT", "").strip()
    if host_str or port_str:
        port: int | None = None
        if port_str:
            try:
                port = int(port_str)
            except ValueError as exc:
                raise EndpointUnresolvable(
                    f"NX_SERVICE_PORT is not an integer: {port_str!r}"
                ) from exc
        host: str | None = host_str or None
        if host is None or port is None:
            lease = read_storage_service_lease(config_dir)
            if lease is not None:
                if host is None:
                    host = lease["host"]
                if port is None:
                    port = lease["port"]
        host = host or "127.0.0.1"
        if port is None:
            lease_path = _lease_path_or_none(config_dir)
            raise EndpointUnresolvable(
                f"NX_SERVICE_HOST={host_str!r} is set but NX_SERVICE_PORT is "
                f"not, and no live local supervisor lease at {_shown(lease_path)} "
                f"supplies a port"
            )
        return f"http://{host}:{port}", False

    lease = read_storage_service_lease(config_dir)
    if lease is not None:
        return f"http://{lease['host']}:{lease['port']}", True

    lease_path = _lease_path_or_none(config_dir)
    raise EndpointUnresolvable(
        f"no service endpoint resolvable: no NX_SERVICE_URL, no persisted "
        f"config.yml service_url, no NX_SERVICE_PORT, and no live local "
        f"supervisor lease at {_shown(lease_path)}"
    )


def _credential(name: str, env_var: str, saved: dict[str, str]) -> str:
    """``nexus.config.get_credential(name).strip()``: the environment variable
    when it is set to anything (a blank value does NOT fall back to the file),
    else the saved ``config.yml`` value."""
    value = os.environ.get(env_var, "")
    if not value:
        value = saved.get(name, "")
    return value.strip()


def endpoint_usable(config_dir: Path, *, retry_budget_s: float | None = None, grace: bool = True) -> bool:
    """Whether the CLI would resolve a service endpoint: the plugin's restatement
    of ``nexus.db.service_endpoint.resolve_service_endpoint()`` returning and not
    raising (RDR-228 Phase 2 Step 4, nexus-sv2wx.17; residual nexus-9i4d4 item 9).

    :func:`resolve_base_url` answers a different question, the base URL the other
    hooks use with a credential policy of their own, and it returns a saved address
    with no token and a lease with no token, where the CLI raises. The CLI is the
    program that connects, so its answer is the one that counts. The four legs, in
    the CLI's order:

    1. ``NX_SERVICE_URL``, else a saved ``service_url``. Usable with a token
       (``NX_SERVICE_TOKEN`` else a saved ``service_token``), else with the token of
       a live local lease, else with a mint credential (``NX_MINT_TOKEN`` else a
       saved ``mint_token``). A URL that is only slashes is no URL.
    2. Otherwise ``NX_SERVICE_HOST`` / ``NX_SERVICE_PORT`` / ``NX_SERVICE_TOKEN``
       from the environment alone (the saved token is not read on this leg), with a
       live lease filling whichever of the three is missing. Usable with a port and
       a token. A port that is not an integer is not usable.
    3. Otherwise the live lease alone, which supplies all three.

    No network request, no wait, nothing written. *retry_budget_s* bounds the
    Windows sharing-violation retry of the lease read (the hook passes what is left of
    its read budget). With *grace* (the default, the hook's), a lease past its TTL is kept
    when its recorded owner process is alive (:func:`_stale_owner_alive`), so a laptop
    that slept is not told "service down". One difference from the CLI, which can probe a
    socket and this cannot: the CLI also requires the port to answer ``/health``, so an
    owner that is alive and wedged reads as usable here and not there
    (``tests/hooks/test_usable_service_agreement.py`` names it). The setup process passes
    ``grace=False``: it decides whether to START a service, it has the time the hook has not,
    and skipping the start on a wedged supervisor would end in a failed ``nx doctor``; a
    lease past its TTL is then not a service. Any error is "not usable".
    """
    try:
        saved = read_config_yml_credentials(config_dir, ("service_url", "service_token", "mint_token"))
        url = _credential("service_url", "NX_SERVICE_URL", saved).rstrip("/")
        if url:
            if _credential("service_token", "NX_SERVICE_TOKEN", saved):
                return True
            lease = read_storage_service_lease(config_dir, retry_budget_s=retry_budget_s, grace=grace)
            if lease is not None and lease["token"]:
                return True
            return bool(_credential("mint_token", "NX_MINT_TOKEN", saved))
        # The host never decides the answer (it defaults to 127.0.0.1), so a lease is
        # read only when the port or the token is missing.
        port_str = os.environ.get("NX_SERVICE_PORT", "").strip()
        port = int(port_str) if port_str else None
        token = os.environ.get("NX_SERVICE_TOKEN", "").strip() or None
        if port is None or token is None:
            lease = read_storage_service_lease(config_dir, retry_budget_s=retry_budget_s, grace=grace)
            if lease is not None:
                port = port if port is not None else lease["port"]
                token = token or lease["token"] or None
        return port is not None and bool(token)
    except Exception:  # noqa: BLE001 -- any failure to resolve is "not usable", as the CLI's raise is
        return False
