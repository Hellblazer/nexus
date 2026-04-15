# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pool lifecycle + PID-liveness reconciliation — RDR-079 §P2.2.

This module owns the pool session file at
``~/.config/nexus/sessions/pool-<uuid>.session`` and the liveness logic
that cleans up stale peers on startup. Worker management (async
subprocess pool, streaming JSON parsing, retirement) lives in P2.1 and
will compose these primitives.

Invariants maintained here:
  I-1: pool session is distinct from any user T1 session — the session
       file is named ``pool-<uuid>.session`` (RDR-078 session files use
       ``{ppid}.session``), and ``resolve_t1_session`` returns the pool
       record to any worker whose ``NEXUS_T1_SESSION_ID`` matches.
  I-3: no orphan sessions after graceful shutdown — teardown removes
       the file and stops the T1 HTTP server.

SC coverage: SC-13 (a graceful stop, b stale reconcile, c live-peer
preserve), part of SC-11 (scratch-sentinel isolation needs a live pool
session to target) and SC-15 (pool startup fails fast when auth missing —
implemented at the pool-core layer in P2.1, not here).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from uuid import uuid4

import structlog

# Indirection for test injection — monkeypatch these attributes to avoid
# spawning a real ChromaDB server in unit tests. Production callers should
# not override.
from nexus.session import (
    SESSIONS_DIR,
    start_t1_server as _start_t1_server,
    stop_t1_server as _stop_t1_server,
    write_session_record,
)

_log = structlog.get_logger()

__all__ = [
    "OperatorPool",
    "PoolAuthUnavailableError",
    "PoolConfigError",
    "PoolSession",
    "PoolSpawnError",
    "Worker",
    "build_worker_cmdline",
    "check_auth",
    "create_pool_session",
    "probe_pid_alive",
    "reconcile_stale_pool_sessions",
    "teardown_pool_session",
    "worker_env",
]


# ── Errors ─────────────────────────────────────────────────────────────────


class PoolConfigError(Exception):
    """Raised when a pool operation is invoked without required configuration.

    Most commonly: ``OperatorPool.spawn_worker()`` called without
    ``NEXUS_T1_SESSION_ID`` in the environment. This is load-bearing for
    worker T1 isolation (RDR-079 invariant I-4); silent spawn without it
    would let the worker's session-discovery fall back to PPID-walk and
    land on the user's T1, violating I-1. SC-15 pins this behaviour.
    """


class PoolAuthUnavailableError(Exception):
    """Raised when ``claude auth status`` reports no active authentication.

    The operator pool cannot dispatch work without a logged-in ``claude``
    CLI. Surfaced by MCP operator tools so callers see a clear "run
    `claude auth login` or set ANTHROPIC_API_KEY" message. SC-10
    graceful-degradation mechanism.
    """


class PoolSpawnError(Exception):
    """Raised when a worker subprocess cannot be started.

    Covers: claude CLI not on PATH, permission denied, unexpected
    subprocess exit during startup, etc.
    """


@dataclass(frozen=True)
class PoolSession:
    """Metadata for a live pool T1 session.

    ``session_id`` is used as the filename stem (``pool-<uuid>.session``)
    and is also what workers receive via ``NEXUS_T1_SESSION_ID``. The
    T1 endpoint (``host``, ``port``) is the ChromaDB HTTP server the
    pool spawned for isolated scratch.
    """
    session_id: str
    host: str
    port: int
    server_pid: int
    pool_pid: int
    tmpdir: str


# ── Liveness probe ─────────────────────────────────────────────────────────


