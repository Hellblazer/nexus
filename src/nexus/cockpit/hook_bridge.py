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

- **PermissionRequest needs explicit allow**: unlike PreToolUse, a
  PermissionRequest hook that emits no output may be treated as a block.
  The bridge emits a transparent allow for PermissionRequest.

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

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import structlog

if TYPE_CHECKING:
    from nexus.tuplespace.index import TupleIndex
    from nexus.tuplespace.registry import Registry

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Plugin/wheel compatibility protocol (nexus-yeu8)
# ---------------------------------------------------------------------------

#: Bridge public-API protocol version.
#:
#: The nx plugin ships seven ``orb_bridge_*.py`` scripts that import this
#: module from the installed ``conexus`` wheel. Plugin and wheel can drift
#: in version (user upgrades one but not the other). Each script embeds an
#: ``EXPECTED_BRIDGE_API_VERSION`` literal and calls
#: :func:`check_bridge_api_version` before doing any hook work. On mismatch
#: the helper logs ``hook_bridge_version_mismatch`` at ERROR level and the
#: script exits 0 so the user's tool flow is not disrupted.
#:
#: Bump policy: increment ``BRIDGE_API_VERSION`` whenever the bridge's
#: public API changes in a way that the existing scripts cannot accommodate.
#: That includes (non-exhaustive):
#:
#: - incompatible signature change to :func:`emit` or :func:`output_for_hook`
#: - removal or rename of a public symbol the scripts import
#: - change in the contract for return values the scripts rely on
#:
#: Bumping this constant without also bumping the embedded
#: ``EXPECTED_BRIDGE_API_VERSION`` in all seven scripts will trip the
#: mismatch path on every hook fire by design: the wheel asserts the
#: protocol shape, and stale scripts are guaranteed to skip rather than
#: silently corrupt telemetry. The version is intentionally distinct from
#: the package ``__version__``; package versions may bump for reasons that
#: do not touch the bridge contract.
BRIDGE_API_VERSION: int = 1


def check_bridge_api_version(expected: int) -> bool:
    """Return True if the script's expected protocol matches the wheel.

    On mismatch, emits a ``hook_bridge_version_mismatch`` ERROR log with
    both versions and returns False. Callers (bridge scripts) should exit 0
    on False so a version skew between plugin and wheel does not crash the
    user's tool flow; the tuple is simply not written.

    Args:
        expected: The ``EXPECTED_BRIDGE_API_VERSION`` literal embedded in
            the calling script at plugin-author time.

    Returns:
        True on match, False on mismatch.
    """
    if expected == BRIDGE_API_VERSION:
        return True
    _log.error(
        "hook_bridge_version_mismatch",
        expected=expected,
        actual=BRIDGE_API_VERSION,
    )
    return False


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

# nexus-6s8v (RDR-112): daemon-mode routing is now the preferred path.
# ``emit()`` tries the daemon first; on connection failure it falls through
# to direct mode (own SQLite + chroma), and on a final failure it logs and
# skips so user-facing hooks never crash because the tuplespace is down.
_ROUTING_TBA = "daemon"

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
    if not os.environ.get("CLAUDECODE"):
        _log.debug("hook_bridge_skip_no_claudecode", hook_type=hook_type)
        return

    if _bridge_disabled():
        _log.debug("hook_bridge_skip_disabled", hook_type=hook_type)
        return

    routed = route_payload(hook_type, payload)
    if routed is None:
        _log.debug("hook_bridge_skip_unrouted", hook_type=hook_type)
        return

    subspace, dimensions, match_text = routed
    content = _build_content(hook_type, payload)

    try:
        if conn is not None and index is not None and registry is not None:
            # Test injection path (resources supplied — bypass routing)
            _direct_out(
                conn=conn,
                index=index,
                registry=registry,
                subspace=subspace,
                content=content,
                dimensions=dimensions,
                match_text=match_text or None,
            )
            route = "direct-injected"
        else:
            # Production routing: prefer daemon, fall back to direct, then skip.
            route = _emit_routed(
                subspace=subspace,
                content=content,
                dimensions=dimensions,
                match_text=match_text or None,
            )
        _log.debug(
            "hook_bridge_emitted",
            hook_type=hook_type,
            subspace=subspace,
            route=route,
        )
    except Exception:
        _log.exception("hook_bridge_emit_error", hook_type=hook_type, subspace=subspace)


