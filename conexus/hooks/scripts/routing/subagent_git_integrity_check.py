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

* ``PreToolUse`` (Bash), subagent call: take a snapshot, store it under
  ``<config>/git-integrity/<session_id>/<tool_use_id>.json``. Says nothing on
  the clean path. It NEVER denies; phase 1 only reports.
* ``PostToolUse`` / ``PostToolUseFailure`` (Bash), subagent call: re-snapshot,
  diff, report. ``PostToolUse`` does not fire for a Bash call that exits
  non-zero, which is precisely a call that can write ``.git/config`` and then
  fail, so both events are registered.
* ``PreToolUse`` (Bash), MAIN-conversation call (no ``agent_id``): drain the
  findings subagents queued (see REPORT CHANNEL). With nothing queued this costs
  one ``stat`` of one path.

WHICH REPOSITORY. Only the project's. The snapshot covers the common git
directory of ``CLAUDE_PROJECT_DIR``'s repository, or of the call's ``cwd`` when
``CLAUDE_PROJECT_DIR`` is unset. A subagent working in some other repository
(a scratch clone) is silent: that is not the shared state this protects. A
linked worktree resolves to the common directory it shares with the primary.
PostToolUse compares the roots stored in the Pre snapshot, never roots derived
from its own ``cwd``, so a ``cd`` inside the call changes nothing.

WHAT IS SNAPSHOTTED (the common git directory ``C`` of the primary): ``C/config``
and ``C/config.worktree`` (parsed to keys), ``C/hooks/`` (top level only: name,
mode, sha256 per entry; subdirectories are not hashed) plus the
``core.hooksPath`` target directory when one is configured, ``C/info/attributes``
and ``C/info/exclude``, ``C/HEAD``, and the value of the branch the primary has
checked out (loose ref, else ``packed-refs``; not compared on a reftable
repository).

NOT COVERED, stated plainly:

* the primary's index and working tree (``git -C <primary> add``, ``reset --hard``,
  ``checkout -- .``): the older spelling guard remains the only defence there;
* a linked worktree's own ``config.worktree``, HEAD, index and refs
  (``git sparse-checkout set`` in a worktree, ``git config --worktree``);
* anything under ``hooks/`` below the top level, refs other than the primary's
  checked-out branch, global and system git config;
* a backgrounded Bash command (``run_in_background``): its Post fires when the
  call returns, so only writes made during that start window are seen;
* a repository where a cap bites (more than 300 hook entries, more than 1500
  config keys, a config over 512 KiB): the rest is not compared by name, and the
  log records a coverage note once per session, but it is not a "change".

SEVERITY. Two kinds of difference:

