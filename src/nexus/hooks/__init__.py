# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""SessionStart and SessionEnd hook logic for Claude Code integration."""
from __future__ import annotations

import os
from pathlib import Path

# NEITHER ``structlog`` NOR ``nexus.session`` IS IMPORTED AT MODULE SCOPE,
# and that is load-bearing rather than tidy (bead nexus-q02nx.21).
#
# Every command-tier hook verb is a submodule of this package
# (``nexus.hooks.session_start_verb``, ``auto_approve``, ``post_compact``,
# ``rdr_verb`` and the rest), and Python runs a package's ``__init__``
# before any submodule. So an eager import here is paid by every verb
# dispatch whatever the verb itself imports. Measured on the dev Mac, 10
# runs, median: bare python 14 ms; the spine
# (``nexus._hook_runtime.entry``) 18 ms; a real verb 78 ms. ``structlog``
# and ``nexus.session`` cost about 81 ms each, and ``nexus.session`` pulls
# ``structlog`` itself, so deferring only one recovers nothing.
#
# That 78 ms is against the 40 ms ``_run_python_hook.sh`` path the command
# tier replaces, i.e. the port was a 2x latency REGRESSION on hooks that
# fire per Bash call. nexus-br31l carved out ``nexus._hook_runtime`` to fix
# exactly this and fixed only the spine;
# ``tests/hooks/test_hook_runtime_thin.py::test_a_stdlib_only_verb_dispatch_never_loads_structlog``
# dispatches a SYNTHETIC verb written into tmp_path, which is not in this
# package, so it was green throughout and structurally could not see it.
#
# Both deferrals are cheap where they land: all nine ``_logger()`` calls
# below are in ``except`` blocks, and the ``nexus.session`` pair is used
# only inside ``session_start``.


def _logger():
    """The package logger, imported on use.

    Same deferral ``nexus._hook_runtime._io`` uses, for the same reason:
    ``import structlog`` drags ``structlog.dev`` -> ``rich.traceback`` ->
    ``pygments`` -> an entry-point scan. Called only from error paths, so
    a healthy dispatch never pays it.
    """
    import structlog  # noqa: PLC0415 — deferred; see the comment above

    return structlog.get_logger()

# -- Helpers ------------------------------------------------------------------

def _open_t1():
    """Open the process's T1 store for the SessionEnd flush, never honoring
    the shared-scope escape hatch.

    nexus-6a19f: ``NX_T1_ALLOW_SHARED_FALLBACK=1`` (see
    :func:`nexus.db.t1.get_t1_database`) exists so a caller with no usable
    lease for an explicit session id can opt back into the shared
    CLI-dedicated scope for READING/WRITING markers (e.g.
    ``conexus/hooks/scripts/pre_close_verification_hook.sh``). It must
    NEVER extend to :func:`session_end_flush`'s ``t1.clear()`` call below --
    clearing the shared scope on every lease-less SessionEnd would wipe
    every OTHER session's markers accumulated there (the nexus-6a19f
    Significant-1 finding: this was a live, undocumented, destructive bug
    pre-f7xyq). Force the flag off for the duration of this call regardless
    of what the ambient environment carries, so an explicit-but-unleased
    session id always takes the fail-loud branch here -- ``t1`` stays
    ``None``, and the ``if t1 is not None: t1.clear()`` guard below never
    fires for it.
    """
    from nexus.db.t1 import get_t1_database  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    prev = os.environ.pop("NX_T1_ALLOW_SHARED_FALLBACK", None)
    try:
        return get_t1_database()
    finally:
        if prev is not None:
            os.environ["NX_T1_ALLOW_SHARED_FALLBACK"] = prev