def probe_pid_alive(pid: int) -> bool:
    """Return True if ``pid`` names a running process on this host.

    ``os.kill(pid, 0)`` sends no signal — it just validates that the
    kernel recognises the PID and the caller has permission to signal
    it. A ``PermissionError`` means the process exists but belongs to
    another user (rare on single-user workstations; still counted as
    alive for safety — we do not own the PID).

    PID 0 is rejected because ``kill(0, ...)`` means "signal the whole
    process group" on POSIX, which is not the liveness question we are
    asking.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Different user owns the process — it exists but we can't signal it.
        # Treat as alive: safer than removing a file that belongs to a live
        # pool owned by someone else on this host.
        return True
    except OSError:
        # Catch-all for platform quirks (EINVAL etc.) — log and assume dead.
        _log.debug("probe_pid_alive_oserror", pid=pid, exc_info=True)
        return False


# ── Reconciliation ─────────────────────────────────────────────────────────


def reconcile_stale_pool_sessions(
    sessions_dir: Path | None = None,
) -> int:
    """Remove ``pool-*.session`` files whose ``pool_pid`` is no longer alive.

    Iterates ``{sessions_dir}/pool-*.session``. For each:
      * Parse JSON. If parseable AND ``pool_session`` is True:
          - If ``pool_pid`` is missing → treat as corrupt, remove.
          - Else probe ``pool_pid`` via :func:`probe_pid_alive`.
          - If dead, remove the file.
      * If JSON is corrupt, remove the file.
      * If ``pool_session`` is absent or False (user session), leave
        untouched — user sessions have their own cleanup path
        (:func:`nexus.session.sweep_stale_sessions`).

    Returns the count of files removed.
    """
    if sessions_dir is None:
        sessions_dir = SESSIONS_DIR
    if not sessions_dir.exists():
        return 0

    removed = 0
    for path in sorted(sessions_dir.glob("pool-*.session")):
        try:
            record = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            _log.debug("pool_reconcile_corrupt_file_removed", path=str(path))
            _try_unlink(path)
            removed += 1
            continue

        if not isinstance(record, dict) or not record.get("pool_session"):
            # Defensive: a file named pool-*.session that isn't a pool
            # record is suspicious but not ours to touch. Skip it.
            continue

        pool_pid = record.get("pool_pid")
        if not isinstance(pool_pid, int):
            _log.debug("pool_reconcile_missing_pool_pid", path=str(path))
            _try_unlink(path)
            removed += 1
            continue

        if not probe_pid_alive(pool_pid):
            _log.info(
                "pool_reconcile_stale_removed",
                path=str(path),
                pool_pid=pool_pid,
            )
            _try_unlink(path)
            removed += 1

    return removed


def _try_unlink(path: Path) -> None:
    """Remove a path, ignoring ``OSError`` (best effort)."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        _log.debug("pool_unlink_failed", path=str(path), error=str(exc))


# ── Lifecycle: create / teardown ───────────────────────────────────────────


def create_pool_session(
    sessions_dir: Path | None = None,
) -> PoolSession:
    """Start a dedicated T1 HTTP server for the pool and record the session.

    Sequence (order matters):
      1. Run :func:`reconcile_stale_pool_sessions` to clean up dead peers
         from prior crashed pools BEFORE writing our own file (avoids
         self-removal on a race between write + scan).
      2. Generate a UUID and start the T1 HTTP server.
      3. Write ``~/.config/nexus/sessions/pool-<uuid>.session`` with
         ``pool_session=True`` and ``pool_pid=os.getpid()`` (for P2.2
         liveness reconciliation).

    Returns a :class:`PoolSession` the caller uses when teardown runs.
    """
    if sessions_dir is None:
        sessions_dir = SESSIONS_DIR

    # Step 1: reconcile BEFORE we add our own file so the scan cannot
    # mistake the new file for a stale peer.
    reconcile_stale_pool_sessions(sessions_dir)

    # Step 2: spawn T1 HTTP server for this pool.
    host, port, server_pid, tmpdir = _start_t1_server()

    # Step 3: generate session identity and persist the record.
    session_id = f"pool-{uuid4()}"
    pool_pid = os.getpid()
    write_session_record(
        sessions_dir=sessions_dir,
        ppid=0,  # unused for pool sessions
        session_id=session_id,
        host=host,
        port=port,
        server_pid=server_pid,
        tmpdir=tmpdir,
        pool_session=True,
        pool_pid=pool_pid,
    )
    _log.info(
        "pool_session_created",
        session_id=session_id,
        host=host,
        port=port,
        server_pid=server_pid,
        pool_pid=pool_pid,
    )
    return PoolSession(
        session_id=session_id,
        host=host,
        port=port,
        server_pid=server_pid,
        pool_pid=pool_pid,
        tmpdir=tmpdir,
    )


