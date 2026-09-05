#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-121 routing-hook framework.

Helpers every routing hook imports. The hook protocol is:

* Read JSON from stdin (the Claude Code PreToolUse payload).
* Print exactly one JSON envelope to stdout.
* Exit 0 on every code path including unexpected exceptions.

Decision envelope shape (PreToolUse):

    allow:
        {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": "..."   # only when allow carries advisory text
        }}

    deny (see ``deny_envelope`` — the reason rides in two audience-specific
    fields, with ``reason`` kept for legacy compatibility):
        {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "<full reason>",   # what the MODEL reads
            "reason": "<full reason>"                       # legacy alias
         },
         "systemMessage": "<short summary>"}                # the USER's transcript banner

Fail-open is the default. Hooks opt in to fail-closed by passing
``fail_closed=True`` to ``run_hook``; the registry.yaml ``fail_closed:
true`` flag is the source of truth and the hook script reads its own
rule entry to decide.

Escape token: a command may include ``# routing-allow: <reason>``
(reason >= 8 characters) to bypass any routing hook. The token is
audited in the telemetry log so over-use is visible.

WRITER SWAP (nexus-gjv9b PART 2, Sam directive 2026-08-20):
``log_routing_event`` records to the engine's ``routing_events`` table
now (best-effort POST via ``urllib``, ~250ms timeout), not the JSONL
log below -- see that function's own docstring for the full design
decision (metered drop on service-down, never a JSONL fallback). The
JSONL machinery (``_log_path``, ``_rotate_log_if_oversized``) stays in
place with no caller from this module any more, deferred to this
bead's PART 3 (protects any install still running pre-swap code).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import re
import sys
import time
from typing import Any, Callable

ESCAPE_TOKEN = "# routing-allow:"
ESCAPE_REASON_MIN_LENGTH = 8

def _default_log_path() -> pathlib.Path:
    """Fallback routing-log path, resolved at CALL time.

    Honours ``NEXUS_CONFIG_DIR`` before falling back to ``~/.config/nexus``.
    Without that, this log was the ONE append log in the tree that ignored
    the config-dir override (the per-session capability census, same append
    -log shape, already resolved through ``nexus.config.nexus_config_dir``).
    The consequence was not cosmetic: a test suite sets ``NEXUS_CONFIG_DIR``
    to isolate itself, this log ignored it and wrote to the REAL config dir
    on every routed tool call, and the nexus-pfuns mutation guard failed the
    whole run -- twice on 2026-08-22, each time reported as `rc=1` with zero
    failing tests. Read the env var directly rather than importing
    ``nexus.config``: this module is loaded by a bare interpreter with no
    dependency on the ``nexus`` package.

    nexus-pfuns: this used to be a module-level constant
    (``pathlib.Path.home()`` evaluated once at import). A bare-interpreter
    subprocess (this script has no dependency on the ``nexus`` package)
    imports fresh per invocation, so the import-time freeze never actually
    diverged from a plain call-time read within a single process lifetime
    -- but freezing it made a direct in-process test (``_load_lib()``
    re-exec'ing the module, ``tests/test_routing_hooks.py``) unable to
    prove the fallback tracks a patched ``Path.home()`` at all, and it is
    the same import-time-default class already fixed once in
    ``gc_purge_marker.py`` (T2 nexus/gc-purge-marker-xdist-leak-2026-08-20)
    for exactly that reason -- a module-level default resists any patch
    applied after the module's own import.
    """
    override = os.environ.get("NEXUS_CONFIG_DIR", "").strip()
    if override:
        return pathlib.Path(override) / "routing_log.jsonl"
    return pathlib.Path.home() / ".config" / "nexus" / "routing_log.jsonl"

# ---------------------------------------------------------------------------
# Envelope builders (pure — return JSON strings)
# ---------------------------------------------------------------------------


def allow_envelope(context: str = "") -> str:
    """Return an allow envelope as a JSON string."""
    payload: dict[str, Any] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
    }
    if context:
        payload["additionalContext"] = context
    return json.dumps({"hookSpecificOutput": payload})


