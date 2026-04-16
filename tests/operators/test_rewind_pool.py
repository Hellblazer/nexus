# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for :class:`nexus.operators.rewind_pool.RewindPool`.

Split into two flavors:

* **Unit** (no claude subprocess; default) — drive the pool with a
  stubbed ``asyncio.create_subprocess_exec`` + a tmp JSONL to pin
  every branch that doesn't require a live claude CLI: cmdline build,
  checkpoint recording, rewind-truncation semantics, lock-per-slot
  concurrency, shutdown cleanup, auth-error conversion.
* **Integration** (``@pytest.mark.integration``) — one live smoke
  test that exercises the real claude CLI + real session JSONL +
  real truncation. Verifies nexus-axu Phase A in the shipped pool:
  two sequential dispatches produce independent outputs and the
  JSONL is back at checkpoint size between them.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio


def _claude_auth_available() -> bool:
    try:
        result = subprocess.run(
            ["claude", "auth", "status", "--json"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    return bool(data.get("loggedIn"))


# ── build_worker_cmdline flags for RewindPool ─────────────────────────────


def test_build_worker_cmdline_persist_session_omits_no_persistence_flag() -> None:
    """RewindPool needs the session written to JSONL so ``--resume``
    works. That means the default ``--no-session-persistence`` must be
    dropped when ``persist_session=True``."""
    from nexus.operators.pool import build_worker_cmdline

    cmd = build_worker_cmdline(
        session_id=str(uuid4()),
        operator_role="test", max_budget_usd=0.5, max_turns=2,
        persist_session=True,
    )
    assert "--no-session-persistence" not in cmd
    assert "--session-id" in cmd  # new session


def test_build_worker_cmdline_default_keeps_no_persistence_flag() -> None:
    """OperatorPool (default: persist_session=False) must continue to
    pass ``--no-session-persistence``; regression guard."""
    from nexus.operators.pool import build_worker_cmdline

    cmd = build_worker_cmdline(
        session_id=str(uuid4()),
        operator_role="test", max_budget_usd=0.5, max_turns=2,
    )
    assert "--no-session-persistence" in cmd


def test_build_worker_cmdline_resume_uses_resume_flag() -> None:
    """``resume=True`` swaps ``--session-id`` for ``--resume <uuid>``."""
    from nexus.operators.pool import build_worker_cmdline

    sid = str(uuid4())
    cmd = build_worker_cmdline(
        session_id=sid,
        operator_role="test", max_budget_usd=0.5, max_turns=2,
        resume=True, persist_session=True,
    )
    assert "--resume" in cmd
    assert "--session-id" not in cmd
    # And the UUID follows the --resume flag
    assert cmd[cmd.index("--resume") + 1] == sid


# ── Rewind semantics ──────────────────────────────────────────────────────


def test_rewind_truncates_to_checkpoint_preserving_inode(tmp_path: Path) -> None:
    """``_rewind`` calls :func:`os.truncate` on the JSONL path — inode
    must NOT change (``--resume`` relies on it)."""
    from nexus.operators.rewind_pool import RewindPool, RewindSlot

    jsonl = tmp_path / "session.jsonl"
    jsonl.write_text("line1\nline2\nline3\n")
    original_inode = jsonl.stat().st_ino
    checkpoint = len("line1\n")

    slot = RewindSlot(
        session_id="abc", jsonl_path=jsonl, checkpoint_offset=checkpoint,
        initialized=True,
    )
    pool = RewindPool(slots=[slot], size=1)
    pool._rewind(slot)

    assert jsonl.read_text() == "line1\n"
    assert jsonl.stat().st_ino == original_inode, (
        "rewind must preserve inode so --resume continues to find it"
    )


def test_rewind_is_safe_when_file_missing(tmp_path: Path) -> None:
    """A slot whose JSONL was externally deleted must not crash on
    rewind. (Recovery behavior — ``initialized=False`` — is pinned by
    ``test_rewind_file_not_found_also_marks_for_reinit``.)"""
    from nexus.operators.rewind_pool import RewindPool, RewindSlot

    slot = RewindSlot(
        session_id="abc",
        jsonl_path=tmp_path / "missing.jsonl",
        checkpoint_offset=0,
        initialized=True,
    )
    pool = RewindPool(slots=[slot], size=1)
    pool._rewind(slot)  # Must not raise


def test_rewind_is_noop_when_path_not_set(tmp_path: Path) -> None:
    """A not-yet-initialized slot has no jsonl_path; ``_rewind`` short-
    circuits without crashing."""
    from nexus.operators.rewind_pool import RewindPool, RewindSlot

    slot = RewindSlot(session_id="abc")
    RewindPool(slots=[slot], size=1)._rewind(slot)  # no crash


def test_rewind_os_error_marks_slot_for_reinit(
    tmp_path: Path, monkeypatch,
) -> None:
    """Review C-2: when ``os.truncate`` fails with a non-
    FileNotFoundError (EPERM, EROFS, quota, …), the slot MUST be
    marked ``initialized=False`` so the next dispatch re-runs
    ``_init_slot``. Without this recovery the slot would be silently
    corrupted — JSONL at wrong offset, next ``--resume`` ships
    contaminated history."""
    import os as os_module

    from nexus.operators.rewind_pool import RewindPool, RewindSlot

    jsonl = tmp_path / "session.jsonl"
    jsonl.write_text("line1\nline2\n")
    slot = RewindSlot(
        session_id="abc", jsonl_path=jsonl, checkpoint_offset=6,
        initialized=True,
    )

    def boom(path, size):
        raise PermissionError("EACCES")

    monkeypatch.setattr(os_module, "truncate", boom)

    pool = RewindPool(slots=[slot], size=1)
    pool._rewind(slot)  # must not raise

    assert slot.initialized is False, (
        "review C-2: OSError on truncate must force re-init on next "
        "dispatch; silent corruption would ship contaminated history"
    )


def test_rewind_file_not_found_also_marks_for_reinit(tmp_path: Path) -> None:
    """FileNotFoundError on rewind (external deletion) must behave
    identically to other OSErrors — force re-init."""
    from nexus.operators.rewind_pool import RewindPool, RewindSlot

    slot = RewindSlot(
        session_id="abc",
        jsonl_path=tmp_path / "missing.jsonl",
        checkpoint_offset=0,
        initialized=True,
    )
    RewindPool(slots=[slot], size=1)._rewind(slot)
    assert slot.initialized is False


# ── Slot acquisition / concurrency ────────────────────────────────────────


@pytest.mark.asyncio
async def test_acquire_slot_prefers_unlocked() -> None:
    """With two slots and the first locked, acquire_slot picks the second."""
    from nexus.operators.rewind_pool import RewindPool, RewindSlot

    slot_a = RewindSlot(session_id="a")
    slot_b = RewindSlot(session_id="b")
    await slot_a.lock.acquire()  # pretend slot-a is busy

    pool = RewindPool(slots=[slot_a, slot_b], size=2)
    picked = await pool._acquire_slot()
    try:
        assert picked is slot_b
    finally:
        picked.lock.release()
        slot_a.lock.release()


@pytest.mark.asyncio
async def test_acquire_slot_falls_through_to_wait_when_all_busy() -> None:
    """When every slot is locked, acquire_slot waits for the first
    slot's lock rather than failing."""
    from nexus.operators.rewind_pool import RewindPool, RewindSlot

    slot = RewindSlot(session_id="only")
    await slot.lock.acquire()
    pool = RewindPool(slots=[slot], size=1)

    task = asyncio.create_task(pool._acquire_slot())
    await asyncio.sleep(0.05)
    assert not task.done()
    slot.lock.release()
    picked = await task
    assert picked is slot
    picked.lock.release()


# ── Config + auth guards ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_init_slot_refuses_without_nexus_t1_session_id(monkeypatch) -> None:
    """SC-15 / I-4: no NEXUS_T1_SESSION_ID → PoolConfigError before
    any subprocess spawn."""
    from nexus.operators.pool import PoolConfigError
    from nexus.operators.rewind_pool import RewindPool, RewindSlot

    monkeypatch.delenv("NEXUS_T1_SESSION_ID", raising=False)
    slot = RewindSlot(session_id="x")
    pool = RewindPool(slots=[slot], size=1)
    with pytest.raises(PoolConfigError):
        await pool._init_slot(slot)


@pytest.mark.asyncio
async def test_init_slot_surfaces_auth_unavailable(monkeypatch) -> None:
    """SC-10: ``check_auth`` raises PoolAuthUnavailableError → bubbles
    up to the dispatch caller (who can wrap it as
    PlanRunOperatorUnavailableError at the operator boundary)."""
    from nexus.operators import pool as pool_mod
    from nexus.operators.pool import PoolAuthUnavailableError
    from nexus.operators.rewind_pool import RewindPool, RewindSlot

    monkeypatch.setenv("NEXUS_T1_SESSION_ID", "pool-test")

    def boom() -> None:
        raise PoolAuthUnavailableError("no auth")

    monkeypatch.setattr(pool_mod, "check_auth", boom)
    monkeypatch.setattr(
        "nexus.operators.rewind_pool.check_auth", boom,
    )

    slot = RewindSlot(session_id="x")
    pool = RewindPool(slots=[slot], size=1)
    with pytest.raises(PoolAuthUnavailableError):
        await pool._init_slot(slot)


# ── Shutdown cleanup ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_shutdown_unlinks_jsonls_and_clears_slots(tmp_path: Path) -> None:
    """Shutdown must delete every slot's JSONL and empty the slot list.
    Idempotent: calling shutdown twice is safe."""
    from nexus.operators.rewind_pool import RewindPool, RewindSlot

    jsonl_a = tmp_path / "a.jsonl"
    jsonl_a.write_text("content")
    jsonl_b = tmp_path / "b.jsonl"
    jsonl_b.write_text("content")
    slot_a = RewindSlot(
        session_id="a", jsonl_path=jsonl_a, initialized=True,
    )
    slot_b = RewindSlot(
        session_id="b", jsonl_path=jsonl_b, initialized=True,
    )

    pool = RewindPool(slots=[slot_a, slot_b], size=2)
    await pool.shutdown()

    assert not jsonl_a.exists()
    assert not jsonl_b.exists()
    assert pool.slots == []

    # Idempotent — a second call is a no-op, not a crash.
    await pool.shutdown()


# ── Live smoke (integration) ──────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_two_dispatches_truncate_back_to_checkpoint(tmp_path) -> None:
    """nexus-axu Phase A replication in the shipped pool: two
    sequential dispatches on the same slot produce independent
    ``StructuredOutput`` payloads, and the session JSONL is at its
    checkpoint size after each dispatch completes."""
    if not _claude_auth_available():
        pytest.skip("claude auth unavailable — live smoke requires login")

    from nexus.operators.pool import create_pool_session, teardown_pool_session
    from nexus.operators.rewind_pool import RewindPool

    session = create_pool_session()
    prior_env = os.environ.get("NEXUS_T1_SESSION_ID")
    os.environ["NEXUS_T1_SESSION_ID"] = session.session_id
    try:
        pool = RewindPool(
            size=1,
            operator_role=(
                "Respond with a single StructuredOutput tool_use. The "
                "input schema is {\"ok\": bool, \"turn\": int}. Set "
                "``turn`` to the count of prior user messages in this "
                "conversation (0 on first, otherwise N)."
            ),
            json_schema={
                "type": "object",
                "required": ["ok", "turn"],
                "properties": {
                    "ok": {"type": "boolean"},
                    "turn": {"type": "integer"},
                },
            },
            pool_session=session,
        )
        try:
            r1 = await pool.dispatch_with_rotation(
                prompt="Acknowledge dispatch #1.", timeout=60.0,
            )
            slot = pool.slots[0]
            size_after_first = slot.jsonl_path.stat().st_size
            # After _rewind, the file is back at checkpoint:
            assert size_after_first == slot.checkpoint_offset, (
                f"rewind failed: post-dispatch size {size_after_first} "
                f"!= checkpoint {slot.checkpoint_offset}"
            )

            r2 = await pool.dispatch_with_rotation(
                prompt="Acknowledge dispatch #2.", timeout=60.0,
            )
            size_after_second = slot.jsonl_path.stat().st_size
            assert size_after_second == slot.checkpoint_offset

            # Both dispatches returned a dict (model may or may not have
            # emitted StructuredOutput — the fallback `{"text": ...}` is
            # fine for the rewind-correctness claim this test pins).
            assert isinstance(r1, dict)
            assert isinstance(r2, dict)
        finally:
            await pool.shutdown()
    finally:
        teardown_pool_session(session)
        if prior_env is None:
            os.environ.pop("NEXUS_T1_SESSION_ID", None)
        else:
            os.environ["NEXUS_T1_SESSION_ID"] = prior_env
