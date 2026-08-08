# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-d76vc: the MCP T1 handoff-marker watcher (``_t1_handoff_tick``).

Half 2 of the aj564 split-brain fix -- the SessionStart hook (Half 1,
see ``tests/test_hooks.py::TestT1HandoffMarkerWriter``) writes the
marker; this is the reader/re-leaser side, living in ``nexus.mcp.core``.

Idiom mirrors ``tests/db/test_t1_mint_race.py``: REAL marker files
(``nexus.daemon.t1_handoff``) and the REAL flock-guarded
mint-or-borrow + lease-publish machinery in ``nexus.db.t1`` against a
tmp config dir, with only the network-touching
``mint_t1_session_token`` mocked.

Fix-round (code-review-expert pass 1) reshaped ``_t1_handoff_tick`` into
an ``async def`` that claims the marker (atomic rename to a tick-private
path) before processing and offloads the blocking mint to a worker
thread -- every test below runs under ``@pytest.mark.asyncio`` and
``await``s the tick directly.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

import nexus.mcp.core as core
import nexus.mcp_infra as mcp_infra
from nexus.config import nexus_config_dir
from nexus.daemon.t1_handoff import (
    claimed_marker_path,
    handoff_marker_path,
    read_handoff_marker,
    write_handoff_marker,
)

_CLAUDE_PID = 4242
_MCP_PID = 4300


@pytest.fixture(autouse=True)
def _isolate_handoff_globals(monkeypatch):
    """Snapshot/restore every module-global ``_t1_handoff_tick`` touches.

    ``NEXUS_CONFIG_DIR`` is already redirected per-test by the suite's
    autouse ``_isolate_config_dir`` fixture (tests/conftest.py); this
    fixture handles the T1-specific globals ``_t1_handoff_tick`` and its
    neighbours read/write: the refresh task pointer, ownership dict,
    server-start timestamp, env vars, the mcp_infra T1 singleton, and
    (fix-round CRITICAL) the pre-init-hook / deferred-mint state.
    """
    monkeypatch.delenv("NX_T1_SESSION", raising=False)
    monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)
    monkeypatch.setattr(core, "_T1_SESSION_REFRESH_TASK", None)
    monkeypatch.setattr(core, "_OWNED_T1_SESSION", {})
    # 0.0 so any real-clock-written marker reads as "not stale" by
    # default; individual tests override this to exercise the stale path.
    monkeypatch.setattr(core, "_T1_SERVER_START_TIME", 0.0)
    monkeypatch.setattr(core, "_DEFERRED_T1_MINT", {})
    mcp_infra.set_t1_pre_init_hook(None)
    mcp_infra.reset_t1_for_release()
    yield
    task = core._T1_SESSION_REFRESH_TASK
    if task is not None:
        task.cancel()
    core._T1_SESSION_REFRESH_TASK = None
    mcp_infra.set_t1_pre_init_hook(None)
    mcp_infra.reset_t1_for_release()


def _fake_mint(session_id_arg: str, *, context: str = "") -> dict:
    return {"session_token": f"tok-{session_id_arg}", "expires_in_seconds": 3600}


# ── valid marker: consumed + re-lease happens ────────────────────────────────


@pytest.mark.asyncio
async def test_valid_marker_reeleases_and_consumes(monkeypatch) -> None:
    from nexus.db import t1 as t1_mod

    monkeypatch.setattr(t1_mod, "mint_t1_session_token", _fake_mint)
    monkeypatch.setattr(
        "nexus.session.find_immediate_claude_pid", lambda start_pid=None: _CLAUDE_PID,
    )

    config_dir = nexus_config_dir()
    write_handoff_marker(
        _MCP_PID, new_session_id="new-sess", claude_pid=_CLAUDE_PID,
        config_dir=config_dir,
    )

    old_task = MagicMock()
    core._T1_SESSION_REFRESH_TASK = old_task
    os.environ["NX_T1_SESSION_ID"] = "old-sess"
    os.environ["NX_T1_SESSION"] = "old-token"

    sentinel = object()
    mcp_infra.inject_t1(sentinel)

    log = MagicMock()
    await core._t1_handoff_tick(_MCP_PID, log)

    # New lease published: env vars swapped to the new session.
    assert os.environ["NX_T1_SESSION_ID"] == "new-sess"
    assert os.environ["NX_T1_SESSION"] == "tok-new-sess"

    # Old heartbeat stopped.
    old_task.cancel.assert_called_once()
    assert core._T1_SESSION_REFRESH_TASK is not old_task

    # T1 singleton dropped so the next get_t1() reconstructs.
    assert mcp_infra._t1_instance is None

    # Ownership transferred (this call won the mint -- no prior lease).
    assert core._OWNED_T1_SESSION.get("session_id") == "new-sess"

    # Marker consumed: gone from both the live path and the claimed path.
    assert read_handoff_marker(_MCP_PID, config_dir) is None
    assert not claimed_marker_path(_MCP_PID, config_dir).exists()

    # Loud success log.
    log.info.assert_called_once()
    assert log.info.call_args.args[0] == "t1_handoff_released"
    assert log.info.call_args.kwargs["old_session_id"] == "old-sess"
    assert log.info.call_args.kwargs["new_session_id"] == "new-sess"


