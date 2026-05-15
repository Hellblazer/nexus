# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hook-to-tuple bridge: shared library for the seven orb_bridge_*.py scripts.

Implements RDR-111 §Step 2. Each Claude Code hook type maps to a distinct
tuplespace subspace. Hook scripts call ``emit()`` to post an event tuple and
``output_for_hook()`` to get the correct stdout for that hook type.

Key design decisions (backed by RDR-111 spike results):

- **PreToolUse is observe-only** (CA-8 spike, 2026-05-14): emitting
  ``permissionDecision`` is not needed — allow-wins regardless of hook
  registration order. The bridge writes its tuple as a side effect and
  emits no stdout, leaving permission decisions to the user's allowlist
  and other installed hooks.

- **PermissionRequest is NOT bridged here**: a separate Bash hook
  (``nx/hooks/scripts/auto-approve-nx-mcp.sh``) handles
  PermissionRequest. ``output_for_hook("PermissionRequest")`` still
  returns a transparent-allow string for any future caller, but no
  ``orb_bridge_permissionrequest.py`` script exists today.

- **Daemon-mode routing deferred** (nexus-pce1.6): ``emit()`` currently
  dispatches in ``direct`` mode only (opens tuples.db directly via
  ``api.out``). When the RDR-112 daemon ships a ``tuplespace.out`` RPC
  the bridge can route through ``T2Client.call("tuplespace.out", ...)``
  instead. The ``_ROUTING_TBA`` constant documents this deferral.

- **RF-5 gate**: all tuplespace side-effects are skipped when the
  ``CLAUDECODE`` environment variable is absent. The hook scripts still
  produce correct stdout (transparent allow where applicable) regardless
  of the gate -- RF-5 is about not contaminating non-Claude environments
  with tuples, not about silencing the output that Claude Code relies on.
