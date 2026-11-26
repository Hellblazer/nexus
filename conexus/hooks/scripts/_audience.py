#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings the plugin's hooks read: a project's tracked file and the user's own file (RDR-259).

Nothing here asks who the user is. The development process is the plugin's product and reaches every user; what a project or a
user can change is a setting, read from one of two files, by this one stdlib module (the hooks, the SessionStart notice and
``show`` all use it, so what a user inspects is what the hooks do).

Project settings, ``.nexus/project.json`` at a checkout root (the first directory at or above a path that holds a ``.git``
entry, a directory or the file a linked worktree and a submodule have). A tracked file is written by whoever controls the
repository, so the plugin keeps only what tightens or adds text from it and never executes any of it. User settings,
``<account home>/.config/nexus/conexus.json``. ``SETTINGS`` is the one table of keys; each entry names the type, the default,
which file may set it, how two values combine, the failure value and the limit (in bytes of UTF-8).

Which project copy is read: the *session root* is the checkout at or above ``CLAUDE_PROJECT_DIR`` (the primary, which stays put
for a worktree subagent), else the one at or above the payload's ``cwd``. The text and list keys come from the session root's
working-tree file. The strictness keys (``subagent_git``, ``advice_block``) combine, tighter wins, the session root's working
tree, its file at ``HEAD`` (two git processes at most, only when the caller asks and a project file may exist: see
``read_settings``) and the working tree of the checkout the payload's ``cwd`` sits in when that is a different one.

Failure: a safety key fails toward the protection (``subagent_git`` strict), the rest to the default. Every problem is a
``Problem`` in the result's list; nothing here raises.

The account home comes from the OS account database (``pwd`` on POSIX, the known-folder API on Windows), never from ``HOME``,
``USERPROFILE``, ``XDG_CONFIG_HOME`` or ``NEXUS_CONFIG_DIR``: a repository's own settings can set an environment variable, so no
variable may move it. The only environment variable read is ``CLAUDE_PROJECT_DIR``. The one test seam is the in-process module
attribute ``_ACCOUNT_HOME_OVERRIDE`` (see ``tests/hooks/audience_harness.py``); it is never read from the environment.

``python3 <plugin root>/hooks/scripts/_audience.py show [--cwd PATH] [--json]`` prints the report.

Stdlib only, importable on Windows. Keep the copy in ``src/nexus/hooks/`` byte-identical.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import time
import unicodedata
from typing import NamedTuple

#: In-process test seam. ``None`` reads the real account home; a path replaces it.
_ACCOUNT_HOME_OVERRIDE: "str | os.PathLike[str] | None" = None

#: Relative to the checkout root.
PROJECT_FILE_PARTS: tuple[str, ...] = (".nexus", "project.json")

#: Read as ``{"subagent_git": "strict"}`` in a checkout with no ``.nexus/project.json``. The window: Stages 1 to 3 of RDR-259
#: ship in the next normal client release after the v8.5.8 plugin-only fast lane (8.5.9, the patch cadence), and the markers
#: stay for two client releases after that, so ``LEGACY_ACCEPTED_UNTIL`` is 8.5.11. ``tests/hooks/test_settings_legacy_window.py``
#: fails once the plugin reaches it while these are still read; nexus-sby49.18 rechecks the value at the cut.
LEGACY_MARKERS: tuple[str, ...] = (
    ".nexus/dev-process.json",
    "scripts/agent-worktree-preflight.sh",
)
LEGACY_ACCEPTED_UNTIL = "8.5.11"

#: Files an earlier release read that nothing reads now; ``show`` lists one that still exists as having no effect.
RETIRED_USER_FILES: tuple[str, ...] = ("maintainer-profile",)

#: Relative to the account home.
USER_FILE_PARTS: tuple[str, ...] = (".config", "nexus", "conexus.json")

#: ssh host aliases a subagent's git write may name: one per line or comma-separated, ``#`` starts a comment. Read from the
#: account home, never from the environment: a project's own settings can set env.
REMOTE_HOSTS_PARTS: tuple[str, ...] = (".config", "nexus", "remote-test-hosts")

STRICT = "strict"
SEATBELT = "seatbelt"

#: A settings file over this is not read (a FIFO or a huge file must not stall a hook).
MAX_FILE_BYTES = 64 * 1024
#: Each git process of the ``HEAD`` read.
GIT_TIMEOUT_S = 2
#: The index byte search reads at most this much; a miss past it is "cannot tell".
INDEX_SEARCH_MAX_BYTES = 4 * 1024 * 1024
KNOWN_VERSION = 1
#: Questions the two files add in total (the SubagentStart byte budget).
MAX_BRIEF_QUESTIONS = 3
#: File-controlled text printed in a notice or a problem is cut to this many bytes.
NOTICE_TEXT_BYTES = 60

#: How a problem names the file it came from.
PROJECT_LABEL = ".nexus/project.json"
HEAD_LABEL = ".nexus/project.json at HEAD"
CWD_LABEL = ".nexus/project.json (cwd checkout)"
USER_LABEL = "conexus.json"
ENVIRONMENT_LABEL = "environment"

#: The advice rows a file may name in ``advice_block`` (``tests/test_audience_settings.py`` holds this list to the advice table).
ADVICE_ROW_IDS: tuple[str, ...] = (
    "pattern_kill", "branch_bulk_delete", "stash_with_worktrees", "bg_pipe_masks_exit", "whole_disk_scan",
    "shared_glob_delete", "hook_verb_by_hand", "running_script_edit",
)


class Setting(NamedTuple):
    """One entry of ``SETTINGS``. ``limit`` is in bytes of UTF-8; ``failure`` is the value a bad value falls to."""

    key: str
    kind: str  # int | enum | ids | lines | line | branch | bool
    default: object
    project: bool
    user: bool
    combine: str
    failure: object
    limit: "int | None" = None