@pytest.mark.asyncio
async def test_borrowed_lease_does_not_start_a_refresh_task(monkeypatch) -> None:
    """A concurrent sibling already minted the new session's lease: this
    tick BORROWS it (mirrors the original _t1_lifespan mint-race borrow
    branch) and must not take ownership or start its own refresh loop."""
    from nexus.db import t1 as t1_mod

    monkeypatch.setattr(
        "nexus.session.find_immediate_claude_pid", lambda start_pid=None: _CLAUDE_PID,
    )
    config_dir = nexus_config_dir()

    # A lease for the NEW session already exists (published by a sibling).
    t1_mod.publish_t1_session_lease(
        "new-sess", "tok-borrowed", config_dir, ttl_seconds=3600,
    )
    mint_calls: list[str] = []
    monkeypatch.setattr(
        t1_mod, "mint_t1_session_token",
        lambda sid, **kw: mint_calls.append(sid) or _fake_mint(sid),
    )

    write_handoff_marker(
        _MCP_PID, new_session_id="new-sess", claude_pid=_CLAUDE_PID,
        config_dir=config_dir,
    )

    log = MagicMock()
    await core._t1_handoff_tick(_MCP_PID, log)

    assert mint_calls == []  # borrowed, never minted
    assert os.environ["NX_T1_SESSION_ID"] == "new-sess"
    assert os.environ["NX_T1_SESSION"] == "tok-borrowed"
    assert core._T1_SESSION_REFRESH_TASK is None  # no ownership, no refresh loop
    assert "session_id" not in core._OWNED_T1_SESSION


# ── no marker present: silent no-op (steady state) ──────────────────────────


@pytest.mark.asyncio
async def test_no_marker_present_is_a_silent_noop() -> None:
    log = MagicMock()
    await core._t1_handoff_tick(_MCP_PID, log)
    log.warning.assert_not_called()
    log.info.assert_not_called()


# ── ancestry mismatch: rejected, deleted, loud log ──────────────────────────


@pytest.mark.asyncio
async def test_ancestry_mismatch_marker_rejected_and_deleted(monkeypatch) -> None:
    monkeypatch.setattr(
        "nexus.session.find_immediate_claude_pid",
        lambda start_pid=None: 9999,  # does NOT match the marker's claimed pid
    )
    config_dir = nexus_config_dir()
    write_handoff_marker(
        _MCP_PID, new_session_id="new-sess", claude_pid=_CLAUDE_PID,
        config_dir=config_dir,
    )
    os.environ["NX_T1_SESSION_ID"] = "old-sess"

    log = MagicMock()
    await core._t1_handoff_tick(_MCP_PID, log)

    # Rejected: no re-lease happened.
    assert os.environ["NX_T1_SESSION_ID"] == "old-sess"
    assert core._T1_SESSION_REFRESH_TASK is None

    # Deleted -- see _t1_handoff_tick's docstring for why delete (not
    # leave) a rejected marker: a spoof/stale marker gets ONE silent
    # (from the marker's own perspective) rejection loudly logged, and
    # never wedges the watcher into re-warning about it forever.
    assert read_handoff_marker(_MCP_PID, config_dir) is None
    assert not claimed_marker_path(_MCP_PID, config_dir).exists()

    log.warning.assert_called_once()
    assert log.warning.call_args.args[0] == "t1_handoff_marker_rejected"
    assert log.warning.call_args.kwargs["reason"] == "ancestry_mismatch"


