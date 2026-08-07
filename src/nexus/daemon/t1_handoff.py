# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-d76vc: the T1 session-handoff marker — closes the aj564 split-brain.

The MCP server's T1 scope is frozen at process spawn (docs/architecture.md
§ T1's three scopes): it samples the transcript's session id once and has
no protocol channel to learn that ``/clear`` or ``/resume`` swapped the
session id out from under it. This module is the shared primitive both
halves of the fix consume:

* The SessionStart hook (``nexus.hooks.session_start``, source=clear|resume)
  WRITES a marker naming the new session id for each live MCP server
  sibling of the same claude process (:func:`write_handoff_marker`).
* The MCP lifespan's handoff watcher (``nexus.mcp.core``) CLAIMS and
  processes it (:func:`claim_handoff_marker`, :func:`read_claimed_marker`,
  :func:`consume_claimed_marker`).

Authentication (MUST-HOLD rn3wo.1, never-share-identity): a marker's
filename is keyed on the TARGET mcp_pid, and its payload carries the
CLAIMED claude_pid ancestor. Neither side trusts the file alone --
the writer only ever writes a marker for an mcp_pid it already found by
walking process ancestry FROM its own claude_pid (see
``nexus.session.find_mcp_sibling_pids``), and the reader independently
re-derives its own claude ancestor and rejects any marker whose claimed
claude_pid does not match (see ``nexus.mcp.core``'s handoff-watch tick).
This module itself does no ancestry verification -- it is pure file I/O
plus structural validation (well-formed JSON, required fields present).
(code-review-expert pass 1, SUGGESTION: this ancestry check prevents
ACCIDENTAL cross-session collision between two legitimate concurrent
hooks -- it is not a defense against a malicious co-resident same-UID
process that calls :func:`write_handoff_marker` directly with a
``claude_pid`` it read via ``ps``. That matches the codebase's existing
same-UID-is-trusted posture for T1 tokens elsewhere (``nexus.db.t1``),
not a new hole this module introduces.)

Live marker file: ``<config_dir>/t1_handoff.<mcp_pid>``, mode 0600 (same
sensitivity posture as the T1 session lease files in ``nexus.db.t1`` --
the marker names a session id, not a secret, but the file lives in the
user-private config dir regardless), atomic temp-file + ``os.replace``
publish so a concurrent reader never observes a torn write.

Claimed marker file (nexus-d76vc fix-round, code-review-expert pass 1
IMPORTANT/TOCTOU finding): ``<config_dir>/t1_handoff.claimed.<mcp_pid>``.
The watcher's mint-or-borrow re-lease can block for a while (flock +
HTTP); the ORIGINAL shape (read the live marker, process it, then
unconditionally unlink whatever is CURRENTLY at the live path) silently
dropped a second ``/clear`` that landed a fresh marker at the live path
mid-tick. :func:`claim_handoff_marker` closes that window with ONE atomic
rename of the live marker to the claimed path, done BEFORE any parsing;
the watcher then only ever reads/deletes the claimed copy, so a
concurrent fresh write to the live name survives untouched and gets its
own tick.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from uuid import uuid4

import structlog

_log = structlog.get_logger(__name__)

#: Filename prefix; the full live-marker name is ``t1_handoff.<mcp_pid>``.
_HANDOFF_MARKER_PREFIX = "t1_handoff."

#: Filename prefix for the tick-private claimed copy:
#: ``t1_handoff.claimed.<mcp_pid>``. Disjoint from the live prefix by
#: construction (the live name is exactly ``_HANDOFF_MARKER_PREFIX +
#: str(mcp_pid)``, which never contains a literal ``.claimed.`` segment),
#: so a glob for one never matches the other.
_CLAIMED_MARKER_PREFIX = "t1_handoff.claimed."


def handoff_marker_path(mcp_pid: int, config_dir: Path) -> Path:
    """Return the LIVE marker path for *mcp_pid* under *config_dir*."""
    return Path(config_dir) / f"{_HANDOFF_MARKER_PREFIX}{mcp_pid}"


def claimed_marker_path(mcp_pid: int, config_dir: Path) -> Path:
    """Return the tick-private CLAIMED marker path for *mcp_pid*."""
    return Path(config_dir) / f"{_CLAIMED_MARKER_PREFIX}{mcp_pid}"


@dataclass(frozen=True)
class HandoffMarker:
    """A parsed, structurally-valid (but NOT yet ancestry-verified) marker."""

    new_session_id: str
    claude_pid: int
    written_at: float