def _t1_clear_if_owned(t1) -> None:
    """Clear ``t1`` only if this process can prove it owns the scope.

    nexus-65a9k / GH #1454: re-derives the tier-1/tier-2 T1 routing
    decision (:func:`nexus.db.t1.resolve_t1_routing_tiers`) immediately
    before the destructive call, rather than trusting whatever
    ``_open_t1()`` returned earlier. ``USE_LEASED`` means the store this
    process is holding is bound to a lease published by a DIFFERENT,
    still-live process (see :func:`session_end_flush`'s docstring for the
    tool-free-dispatch scenario that reaches this routinely) -- clearing
    it would delete that OTHER process's working memory, not this
    process's own. The clear is skipped in that case; every other
    decision (``USE_INHERITED``, or the ``MINT`` that produced the
    genuinely-bare CLI-dedicated store) is unchanged from before this fix
    and clears exactly as it always has.

    Fails loud, never silently defaults to clearing: if the ownership
    decision itself cannot be computed (an unexpected exception --
    config dir unreadable, lease file corrupt, etc.), that is logged at
    ERROR and the clear is skipped. A skipped clear only leaks rows to
    the 24h TTL sweep; a wrongly-executed clear is an unrecoverable
    delete of a live process's scope -- on doubt, do not delete.
    """
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred import; rare/branch-local path
    from nexus.db.t1 import T1RoutingAction, resolve_t1_routing_tiers  # noqa: PLC0415 — deferred import; rare/branch-local path

    try:
        decision = resolve_t1_routing_tiers(nexus_config_dir())
    except Exception as exc:  # noqa: BLE001 — ownership cannot be proven; must never default to clearing
        # nexus-z0idx follow-on: kwarg is "detail", not "message" — stdlib
        # logging's LogRecord reserves "message" (set internally by
        # getMessage()); passing message= as an extra kwarg raises
        # KeyError("Attempt to overwrite 'message' in LogRecord") the
        # moment structlog is stdlib-routed.
        _logger().error(
            "session_end_t1_ownership_check_failed",
            error=str(exc),
            detail=(
                "could not determine T1 ownership; skipping clear() to avoid "
                "deleting a scope this process may not own"
            ),
        )
        return

    if decision.action == T1RoutingAction.USE_LEASED:
        _logger().warning(
            "session_end_t1_clear_skipped_leased_scope",
            session_id=decision.session_id,
            detail=(
                "T1 resolved via a lease borrowed from another live process; "
                "skipping clear() -- rows age out via the 24h TTL sweep "
                "instead of being deleted immediately"
            ),
        )
        return

    t1.clear()


def _infer_repo() -> str:
    """Detect current repo name from git, or fall back to cwd name."""
    from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 — deferred: a hook process pays its import cost on every invocation, and a module-scope import of this pulls structlog + ~231 modules (measured on verification_config: 14ms/106 -> 62-84ms/337). Deferred, it is paid only when we actually spawn

    try:
        result = run_bounded(
            ["git", "rev-parse", "--show-toplevel"],
            check=True, timeout=10,
        )
        return Path(result.stdout.strip()).name
    except Exception as exc:  # noqa: BLE001 — best-effort; error surfaced via log/echo, must not crash caller
        _logger().debug("infer_repo_git_failed", error=str(exc))
        return Path.cwd().name


#: SessionStart ``source`` values that mean "the transcript now has a
#: DIFFERENT session id than whatever a live MCP server sibling last
#: sampled at spawn" (nexus-d76vc). ``startup`` spawns fresh MCP servers
#: (nothing frozen yet, nothing to hand off) and ``compact`` keeps the
#: SAME session id (no divergence) -- neither writes a marker. ``fork``
#: (``/branch`` and ``--fork-session``, Claude Code >= 2.1.213) mints a
#: new session id in the SAME process, so it needs the handoff exactly as
#: ``clear`` does; added by bead nexus-kdxyv (it was deferred at d76vc as
#: unsized scope) so the live MCP server's channel waiter and directory
#: lease follow the fork and the parent's mail stays with the parent
#: (RDR-208 Fork paragraph). The plugin's SessionStart matcher must name
#: ``fork`` for this source to reach here at all (conexus/hooks/hooks.json).
_T1_HANDOFF_SOURCES = frozenset({"clear", "resume", "fork"})