@pytest.mark.asyncio
async def test_unresolvable_own_ancestry_rejected(monkeypatch) -> None:
    """own_claude_pid <= 0 (this process's OWN ancestry cannot be
    resolved) must reject too -- there is nothing to compare against."""
    monkeypatch.setattr(
        "nexus.session.find_immediate_claude_pid", lambda start_pid=None: 0,
    )
    config_dir = nexus_config_dir()
    write_handoff_marker(
        _MCP_PID, new_session_id="new-sess", claude_pid=_CLAUDE_PID,
        config_dir=config_dir,
    )

    log = MagicMock()
    await core._t1_handoff_tick(_MCP_PID, log)

    assert read_handoff_marker(_MCP_PID, config_dir) is None
    log.warning.assert_called_once()
    assert log.warning.call_args.kwargs["reason"] == "ancestry_mismatch"


# ── stale / malformed marker: fail-safe, no re-lease, loud log ─────────────


@pytest.mark.asyncio
async def test_stale_marker_rejected_fail_safe(monkeypatch) -> None:
    """A marker written BEFORE this server incarnation started (e.g. a
    leftover from a prior process that reused this pid) must never be
    treated as fresh."""
    monkeypatch.setattr(
        "nexus.session.find_immediate_claude_pid", lambda start_pid=None: _CLAUDE_PID,
    )
    monkeypatch.setattr(core, "_T1_SERVER_START_TIME", 1_000_000.0)
    config_dir = nexus_config_dir()
    write_handoff_marker(
        _MCP_PID, new_session_id="new-sess", claude_pid=_CLAUDE_PID,
        config_dir=config_dir, clock=lambda: 1.0,  # long before server start
    )
    os.environ["NX_T1_SESSION_ID"] = "old-sess"

    log = MagicMock()
    await core._t1_handoff_tick(_MCP_PID, log)

    assert os.environ["NX_T1_SESSION_ID"] == "old-sess"  # no re-lease
    assert read_handoff_marker(_MCP_PID, config_dir) is None  # deleted
    log.warning.assert_called_once()
    assert log.warning.call_args.kwargs["reason"] == "stale_marker"


