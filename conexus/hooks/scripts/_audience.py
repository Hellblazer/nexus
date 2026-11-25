#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who is this hook running for: a user of the plugin, or its maintainer.

Two predicates answer from the hook payload's ``cwd`` and the OS account:

``nexus_repo(payload)``
    The checkout the payload names carries a tracked nexus marker file.
``maintainer(payload)``
    ``account_flag()`` OR ``nexus_repo(payload)``.
``account_flag()``
    ``<account home>/.config/nexus/maintainer-profile`` exists.

The account home comes from the OS account database (``pwd`` on POSIX, the
known-folder API on Windows), never from ``HOME``, ``USERPROFILE`` or
``NEXUS_CONFIG_DIR``: no variable a repository or a project settings file can
set may hide the flag. The one test seam is the in-process module attribute
``_ACCOUNT_HOME_OVERRIDE`` (see ``tests/hooks/audience_harness.py``); it is
never read from the environment.

Failure goes in the safe direction. A predicate that tightens (``maintainer``,
``nexus_repo``) answers True on any exception, so a broken helper restores the
full guard. A predicate that loosens (``account_flag``) answers False, so a
broken helper never widens a permission. A missing passwd entry is not an
error: the account has no home, so there is no flag.

The cwd is the payload's ``cwd``, then ``CLAUDE_PROJECT_DIR``; never the
process cwd (uv launches the hook scripts with ``--directory`` at the plugin
root). An unsubstituted ``${cwd}`` or a relative path counts as no path.

