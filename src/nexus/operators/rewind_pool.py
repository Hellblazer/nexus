# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RewindPool — per-dispatch subprocess pool with JSONL-truncation rewind.

RDR-079 Amendment 2 (nexus-ilm). Architectural sibling to
:class:`~nexus.operators.pool.OperatorPool`. Where OperatorPool keeps
long-running stream-json subprocesses and retires them at a token
threshold, RewindPool spawns a FRESH subprocess for every dispatch and
rewinds the session JSONL back to a post-warmup checkpoint between
calls.

Trade-off:
  * +3–5s cold startup tax per dispatch (nexus-axu Phase A measured
    $0.01–0.06 per call on Haiku).
  * Guaranteed clean state: no cross-dispatch contamination, no
    cumulative context drift, no 150k-token retirement drama.
  * Warm prompt cache: Anthropic's cache survives the rewind (nexus-axu
    Phase A confirmed 47–97k cache_read tokens per post-rewind turn).
  * Used by batch-style operators where correctness beats latency —
    RDR-080's ``nx_answer`` is the intended first consumer.

Mechanics:

1. **Slot init** — On first dispatch, the pool spawns a fresh
   ``claude -p --session-id <uuid> --no-no-session-persistence``
   subprocess with a warmup turn ("Reply OK."). After the result record
   lands, it locates the session JSONL under
   ``~/.claude/projects/<slug>/<uuid>.jsonl`` and records the file
   size as the ``checkpoint_offset``.

2. **Subsequent dispatch** — Spawn ``claude -p --resume <uuid> ...``
   with the real prompt. Process emits one ``result`` record, exits.

3. **Rewind** — ``os.truncate(jsonl_path, checkpoint_offset)`` rewinds
   the file in place (inode preserved, which ``--resume`` requires).

Upstream ask: a stream-json control message
``{"type":"control","action":"rewind_to","checkpoint":"post-setup"}``
OR a ``--resume-at-turn N`` flag would make this trivial and schema-
drift-proof. Until then, file surgery is the shipped primitive.

The pool respects the same invariants as OperatorPool:
  * SC-11 / I-1: worker T1 isolation via ``NEXUS_T1_SESSION_ID``.
  * SC-15 / I-4: ``PoolConfigError`` if the env var is missing.
  * SC-10: first-dispatch ``check_auth`` raises
    :class:`~nexus.operators.pool.PoolAuthUnavailableError` on failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import structlog

from nexus.operators.pool import (
    PoolAuthUnavailableError,
    PoolConfigError,
    PoolSession,
    PoolSpawnError,
    build_worker_cmdline,
    check_auth,
    create_pool_session,
    teardown_pool_session,
    worker_env,
)

__all__ = ["RewindPool", "RewindSlot"]

_log = structlog.get_logger(__name__)


# Project slug derivation — claude CLI maps a working directory to
# ``-Users-hal-hildebrand-git-nexus`` (/ → -, leading /). Tested
# empirically in the nexus repo.
def _project_slug(cwd: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd.resolve()))


def _projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _jsonl_for(session_id: str, cwd: Path | None = None) -> Path:
    """Return the session JSONL path for *session_id*.

    Resolution order mirrors the CLI's own:
      1. ``<projects>/<slug(cwd)>/<session_id>.jsonl`` — expected path.
      2. Fallback: walk every subdir of ``~/.claude/projects`` looking
         for the matching filename. Covers cwd-slug edge cases (tests
         run from varied tmp dirs).

    Raises ``PoolSpawnError`` if the file can't be found.
    """
    cwd = cwd or Path.cwd()
    expected = _projects_dir() / _project_slug(cwd) / f"{session_id}.jsonl"
    if expected.exists():
        return expected
    for sub in _projects_dir().iterdir() if _projects_dir().exists() else ():
        candidate = sub / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate
    raise PoolSpawnError(
        f"session JSONL for {session_id!r} not found under "
        f"{_projects_dir()}; expected {expected}",
    )