def write_handoff_marker(
    mcp_pid: int,
    *,
    new_session_id: str,
    claude_pid: int,
    config_dir: Path,
    clock: Callable[[], float] = time.time,
) -> None:
    """Publish a handoff marker for *mcp_pid* at the LIVE path.

    Called ONLY by a writer that has already verified *mcp_pid* is a live
    ``nx-mcp``/``nx-mcp-catalog`` child of *claude_pid* (see
    ``nexus.session.find_mcp_sibling_pids`` and
    ``nexus.hooks._write_t1_handoff_markers``) -- this function itself
    performs no ancestry check, it only writes what it is told.

    Atomic (temp-file + ``os.replace``), mode 0600. Overwrites any prior
    unconsumed marker for the same pid (last writer wins -- a rapid
    clear-then-resume before the watcher's next tick should hand off to
    the LATEST session id, not queue stale ones).

    NOT used for the watcher's mint-failure reinstate path (nexus-d76vc
    fix-round 2, code-review-expert IMPORTANT finding): an earlier
    revision of this docstring claimed the reinstate case was covered
    "symmetrically" by this same last-write-wins policy -- that framing
    was backwards. The reinstate write happens CHRONOLOGICALLY AFTER a
    concurrent newer marker (the mint failure that triggers a reinstate
    takes real wall-clock time), so unconditional last-write-wins would
    make the STALE reinstated marker clobber the newer one -- silent data
    loss, the same class the claim-first design exists to prevent, just
    relocated to this call site. See :func:`write_handoff_marker_if_absent`
    for the reinstate-specific primitive that closes this instead.
    """
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = handoff_marker_path(mcp_pid, config_dir)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    payload = json.dumps(
        {
            "new_session_id": new_session_id,
            "claude_pid": claude_pid,
            "written_at": clock(),
        }
    ).encode("utf-8")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))
    _log.info(
        "t1_handoff_marker_written",
        mcp_pid=mcp_pid,
        new_session_id=new_session_id,
        claude_pid=claude_pid,
    )


def write_handoff_marker_if_absent(
    mcp_pid: int,
    *,
    new_session_id: str,
    claude_pid: int,
    config_dir: Path,
    clock: Callable[[], float] = time.time,
) -> bool:
    """Publish a handoff marker at the LIVE path ONLY if none exists there.

    Used EXCLUSIVELY by the watcher's mint-failure reinstate path
    (``nexus.mcp.core._t1_handoff_tick``, nexus-d76vc fix-round 2,
    code-review-expert IMPORTANT finding). A reinstate is republishing a
    marker the watcher already claimed (renamed away from the live path)
    and then failed to process -- by the time the reinstate runs, a
    concurrent SessionStart hook may already have published a NEWER
    marker at the live path (a second ``/clear`` landing while the mint
    was failing/in-flight). That newer marker is, by construction, more
    truthful than the stale one this call would otherwise republish, and
    must win -- the OPPOSITE of :func:`write_handoff_marker`'s ordinary
    last-write-wins policy, which is correct for a genuine writer (whose
    own write IS always the newest truth at the moment it happens) but
    wrong here (the reinstated content is OLDER truth, arriving late).

    Atomicity: writes the payload to a temp file, then publishes via
    ``os.link`` (a hardlink) rather than ``os.replace`` -- ``os.link``
    FAILS with ``FileExistsError`` if the destination already exists
    instead of silently overwriting it, giving a true no-clobber publish
    with no separate exists-check-then-write window (no TOCTOU: the
    kernel makes the "does it exist" check and the "create it" action
    one atomic operation). The temp file is unlinked afterward either way
    -- on success it is now a second (throwaway) name for the same inode
    the live path also references; on failure it is simply discarded.

    Returns ``True`` if this call's marker was published (the live path
    was empty), ``False`` if a marker already existed there and was left
    COMPLETELY untouched.
    """
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = handoff_marker_path(mcp_pid, config_dir)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex}.reinstate.tmp")
    payload = json.dumps(
        {
            "new_session_id": new_session_id,
            "claude_pid": claude_pid,
            "written_at": clock(),
        }
    ).encode("utf-8")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    try:
        os.link(str(tmp), str(path))
    except FileExistsError:
        return False
    finally:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
    _log.info(
        "t1_handoff_marker_reinstated",
        mcp_pid=mcp_pid,
        new_session_id=new_session_id,
        claude_pid=claude_pid,
    )
    return True