"""

from __future__ import annotations

import atexit
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from nexus.tuplespace.index import TupleIndex
    from nexus.tuplespace.registry import Registry

_log = structlog.get_logger(__name__)

# Module-level lazy singletons for the direct-mode emit path. ChromaDB
# PersistentClient init is 200-500ms; opening it on every hook invocation
# (PostToolUse fires per Bash/Read/Write/Edit) eats most of the 5s hook
# timeout. We cache the (registry, conn, index) triple per (db_path, chroma_dir)
# key so subsequent invocations within the same process reuse them.
#
# Note: hook bridge scripts are short-lived subprocesses, so this caching
# helps when the same process emits multiple events (e.g. emit() called
# more than once before script exit). Across separate subprocess invocations
# the singleton resets — that is fine, the OS page cache keeps the chroma
# pages warm and the cost is bounded by the first invocation per process.
_singleton_lock = threading.Lock()
_singleton: dict[tuple[str, str], tuple[Any, Any, Any]] = {}
_cached_key: tuple[str, str] | None = None
_registry_load_failed_once = False
_atexit_registered = False


def _close_singleton_at_exit() -> None:
    """Best-effort cleanup of cached resources at interpreter shutdown.

    Hook bridge scripts are short-lived so SQLite WAL + chromadb's own
    shutdown handlers usually win the race, but importers that hold the
    module for the lifetime of a longer process (tests, future daemon
    routing) benefit from explicit close to flush any pending writes.
    Errors are swallowed: the interpreter is exiting either way.
    """
    with _singleton_lock:
        for conn, _index, _registry in _singleton.values():
            try:
                conn.close()
            except Exception:
                pass
        _singleton.clear()


def configure_logging_to_stderr() -> None:
    """Redirect structlog output to stderr.

    Hook scripts must call this before any logging so that structlog's output
    does not contaminate the stdout channel that Claude Code reads for hook
    decisions. Call once at script startup, before importing from
    nexus.cockpit.hook_bridge (or after -- structlog reconfigures lazily).
    """
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )

# TODO(nexus-pce1.6): emit() currently uses direct mode exclusively. When
# the RDR-112 daemon ships a ``tuplespace.out`` RPC, route via
# ``T2Client.call("tuplespace.out", ...)`` under ``NX_STORAGE_MODE=daemon``
# and reserve direct mode for the no-daemon fallback.

# ---------------------------------------------------------------------------
# Subspace routing table
# Canonical names from RDR-111 lines 387-393.
# ---------------------------------------------------------------------------

_SUBSPACE_MAP: dict[str, str] = {
    "PreToolUse": "hook_events/tool_call_intent",
    "PostToolUse": "hook_events/tool_call_completed",
    "SubagentStop": "hook_events/agent_completed",
    "Stop": "hook_events/assistant_turn_ended",
    "StopFailure": "hook_events/assistant_turn_ended",
    "UserPromptSubmit": "hook_events/user_prompt",
    "SessionStart": "hook_events/session_lifecycle",
    "SessionEnd": "hook_events/session_lifecycle",
    "Notification": "hook_events/notification",
    # PreCompact / PostCompact / SubagentStart intentionally excluded (RDR-111 §400)
}

# ---------------------------------------------------------------------------
# Transparent-allow output shapes (RF-2)
# ---------------------------------------------------------------------------

_PERMISSIONREQUEST_ALLOW = json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PermissionRequest",
        "permissionDecision": "allow",
    }
})

# Hooks with no stdout output (None means: don't write anything to stdout)
_NO_OUTPUT: frozenset[str] = frozenset({
    "PreToolUse",       # observe-only per CA-8 spike
    "PostToolUse",
    "Stop",
    "StopFailure",
    "SubagentStop",
    "UserPromptSubmit",
    "SessionStart",
    "SessionEnd",
    "Notification",
})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def route_payload(
    hook_type: str,
    payload: dict[str, Any],
) -> tuple[str, dict[str, Any], str] | None:
    """Map a hook payload to ``(subspace, dimensions, match_text)``.

    Returns ``None`` for hook types the bridge does not handle (PreCompact,
    PostCompact, SubagentStart, or any unknown type). The caller should
    silently skip emission in that case.

    The four required dimensions -- ``actor``, ``session``, ``project``,
    ``timestamp`` -- are always populated. Optional dimensions (``tool``,
    ``workflow``, ``intent``, ``priority``) are populated when the payload
    provides the relevant field.

    Args:
        hook_type: The CC hook type string (e.g. ``"PreToolUse"``).
        payload: The JSON object read from the hook's stdin.

    Returns:
        A 3-tuple ``(subspace, dimensions, match_text)`` or ``None``.
    """
    subspace = _SUBSPACE_MAP.get(hook_type)
    if subspace is None:
        return None

    dims = _build_dimensions(hook_type, payload)
    match_text = _build_match_text(hook_type, payload)
    return subspace, dims, match_text


def output_for_hook(hook_type: str) -> str | None:
    """Return the stdout string for a hook type, or None for silent hooks.

    Pure function -- not gated on CLAUDECODE. Callers (bridge scripts) are
    responsible for writing the returned string to stdout.

    Per CA-8 spike, PreToolUse is observe-only (no permissionDecision emitted).
    PermissionRequest emits transparent allow because silent output may block.

    Args:
        hook_type: The CC hook type string.

    Returns:
        A JSON string to write to stdout, or ``None`` for silent hooks.
    """
    if hook_type == "PermissionRequest":
        return _PERMISSIONREQUEST_ALLOW
    # All other hook types (including PreToolUse -- observe-only) emit nothing
    return None


def emit(
    hook_type: str,
    payload: dict[str, Any],
    *,
    conn: "sqlite3.Connection | None" = None,
    index: "TupleIndex | None" = None,
    registry: "Registry | None" = None,
) -> None:
    """Post a hook-event tuple to the tuplespace (if CLAUDECODE is set).

    RF-5: skips all side-effects when ``CLAUDECODE`` is not in the environment.
    This prevents the bridge from contaminating non-Claude shell sessions.

    When ``conn``, ``index``, and ``registry`` are all ``None`` (the default
    for bridge scripts), ``emit`` opens the tuplespace in direct mode using
    the default nexus config paths. This is the standard hook-script call path.

    When all three are provided (the test call path), the caller-supplied
    resources are used directly; no file-system or ChromaDB connections are
    opened.

    Daemon-mode routing (nexus-pce1.6 / _ROUTING_TBA): currently routes
    through direct mode only. When the RDR-112 ``tuplespace.out`` daemon RPC
    ships, this function will detect the daemon and route accordingly.

    Args:
        hook_type: The CC hook type string.
        payload: The JSON object read from the hook's stdin.
        conn: Open SQLite connection to tuples.db (test injection; None to auto-open).
        index: TupleIndex wrapping ChromaDB (test injection; None to auto-open).
        registry: Loaded Registry of subspace schemas (test injection; None to auto-load).
    """
    from nexus.tuplespace.api import SubspaceSchemaError  # noqa: PLC0415

    # NX_BRIDGE_DISABLE is the per-bridge escape hatch (clearing CLAUDECODE
    # would also disable Claude Code itself, which is rarely what a user
    # wants when temporarily disabling tuple emission for privacy /
    # debugging / perf testing).
    if os.environ.get("NX_BRIDGE_DISABLE"):
        _log.debug("hook_bridge_skip_disabled_env", hook_type=hook_type)
        return

    if not os.environ.get("CLAUDECODE"):
        _log.debug("hook_bridge_skip_no_claudecode", hook_type=hook_type)
        return

    routed = route_payload(hook_type, payload)
    if routed is None:
        _log.debug("hook_bridge_skip_unrouted", hook_type=hook_type)
        return

    subspace, dimensions, match_text = routed
    content = _build_content(hook_type, payload)

    try:
        if conn is not None and index is not None and registry is not None:
            # Test injection path
            _direct_out(
                conn=conn,
                index=index,
                registry=registry,
                subspace=subspace,
                content=content,
                dimensions=dimensions,
                match_text=match_text or None,
            )
        else:
            # Script self-initialization path
            _emit_direct_auto(
                subspace=subspace,
                content=content,
                dimensions=dimensions,
                match_text=match_text or None,
            )
        _log.debug(
            "hook_bridge_emitted",
            hook_type=hook_type,
            subspace=subspace,
        )
    except SubspaceSchemaError as exc:
        # Schema-violation drops are a data-correctness problem (per the
        # "no silent fallbacks for correctness" rule) — surface them under
        # a distinct event so they're filterable and alertable. The hook
        # script still exits 0 per the bridge contract.
        _log.error(
            "hook_bridge_schema_violation",
            hook_type=hook_type,
            subspace=subspace,
            error=str(exc),
            dimensions=list(dimensions.keys()),
        )
    except sqlite3.OperationalError as exc:
        # Transient: WAL contention, "database is locked". Once RDR-112
        # daemon mode ships, multi-writer contention becomes common;
        # categorise it under a distinct event so alerts don't fire on
        # every transient. No retry here — retry handling is deferred to
        # nexus-pce1.6's _chroma_with_retry wrap.
        _log.warning(
            "hook_bridge_transient",
            hook_type=hook_type,
            subspace=subspace,
            error=str(exc),
            error_type="OperationalError",
        )
    except (sqlite3.ProgrammingError, sqlite3.DatabaseError) as exc:
        # The cached singleton conn is poisoned — closed, or pointing at
        # a corrupted db. Invalidate so the next emit() rebuilds. Without
        # this, every subsequent hook fire in this process would raise
        # the same error.
        _invalidate_singleton()
        _log.error(
            "hook_bridge_singleton_poison",
            hook_type=hook_type,
            subspace=subspace,
            error=str(exc),
            error_type=type(exc).__name__,
            remediation=(
                "Cached SQLite connection or chroma client poisoned; "
                "singleton invalidated, next emit() will reinit."
            ),
        )
    except Exception:
        # Last-resort guard so a hook script never crashes Claude Code.
        # Specific exception classes that have known handling above this
        # point should NOT reach here; if they do, log with the traceback
        # for postmortem.
        _log.exception("hook_bridge_emit_error", hook_type=hook_type, subspace=subspace)


def _emit_direct_auto(
    *,
    subspace: str,
    content: str,
    dimensions: dict[str, Any],
    match_text: str | None,
) -> None:
    """Open tuplespace resources from default config paths and call out().

    This is the production path for bridge scripts. Opens tuples.db and
    ChromaDB directly (no daemon) using the nexus config. The registry
    is loaded from the default builtin dir, augmented with any hook-event
    YAML schemas in ``nx/tuplespace/builtin/hooks/`` if they exist.

    Resources are cached at module scope (per-process singleton) so repeated
    emit() calls within the same process do not re-pay the 200-500ms
    PersistentClient init cost.

    A failure to load the registry (e.g. wheel-install where
    ``default_builtin_dir()`` does not exist on disk) is logged once via a
    structured WARN so the silent-no-op condition is detectable.
    """
    from nexus.tuplespace.registry import UnknownSubspaceError

    resources = _get_or_init_resources()
    if resources is None:
        return  # registry unavailable — already logged
    conn, index, registry = resources

    try:
        _direct_out(
            conn=conn,
            index=index,
            registry=registry,
            subspace=subspace,
            content=content,
            dimensions=dimensions,
            match_text=match_text,
        )
    except UnknownSubspaceError:
        # In direct mode this is reachable only if the registry loaded but
        # is missing one of the seven hook subspace YAMLs (partial-deploy
        # state). Daemon-mode (nexus-pce1.6) will also hit this when the
        # bridge fires before nexus-78mh registers the schemas. Either way
        # we surface a structured WARN with a remediation hint instead of
        # an exception traceback.
        _log.warning(
            "hook_bridge_unknown_subspace",
            subspace=subspace,
            remediation=(
                "Subspace not in local registry (direct mode) or not yet "
                "registered with the daemon. Verify the hook YAMLs are "
                "installed; in daemon mode wait for nexus-78mh registration."
            ),
        )


def _get_or_init_resources() -> tuple[Any, Any, Any] | None:
    """Return cached (conn, index, registry) triple or initialise on first use.

    Returns ``None`` (and logs a structured WARN once) if registry load fails,
    which is the wheel-install silent-drop condition: ``default_builtin_dir()``
    is repo-relative and does not exist when conexus is installed from a wheel.

    Uses a double-checked-lock so the cached fast-path skips the
    ``_singleton_lock`` entirely once the resources are initialised.
    """
    global _registry_load_failed_once, _atexit_registered, _cached_key

    # Fast path: if the key has already been resolved once and the cache is
    # warm for it, skip the lock AND the load_config() disk read. nexus_dir
    # is stable across a process lifetime so the cached key is safe to reuse.
    if _cached_key is not None:
        cached = _singleton.get(_cached_key)
        if cached is not None:
            return cached

    import chromadb as _chromadb

    from nexus.config import load_config
    from nexus.tuplespace.index import TupleIndex
    from nexus.tuplespace.registry import RegistryError, default_builtin_dir
    from nexus.tuplespace.store import open_tuples_db

    cfg = load_config()
    nexus_dir = cfg.get("nexus_dir", "~/.config/nexus")
    db_path = Path(os.path.expanduser(f"{nexus_dir}/tuples.db"))
    chroma_dir = Path(os.path.expanduser(f"{nexus_dir}/chroma"))

    key = (str(db_path), str(chroma_dir))

    # Second fast-path check now that we have the freshly resolved key
    # (handles the first-call race where _cached_key wasn't populated yet).
    cached = _singleton.get(key)
    if cached is not None:
        _cached_key = key
        return cached

    with _singleton_lock:
        cached = _singleton.get(key)
        if cached is not None:
            _cached_key = key
            return cached

        builtin = default_builtin_dir()
        try:
            registry = _load_registry_with_hooks(builtin)
            conn = open_tuples_db(db_path)
            conn.row_factory = sqlite3.Row
            chroma_client = _chromadb.PersistentClient(path=str(chroma_dir))
            index = TupleIndex.from_registry(registry, chroma_client)
        except (RegistryError, FileNotFoundError, OSError, ValueError) as exc:
            # Covers the wheel-install silent-drop (RegistryLoadError /
            # FileNotFoundError), a corrupted chroma directory or bad
            # collection name (ValueError from chromadb), or a permission
            # problem opening tuples.db (OSError). All are reported under
            # the same structured event so observability stays consistent.
            if not _registry_load_failed_once:
                _log.warning(
                    "hook_bridge_registry_unavailable",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    builtin_dir=str(builtin),
                    remediation=(
                        "Registry YAMLs not found on disk or storage init "
                        "failed. For wheel installs, pass an explicit "
                        "builtin_dir or install from source."
                    ),
                )
                _registry_load_failed_once = True
            return None

        _singleton[key] = (conn, index, registry)
        _cached_key = key
        if not _atexit_registered:
            atexit.register(_close_singleton_at_exit)
            _atexit_registered = True
        return _singleton[key]


def _invalidate_singleton() -> None:
    """Drop the cached resources so the next emit() rebuilds.

    Called from `_emit_direct_auto` when a cached resource is poisoned
    (closed sqlite3.Connection, corrupted database). Unlike
    `_reset_singleton_for_tests` this preserves `_atexit_registered`
    (the atexit handler is per-process and still valid) and the
    `_registry_load_failed_once` flag (if the registry is genuinely
    unavailable we don't want a poison-driven invalidation to undo the
    one-shot WARN suppression).
    """
    global _cached_key
    with _singleton_lock:
        for conn, _, _ in _singleton.values():
            try:
                conn.close()
            except Exception:
                pass
        _singleton.clear()
        _cached_key = None


def _reset_singleton_for_tests() -> None:
    """Clear all module-level state. Test-only helper.

    Resets the resource cache, the once-per-process registry-load
    warning flag, AND the atexit-registered flag so the next emit()
    starts from a clean slate. Without the atexit reset, tests that
    run after a successful init would see "already registered" even
    though _singleton was cleared.
    """
    global _registry_load_failed_once, _atexit_registered, _cached_key
    with _singleton_lock:
        for conn, _, _ in _singleton.values():
            try:
                conn.close()
            except Exception:
                pass
        _singleton.clear()
        _cached_key = None
        _registry_load_failed_once = False
        _atexit_registered = False


def _load_registry_with_hooks(builtin_dir: Path) -> "Registry":
    """Load the registry, including the hook-event subdir.

    Delegates to ``Registry.load(builtin_dir, subdirs=("hooks",))`` so the
    hook-event YAMLs in ``<builtin_dir>/hooks/`` participate in the same
    duplicate-name guard and ``_compile_template`` flow as top-level YAMLs.
    No private-attribute access.
    """
    from nexus.tuplespace.registry import Registry

    return Registry.load(builtin_dir, subdirs=("hooks",))


def _direct_out(
    *,
    conn: sqlite3.Connection,
    index: "TupleIndex",
    registry: "Registry",
    subspace: str,
    content: str,
    dimensions: dict[str, Any],
    match_text: str | None = None,
    ttl_seconds: float | None = None,
) -> str:
    """Thin wrapper around api.out for easy mocking in tests."""
    from nexus.tuplespace.api import out

    return out(
        conn=conn,
        index=index,
        registry=registry,
        subspace=subspace,
        content=content,
        dimensions=dimensions,
        match_text=match_text,
        ttl_seconds=ttl_seconds,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_dimensions(hook_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Construct the dimension dict for a hook event tuple.

    Required dimensions: actor, session, project, timestamp.
    Optional: tool, workflow, intent, priority (only when non-empty).
    """
    # User-controlled strings are length-capped before storage. ChromaDB
    # accepts arbitrary-length metadata, but downstream renderers / log
    # parsers / dim-aware queries should not see multi-KiB strings. The
    # 512-byte cap is generous for legitimate paths/IDs.
    _DIM_VALUE_CAP = 512
    session_id = str(payload.get("session_id", "unknown"))[:_DIM_VALUE_CAP]
    cwd = str(payload.get("cwd", os.environ.get("CLAUDE_PROJECT_DIR", "")))[:_DIM_VALUE_CAP]
    # "actor" is the process/agent identifier -- use session_id as a stable key
    actor = session_id
    timestamp = str(time.time())

    dims: dict[str, Any] = {
        "actor": actor,
        "session": session_id,
        "project": cwd,
        "timestamp": timestamp,
    }

    # Optional dimensions -- populated per hook type
    tool_name = payload.get("tool_name")
    if tool_name and hook_type in ("PreToolUse", "PostToolUse"):
        dims["tool"] = str(tool_name)[:_DIM_VALUE_CAP]

    # hook_event_name is the canonical CC payload key. Populate it on
    # every dim dict, not only for the collapsed subspaces (Stop+StopFailure,
    # SessionStart+SessionEnd). Cheap to include, and it removes the entire
    # class of silent-drop bugs where a future schema marks the dim required
    # on a subspace whose hook type forgot to populate it. Named to match
    # the CC API field and avoid colliding with ``event_type`` reserved for
    # layout_state/<profile> subspaces (RDR-111 §Phase 2, line 519).
    dims["hook_event_name"] = hook_type

    # TODO(rdr-111-phase2): wire workflow / intent / priority dims when
    # the binding-reaction loop (nexus-7ncn) needs them for matching.

    return dims


