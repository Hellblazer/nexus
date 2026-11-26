#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings the plugin's hooks read: a project's tracked file and the user's own file.

Nothing here asks who the user is. The development process is the plugin's product and reaches every user; what a
project or a user can change is a setting, read from one of two files.

Project settings, ``.nexus/project.json`` in the checkout the payload names, found by walking up from the payload's cwd to the
first ``.git`` (a directory, or the file a linked worktree and a submodule have). A tracked file is written by whoever controls
the repository, so it can only TIGHTEN or add text; it can never loosen a rule or turn telemetry on. Keys:

``subagent_git``
    ``"strict"`` runs the full subagent git guard in this project; ``"seatbelt"`` (the default) leaves the short deny list.
``preflight``
    A command (a string) the SubagentStart worktree row tells a dispatched agent to run first.
``ci_wait``
    A command (a string) the Stop guard names for waiting on a push's CI; without it the wording is neutral.
``credential_helper``
    Text the credential guard's deny adds, naming this project's safe way to use the credential; without it the deny is neutral.
``brief_questions``
    A list of strings added to the implementer brief check.
``advice_block``
    A list of advice row ids that block (deny) in this project instead of warning.

``.nexus/dev-process.json`` (the Phase 2 file) and ``scripts/agent-worktree-preflight.sh`` (the Phase 1 marker) are still
accepted for two client releases, each read as ``{"subagent_git": "strict"}``. When ``.nexus/project.json`` exists it alone
decides. A project file that exists but cannot be read or parsed answers the strict reading, so a broken file never loosens.

User settings, ``<account home>/.config/nexus/conexus.json``. Keys: ``subagent_git`` (as above), ``telemetry`` (bool, default
false: routing events and the dispatch record are sent only when true), ``t1_nudge`` (bool, default true: the SubagentStop
reminder to write findings to T1) and ``advice_block`` (as above). On any error the defaults apply.

The account home comes from the OS account database (``pwd`` on POSIX, the known-folder API on Windows), never from ``HOME``,
``USERPROFILE`` or ``NEXUS_CONFIG_DIR``: no variable a repository or a project settings file can set may move it. The one test
seam is the in-process module attribute ``_ACCOUNT_HOME_OVERRIDE`` (see ``tests/hooks/audience_harness.py``); it is never read
from the environment.

The cwd is the payload's ``cwd``, then ``CLAUDE_PROJECT_DIR``; never the process cwd (uv launches the hook scripts with
``--directory`` at the plugin root). An unsubstituted ``${cwd}`` or a relative path counts as no path.

Stdlib only. Keep the copy in ``src/nexus/hooks/`` byte-identical.
"""
from __future__ import annotations

import json
import os
import time

#: In-process test seam. ``None`` reads the real account home; a path replaces it.
_ACCOUNT_HOME_OVERRIDE: "str | os.PathLike[str] | None" = None

#: Relative to the checkout root.
PROJECT_FILE_PARTS: tuple[str, ...] = (".nexus", "project.json")

#: Accepted for two client releases after this one ships: each reads as ``{"subagent_git": "strict"}``.
LEGACY_MARKERS: tuple[str, ...] = (
    ".nexus/dev-process.json",
    "scripts/agent-worktree-preflight.sh",
)

#: Relative to the account home.
USER_FILE_PARTS: tuple[str, ...] = (".config", "nexus", "conexus.json")

#: ssh host aliases a subagent's git write may name: one per line or comma-separated, ``#`` starts a comment. Read from the
#: account home, never from the environment: a project's own settings can set env.
REMOTE_HOSTS_PARTS: tuple[str, ...] = (".config", "nexus", "remote-test-hosts")

STRICT = "strict"
SEATBELT = "seatbelt"

#: What a project file that cannot be read gives: the strict reading.
_UNREADABLE_PROJECT: "dict[str, object]" = {"subagent_git": STRICT}


def _windows_profile_dir() -> "str | None":
    """The profile folder from the known-folder API (``FOLDERID_Profile``)."""
    import ctypes  # noqa: PLC0415 -- Windows only; a POSIX hook never pays for it
    from ctypes import wintypes  # noqa: PLC0415 -- same

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

    Raises on an unexpected failure; callers that must not raise go through the readers below.
    """
    if _ACCOUNT_HOME_OVERRIDE is not None:
        return os.fspath(_ACCOUNT_HOME_OVERRIDE)
    if os.name == "nt":
        return _windows_profile_dir()
    import pwd  # noqa: PLC0415 -- POSIX only; the module has to import on Windows

    try:
        return pwd.getpwuid(os.getuid()).pw_dir or None
    except KeyError:  # no passwd entry (a bare container): no home, no user settings
        return None


def _payload_cwd(payload: object) -> "str | None":
    """The checkout path the payload names, or None when it names none."""
    cwd = ""
    if isinstance(payload, dict):
        cwd = str(payload.get("cwd") or "")
    cwd = cwd or os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not cwd or "$" in cwd or "\x00" in cwd or not os.path.isabs(cwd):
        return None
    return cwd