def _parse_marker_bytes(raw: str) -> HandoffMarker | None:
    """Structural validation shared by the live and claimed read paths.

    Returns ``None`` for malformed JSON, missing/wrong-typed fields, or
    an empty ``new_session_id`` -- fail-safe, never fail-open (the
    caller must never re-lease onto a bogus value just because SOME file
    was present). Ancestry (does ``claude_pid`` actually match the
    reader's own parentage?) is NOT checked here -- that is the caller's
    job, using its own independently-derived claude ancestor, never this
    field alone.
    """
    try:
        data = json.loads(raw)
        new_session_id = data["new_session_id"]
        claude_pid = data["claude_pid"]
        written_at = data["written_at"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    if not isinstance(new_session_id, str) or not new_session_id.strip():
        return None
    if not isinstance(claude_pid, int) or isinstance(claude_pid, bool):
        return None
    try:
        written_at = float(written_at)
    except (TypeError, ValueError):
        return None
    return HandoffMarker(
        new_session_id=new_session_id.strip(),
        claude_pid=claude_pid,
        written_at=written_at,
    )


def read_handoff_marker(mcp_pid: int, config_dir: Path) -> HandoffMarker | None:
    """Read + structurally validate the LIVE marker for *mcp_pid*.

    Non-consuming (unlike the claim/read/consume triad below) -- used by
    tests and by any caller that only needs to OBSERVE whether a live
    marker is currently present, without claiming it. Production re-lease
    processing goes through :func:`claim_handoff_marker` +
    :func:`read_claimed_marker` instead (see module docstring).
    """
    path = handoff_marker_path(mcp_pid, Path(config_dir))
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return _parse_marker_bytes(raw)


def consume_handoff_marker(mcp_pid: int, config_dir: Path) -> None:
    """Best-effort, idempotent delete of the LIVE marker for *mcp_pid*."""
    path = handoff_marker_path(mcp_pid, Path(config_dir))
    try:
        path.unlink()
    except OSError:
        pass


def claim_handoff_marker(mcp_pid: int, config_dir: Path) -> Path | None:
    """Atomically claim the live marker for *mcp_pid*, or return ``None``.

    Renames the LIVE marker to the tick-private CLAIMED path in ONE
    atomic ``os.replace`` call -- there is no separate
    exists-then-read-then-delete window in which a second write to the
    live name (a second ``/clear`` landing mid-tick, while the watcher is
    still processing the first marker's blocking mint) could be silently
    dropped by an unconditional unlink of "whatever is on disk now". The
    caller processes ONLY the returned claimed path; a fresh write to the
    live name during that processing survives untouched and gets its own
    tick.

    Returns ``None`` (no claim made, no side effect) when there is no
    live marker -- the overwhelmingly common per-tick case, so this must
    stay silent for callers to treat identically to "nothing to do". Any
    prior unconsumed CLAIMED file (e.g. left by a crashed tick) is
    overwritten by the new claim -- it was already in an indeterminate,
    partially-processed state from an incarnation that never finished,
    so this is not a new loss.
    """
    config_dir = Path(config_dir)
    src = handoff_marker_path(mcp_pid, config_dir)
    dst = claimed_marker_path(mcp_pid, config_dir)
    try:
        os.replace(src, dst)
    except OSError:
        return None
    return dst


def read_claimed_marker(claimed_path: Path) -> HandoffMarker | None:
    """Read + structurally validate a CLAIMED marker file at *claimed_path*.

    Same validation as :func:`read_handoff_marker`, operating on the
    exact path :func:`claim_handoff_marker` returned rather than
    recomputing a path from an mcp_pid.
    """
    try:
        raw = Path(claimed_path).read_text(encoding="utf-8")
    except OSError:
        return None
    return _parse_marker_bytes(raw)


def consume_claimed_marker(claimed_path: Path) -> None:
    """Best-effort, idempotent delete of a CLAIMED marker file.

    Called on every terminal outcome of processing a claimed marker
    (rejected, or successfully re-leased) -- NEVER on a re-lease failure
    that re-instates the marker at the live path instead (see
    ``nexus.mcp.core._t1_handoff_tick``'s mint-failure handling, which
    calls :func:`write_handoff_marker` then this function, in that
    order, so the live path is never briefly empty).
    """
    try:
        Path(claimed_path).unlink()
    except OSError:
        pass
