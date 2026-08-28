# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""MCP startup notices: embedder advisory, stranded-install.

Originally RDR-126 P2 (nexus-bsjro): first-run T2-daemon install for MCP
startup. That install path is GONE — it retired with the T2 daemon itself
(nexus-i711w Stage 2 sub-stage B) and had already stopped firing on any
service-backed install (RDR-176 Phase 1). What remains here is the notice
plumbing that grew up around it:

- the RDR-144 P5b embedder advisory (``apply_embedder_notice``), and
- the nexus-gynt2 stranded-install redirect (``apply_stranded_notice``).

RDR-126 §3 SUPERSESSION (nexus-37jha, decided 2026-08-07): the first-run
banner (``BannerSpec`` / ``maybe_banner`` / ``queue_banner`` /
``deliver_pending_banner`` / ``apply_first_run_banner_instructions`` /
``install_banner_dispatch_hook``) was DELETED outright, option (a) of the
bead. It had been producer-less since the sub-stage B deletion above — the
banner is never queued in production, so ``apply_first_run_banner_instructions``
and ``install_banner_dispatch_hook`` at MCP startup were a no-op every boot.
The service topology now owns first-run via ``nx init`` (RDR-174); no
service-mode first-run banner exists, and re-establishing one is a NEW
capability (option (b), filed separately if the Desktop/plugin-first
onboarding story wants it — see ``docs/desktop-deployment.md`` § Minimum
Viable Validation). ``tests/test_first_run_banner.py`` was deleted with it.

``_first_run_marker_path`` / ``mark_shown`` SURVIVE: the marker file itself
is still consumed by ``nexus.daemon.installer.uninstall_daemon``, which
reports and removes a stale ``.mcp_first_run_complete`` left behind by a
pre-retirement install — a cleanup concern independent of who used to write
it live.