* CHANGE (reaches the subagent as ``decision: "block"`` with a user banner):
  any config key other than the routine ones below, the primary's
  ``config.worktree``, ``hooks/``, a ``core.hooksPath`` target, ``info/*``.
* INFO (log and orchestrator drain only; no decision, no banner): the routine
  products of sanctioned flows, i.e. ``branch.<name>.{remote,merge,pushremote,
  rebase}`` (``worktree add -b X origin/...``, ``push -u``, ``branch
  --set-upstream-to``, ``branch -D`` of a tracked branch) and movement of
  ``HEAD`` or the checked-out branch's ref, which is usually the orchestrator
  committing while a subagent runs.

REPORT CHANNEL. A hook that runs inside a subagent cannot reach the parent
conversation: PostToolUse output goes to the model that made the call, here the
subagent, and ``systemMessage`` goes to the user. So a CHANGE is delivered three
ways: to the subagent (``reason`` of the block, or ``additionalContext`` on a
failing call; one copy of the text, not two), to the user (``systemMessage``),
and queued for the orchestrator in ``<config>/git-integrity/<session_id>/
findings.jsonl``. ``session_id`` is shared between a subagent and its parent.
The main conversation's own ``PreToolUse`` on Bash (this same script, already
running and exiting at the gate) claims that file by an atomic rename and
returns it as ``hookSpecificOutput.additionalContext``, once. LATENCY: the
orchestrator hears of a finding at its NEXT Bash call, which may be long after
the subagent's call, and never if it makes no Bash call. Delivery to a real
Claude Code orchestrator is expected to work as documented and has not been
measured end to end. The documented alternative, a PostToolUse hook on the
``Agent`` tool, is not built: it fires at launch for a background subagent, and
the hook budget on Bash is spent (RDR-121 cap).

FAIL CLOSED ON THE REPORT SIDE (the vacuous-gate doctrine). A snapshot that
cannot be taken completely (a file that exists and cannot be read) is stored
with the error and reported by Post. A Post with no snapshot to compare against
is reported as UNVERIFIED, never skipped. The silent no-ops are "this call has
nothing to protect" (no ``agent_id``, or no project repository in sight) and
the covered-but-not-compared cases above, which are logged as coverage notes.

SECRETS. A report never carries a token. Values are shown only for a short
list of sections, command-valued keys are dropped when they look
credential-bearing, URLs lose userinfo, query and fragment, ``alias.*`` values
are never shown, and a subsection that names a URL or holds ``:`` or ``@``
(``url."https://x-access-token:T@github.com/".insteadOf``,
``credential."https://u:p@host".username``) is shown as ``section.<redacted>.key``
and stored under a hash, so the state file holds no such name either. Residual:
a command-valued key (``filter``, ``diff``, ``merge``, ``pager``, ``hook``) is
shown with only 32+ character tokens and credential-looking words removed.

Standard library only: hook scripts run under ``uv tool run python`` with no
``nexus`` import, and this runs on every Bash call. No process is spawned (the
repository is found by walking up for ``.git``). Cost, measured through
hooks.json's own ``uv tool run`` launch: see PENDING_RELEASE. The shared routing
preamble's interpreter re-exec (about 50 ms) is left out here on purpose: it
only serves scripts that import ``nexus``, and this one imports the standard
library alone. The main-conversation path imports nothing beyond ``os`` and
``sys`` unless findings are pending.
"""
from __future__ import annotations

import os
import sys

BEAD = "nexus-i93tz"
RULE_NAME = "subagent_git_integrity_check"
STATE_DIR = "git-integrity"
FINDINGS_NAME = "findings.jsonl"
MAX_FINDINGS_BYTES = 256 * 1024


# ---------------------------------------------------------------------------
# The main-conversation fast path: stdlib `os` and `sys` only, defined BEFORE
# the gate so a main call pays for nothing else.
# ---------------------------------------------------------------------------


def _config_base() -> str:
    """Same resolution as ``_endpoint_resolve.default_config_dir`` (a parity test
    compares them), written out here so the fast path imports nothing."""
    chosen = os.environ.get("NEXUS_CONFIG_DIR") or os.environ.get("NX_CONFIG_DIR")
    return chosen if chosen else os.path.join(os.path.expanduser("~"), ".config", "nexus")


def _safe_id(value: str) -> str | None:
    """A path component made from an id, or ``None`` when no safe one exists.
    Empty, and ids made only of dots (``.``, ``..``), are refused: they would
    name the parent directory."""
    cleaned = "".join(
        ch if (ch.isascii() and ch.isalnum()) or ch in "_.-" else "_" for ch in value
    )[:96]
    if not cleaned or not cleaned.strip("."):
        return None
    return cleaned


def _session_dir(session_id: str) -> str | None:
    sid = _safe_id(session_id)
    return os.path.join(_config_base(), STATE_DIR, sid) if sid else None


def _raw_string_field(raw: str, key: str) -> str:
    """The string value of top-level-looking ``"key": "value"`` in *raw* JSON,
    found without parsing. A value holding an escape is refused (``""``)."""
    needle = f'"{key}"'
    at = raw.find(needle)
    if at < 0:
        return ""
    i = at + len(needle)
    n = len(raw)
    while i < n and raw[i] in " \t\r\n":
        i += 1
    if i >= n or raw[i] != ":":
        return ""
    i += 1
    while i < n and raw[i] in " \t\r\n":
        i += 1
    if i >= n or raw[i] != '"':
        return ""
    end = raw.find('"', i + 1)
    value = raw[i + 1:end] if end > 0 else ""
    return "" if "\\" in value else value


def _drain_text(records: list[dict]) -> str:
    lines = [
        f"git-integrity check ({BEAD}): {len(records)} finding(s) queued by subagent Bash calls "
        "since your last Bash call. The check sees bytes, not authors, and phase 1 restored nothing."
    ]
    for rec in records:
        who = rec.get("agent") or "subagent"
        lines.append(f"- {who}:")
        for tag, text in rec.get("lines", []):
            lines.append(f"    [{tag}] {text}")
    lines.append(
        "INFO lines are routine flows (upstream tracking config, HEAD or branch movement) and may be "
        "your own work. CHANGE lines were also shown to the subagent and the user."
    )
    return "\n".join(lines)


def _drain_main(payload: dict) -> None:
    """Claim and return the queued findings once, from a main-conversation
    ``PreToolUse`` on Bash."""
    if str(payload.get("agent_id") or "") or payload.get("tool_name") != "Bash":
        return
    if payload.get("hook_event_name") != "PreToolUse":
        return
    session = _session_dir(str(payload.get("session_id") or ""))
    if session is None:
        return
    import json  # noqa: PLC0415 -- only when findings are pending: the fast path stays at os/sys cost

    pending = os.path.join(session, FINDINGS_NAME)
    claimed = f"{pending}.drain.{os.getpid()}"
    try:
        os.rename(pending, claimed)  # atomic: of two racing calls, one wins
    except OSError:
        return
    records: list[dict] = []
    try:
        with open(claimed, encoding="utf-8", errors="replace") as fh:
            for line in fh.read(MAX_FINDINGS_BYTES + 4096).splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    records.append(rec)
    except OSError:
        pass
    finally:
        try:
            os.unlink(claimed)
        except OSError:
            pass
    if not records:
        return
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "additionalContext": _drain_text(records),
    }}))
    sys.stdout.flush()


def _main_conversation(raw: str) -> None:
    """Fast path for a payload with no ``agent_id``: one ``stat`` when nothing is
    pending; the payload is parsed only when something is."""
    session = _session_dir(_raw_string_field(raw, "session_id"))
    if session is None or not os.path.exists(os.path.join(session, FINDINGS_NAME)):
        return
    import json  # noqa: PLC0415 -- only when findings are pending: the fast path stays at os/sys cost

    try:
        payload = json.loads(raw)
    except ValueError:
        return
    if isinstance(payload, dict):
        _drain_main(payload)


# Cheap gate BEFORE any other import: the rule targets subagents, and a hook
# fires on every Bash call. A payload with no `agent_id` key (the main
# conversation) takes the fast path above and exits. The key check is a
# superset test; `body` still requires a non-empty value.
_RAW = sys.stdin.read() if __name__ == "__main__" else ""
if __name__ == "__main__" and '"agent_id"' not in _RAW:
    _main_conversation(_RAW)
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

#: Snapshot caps. A pathological repository must not make a hook slow or its
#: state file large; a capped snapshot says so in the log (once per session).
MAX_CONFIG_BYTES = 512 * 1024
MAX_CONFIG_KEYS = 1500
MAX_HOOK_ENTRIES = 300
MAX_HASH_BYTES = 1024 * 1024
MAX_TEXT_LINES = 200
MAX_TEXT_BYTES = 64 * 1024
MAX_SNAPSHOT_BYTES = 512 * 1024
MAX_REPORT_LINES = 60
MAX_FINDING_LINES = 40
STALE_SNAPSHOT_SECONDS = 6 * 3600
STALE_SESSION_SECONDS = 2 * 86400
#: Cross-session pruning scans every entry (a DirEntry stat is cheap) but runs
#: at most once per PRUNE_INTERVAL_SECONDS and removes at most MAX_PRUNE_REMOVALS.
MAX_PRUNE_SCAN = 5000
MAX_PRUNE_REMOVALS = 200
PRUNE_INTERVAL_SECONDS = 3600
PRUNE_MARKER = ".last-prune"

#: Config sections whose values are shown in a report. Everything else is
#: reported by key name only (``credential.*``, ``http.*``, ``url.*`` and the
#: like can hold tokens). ``alias`` is deliberately absent: an alias is a free
#: shell command. A remote's url is shown with userinfo, query and fragment removed.
_SHOW_SECTIONS = frozenset({
    "core", "user", "init", "branch", "extensions", "include", "includeif",
    "filter", "merge", "diff", "pager", "gc", "hook", "safe",
    "worktree", "maintenance", "scalar", "feature", "fetch", "pull", "push",
    "sequence", "receive", "uploadpack", "submodule", "commit", "tag",
})
_SHOW_REMOTE_KEYS = frozenset({"url", "pushurl", "fetch", "push", "tagopt", "vcs"})
#: Sections whose subsection names are URLs or URL patterns.
_URL_SUBSECTIONS = frozenset({"url", "credential", "http", "includeif"})
#: Keys written by sanctioned flows; a change to one is INFO, not a CHANGE.
#: gh-merge-base (gh pr checkout), vscode-merge-base and description are bookkeeping
#: that tools write beside a branch; none runs code.
_ROUTINE_BRANCH_KEYS = frozenset({
    "remote", "merge", "pushremote", "rebase",
    "gh-merge-base", "vscode-merge-base", "description",
})

_URL_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s'\"]+")
_TOKENISH = re.compile(r"[A-Za-z0-9_\-]{32,}")
_CREDENTIAL_WORDS = re.compile(r"(?i)(authorization|bearer|token|passw|secret|api[-_]?key|credential)")
_SECTION = re.compile(
    r'^\s*\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]\s*(.*)$'
)
_KEYLINE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9-]*)\s*(?:=\s*(.*))?$")

CHANGE = "CHANGE"
INFO = "INFO"
UNVERIFIED = "UNVERIFIED"
#: One finding line: ``(tag, text)``.
Line = tuple[str, str]


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
    """The one project repository: ``CLAUDE_PROJECT_DIR``'s, else the call's cwd's.
    Any other repository the cwd happens to be in is not what this protects."""
    start = os.environ.get("CLAUDE_PROJECT_DIR", "") or str(payload.get("cwd") or "")
    found = _common_dir_for(start)
    return [found] if found else []


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


def _scrub_url(url: str) -> str:
    """Drop userinfo (everything before the LAST ``@``, so a password holding
    ``/`` goes too), the query string and the fragment."""
    scheme, sep, rest = url.partition("://")
    cut = min((i for i in (rest.find("?"), rest.find("#")) if i >= 0), default=-1)
    tail = ""
    if cut >= 0:
        rest, tail = rest[:cut], "?<removed>"
    if "@" in rest:
        rest = "***@" + rest.rsplit("@", 1)[1]
    return f"{scheme}{sep}{rest}{tail}"


def _scrub(value: str, *, urlish: bool = False) -> str:
    value = _URL_TEXT.sub(lambda m: _scrub_url(m.group(0)), value)
    if urlish and "://" not in value and "@" in value:  # scp-like user:pass@host:path
        value = "***@" + value.rsplit("@", 1)[1]
    if _CREDENTIAL_WORDS.search(value):
        return "<redacted: looks credential-bearing>"
    if not value.startswith(("/", "~", "./", "../")):  # a filesystem path is not a token
        value = _TOKENISH.sub("<redacted>", value)
    return value if len(value) <= 120 else value[:117] + "..."


def _shown(section: str, name: str) -> bool:
    if section == "remote":
        return name in _SHOW_REMOTE_KEYS
    return section in _SHOW_SECTIONS


def _sub_is_sensitive(section: str, sub: str) -> bool:
    return section in _URL_SUBSECTIONS or ":" in sub or "@" in sub


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
    """``({canonical_key: {"d": display, "s": section, "n": name, "v": [[shown|None, sha16], ...]}}, capped)``.

    Not a full git-config parser: section headers (including the old
    ``[section.sub]`` form), keys, comments and trailing-backslash joins.
    A misparse can only blur WHICH key changed, never hide that the file did:
    the whole-file hash is compared first. A sensitive subsection is stored
    only as a hash (canonical form) and shown as ``<redacted>``."""
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
        lname = name.lower()
        if sub is None:
            canon, display = f"{section}.{lname}", f"{section}.{name}"
        elif _sub_is_sensitive(section, sub):
            canon = f"{section}.<h:{_h(sub.encode())[:12]}>.{lname}"
            display = f"{section}.<redacted>.{name}"
        else:
            canon, display = f"{section}.{sub}.{lname}", f"{section}.{sub}.{name}"
        if canon not in keys:
            if len(keys) >= MAX_CONFIG_KEYS:
                capped = True
                continue
            keys[canon] = {"d": display, "s": section, "n": lname, "v": []}
        urlish = section == "remote" and lname in ("url", "pushurl")
        shown = _scrub(value, urlish=urlish) if _shown(section, lname) else None
        keys[canon]["v"].append([shown, _h(value.encode())[:16]])
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
    notes: list[str] = []
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
    if branch_ref and os.path.isdir(os.path.join(common, "reftable")):
        snap["branch"] = {"ref": branch_ref, "sha": None, "where": "reftable"}
        notes.append("reftable ref backend: the checked-out branch's ref value is not compared")
    else:
        snap["branch"] = _ref_value(common, branch_ref, errors) if branch_ref else None
    capped_dirs = [snap["hooks"], *extra.values()]
    if any(isinstance(d, dict) and d.get("capped") for d in capped_dirs):
        notes.append(f"a hooks directory has more than {MAX_HOOK_ENTRIES} entries: the rest are not compared")
    if any(isinstance(c, dict) and c.get("capped") for c in (snap["config"], snap["config.worktree"])):
        notes.append(f"a git config is over its cap ({MAX_CONFIG_KEYS} keys or {MAX_CONFIG_BYTES} bytes): later keys are not compared by name")
    snap["errors"] = errors
    snap["notes"] = notes
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


def _state_path(payload: dict[str, Any]) -> str | None:
    """The snapshot path, or ``None`` when the session id or the tool use id
    is missing or unusable (empty, or only dots)."""
    session = _session_dir(str(payload.get("session_id") or ""))
    leaf = _safe_id(str(payload.get("tool_use_id") or ""))
    if session is None or leaf is None:
        return None
    return os.path.join(session, leaf[:128] + ".json")


def _ensure_private_dirs(session_dir: str) -> None:
    """Every level we own is 0700: ``makedirs(mode=)`` would apply it to the
    leaf only. An existing level (left by an older run) is tightened."""
    base = os.path.dirname(session_dir)
    os.makedirs(os.path.dirname(base), mode=0o700, exist_ok=True)
    for level in (base, session_dir):
        try:
            os.mkdir(level, 0o700)
        except FileExistsError:
            pass
        os.chmod(level, 0o700)


def _write_private(path: str, data: bytes, *, append: bool = False, exclusive: bool = False) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    if exclusive:
        flags |= os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _prune(session_dir: str, now: float) -> None:
    """Remove snapshots a Post never consumed (call interrupted, or denied by
    another hook) and stale drain claims; and, at most once an hour, whole
    session directories untouched for two days. The findings file and coverage
    markers are not touched by the six-hour rule: only the two-day directory
    rule removes them."""
    try:
        for name in os.listdir(session_dir):
            if not (name.endswith((".json", ".tmp")) or ".drain." in name):
                continue
            p = os.path.join(session_dir, name)
            try:
                if now - os.stat(p).st_mtime > STALE_SNAPSHOT_SECONDS:
                    os.unlink(p)
            except OSError:
                pass
    except OSError:
        return
    parent = os.path.dirname(session_dir)
    marker = os.path.join(parent, PRUNE_MARKER)
    try:
        if now - os.stat(marker).st_mtime < PRUNE_INTERVAL_SECONDS:
            return
    except OSError:
        pass
    try:
        with open(marker, "w", encoding="utf-8"):
            pass
        removed = 0
        with os.scandir(parent) as scan:
            for n, de in enumerate(scan):
                if n >= MAX_PRUNE_SCAN or removed >= MAX_PRUNE_REMOVALS:
                    break
                try:
                    if de.path == session_dir or not de.is_dir(follow_symlinks=False):
                        continue
                    if now - de.stat(follow_symlinks=False).st_mtime <= STALE_SESSION_SECONDS:
                        continue
                    for leaf in os.listdir(de.path):
                        os.unlink(os.path.join(de.path, leaf))
                    os.rmdir(de.path)
                    removed += 1
                except OSError:
                    pass
    except OSError:
        pass


def _store(path: str, snap: dict[str, Any]) -> None:
    blob = json.dumps(snap, separators=(",", ":"))
    if len(blob) > MAX_SNAPSHOT_BYTES:
        blob = json.dumps(_shrink(snap), separators=(",", ":"))
    tmp = f"{path}.{os.getpid()}.tmp"
    _write_private(tmp, blob.encode("utf-8"))
    os.replace(tmp, path)


def _note_once(payload: dict[str, Any], notes: list[str]) -> None:
    """Coverage notes go to the routing log once per session and note kind, so a
    large repository does not log on every Bash call."""
    session = _session_dir(str(payload.get("session_id") or ""))
    for note in notes:
        kind = _safe_id(note.split(":", 1)[0].replace(" ", "-"))
        if session is not None and kind is not None:
            try:
                _ensure_private_dirs(session)
                _write_private(os.path.join(session, f"note-{kind}"), b"", exclusive=True)
            except FileExistsError:
                continue
            except OSError:
                pass
        _lib.log_routing_event(
            rule=RULE_NAME, outcome="integrity_coverage_note", tool_name="Bash",
            command_fragment=note[:200], session_id=str(payload.get("session_id") or ""),
        )


def _queue_finding(payload: dict[str, Any], lines: list[Line]) -> None:
    """Append one finding record for the orchestrator's next Bash call. Quiet on
    failure: the subagent and the user were told through the other channels."""
    session = _session_dir(str(payload.get("session_id") or ""))
    if session is None:
        return
    who = str(payload.get("agent_type") or "subagent")
    agent_id = str(payload.get("agent_id") or "")
    record = {
        "ts": time.time(),
        "agent": f"{who} ({agent_id[:12]})" if agent_id else who,
        "lines": [list(ln) for ln in lines[:MAX_FINDING_LINES]],
    }
    try:
        _ensure_private_dirs(session)
        path = os.path.join(session, FINDINGS_NAME)
        try:
            if os.stat(path).st_size > MAX_FINDINGS_BYTES:
                return
        except FileNotFoundError:
            pass
        _write_private(path, (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8"), append=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

_MAY_BE_ORCHESTRATOR = "changed during a subagent call (may be the orchestrator)"


def _short(sha: str | None) -> str:
    return (sha or "none")[:12]


def _key_tag(entry: dict[str, Any]) -> str:
    """INFO for the branch tracking keys sanctioned flows write; CHANGE otherwise."""
    if entry.get("s") == "branch" and entry.get("n") in _ROUTINE_BRANCH_KEYS:
        return INFO
    return CHANGE


def _diff_config(label: str, a: dict[str, Any], b: dict[str, Any], out: list[Line]) -> None:
    if a.get("unreadable") or b.get("unreadable"):
        return  # already reported as a snapshot error
    if a.get("present") != b.get("present"):
        out.append((CHANGE, f"{label}: {'created' if b.get('present') else 'deleted'}"))
        return
    if not a.get("present") or a.get("sha") == b.get("sha"):
        return
    ak, bk = a.get("keys"), b.get("keys")
    if ak is None or bk is None:
        out.append((CHANGE, f"{label}: file changed (size {a.get('size')} -> {b.get('size')} bytes; too large to name keys)"))
        return
    named = 0
    for canon in sorted(set(ak) | set(bk)):
        av, bv = ak.get(canon), bk.get(canon)
        entry = bv or av
        disp = entry["d"]
        if av is None:
            out.append((_key_tag(entry), f"{label}: key {disp} ADDED{_values(bv)}"))
        elif bv is None:
            out.append((_key_tag(entry), f"{label}: key {disp} REMOVED{_values(av)}"))
        elif [x[1] for x in av["v"]] != [x[1] for x in bv["v"]]:
            out.append((_key_tag(entry), f"{label}: key {disp} CHANGED (before{_values(av)}; after{_values(bv)})"))
        else:
            continue
        named += 1
    if named == 0:
        out.append((CHANGE, f"{label}: file bytes changed with no key-level difference (comment, whitespace or an unparsed form)"))


def _values(entry: dict[str, Any] | None) -> str:
    if not entry:
        return ""
    vals = [v[0] for v in entry["v"]]
    if all(v is None for v in vals):
        return " (value not shown)"
    return " (" + ", ".join(repr(v) if v is not None else "<not shown>" for v in vals) + ")"


def _diff_dir(label: str, a: dict[str, Any], b: dict[str, Any], out: list[Line]) -> None:
    if a.get("unreadable") or b.get("unreadable"):
        return
    if a.get("present") != b.get("present"):
        out.append((CHANGE, f"{label}/: directory {'created' if b.get('present') else 'deleted'}"))
    ae = a.get("entries", {}) if a.get("present") else {}
    be = b.get("entries", {}) if b.get("present") else {}
    for name in sorted(set(ae) | set(be)):
        x, y = ae.get(name), be.get(name)
        if x is None:
            out.append((CHANGE, f"{label}/{name}: ADDED ({y[0]}, mode {y[1]}, sha256 {_short(y[2]) if y[0] == 'file' else y[2]})"))
        elif y is None:
            out.append((CHANGE, f"{label}/{name}: REMOVED (was {x[0]}, mode {x[1]})"))
        elif x != y:
            parts = []
            if x[0] != y[0]:
                parts.append(f"kind {x[0]} -> {y[0]}")
            if x[1] != y[1]:
                parts.append(f"mode {x[1]} -> {y[1]}")
            if x[2] != y[2] or x[3] != y[3]:
                parts.append(f"sha256 {_short(x[2])} -> {_short(y[2])}" if x[0] == "file" else f"target {x[2]} -> {y[2]}")
            out.append((CHANGE, f"{label}/{name}: CHANGED ({', '.join(parts)})"))


def _diff_text(label: str, a: dict[str, Any], b: dict[str, Any], out: list[Line]) -> None:
    if a.get("unreadable") or b.get("unreadable"):
        return
    if a.get("present") != b.get("present"):
        created = bool(b.get("present"))
        lines = (b if created else a).get("lines") or []
        shown = ", ".join(repr(x) for x in lines[:5])
        out.append((CHANGE, f"{label}: {'created' if created else 'deleted'}" + (f" ({'content' if created else 'was'}: {shown})" if shown else "")))
        return
    if not a.get("present") or a.get("sha") == b.get("sha"):
        return
    al, bl = a.get("lines"), b.get("lines")
    if al is None or bl is None:
        out.append((CHANGE, f"{label}: changed (size {a.get('size')} -> {b.get('size')} bytes)"))
        return
    added = [x for x in bl if x not in al]
    removed = [x for x in al if x not in bl]
    detail = ""
    if added:
        detail += "; added " + ", ".join(repr(x) for x in added[:5])
    if removed:
        detail += "; removed " + ", ".join(repr(x) for x in removed[:5])
    out.append((CHANGE, f"{label}: changed (+{len(added)} -{len(removed)} lines{detail})"))


def diff_root(a: dict[str, Any], b: dict[str, Any]) -> list[Line]:
    out: list[Line] = []
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
            out.append((
                CHANGE,
                f"{label}: {'now' if d in b['hooksPath'] else 'no longer'} the hooks directory"
                + (f" (entries: {', '.join(names[:10])})" if names else ""),
            ))
    _diff_text("info/attributes", a["info/attributes"], b["info/attributes"], out)
    _diff_text("info/exclude", a["info/exclude"], b["info/exclude"], out)
    ah, bh = a["HEAD"], b["HEAD"]
    if not (ah["unreadable"] or bh["unreadable"]) and ah["raw"] != bh["raw"]:
        out.append((INFO, f"HEAD: {ah['raw']!r} -> {bh['raw']!r}, {_MAY_BE_ORCHESTRATOR}"))
    ar, br = a["branch"], b["branch"]
    if (ar and ar["where"] == "reftable") or (br and br["where"] == "reftable"):
        return out  # the ref value cannot be read here; logged as a coverage note
    if ar and br and ar["ref"] == br["ref"]:
        if ar["sha"] != br["sha"] and "unreadable" not in (ar["where"], br["where"]):
            out.append((INFO, f"{ar['ref']}: {_short(ar['sha'])} -> {_short(br['sha'])}, {_MAY_BE_ORCHESTRATOR}"))
    elif ar and not br:
        out.append((INFO, f"{ar['ref']}: the primary no longer has this branch checked out, {_MAY_BE_ORCHESTRATOR}"))
    elif br and not ar:
        out.append((INFO, f"{br['ref']}: now the primary's checked-out branch at {_short(br['sha'])}, {_MAY_BE_ORCHESTRATOR}"))
    return out


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def _report(header: str, lines: list[Line]) -> str:
    shown = lines[:MAX_REPORT_LINES]
    body_lines = [f"- [{tag}] {text}" for tag, text in shown]
    if len(lines) > MAX_REPORT_LINES:
        body_lines.append(f"... and {len(lines) - MAX_REPORT_LINES} more")
    return (
        f"{header}\n" + "\n".join(body_lines)
        + "\nPhase 1 reports only; nothing was restored. The check sees bytes, not authors: any line can "
        "come from the orchestrator or a parallel agent working at the same time. A hook inside a subagent "
        "cannot reach the parent conversation; a copy is queued for the orchestrator and is expected to "
        "reach it at its next Bash call. Put this in your hand-back as well."
    )


def _context_env(event: str, text: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def _finish(payload: dict[str, Any], event: str, lines: list[Line]) -> None:
    """Log, queue for the orchestrator, and tell the subagent and the user.
    CHANGE lines block-and-banner; INFO lines are logged and queued only;
    UNVERIFIED lines reach the subagent as context, with no banner."""
    if not lines:
        return
    changes = [ln for ln in lines if ln[0] == CHANGE]
    gaps = [ln for ln in lines if ln[0] == UNVERIFIED]
    outcome = "integrity_change" if changes else ("integrity_unverified" if gaps else "integrity_info")
    _lib.log_routing_event(
        rule=RULE_NAME, outcome=outcome, tool_name="Bash",
        command_fragment="; ".join(f"{tag}: {text}" for tag, text in lines)[:200],
        session_id=str(payload.get("session_id") or ""),
    )
    _queue_finding(payload, lines)
    if changes:
        header = f"git-integrity check ({BEAD}): the primary checkout's shared git state changed during this Bash call."
        if gaps:
            header += " Part of the comparison is UNVERIFIED."
        report = _report(header, lines)
        banner = f"git-integrity ({BEAD}): {len(changes)} change(s), {len(gaps)} unverified; first: {changes[0][1][:140]}"
        if event == "PostToolUse":
            # The tool already ran; the block carries no veto. `reason` is the
            # one copy of the text (no additionalContext beside it).
            _emit({"decision": "block", "reason": report, "systemMessage": banner})
        else:  # PostToolUseFailure has no decision field
            env = _context_env(event, report)
            env["systemMessage"] = banner
            _emit(env)
    elif gaps:
        header = (
            f"git-integrity check ({BEAD}): the primary checkout's shared git state could not be "
            "fully verified for this Bash call (UNVERIFIED)."
        )
        _emit(_context_env(event, _report(header, lines)))
    # INFO only: nothing for the subagent or the user.


# ---------------------------------------------------------------------------
# Hook bodies
# ---------------------------------------------------------------------------


def _pre_unverified(payload: dict[str, Any], text: str) -> None:
    _queue_finding(payload, [(UNVERIFIED, text)])
    _emit(_context_env("PreToolUse", f"git-integrity check ({BEAD}): {text}"))


def _pre(payload: dict[str, Any]) -> None:
    roots = _roots(payload)
    if not roots:
        return
    path = _state_path(payload)
    if path is None:
        _pre_unverified(
            payload,
            "the payload carries no usable session_id or tool_use_id, so no snapshot was stored "
            "and this call's effect on the primary's git state will be reported UNVERIFIED.",
        )
        return
    snap: dict[str, Any] = {"v": 2, "taken_at": time.time(), "roots": []}
    for common, top in roots:
        snap["roots"].append(snapshot(common, top))
    try:
        _ensure_private_dirs(os.path.dirname(path))
        _prune(os.path.dirname(path), snap["taken_at"])
        _store(path, snap)
    except OSError as exc:
        _pre_unverified(
            payload,
            f"the snapshot could not be stored ({exc.__class__.__name__}); this call's effect "
            "on the primary's git state will be reported UNVERIFIED.",
        )


def _load_snapshot(path: str | None) -> tuple[dict[str, Any] | None, str]:
    """``(snapshot, why_not)``; the file is consumed either way."""
    if path is None:
        return None, "the payload carries no usable session_id or tool_use_id, so there is no snapshot to compare against"
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), ""
    except (OSError, ValueError) as exc:
        return None, (
            f"no readable pre-call snapshot ({exc.__class__.__name__}); the PreToolUse hook did not run "
            "or its state was lost"
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _post(payload: dict[str, Any], event: str) -> None:
    path = _state_path(payload)
    before, why_not = _load_snapshot(path)
    lines: list[Line] = []
    if before is not None:
        # The roots are the ones Pre stored, never ones derived from this
        # call's cwd: a `cd` inside the call changes nothing.
        roots = [(r["common"], r["top"]) for r in before.get("roots", []) if isinstance(r, dict) and "common" in r]
    else:
        roots = _roots(payload)
        if not roots:
            return  # nothing to protect; the same test Pre applied
        lines.append((UNVERIFIED, why_not))
    after_roots = [snapshot(common, top) for common, top in roots]
    if before is not None:
        old = {r["common"]: r for r in before.get("roots", []) if isinstance(r, dict) and "common" in r}
        for new in after_roots:
            prior = old.get(new["common"])
            if prior is None:
                continue
            lines.extend((UNVERIFIED, f"pre-call snapshot incomplete: {err}") for err in prior.get("errors", []))
            lines.extend((tag, f"{new['top']}: {text}") for tag, text in diff_root(prior, new))
        if before.get("shrunk"):
            lines.append((
                UNVERIFIED,
                "pre-call snapshot exceeded its size cap; key and line detail was dropped, file hashes were compared",
            ))
    notes: list[str] = []
    for new in after_roots:
        lines.extend((UNVERIFIED, f"post-call snapshot incomplete: {err}") for err in new.get("errors", []))
        notes.extend(n for n in new.get("notes", []) if n not in notes)
    if notes:
        _note_once(payload, notes)
    # CHANGE first, then INFO, then UNVERIFIED, for a readable report.
    order = {CHANGE: 0, INFO: 1, UNVERIFIED: 2}
    lines.sort(key=lambda ln: order[ln[0]])
    _finish(payload, event, lines)


def body(payload: dict[str, Any]) -> None:
    if not str(payload.get("agent_id") or ""):
        _drain_main(payload)  # main conversation: drain what subagents queued
        return
    if payload.get("tool_name") != "Bash":
        return
    event = str(payload.get("hook_event_name") or "")
    if event == "PreToolUse":
        try:
            _pre(payload)
        except Exception as exc:  # noqa: BLE001 -- Pre must never wedge a call
            _pre_unverified(
                payload,
                f"the check crashed taking its snapshot ({exc.__class__.__name__}); this call will be reported UNVERIFIED.",
            )
    elif event in ("PostToolUse", "PostToolUseFailure"):
        try:
            _post(payload, event)
        except Exception as exc:  # noqa: BLE001 -- a crash is a report line, never silence
            _finish(payload, event, [(UNVERIFIED, f"the check crashed comparing snapshots: {exc.__class__.__name__}: {str(exc)[:160]}")])


def main() -> None:
    # The module-level gate already consumed stdin; hand run_hook the same text.
    sys.stdin = io.StringIO(_RAW)
    # fail_closed=False is the framework flag only: `body` reports its own
    # failures (a crash is a report line, never silence), so nothing reaches
    # run_hook's fail-open branch except a bug in that reporting itself.
    _lib.run_hook(body, fail_closed=False, rule_name=RULE_NAME)


if __name__ == "__main__":
    main()