def deny_envelope(reason: str, summary: str | None = None) -> str:
    """Return a deny envelope as a JSON string.

    The reason rides in three fields for cross-version robustness:

    * ``permissionDecisionReason`` -- the canonical PreToolUse field
      current Claude Code feeds back to the model on a deny. Carries the
      *full* ``reason`` (cause + remediation) so the model can correct.
    * ``systemMessage`` (top-level) -- surfaced in the user transcript.
      Carries the short ``summary`` so the banner stays a one-liner
      instead of the full remediation essay.
    * ``reason`` -- the legacy key earlier envelopes used.

    Earlier envelopes carried *only* ``reason``, which current Claude
    Code does not read: a deny then arrived as a bare "denied" with no
    cause and no remediation, leaving the model to guess what to do
    next. Emitting the canonical field is what makes the redirect
    message actually reach the model.

    ``summary`` decouples the two audiences. When omitted, the first
    non-empty line of ``reason`` is used so callers that don't supply a
    summary still get a terse banner rather than the whole block.
    """
    # Strip BEFORE the truthiness check: a whitespace-only reason is truthy, so
    # ``reason or default`` would keep it, and ``"".splitlines()[0]`` would then
    # IndexError. deny_envelope is on every routing hook's deny path, so it must
    # never raise. Stripping makes the guard fire and keeps the first-line slice
    # safe (reason is now non-empty).
    reason = reason.strip() or "(no reason provided)"
    system_message = summary or reason.splitlines()[0]
    payload = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
        "reason": reason,
    }
    return json.dumps(
        {"hookSpecificOutput": payload, "systemMessage": system_message}
    )


def warn_envelope(message: str) -> str:
    """Semantic alias for ``allow_envelope`` that signals advisory intent.

    Routing hooks emit warnings when a pattern looks suspicious but the
    command should proceed. The permission decision stays ``allow``;
    the message rides in ``additionalContext`` so the user sees it.
    """
    return allow_envelope(message)


# ---------------------------------------------------------------------------
# Stdout writers (impure — print then exit 0)
# ---------------------------------------------------------------------------


def allow(context: str = "") -> None:
    """Emit allow envelope to stdout and ``exit 0``."""
    sys.stdout.write(allow_envelope(context) + "\n")
    sys.stdout.flush()
    sys.exit(0)


def deny(reason: str, summary: str | None = None) -> None:
    """Emit deny envelope to stdout and ``exit 0`` (never exit 2).

    ``summary`` rides in ``systemMessage`` (the transcript banner);
    ``reason`` rides in ``permissionDecisionReason`` (the model-facing
    feedback). See :func:`deny_envelope`.
    """
    sys.stdout.write(deny_envelope(reason, summary) + "\n")
    sys.stdout.flush()
    sys.exit(0)