def _emit_routed(
    *,
    subspace: str,
    content: str,
    dimensions: dict[str, Any],
    match_text: str | None,
) -> str:
    """Try daemon -> direct -> skip. Returns the chosen route as a string.

    Daemon path (preferred): discover the running T2 daemon and call its
    ``tuplespace.out`` RPC. If discovery fails or the RPC raises, fall
    back to direct mode. If direct mode also fails (e.g. corrupt
    ``tuples.db``, missing chroma dir) the exception propagates to
    ``emit``'s outer try/except, which logs and swallows it so the hook
    cannot crash user-facing tools.

    nexus-6s8v (RDR-112): with the daemon owning ``tuples.db``, direct
    mode runs the risk of WAL conflicts. Direct-mode fallback is kept
    as a defense-in-depth for environments without a daemon (e.g. early
    test runs); the regression test for daemon-mode routing
    (``tests/cockpit/test_hook_bridge_daemon_routing.py``) asserts that
    when a daemon is reachable the daemon path is taken.
    """
    if _ROUTING_TBA == "daemon":
        try:
            from nexus.daemon.discovery import find_t2_daemon  # noqa: PLC0415
            from nexus.daemon.t2_client import T2Client  # noqa: PLC0415

            info = find_t2_daemon()
            if info is not None:
                uds_path_str = info.get("uds_path") or ""
                uds_path = Path(uds_path_str) if uds_path_str else None
                if uds_path is not None and uds_path.exists():
                    client = T2Client(uds_path=uds_path)
                else:
                    client = T2Client(tcp_addr=(info["tcp_host"], info["tcp_port"]))
                try:
                    client.tuplespace.out(
                        subspace=subspace,
                        content=content,
                        dimensions=dimensions,
                        match_text=match_text,
                    )
                    return "daemon"
                finally:
                    client.close()
        except Exception as exc:
            _log.warning(
                "hook_bridge_daemon_route_failed_falling_back_to_direct",
                error=str(exc),
            )
    # Fallback (or _ROUTING_TBA == "direct"): direct-mode auto path.
    _emit_direct_auto(
        subspace=subspace,
        content=content,
        dimensions=dimensions,
        match_text=match_text,
    )
    return "direct"


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
    """
    import chromadb as _chromadb

    from nexus.config import load_config
    from nexus.tuplespace.index import TupleIndex
    from nexus.tuplespace.registry import Registry, default_builtin_dir
    from nexus.tuplespace.store import open_tuples_db

    cfg = load_config()
    nexus_dir = cfg.get("nexus_dir", "~/.config/nexus")
    db_path = Path(os.path.expanduser(f"{nexus_dir}/tuples.db"))
    chroma_dir = Path(os.path.expanduser(f"{nexus_dir}/chroma"))

    # Load registry from default builtin dir; also try the hooks subdir
    # where nexus-78mh will place the seven hook-event YAMLs.
    builtin = default_builtin_dir()
    registry = _load_registry_with_hooks(builtin)

    conn = open_tuples_db(db_path)
    conn.row_factory = sqlite3.Row

    chroma_client = _chromadb.PersistentClient(path=str(chroma_dir))
    index = TupleIndex.from_registry(registry, chroma_client)

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
    finally:
        conn.close()


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
    """Thin wrapper around api.out for easy mocking in tests.

    nexus-wf07: wraps the write in a tight retry loop (3 attempts, 50/100/200ms
    backoff) for SQLite ``OperationalError`` whose message indicates "locked"
    or "busy". Under daemon-mode WAL contention (RDR-112) the bridge and the
    daemon may compete for ``tuples.db``; without retry every contention drops
    a tuple silently. Non-locking ``OperationalError``s (e.g. malformed SQL)
    are not retried. Applies to both the daemon-fallback path (via
    ``_emit_direct_auto``) and the injected-test path.
    """
    from nexus.retry import _sqlite_with_retry
    from nexus.tuplespace.api import out

    return _sqlite_with_retry(
        out,
        conn=conn,
        index=index,
        registry=registry,
        subspace=subspace,
        content=content,
        dimensions=dimensions,
        match_text=match_text,
        ttl_seconds=ttl_seconds,
        event="hook_bridge_retried",
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_dimensions(hook_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Construct the dimension dict for a hook event tuple.

    Required dimensions: actor, session, project, timestamp.
    Optional: tool, workflow, intent, priority (only when non-empty).
    """
    session_id = payload.get("session_id", "unknown")
    cwd = payload.get("cwd", os.environ.get("CLAUDE_PROJECT_DIR", ""))
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
        dims["tool"] = tool_name

    # No additional optional dims for the other types at this stage
    # (workflow, intent, priority are reserved for future enrichment)

    return dims


