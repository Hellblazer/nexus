#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# PEP 723 metadata: `uv run` reads the interpreter floor from here, so no shell-
# special `>=` has to ride in hooks.json argv (nexus-efk2h). This script refuses
# Python 3.11 and older below.
# /// script
# requires-python = ">=3.12"
# ///
"""Subagent shared-git-state integrity check, phase 1 (nexus-i93tz): detect and
report, never restore.

Sam's decision of 2026-10-18: stop growing the spelling-level guard
(``subagent_git_write_requires_orchestrator``, frozen except for real
incidents) and check the OUTCOME instead. A subagent's Bash call can reach the
primary checkout's shared git state through more routes than any spelling scan
will ever list (a variable-held verb, ``scalar``, ``branch -M`` from a worktree,
a raw ``printf >> .git/config``). Whatever the route, the bytes end up in the
same few files, so this script looks at those files before and after the call.

One script, three registrations (hooks.json), dispatched on the payload's
``hook_event_name``:

* ``PreToolUse`` (Bash): take a snapshot, store it under
  ``<config>/git-integrity/<session_id>/<tool_use_id>.json``. Says nothing on
  the clean path. It NEVER denies; phase 1 only reports.
* ``PostToolUse`` / ``PostToolUseFailure`` (Bash): re-snapshot, diff, report.
  ``PostToolUse`` does not fire for a Bash call that exits non-zero, which is
  precisely a call that can write ``.git/config`` and then fail, so both
  events are registered.

Active only for a SUBAGENT call (payload carries ``agent_id``, the guard's own
gate) whose ``cwd`` or ``CLAUDE_PROJECT_DIR`` resolves to a repository. A linked
worktree resolves to the common git directory it shares with the primary, which
is what gets snapshotted.

WHAT IS SNAPSHOTTED (the common git directory ``C`` of the primary): ``C/config``
and ``C/config.worktree`` (parsed to keys), ``C/hooks/`` (name, mode, sha256 per
entry) plus the ``core.hooksPath`` target directory when one is configured,
``C/info/attributes`` and ``C/info/exclude``, ``C/HEAD``, and the value of the
branch the primary has checked out (loose ref, else ``packed-refs``). A linked
worktree's own HEAD, index, refs and ``config.worktree`` are out of scope.

REPORT CHANNEL. Claude Code feeds a PostToolUse hook's output to the model that
made the call, here the subagent: ``decision: "block"`` with ``reason`` (the
tool already ran; the reason lands next to the tool result) and
``hookSpecificOutput.additionalContext`` (``PostToolUseFailure`` has no
decision, so it carries the report as ``additionalContext``). ``systemMessage``
is shown to the user. Nothing documented carries a subagent's hook output to
the PARENT conversation, which sees only the subagent's final message, so the
report is also written to the routing log (``log_routing_event``: the engine's
``routing_events`` table, else the drop meter) and the subagent's context states
that the report belongs in its hand-back. See the bead for the verdict.

FAIL CLOSED ON THE REPORT SIDE (the vacuous-gate doctrine). A snapshot that
cannot be taken completely (a file that exists and cannot be read) is stored
with the error and reported by Post. A Post with no snapshot to compare against
is reported as UNVERIFIED, never skipped. The one silent no-op is "this call
has nothing to protect" (no agent_id, or no repository in sight).

CONCURRENCY. The orchestrator works in the primary while a subagent runs, so
HEAD and ref movement is reported as "changed during a subagent call (may be
the orchestrator)", not as an accusation. The same footer applies to every line:
the check sees bytes, not authors.

Standard library only: hook scripts run under ``uv tool run python`` with no
``nexus`` import, and this runs on every Bash call. No process is spawned (the
repository is found by walking up for ``.git``), so the added cost is one
interpreter start plus a few dozen small file reads. Measured (macOS, this
repository with 29 branches and 19 hook entries, hooks.json's own ``uv tool
run`` launch, median of 20, two runs, bare interpreter start 20 ms): a
main-conversation call exits at the `agent_id` gate in 22-27 ms; a subagent Pre
costs 32-39 ms and a subagent Post 32-37 ms, of which the snapshot itself is
about 1 ms. For scale the credential guard measures 70-98 ms in the same
harness, because the shared routing
preamble's interpreter re-exec (about 50 ms) is left out here on purpose: it
only serves scripts that import `nexus`, and this one imports the standard
library alone.
"""
from __future__ import annotations