@pytest.mark.asyncio
async def test_malformed_marker_rejected_fail_safe(monkeypatch) -> None:
    """A marker file that exists but fails to parse (corruption, a
    pre-format-bump leftover) must be rejected loudly, not silently
    treated the same as "no marker at all". Written directly to the LIVE
    path (bypassing write_handoff_marker, which only ever writes valid
    JSON) -- claim_handoff_marker's rename doesn't care about content, so
    this still gets claimed before the parse failure is discovered."""
    config_dir = nexus_config_dir()
    live_path = handoff_marker_path(_MCP_PID, config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    live_path.write_text("not json {{", encoding="utf-8")

    log = MagicMock()
    await core._t1_handoff_tick(_MCP_PID, log)

    assert not live_path.exists()  # claimed away
    assert not claimed_marker_path(_MCP_PID, config_dir).exists()  # then deleted
    log.warning.assert_called_once()
    assert log.warning.call_args.args[0] == "t1_handoff_marker_rejected"
    assert log.warning.call_args.kwargs["reason"] == "malformed_marker"


@pytest.mark.asyncio
async def test_malformed_marker_does_not_raise(monkeypatch) -> None:
    """The primary fail-safe property: a corrupt marker must never crash
    the watcher tick."""
    config_dir = nexus_config_dir()
    live_path = handoff_marker_path(_MCP_PID, config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    live_path.write_text("{}", encoding="utf-8")  # valid JSON, missing fields

    await core._t1_handoff_tick(_MCP_PID, MagicMock())  # must not raise


@pytest.mark.asyncio
async def test_empty_session_id_rejected(monkeypatch) -> None:
    """``read_claimed_marker`` already rejects an empty new_session_id at
    the field-validation layer -- this pins the tick still rejects
    "unknown" specifically (a resolvable-but-meaningless id that would
    otherwise slip past the non-empty check)."""
    monkeypatch.setattr(
        "nexus.session.find_immediate_claude_pid", lambda start_pid=None: _CLAUDE_PID,
    )
    config_dir = nexus_config_dir()
    write_handoff_marker(
        _MCP_PID, new_session_id="unknown", claude_pid=_CLAUDE_PID,
        config_dir=config_dir,
    )

    log = MagicMock()
    await core._t1_handoff_tick(_MCP_PID, log)

    assert read_handoff_marker(_MCP_PID, config_dir) is None
    log.warning.assert_called_once()
    assert log.warning.call_args.kwargs["reason"] == "malformed_session_id"


# ── already on the target scope: consume, no-op ─────────────────────────────


@pytest.mark.asyncio
async def test_marker_naming_the_already_active_session_is_a_quiet_noop(monkeypatch) -> None:
    monkeypatch.setattr(
        "nexus.session.find_immediate_claude_pid", lambda start_pid=None: _CLAUDE_PID,
    )
    config_dir = nexus_config_dir()
    write_handoff_marker(
        _MCP_PID, new_session_id="same-sess", claude_pid=_CLAUDE_PID,
        config_dir=config_dir,
    )
    os.environ["NX_T1_SESSION_ID"] = "same-sess"

    log = MagicMock()
    await core._t1_handoff_tick(_MCP_PID, log)

    assert read_handoff_marker(_MCP_PID, config_dir) is None  # consumed
    log.warning.assert_not_called()
    log.info.assert_not_called()  # not treated as a "release" event


# ── re-lease mint failure: never crash, marker REINSTATED for next tick ────


@pytest.mark.asyncio
async def test_mint_failure_reinstates_marker_at_live_path(monkeypatch) -> None:
    """fix-round IMPORTANT/TOCTOU + event-loop-block interaction: since
    the tick CLAIMS (renames away) the marker before attempting the mint,
    a failed mint must explicitly REINSTATE it at the live path -- doing
    nothing would leave it stranded at the claimed path forever (no
    future tick ever looks there), silently losing the handoff instead of
    retrying it."""
    from nexus.db import t1 as t1_mod

    monkeypatch.setattr(
        "nexus.session.find_immediate_claude_pid", lambda start_pid=None: _CLAUDE_PID,
    )
    def _boom(sid, **kw):
        raise RuntimeError("service unreachable")
    monkeypatch.setattr(t1_mod, "mint_t1_session_token", _boom)

    config_dir = nexus_config_dir()
    write_handoff_marker(
        _MCP_PID, new_session_id="new-sess", claude_pid=_CLAUDE_PID,
        config_dir=config_dir, clock=lambda: 42.0,
    )

    log = MagicMock()
    await core._t1_handoff_tick(_MCP_PID, log)  # must not raise

    # Reinstated at the LIVE path with the SAME content -- a transient
    # failure retries on the next tick, never silently drops the handoff.
    marker = read_handoff_marker(_MCP_PID, config_dir)
    assert marker is not None
    assert marker.new_session_id == "new-sess"
    assert marker.claude_pid == _CLAUDE_PID
    assert marker.written_at == 42.0

    # The claimed copy is gone -- not left behind as a duplicate.
    assert not claimed_marker_path(_MCP_PID, config_dir).exists()

    log.warning.assert_called_once()
    assert log.warning.call_args.args[0] == "t1_handoff_release_failed"

    # No re-lease happened.
    assert "NX_T1_SESSION_ID" not in os.environ


@pytest.mark.asyncio
async def test_reinstated_marker_is_processed_by_a_later_tick(monkeypatch) -> None:
    """The reinstated marker isn't just present on disk -- a SUBSEQUENT
    tick actually picks it up and completes the re-lease."""
    from nexus.db import t1 as t1_mod

    monkeypatch.setattr(
        "nexus.session.find_immediate_claude_pid", lambda start_pid=None: _CLAUDE_PID,
    )
    config_dir = nexus_config_dir()
    write_handoff_marker(
        _MCP_PID, new_session_id="new-sess", claude_pid=_CLAUDE_PID,
        config_dir=config_dir,
    )

    def _boom(sid, **kw):
        raise RuntimeError("service unreachable")
    monkeypatch.setattr(t1_mod, "mint_t1_session_token", _boom)
    log = MagicMock()
    await core._t1_handoff_tick(_MCP_PID, log)  # fails, reinstates

    monkeypatch.setattr(t1_mod, "mint_t1_session_token", _fake_mint)
    await core._t1_handoff_tick(_MCP_PID, log)  # succeeds on retry

    assert os.environ["NX_T1_SESSION_ID"] == "new-sess"
    assert read_handoff_marker(_MCP_PID, config_dir) is None


@pytest.mark.asyncio
async def test_mint_failure_racing_a_newer_marker_does_not_clobber_it(monkeypatch) -> None:
    """fix-round 2 (code-review-expert IMPORTANT, re-review): a mint
    failure whose reinstate races a GENUINELY NEWER marker (a second
    /clear that landed at the live path WHILE the first mint was
    failing/in-flight) must NOT let the stale reinstated content clobber
    it. The newer marker must survive, get processed on its own later
    tick, and the stale one must be dropped with a loud log -- never
    silently.

    Simulated by having the mocked mint function itself write the newer
    marker (standing in for a SessionStart hook whose write genuinely
    races the first tick's blocking mint call) before raising.
    """
    from nexus.db import t1 as t1_mod

    monkeypatch.setattr(
        "nexus.session.find_immediate_claude_pid", lambda start_pid=None: _CLAUDE_PID,
    )
    config_dir = nexus_config_dir()
    write_handoff_marker(
        _MCP_PID, new_session_id="stale-sess", claude_pid=_CLAUDE_PID,
        config_dir=config_dir, clock=lambda: 10.0,
    )

    def _mint_fails_after_a_newer_clear_lands(sid, **kw):
        # A second /clear's SessionStart hook publishes a NEWER marker
        # at the live path while THIS mint is still in flight...
        write_handoff_marker(
            _MCP_PID, new_session_id="fresh-sess", claude_pid=_CLAUDE_PID,
            config_dir=config_dir, clock=lambda: 20.0,
        )
        # ...and then the in-flight mint (for the STALE marker) fails.
        raise RuntimeError("service unreachable")

    monkeypatch.setattr(t1_mod, "mint_t1_session_token", _mint_fails_after_a_newer_clear_lands)

    log = MagicMock()
    await core._t1_handoff_tick(_MCP_PID, log)  # processes "stale-sess", fails, reinstate attempted

    # The reinstate must NOT have clobbered the newer marker.
    marker = read_handoff_marker(_MCP_PID, config_dir)
    assert marker is not None
    assert marker.new_session_id == "fresh-sess"
    assert marker.written_at == 20.0

    # No re-lease happened yet (this tick's own mint failed).
    assert "NX_T1_SESSION_ID" not in os.environ

    # The stale reinstate attempt was dropped loudly, not silently: TWO
    # warnings this tick -- the mint failure itself, and the supersede.
    assert log.warning.call_count == 2
    event_names = [call.args[0] for call in log.warning.call_args_list]
    assert event_names == ["t1_handoff_release_failed", "t1_handoff_marker_reinstate_superseded"]
    supersede_call = log.warning.call_args_list[1]
    assert supersede_call.kwargs["stale_new_session_id"] == "stale-sess"

    # The NEWER marker survives to be processed on ITS OWN later tick.
    monkeypatch.setattr(t1_mod, "mint_t1_session_token", _fake_mint)
    await core._t1_handoff_tick(_MCP_PID, log)

    assert os.environ["NX_T1_SESSION_ID"] == "fresh-sess"
    assert read_handoff_marker(_MCP_PID, config_dir) is None


# ── TOCTOU: a second /clear landing mid-tick is not dropped ────────────────


@pytest.mark.asyncio
async def test_second_clear_landing_mid_tick_is_processed_on_next_tick(monkeypatch) -> None:
    """fix-round IMPORTANT/TOCTOU finding: a second marker write to the
    LIVE path while the first is still being processed (a blocking mint
    in flight) must survive and get its OWN tick -- not be silently
    deleted by the first tick's completion. Simulated by having the
    mocked mint function itself write the second marker, standing in for
    a SessionStart hook whose write genuinely races the first tick's
    blocking mint-or-borrow call."""
    from nexus.db import t1 as t1_mod

    monkeypatch.setattr(
        "nexus.session.find_immediate_claude_pid", lambda start_pid=None: _CLAUDE_PID,
    )
    config_dir = nexus_config_dir()
    write_handoff_marker(
        _MCP_PID, new_session_id="sess-A", claude_pid=_CLAUDE_PID,
        config_dir=config_dir, clock=lambda: 10.0,
    )

    def _mint_that_races_a_second_clear(sid, **kw):
        write_handoff_marker(
            _MCP_PID, new_session_id="sess-B", claude_pid=_CLAUDE_PID,
            config_dir=config_dir, clock=lambda: 20.0,
        )
        return _fake_mint(sid, **kw)

    monkeypatch.setattr(t1_mod, "mint_t1_session_token", _mint_that_races_a_second_clear)

    log = MagicMock()
    await core._t1_handoff_tick(_MCP_PID, log)  # processes A

    assert os.environ["NX_T1_SESSION_ID"] == "sess-A"

    # B survived untouched at the live path, waiting for its own tick --
    # NOT silently deleted by A's completion.
    marker_b = read_handoff_marker(_MCP_PID, config_dir)
    assert marker_b is not None
    assert marker_b.new_session_id == "sess-B"

    # The following tick picks up B with no further racing.
    monkeypatch.setattr(t1_mod, "mint_t1_session_token", _fake_mint)
    await core._t1_handoff_tick(_MCP_PID, log)

    assert os.environ["NX_T1_SESSION_ID"] == "sess-B"
    assert read_handoff_marker(_MCP_PID, config_dir) is None


# ── successful re-lease clears stale pre-init-hook state (fix-round CRITICAL) ─


@pytest.mark.asyncio
async def test_successful_release_clears_stale_brw1s_deferred_mint_hook(monkeypatch) -> None:
    """A stale deferred-mint pre-init hook (registered when the storage
    service was unreachable at MCP startup, for the OLD pre-clear
    session) must NOT fire on the next get_t1() call and silently revert
    NX_T1_SESSION_ID back to the old session -- the exact split-brain
    this bead exists to close."""
    from nexus.db import t1 as t1_mod

    monkeypatch.setattr(t1_mod, "mint_t1_session_token", _fake_mint)
    monkeypatch.setattr(
        "nexus.session.find_immediate_claude_pid", lambda start_pid=None: _CLAUDE_PID,
    )

    config_dir = nexus_config_dir()
    write_handoff_marker(
        _MCP_PID, new_session_id="new-sess", claude_pid=_CLAUDE_PID,
        config_dir=config_dir,
    )
    os.environ["NX_T1_SESSION_ID"] = "old-sess"
    os.environ["NX_T1_SESSION"] = "old-token"

    # A hook that, if it fires, mimics the REAL damage
    # _retry_deferred_t1_mint would do: re-mint/borrow for the OLD
    # session id and overwrite the env vars back to it.
    hook_calls: list[bool] = []

    def _stale_brw1s_hook() -> None:
        hook_calls.append(True)
        os.environ["NX_T1_SESSION_ID"] = "old-sess"
        os.environ["NX_T1_SESSION"] = "old-token-reminted"

    mcp_infra.set_t1_pre_init_hook(_stale_brw1s_hook)
    core._DEFERRED_T1_MINT.update(
        session_id="old-sess", config_dir=config_dir, loop=None,
    )

    log = MagicMock()
    await core._t1_handoff_tick(_MCP_PID, log)

    # Cleared, never invoked.
    assert mcp_infra._t1_pre_init_hook is None
    assert hook_calls == []
    assert core._DEFERRED_T1_MINT == {}

    # Env stays on the NEW session -- not reverted.
    assert os.environ["NX_T1_SESSION_ID"] == "new-sess"

    # The NEXT get_t1() call (which would have run the stale hook FIRST,
    # per mcp_infra.get_t1()'s own contract, had it survived) reconstructs
    # cleanly against the new session with no revert.
    monkeypatch.setattr(
        t1_mod, "get_t1_database",
        lambda: f"constructed-for-{os.environ['NX_T1_SESSION_ID']}",
    )
    result, _ = mcp_infra.get_t1()
    assert result == "constructed-for-new-sess"
    assert hook_calls == []  # still never invoked


@pytest.mark.asyncio
async def test_successful_release_clears_stale_4lkmz_unavailable_hook(monkeypatch) -> None:
    """A stale 'no resolvable session' pre-init hook (registered when
    THIS process started with no resolvable session id at all) must stop
    raising after a successful handoff supplies a perfectly good new
    session id -- an availability regression in the OPPOSITE direction
    from the brw1s case, but the same root cause (nothing but final
    lifespan teardown cleared it)."""
    from nexus.db import t1 as t1_mod

    monkeypatch.setattr(t1_mod, "mint_t1_session_token", _fake_mint)
    monkeypatch.setattr(
        "nexus.session.find_immediate_claude_pid", lambda start_pid=None: _CLAUDE_PID,
    )

    config_dir = nexus_config_dir()
    write_handoff_marker(
        _MCP_PID, new_session_id="new-sess", claude_pid=_CLAUDE_PID,
        config_dir=config_dir,
    )

    # The REAL 4lkmz hook, registered exactly as _t1_lifespan's
    # no-resolvable-session branch does.
    mcp_infra.set_t1_pre_init_hook(core._raise_t1_unavailable_this_process)

    # Sanity: before the handoff, get_t1() genuinely raises (proves the
    # hook is live and this test would fail red without the fix).
    with pytest.raises(core.T1UnavailableThisProcessError):
        mcp_infra.get_t1()

    log = MagicMock()
    await core._t1_handoff_tick(_MCP_PID, log)

    assert mcp_infra._t1_pre_init_hook is None

    # The next get_t1() call WORKS -- no more raise.
    monkeypatch.setattr(
        t1_mod, "get_t1_database",
        lambda: f"constructed-for-{os.environ['NX_T1_SESSION_ID']}",
    )
    result, _ = mcp_infra.get_t1()
    assert result == "constructed-for-new-sess"


# ── event-loop non-blocking (fix-round IMPORTANT) ───────────────────────────


@pytest.mark.asyncio
async def test_slow_mint_does_not_block_the_event_loop(monkeypatch) -> None:
    """The mint-or-borrow call is offloaded to a worker thread
    (asyncio.to_thread): a slow mint must not prevent OTHER coroutines
    scheduled on the same event loop from making progress concurrently."""
    import asyncio
    import time as time_mod

    from nexus.db import t1 as t1_mod

    monkeypatch.setattr(
        "nexus.session.find_immediate_claude_pid", lambda start_pid=None: _CLAUDE_PID,
    )

    def _slow_mint(sid, **kw):
        time_mod.sleep(0.3)  # blocking sleep -- would freeze the loop if run on it
        return _fake_mint(sid, **kw)

    monkeypatch.setattr(t1_mod, "mint_t1_session_token", _slow_mint)

    config_dir = nexus_config_dir()
    write_handoff_marker(
        _MCP_PID, new_session_id="new-sess", claude_pid=_CLAUDE_PID,
        config_dir=config_dir,
    )

    progressed = []

    async def _other_coroutine_progress():
        for _ in range(6):
            await asyncio.sleep(0.05)
            progressed.append(time_mod.monotonic())

    log = MagicMock()
    tick_task = asyncio.create_task(core._t1_handoff_tick(_MCP_PID, log))
    progress_task = asyncio.create_task(_other_coroutine_progress())
    await asyncio.gather(tick_task, progress_task)

    # The "other coroutine" made multiple progress ticks WHILE the slow
    # mint was in flight -- proof the event loop was never blocked by it.
    assert len(progressed) == 6
    assert os.environ["NX_T1_SESSION_ID"] == "new-sess"


# ── subagents follow automatically (MUST-HOLD 4) ────────────────────────────
#
# No dedicated test: subagent-visible MCP tools ride the SAME MCP server
# PROCESS as their parent conversation (Agent-tool dispatches share the
# parent's MCP connection -- there is no separate subagent MCP server
# pid to write a marker for or watch). test_valid_marker_reeleases_and_
# consumes above already proves the re-lease mutates PROCESS-WIDE state
# (os.environ + the mcp_infra._t1_instance singleton) rather than any
# per-caller/per-tool-invocation state, so every tool call sharing that
# process -- parent or dispatched subagent alike -- observes the swapped
# scope identically on its next T1 touch. Asserting that in a dedicated
# test would just re-assert the same os.environ/singleton checks already
# covered above under a different name.