def _write_t1_handoff_markers(new_session_id: str) -> None:
    """nexus-d76vc: hand THIS conversation's new session id to any live
    MCP server sibling of the current claude process, so its frozen T1
    scope can follow across ``/clear``/``/resume`` instead of staying
    pinned to the pre-event session (the nexus-aj564 split-brain).

    Ancestry authentication (MUST-HOLD rn3wo.1) happens entirely via
    *selection*: :func:`~nexus.session.find_mcp_sibling_pids` only ever
    returns pids that are LIVE, IMMEDIATE children of this hook's own
    claude ancestor -- there is no separate "verify" step because a
    marker is never written for a pid that was not found this way, so a
    concurrent session's hook (walking its OWN, necessarily different,
    claude ancestor) can structurally never enumerate this session's MCP
    pids. The watcher side (``nexus.mcp.core``) independently re-derives
    its own ancestor and re-checks anyway -- defense in depth, never
    trusting the marker file alone from either direction.

    Best-effort: any failure (no `ps`, no siblings, disk error) is
    logged at debug and swallowed -- a SessionStart hook must never fail
    the session over a T1-scope convenience feature. Every non-T1 tool,
    and T1 itself via the frozen pre-existing scope, keeps working either
    way.
    """
    try:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred import; rare/branch-local path
        from nexus.daemon.t1_handoff import write_handoff_marker  # noqa: PLC0415 — deferred import; rare/branch-local path
        from nexus.session import (  # noqa: PLC0415 — deferred import; rare/branch-local path
            find_immediate_claude_pid,
            find_mcp_sibling_pids,
        )

        claude_pid = find_immediate_claude_pid()
        if claude_pid <= 0:
            return
        mcp_pids = find_mcp_sibling_pids(claude_pid)
        if not mcp_pids:
            return
        config_dir = nexus_config_dir()
        for mcp_pid in mcp_pids:
            write_handoff_marker(
                mcp_pid,
                new_session_id=new_session_id,
                claude_pid=claude_pid,
                config_dir=config_dir,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort; hook must never crash session-start over a T1-scope convenience feature
        _logger().debug("t1_handoff_marker_write_failed", error=str(exc))


def _write_tuple_watch_session_marker(new_session_id: str, source: str | None) -> None:
    """nexus-6konb.12 (MM-3.4 fix 1): record this conversation's current
    session id under this claude process's own marker file, so a reader
    that resolved an OLDER session id for the same conversation can tell
    it changed. Originally written for a live CLI watcher Monitor to check
    against its own spawn-time session id and self-stop on a mismatch
    rather than keep holding its per-address lock on a mailbox nobody
    watched any more; RDR-211 nexus-rplay.14 deleted that watcher, and the
    marker's remaining readers are ``nexus.tuple_directory.resolve_default_from``
    and this function's own ``record_clear_and_write_session_marker`` call
    below.

    Originally also replaced the deleted Monitor-arm instruction's old
    TaskStop rule, which could not work in the first place: the fresh
    conversation it would run in has no memory of the OLD Monitor's
    harness task id, so there was never a way for it to discover what to
    stop. This writes a marker a reader checks ITSELF instead, reusing the
    nexus-d76vc T1-handoff pattern immediately above: writer and
    reader independently derive the SAME claude ancestor pid via
    :func:`nexus.session.find_immediate_claude_pid`, from different vantage
    points, rather than passing it between them.

    Written on EVERY SessionStart source, unlike the T1 handoff marker. The
    marker is keyed by pid and nothing prunes it, and pids are reused: a new
    Claude process that inherited a dead one's pid would otherwise read that
    process's last marker, name a session it is not, and stop its own freshly
    armed watcher on the first cycle. Writing on ``startup`` (and on
    ``compact``, where the id is unchanged) overwrites whatever a previous
    owner of the pid left, so a marker always names the CURRENT process's
    session. The watcher still only stops on a mismatch, so a same-id write
    is a no-op for it.

    RDR-208 Phase 2 Step 3: on ``source == "clear"`` this also records the
    session a ``/clear`` just stranded, so
    :mod:`nexus.hooks.mailbox_drain` can empty that mailbox once
    (:func:`nexus.session_marker.record_clear_and_write_session_marker`). Never
    on an INHERITED session id (``NX_SESSION_ID`` set): that names a nested
    subprocess reusing its parent's session, not a real ``/clear`` boundary
    a mailbox was stranded at, and recording one there would name a
    "previous" session that never stopped receiving mail. Every other
    source (``resume``, ``compact``, ``startup``, ``fork``, or none) writes
    no record, per the RDR's Fork and Two-processes paragraphs and Sam's
    decision that a fork leaves the parent's mailbox with the parent.

    Best-effort: any failure (no ``ps``, no config dir, disk error) is
    logged at debug and swallowed -- a SessionStart hook must never fail
    the session over a mailbox-watch convenience feature.
    """
    try:
        from nexus import config as _nx_config  # noqa: PLC0415 — deferred import; module attribute so a patched nexus.config reaches it (nexus-78blw)
        from nexus.session import find_immediate_claude_pid  # noqa: PLC0415 — deferred import; rare/branch-local path
        from nexus.session_marker import (  # noqa: PLC0415 — deferred import; rare/branch-local path
            record_clear_and_write_session_marker,
        )

        claude_pid = find_immediate_claude_pid()
        if claude_pid <= 0:
            return
        inherited = bool(os.environ.get("NX_SESSION_ID", "").strip())
        record_clear_and_write_session_marker(
            _nx_config.nexus_config_dir(), claude_pid, new_session_id,
            record_clear=(source == "clear" and not inherited),
        )
    except Exception as exc:  # noqa: BLE001 — best-effort; hook must never crash session-start over a mailbox-watch convenience feature
        _logger().debug("tuple_watch_session_marker_write_failed", error=str(exc))


def adopt_session_marker(session_id: str) -> None:
    """Point this claude process's watcher self-stop marker at *session_id*,
    recording no ``/clear``.

    ``nx hook mailbox-arm`` calls this. ``/branch`` forks a new session in the
    same claude process and runs no SessionStart, so the marker still names
    the parent session and the parent's watcher keeps running in the fork.
    Writing the fork's id stops that watcher. No cleared record is written:
    the parent's mailbox stays with the parent (RDR-208 Fork paragraph).
    Best-effort, like every marker write here.
    """
    _write_tuple_watch_session_marker(session_id, None)


# -- SessionStart -------------------------------------------------------------

def render_session_start(session_id: str, *, mailbox_arm_text: str = "") -> str:
    """The SessionStart text for *session_id*: computation only.

    nexus-cnzei.2 item 8: split out of :func:`session_start` after a
    coordinator session called ``session_start()`` directly from an ad
    hoc script (outside pytest's ``_isolate_config_dir`` autouse fence,
    HOME unset) to hand-measure byte budgets, and its unconditional
    :func:`_write_tuple_watch_session_marker` call wrote a marker under
    the REAL ``~/.config/nexus`` naming a stale fixture session id, which
    is exactly what the (since-deleted) CLI mailbox watcher's self-stop
    check watched for, and it stopped the live watcher on the next poll. Two
    sessions hit this same trap the same day.

    This function performs NO writes: no ``current_session`` file, no T1
    handoff marker, no tuple-watch session marker, no T1 lease. It calls
    only :func:`_stale_mcp_host_warning` (process-table reads via ``ps``)
    and :func:`_guidance_imperative_block` (reads ``hooks.json``), both
    already read-only. It is therefore safe to call directly against ANY
    HOME/NEXUS_CONFIG_DIR, including manually, outside pytest, with no
    risk of touching another session's state.

    *mailbox_arm_text* is passed in rather than computed here because
    :func:`_mailbox_arm_block` (:func:`nexus.mailbox_arm.arm_block`)
    performs a real network probe of the tuple-space engine and writes a
    probe-result cache file under ``<config>/tuple-watch/``: genuine
    I/O this function must never perform on its own. :func:`session_start`
    computes it once and threads it through; a caller measuring the pure
    render (byte budgets, ad hoc inspection) passes "" or a literal
    fixture string instead of triggering that probe.

    No ``source`` parameter: the SessionStart ``source`` field
    (startup/resume/clear/compact) only ever decided which WRITE fired in
    the old combined function (see :func:`session_start`'s docstring); it
    never changed this text, so a pure render has nothing to do with it.
    """
    return (
        f"Nexus ready (session: {session_id})."
        f"{_stale_mcp_host_warning()}"
        f"{_guidance_imperative_block()}"
        f"{mailbox_arm_text}"
    )


def session_start(claude_session_id: str | None = None, source: str | None = None) -> str:
    """Execute the SessionStart hook.

    Resolves the session UUID and persists it to ``current_session``
    so cross-process tools (shell ``nx scratch``, doctor diagnostics,
    SessionEnd flush) can look it up. Nested subprocesses (operator
    ``claude -p`` calls, subagents) inherit ``NX_SESSION_ID`` from
    their parent's env; their SessionStart must leave the parent's
    pointer alone so the parent's shell-side tools stay in sync.

    Chroma lifecycle is owned by the MCP server's FastMCP lifespan
    (RDR-105 P4) and is no fixture of this hook. Multi-writer record
    machinery (``sessions/<uuid>.session``, sweep, reconcile) was
    deleted in P4; the hook does session-id propagation only.

    ``source`` (nexus-d76vc, RDR-105 aj564 follow-up): the Claude Code
    SessionStart ``source`` field (``startup``/``resume``/``clear``/
    ``compact``/``fork``). On ``resume``/``clear``/``fork`` -- the sources
    where the transcript's session id changes out from under a live,
    already-frozen MCP server -- writes a T1 handoff marker for each MCP
    server sibling so its FastMCP lifespan can re-lease onto the new
    session id (see :func:`_write_t1_handoff_markers`). ``startup``
    spawns brand-new MCP servers (nothing frozen yet) and ``compact``
    keeps the same session id (no divergence); neither writes a marker.
    """
    # Resolve session_id with this precedence:
    #   1. ``NX_SESSION_ID`` env: nested subprocess that inherited the
    #      parent's UUID. Leave ``current_session`` untouched.
    #   2. ``claude_session_id`` from stdin: top-level Claude session.
    #   3. Fresh UUID: fallback for invocations outside Claude Code.
    inherited = os.environ.get("NX_SESSION_ID", "").strip() or None
    from nexus.session import (  # noqa: PLC0415 — deferred; see the comment at module scope
        generate_session_id,
        write_claude_session_id,
    )

    session_id = inherited or claude_session_id or generate_session_id()

    if not inherited:
        write_claude_session_id(session_id)

    if source in _T1_HANDOFF_SOURCES and session_id and session_id != "unknown":
        _write_t1_handoff_markers(session_id)
    if session_id and session_id != "unknown":
        # Every source, not just clear/resume: see the writer's docstring.
        _write_tuple_watch_session_marker(session_id, source)

    # nexus-gff3g: do NOT claim "T1 scratch initialized" here. This hook only
    # records the session-id; T1 chroma is owned by the MCP server's FastMCP
    # lifespan (RDR-105 P4), which may key its lease on a DIFFERENT session-id
    # (its NX_SESSION_ID) than the one written above. Claiming initialization
    # masked exactly that divergence during the 5.10.x T1-scratch failure.
    # RDR-155 P4b: the substrate-migration bridge notice (nexus-0rwwv) died
    # with the migration machinery; stranded pre-PG installs are redirected
    # to the LAST_MIGRATION_CAPABLE release by the stranded-install detector.
    #
    # nexus-cnzei.2 item 8: the writes above are this function's whole job;
    # everything text-shaped is delegated to the side-effect-free
    # :func:`render_session_start`. The mailbox-arm probe is the one
    # remaining piece of real I/O (network probe + cache write), computed
    # here rather than inside the pure render.
    return render_session_start(session_id, mailbox_arm_text=_mailbox_arm_block(session_id))


def _stale_mcp_host_warning() -> str:
    """One-line SessionStart nudge when THIS session's own nx-mcp/
    nx-mcp-catalog process predates the installed conexus distribution.

    nexus-otnvr item 5 (substantive-critic 2026-08-08): ``nx doctor``'s
    "Process freshness" check (:func:`nexus.health._check_process_skew`,
    nexus-4xgfy) already detects this, but only when an operator happens to
    run ``nx doctor`` by hand — every OTHER live session stays blind to a
    background upgrade until it hits an import error mid tool-call (the
    reactive :mod:`nexus.mcp._stale_host` decorator). ``nx hook
    session-start`` is the one hook-surface invocation that already runs
    the FULL installed ``nx`` (not a bare, package-less interpreter like
    the other SessionStart scripts), so this is the cheapest proactive
    close for that gap: reusing :func:`nexus.upgrade_finish.
    detect_stale_processes` directly, the identical primitive doctor
    calls, so the two surfaces can never diverge on what "stale" means.

    nexus-cnzei.2 (S4): the machine-wide form of this note counted every
    live nx-mcp host on the box, including OTHER sessions'. A session
    with a perfectly fresh MCP host would still see "N nx-mcp process(es)
    ... predate ..." because a sibling terminal's server was stale, which
    is neither this session's business nor actionable from inside it.
    Scoped down to THIS session's own MCP siblings via the same
    ancestry primitive :func:`_write_t1_handoff_markers` already uses
    (:func:`nexus.session.find_immediate_claude_pid` ->
    :func:`nexus.session.find_mcp_sibling_pids`): only a stale host that
    is a live, immediate child of the Claude process running THIS hook
    counts. A ``run /mcp`` instruction also only ever means anything to
    the human user, never to a model reading this text, reworded to say
    so. Never a network call, never a write: both ancestry lookups and
    the process scan are read-only.

    Never raises — a probe failure here must not break session start
    (mirrors every other best-effort leg in this module).
    """
    try:
        from nexus.upgrade_finish import detect_stale_processes  # noqa: PLC0415 — deferred import, only needed on this path
        report = detect_stale_processes()
    except Exception:  # noqa: BLE001 — session start must never break on this probe
        return ""
    candidate_hosts = [p for p in report.stale if p.kind == "mcp-host"]
    if not candidate_hosts:
        return ""
    # Ancestry scan (a second process-table read) only runs when there is
    # something to scope: the overwhelmingly common case (no stale hosts
    # anywhere) costs nothing beyond the probe above.
    try:
        from nexus.session import (  # noqa: PLC0415 -- deferred import, only needed on this path
            find_immediate_claude_pid,
            find_mcp_sibling_pids,
        )

        claude_pid = find_immediate_claude_pid()
        sibling_pids = set(find_mcp_sibling_pids(claude_pid)) if claude_pid > 0 else set()
    except Exception:  # noqa: BLE001 -- an ancestry-scan failure means "no note", not "fall back to unscoped"
        sibling_pids = set()
    hosts = [p for p in candidate_hosts if p.pid in sibling_pids]
    if not hosts:
        return ""
    return (
        f" NOTE: {len(hosts)} nx-mcp process(es) for this session predate "
        f"the installed conexus {report.installed_version}: ask the user "
        f"to run /mcp if tool calls start failing with import errors."
    )


def _mailbox_arm_block(session_id: str) -> str:
    """nexus-6konb.9 (MM-3.1), superseded by RDR-211 nexus-rplay.14: the
    RDR-205 mailbox subscribe instruction.

    Emitted on every SessionStart source (startup/resume/clear/compact --
    unlike :func:`_write_t1_handoff_markers`, there is no reason to gate
    this on ``source``: subscribing again is worth repeating after a
    ``/compact`` exactly as much as after ``/clear``, and re-subscribing an
    already-subscribed instance mailbox is a harmless no-op). See
    :mod:`nexus.mailbox_arm` for the instruction text, the bounded+cached
    tuple-surface availability probe, and why it is silent when that probe
    fails.

    Never raises — mirrors every other best-effort leg in this module.
    """
    try:
        from nexus.mailbox_arm import arm_block  # noqa: PLC0415 — deferred import, only needed on this path

        text = arm_block(session_id)
    except Exception as exc:  # noqa: BLE001 — session start must never break on this probe
        _logger().debug("mailbox_arm_block_failed", error=str(exc))
        return ""
    return f"\n\n{text}" if text else ""


def _guidance_imperative_block() -> str:
    """nexus-h33x8.4: emit the SessionStart guidance imperative from the
    wheel (Tier B) instead of the pinned plugin's ``cat .../SKILL.md``
    hooks.json entry (Tier C).

    See :mod:`nexus.session_start_guidance` for the moved content and
    the interim double-emission guard (suppresses this block while the
    installed plugin's own ``hooks.json`` still carries the legacy
    ``cat`` entry, so a session under an un-upgraded plugin gets the
    imperative exactly once, not twice).

    Never raises — a probe failure here must not break session start
    (mirrors every other best-effort leg in this module).
    """
    try:
        from nexus.session_start_guidance import guidance_block  # noqa: PLC0415 — deferred import, only needed on this path
        text = guidance_block()
    except Exception as exc:  # noqa: BLE001 — session start must never break on this probe
        _logger().debug("guidance_imperative_block_failed", error=str(exc))
        return ""
    return f"\n\n{text}" if text else ""


# -- SessionEnd ---------------------------------------------------------------


def session_end_flush() -> str:
    """Run the storage-only portion of SessionEnd: T1 flush + T2 expire.

    Fork-safe: each call opens fresh T1/T2 handles and does not touch
    module-level state acquired pre-fork. Constructs ``T1Database()``
    so any flagged scratch entries can be flushed; if T1 cannot be
    resolved (no live MCP, no addr file, no isolation flag), the
    constructor's fail-loud raise surfaces the gap and the flush is
    skipped.

    ``T1ServerNotFoundError`` frequency (nexus-6a19f, updated from the
    original "known race window" framing below): since nexus-f7xyq this
    raise is no longer a narrow teardown-race case -- it fires for the
    ROUTINE split-brain scenario where this detached grandchild's
    resolvable session id (``NX_SESSION_ID`` inherited via ``fork()``, or
    the current transcript's ``CLAUDE_CODE_SESSION_ID``) has no fresh
    published T1 lease, which is common whenever the owning MCP process
    was spawned for a DIFFERENT (earlier or divergent) session than the
    one this hook invocation resolves. This is expected, not exceptional:
    a best-effort flush that skips under session-id divergence is the
    correct, session-isolation-safe behavior -- the alternative (silently
    reading/writing a different session's shared scope, the pre-f7xyq
    bug) was worse. ``_open_t1()`` additionally forces
    ``NX_T1_ALLOW_SHARED_FALLBACK`` off for this call (see its own
    docstring) so this path can never be talked into the shared-scope
    ``clear()`` below via that escape hatch.

    Known race window (the original, narrower case this docstring used to
    describe exclusively)
        On stdio transport the SessionEnd hook fires when stdin EOFs,
        which is the same event that drives the MCP server's lifespan
        ``async finally`` to relinquish its T1 lease record
        (``~/.config/nexus/t1_addr.<session_id>``) and stop chroma
        (RDR-149 P4). The launcher daemonizes ``session_end_flush``
        in a grandchild, but if the lifespan finally wins the race the
        grandchild's ``T1Database()`` resolves the session-id, finds no
        live lease, and raises ``T1ServerNotFoundError``. The
        ``except`` below catches the raise and logs
        ``session_end_flush_t1_unavailable``; flagged entries are then
        silently dropped. Best-effort flush is the documented contract;
        a future improvement would be for the lifespan to drain the
        flagged-entries queue itself before unlinking the addr file.

    ``clear()`` is gated on OWNERSHIP, not merely on ``t1 is not None``
    (nexus-65a9k / GH #1454). A tool-free ``claude -p`` operator dispatch
    strips this child's ``NX_T1_SESSION``/``NX_T1_SESSION_ID`` but still
    forwards ``NX_SESSION_ID=<PARENT's session id>`` (see
    :mod:`nexus.operators.dispatch`). When THIS grandchild's SessionEnd
    fires, tier 1 (``USE_INHERITED``) misses -- the T1 env was stripped --
    but tier 2 (``USE_LEASED``) resolves the parent's id, finds the
    parent MCP's still-live, still-fresh published lease, and binds a
    store to it. That store is real and gets flushed normally below
    (flushing a live borrowed scope's flagged entries is not the unsafe
    operation), but a BORROWED lease is never grounds to delete the scope
    out from under the process that still owns it: right before the
    ``clear()`` call, this function independently re-derives the SAME
    tier-1/tier-2 decision
    (:func:`nexus.db.t1.resolve_t1_routing_tiers`) and skips ``clear()``
    whenever it comes back ``USE_LEASED``.

    ACCEPTED TRADEOFF, stated at its REAL blast radius -- which is much
    wider than "the owning MCP already tore its lease down", the narrow
    case an earlier draft of this comment described. Measured by tracing
    :func:`nexus.db.t1.resolve_t1_routing_tiers` against how this hook
    actually resolves: the detached SessionEnd grandchild never inherits
    ``NX_T1_SESSION`` from the MCP server's process env, so
    ``USE_INHERITED`` is effectively unreachable for a genuine top-level
    session's own hook; ``resolve_active_session_id()`` virtually always
    lands on ``CLAUDE_CODE_SESSION_ID`` (Claude Code sets it on every
    spawned subprocess), and the top-level MCP publishes a lease for its
    OWN session. So when this hook wins the pre-existing stdio-EOF race
    it resolves ``USE_LEASED`` against its own session's own lease --
    which :mod:`nexus.db.t1` documents as the DESIGNED mechanism, not a
    foreign borrow -- and the gate skips.

    Net effect: after nexus-65a9k, ``clear()`` essentially never fires
    for a genuine top-level SessionEnd. Rows are reaped by the 24h TTL
    sweep (``SWEEP_TTL_HOURS`` in ``NexusService.java``, a live recurring
    per-tenant task, verified -- not aspirational) rather than deleted
    immediately.

    That is still strictly the safe direction: leaking rows to a TTL
    sweep is recoverable, deleting a live process's working memory is
    not. It is written out at full width deliberately. The defect this
    fix repairs survived review because the comment above the old
    ``clear()`` asserted a safety property the code did not have; an
    understated comment here would repeat that exact mistake in the
    opposite direction.

    The gate infers ownership from the ROUTING TIER, which is a proxy,
    not a real ownership signal -- ``USE_INHERITED``'s safety currently
    rests on discipline in :mod:`nexus.operators.dispatch` that this
    function cannot verify. A future drift there could reopen an
    equivalent hole. Tracked as a follow-up.
    """
    from nexus.db.t2.http_memory_store import MemoryExpireResult  # noqa: PLC0415 — deferred import; keeps the hook's startup cost off the T2 client

    flushed = 0
    expired = MemoryExpireResult()

    try:
        try:
            t1 = _open_t1()
        except Exception as exc:  # noqa: BLE001 — best-effort; error surfaced via log/echo, must not crash caller
            _logger().warning(
                "session_end_flush_t1_unavailable",
                error=str(exc),
                detail="flagged scratch entries were not flushed",
            )
            t1 = None
        # T1 access is process-local; snapshot the flagged entries here so
        # only the T2 writes cross the daemon boundary below.
        entries = list(t1.flagged_entries()) if t1 is not None else []

        def _flush_and_expire(db):
            n = 0
            for entry in entries:
                db.memory.put(
                    project=entry["flush_project"],
                    title=entry["flush_title"],
                    content=entry["content"],
                    tags=entry.get("tags", ""),
                    ttl=None,
                )
                n += 1
            # Flushed entries are permanent (ttl=None); the expire sweep
            # below only reaps already-expired rows, so flush-then-expire
            # ordering is safe.
            return n, db.expire_detail()

        # RDR-128 P3 (nexus-sbxbe.3): route the flush + TTL sweep through the
        # T2 daemon so the detached SessionEnd grandchild does not open
        # memory.db directly and contend on its single WAL writer lock.
        # t2_index_write falls back to a direct T2Database when the daemon
        # is unreachable (the grandchild can outlive the MCP lifespan).
        from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
        flushed, expired = t2_index_write(_flush_and_expire)
        # nexus-6a19f: `t1 is None` here covers every case where
        # `_open_t1()` raised -- including, since nexus-f7xyq, an explicit
        # session id with no usable lease. That is load-bearing, not
        # incidental: pre-f7xyq, that same case returned the SHARED
        # CLI-dedicated store (the fallback identity every bare `nx
        # scratch` invocation on the machine reads/writes), and clear()
        # below would have wiped that ENTIRE shared scope -- every
        # session's markers, not just this one's -- on every lease-less
        # SessionEnd. `_open_t1()` forces `NX_T1_ALLOW_SHARED_FALLBACK` off
        # for its call, so that escape hatch can never route this
        # particular clear() at the shared scope either.
        #
        # nexus-65a9k / GH #1454 (CORRECTS the claim this comment used to
        # make): `t1 is not None` does NOT by itself mean "a store this
        # process can prove is scoped to its own session." It means
        # USE_INHERITED, USE_LEASED, or the genuinely-bare CLI-dedicated
        # mint -- and USE_LEASED is a BORROW of a scope a DIFFERENT, still
        # -live process owns (see this function's docstring). A tool-free
        # `claude -p` operator dispatch forwards `NX_SESSION_ID=<parent>`
        # while stripping the T1 env, so this grandchild's tier-1 check
        # misses and tier-2 binds to the parent MCP's own live, fresh
        # lease -- `t1 is not None` is true, but `t1` is the PARENT's
        # scope, not this process's. Ownership is therefore re-checked
        # HERE, immediately before the destructive call, rather than
        # trusted from whatever `_open_t1()` returned: if this process
        # cannot prove (via the same tier-1/tier-2 decision function) that
        # it is not merely borrowing the scope, it must not delete it.
        if t1 is not None:
            _t1_clear_if_owned(t1)
    except Exception as exc:  # noqa: BLE001 — session-end boundary: a storage error of ANY class must not crash the host (the SQLite-specific catch went with the substrate, 2026-08-29)
        _logger().warning("session_end_storage_error", phase="flush_expire", error=str(exc))

    return f"Session ended. Flushed {flushed} scratch entries. {expired.describe(prefix='memory ')}"


def session_end() -> str:
    """Execute the SessionEnd hook.

    Thin wrapper around :func:`session_end_flush`. nx-mcp owns chroma
    teardown via its FastMCP lifespan + signal handler + atexit chain
    (RDR-094 Phase 4, unconditional as of 4.13.0); the watchdog is the
    safety net if all three of those paths fail. The hook does T1
    flush + T2 expire only.
    """
    return session_end_flush()