import os
import sys

# Cheap gate BEFORE any other import: the rule targets subagents, and a hook
# fires on every Bash call. A payload with no `agent_id` key (the main
# conversation) exits here at bare-interpreter cost. The key check is a
# superset test; `body` still requires a non-empty value.
_RAW = sys.stdin.read() if __name__ == "__main__" else ""
if __name__ == "__main__" and '"agent_id"' not in _RAW:
    sys.exit(0)

import hashlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import stat  # noqa: E402
import time  # noqa: E402
from typing import Any  # noqa: E402

# No `_interpreter.reexec_if_needed()`: that resolution exists for scripts that
# import `nexus` from the install generation's interpreter, and this one imports
# only the standard library. hooks.json already passes `--python >=3.12`, and the
# re-exec probe measured about 50 ms per call (see the cost note in the docstring).
sys.path.insert(0, os.path.dirname(__file__))
import _lib  # noqa: E402

RULE_NAME = "subagent_git_integrity_check"
BEAD = "nexus-i93tz"

#: Snapshot caps. A pathological repository must not make a hook slow or its
#: state file large; a capped snapshot says so, and the report says so.
MAX_CONFIG_BYTES = 512 * 1024
MAX_CONFIG_KEYS = 1500
MAX_HOOK_ENTRIES = 300
MAX_HASH_BYTES = 1024 * 1024
MAX_TEXT_LINES = 200
MAX_TEXT_BYTES = 64 * 1024
MAX_SNAPSHOT_BYTES = 512 * 1024
MAX_REPORT_LINES = 60
STALE_SNAPSHOT_SECONDS = 6 * 3600
STALE_SESSION_SECONDS = 2 * 86400
MAX_PRUNE_DIRS = 50

PENDING = "pending"  # marker value, never a real sha

#: Config sections whose values are shown in a report. Everything else is
#: reported by key name only (``credential.*``, ``http.*``, ``url.*`` and the
#: like can hold tokens). A remote's url is shown with any userinfo removed.
_SHOW_SECTIONS = frozenset({
    "core", "user", "init", "branch", "extensions", "include", "includeif",
    "filter", "alias", "merge", "diff", "pager", "gc", "hook", "safe",
    "worktree", "maintenance", "scalar", "feature", "fetch", "pull", "push",
    "sequence", "receive", "uploadpack", "submodule", "commit", "tag",
})
_SHOW_REMOTE_KEYS = frozenset({"url", "pushurl", "fetch", "push", "tagopt", "vcs"})

_NAME_OK = re.compile(r"[^A-Za-z0-9_.-]")
_USERINFO = re.compile(r"(://)[^/@\s]*@")
_TOKENISH = re.compile(r"[A-Za-z0-9_\-]{32,}")
_SECTION = re.compile(
    r'^\s*\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]\s*(.*)$'
)
_KEYLINE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9-]*)\s*(?:=\s*(.*))?$")


# ---------------------------------------------------------------------------
# Locating the repository
# ---------------------------------------------------------------------------