SETTINGS: "dict[str, Setting]" = {
    s.key: s
    for s in (
        Setting("version", "int", KNOWN_VERSION, True, True, "per file", KNOWN_VERSION),
        Setting("subagent_git", "enum", SEATBELT, True, True, "strictest wins", STRICT),
        Setting("advice_block", "ids", [], True, True, "union", []),
        Setting("brief_questions", "lines", [], True, True, "append", [], 160),
        Setting("preflight", "line", None, True, False, "project only", None, 160),
        Setting("ci_wait", "line", None, True, False, "project only", None, 200),
        Setting("credential_helper", "line", None, True, False, "project only", None, 600),
        Setting("integration_branch", "branch", None, True, False, "project only", None, 100),
        Setting("telemetry", "bool", False, False, True, "user only", False),
        Setting("t1_nudge", "bool", True, False, True, "user only", True),
    )
}
_STRICTNESS_KEYS = ("subagent_git", "advice_block")
_BAD_CATEGORIES = ("Cc", "Zl", "Zp")  # control characters and line breaks make a text invalid
_UNSAFE_CATEGORIES = ("Cc", "Cf", "Zl", "Zp")  # these are also dropped from anything printed


class Problem(NamedTuple):
    """A settings problem: the file, the key (``""`` for the whole file), what was wrong, what is used instead.

    ``category`` is ``"settings"`` for a fault in a file and ``"environment"`` for one in the machine (no account home, a
    repository owned by another user, a remote branch that is absent): ``nx settings check`` leaves the second out of its exit
    code. ``forced_strict`` marks a problem that made ``subagent_git`` strict.
    """

    file: str
    key: str
    what: str
    used: str
    category: str = "settings"
    forced_strict: bool = False


# ---------------------------------------------------------------------------
# Text cleaning: file-controlled text is cleaned and bounded before it is printed
# ---------------------------------------------------------------------------


def clean_text(text: object, limit: int = NOTICE_TEXT_BYTES) -> str:
    """*text* with control characters, bidi marks and line breaks removed, cut to *limit* bytes of UTF-8 (``...`` marks a cut)."""
    kept = "".join(c for c in str(text) if unicodedata.category(c) not in _UNSAFE_CATEGORIES)
    if len(kept.encode("utf-8")) <= limit:
        return kept
    return kept.encode("utf-8")[: max(limit - 3, 0)].decode("utf-8", "ignore") + "..."


def quoted(text: object, limit: int = NOTICE_TEXT_BYTES) -> str:
    """*text* cleaned, cut and in double quotes."""
    return json.dumps(clean_text(text, limit), ensure_ascii=False)