Side effects are silent on success; failures log a structured warning and
continue — a startup notice must never block MCP boot.
"""
from __future__ import annotations

from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)


# NO ensure_installed_and_running: the MCP first-run path (RDR-126 §2) existed to
# install the T2 daemon's autostart unit and shell out to
# ``nx daemon t2 ensure-running``. Both are retired with the T2 daemon itself
# (nexus-i711w Stage 2 sub-stage B). It was already a no-op wherever the storage
# backend was SERVICE (RDR-176 Phase 1 made it return before installing
# anything), which is the only backend Stage 2 leaves standing — so this removes
# a path that no longer fired, not a live capability.
#
# CONSEQUENCE: this was the sole producer of the RDR-126 §3 first-run banner.
# Its disposition was decided at nexus-37jha — see the module docstring's
# "RDR-126 §3 SUPERSESSION" note — and the banner subsystem is deleted.



# ── RDR-144 P5b: user-visible embedder notice ─────────────────────────────────


def embedder_startup_notice() -> str | None:
    """Return a one-line notice for a local-mode user whose active embedder is
    not what they (would) want, or ``None`` when nothing needs saying.

    Plugin / Claude-Desktop / Cowork-first users never run the Claude Code
    SessionStart hook, so the embedder advisory that ``nx doctor`` shows (P5a)
    never reaches them. The MCP server is their only channel, and it cannot
    print (stdout is JSON-RPC). The notice is delivered via the server
    ``instructions`` string instead (see :func:`apply_embedder_notice`).

    Reuses the single source of truth for the two states
    (:func:`nexus.health.local_embedder_advisory`): State 1 (default 384, no
    ``nx init`` choice) and State 2 (chose bge-768 but the ``[local]`` extra
    is missing, so the resolver silently fell back to 384). Cloud mode and a
    correctly-active bge return ``None``.
    """
    from nexus.config import is_local_mode, local_embed_model_choice  # noqa: PLC0415 — branch-local; only on local-mode advisory path

    if not is_local_mode():
        return None

    from nexus.db.local_ef import _resolve_local_model  # noqa: PLC0415 — branch-local; only reached in local mode
    from nexus.health import local_embedder_advisory  # noqa: PLC0415 — branch-local; only reached in local mode

    active = _resolve_local_model(warn=False)
    advisory = local_embedder_advisory(local_embed_model_choice(), active)
    if advisory is None:
        return None

    fix = advisory.fix_suggestions[0] if advisory.fix_suggestions else "run `nx init`"
    # Collapse to a single line — server instructions should stay compact.
    return f"nexus embedder: {advisory.detail}. {fix}".replace("\n", " ")


def apply_embedder_notice(server: object) -> bool:
    """Write the embedder notice (if any) into ``server``'s low-level MCP
    ``instructions`` so it reaches the client at ``initialize``.

    ``FastMCP.instructions`` is a read-only property; the writable surface is
    the low-level ``server._mcp_server.instructions`` (P5b spike). An existing
    instructions string is preserved (notice appended), never clobbered.

    Best-effort: a startup advisory must never break MCP boot, so any failure
    is logged at debug and returns ``False``.
    """
    try:
        notice = embedder_startup_notice()
        if notice is None:
            return False
        low = server._mcp_server  # type: ignore[attr-defined]
        existing = getattr(low, "instructions", None)
        low.instructions = f"{existing}\n\n{notice}" if existing else notice
        return True
    except Exception as exc:  # noqa: BLE001 — never block startup on an advisory
        _log.debug("embedder_notice_apply_failed", error=f"{type(exc).__name__}: {exc}")
        return False


def apply_stranded_notice(server: object) -> bool:
    """nexus-gynt2: surface the stranded-install two-hop redirect through
    the server ``instructions`` channel at ``initialize``.

    A structlog line alone is invisible to MCP-only users (Desktop /
    Cowork / plugin-first — the dominant post-upgrade path per
    nexus-4xgfy), and stdout is JSON-RPC, so the instructions string is
    the LOUD channel here — same mechanism as :func:`apply_embedder_notice`.
    Unlike the embedder advisory (single-channel on core, RDR-144 P5b —
    doubling a cosmetic notice is noise), this is the data-loss-shaped
    correctness class: BOTH servers apply it, so a hand-configured
    catalog-only client still hears it. Disarmed (a pure constant check)
    on every migration-capable release. Detection-only: the server still
    serves — ``nx init``'s refusal and ``nx doctor``'s fatal check carry
    the blocking surface.

    Best-effort on the DELIVERY path only: an instructions-write failure
    must never break MCP boot, but the detection result is logged at
    ERROR before any write is attempted, so a delivery failure never
    silences the finding entirely.
    """
    try:
        from nexus.config import detect_stranded_install_default  # noqa: PLC0415 — deferred import — near-zero while disarmed

        stranded = detect_stranded_install_default()
    except Exception:  # noqa: BLE001 — the detector must never break server startup
        _log.warning("stranded_install_check_failed", exc_info=True)
        return False
    if stranded is None:
        return False
    # nexus-z0idx follow-on: "detail", not "message" — stdlib logging
    # reserves "message" on LogRecord.
    _log.error("stranded_install_detected", detail=stranded.message)
    try:
        low = server._mcp_server  # type: ignore[attr-defined]  # noqa: SLF001 — the writable instructions surface (P5b spike)
        notice = f"nexus STRANDED INSTALL — relay this to the user verbatim: {stranded.message}"
        existing = getattr(low, "instructions", None)
        low.instructions = f"{existing}\n\n{notice}" if existing else notice
        return True
    except Exception as exc:  # noqa: BLE001 — never block startup on notice delivery (already logged at ERROR above)
        _log.warning("stranded_notice_apply_failed", error=f"{type(exc).__name__}: {exc}")
        return False


# ── RDR-126 §3: first-run banner — RETIRED (nexus-37jha) ──────────────────────
#
# BannerSpec / _PENDING_BANNER / maybe_banner / queue_banner /
# _clear_pending_banner / deliver_pending_banner /
# apply_first_run_banner_instructions / install_banner_dispatch_hook are
# DELETED — see the module docstring's "RDR-126 §3 SUPERSESSION" note. The
# marker-file utilities below survive: they are the stale-marker cleanup
# contract consumed by ``nexus.daemon.installer.uninstall_daemon``, not
# banner-delivery machinery.


def _first_run_marker_path() -> Path:
    """Location of the one-shot first-run marker. Lives under
    ``nexus_config_dir()`` so it honours ``NEXUS_CONFIG_DIR`` for tests
    and multi-profile installs.

    Nothing in production writes this marker any more (the banner that
    used to write it is retired — see the module docstring). It is
    resolved here solely so ``uninstall_daemon`` can detect and remove a
    stale marker left behind by a pre-retirement install.
    """
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — branch-local helper import

    return nexus_config_dir() / ".mcp_first_run_complete"


def mark_shown() -> None:
    """Create the first-run marker file. Idempotent; creates parent
    directories as needed.

    No production caller remains (the banner-delivery paths that used to
    call this at the end of a successful delivery are deleted — see the
    module docstring). Retained for ``tests/daemon/test_uninstall_daemon.py``,
    which uses it to plant a marker simulating a pre-retirement install's
    leftover state for the ``uninstall_daemon`` cleanup contract.
    """
    marker = _first_run_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch(exist_ok=True)