def _read_small(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(4096)
    except OSError:
        return None


def _common_dir_for(start: str) -> tuple[str, str] | None:
    """``(common_git_dir, primary_toplevel)`` for the repository containing
    *start*, or ``None``. Walks up looking for ``.git``; a ``.git`` FILE is a
    linked worktree (or submodule) pointer, followed to its ``commondir``.
    Spawns nothing."""
    if not start:
        return None
    here = os.path.abspath(start)
    while True:
        dotgit = os.path.join(here, ".git")
        try:
            st = os.lstat(dotgit)
        except OSError:
            st = None
        if st is not None:
            if stat.S_ISDIR(st.st_mode):
                return _real(dotgit), _top_of(_real(dotgit))
            if stat.S_ISREG(st.st_mode):
                text = _read_small(dotgit) or ""
                m = re.match(r"gitdir:\s*(.+?)\s*$", text.splitlines()[0] if text else "")
                if not m:
                    return None
                gitdir = m.group(1)
                if not os.path.isabs(gitdir):
                    gitdir = os.path.join(here, gitdir)
                gitdir = _real(gitdir)
                common = _read_small(os.path.join(gitdir, "commondir"))
                if common and common.strip():
                    c = common.strip().splitlines()[0]
                    common_dir = _real(c if os.path.isabs(c) else os.path.join(gitdir, c))
                else:
                    common_dir = gitdir
                return common_dir, _top_of(common_dir)
            return None
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def _real(path: str) -> str:
    return os.path.realpath(path)


def _top_of(common: str) -> str:
    return os.path.dirname(common) if os.path.basename(common) == ".git" else common


def _roots(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """Distinct ``(common_dir, top)`` for the call's cwd and the project dir.
    The project dir covers a command that ``cd``s out of a worktree into the
    primary."""
    seen: dict[str, str] = {}
    for start in (str(payload.get("cwd") or ""), os.environ.get("CLAUDE_PROJECT_DIR", "")):
        found = _common_dir_for(start)
        if found and found[0] not in seen:
            seen[found[0]] = found[1]
    return sorted(seen.items())


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def _h(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path: str, cap: int) -> tuple[bytes | None, int]:
    """``(data_or_None_if_absent, full_size)``. Reads at most *cap* bytes.
    Raises ``OSError`` for anything but absence: unreadable is an error, not
    an empty file."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(cap + 1)
            size = os.fstat(fh.fileno()).st_size
    except (FileNotFoundError, NotADirectoryError):
        return None, 0
    return data, size


def _sha_file(path: str, cap: int = MAX_HASH_BYTES) -> tuple[str, int]:
    """Streamed sha256 of up to *cap* bytes plus the file's size."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        digest.update(fh.read(cap))
        return digest.hexdigest(), os.fstat(fh.fileno()).st_size


def _scrub(value: str) -> str:
    value = _USERINFO.sub(r"\1***@", value)
    if not value.startswith(("/", "~", "./", "../")):  # a filesystem path is not a token
        value = _TOKENISH.sub("<redacted>", value)
    return value if len(value) <= 120 else value[:117] + "..."


def _shown(key: str) -> bool:
    section = key.split(".", 1)[0]
    if section == "remote":
        return key.rsplit(".", 1)[-1] in _SHOW_REMOTE_KEYS
    return section in _SHOW_SECTIONS


def _strip_comment(value: str) -> str:
    out: list[str] = []
    quoted = False
    prev = ""
    for ch in value:
        if ch == '"' and prev != "\\":
            quoted = not quoted
        elif ch in "#;" and not quoted:
            break
        out.append(ch)
        prev = ch
    return "".join(out).strip()


def _parse_config(text: str) -> tuple[dict[str, dict[str, Any]], bool]:
    """``({canonical_key: {"d": display, "v": [[shown|None, sha16], ...]}}, capped)``.

    Not a full git-config parser: section headers (including the old
    ``[section.sub]`` form), keys, comments and trailing-backslash joins.
    A misparse can only blur WHICH key changed, never hide that the file did:
    the whole-file hash is compared first."""
    keys: dict[str, dict[str, Any]] = {}
    section = ""
    sub: str | None = None
    capped = False
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        stripped = line.strip()
        if not stripped or stripped[0] in "#;":
            continue
        m = _SECTION.match(line)
        rest = ""
        if m:
            name, sub, rest = m.group(1), m.group(2), m.group(3)
            if sub is None and "." in name:
                name, _, sub = name.partition(".")
            section = name.lower()
            if not rest.strip() or rest.strip()[0] in "#;":
                continue
            line = rest
        km = _KEYLINE.match(line)
        if not km or not section:
            continue
        raw = km.group(2) if km.group(2) is not None else "true"
        while raw.endswith("\\") and i < len(lines):
            raw = raw[:-1] + lines[i]
            i += 1
        value = _strip_comment(raw)
        name = km.group(1)
        canon = f"{section}.{sub}.{name.lower()}" if sub is not None else f"{section}.{name.lower()}"
        display = f"{section}.{sub}.{name}" if sub is not None else f"{section}.{name}"
        if canon not in keys:
            if len(keys) >= MAX_CONFIG_KEYS:
                capped = True
                continue
            keys[canon] = {"d": display, "v": []}
        keys[canon]["v"].append([_scrub(value) if _shown(canon) else None, _h(value.encode())[:16]])
        if canon == "core.hookspath":
            # The resolver needs the real path, not the display form (long
            # values are truncated there).
            keys[canon].setdefault("r", []).append(value[:1024])
    return keys, capped


def _config_entry(path: str, label: str, errors: list[str]) -> dict[str, Any]:
    try:
        data, size = _read_bytes(path, MAX_CONFIG_BYTES)
    except OSError as exc:
        errors.append(f"could not read {label}: {exc.__class__.__name__} ({exc.strerror or exc})")
        return {"unreadable": True}
    if data is None:
        return {"present": False}
    if len(data) > MAX_CONFIG_BYTES:
        return {"present": True, "sha": _h(data), "size": size, "keys": None, "capped": True}
    keys, capped = _parse_config(data.decode("utf-8", "replace"))
    return {"present": True, "sha": _h(data), "size": size, "keys": keys, "capped": capped}


def _text_entry(path: str, label: str, errors: list[str]) -> dict[str, Any]:
    try:
        data, size = _read_bytes(path, MAX_TEXT_BYTES)
    except OSError as exc:
        errors.append(f"could not read {label}: {exc.__class__.__name__} ({exc.strerror or exc})")
        return {"unreadable": True}
    if data is None:
        return {"present": False}
    entry: dict[str, Any] = {"present": True, "sha": _h(data), "size": size, "lines": None}
    if len(data) <= MAX_TEXT_BYTES:
        entry["lines"] = [ln[:200] for ln in data.decode("utf-8", "replace").splitlines()[:MAX_TEXT_LINES]]
    return entry


def _dir_entry(path: str, label: str, errors: list[str]) -> dict[str, Any]:
    """``{"present": bool, "entries": {name: [kind, mode, sha, size]}, "capped": bool}``."""
    try:
        scan = os.scandir(path)
    except (FileNotFoundError, NotADirectoryError):
        return {"present": False}
    except OSError as exc:
        errors.append(f"could not read {label}: {exc.__class__.__name__} ({exc.strerror or exc})")
        return {"unreadable": True}
    entries: dict[str, list[Any]] = {}
    capped = False
    with scan:
        for de in sorted(scan, key=lambda e: e.name):
            if len(entries) >= MAX_HOOK_ENTRIES:
                capped = True
                break
            try:
                st = de.stat(follow_symlinks=False)
                mode = format(stat.S_IMODE(st.st_mode), "04o")
                if stat.S_ISLNK(st.st_mode):
                    entries[de.name] = ["link", mode, os.readlink(de.path), 0]
                elif stat.S_ISREG(st.st_mode):
                    sha, size = _sha_file(de.path)
                    entries[de.name] = ["file", mode, sha, size]
                else:
                    entries[de.name] = ["other", mode, "", 0]
            except OSError as exc:
                errors.append(f"could not read {label}/{de.name}: {exc.__class__.__name__}")
                entries[de.name] = ["unreadable", "", "", 0]
    return {"present": True, "entries": entries, "capped": capped}


def _ref_value(common: str, ref: str, errors: list[str]) -> dict[str, Any]:
    """The sha *ref* resolves to: the loose file, else ``packed-refs``."""
    if ".." in ref.split("/") or not ref.startswith("refs/"):
        return {"ref": ref, "sha": None, "where": "invalid"}
    try:
        data, _ = _read_bytes(os.path.join(common, *ref.split("/")), 4096)
    except OSError as exc:
        errors.append(f"could not read {ref}: {exc.__class__.__name__}")
        return {"ref": ref, "sha": None, "where": "unreadable"}
    if data is not None and data.strip():
        return {"ref": ref, "sha": data.decode("utf-8", "replace").strip().splitlines()[0], "where": "loose"}
    try:
        packed, _ = _read_bytes(os.path.join(common, "packed-refs"), 64 * 1024 * 1024)
    except OSError as exc:
        errors.append(f"could not read packed-refs: {exc.__class__.__name__}")
        return {"ref": ref, "sha": None, "where": "unreadable"}
    if packed is not None:
        needle = f" {ref}"
        for raw in packed.decode("utf-8", "replace").splitlines():
            if raw.endswith(needle) and not raw.startswith(("#", "^")):
                return {"ref": ref, "sha": raw.split(" ", 1)[0], "where": "packed"}
    if os.path.isdir(os.path.join(common, "reftable")):
        errors.append(f"{ref} cannot be read: this repository uses the reftable ref backend")
        return {"ref": ref, "sha": None, "where": "reftable"}
    return {"ref": ref, "sha": None, "where": "missing"}


def _hooks_path_dirs(common: str, top: str, cfg: dict[str, Any], wcfg: dict[str, Any]) -> list[str]:
    """The ``core.hooksPath`` target (last value wins, ``config.worktree`` over
    ``config``), resolved against the primary's top level."""
    value = None
    for entry in (cfg, wcfg):
        keys = entry.get("keys") if isinstance(entry, dict) else None
        if keys and "core.hookspath" in keys:
            value = keys["core.hookspath"]["r"][-1]
    if not value:
        return []
    value = os.path.expanduser(value)
    path = value if os.path.isabs(value) else os.path.join(top, value)
    return [os.path.normpath(path)]


def snapshot(common: str, top: str) -> dict[str, Any]:
    errors: list[str] = []
    snap: dict[str, Any] = {"common": common, "top": top}
    snap["config"] = _config_entry(os.path.join(common, "config"), "config", errors)
    snap["config.worktree"] = _config_entry(os.path.join(common, "config.worktree"), "config.worktree", errors)
    snap["hooks"] = _dir_entry(os.path.join(common, "hooks"), "hooks", errors)
    extra: dict[str, Any] = {}
    for d in _hooks_path_dirs(common, top, snap["config"], snap["config.worktree"]):
        extra[d] = _dir_entry(d, f"core.hooksPath dir {d}", errors)
    snap["hooksPath"] = extra
    snap["info/attributes"] = _text_entry(os.path.join(common, "info", "attributes"), "info/attributes", errors)
    snap["info/exclude"] = _text_entry(os.path.join(common, "info", "exclude"), "info/exclude", errors)
    head = _text_entry(os.path.join(common, "HEAD"), "HEAD", errors)
    raw = (head.get("lines") or [""])[0] if head.get("present") else None
    snap["HEAD"] = {"raw": raw, "present": bool(head.get("present")), "unreadable": bool(head.get("unreadable"))}
    branch_ref = None
    if raw and raw.startswith("ref:"):
        branch_ref = raw[4:].strip()
    snap["branch"] = _ref_value(common, branch_ref, errors) if branch_ref else None
    snap["errors"] = errors
    return snap


def _shrink(snap: dict[str, Any]) -> dict[str, Any]:
    """Drop the bulky per-key and per-line detail; the whole-file hashes stay,
    so a change is still detected, only less precisely named."""
    for root in snap["roots"]:
        for name in ("config", "config.worktree"):
            if isinstance(root.get(name), dict):
                root[name]["keys"] = None
        for name in ("info/attributes", "info/exclude"):
            if isinstance(root.get(name), dict):
                root[name]["lines"] = None
        root["errors"] = list(root.get("errors", []))[:20]
    snap["shrunk"] = True
    return snap


# ---------------------------------------------------------------------------
# State files
# ---------------------------------------------------------------------------


def _state_dir(session_id: str) -> str:
    base = str(_lib._default_config_dir())
    return os.path.join(base, "git-integrity", _NAME_OK.sub("_", session_id)[:96] or "nosession")


def _state_path(payload: dict[str, Any]) -> str | None:
    tool_use_id = str(payload.get("tool_use_id") or "")
    if not tool_use_id:
        return None
    session = str(payload.get("session_id") or "")
    return os.path.join(_state_dir(session), _NAME_OK.sub("_", tool_use_id)[:128] + ".json")


def _prune(session_dir: str, now: float) -> None:
    """Remove snapshots a Post never consumed (call interrupted, or denied by
    another hook), and whole session directories untouched for two days."""
    try:
        for name in os.listdir(session_dir):
            p = os.path.join(session_dir, name)
            try:
                if now - os.stat(p).st_mtime > STALE_SNAPSHOT_SECONDS:
                    os.unlink(p)
            except OSError:
                pass
        parent = os.path.dirname(session_dir)
        for n, name in enumerate(os.listdir(parent)):
            if n >= MAX_PRUNE_DIRS:
                break
            p = os.path.join(parent, name)
            if p != session_dir and now - os.stat(p).st_mtime > STALE_SESSION_SECONDS:
                for leaf in os.listdir(p):
                    os.unlink(os.path.join(p, leaf))
                os.rmdir(p)
    except OSError:
        pass


def _store(path: str, snap: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    blob = json.dumps(snap, separators=(",", ":"))
    if len(blob) > MAX_SNAPSHOT_BYTES:
        blob = json.dumps(_shrink(snap), separators=(",", ":"))
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, blob.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

_MAY_BE_ORCHESTRATOR = "changed during a subagent call (may be the orchestrator)"


def _short(sha: str | None) -> str:
    return (sha or "none")[:12]


def _diff_config(label: str, a: dict[str, Any], b: dict[str, Any], out: list[str]) -> None:
    if a.get("unreadable") or b.get("unreadable"):
        return  # already reported as a snapshot error
    if a.get("present") != b.get("present"):
        out.append(f"{label}: {'created' if b.get('present') else 'deleted'}")
        return
    if not a.get("present") or a.get("sha") == b.get("sha"):
        return
    ak, bk = a.get("keys"), b.get("keys")
    if ak is None or bk is None:
        out.append(f"{label}: file changed (size {a.get('size')} -> {b.get('size')} bytes; too large to name keys)")
        return
    named = 0
    for canon in sorted(set(ak) | set(bk)):
        av, bv = ak.get(canon), bk.get(canon)
        disp = (bv or av)["d"]
        if av is None:
            out.append(f"{label}: key {disp} ADDED{_values(bv)}")
        elif bv is None:
            out.append(f"{label}: key {disp} REMOVED{_values(av)}")
        elif [x[1] for x in av["v"]] != [x[1] for x in bv["v"]]:
            out.append(f"{label}: key {disp} CHANGED (before{_values(av)}; after{_values(bv)})")
        else:
            continue
        named += 1
    if named == 0:
        out.append(f"{label}: file bytes changed with no key-level difference (comment, whitespace or an unparsed form)")
    if a.get("capped") or b.get("capped"):
        out.append(f"{label}: key list capped at {MAX_CONFIG_KEYS}; later keys were not compared by name")


def _values(entry: dict[str, Any] | None) -> str:
    if not entry:
        return ""
    vals = [v[0] for v in entry["v"]]
    if all(v is None for v in vals):
        return " (value not shown)"
    return " (" + ", ".join(repr(v) if v is not None else "<not shown>" for v in vals) + ")"


def _diff_dir(label: str, a: dict[str, Any], b: dict[str, Any], out: list[str]) -> None:
    if a.get("unreadable") or b.get("unreadable"):
        return
    if a.get("present") != b.get("present"):
        out.append(f"{label}/: directory {'created' if b.get('present') else 'deleted'}")
    ae = a.get("entries", {}) if a.get("present") else {}
    be = b.get("entries", {}) if b.get("present") else {}
    for name in sorted(set(ae) | set(be)):
        x, y = ae.get(name), be.get(name)
        if x is None:
            out.append(f"{label}/{name}: ADDED ({y[0]}, mode {y[1]}, sha256 {_short(y[2]) if y[0] == 'file' else y[2]})")
        elif y is None:
            out.append(f"{label}/{name}: REMOVED (was {x[0]}, mode {x[1]})")
        elif x != y:
            parts = []
            if x[0] != y[0]:
                parts.append(f"kind {x[0]} -> {y[0]}")
            if x[1] != y[1]:
                parts.append(f"mode {x[1]} -> {y[1]}")
            if x[2] != y[2] or x[3] != y[3]:
                parts.append(f"sha256 {_short(x[2])} -> {_short(y[2])}" if x[0] == "file" else f"target {x[2]} -> {y[2]}")
            out.append(f"{label}/{name}: CHANGED ({', '.join(parts)})")
    if a.get("capped") or b.get("capped"):
        out.append(f"{label}/: more than {MAX_HOOK_ENTRIES} entries; the rest were not compared")


def _diff_text(label: str, a: dict[str, Any], b: dict[str, Any], out: list[str]) -> None:
    if a.get("unreadable") or b.get("unreadable"):
        return
    if a.get("present") != b.get("present"):
        created = bool(b.get("present"))
        lines = (b if created else a).get("lines") or []
        shown = ", ".join(repr(x) for x in lines[:5])
        out.append(f"{label}: {'created' if created else 'deleted'}" + (f" ({'content' if created else 'was'}: {shown})" if shown else ""))
        return
    if not a.get("present") or a.get("sha") == b.get("sha"):
        return
    al, bl = a.get("lines"), b.get("lines")
    if al is None or bl is None:
        out.append(f"{label}: changed (size {a.get('size')} -> {b.get('size')} bytes)")
        return
    added = [x for x in bl if x not in al]
    removed = [x for x in al if x not in bl]
    detail = ""
    if added:
        detail += "; added " + ", ".join(repr(x) for x in added[:5])
    if removed:
        detail += "; removed " + ", ".join(repr(x) for x in removed[:5])
    out.append(f"{label}: changed (+{len(added)} -{len(removed)} lines{detail})")


def diff_root(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    out: list[str] = []
    _diff_config("config", a["config"], b["config"], out)
    _diff_config("config.worktree", a["config.worktree"], b["config.worktree"], out)
    _diff_dir("hooks", a["hooks"], b["hooks"], out)
    for d in sorted(set(a["hooksPath"]) | set(b["hooksPath"])):
        label = f"core.hooksPath dir {d}"
        if d in a["hooksPath"] and d in b["hooksPath"]:
            _diff_dir(label, a["hooksPath"][d], b["hooksPath"][d], out)
        else:
            # The key change is reported above; also name what is now live.
            now = b["hooksPath"].get(d, {})
            names = sorted(now.get("entries", {})) if now.get("present") else []
            out.append(
                f"{label}: {'now' if d in b['hooksPath'] else 'no longer'} the hooks directory"
                + (f" (entries: {', '.join(names[:10])})" if names else "")
            )
    _diff_text("info/attributes", a["info/attributes"], b["info/attributes"], out)
    _diff_text("info/exclude", a["info/exclude"], b["info/exclude"], out)
    ah, bh = a["HEAD"], b["HEAD"]
    if not (ah["unreadable"] or bh["unreadable"]) and ah["raw"] != bh["raw"]:
        out.append(f"HEAD: {ah['raw']!r} -> {bh['raw']!r}, {_MAY_BE_ORCHESTRATOR}")
    ar, br = a["branch"], b["branch"]
    if ar and br and ar["ref"] == br["ref"]:
        if ar["sha"] != br["sha"] and "unreadable" not in (ar["where"], br["where"]):
            out.append(f"{ar['ref']}: {_short(ar['sha'])} -> {_short(br['sha'])}, {_MAY_BE_ORCHESTRATOR}")
    elif ar and not br:
        out.append(f"{ar['ref']}: the primary no longer has this branch checked out, {_MAY_BE_ORCHESTRATOR}")
    elif br and not ar:
        out.append(f"{br['ref']}: now the primary's checked-out branch at {_short(br['sha'])}, {_MAY_BE_ORCHESTRATOR}")
    return out


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def _post_envelope(event: str, report: str, summary: str) -> dict[str, Any]:
    env: dict[str, Any] = {
        "hookSpecificOutput": {"hookEventName": event, "additionalContext": report},
        "systemMessage": summary,
    }
    if event == "PostToolUse":
        # The tool already ran. `reason` lands next to the tool result; the
        # block carries no veto. PostToolUseFailure has no decision field.
        env["decision"] = "block"
        env["reason"] = report
    return env


def _report(header: str, lines: list[str]) -> str:
    shown = lines[:MAX_REPORT_LINES]
    if len(lines) > MAX_REPORT_LINES:
        shown.append(f"... and {len(lines) - MAX_REPORT_LINES} more")
    return (
        f"{header}\n"
        + "\n".join(f"- {ln}" for ln in shown)
        + "\nPhase 1 reports only; nothing was restored. The check sees bytes, not authors: "
        "any line can come from the orchestrator or a parallel agent working at the same time. "
        "The orchestrator reads only the final hand-back of this subagent, so this report belongs in it."
    )


# ---------------------------------------------------------------------------
# Hook bodies
# ---------------------------------------------------------------------------


def _pre(payload: dict[str, Any]) -> None:
    roots = _roots(payload)
    if not roots:
        return
    path = _state_path(payload)
    if path is None:
        _emit({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": (
                f"git-integrity check ({BEAD}): the payload carries no tool_use_id, so no snapshot "
                "was stored and this call's effect on the primary's git state will be reported UNVERIFIED."
            ),
        }})
        return
    snap: dict[str, Any] = {"v": 1, "taken_at": time.time(), "roots": []}
    for common, top in roots:
        snap["roots"].append(snapshot(common, top))
    try:
        _prune(os.path.dirname(path), snap["taken_at"])
        _store(path, snap)
    except OSError as exc:
        _emit({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": (
                f"git-integrity check ({BEAD}): the snapshot could not be stored "
                f"({exc.__class__.__name__}); this call's effect on the primary's git state "
                "will be reported UNVERIFIED."
            ),
        }})