def _value_text(value: object) -> str:
    """A value a file held, for a problem: a string quoted, anything else as its JSON, cleaned and cut either way."""
    if isinstance(value, str):
        return quoted(value)
    try:
        return clean_text(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return clean_text(repr(value))


def format_problem(p: Problem) -> str:
    """One clean line for a notice, the log or ``show``."""
    key = clean_text(p.key) + ": " if p.key else ""
    return f"{p.file}: {key}{p.what}; used: {p.used}"


def provenance_label(key: str, source: str, value: "str | None" = None) -> str:
    """The sentence that introduces a settings text, in the file's voice and never the plugin's (Decision 2).

    ``("preflight", "project", cmd)`` and the like carry the value; the two ``brief_questions`` labels are the line before
    the list. Any other pair raises ``KeyError``: a project text never carries the user label, nor the reverse.
    """
    limit = SETTINGS[key].limit if key in SETTINGS else None
    text = clean_text(value, limit or 600) if value is not None else ""
    if (key, source) == ("preflight", "project"):
        return f"This project's `.nexus/project.json` asks: run `{text}` first. If it exits non-zero, stop and report its output."
    if (key, source) == ("brief_questions", "project"):
        return "This project's `.nexus/project.json` adds these questions:"
    if (key, source) == ("brief_questions", "user"):
        return "Your `conexus.json` adds these questions:"
    if (key, source) == ("ci_wait", "project"):
        return (
            f"This project's `.nexus/project.json` names its CI wait command: `{text}`. "
            "Run it as a background command; its exit is the wake."
        )
    if (key, source) == ("credential_helper", "project"):
        return f"This project's `.nexus/project.json` adds (project text, not this guard's): {text}"
    raise KeyError((key, source))


# ---------------------------------------------------------------------------
# The account home and the checkout walk
# ---------------------------------------------------------------------------


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


def _usable_path(value: object) -> bool:
    """An absolute path that names itself: an unsubstituted ``${cwd}``, a relative path or a NUL byte counts as none."""
    return isinstance(value, str) and bool(value) and "$" not in value and "\x00" not in value and os.path.isabs(value)


def _payload_cwd(payload: object) -> "str | None":
    if isinstance(payload, dict):
        cwd = payload.get("cwd")
        if _usable_path(cwd):
            return str(cwd)
    return None


def _checkout_above(path: str) -> "str | None":
    """The first directory at or above *path*'s realpath that holds a ``.git`` entry; None when none does.

    A path inside ``.git/`` reaches the checkout root on the way up. A nested repository is its own project.
    """
    here = os.path.realpath(path)
    while True:
        if os.path.lexists(os.path.join(here, ".git")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def _env_project_dir() -> "str | None":
    value = os.environ.get("CLAUDE_PROJECT_DIR", "")
    return value if _usable_path(value) else None


def project_root(payload: object) -> "str | None":
    """The checkout the payload's ``cwd`` sits in (``CLAUDE_PROJECT_DIR``'s when the payload names no usable path); None when none."""
    cwd = _payload_cwd(payload)
    if cwd is None:
        cwd = _env_project_dir()
    return _checkout_above(cwd) if cwd else None


def session_root(payload: object) -> "str | None":
    """The checkout at or above ``CLAUDE_PROJECT_DIR`` when that is set and inside a repository, else the payload's checkout."""
    env = _env_project_dir()
    if env:
        root = _checkout_above(env)
        if root:
            return root
    cwd = _payload_cwd(payload)
    return _checkout_above(cwd) if cwd else None


def git_dir(root: str) -> "str | None":
    """The git directory of the checkout at *root*: ``.git`` itself, or the directory a linked worktree's or a submodule's
    ``.git`` file names. None when neither."""
    dotgit = os.path.join(root, ".git")
    try:
        if os.path.isdir(dotgit):
            return dotgit
        if os.path.isfile(dotgit):
            with open(dotgit, "rb") as handle:
                line = handle.read(4096).decode("utf-8", "replace").splitlines()[0:1]
            if line and line[0].startswith("gitdir:"):
                return os.path.normpath(os.path.join(root, line[0][len("gitdir:"):].strip()))
    except OSError:
        pass
    return None


# ---------------------------------------------------------------------------
# One file: size and type checks, then JSON
# ---------------------------------------------------------------------------


class _FileRead:
    """What reading one settings file gave: ``state`` is absent, ok or broken (or nohome for the user file)."""

    def __init__(self, label: str, path: str, role: str) -> None:
        self.label, self.path, self.role = label, path, role
        self.state = "absent"
        self.values: "dict[str, object]" = {}
        self.problems: "list[Problem]" = []
        self.raw: "bytes | None" = None
        self.forced = False  # a value here forced subagent_git strict
        self.marker = ""  # the legacy marker that stands in for a missing project file

    def as_dict(self) -> dict:
        return {"role": self.role, "label": self.label, "path": self.path, "state": self.state, "exists": self.state != "absent"}


def _file_problem(label: str, reason: str, used: str) -> Problem:
    return Problem(label, "", "could not be read: " + reason, used, "settings", True)


def _load_json(path: str, *, follow: bool) -> "tuple[str, object, str, bytes | None]":
    """``(state, parsed, reason, raw bytes)``. ``state`` is absent, ok or broken. The file's type and size are checked before
    it is opened, so a FIFO cannot block the read and a huge file is not read. A project file is not followed through a link."""
    try:
        st = os.stat(path) if follow else os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        if follow and os.path.islink(path):
            return "broken", None, "a link to a file that does not exist", None
        return "absent", None, "", None
    except OSError as exc:
        return "broken", None, "unreadable (" + type(exc).__name__ + ")", None
    if stat.S_ISLNK(st.st_mode):
        return "broken", None, "a symbolic link", None
    if not stat.S_ISREG(st.st_mode):
        return "broken", None, "not a regular file", None
    if st.st_size > MAX_FILE_BYTES:
        return "broken", None, "larger than 64 KiB", None
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        return "broken", None, "unreadable (" + type(exc).__name__ + ")", None
    if len(data) > MAX_FILE_BYTES:
        return "broken", None, "larger than 64 KiB", None
    return _parse_bytes(data)


def _parse_bytes(data: bytes) -> "tuple[str, object, str, bytes | None]":
    try:
        text = data.decode("utf-8-sig")  # a byte-order mark is accepted
    except UnicodeDecodeError:
        return "broken", None, "not valid UTF-8", data
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return "broken", None, "not valid JSON", data
    if not isinstance(parsed, dict):
        return "broken", None, "the top level is not an object", data
    return "ok", parsed, "", data


# ---------------------------------------------------------------------------
# Validation: a bad value is rejected (never cleaned into a value)
# ---------------------------------------------------------------------------


def _suggest(name: str, choices: "tuple[str, ...] | list[str]") -> str:
    import difflib  # noqa: PLC0415 -- only a file with a typo pays for it

    close = difflib.get_close_matches(str(name).lower().replace("-", "_"), list(choices), n=1, cutoff=0.6)
    return "; did you mean " + close[0] + "?" if close else ""


def _text_fault(text: str, limit: int) -> "str | None":
    if any(unicodedata.category(c) in _BAD_CATEGORIES for c in text):
        return "contains a control character or a line break"
    if len(text.encode("utf-8")) > limit:
        return "is longer than " + str(limit) + " bytes"
    return None


def _branch_fault(text: str) -> "str | None":
    if any(c.isspace() for c in text):
        return "contains whitespace"
    if ".." in text:
        return 'contains ".."'
    if text.startswith("-"):
        return 'starts with "-"'
    return None


def _validate(raw: dict, label: str, who: str, keys: "tuple[str, ...] | None" = None) -> "tuple[dict[str, object], list[Problem], bool]":
    """The valid values of a parsed file, the problems found, and whether a bad ``subagent_git`` forced strict.

    *who* is ``project`` or ``user``. With *keys*, only those keys are kept and only their problems reported.
    """
    values: "dict[str, object]" = {}
    problems: "list[Problem]" = []
    forced = False

    def add(key: str, what: str, used: str, forced_strict: bool = False) -> None:
        problems.append(Problem(label, key, what, used, "settings", forced_strict))

    for key, value in raw.items():
        if not isinstance(key, str) or key.startswith("$"):
            continue
        spec = SETTINGS.get(key)
        if keys is not None and key not in keys:
            continue
        if spec is None:
            add(key, "unknown key " + quoted(key) + _suggest(key, list(SETTINGS)), "ignored")
            continue
        if who == "project" and not spec.project:
            add(key, key + " is a user setting; a project cannot set it", "ignored")
            continue
        if who == "user" and not spec.user:
            add(key, key + " is a project setting; ignored in the user file", "ignored")
            continue
        if spec.kind == "int":
            if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
                values[key] = value
                if value > KNOWN_VERSION:
                    add(key, "written for a newer conexus (version " + str(value) + "); the keys this one knows are read", "known keys")
            else:
                add(key, "version must be an integer of 1 or more, got " + _value_text(value), "version 1")
        elif spec.kind == "enum":
            if value in (STRICT, SEATBELT) and isinstance(value, str):
                values[key] = value
            else:
                add(key, 'must be "strict" or "seatbelt", got ' + _value_text(value), "strict", True)
                values[key] = STRICT
                forced = True
        elif spec.kind == "ids":
            if not isinstance(value, list):
                add(key, "must be a list, got " + _value_text(value), "empty list")
                values[key] = []
                continue
            kept: "list[str]" = []
            for item in value:
                if not isinstance(item, str):
                    add(key, "list item of the wrong type: " + _value_text(item), "dropped")
                elif item not in ADVICE_ROW_IDS:
                    add(key, "unknown advice row " + quoted(item) + _suggest(item, ADVICE_ROW_IDS), "dropped")
                elif item not in kept:
                    kept.append(item)
            values[key] = kept
        elif spec.kind == "lines":
            if not isinstance(value, list):
                add(key, "must be a list, got " + _value_text(value), "empty list")
                values[key] = []
                continue
            lines: "list[str]" = []
            for item in value:
                if not isinstance(item, str):
                    add(key, "list item of the wrong type: " + _value_text(item), "dropped")
                    continue
                text = item.strip()
                fault = _text_fault(text, int(spec.limit or 0)) if text else "is empty"
                if fault:
                    add(key, "a question " + fault, "dropped")
                else:
                    lines.append(text)
            values[key] = lines
        elif spec.kind in ("line", "branch"):
            if not isinstance(value, str):
                add(key, "must be text, got " + _value_text(value), "absent")
                continue
            text = value.strip()
            if not text:
                continue  # empty text is unset
            fault = _text_fault(text, int(spec.limit or 0)) or (_branch_fault(text) if spec.kind == "branch" else None)
            if fault:
                add(key, "the text " + fault, "absent")
            else:
                values[key] = text
        elif spec.kind == "bool":
            if isinstance(value, bool):
                values[key] = value
            else:
                add(key, "must be true or false, got " + _value_text(value), "false" if spec.default is False else "true")
    return values, problems, forced


def _read_file(label: str, path: str, role: str, *, who: str, follow: bool, keys: "tuple[str, ...] | None" = None) -> _FileRead:
    fr = _FileRead(label, path, role)
    state, parsed, reason, raw = _load_json(path, follow=follow)
    fr.state, fr.raw = state, raw
    if state == "broken":
        used = "subagent_git strict; other keys absent" if who == "project" else "subagent_git strict; other keys at their defaults"
        fr.problems.append(_file_problem(label, reason, used))
        fr.values = {"subagent_git": STRICT}
        fr.forced = True
    elif state == "ok":
        fr.values, fr.problems, fr.forced = _validate(parsed, label, who, keys)  # type: ignore[arg-type]
    return fr


# ---------------------------------------------------------------------------
# The committed copy: one or two git processes, the index check, the case table
# ---------------------------------------------------------------------------


def index_names_project(gitdir: str) -> str:
    """Whether the git index names ``.nexus/project.json``: ``"yes"``, ``"no"`` or ``"unknown"``.

    A plain ``rm`` of the working copy leaves the index entry, so this says a project file may exist when the working one is
    gone. A stdlib byte search of index versions 2 and 3 (full paths), reading at most ``INDEX_SEARCH_MAX_BYTES``. Version 4
    (prefix-compressed), a split index (``sharedindex.*``), a sparse index (a ``.nexus/`` directory entry or an ``sdir``
    extension), a sparse checkout, a miss past the bound and any read error are "unknown", which the caller treats as "may name
    it": git is then asked, and only a failure of git itself makes it strict.
    """
    try:
        try:
            entries = os.listdir(gitdir)
        except OSError:
            entries = []
        if any(name.startswith("sharedindex.") for name in entries) or os.path.exists(os.path.join(gitdir, "info", "sparse-checkout")):
            return "unknown"
        try:
            handle = open(os.path.join(gitdir, "index"), "rb")
        except FileNotFoundError:
            return "no"
        with handle:
            data = handle.read(INDEX_SEARCH_MAX_BYTES + 1)
        if len(data) < 12 or data[:4] != b"DIRC" or int.from_bytes(data[4:8], "big") not in (2, 3):
            return "unknown"
        truncated = len(data) > INDEX_SEARCH_MAX_BYTES
        window = data[:INDEX_SEARCH_MAX_BYTES]
        if b".nexus/project.json" in window:
            return "yes"
        # A sparse index folds `.nexus/` into one directory entry (the path then ends at its NUL) and carries an `sdir`
        # extension; neither names the file, and the file may be under them.
        if truncated or b".nexus/\x00" in window or b"sdir" in window:
            return "unknown"
        return "no"
    except OSError:
        return "unknown"


def _head_problem(what: str, forced: bool = True, category: str = "settings") -> Problem:
    used = "subagent_git strict" if forced else "the working-tree value"
    return Problem(HEAD_LABEL, "", what, used, category, forced)


def _run_git(root: str, gitdir: str, *args: str) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell
        ["git", "--git-dir", gitdir, *args], cwd=root, stdin=subprocess.DEVNULL, capture_output=True,
        timeout=GIT_TIMEOUT_S, check=False,
    )


def _read_head(root: str, gitdir: str) -> _FileRead:
    """The session root's committed ``.nexus/project.json``, strictness keys only (the case table of Decision 1).

    Told apart by what ran and its exit code, never by git's message (locale dependent): ``ls-tree`` exit 128 (not a
    repository, an unborn HEAD) or exit 0 with no output means no committed copy; a blob that does not parse, or any other
    failure, means ``subagent_git`` strict, reported.
    """
    fr = _FileRead(HEAD_LABEL, PROJECT_LABEL + " at HEAD", "head")
    try:
        if hasattr(os, "getuid") and os.stat(root).st_uid != os.getuid():
            fr.problems.append(_head_problem("the repository is owned by another user; the committed copy was not read", False, "environment"))
            return fr
        listing = _run_git(root, gitdir, "ls-tree", "-z", "HEAD", "--", ".nexus/project.json")
        if listing.returncode == 128 or (listing.returncode == 0 and not listing.stdout.strip(b"\0 \n")):
            return fr
        if listing.returncode != 0:
            raise _GitFailure("git exited " + str(listing.returncode))
        meta = listing.stdout.split(b"\t", 1)[0].split()
        if len(meta) != 3 or meta[1] != b"blob":
            raise _GitFailure("the committed path is not a file")
        blob = _run_git(root, gitdir, "cat-file", "blob", meta[2].decode("ascii", "replace"))
        if blob.returncode != 0:
            raise _GitFailure("git cat-file exited " + str(blob.returncode))
    except subprocess.TimeoutExpired:
        fr.state, fr.forced, fr.values = "broken", True, {"subagent_git": STRICT}
        fr.problems.append(_head_problem("could not be read: git timed out after " + str(GIT_TIMEOUT_S) + " seconds"))
        return fr
    except _GitFailure as exc:
        fr.state, fr.forced, fr.values = "broken", True, {"subagent_git": STRICT}
        fr.problems.append(_head_problem("could not be read: " + str(exc)))
        return fr
    except OSError as exc:
        fr.state, fr.forced, fr.values = "broken", True, {"subagent_git": STRICT}
        fr.problems.append(_head_problem("could not be read: git could not be run (" + type(exc).__name__ + ")"))
        return fr
    data = blob.stdout
    if len(data) > MAX_FILE_BYTES:
        state, parsed, reason, raw = "broken", None, "larger than 64 KiB", data
    else:
        state, parsed, reason, raw = _parse_bytes(data)
    fr.state, fr.raw = state, raw
    if state == "ok":
        fr.values, fr.problems, fr.forced = _validate(parsed, HEAD_LABEL, "project", _STRICTNESS_KEYS)  # type: ignore[arg-type]
    else:
        fr.values, fr.forced = {"subagent_git": STRICT}, True
        fr.problems.append(_head_problem("could not be read: " + reason))
    return fr


class _GitFailure(Exception):
    pass


# ---------------------------------------------------------------------------
# Reading everything: the effective settings
# ---------------------------------------------------------------------------


class Settings:
    """The effective settings and where each came from.

    ``values`` has every key but ``version``; ``sources`` names where each came from (``default``, ``user``, ``project``,
    ``project (HEAD)``, ``project (cwd checkout)`` or ``legacy marker``); ``problems`` is the list; ``files`` the files read;
    ``project`` the session root's own validated keys (no ``version``) and ``user`` the user file's with defaults filled in;
    ``project_questions`` and ``user_questions`` the brief questions each file contributes after dropping duplicates and the cap.
    """

    def __init__(self) -> None:
        self.values: "dict[str, object]" = {k: _default_of(s) for k, s in SETTINGS.items() if k != "version"}
        self.sources: "dict[str, str]" = {k: "default" for k in self.values}
        self.problems: "list[Problem]" = []
        self.files: "list[_FileRead]" = []
        self.project: "dict[str, object]" = {}
        self.user: "dict[str, object]" = {k: _default_of(s) for k, s in SETTINGS.items() if s.user and k != "version"}
        self.project_questions: "list[str]" = []
        self.user_questions: "list[str]" = []
        self.session_root: "str | None" = None
        self.payload_root: "str | None" = None
        self.head: "_FileRead | None" = None
        self.head_read = False
        self.explicit_strict = False  # some file asked for strict outright, not only through a failure


def _default_of(spec: Setting) -> object:
    return list(spec.default) if isinstance(spec.default, list) else spec.default


def _failed(exc: BaseException) -> Settings:
    s = Settings()
    s.values["subagent_git"] = STRICT
    s.sources["subagent_git"] = "default"
    s.project = {"subagent_git": STRICT}
    s.problems.append(Problem(ENVIRONMENT_LABEL, "", "the settings could not be read (" + type(exc).__name__ + ")", "subagent_git strict", "settings", True))
    return s


def _read_user() -> _FileRead:
    fr = _FileRead(USER_LABEL, "", "user")
    try:
        home = account_home()
    except BaseException:  # noqa: BLE001 -- an unexpected failure of the lookup is "no account"
        home = None
    if not home:
        fr.state = "nohome"
        fr.problems.append(Problem(ENVIRONMENT_LABEL, "", "no account home, so no user settings", "defaults", "environment"))
        return fr
    path = os.path.join(home, *USER_FILE_PARTS)
    got = _read_file(USER_LABEL, path, "user", who="user", follow=True)  # a user file may be a link (dotfile managers)
    return got


def _read_project_copy(root: str, label: str, role: str, keys: "tuple[str, ...] | None") -> _FileRead:
    path = os.path.join(root, *PROJECT_FILE_PARTS)
    fr = _read_file(label, path, role, who="project", follow=False, keys=keys)
    if fr.state == "absent":
        for marker in LEGACY_MARKERS:
            if os.path.isfile(os.path.join(root, *marker.split("/"))):
                fr.marker = marker
                fr.values = {"subagent_git": STRICT}
                break
    return fr


def _combine(s: Settings, copies: "list[tuple[str, _FileRead]]", session: "_FileRead | None", user: _FileRead) -> None:
    """Fold the copies into ``s``: the strictest ``subagent_git``, the union of ``advice_block``, the text keys from the
    session root, the questions appended, the user-only keys from the user file."""
    explicit_seatbelt = ""
    for source, fr in copies:
        value = fr.values.get("subagent_git")
        if value == STRICT:
            if s.values["subagent_git"] != STRICT:
                s.values["subagent_git"], s.sources["subagent_git"] = STRICT, source
            if not fr.forced:
                s.explicit_strict = True
        elif value == SEATBELT and not explicit_seatbelt:
            explicit_seatbelt = source
    if s.values["subagent_git"] != STRICT and explicit_seatbelt:
        s.sources["subagent_git"] = explicit_seatbelt
    rows: "list[str]" = []
    contributors: "list[str]" = []
    for source, fr in copies:
        for row in fr.values.get("advice_block") or []:  # type: ignore[union-attr]
            if row not in rows:
                rows.append(str(row))
            if source not in contributors:
                contributors.append(source)
    s.values["advice_block"] = sorted(rows)
    if contributors:
        s.sources["advice_block"] = "+".join(contributors)
    if session is not None:
        for key in ("preflight", "ci_wait", "credential_helper", "integration_branch"):
            if key in session.values:
                s.values[key], s.sources[key] = session.values[key], "project"
    seen: "list[str]" = []
    for label, role, fr in ((PROJECT_LABEL, "project", session), (USER_LABEL, "user", user)):
        if fr is None:
            continue
        kept = s.project_questions if role == "project" else s.user_questions
        for q in fr.values.get("brief_questions") or []:  # type: ignore[union-attr]
            if q in seen:
                continue
            if len(seen) >= MAX_BRIEF_QUESTIONS:
                s.problems.append(Problem(
                    label, "brief_questions", "more than " + str(MAX_BRIEF_QUESTIONS) + " questions added in total; dropped " + quoted(q), "dropped"))
                continue
            seen.append(q)
            kept.append(str(q))
    s.values["brief_questions"] = list(s.project_questions) + list(s.user_questions)
    if s.values["brief_questions"]:
        s.sources["brief_questions"] = "+".join(n for n, q in (("project", s.project_questions), ("user", s.user_questions)) if q)
    for key in ("telemetry", "t1_nudge"):
        if key in user.values:
            s.values[key], s.sources[key] = user.values[key], "user"
    s.user = {k: _default_of(sp) for k, sp in SETTINGS.items() if sp.user and k != "version"}
    s.user.update({k: v for k, v in user.values.items() if k in s.user})


def read_settings(payload: object, head: bool = False, full: bool = False) -> Settings:
    """Read both files and return the effective settings. Never raises: a failure answers ``subagent_git`` strict.

    With *head*, the strictness keys' ``HEAD`` read runs when it can change the answer and a project file may exist (the
    working-tree file exists, or the git index names it): the result is not already strict and, for a checkout with no
    working-tree file, the index check allows it. A repository with no settings file therefore runs no git process. *full* reads
    ``HEAD`` whatever the working tree says (``show`` and ``check``). Call it with *head* only where the decision depends on it:
    a guard about to pass a command the strict scan would deny, an advice row that would only warn.
    """
    try:
        return _read_settings(payload, head or full, full)
    except BaseException as exc:  # noqa: BLE001 -- the reader never raises
        return _failed(exc)


def _read_settings(payload: object, head: bool, full: bool) -> Settings:
    s = Settings()
    user = _read_user()
    root = session_root(payload)
    pcheck = _checkout_above(_payload_cwd(payload)) if _payload_cwd(payload) else None
    s.session_root, s.payload_root = root, pcheck
    session = _read_project_copy(root, PROJECT_LABEL, "session", None) if root else None
    cwdc = _read_project_copy(pcheck, CWD_LABEL, "cwd", _STRICTNESS_KEYS) if pcheck and pcheck != root else None
    copies: "list[tuple[str, _FileRead]]" = []
    if session is not None:
        copies.append(("legacy marker" if session.marker else "project", session))
    if cwdc is not None:
        copies.append(("legacy marker" if cwdc.marker else "project (cwd checkout)", cwdc))
    already_strict = any(fr.values.get("subagent_git") == STRICT for _, fr in copies) or user.values.get("subagent_git") == STRICT
    headc = None
    if head and root and session is not None and (full or not already_strict):
        wt_exists = os.path.lexists(os.path.join(root, *PROJECT_FILE_PARTS))
        gd = git_dir(root)
        if gd is not None and (wt_exists or index_names_project(gd) != "no"):
            headc = _read_head(root, gd)
            s.head, s.head_read = headc, True
            copies.insert(1, ("project (HEAD)", headc))
    copies.append(("user", user))
    _combine(s, copies, session, user)
    s.files = [fr for _, fr in copies]
    if session is not None:
        s.project = {k: v for k, v in session.values.items() if k != "version"}
    for _, fr in copies:
        for p in fr.problems:
            if p not in s.problems:
                s.problems.append(p)
    return s


# ---------------------------------------------------------------------------
# The readers the hooks call (each answers its failure value on any error)
# ---------------------------------------------------------------------------


def project_settings(payload: object) -> "dict[str, object]":
    """The session root's project file as validated (no ``version``); ``{}`` when it has none; the strict reading on a failure."""
    try:
        return dict(read_settings(payload).project)
    except BaseException:  # noqa: BLE001
        return {"subagent_git": STRICT}


def _user_values() -> "dict[str, object]":
    """The user file's values over the user keys' defaults. No project file is read."""
    out = {k: _default_of(sp) for k, sp in SETTINGS.items() if sp.user and k != "version"}
    out.update({k: v for k, v in _read_user().values.items() if k in out})
    return out


def user_settings() -> "dict[str, object]":
    """The account's ``conexus.json`` with the defaults filled in; a broken file reads strict, the rest at its defaults."""
    try:
        return _user_values()
    except BaseException:  # noqa: BLE001
        return {"subagent_git": STRICT, "advice_block": [], "brief_questions": [], "telemetry": False, "t1_nudge": True}


def guard_mode(payload: object, head: bool = False) -> str:
    """``"strict"`` when the project or the user asks for the full subagent git guard, else ``"seatbelt"``. Strict on any error.

    Pass ``head=True`` only where the answer depends on it (see ``read_settings``): the default reads no ``HEAD``.
    """
    try:
        return STRICT if read_settings(payload, head=head).values["subagent_git"] == STRICT else SEATBELT
    except BaseException:  # noqa: BLE001 -- a tightening reader answers strict
        return STRICT


def forced_strict_clause(payload: object, head: bool = False) -> "str | None":
    """For a deny: ``the full guard applies because `<file>` could not be read: <reason>`` when the strict reading comes only
    from a file that could not be read; None when a file asked for strict or nothing is wrong."""
    try:
        s = read_settings(payload, head=head)
        if s.explicit_strict or s.values["subagent_git"] != STRICT:
            return None
        for p in s.problems:
            if p.forced_strict and not p.key:
                return "the full guard applies because `" + p.file + "` " + clean_text(p.what, 160)
        return None
    except BaseException:  # noqa: BLE001
        return None


def advice_blocked(payload: object, head: bool = False) -> "frozenset[str]":
    """The advice row ids a project or the user switched to block. Empty on any error (a row only warns)."""
    try:
        return frozenset(str(r) for r in read_settings(payload, head=head).values["advice_block"])  # type: ignore[union-attr]
    except BaseException:  # noqa: BLE001
        return frozenset()


def settings_problems(payload: object, head: bool = True) -> "list[Problem]":
    """Every problem the session's settings have, for the SessionStart notice (the list ``show`` and ``check`` print, minus the
    remote-branch check). Reads ``HEAD`` by default, since a committed copy that cannot be read is a problem to report; a
    repository with no settings file still starts no ``git`` process. An empty list on any error."""
    try:
        return list(read_settings(payload, head=head).problems)
    except BaseException:  # noqa: BLE001 -- a notice must never fail the hook
        return []


def telemetry_enabled() -> bool:
    """The user's ``telemetry`` setting (default false). False on any error: it sends data, so it loosens."""
    try:
        return _user_values()["telemetry"] is True
    except BaseException:  # noqa: BLE001
        return False


def t1_nudge_enabled() -> bool:
    """The user's ``t1_nudge`` setting (default true). True on any error: the nudge adds a reminder."""
    try:
        return _user_values()["t1_nudge"] is not False
    except BaseException:  # noqa: BLE001
        return True


def project_text(payload: object, key: str) -> "str | None":
    """A project setting that is text (``preflight``, ``ci_wait``, ``credential_helper``), or None."""
    try:
        value = read_settings(payload).values.get(key)
        return value if isinstance(value, str) and value else None
    except BaseException:  # noqa: BLE001
        return None


def integration_branch(payload: object) -> "str | None":
    """The project's ``integration_branch``, or None (the caller falls back to ``origin/HEAD``, ``main``, ``master``)."""
    return project_text(payload, "integration_branch")


def project_brief_questions(payload: object) -> "list[str]":
    """The project's extra implementer brief questions that survive the cap, in order."""
    try:
        return list(read_settings(payload).project_questions)
    except BaseException:  # noqa: BLE001
        return []


def user_brief_questions(payload: object = None) -> "list[str]":
    """The user's extra brief questions. With a *payload*, those that survive dropping the project's duplicates and the cap."""
    try:
        if payload is not None:
            return list(read_settings(payload).user_questions)
        questions = [str(q) for q in _read_user().values.get("brief_questions") or []]  # type: ignore[union-attr]
        return questions[:MAX_BRIEF_QUESTIONS]
    except BaseException:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Logging: each problem once per session
# ---------------------------------------------------------------------------


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


def note_problems(payload: object, problems: "list[Problem] | tuple[Problem, ...]") -> None:
    """Log each problem once per session (one sentinel per problem). Never raises."""
    try:
        for p in problems:
            digest = hashlib.sha1("\0".join((p.file, p.key, p.what)).encode("utf-8", "replace")).hexdigest()[:12]  # noqa: S324 -- a name, not a secret
            _note_once("settings-" + digest, payload, "settings problem: " + format_problem(p)[:300])
    except BaseException:  # noqa: BLE001
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


# ---------------------------------------------------------------------------
# show: the report (``nx settings show`` imports build_report and render_report)
# ---------------------------------------------------------------------------


def plugin_version() -> "str | None":
    """The conexus plugin's version from the ``plugin.json`` two directories above this file; None for the copy in ``nexus``."""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, os.pardir, ".claude-plugin", "plugin.json")
        with open(path, encoding="utf-8") as handle:
            return str(json.load(handle)["version"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _nx_version() -> "str | None":
    import shutil  # noqa: PLC0415 -- the report only

    exe = shutil.which("nx")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10, check=False).stdout.split()  # noqa: S603
    except (OSError, subprocess.SubprocessError):
        return None
    return out[-1] if out else None


def _branch_problem(root: str, branch: str) -> "Problem | None":
    """``origin`` has no branch named *branch*: one ``git rev-parse --verify`` of the remote ref (an environment problem)."""
    gd = git_dir(root)
    if gd is None:
        return None
    try:
        proc = _run_git(root, gd, "rev-parse", "--verify", "--quiet", "--end-of-options", "refs/remotes/origin/" + branch)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0:
        return None
    return Problem(
        PROJECT_LABEL, "integration_branch", "origin has no branch " + quoted(branch), "origin/HEAD, then main, then master", "environment")


def build_report(cwd: "str | None" = None, versions: "dict[str, str | None] | None" = None) -> dict:
    """The inspection report as a JSON-able dict (``show`` prints it; ``nx settings show`` imports it)."""
    cwd = os.path.abspath(cwd or os.getcwd())
    payload = {"cwd": cwd}
    s = read_settings(payload, head=True, full=True)
    problems = list(s.problems)
    branch = s.values.get("integration_branch")
    if s.session_root and isinstance(branch, str):
        extra = _branch_problem(s.session_root, branch)
        if extra is not None:
            problems.append(extra)
    root = s.session_root
    project_path = os.path.join(root, *PROJECT_FILE_PARTS) if root else None
    session = next((fr for fr in s.files if fr.role == "session"), None)
    user = next((fr for fr in s.files if fr.role == "user"), None)
    try:
        home = account_home()
    except BaseException:  # noqa: BLE001
        home = None
    user_path = os.path.join(home, *USER_FILE_PARTS) if home else None
    head_differs = None
    if s.head_read and s.head is not None and s.head.state != "broken" and not any(p.category == "environment" for p in s.head.problems):
        head_differs = (session.raw if session else None) != s.head.raw
    files = [
        {"role": "project", "path": project_path, "exists": bool(session and session.state != "absent")},
        {"role": "user", "path": user_path, "exists": bool(user and user.state not in ("absent", "nohome"))},
    ]
    rows = []
    for key in (k for k in SETTINGS if k != "version"):
        per_file = {}
        for fr in s.files:
            if key in fr.values and fr.state in ("ok", "broken"):
                per_file[fr.label] = fr.values[key]
        rows.append({"key": key, "value": s.values[key], "source": s.sources[key], "files": per_file})
    texts = []
    for key in ("preflight", "ci_wait", "credential_helper"):
        if isinstance(s.values.get(key), str) and s.values[key]:
            texts.append({"key": key, "source": "project", "text": provenance_label(key, "project", str(s.values[key]))})
    for src, qs in (("project", s.project_questions), ("user", s.user_questions)):
        if qs:
            texts.append({"key": "brief_questions", "source": src, "text": provenance_label("brief_questions", src), "items": list(qs)})
    retired = []
    if root and session is not None and session.state != "absent":
        for marker in LEGACY_MARKERS:
            mpath = os.path.join(root, *marker.split("/"))
            if os.path.isfile(mpath):
                retired.append({"path": mpath, "note": "no effect while .nexus/project.json exists; without it, read as strict until " + LEGACY_ACCEPTED_UNTIL})
    if home:
        for name in RETIRED_USER_FILES:
            rpath = os.path.join(home, ".config", "nexus", name)
            if os.path.exists(rpath):
                retired.append({"path": rpath, "note": "no effect; safe to delete"})
    v = dict(versions) if versions is not None else {"plugin": plugin_version(), "nx": _nx_version()}
    plugin, nx = v.get("plugin"), v.get("nx")
    warning = None
    if plugin and nx and plugin != nx:
        warning = (
            "the plugin (" + str(plugin) + ") and nx (" + str(nx) + ") differ: the nx-side hooks follow the installed CLI, "
            "so a setting named here may not yet be implemented by every hook"
        )
    return {
        "cwd": cwd, "session_root": root, "files": files, "head_differs": head_differs, "settings": rows, "texts": texts,
        "problems": [dict(p._asdict()) for p in problems], "retired": retired,
        "versions": {"plugin": plugin, "nx": nx, "warning": warning},
    }


def _fmt(value: object) -> str:
    if value is None:
        return "(not set)"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else "(none)"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render_report(report: dict) -> str:
    """The text a user reads."""
    out = ["conexus settings for " + report["cwd"]]
    v = report["versions"]
    out.append("versions: conexus plugin " + (v["plugin"] or "unknown") + ", nx " + (v["nx"] or "not found"))
    if v.get("warning"):
        out.append("  warning: " + v["warning"])
    out.append("files:")
    for f in report["files"]:
        where = f["path"] or "(no account home)"
        out.append("  " + f["role"] + ": " + where + (" (present)" if f["exists"] else " (not present)"))
    hd = report["head_differs"]
    out.append("  project file at HEAD: " + ("not read" if hd is None else ("differs from the working tree" if hd else "same as the working tree")))
    out.append("settings:")
    for row in report["settings"]:
        out.append("  " + row["key"] + " = " + _fmt(row["value"]) + "  (source: " + row["source"] + ")")
        eff = _fmt(row["value"])
        own = {k: _fmt(x) for k, x in row["files"].items()}
        if any(x != eff for x in own.values()):
            out.append("      in the files: " + "; ".join(k + ": " + x for k, x in own.items()))
    if report["texts"]:
        out.append("texts the hooks print:")
        for t in report["texts"]:
            out.append("  " + t["text"])
            for item in t.get("items", []):
                out.append("    - " + item)
    out.append("problems:")
    if report["problems"]:
        for p in report["problems"]:
            out.append("  " + format_problem(Problem(**p)))
    else:
        out.append("  no problems")
    if report["retired"]:
        out.append("retired files that still exist:")
        for r in report["retired"]:
            out.append("  " + r["path"] + ": " + r["note"])
    return "\n".join(out) + "\n"


def main(argv: "list[str] | None" = None, versions: "dict[str, str | None] | None" = None) -> int:
    """``show [--cwd PATH] [--json]``. Exit 0, or 2 on a bad argument."""
    args = list(sys.argv[1:] if argv is None else argv)
    usage = "usage: _audience.py show [--cwd PATH] [--json]\n"
    if not args or args[0] != "show":
        sys.stderr.write(usage)
        return 2
    cwd, as_json, rest = None, False, args[1:]
    while rest:
        arg = rest.pop(0)
        if arg == "--json":
            as_json = True
        elif arg == "--cwd" and rest:
            cwd = rest.pop(0)
        else:
            sys.stderr.write(usage)
            return 2
    report = build_report(cwd, versions)
    sys.stdout.write((json.dumps(report, indent=2, ensure_ascii=False) + "\n") if as_json else render_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