Stdlib only. Keep the copy in ``src/nexus/hooks/`` (Phase 2) byte-identical.
"""
from __future__ import annotations

import os
import time

#: In-process test seam. ``None`` reads the real account home; a path replaces it.
_ACCOUNT_HOME_OVERRIDE: "str | os.PathLike[str] | None" = None

#: Relative to the account home.
FLAG_PARTS: tuple[str, ...] = (".config", "nexus", "maintainer-profile")

#: Tracked files only the nexus checkout has. The Phase 1 marker is kept
#: accepted for at least two client releases after the Phase 2 one ships.
MARKERS: tuple[str, ...] = (
    "scripts/agent-worktree-preflight.sh",
    ".nexus/dev-process.json",
)

#: ssh host aliases a subagent's git write may name (nexus-yc7fs, D11): one per
#: line or comma-separated, ``#`` starts a comment. Read from the account home,
#: never from the environment: a project's own settings can set env.
REMOTE_HOSTS_PARTS: tuple[str, ...] = (".config", "nexus", "remote-test-hosts")

TRIPWIRE_LINE = "maintainer flag absent on this account; resolved by the repo marker"


def _windows_profile_dir() -> "str | None":
    """The profile folder from the known-folder API (``FOLDERID_Profile``)."""
    import ctypes
    from ctypes import wintypes

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8),
        ]

    folderid = _GUID(
        0x5E6C858F, 0x0E22, 0x4760,
        (ctypes.c_ubyte * 8)(0x9A, 0xFE, 0xEA, 0x33, 0x17, 0xB6, 0x71, 0x73),
    )
    path = ctypes.c_wchar_p()
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)  # type: ignore[attr-defined]
    ole32 = ctypes.WinDLL("ole32")  # type: ignore[attr-defined]
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(_GUID), wintypes.DWORD, wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    if shell32.SHGetKnownFolderPath(ctypes.byref(folderid), 0, None, ctypes.byref(path)) != 0:
        return None
    try:
        return path.value
    finally:
        ole32.CoTaskMemFree(path)


def account_home() -> "str | None":
    """The OS account's home directory, or None when the account has none.

    Raises on an unexpected failure; callers that must not raise go through
    :func:`account_flag`.
    """
    if _ACCOUNT_HOME_OVERRIDE is not None:
        return os.fspath(_ACCOUNT_HOME_OVERRIDE)
    if os.name == "nt":
        return _windows_profile_dir()
    import pwd

    try:
        return pwd.getpwuid(os.getuid()).pw_dir or None
    except KeyError:  # no passwd entry (a bare container): no home, no flag
        return None


def account_flag() -> bool:
    """True when the maintainer flag file exists. False on any error (it loosens)."""
    try:
        home = account_home()
        if not home:
            return False
        return os.path.isfile(os.path.join(home, *FLAG_PARTS))
    except BaseException:  # noqa: BLE001 -- a loosening predicate answers False
        return False


def _payload_cwd(payload: object) -> "str | None":
    """The checkout path the payload names, or None when it names none."""
    cwd = ""
    if isinstance(payload, dict):
        cwd = str(payload.get("cwd") or "")
    cwd = cwd or os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not cwd or "$" in cwd or "\x00" in cwd or not os.path.isabs(cwd):
        return None
    return cwd


def nexus_repo(payload: object) -> bool:
    """True when the payload's checkout carries a nexus marker; True on any error.

    The walk starts at the realpath of the cwd and stops at the first ``.git``
    entry (a directory, or the file a linked worktree and a submodule have). The
    markers are checked at that directory only, so a nested repo is its own
    project. A cwd inside ``.git/`` reaches the checkout root on the way up.
    """
    try:
        cwd = _payload_cwd(payload)
        if cwd is None:
            return False
        here = os.path.realpath(cwd)
        while True:
            if os.path.lexists(os.path.join(here, ".git")):
                return any(os.path.isfile(os.path.join(here, *m.split("/"))) for m in MARKERS)
            parent = os.path.dirname(here)
            if parent == here:
                return False
            here = parent
    except BaseException:  # noqa: BLE001 -- a tightening predicate answers True
        return True


def maintainer(payload: object) -> bool:
    """The account flag OR the repo marker; True on any error."""
    try:
        return account_flag() or nexus_repo(payload)
    except BaseException:  # noqa: BLE001 -- a tightening predicate answers True
        return True


def note_marker_only(payload: object) -> None:
    """Tripwire: say once per session that only the marker made this a maintainer.

    Writes ``TRIPWIRE_LINE`` to ``<account home>/.config/nexus/logs/hook.log``
    only when that directory already exists, and at most once per session: an
    empty sentinel ``logs/audience-tripwire/<session_id>`` is created with
    ``O_EXCL``, and a second hit finds it and writes nothing. Never raises.
    """
    try:
        if account_flag() or not nexus_repo(payload):
            return
        home = account_home()
        if not home:
            return
        logs = os.path.join(home, ".config", "nexus", "logs")
        if not os.path.isdir(logs):
            return
        sid = str(payload.get("session_id") or "") if isinstance(payload, dict) else ""
        sid = "".join(c for c in sid if c.isalnum() or c in "-_")[:80] or "unknown"
        sentinels = os.path.join(logs, "audience-tripwire")
        os.makedirs(sentinels, exist_ok=True)
        try:
            os.close(os.open(os.path.join(sentinels, sid), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            return
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        with open(os.path.join(logs, "hook.log"), "a", encoding="utf-8") as handle:
            handle.write(f"{stamp}+00:00 WARNING audience: {TRIPWIRE_LINE}\n")
    except BaseException:  # noqa: BLE001 -- a tripwire must never fail the hook
        pass


def remote_test_hosts() -> "frozenset[str]":
    """The ssh aliases named in ``<account home>/.config/nexus/remote-test-hosts``.

    Loosening, so any error, a missing home or a missing file answers the empty
    set (no host exempt). The file holds one alias per line or comma-separated
    aliases; ``#`` starts a comment.
    """
    try:
        home = account_home()
        if not home:
            return frozenset()
        with open(os.path.join(home, *REMOTE_HOSTS_PARTS), encoding="utf-8") as handle:
            text = handle.read()
        hosts: set[str] = set()
        for line in text.splitlines():
            for name in line.split("#", 1)[0].replace(",", " ").split():
                hosts.add(name)
        return frozenset(hosts)
    except BaseException:  # noqa: BLE001 -- a loosening reader answers "none"
        return frozenset()