def _post(payload: dict[str, Any], event: str) -> None:
    roots = _roots(payload)
    if not roots:
        return  # nothing to protect; the same test Pre applied
    changes: list[str] = []  # bytes that differ
    gaps: list[str] = []  # what could not be compared
    path = _state_path(payload)
    before: dict[str, Any] | None = None
    if path is None:
        gaps.append("the payload carries no tool_use_id, so there is no snapshot to compare against")
    else:
        try:
            with open(path, encoding="utf-8") as fh:
                before = json.load(fh)
        except (OSError, ValueError) as exc:
            gaps.append(
                f"no readable pre-call snapshot ({exc.__class__.__name__}); the PreToolUse hook did not run "
                "or its state was lost"
            )
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    after_roots = [snapshot(common, top) for common, top in roots]
    if before is not None:
        old = {r["common"]: r for r in before.get("roots", [])}
        for new in after_roots:
            prior = old.get(new["common"])
            if prior is None:
                gaps.append(f"{new['common']} is not in the pre-call snapshot")
                continue
            gaps.extend(f"pre-call snapshot incomplete: {err}" for err in prior.get("errors", []))
            changes.extend(f"{new['top']}: {ln}" for ln in diff_root(prior, new))
        changes.extend(
            f"{common}: the git directory is gone after the call"
            for common in sorted(set(old) - {r["common"] for r in after_roots})
        )
        if before.get("shrunk"):
            gaps.append(
                "pre-call snapshot exceeded its size cap; key and line detail was dropped, file hashes were compared"
            )
    for new in after_roots:
        gaps.extend(f"post-call snapshot incomplete: {err}" for err in new.get("errors", []))
    if not changes and not gaps:
        return
    if changes:
        header = f"git-integrity check ({BEAD}): the primary checkout's shared git state changed during this Bash call."
        if gaps:
            header += " Part of the comparison is UNVERIFIED."
    else:
        header = (
            f"git-integrity check ({BEAD}): the primary checkout's shared git state could not be "
            "fully verified for this Bash call (UNVERIFIED)."
        )
    lines = changes + gaps
    _lib.log_routing_event(
        rule=RULE_NAME, outcome="integrity_change" if changes else "integrity_unverified", tool_name="Bash",
        command_fragment="; ".join(lines)[:200],
        session_id=str(payload.get("session_id") or ""),
    )
    _emit(_post_envelope(
        event, _report(header, lines),
        f"git-integrity ({BEAD}): {len(changes)} change(s), {len(gaps)} unverified; first: {lines[0][:140]}",
    ))