def _build_match_text(hook_type: str, payload: dict[str, Any]) -> str:
    """Extract the semantic match text for a hook event tuple.

    The match text is what gets embedded for semantic search. Each hook
    type uses the most semantically rich field available.
    """
    match hook_type:
        case "PreToolUse":
            tool = payload.get("tool_name", "")
            tool_input = payload.get("tool_input", {})
            if isinstance(tool_input, dict):
                input_summary = " ".join(str(v) for v in tool_input.values())
            else:
                input_summary = str(tool_input)
            return f"{tool} {input_summary}".strip()

        case "PostToolUse":
            tool = payload.get("tool_name", "")
            response = payload.get("tool_response", "")
            return f"{tool} {response}"[:512].strip()

        case "SubagentStop":
            return payload.get("last_assistant_message", "")

        case "UserPromptSubmit":
            return payload.get("prompt", "")

        case "Notification":
            return payload.get("message", "")

        case "Stop" | "StopFailure":
            # Prefer per-turn content (last_assistant_message) when the
            # payload carries it; otherwise fall back to the bare hook
            # type. Earlier revisions embedded session_id+cwd, but those
            # are session-constants — embedding them at every turn produces
            # N near-identical vectors that collapse the subspace into one
            # cluster per session. The hook_event_name DIMENSION carries
            # the Stop/StopFailure variant for exact-match filtering.
            last_msg = payload.get("last_assistant_message", "")
            if last_msg:
                return f"{hook_type} {last_msg}"[:512].strip()
            return hook_type

        case "SessionStart" | "SessionEnd":
            # No per-event content is reliably available on these payloads;
            # the bare hook_event_name (in the dim) plus the literal hook
            # type as match_text is the honest answer. Avoid session-
            # constant string compounding that would cluster all session
            # boundaries together.
            return hook_type

        case _:
            return ""


def _build_content(hook_type: str, payload: dict[str, Any]) -> str:
    """Build the tuple content field (stored verbatim, not embedded).

    Serialises the raw payload as JSON, capped to ``SAFE_CHUNK_BYTES``
    (which is 4 KiB below ``MAX_DOCUMENT_BYTES``) so api.out's quota
    validator cannot reject. Truncation is BYTE-aware, not codepoint-
    aware: a payload of 4-byte codepoints (CJK supplementary, emoji)
    encoding to ~4x the codepoint count would otherwise blow through
    MAX_DOCUMENT_BYTES under a naive slice. We re-decode with errors=
    "ignore" so a multi-byte sequence cut mid-character does not produce
    invalid UTF-8.
    """
    from nexus.db.chroma_quotas import SAFE_CHUNK_BYTES  # noqa: PLC0415

    raw = json.dumps(payload, ensure_ascii=False)
    encoded = raw.encode("utf-8")
    if len(encoded) <= SAFE_CHUNK_BYTES:
        return raw
    return encoded[:SAFE_CHUNK_BYTES].decode("utf-8", errors="ignore")