@dataclass
class RewindSlot:
    """One reusable session-identity slot.

    A slot holds a stable session UUID that's reused for every dispatch
    it serves. No live subprocess between dispatches — the slot is the
    JSONL on disk plus the checkpoint offset to rewind to.
    """
    session_id: str
    jsonl_path: Path | None = None
    checkpoint_offset: int = 0
    initialized: bool = False
    dispatch_count: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class RewindPool:
    """Per-dispatch ``claude -p --resume`` spawn pool with JSONL rewind.

    API-compatible with :class:`~nexus.operators.pool.OperatorPool` for
    the consumer-facing methods: ``dispatch_with_rotation`` and
    ``shutdown``. The ``rotation`` term is vestigial here — RewindPool
    does not rotate workers; every dispatch lands on a slot whose lock
    is available.
    """
    size: int = 2
    model: str = "haiku"
    max_budget_usd: float = 1.0
    max_turns: int = 6
    operator_role: str = "You are a pool worker."
    json_schema: dict | None = None
    pool_session: PoolSession | None = None
    slots: list[RewindSlot] = field(default_factory=list)
    _auth_checked: bool = False

    def __post_init__(self) -> None:
        if not self.slots:
            self.slots = [
                RewindSlot(session_id=str(uuid4())) for _ in range(self.size)
            ]

    # ── Slot init (first dispatch per slot) ────────────────────────────

    async def _init_slot(self, slot: RewindSlot) -> None:
        """Establish the slot's session JSONL and record its checkpoint.

        Runs a single warmup turn so the JSONL exists, then captures
        the file size as the rewind target. The warmup turn is included
        in every subsequent dispatch's conversation history (one
        acknowledge/OK pair as cheap setup). The operator role lives in
        ``--append-system-prompt`` so it's part of the cached prefix
        regardless.
        """
        if slot.initialized:
            return

        t1_sid = os.environ.get("NEXUS_T1_SESSION_ID", "").strip()
        if not t1_sid:
            raise PoolConfigError(
                "NEXUS_T1_SESSION_ID must be set before spawning a "
                "RewindPool worker (SC-15 / I-4). Call "
                "create_pool_session() and set the env var first.",
            )

        if not self._auth_checked:
            check_auth()  # may raise PoolAuthUnavailableError
            self._auth_checked = True

        # Critical: spawn the warmup WITHOUT `--json-schema`.
        # If the schema is active at warmup time, the synthetic
        # ``StructuredOutput`` tool lands in the cached prefix and the
        # warmup turn may produce a tool_use that then lives forever
        # in the JSONL below the checkpoint. Every subsequent
        # ``--resume`` dispatch would see "on my last turn I emitted
        # StructuredOutput with <values>" as prior context —
        # contamination the critique flagged as a latent defect.
        #
        # Workflow: warmup-without-schema establishes the session
        # file; `_dispatch_on_slot` re-spawns with the schema for
        # every real dispatch. The prompt cache still warms on the
        # system prompt + operator role which DON'T change.
        cmd = build_worker_cmdline(
            session_id=slot.session_id,
            operator_role=self.operator_role,
            max_budget_usd=self.max_budget_usd,
            max_turns=self.max_turns,
            model=self.model,
            json_schema=None,
            resume=False,
            persist_session=True,
        )
        env = worker_env(pool_session_id=t1_sid)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except (FileNotFoundError, OSError) as exc:
            raise PoolSpawnError(f"failed to spawn claude: {exc}") from exc

        # Warmup turn. Stream-json input format expects one JSON object
        # per line terminated by newline, stdin closed to signal the
        # last turn.
        warmup = {
            "type": "user",
            "message": {"role": "user", "content": "Reply OK."},
        }
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(warmup).encode() + b"\n")
        await proc.stdin.drain()
        proc.stdin.close()

        # Read until the warmup's result record lands. We don't need
        # the payload — we just need the session file to exist.
        await asyncio.wait_for(
            self._drain_until_result(proc), timeout=120.0,
        )
        await proc.wait()

        slot.jsonl_path = _jsonl_for(slot.session_id)
        slot.checkpoint_offset = slot.jsonl_path.stat().st_size
        slot.initialized = True
        _log.info(
            "rewind_pool_slot_initialized",
            session_id=slot.session_id,
            jsonl_path=str(slot.jsonl_path),
            checkpoint_offset=slot.checkpoint_offset,
        )

    # ── Dispatch ──────────────────────────────────────────────────────

    async def _acquire_slot(self) -> RewindSlot:
        """Pick a slot whose lock is free, or fall through to the
        first slot and wait on its lock.

        Simple scan — for pool sizes up to ~8 this is fine. Larger
        pools could swap in an asyncio.Semaphore.
        """
        for slot in self.slots:
            if not slot.lock.locked():
                await slot.lock.acquire()
                return slot
        # All locked — contend for the first slot.
        await self.slots[0].lock.acquire()
        return self.slots[0]

    async def dispatch_with_rotation(
        self,
        prompt: str,
        *,
        timeout: float = 60.0,
        operator_role: str = "You are a pool worker.",
    ) -> dict:
        """Dispatch *prompt* via the pool, rewinding state afterwards.

        The ``operator_role`` parameter is accepted for API symmetry
        with :class:`OperatorPool.dispatch_with_rotation` but ignored —
        RewindPool pins the role at construction time via
        ``self.operator_role`` (slots can't switch roles mid-lifetime
        without reinitializing, and batch consumers rarely need it).
        A caller that passes a DIFFERENT role gets a warning so the
        silent drop doesn't masquerade as success.
        """
        if operator_role != "You are a pool worker." and operator_role != self.operator_role:
            _log.warning(
                "rewind_pool_operator_role_ignored",
                passed=operator_role[:80],
                pool_role=self.operator_role[:80],
            )
        slot = await self._acquire_slot()
        try:
            if not slot.initialized:
                await self._init_slot(slot)
            return await self._dispatch_on_slot(slot, prompt, timeout)
        finally:
            slot.lock.release()

    async def _dispatch_on_slot(
        self, slot: RewindSlot, prompt: str, timeout: float,
    ) -> dict:
        t1_sid = os.environ["NEXUS_T1_SESSION_ID"]
        cmd = build_worker_cmdline(
            session_id=slot.session_id,
            operator_role=self.operator_role,
            max_budget_usd=self.max_budget_usd,
            max_turns=self.max_turns,
            model=self.model,
            json_schema=self.json_schema,
            resume=True,
            persist_session=True,
        )
        env = worker_env(pool_session_id=t1_sid)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except (FileNotFoundError, OSError) as exc:
            raise PoolSpawnError(f"failed to spawn claude: {exc}") from exc

        turn = {
            "type": "user",
            "message": {"role": "user", "content": prompt},
        }
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(turn).encode() + b"\n")
        await proc.stdin.drain()
        proc.stdin.close()

        payload: dict | None = None
        try:
            payload = await asyncio.wait_for(
                self._drain_until_result(proc),
                timeout=timeout,
            )
            # Claude sometimes lingers a fraction of a second after the
            # `result` record before exiting. Cap the tail wait so a
            # hung subprocess cannot block the rewind.
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    _log.error(
                        "rewind_pool_kill_did_not_reap",
                        session_id=slot.session_id,
                        pid=proc.pid,
                    )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            raise
        finally:
            # Rewind MUST run whether the dispatch succeeded, timed
            # out, or raised — otherwise the next dispatch on this
            # slot sees the in-flight turn as prior history and every
            # subsequent call accumulates state (the very contamination
            # this pool exists to prevent).
            self._rewind(slot)

        # Count only successful dispatches so telemetry matches reality.
        slot.dispatch_count += 1
        assert payload is not None  # no TimeoutError → payload was set
        return payload

    # ── StreamReader loop ─────────────────────────────────────────────

    async def _drain_until_result(
        self, proc: asyncio.subprocess.Process,
    ) -> dict:
        """Read stream-json lines until the turn's ``result`` record.

        Mirrors :meth:`OperatorPool._read_until_result`: intercepts the
        synthetic ``StructuredOutput`` tool_use (Empirical Finding 3)
        and returns its validated input dict; falls back to
        ``{"text": result.result}`` when the model skipped the schema.
        """
        if proc.stdout is None:
            raise PoolSpawnError("subprocess stdout is None — PIPE not wired")

        structured_payload: dict | None = None
        while True:
            line = await proc.stdout.readline()
            if not line:
                # EOF — subprocess exited without a result record.
                stderr = b""
                if proc.stderr is not None:
                    try:
                        stderr = await asyncio.wait_for(
                            proc.stderr.read(2048), timeout=0.5,
                        )
                    except asyncio.TimeoutError:
                        pass
                raise PoolSpawnError(
                    f"claude subprocess exited before `result` record "
                    f"(rc={proc.returncode}); stderr: "
                    f"{stderr.decode(errors='replace')[:500]}",
                )
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            etype = event.get("type")
            if etype == "assistant":
                msg = event.get("message", {}) or {}
                for item in msg.get("content", []) or []:
                    if (
                        isinstance(item, dict)
                        and item.get("type") == "tool_use"
                        and item.get("name") == "StructuredOutput"
                    ):
                        input_val = item.get("input")
                        if isinstance(input_val, dict):
                            structured_payload = input_val
            elif etype == "result":
                if structured_payload is not None:
                    return structured_payload
                return {"text": str(event.get("result", ""))}

    # ── Rewind ────────────────────────────────────────────────────────

    def _rewind(self, slot: RewindSlot) -> None:
        """Truncate the slot's JSONL to the post-warmup checkpoint.

        ``os.truncate`` preserves the inode — required for
        ``--resume <uuid>`` to continue finding the file on subsequent
        dispatches. Shell ``>`` redirect replaces the inode and
        breaks the next resume (nexus-axu Phase A finding).

        On any ``OSError`` (``EPERM``, ``EROFS``, quota, etc.), the
        slot is marked ``initialized=False`` so the NEXT dispatch
        re-runs ``_init_slot`` and re-establishes the JSONL + fresh
        checkpoint. Without this recovery the slot would be silently
        corrupted (JSONL at wrong offset, next ``--resume`` ships
        contaminated history).
        """
        if slot.jsonl_path is None:
            return
        try:
            os.truncate(slot.jsonl_path, slot.checkpoint_offset)
        except FileNotFoundError:
            _log.warning(
                "rewind_pool_jsonl_missing",
                session_id=slot.session_id,
                jsonl_path=str(slot.jsonl_path),
            )
            slot.initialized = False
        except OSError as exc:
            _log.error(
                "rewind_pool_truncate_failed",
                session_id=slot.session_id,
                jsonl_path=str(slot.jsonl_path),
                error=str(exc),
                errno=getattr(exc, "errno", None),
            )
            # Force re-init on the next dispatch so downstream callers
            # don't pick up contaminated history via a stale JSONL.
            slot.initialized = False

    # ── Shutdown ──────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Release every slot, delete its JSONL, and tear down the
        pool session. Idempotent.

        RewindPool has no persistent worker subprocesses between
        dispatches — per-dispatch spawns are self-reaping. Shutdown
        is therefore mostly filesystem cleanup. Concurrent in-flight
        dispatches are serialized out by acquiring each slot's lock
        BEFORE unlinking; without this guard, a concurrent dispatch
        could still be reading/writing the JSONL when shutdown runs.
        """
        for slot in list(self.slots):
            async with slot.lock:
                if slot.jsonl_path is not None:
                    try:
                        slot.jsonl_path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        _log.debug(
                            "rewind_pool_jsonl_unlink_failed",
                            jsonl_path=str(slot.jsonl_path),
                            error=str(exc),
                        )
                slot.initialized = False
        self.slots.clear()

        if self.pool_session is not None:
            try:
                teardown_pool_session(self.pool_session)
            except Exception as exc:
                _log.debug(
                    "rewind_pool_shutdown_teardown_session_failed",
                    error=str(exc),
                )
            self.pool_session = None

    # ── Convenience ───────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        *,
        size: int = 2,
        model: str = "haiku",
        operator_role: str = "You are a pool worker.",
        json_schema: dict | None = None,
        max_budget_usd: float = 1.0,
        max_turns: int = 6,
    ) -> "RewindPool":
        """Construct a RewindPool alongside a dedicated pool session.

        Mirrors the one-call factory used by
        :func:`~nexus.mcp_infra.get_operator_pool`: creates the pool
        session AND sets ``NEXUS_T1_SESSION_ID`` on the current
        process env so the first dispatch satisfies the SC-15 / I-4
        precondition without extra ceremony at the call site.
        """
        session = create_pool_session()
        # Wire the env var so _init_slot's precondition check passes
        # on first dispatch. Callers that want isolation from a
        # pre-existing NEXUS_T1_SESSION_ID must unset it first.
        os.environ["NEXUS_T1_SESSION_ID"] = session.session_id
        return cls(
            size=size,
            model=model,
            operator_role=operator_role,
            json_schema=json_schema,
            max_budget_usd=max_budget_usd,
            max_turns=max_turns,
            pool_session=session,
        )