def body(payload: dict[str, Any]) -> None:
    if not str(payload.get("agent_id") or ""):
        return  # main conversation: the rule targets subagents only
    if payload.get("tool_name") != "Bash":
        return
    event = str(payload.get("hook_event_name") or "")
    if event == "PreToolUse":
        try:
            _pre(payload)
        except Exception as exc:  # noqa: BLE001 -- Pre must never wedge a call
            _emit({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": (
                    f"git-integrity check ({BEAD}) crashed taking its snapshot "
                    f"({exc.__class__.__name__}); this call will be reported UNVERIFIED."
                ),
            }})
    elif event in ("PostToolUse", "PostToolUseFailure"):
        try:
            _post(payload, event)
        except Exception as exc:  # noqa: BLE001 -- a crash is a report line, never silence
            _emit(_post_envelope(
                event,
                _report(
                    f"git-integrity check ({BEAD}) crashed comparing snapshots (UNVERIFIED).",
                    [f"{exc.__class__.__name__}: {str(exc)[:160]}"],
                ),
                f"git-integrity ({BEAD}): check crashed, UNVERIFIED",
            ))


def main() -> None:
    # The module-level gate already consumed stdin; hand run_hook the same text.
    sys.stdin = io.StringIO(_RAW)
    # fail_closed=False is the framework flag only: `body` reports its own
    # failures (a crash is a report line, never silence), so nothing reaches
    # run_hook's fail-open branch except a bug in that reporting itself.
    _lib.run_hook(body, fail_closed=False, rule_name=RULE_NAME)


if __name__ == "__main__":
    main()