# nexus-es13: ANSI CSI (Control Sequence Introducer) sequences and other
# C0/C1 control chars must not survive into match_text. The bridge embeds
# match_text via Voyage and surfaces it in cockpit panels; a malicious
# tool_input carrying ANSI cursor moves or terminal-control escapes would
# otherwise be displayed verbatim (log injection / panel scramble).
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CTRL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_match_text(s: str) -> str:
    """Strip ANSI escape sequences and disallowed control chars from *s*.

    Keeps printable text, ``\\t``, ``\\n``, ``\\r`` (which are legitimate
    whitespace), and strips:
      * ANSI CSI sequences (``ESC [ ... <letter>``) — cursor / colour moves.
      * ANSI OSC sequences (``ESC ] ... BEL`` or ``ESC \\``) — window-title
        manipulation and the like.
      * C0 control bytes 0x00..0x08, 0x0b, 0x0c, 0x0e..0x1f, plus DEL (0x7f).
        The kept whitespace bytes (TAB 0x09, LF 0x0a, CR 0x0d) are excluded
        from the strip class so multi-line tool input survives intact.

    Returned strings are safe to embed and to render in cockpit panels.
    """
    if not s:
        return s
    s = _ANSI_OSC_RE.sub("", s)
    s = _ANSI_CSI_RE.sub("", s)
    s = _CTRL_CHARS_RE.sub("", s)
    return s


def _build_match_text(hook_type: str, payload: dict[str, Any]) -> str:
    """Extract the semantic match text for a hook event tuple.

    The match text is what gets embedded for semantic search. Each hook
    type uses the most semantically rich field available. nexus-es13:
    control characters and ANSI escape sequences are stripped before
    returning so a malicious or careless tool_input cannot pollute log
    output or cockpit-panel rendering.
    """
    match hook_type:
        case "PreToolUse":
            tool = payload.get("tool_name", "")
            tool_input = payload.get("tool_input", {})
            if isinstance(tool_input, dict):
                input_summary = " ".join(str(v) for v in tool_input.values())
            else:
                input_summary = str(tool_input)
            raw = f"{tool} {input_summary}".strip()

        case "PostToolUse":
            tool = payload.get("tool_name", "")
            response = payload.get("tool_response", "")
            raw = f"{tool} {response}"[:512].strip()

        case "SubagentStop":
            raw = payload.get("last_assistant_message", "")

        case "UserPromptSubmit":
            raw = payload.get("prompt", "")

        case "Notification":
            raw = payload.get("message", "")

        case "Stop" | "StopFailure":
            raw = hook_type

        case "SessionStart" | "SessionEnd":
            cwd = payload.get("cwd", "")
            raw = f"{hook_type} {cwd}".strip()

        case _:
            raw = ""

    return _sanitize_match_text(raw)


_CONTENT_BYTE_CAP = 12000


def _bridge_disabled() -> bool:
    """Return True if NX_BRIDGE_DISABLE is set to a truthy value.

    Falsy tokens (bridge runs): unset, ``""``, ``"0"``, ``"false"``, ``"False"``.
    Any other non-empty value disables the bridge.

    This is the documented privacy opt-out (docs/tuplespace-env.md). It
    fires after the CLAUDECODE gate in ``emit()`` so it covers both the
    daemon and direct-fallback paths. ``output_for_hook()`` is *not*
    gated by this — disabling tuple emission must not break the hook
    protocol (e.g. PermissionRequest still needs a transparent-allow
    response).
    """
    val = os.environ.get("NX_BRIDGE_DISABLE", "").strip()
    return val not in ("", "0", "false", "False")


def _build_content(hook_type: str, payload: dict[str, Any]) -> str:
    """Build the tuple content field (stored verbatim, not embedded).

    Returns a JSON-parseable string. When the full JSON exceeds the byte
    cap, projects the payload down to a stable set of small fields with
    a ``_truncated: true`` marker so consumers always see valid JSON
    instead of a mid-string slice. The byte cap stays well under
    ``SAFE_CHUNK_BYTES = 12288``.
    """
    raw = json.dumps(payload, ensure_ascii=False)
    if len(raw.encode("utf-8")) <= _CONTENT_BYTE_CAP:
        return raw

    original_bytes = len(raw.encode("utf-8"))

    projection: dict[str, Any] = {
        "_truncated": True,
        "_original_bytes": original_bytes,
    }
    for key in ("session_id", "hook_event_name", "tool_name", "cwd", "permission_mode"):
        if key in payload:
            projection[key] = payload[key]
    projected = json.dumps(projection, ensure_ascii=False)
    if len(projected.encode("utf-8")) <= _CONTENT_BYTE_CAP:
        return projected

    # Last-resort: minimal envelope with just the event name. Bounded by
    # construction (event name is a short identifier), guaranteed to fit.
    minimal = {
        "_truncated": True,
        "_original_bytes": original_bytes,
        "hook_event_name": str(payload.get("hook_event_name", ""))[:64],
    }
    return json.dumps(minimal, ensure_ascii=False)