def project_root(payload: object) -> "str | None":
    """The checkout root the payload's cwd sits in: the first directory up with a ``.git`` entry; None when none.

    The walk starts at the realpath of the cwd. A cwd inside ``.git/`` reaches the checkout root on the way up. A nested
    repository is its own project.
    """
    cwd = _payload_cwd(payload)
    if cwd is None:
        return None
    here = os.path.realpath(cwd)
    while True:
        if os.path.lexists(os.path.join(here, ".git")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def _clean_project(raw: object) -> "dict[str, object]":
    """The keys of a parsed project file that have the right type, and only the values that tighten or add text."""
    if not isinstance(raw, dict):
        return dict(_UNREADABLE_PROJECT)
    out: dict[str, object] = {}
    if raw.get("subagent_git") == STRICT:
        out["subagent_git"] = STRICT
    for key in ("preflight", "ci_wait", "credential_helper"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    for key in ("brief_questions", "advice_block"):
        value = raw.get(key)
        if isinstance(value, list):
            items = [v.strip() for v in value if isinstance(v, str) and v.strip()]
            if items:
                out[key] = items
    return out


def project_settings(payload: object) -> "dict[str, object]":
    """The payload's project settings, tightened; ``{}`` when the project has none; the strict reading on any error."""
    try:
        root = project_root(payload)
        if root is None:
            return {}
        path = os.path.join(root, *PROJECT_FILE_PARTS)
        if os.path.lexists(path):
            with open(path, encoding="utf-8") as handle:
                return _clean_project(json.load(handle))
        if any(os.path.isfile(os.path.join(root, *m.split("/"))) for m in LEGACY_MARKERS):
            return {"subagent_git": STRICT}
        return {}
    except BaseException:  # noqa: BLE001 -- a project file that cannot be read gives the strict reading
        return dict(_UNREADABLE_PROJECT)


def user_settings() -> "dict[str, object]":
    """The account's ``conexus.json`` with the defaults filled in; the defaults on any error."""
    defaults: dict[str, object] = {"subagent_git": SEATBELT, "telemetry": False, "t1_nudge": True, "advice_block": []}
    try:
        home = account_home()
        if not home:
            return defaults
        path = os.path.join(home, *USER_FILE_PARTS)
        if not os.path.isfile(path):
            return defaults
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            return defaults
        out = dict(defaults)
        if raw.get("subagent_git") in (STRICT, SEATBELT):
            out["subagent_git"] = raw["subagent_git"]
        for key in ("telemetry", "t1_nudge"):
            if isinstance(raw.get(key), bool):
                out[key] = raw[key]
        rows = raw.get("advice_block")
        if isinstance(rows, list):
            out["advice_block"] = [v.strip() for v in rows if isinstance(v, str) and v.strip()]
        return out
    except BaseException:  # noqa: BLE001 -- a user setting that cannot be read leaves the defaults
        return defaults


def guard_mode(payload: object) -> str:
    """``"strict"`` when the project or the user asks for the full subagent git guard, else ``"seatbelt"``. Strict on any error."""
    try:
        if project_settings(payload).get("subagent_git") == STRICT:
            return STRICT
        return STRICT if user_settings().get("subagent_git") == STRICT else SEATBELT
    except BaseException:  # noqa: BLE001 -- a tightening reader answers strict
        return STRICT


def telemetry_enabled() -> bool:
    """The user's ``telemetry`` setting (default false). False on any error: it sends data, so it loosens."""
    try:
        return user_settings().get("telemetry") is True
    except BaseException:  # noqa: BLE001
        return False


def t1_nudge_enabled() -> bool:
    """The user's ``t1_nudge`` setting (default true). True on any error: the nudge adds a reminder."""
    try:
        return user_settings().get("t1_nudge") is not False
    except BaseException:  # noqa: BLE001
        return True


def advice_blocked(payload: object) -> "frozenset[str]":
    """The advice row ids a project or the user switched to block. Empty on any error (a row only warns)."""
    try:
        rows = set(user_settings().get("advice_block") or [])
        rows.update(project_settings(payload).get("advice_block") or [])
        return frozenset(str(r) for r in rows)
    except BaseException:  # noqa: BLE001
        return frozenset()


def project_text(payload: object, key: str) -> "str | None":
    """A project setting that is text (``preflight``, ``ci_wait``, ``credential_helper``), or None."""
    try:
        value = project_settings(payload).get(key)
        return value if isinstance(value, str) and value else None
    except BaseException:  # noqa: BLE001
        return None


def project_brief_questions(payload: object) -> "list[str]":
    """The project's extra implementer brief questions, in order."""
    try:
        value = project_settings(payload).get("brief_questions")
        return [str(v) for v in value] if isinstance(value, list) else []
    except BaseException:  # noqa: BLE001
        return []


def _note_once(kind: str, payload: object, line: str) -> None:
    """Append *line* to ``<account home>/.config/nexus/logs/hook.log``, once per (kind, session).

    Writes only when that logs directory already exists, and at most once per session:
    an empty sentinel ``logs/audience-<kind>/<session_id>`` is created with ``O_EXCL``,
    and a second call finds it and writes nothing. Never raises.
    """
    try:
        home = account_home()
        if not home:
            return
        logs = os.path.join(home, ".config", "nexus", "logs")
        if not os.path.isdir(logs):
            return
        sid = str(payload.get("session_id") or "") if isinstance(payload, dict) else ""
        sid = "".join(c for c in sid if c.isalnum() or c in "-_")[:80] or "unknown"
        sentinels = os.path.join(logs, f"audience-{kind}")
        os.makedirs(sentinels, exist_ok=True)
        try:
            os.close(os.open(os.path.join(sentinels, sid), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            return
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        with open(os.path.join(logs, "hook.log"), "a", encoding="utf-8") as handle:
            handle.write(f"{stamp}+00:00 WARNING audience: {line}\n")
    except BaseException:  # noqa: BLE001 -- a note must never fail the hook
        pass


def note_hook_error(payload: object, what: str) -> None:
    """Say once per session that a hook's user-mode path raised (the hook then passes the call)."""
    _note_once("hook-error", payload, f"hook error, call passed: {what}"[:300])


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
