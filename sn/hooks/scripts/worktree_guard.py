# SPDX-License-Identifier: AGPL-3.0-or-later
"""Linked-worktree detection for the sn hooks.

Serena's MCP server resolves every path against the project root it found
at startup (``--project-from-cwd``, read once in ``serena.cli``). Claude Code
subagents share the parent's MCP connection, so a subagent dispatched with
``isolation: "worktree"`` sends its edits to that shared server, and the
server writes them into the PRIMARY checkout while the subagent believes it
is working in its worktree (T3 "Serena MCP write tools escape git-worktree
isolation", 2026-08-09; T2 nexus/incident-serena-replace-in-files-wrong-
checkout-worktree-agent, 2026-09-05). Three incidents; the tool reported
success each time.

Both hooks receive the caller's ``cwd`` on stdin. A LINKED worktree (one
made by ``git worktree add``) has a ``.git`` FILE holding ``gitdir: <path>``
that points under some other repository's ``.git/worktrees/``; the primary
checkout has a ``.git`` DIRECTORY. That distinction is the whole test.

``is_linked_worktree`` only catches a call whose OWN cwd sits inside a
linked worktree -- exactly the shape a worktree-dispatched subagent has for
its whole life. It does not catch a RELOCATED session (nexus-ebx0s): one
whose cwd stays in the primary throughout (the Bash tool resets cwd to the
primary after every call, so `cd`-ing into a worktree path does not stick),
while Serena's server is rooted wherever THAT session's cwd was when it
started. From cwd alone the two are indistinguishable -- a call from the
primary asking to write to the primary looks legitimate either way.

``git_toplevel``/``record_startup_root``/``read_recorded_root`` close that
gap from the other side: ``session_start.py`` records, per session_id, the
git working-tree root of the session's cwd at ``startup`` (only ``startup``
-- see ``record_startup_root``'s docstring for why the other three
SessionStart sources do not write here). ``write_denial_reason`` is the
single PreToolUse decision this module exports, and round 2 (nexus-ebx0s)
changed its ORDER, not just its coverage:

- When this session has a recorded root, the comparison against the call's
  OWN cwd decides ALONE. A match allows -- including when cwd is itself a
  linked worktree, because that is exactly what a session that legitimately
  STARTED inside a worktree looks like (its own server really is rooted
  there; denying it anyway was round 1's own gap, caught in review). A
  mismatch denies, naming both trees, regardless of whether either side is
  a linked worktree.
- Only when NO comparison is possible (no session_id, or nothing was ever
  recorded for it) does ``is_linked_worktree`` run at all, as the fallback:
  deny when cwd itself is a linked worktree (the shape a worktree-dispatched
  subagent sharing the parent's primary-rooted server has for its whole
  life), allow otherwise. This is where round 1 left is_linked_worktree as
  an UNCONDITIONAL check; it is now conditional on having nothing better.

Stdlib only: hooks run under system python with no conexus installed.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import sys
import time

SERENA_PREFIX = "mcp__plugin_sn_serena__"

# Every Serena tool that writes to the project through the server's root.
# Memory tools are included: ``.serena/memories`` is also under that root.
# Read/navigation tools stay allowed; they return locations in the wrong
# tree, but that is visible and harmless. Kept in sync with serena-tools.txt
# by tests/test_sn_plugin.py.
SERENA_WRITE_TOOLS = frozenset({
    "delete_lines",
    "delete_memory",
    "edit_memory",
    "insert_after_symbol",
    "insert_at_line",
    "insert_before_symbol",
    "jet_brains_inline_symbol",
    "jet_brains_move",
    "jet_brains_rename",
    "jet_brains_safe_delete",
    "rename_memory",
    "rename_symbol",
    "replace_content",
    "replace_in_files",
    "replace_lines",
    "replace_symbol_body",
    "safe_delete_symbol",
    "write_memory",
})


def cwd_from_payload(payload: str) -> str:
    """The ``cwd`` field of a hook stdin payload, or '' when absent/unparseable."""
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    cwd = data.get("cwd", "")
    return cwd if isinstance(cwd, str) else ""


def is_linked_worktree(cwd: str | pathlib.Path) -> bool:
    """True when *cwd* (or an ancestor, up to the nearest ``.git``) is a linked git worktree.

    Walks up from *cwd* to the first entry named ``.git``; a directory means
    the primary checkout (or a bare-attached primary), a file whose content
    starts with ``gitdir:`` and names a ``.git/worktrees/`` path means a
    linked worktree. Submodules also carry a ``.git`` file, but their gitdir
    points under ``.git/modules/``, so they are NOT reported as worktrees.
    """
    if not cwd:
        return False
    here = pathlib.Path(cwd)
    for candidate in (here, *here.parents):
        dot_git = candidate / ".git"
        if dot_git.is_dir():
            return False
        if dot_git.is_file():
            try:
                head = dot_git.read_text(errors="replace").strip()
            except OSError:
                return False
            if not head.startswith("gitdir:"):
                return False
            target = head[len("gitdir:"):].strip().replace("\\", "/")
            return "/.git/worktrees/" in target or target.endswith("/.git/worktrees")
    return False


def is_serena_write_tool(tool_name: str) -> bool:
    """Whether *tool_name* is a Serena tool that writes through the server's root.

    The isinstance check is not defensive noise: ``tool_name`` comes out of a
    hook payload that this process did not build, and ``{"tool_name": 123}``
    is valid JSON. Without it that raises ``AttributeError`` here. The
    outcome was never unsafe — the crash reached a boundary and nothing was
    approved — but it cost a traceback where a non-string simply is not a
    write tool. Found by adversarial review of RDR-215 bead nexus-q02nx.23.
    """
    if not isinstance(tool_name, str):
        return False
    return tool_name.startswith(SERENA_PREFIX) and tool_name[len(SERENA_PREFIX):] in SERENA_WRITE_TOOLS


def deny_reason(tool_name: str, cwd: str) -> str:
    short = tool_name[len(SERENA_PREFIX):] if tool_name.startswith(SERENA_PREFIX) else tool_name
    return (
        f"sn worktree guard: {short} refused. This agent's cwd ({cwd}) is a linked git worktree, "
        "but the Serena MCP server writes to the project root it resolved at startup, which is the "
        "shared primary checkout, not this worktree. Use Edit/Write/Bash with absolute paths under "
        "the worktree, and the built-in LSP tool for navigation. Serena read tools remain available."
    )


def deny_reason_relocated(tool_name: str, cwd: str, recorded_root: str) -> str:
    """Deny reason for a relocated session (nexus-ebx0s): cwd is not itself a linked
    worktree, but it resolves to a different working tree than the one Serena's
    server was rooted at when this session started."""
    short = tool_name[len(SERENA_PREFIX):] if tool_name.startswith(SERENA_PREFIX) else tool_name
    return (
        f"sn worktree guard: {short} refused. This session's cwd ({cwd}) resolves to a different "
        f"git working tree than the one Serena's MCP server was rooted at when this session started "
        f"({recorded_root}). The write would land in {recorded_root}, not the tree this call's cwd "
        "names -- Serena's own 'DRY RUN - no changes were applied' report cannot be trusted here "
        "either (nexus-ebx0s). Use Edit/Write with absolute paths under the tree you intend instead."
    )


# ── Per-session Serena-root record (nexus-ebx0s) ─────────────────────────────
#
# ONE FILE PER SESSION, deliberately (round 3 review finding): a single
# shared ``serena-roots.json`` read-modify-written by every session on the
# box is a race -- two sessions starting together can each read the file
# before the other's write lands, and one record is silently lost, exactly
# contrary to the "concurrent sessions... must not clobber each other's
# rows" claim round 2's docstring made without earning it. The fix is not a
# lock: sn now runs on native Windows too (nexus-j4iy0), where ``fcntl``
# does not exist, so a Unix-only lock would either not build there or
# silently not protect anything there. Splitting the shared file into one
# file per session_id removes the shared mutable state instead -- two
# sessions writing their OWN, DIFFERENT files never contend for anything,
# on any platform, with no lock of any kind required.

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_-]")
#: How long a per-session record is kept before opportunistic pruning
#: removes it (``_prune_stale_records``). A real Claude Code session_id is a
#: UUID that lives far longer than this in the harness's own history, but
#: nothing here needs it past this session's lifetime, and an abandoned
#: worktree's session_id should not accumulate a file forever.
_STALE_RECORD_MAX_AGE_DAYS = 30


def _state_dir() -> pathlib.Path:
    """Where per-session Serena-root records live.

    ``XDG_STATE_HOME`` when set -- the conventional home for this kind of
    machine-local, non-config, non-cache record, and the override tests use
    for isolation -- else ``~/.claude/sn``, since that directory already
    exists on any box this plugin runs on and this state is Claude-Code-
    session-shaped, not nexus-shaped: sn has no dependency on nexus's own
    config directory.
    """
    xdg = os.environ.get("XDG_STATE_HOME")
    base = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".claude"
    return base / "sn"


def _roots_dir() -> pathlib.Path:
    return _state_dir() / "serena-roots"


def _sanitized_session_filename(session_id: str) -> str:
    """A filesystem-safe basename for *session_id*'s record file.

    Every character outside ``[A-Za-z0-9_-]`` becomes ``_`` -- a real
    session_id is a UUID, which is already entirely in that set and so
    passes through unchanged (readable on disk); anything else, including
    an adversarial ``"../../../etc/passwd"``, has every ``.`` and ``/``
    replaced, leaving no path-traversal segment standing to interpret. The
    caller still joins this against ``_roots_dir()`` with a plain ``/``, so
    there is no directory component left in the result for that join to
    honour even if the regex above were ever wrong.

    A result that is ALL underscores (the input was entirely unsafe
    characters, e.g. ``"////"``) falls back to a content hash of the
    ORIGINAL session_id instead, so two different all-unsafe inputs of the
    same length still land on two different files rather than colliding on
    one shared ``"____.json"``. This is defence in depth for a malformed or
    adversarial session_id, not a collision-free general hash -- two
    different malformed inputs that sanitise to the same MIXED string
    (retaining some safe characters) can still collide; that residual risk
    is accepted because a real session_id never exercises it.
    """
    safe = _SAFE_FILENAME_RE.sub("_", session_id)[:200]
    if not safe.strip("_"):
        digest = hashlib.sha256(session_id.encode("utf-8", "surrogateescape")).hexdigest()
        safe = f"sha256-{digest}"
    return safe + ".json"


def _session_root_file(session_id: str) -> pathlib.Path:
    """The per-session record file for *session_id*, guaranteed inside ``_roots_dir()``.

    Belt and suspenders on top of the sanitisation above: refuses a
    filename containing a path separator (either slash, so this also holds
    on native Windows) or a bare ``.``/``..`` before any caller reads or
    writes it, so a defect in the sanitiser would raise here rather than
    silently escape the directory. Unreachable given the regex above
    (neither character survives it), which is the point -- a provably-dead
    check costs nothing and catches a future regression in the sanitiser
    itself.
    """
    filename = _sanitized_session_filename(session_id)
    if "/" in filename or "\\" in filename or filename in (".", "..", ".json", "..json"):
        raise ValueError(f"sanitised session filename is unsafe: {filename!r}")
    return _roots_dir() / filename


def _prune_stale_records(max_age_days: int = _STALE_RECORD_MAX_AGE_DAYS) -> None:
    """Best-effort: delete per-session record files older than *max_age_days*.

    Opportunistic, not a scheduled sweep -- called once from
    ``record_startup_root``, itself once per session at startup, so this is
    a chance to not accumulate one file per session forever rather than a
    background job. Every failure (the directory listing, a single file's
    stat or unlink) is swallowed: pruning is housekeeping, never something a
    SessionStart hook can fail, or even log noisily, over.
    """
    try:
        roots_dir = _roots_dir()
        if not roots_dir.is_dir():
            return
        cutoff = time.time() - max_age_days * 86400
        for entry in roots_dir.iterdir():
            try:
                if entry.is_file() and entry.suffix == ".json" and entry.stat().st_mtime < cutoff:
                    entry.unlink()
            except OSError:
                continue
    except OSError:
        pass


def git_toplevel(cwd: str | pathlib.Path) -> str | None:
    """The working-tree root *cwd* belongs to, or None if it is not inside one.

    Unlike ``is_linked_worktree``, this does not classify primary vs.
    linked -- it names the root of whichever tree *cwd* is in, so two cwds
    (or a cwd and a recorded root) can be compared for "same working tree"
    regardless of which kind either one is.
    """
    if not cwd:
        return None
    here = pathlib.Path(cwd)
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            try:
                return str(candidate.resolve())
            except OSError:
                return str(candidate)
    return None


def record_startup_root(session_id: str, root: str) -> None:
    """Record *root* (already resolved via ``git_toplevel``) as the Serena root
    for *session_id*, called only on SessionStart's ``source == "startup"``.

    Serena's server is rooted once, at spawn, from the session's startup cwd
    (``--project-from-cwd``). Whether Claude Code restarts a session's MCP
    servers on ``/resume``, ``/clear``, or ``/compact`` is NOT established
    here (each of those fires SessionStart too, with a different ``source``
    value) -- so only ``startup`` writes this record. Recording on a source
    that does not actually respawn the server would silently move the
    recorded root out from under a server that never moved, turning every
    subsequent legitimate write in the ORIGINAL tree into a false denial.
    A record that lags an actual respawn is the safer failure: it degrades
    to "no mismatch detected" for whatever changed, which is exactly the
    pre-existing gap this closes only partially, not a new false denial.

    Writes ONLY this session's own file (round 3, nexus-ebx0s): no read,
    no merge, no other session's data anywhere in this call, so two
    sessions recording concurrently touch two different files and neither
    can observe or clobber the other's write. `os.replace`/`Path.replace`
    is atomic within one directory on every platform this plugin ships to,
    POSIX and native Windows alike (unlike the file-per-session choice
    itself, this one predates and is unrelated to nexus-j4iy0's Windows
    support -- `Path.replace` has always been the cross-platform primitive
    here). Best-effort: a write failure costs the guard's precision for
    this session (it fails open on the next mismatch check), never the
    session itself -- so failures here are swallowed, not raised, only
    logged.

    Also prunes stale per-session files opportunistically (best-effort,
    see ``_prune_stale_records``) -- this is the one place in the module
    that runs at all reliably once per session, so it is the natural home
    for that housekeeping even though it has nothing to do with root
    recording itself.
    """
    if not session_id or not root:
        return
    try:
        path = _session_root_file(session_id)
    except ValueError as exc:
        print(f"sn worktree guard: could not record Serena root for session {session_id}: {exc}",
              file=sys.stderr)
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps({"root": root}), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        print(f"sn worktree guard: could not record Serena root for session {session_id}: {exc}",
              file=sys.stderr)
    _prune_stale_records()


def read_recorded_root(session_id: str) -> str | None:
    """The cwd recorded for *session_id* at its last ``startup``, or None.

    Reads ONLY this session's own file -- no other session's record is ever
    opened, so this cannot observe a partial write from a concurrent
    ``record_startup_root`` call for a DIFFERENT session_id (there is
    nothing shared to observe). A concurrent write for the SAME session_id
    is not a case this module defends against: two processes racing to
    record the SAME session's OWN startup root is not a shape SessionStart
    produces (one hook invocation per session start).
    """
    if not session_id:
        return None
    try:
        path = _session_root_file(session_id)
    except ValueError:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("root")
    return value if isinstance(value, str) else None


def write_denial_reason(tool_name: str, cwd: str, session_id: str) -> str | None:
    """The PreToolUse deny reason for a Serena WRITE tool call, or None to allow.

    *tool_name* is used only to build the human-readable reason string (via
    ``deny_reason``/``deny_reason_relocated``) -- callers are expected to have
    already confirmed ``is_serena_write_tool(tool_name)`` themselves.

    Ordering (nexus-ebx0s round 2, T2 finding from round 1's own report:
    ``is_linked_worktree`` denied a session that legitimately STARTED inside a
    worktree, because it never consulted the record that would have cleared
    it): when this session_id has a recorded root, the comparison against
    *cwd*'s OWN working tree decides ALONE, and ``is_linked_worktree`` is not
    consulted at all in this branch -- consulting it here would re-deny the
    legitimate started-in-a-worktree case the comparison just cleared.

        - match  -> allow (covers both "never left the primary" and
                    "started inside this worktree and is still in it")
        - differ -> deny, naming both trees (the relocated-session shape)

    With NO recorded root (no session_id, or nothing was ever recorded for
    it -- a subagent dispatch carries its PARENT session's session_id per
    Claude Code's own hook contract, so this branch is reached only when
    that parent never recorded one either), the comparison cannot be made,
    so this falls back to the pre-round-2 behaviour: deny when *cwd* itself
    is a linked worktree (the shape a worktree-dispatched subagent sharing
    the parent's primary-rooted server has for its whole life), allow
    otherwise. Fails TOWARD denial for that known hazard shape, same as
    before this round -- and every fallback path logs to stderr, because
    "nothing recorded" and "checked, and it matched" both return None here.
    """
    recorded = read_recorded_root(session_id) if session_id else None
    if recorded is not None:
        current = git_toplevel(cwd)
        if current is None:
            print(f"sn worktree guard: cwd {cwd!r} is not inside a git working tree; allowing",
                  file=sys.stderr)
            return None
        if current == recorded:
            return None
        return deny_reason_relocated(tool_name, cwd, recorded)
    if session_id:
        print(f"sn worktree guard: no recorded Serena root for session {session_id}; "
              "falling back to the cwd-only worktree check", file=sys.stderr)
    else:
        print("sn worktree guard: no session_id on this call; falling back to the "
              "cwd-only worktree check", file=sys.stderr)
    if is_linked_worktree(cwd):
        return deny_reason(tool_name, cwd)
    return None