def teardown_pool_session(
    session: PoolSession,
    sessions_dir: Path | None = None,
) -> None:
    """Stop the pool's T1 HTTP server and remove its session file.

    Idempotent — a second call after a successful teardown is a no-op.
    Order: remove the session file first (so a racing reconcile cannot
    observe a live file pointing at a just-killed server), then stop
    the server.
    """
    if sessions_dir is None:
        sessions_dir = SESSIONS_DIR
    session_file = sessions_dir / f"{session.session_id}.session"
    _try_unlink(session_file)
    try:
        _stop_t1_server(session.server_pid)
    except Exception as exc:
        # Server may already be gone (e.g. crashed or torn down by a
        # signal handler). Not fatal.
        _log.debug(
            "pool_teardown_stop_server_failed",
            server_pid=session.server_pid,
            error=str(exc),
        )


# ── Auth guard (SC-10) ─────────────────────────────────────────────────────


def check_auth() -> None:
    """Verify ``claude auth status --json`` reports a logged-in session.

    Raises :class:`PoolAuthUnavailableError` on any failure (command
    missing, JSON unparseable, missing ``loggedIn`` key, or
    ``loggedIn=false``). RDR-079 risk mitigation: the JSON schema may
    drift across claude CLI versions, so we probe for key PRESENCE
    before trusting its value.
    """
    try:
        result = subprocess.run(
            ["claude", "auth", "status", "--json"],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError as exc:
        raise PoolAuthUnavailableError(
            "`claude` CLI not found on PATH. Install claude code and "
            "run `claude auth login`, or set ANTHROPIC_API_KEY."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise PoolAuthUnavailableError(
            "`claude auth status` timed out after 15s"
        ) from exc

    if result.returncode != 0:
        raise PoolAuthUnavailableError(
            f"`claude auth status --json` exited with {result.returncode}: "
            f"{(result.stderr or result.stdout)[:200]}"
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PoolAuthUnavailableError(
            f"`claude auth status --json` returned unparseable JSON "
            f"(claude CLI schema drift?): {exc}"
        ) from exc

    if not isinstance(data, dict) or "loggedIn" not in data:
        raise PoolAuthUnavailableError(
            "`claude auth status --json` did not include a `loggedIn` key "
            "(claude CLI schema drift?). Expected shape: "
            "{\"loggedIn\": bool, \"authMethod\": str, ...}"
        )

    if not data["loggedIn"]:
        raise PoolAuthUnavailableError(
            "`claude auth status` reports loggedIn=false. "
            "Run `claude auth login` or set ANTHROPIC_API_KEY to enable "
            "operator-backed plan steps."
        )


# ── Worker command-line + env (RDR-079 §Worker pool) ───────────────────────


def build_worker_cmdline(
    session_id: str,
    operator_role: str,
    max_budget_usd: float,
    max_turns: int,
    model: str = "haiku",
    mcp_config: str | None = None,
) -> list[str]:
    """Compose the ``claude -p`` streaming-RPC invocation for a pool worker.

    Matches RDR-079 §Worker pool shape verbatim (Empirical Finding 1 +
    3). Flags:
      * ``--input-format stream-json --output-format stream-json
        --verbose`` — the persistent RPC protocol.
      * ``--no-session-persistence`` — workers are ephemeral; session
        identity is carried via ``NEXUS_T1_SESSION_ID`` in env.
      * ``--session-id`` — per-worker identity for tracing.
      * ``--append-system-prompt`` — operator role prompt.
      * ``--max-budget-usd`` / ``--max-turns`` — hard cost caps.
      * ``--model`` — Haiku by default (fast structured-output work).

    Notably does NOT use ``--bare``: bare mode forces API-key auth and
    breaks OAuth inheritance (Empirical Finding 4). Paying the startup
    cost of a full ``claude`` init is the RDR's deliberate trade-off.

    ``mcp_config`` — optional path to a worker-mode ``.mcp.json`` used
    with ``--strict-mcp-config`` for tool-surface restriction (P2.4).
    """
    claude = shutil.which("claude") or "claude"
    cmd: list[str] = [
        claude, "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--session-id", session_id,
        "--append-system-prompt", operator_role,
        "--max-budget-usd", str(max_budget_usd),
        "--max-turns", str(max_turns),
        "--model", model,
    ]
    if mcp_config:
        cmd += ["--mcp-config", mcp_config, "--strict-mcp-config"]
    return cmd


def worker_env(pool_session_id: str) -> dict[str, str]:
    """Build the environment passed to a worker subprocess.

    Inherits the parent env (PATH, HOME, auth tokens, etc.) then overlays:
      * ``NEXUS_T1_SESSION_ID=<pool_session_id>`` — worker attaches to the
        pool's isolated T1 session (I-1).
      * ``NEXUS_MCP_WORKER_MODE=1`` — any nested ``nx-mcp`` the worker
        talks to drops plan_match/plan_run/operator_* from its surface
        (I-2, P2.4).
    """
    env = os.environ.copy()
    env["NEXUS_T1_SESSION_ID"] = pool_session_id
    env["NEXUS_MCP_WORKER_MODE"] = "1"
    return env


# ── Worker + Pool (skeleton; commits B/C fill in dispatch/retirement) ──────


@dataclass
class Worker:
    """A single pool worker subprocess.

    Fields are populated as the worker's lifetime progresses; a just-
    spawned worker has ``process`` set and counters at zero. The
    ``_lock`` serialises dispatches on this worker so two concurrent
    callers don't interleave on stdin.
    """
    session_id: str
    process: asyncio.subprocess.Process
    cumulative_input_tokens: int = 0
    cumulative_output_tokens: int = 0
    in_flight: int = 0
    alive: bool = True
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def cumulative_tokens(self) -> int:
        return self.cumulative_input_tokens + self.cumulative_output_tokens


@dataclass
class OperatorPool:
    """Asyncio-driven pool of long-running ``claude`` workers.

    This is the commit-A skeleton: construction, spawn guard, worker
    builder. Dispatch + streaming JSON parse lands in commit B.
    Retirement + health probe in commit C. Singleton wiring via
    ``mcp_infra.py`` ships alongside commit C.
    """
    size: int = 2
    model: str = "haiku"
    max_budget_usd: float = 1.0
    max_turns: int = 6
    retirement_token_threshold: int = 150_000
    workers: list[Worker] = field(default_factory=list)
    pool_session: PoolSession | None = None
    _auth_checked: bool = False

    async def spawn_worker(
        self,
        operator_role: str = "You are a pool worker.",
    ) -> Worker:
        """Launch one ``claude -p`` worker subprocess.

        Pre-conditions:
          * ``NEXUS_T1_SESSION_ID`` must be set in this process's env
            (SC-15, invariant I-4). Raises :class:`PoolConfigError` if
            missing or empty — fail loud, not silent.
          * On first call in the pool's lifetime, :func:`check_auth` is
            invoked (SC-10). Subsequent spawns skip it (cached).

        Returns the live :class:`Worker` on success.
        """
        t1_sid = os.environ.get("NEXUS_T1_SESSION_ID", "").strip()
        if not t1_sid:
            raise PoolConfigError(
                "NEXUS_T1_SESSION_ID must be set before spawning a pool "
                "worker — the env var is the load-bearing mechanism for "
                "worker T1 isolation (RDR-079 invariant I-4). Set it to "
                "a pool-scoped session id (e.g. pool-<uuid>) or call "
                "create_pool_session() first."
            )

        if not self._auth_checked:
            check_auth()  # raises PoolAuthUnavailableError on failure
            self._auth_checked = True

        session_id = f"worker-{uuid4()}"
        cmd = build_worker_cmdline(
            session_id=session_id,
            operator_role=operator_role,
            max_budget_usd=self.max_budget_usd,
            max_turns=self.max_turns,
            model=self.model,
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
            raise PoolSpawnError(
                f"failed to spawn claude worker: {exc}"
            ) from exc

        worker = Worker(session_id=session_id, process=proc)
        self.workers.append(worker)
        _log.info(
            "pool_worker_spawned",
            session_id=session_id,
            pid=proc.pid,
            pool_session=t1_sid,
        )
        return worker

    async def dispatch(
        self,
        worker: Worker,
        prompt: str,
        *,
        timeout: float = 60.0,
    ) -> dict:
        """Send one user turn to *worker*, await its response, return the
        StructuredOutput tool_use payload.

        Protocol (RDR-079 Empirical Finding 1 + 3):
          * Write one JSON line ``{"type":"user","message":{"role":
            "user","content":"<prompt>"}}`` to worker.stdin.
          * Read worker.stdout JSON lines until a ``result`` record with
            ``subtype: success`` appears.
          * During that read, capture the ``StructuredOutput`` tool_use
            input and accumulate token counters from the result record.

        The returned dict is the tool_use ``input`` payload. The ``result``
        record's ``result`` text field is often empty (model's last turn
        may be pure thinking after the tool call) — do NOT depend on it.

        Raises:
          * ``asyncio.TimeoutError`` — worker did not reach ``result``
            within *timeout* seconds. Worker is killed; ``alive=False``.
          * ``PoolSpawnError`` — worker exited before producing a result.
        """
        async with worker._lock:
            if worker.process.returncode is not None:
                worker.alive = False
                raise PoolSpawnError(
                    f"worker {worker.session_id!r} exited "
                    f"(rc={worker.process.returncode}) before dispatch"
                )

            turn = json.dumps({
                "type": "user",
                "message": {"role": "user", "content": prompt},
            }) + "\n"
            assert worker.process.stdin is not None
            try:
                worker.process.stdin.write(turn.encode())
                await worker.process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                worker.alive = False
                raise PoolSpawnError(
                    f"worker {worker.session_id!r} stdin closed: {exc}"
                ) from exc

            worker.in_flight += 1
            try:
                payload = await asyncio.wait_for(
                    self._read_until_result(worker),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                # Kill the hung worker so it doesn't linger.
                worker.alive = False
                try:
                    worker.process.kill()
                except ProcessLookupError:
                    pass
                raise
            finally:
                worker.in_flight -= 1
            return payload

    async def _read_until_result(self, worker: Worker) -> dict:
        """Consume worker.stdout JSON lines until the turn's final
        ``result`` record. Captures the StructuredOutput tool_use payload
        along the way and accumulates token counters from ``result.usage``.

        Returns the StructuredOutput.input dict. If no StructuredOutput
        event was seen (e.g. the model replied with plain text), returns
        ``{"text": result.result}`` as a safety fallback so callers still
        get a dict.
        """
        assert worker.process.stdout is not None
        structured_payload: dict | None = None

        while True:
            line = await worker.process.stdout.readline()
            if not line:
                # EOF — worker exited
                worker.alive = False
                # Drain stderr for the error message.
                stderr = b""
                if worker.process.stderr is not None:
                    try:
                        stderr = await asyncio.wait_for(
                            worker.process.stderr.read(2048), timeout=0.5,
                        )
                    except asyncio.TimeoutError:
                        pass
                raise PoolSpawnError(
                    f"worker {worker.session_id!r} exited before result "
                    f"(rc={worker.process.returncode}); stderr: "
                    f"{stderr.decode(errors='replace')[:500]}"
                )

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # Skip malformed lines (e.g. log noise interleaved with
                # the stream — shouldn't happen with --output-format
                # stream-json, but be defensive).
                continue

            if not isinstance(event, dict):
                continue

            etype = event.get("type")

            if etype == "assistant":
                # Scan for StructuredOutput tool_use
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
                # Per-turn final record; accumulate tokens + return.
                usage = event.get("usage", {}) or {}
                worker.cumulative_input_tokens += int(
                    usage.get("input_tokens", 0) or 0
                )
                worker.cumulative_output_tokens += int(
                    usage.get("output_tokens", 0) or 0
                )
                if structured_payload is not None:
                    return structured_payload
                # Fallback: no StructuredOutput event; return the result
                # text wrapped. Keeps the contract "dispatch returns dict"
                # honored even when the model skipped the schema.
                return {"text": str(event.get("result", ""))}