def warn(message: str) -> None:
    """Emit warn envelope (allow + additionalContext) and ``exit 0``."""
    sys.stdout.write(warn_envelope(message) + "\n")
    sys.stdout.flush()
    sys.exit(0)


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def parse_stdin(raw: str) -> dict[str, Any]:
    """Parse the Claude Code hook payload; return ``{}`` on any failure."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def get_bash_command(payload: dict[str, Any]) -> str:
    """Extract the Bash ``command`` field; ``""`` if not a Bash call."""
    if payload.get("tool_name") != "Bash":
        return ""
    tool_input = payload.get("tool_input") or {}
    cmd = tool_input.get("command") if isinstance(tool_input, dict) else ""
    return cmd if isinstance(cmd, str) else ""


# ---------------------------------------------------------------------------
# Escape token
# ---------------------------------------------------------------------------

_ESCAPE_RE = re.compile(
    r"#\s*routing-allow\s*:\s*(?P<reason>.+?)\s*$",
    re.MULTILINE,
)


def extract_escape_reason(command: str) -> str:
    """Return the ``# routing-allow:`` reason text, or ``""`` when absent.

    nexus-mzvwa.9: the reason trails the command, so the 200-char
    ``command_fragment`` cap in :func:`log_routing_event` routinely cut
    it — making escape reasons un-auditable from the log. Callers pass
    this as the dedicated ``escape_reason`` field instead.
    """
    if not command or ESCAPE_TOKEN not in command:
        return ""
    match = _ESCAPE_RE.search(command)
    return match.group("reason").strip() if match else ""


def should_skip_for_reason(command: str) -> bool:
    """Return True iff ``command`` carries a valid ``# routing-allow:`` escape.

    Valid means: token present and the trailing reason text is at least
    ``ESCAPE_REASON_MIN_LENGTH`` characters after stripping whitespace.
    """
    if not command or ESCAPE_TOKEN not in command:
        return False
    match = _ESCAPE_RE.search(command)
    if not match:
        return False
    reason = match.group("reason").strip()
    return len(reason) >= ESCAPE_REASON_MIN_LENGTH


def degraded_token_variants(segment: str) -> list[list[str]]:
    """Rough tokenizations of a segment ``shlex`` rejected for unbalanced
    quoting (nexus-2e874). The old ``except ValueError: continue`` silently
    DROPPED the whole segment, so a single stray quote anywhere in a gated
    command fully bypassed the guard (``git push origin main
    --receive-pack="x`` was ALLOWed with zero warning).

    Two variants, because neither alone keeps every anchor visible
    (review Important-1): quote-chars-as-whitespace keeps a quote glued to
    a token BOUNDARY splitting (``--receive-pack="x`` -> ``--receive-pack=``,
    ``x``), while quote-chars-removed keeps a quote INSIDE a verb from
    fracturing it (``gi"t push`` -> ``git``, ``push``). Callers must treat a
    match in EITHER variant as a match — the safe, over-inclusive
    direction; only quoting fidelity inside VALUES is lost.
    """
    blanked = segment.replace('"', " ").replace("'", " ").split()
    stripped = segment.replace('"', "").replace("'", "").split()
    return [blanked] if blanked == stripped else [blanked, stripped]


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


def _log_path() -> pathlib.Path:
    override = os.environ.get("NX_ROUTING_LOG_PATH")
    return pathlib.Path(override) if override else _default_log_path()


#: Byte cap that triggers rotation (~1 MiB) -- same design and constant
#: as ``nexus._session_end_census``'s capability-census log; see that
#: module's docstring for the full "rotation, not trim-in-place" rationale
#: (a read-modify-write races the many concurrent routing-hook processes
#: that append to this exact file, one per gated tool call, across
#: however many Claude Code sessions are live at once -- worse than the
#: once-per-SessionEnd census log this pattern was first written for). A
#: byte ``stat()`` is O(1); a line count would require reading the whole
#: file on every single hook invocation.
_ROUTING_LOG_ROTATION_MAX_BYTES = 1_048_576

if sys.platform == "win32":
    import msvcrt as _msvcrt
else:
    import fcntl as _fcntl


def _lock_file(file_obj: Any, *, blocking: bool) -> None:
    """Exclusive lock on an already-open regular file. Cross-platform:
    ``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows. Deliberately
    self-contained (duplicates ``nexus._locking.lock_file``'s logic
    rather than importing it) -- this script has no dependency on the
    ``nexus`` package being installed/importable at all, by design (no
    other routing script under ``conexus/hooks/scripts/`` imports it
    either). Raises ``BlockingIOError`` if ``blocking=False`` and the
    lock is contended.
    """
    fd = file_obj.fileno()
    if sys.platform == "win32":
        if blocking:
            while True:
                try:
                    _msvcrt.locking(fd, _msvcrt.LK_LOCK, 1)
                    return
                except OSError:
                    time.sleep(0.5)
        else:
            try:
                _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
            except OSError as e:
                raise BlockingIOError(str(e)) from e
    else:
        flag = _fcntl.LOCK_EX if blocking else _fcntl.LOCK_EX | _fcntl.LOCK_NB
        _fcntl.flock(fd, flag)


def _unlock_file(file_obj: Any) -> None:
    """Release a lock acquired by :func:`_lock_file`."""
    fd = file_obj.fileno()
    if sys.platform == "win32":
        try:
            _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        _fcntl.flock(fd, _fcntl.LOCK_UN)


def _rotate_log_if_oversized(log_path: pathlib.Path) -> None:
    """Rotate ``log_path`` to ``<name>.1`` via atomic rename if it has
    grown past :data:`_ROUTING_LOG_ROTATION_MAX_BYTES`.

    ROTATION, NOT TRIM-IN-PLACE: rewriting the file in place to keep only
    the newest N lines is a foot-cannon here specifically -- MANY routing
    hook processes append to this file concurrently (one per gated
    Bash/Agent/git command), so a read-modify-write can clobber another
    process's line-atomic append mid-flight, and a crash partway through
    the rewrite loses the file outright. ``os.replace`` is atomic on
    POSIX: at every instant the path either names the pre-rotation file
    or nothing, never a half-written intermediate. Do NOT "simplify"
    this back into a rewrite.

    Exactly one older generation is retained -- any existing ``.1`` is
    CLOBBERED, never pushed to ``.2``, bounding total on-disk size at
    roughly 2x the cap ONCE rotation has run at least once. The FIRST
    rotation is an exception to that bound: a pre-existing file already
    over the cap when this code first ships (this project's own real
    ``routing_log.jsonl``, observed at ~5MB against a 1 MiB cap) lands in
    ``.1`` WHOLE, not truncated to the cap -- and persists at that size
    until the NEXT rotation clobbers it. Steady-state is bounded; the
    one-time first-rotation transient is not.

    TOCTOU DOUBLE-ROTATION CLOBBER (code-review Critical, nexus-g3jw6,
    fix pass 2026-08-20) -- why this function takes a lock at all: two
    concurrent rotators, P1 and P2, can BOTH observe the file oversize
    (P2's observation stale by the time it acts). P1 rotates the real
    history into ``.1`` and reappends a small live file. If P2 then
    blindly replays its stale decision, its rename SUCCEEDS AGAIN (the
    live path exists again) and CLOBBERS P1's real ``.1`` with P1's
    small reappended content -- silent, irreversible history loss, not
    the benign "someone else already rotated it" FileNotFoundError case
    below. FIX: a non-blocking advisory lock on a sidecar
    ``<name>.rotate.lock`` serializes the {re-stat, os.replace} critical
    section across rotators. Appends stay completely lock-free
    (unchanged, single ``fh.write()`` in ``log_routing_event``) -- only
    the RARE rotation path pays any lock cost, and only once oversize
    was observed at all. Losing the lock race (``BlockingIOError``)
    means someone else is rotating right now -- skip entirely, do not
    block or retry. Winning the lock means re-stat under it: if the file
    is no longer oversize, skip -- the earlier, unlocked stat() that
    triggered this call may be stale, but the DECISION to actually
    rename is always made with fresh data. This eliminates the
    stale-observation rename by construction, not merely narrows its
    window -- a bare re-stat immediately before ``os.replace`` WITHOUT
    the lock would still let two rotators interleave their re-stat and
    replace calls.

    A concurrent rotation race that manifests as ``FileNotFoundError``
    on the rename itself (another process's rotation completed between
    OUR re-stat-under-the-lock and OUR own replace -- possible only if
    that other process is not participating in this same lock) is still
    tolerated silently: the file is rotated either way. Any OTHER
    failure propagates to the caller, which is responsible for keeping
    rotation failure from blocking the append (see ``log_routing_event``).
    """
    try:
        size = log_path.stat().st_size
    except FileNotFoundError:
        return  # nothing to rotate

    if size < _ROUTING_LOG_ROTATION_MAX_BYTES:
        return  # cheap, lock-free common case: not even apparently oversize

    # Apparently oversize (per a possibly-stale stat()) -- escalate to the
    # serialized, re-checked critical section.
    lock_path = log_path.with_name(log_path.name + ".rotate.lock")
    try:
        lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return  # can't even open the lockfile -- best-effort, skip rotation
    lock_file_obj = os.fdopen(lock_fd, "r+")
    try:
        try:
            _lock_file(lock_file_obj, blocking=False)
        except BlockingIOError:
            # Someone else is inside the rotation critical section right
            # now -- skip entirely rather than wait or race them.
            return

        # RE-CHECK under the lock: the stat() above may be stale.
        try:
            size = log_path.stat().st_size
        except FileNotFoundError:
            return  # nothing left to rotate
        if size < _ROUTING_LOG_ROTATION_MAX_BYTES:
            return  # a fresher rotator already handled it

        rotated = log_path.with_name(log_path.name + ".1")
        try:
            os.replace(log_path, rotated)
        except FileNotFoundError:
            # Another (non-participating) process removed/rotated the
            # file between our re-stat and our rename -- rotated either
            # way.
            pass
    finally:
        try:
            _unlock_file(lock_file_obj)
        except OSError:
            pass
        lock_file_obj.close()


#: Matches ServiceRegistry's tier name for the shared nexus-service engine
#: (src/nexus/daemon/service_registry.py TIER_TTLS) -- the lease file is
#: ``<config_dir>/storage_service_addr.<uid>``. Mirrors
#: ``conexus/hooks/scripts/t2_prefix_scan.py``'s identical constant.
_STORAGE_SERVICE_TIER = "storage_service"

#: nexus-znvjd: the client's cross-process DATA-token lease, written by
#: ``nexus.db.data_token.DataTokenManager._write_lease`` at
#: ``<config_dir>/data_token_lease.<sha256(host[:port]\x00tenant)>``.
#: Mirrors ``t2_prefix_scan.py``'s identical constants.
_DATA_TOKEN_LEASE_PREFIX = "data_token_lease."
_DATA_TOKEN_LEASE_FORMAT_VERSION = 1


def _default_config_dir() -> pathlib.Path:
    """Stdlib-only mirror of ``nexus.config.nexus_config_dir`` (same
    resolution ``conexus/hooks/scripts/t2_prefix_scan.py``'s identically-
    named function uses)."""
    config_dir = os.environ.get("NEXUS_CONFIG_DIR") or os.environ.get("NX_CONFIG_DIR")
    if config_dir:
        return pathlib.Path(config_dir)
    return pathlib.Path.home() / ".config" / "nexus"


def _read_service_lease(config_dir: pathlib.Path) -> dict | None:
    """Best-effort read of the local supervisor's ServiceRegistry lease.

    Ported verbatim (nexus-gjv9b PART 2 CRITICAL review fix) from
    ``conexus/hooks/scripts/t2_prefix_scan.py``'s ``_read_lease`` --
    see that function's own docstring for the full design rationale
    (this hook cannot import ``nexus.daemon.service_registry`` either,
    RDR-121 § Contract mirroring nexus-vg6d4's identical constraint for
    the T2 prefix scan). ``tests/test_routing_hooks.py``'s
    ``test_parity_read_service_lease_*`` suite (nexus-gjv9b review
    fold-in round 3, code-review item 2 -- this docstring claimed the
    suite before it existed) runs BOTH implementations against the SAME
    on-disk lease fixture and asserts identical return values across
    fresh/expired/malformed/missing -- edit both functions, or edit one
    and let the parity test catch the drift. A source-level byte-diff
    would false-positive: this file uses ``import pathlib`` /
    ``pathlib.Path``, ``t2_prefix_scan.py`` uses ``from pathlib import
    Path`` -- behavioral parity is the actual contract, not textual
    identity.
    """
    path = config_dir / f"{_STORAGE_SERVICE_TIER}_addr.{os.getuid()}"
    try:
        data = json.loads(path.read_text())
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
    if port <= 0 or not token:
        return None
    if (time.time() - heartbeat_epoch) >= ttl:
        return None
    return {"host": host, "port": port, "token": token}


def _read_data_token_lease(config_dir: pathlib.Path, base_url: str) -> str | None:
    """Best-effort read of the client's cached DATA token for *base_url*.

    Ported verbatim (nexus-gjv9b PART 2 CRITICAL review fix) from
    ``t2_prefix_scan.py``'s identically-named function -- same
    format-version check, same digest rule, same fail-safe stance.
    ``tests/test_routing_hooks.py``'s ``test_parity_read_data_token_
    lease_*`` suite runs both against the same on-disk lease fixture
    (fresh match, wrong digest, expired, missing) and asserts identical
    return values.
    """
    import hashlib  # noqa: PLC0415 — stdlib, only needed on this path
    import urllib.parse  # noqa: PLC0415 — stdlib, only needed on this path

    host = urllib.parse.urlsplit(base_url).netloc or base_url
    now = time.time()
    best_token, best_expiry = "", 0.0
    try:
        candidates = sorted(config_dir.glob(f"{_DATA_TOKEN_LEASE_PREFIX}*"))
    except OSError:
        return None
    for path in candidates:
        try:
            data = json.loads(path.read_text())
            if data.get("format_version") != _DATA_TOKEN_LEASE_FORMAT_VERSION:
                continue
            tenant = str(data["tenant"])
            digest = hashlib.sha256(f"{host}\x00{tenant}".encode("utf-8")).hexdigest()
            if data.get("base_url_digest") != digest:
                continue
            token = str(data["token"])
            expires_at = float(data["expires_at"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if token and expires_at > now and expires_at > best_expiry:
            best_token, best_expiry = token, expires_at
    return best_token or None


def _read_config_yml_credentials(config_dir: pathlib.Path) -> dict:
    """Bounded, stdlib-only extraction of ``service_url``/``service_token``
    from the persisted ``config.yml``.

    Ported verbatim (nexus-gjv9b PART 2 CRITICAL review fix) from
    ``t2_prefix_scan.py``'s identically-named function -- see that
    docstring for the full "why a line-oriented scan, not a YAML parser"
    rationale. Returns ``{}`` when the file is absent, unreadable, or has
    no ``credentials:`` block. ``tests/test_routing_hooks.py``'s
    ``test_parity_read_config_yml_credentials_*`` suite runs both
    against the same on-disk ``config.yml`` fixture and asserts
    identical return values.
    """
    path = config_dir / "config.yml"
    try:
        text = path.read_text()
    except OSError:
        return {}

    result: dict = {}
    in_credentials = False
    cred_indent = 0
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if not in_credentials:
            if stripped == "credentials:":
                in_credentials = True
                cred_indent = indent
            continue
        if indent <= cred_indent:
            break
        for key in ("service_url", "service_token"):
            prefix = f"{key}:"
            if not stripped.startswith(prefix):
                continue
            value = stripped[len(prefix):].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            if value:
                result[key] = value
    return result


def _engine_endpoint() -> "tuple[str, str] | tuple[None, None]":
    """``(base_url, token)`` for the nexus-service engine, or
    ``(None, None)`` when nothing is resolvable. Never raises.

    FULL RESOLUTION (nexus-gjv9b PART 2 CRITICAL review fix, replacing
    an env-only leg that left the routing hooks write-never on any
    normal interactive install -- nothing exports ``NX_SERVICE_HOST``/
    ``PORT``/``TOKEN`` into a Claude Code process by default). Reuses
    ``t2_prefix_scan.py``'s stdlib-only discovery: a fresh
    ``ServiceRegistry`` lease file, then ``config.yml`` credentials, then
    the ``NX_SERVICE_*`` env vars, with a data-token lease (nexus-znvjd)
    preferred over any static token once a base URL is known. Mirrors
    ``nexus.db.service_endpoint.resolve_service_endpoint``'s precedence
    exactly, minus the raise-on-failure (this caller wants a quiet
    ``(None, None)`` to fall through to the metered drop, never an
    exception to handle).

    Precedence, matching ``t2_prefix_scan._resolve_endpoint``:
      1. ``service_url`` -- ``NX_SERVICE_URL`` env, else ``config.yml``'s
         ``service_url``. Token: ``NX_SERVICE_TOKEN`` env, else
         ``config.yml``'s ``service_token``, else the lease's token.
      2. ``NX_SERVICE_HOST``/``PORT`` env (+ ``NX_SERVICE_TOKEN``, else
         the lease's token).
      3. The bare local-supervisor lease alone.
    On every leg, a fresh data-token lease for the resolved host wins
    over whatever static token was found.
    """
    config_dir = _default_config_dir()
    lease = _read_service_lease(config_dir)
    yaml_creds = _read_config_yml_credentials(config_dir)

    url = os.environ.get("NX_SERVICE_URL", "").strip().rstrip("/")
    if not url:
        url = yaml_creds.get("service_url", "").strip().rstrip("/")
    if url:
        data_token = _read_data_token_lease(config_dir, url)
        if data_token:
            return url, data_token
        token = os.environ.get("NX_SERVICE_TOKEN", "").strip()
        if not token:
            token = yaml_creds.get("service_token", "").strip()
        if not token:
            token = lease["token"] if lease else ""
        if not token:
            return None, None
        return url, token

    port_str = os.environ.get("NX_SERVICE_PORT", "").strip()
    if port_str:
        try:
            port = int(port_str)
        except ValueError:
            return None, None
        host = os.environ.get("NX_SERVICE_HOST", "").strip() or "127.0.0.1"
        url = f"http://{host}:{port}"
        data_token = _read_data_token_lease(config_dir, url)
        if data_token:
            return url, data_token
        token = os.environ.get("NX_SERVICE_TOKEN", "").strip() or (
            lease["token"] if lease else ""
        )
        if not token:
            return None, None
        return url, token

    if lease is not None:
        url = f"http://{lease['host']}:{lease['port']}"
        data_token = _read_data_token_lease(config_dir, url)
        if data_token:
            return url, data_token
        return url, lease["token"]

    return None, None


#: Mirrors ``nexus.db.t2.http_telemetry_store.DEFAULT_TENANT`` verbatim --
#: hardcoded, not imported, because this script has no ``nexus`` import.
_DEFAULT_TENANT = "default"


def _post_routing_event_http(record: dict, *, timeout: float = 0.25) -> str:
    """Best-effort ``POST /v1/telemetry/routing_events/record`` via
    ``urllib`` (no ``httpx``/``requests`` dependency — this script runs
    under the system interpreter, RDR-121 § Contract). Returns ``""`` on
    a 2xx response, else a short CAUSE string classifying the failure —
    never raises.

    Cause vocabulary (nexus-gjv9b review fold-in round 3, critique
    CRITICAL 1/2 and code-review item 1): ``"unresolvable"`` (no
    endpoint/credential at all — :func:`_engine_endpoint` returned
    ``(None, None)``), ``"401"``/``"403"``/``"5xx"``/``"http_<code>"``
    (a non-2xx response, read straight from the raised
    ``urllib.error.HTTPError.code`` — never guessed from response text),
    ``"timeout"`` (a connect/read timeout), ``"connect"`` (any other
    transport-level failure — DNS, connection refused, TLS), ``"other"``
    for anything unrecognized. Classifying HERE, at the transport layer
    that actually knows the failure mode, is strictly more reliable than
    :func:`nexus.dropped_writes.classify_drop_cause`'s text-matching
    fallback (which exists for producers, like
    ``_session_end_census._post_capability_census``, that only have an
    exception's ``str()`` to work with) — this is why the cause travels
    through to :func:`_record_dropped_routing_event` explicitly rather
    than being re-derived from the error string on the far side.
    """
    base_url, token = _engine_endpoint()
    if base_url is None:
        return "unresolvable"
    try:
        import urllib.error  # noqa: PLC0415 — stdlib, only needed on this path
        import urllib.request  # noqa: PLC0415 — stdlib, only needed on this path

        body = json.dumps(record).encode("utf-8")
        req = urllib.request.Request(
            base_url + "/v1/telemetry/routing_events/record",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Nexus-Tenant": _DEFAULT_TENANT,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed internal engine URL, not user input
            if 200 <= resp.status < 300:
                return ""
            return f"http_{resp.status}"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return str(exc.code)
        if 500 <= exc.code < 600:
            return "5xx"
        return f"http_{exc.code}"
    except TimeoutError:
        # Raw socket.timeout (an alias of TimeoutError since Python 3.10),
        # raised directly by urlopen on a connect-phase timeout.
        return "timeout"
    except urllib.error.URLError as exc:
        # A read-phase (post-connect) timeout arrives wrapped here, with
        # .reason carrying the underlying TimeoutError -- everything else
        # (connection refused, DNS failure, TLS) is a genuine "connect".
        if isinstance(exc.reason, TimeoutError):
            return "timeout"
        return "connect"
    except Exception:  # noqa: BLE001 — boundary: anything else recognized as failed but not classifiable further
        return "other"


def _record_dropped_routing_event(error: str, *, cause: str = "") -> None:
    """Metered-drop fallback (nexus-gjv9b PART 2 design decision): a
    routing event that could not reach the engine is counted, not
    silently discarded and not appended to ``routing_log.jsonl`` either
    (that JSONL machinery stays in place for PART 3's deferred deletion
    only -- see this module's docstring). Hand-replicates
    ``nexus.dropped_writes.record_drop``'s exact on-disk record shape,
    ``cause`` field included (never imported -- no ``nexus`` dependency
    here) so ``nx doctor``'s existing drop-meter aggregation (including
    its dominant-cause tally) picks these up with no changes of its own.
    """
    try:
        override = os.environ.get("NX_DROPPED_WRITES_LOG_PATH", "").strip()
        if override:
            path = pathlib.Path(override)
        else:
            cfg_override = os.environ.get("NEXUS_CONFIG_DIR", "").strip()
            base = pathlib.Path(cfg_override) if cfg_override else pathlib.Path.home() / ".config" / "nexus"
            path = base / "dropped_writes.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "hook": "routing_events",
            "collection": "",
            "rows": 1,
            "error": str(error)[:200],
            "cause": str(cause)[:32],
        }
        line = json.dumps(record, separators=(",", ":")) + "\n"
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        # The meter is itself best-effort; must never raise.
        pass


def log_routing_event(
    rule: str,
    outcome: str,
    *,
    tool_name: str = "",
    command_fragment: str = "",
    escape_reason: str = "",
    session_id: str = "",
) -> None:
    """Record one routing-hook event (nexus-gjv9b PART 2 writer swap: the
    engine's ``routing_events`` table, replacing the JSONL append below).
    Never raises; the hook's own exit code NEVER depends on this call.

    Best-effort, fire-and-forget: POSTs to the engine with a SHORT
    (~250ms) timeout via :func:`_post_routing_event_http`; on ANY
    failure (unresolvable endpoint -- no live supervisor lease, no
    config.yml credentials, no NX_SERVICE_* env, per
    :func:`_engine_endpoint` -- timeout, non-2xx), degrades to
    :func:`_record_dropped_routing_event`
    rather than a JSONL fallback (same design decision as
    ``nexus._session_end_census.write_session_capability_census`` for
    PART 1 — the routing hooks' own timeout budget has no room for a
    filesystem retry story either). The JSONL append machinery below
    (:func:`_log_path`, :func:`_rotate_log_if_oversized`) is UNCHANGED
    and has no caller from this function any more -- kept in place per
    this bead's PART 3, deferred until the plugin pin advances (a
    pre-swap install is a legacy JSONL writer this machinery still
    protects in the interim).
    """
    try:
        record: dict = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "rule": rule,
            "outcome": outcome,
        }
        if session_id:
            record["session_id"] = session_id
        if tool_name:
            record["tool_name"] = tool_name
        if command_fragment:
            # Cap fragment length so the wire payload stays small.
            record["command_fragment"] = command_fragment[:200]
        if escape_reason:
            # Dedicated field (nexus-mzvwa.9): the reason trails the command,
            # so the fragment cap above routinely truncated it away.
            record["escape_reason"] = escape_reason[:300]
        cause = _post_routing_event_http(record)
        if cause:
            _record_dropped_routing_event(
                f"routing_events POST failed: {cause}", cause=cause,
            )
    except Exception:
        # Telemetry must never crash a hook. Swallow.
        pass


# ---------------------------------------------------------------------------
# Top-level runner — wraps every hook entry point
# ---------------------------------------------------------------------------


def run_hook(
    body: Callable[[dict[str, Any]], None],
    *,
    fail_closed: bool = False,
    rule_name: str = "",
) -> None:
    """Execute ``body(payload)`` under the fail-open / fail-closed contract.

    ``body`` is responsible for calling ``allow()`` / ``deny()`` /
    ``warn()`` itself; those calls ``sys.exit(0)``. If ``body`` returns
    normally without emitting an envelope, we fall through to a default
    allow. If ``body`` raises ``SystemExit`` (from our own emitters), we
    re-raise — that is the normal path. Any other exception triggers
    the fail-open / fail-closed branch.
    """
    raw = ""
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""

    payload = parse_stdin(raw)

    try:
        body(payload)
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001
        if fail_closed:
            log_routing_event(
                rule=rule_name or "unknown",
                outcome="deny_fail_closed",
                tool_name=payload.get("tool_name", "") or "",
                session_id=payload.get("session_id", "") or "",
            )
            deny(f"cannot verify, fail-closed: {exc}")
        else:
            log_routing_event(
                rule=rule_name or "unknown",
                outcome="allow_fail_open",
                tool_name=payload.get("tool_name", "") or "",
                session_id=payload.get("session_id", "") or "",
            )
            allow()

    # Body returned without emitting — default allow.
    allow()
