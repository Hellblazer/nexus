# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-233 (bead nexus-7hzb2): ``nx tuple wait``, the channel-free push path,
and the launch-flag read that decides when a session needs it.

The substrate half runs the waiter against a real engine: a post by another
session ends the wait with the channel's notice; a park with nothing new, a
post by the session itself, and mail already consumed do not; a newer waiter
for the same session supersedes an older one. In the ``t2_service_env``
fixture every ``tuple_out`` is stamped with the test's own T1 session
(``NX_T1_SESSION_ID``), so a waiter for THAT session sees its own posts and a
waiter for a fresh id sees another session's.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from click.testing import CliRunner

from nexus import channel_launch
from nexus.commands import tuple_cmd
from nexus.commands.tuple_cmd import EXIT_CHANNEL_ACTIVE, REARM_HINT, tuple_group
from nexus.mailbox_arm import arm_block, mailbox_arm_instruction
from nexus.tuple_wait import (
    DELIVERED,
    PARKS_EXHAUSTED,
    SESSION_CHANGED,
    SUPERSEDED,
    CLAUDE_GONE,
    run_wait,
)

#: Captured at import, before the conftest autouse pin replaces it per test.
_REAL_ANCESTOR_ARGV = channel_launch._claude_ancestor_argv  # noqa: SLF001

#: A hang bound, not a performance assertion.
_HANG_S = 60.0


# ── The launch-flag read ─────────────────────────────────────────────────────


class TestChannelLaunchFlag:
    @pytest.mark.parametrize("argv", [
        "claude --channels plugin:conexus@nexus-plugins",
        "claude --dangerously-load-development-channels server:nexus --resume x",
        "/opt/claude --channels server:nexus",
        "claude --channels=server:nexus",
        "claude --channels server:other plugin:conexus@nexus-plugins --resume x",
        "claude --channels server:other,server:nexus",
    ])
    def test_a_conexus_channel_flag_is_seen(self, argv: str) -> None:
        assert channel_launch.argv_has_channel_flag(argv)

    @pytest.mark.parametrize("argv", [
        "claude",
        "claude --resume 1234",
        "claude --channels server:nexusdev",
        "claude --channels plugin:other@market",
        "claude --channels server:other --resume server:nexus",
        "claude server:nexus",
    ])
    def test_no_conexus_channel_flag_is_seen(self, argv: str) -> None:
        assert not channel_launch.argv_has_channel_flag(argv)

    def test_a_non_claude_parent_is_cannot_tell(self, monkeypatch) -> None:
        """find_immediate_claude_pid falls back to the parent pid; a parent
        that is not claude must read as "cannot tell", not as "no flag"."""
        # the conftest pin replaced the reader; restore the real one for this test
        monkeypatch.setattr(channel_launch, "_claude_ancestor_argv", _REAL_ANCESTOR_ARGV)
        monkeypatch.setattr("nexus.session.find_immediate_claude_pid", lambda: 4242)
        monkeypatch.setattr("nexus.session._command_name_of", lambda _pid: "zsh")
        monkeypatch.setattr(channel_launch, "read_command_line", lambda _pid: "zsh -c nx tuple wait")
        assert channel_launch.channel_launch_state() is None
        monkeypatch.setattr("nexus.session._command_name_of", lambda _pid: "claude")
        assert channel_launch.channel_launch_state() is False

    def test_state_is_three_valued(self) -> None:
        assert channel_launch.channel_launch_state(argv_reader=lambda: "") is None
        assert channel_launch.channel_launch_state(argv_reader=lambda: "claude --resume x") is False
        assert channel_launch.channel_launch_state(
            argv_reader=lambda: "claude --channels plugin:conexus@nexus-plugins",
        ) is True


# ── The SessionStart line ────────────────────────────────────────────────────


class TestSessionStartLine:
    def test_channel_less_line_arms_the_waiter_and_drops_the_channel_claim(self) -> None:
        text = mailbox_arm_instruction("sess-abc", channel=False)
        assert "nx tuple wait" in text and "run_in_background" in text
        assert "again after each wake that says to" in text
        assert "delivered over the channel" not in text
        assert "--channels" not in text
        assert "mailbox/sess-abc is already subscribed" in text

    def test_channel_less_line_fits_the_channel_lines_budget(self) -> None:
        sid = str(uuid.uuid4())
        assert len(mailbox_arm_instruction(sid, channel=False).encode()) <= len(
            mailbox_arm_instruction(sid).encode()
        )

    @pytest.mark.parametrize(("argv", "waiter"), [
        ("claude --resume x", True),
        ("claude --channels plugin:conexus@nexus-plugins", False),
        ("", False),
    ])
    def test_arm_block_picks_the_line_from_the_launch_flag(
        self, monkeypatch, tmp_path, argv: str, waiter: bool,
    ) -> None:
        monkeypatch.setattr("nexus.mailbox_arm.tuple_surface_available", lambda _d: True)
        monkeypatch.setattr("nexus.channel_launch._claude_ancestor_argv", lambda: argv)
        text = arm_block("sess-abc", config_dir=tmp_path)
        assert ("nx tuple wait" in text) is waiter
        assert ("delivered over the channel" in text) is (not waiter)


# ── The loop's stop conditions (no engine needed) ───────────────────────────


class _NeverStore:
    def __init__(self) -> None:
        self.calls = 0

    def wait(self, specs, timeout_s):
        self.calls += 1
        return []


def _row(*, sender_from: str, stamp: str | None, known: bool = True):
    from nexus.db.t2.records import TupleRow  # noqa: PLC0415 — test-local

    return TupleRow(
        id="ab" * 32, subspace="mailbox/sess-a", template="mailbox/<address>", keys={"to": "sess-a"},
        dims={"from": sender_from}, body=None, claim_state=None, claimant=None, lease_until=None,
        attempts=0, consumed_at=None, consumed_by=None, expires_at=None, created_at=None,
        sender_session=stamp, sender_session_known=known,
    )


class TestOwnRow:
    _S = "0f8fad5b-d9cb-469f-a165-70867728950e"

    def test_the_stamp_decides(self) -> None:
        from nexus.tuple_wait import is_own_row  # noqa: PLC0415 — test-local

        assert is_own_row(_row(sender_from="any-name", stamp=self._S), self._S)
        assert is_own_row(_row(sender_from="x", stamp=self._S.upper()), self._S)
        assert not is_own_row(_row(sender_from=self._S, stamp=None), self._S), "a forged from is delivered"
        assert not is_own_row(_row(sender_from=self._S, stamp=str(uuid.uuid4())), self._S)

    def test_a_subagents_mail_is_not_own(self) -> None:
        """Subagents share the parent's token, so their rows carry the parent's
        stamp with an agent-id from: mail TO the parent."""
        from nexus.tuple_wait import is_own_row  # noqa: PLC0415 — test-local

        assert not is_own_row(_row(sender_from="a0123456789abcdef", stamp=self._S), self._S)

    def test_without_a_stamp_the_claimed_from_decides(self) -> None:
        from nexus.tuple_wait import is_own_row  # noqa: PLC0415 — test-local

        assert is_own_row(_row(sender_from=self._S, stamp=None, known=False), self._S)
        assert not is_own_row(_row(sender_from="peer", stamp=None, known=False), self._S)


class TestStopConditions:
    def _run(self, store, **kw):
        defaults = dict(
            board_entries=lambda: [], alive=lambda: True, current_session=lambda: None,
            waiter_token=f"{time.time_ns()}-a", park_s=1, max_parks=3,
        )
        defaults.update(kw)
        return run_wait(store, "sess-a", **defaults)

    def test_a_gone_claude_stops_before_parking(self) -> None:
        store = _NeverStore()
        assert self._run(store, alive=lambda: False).reason == CLAUDE_GONE
        assert store.calls == 0

    def test_a_cleared_session_stops(self) -> None:
        store = _NeverStore()
        assert self._run(store, current_session=lambda: "sess-b").reason == SESSION_CHANGED
        assert store.calls == 0

    def test_a_transient_store_error_is_slept_off(self) -> None:
        from nexus.db.t2.records import TupleRow, WaitResult  # noqa: PLC0415 — test-local

        row = TupleRow(
            id="ab" * 32, subspace="mailbox/sess-a", template="mailbox/<address>", keys={"to": "sess-a"},
            dims={"from": "peer"}, body=None, claim_state=None, claimant=None, lease_until=None,
            attempts=0, consumed_at=None, consumed_by=None, expires_at=None, created_at=None,
            sender_session=None, sender_session_known=True,
        )

        class Flaky:
            calls = 0

            def wait(self, specs, timeout_s):
                Flaky.calls += 1
                if Flaky.calls <= 2:
                    raise ConnectionError("gateway 503")
                return [WaitResult(subspace="mailbox/sess-a", tuples=[row], subscriber="wait:sess-a")]

        naps: list[float] = []
        outcome = self._run(Flaky(), max_parks=5, sleep=naps.append)
        assert outcome.reason == DELIVERED and Flaky.calls == 3
        assert naps == [5.0, 10.0]

    def test_a_full_park_ceiling_is_slept_off(self) -> None:
        from nexus.db.t2.http_tuple_store import ParkCapExceededError  # noqa: PLC0415 — test-local

        class Full:
            calls = 0

            def wait(self, specs, timeout_s):
                Full.calls += 1
                if Full.calls == 1:
                    raise ParkCapExceededError("global park cap")
                return []

        naps: list[float] = []
        outcome = self._run(Full(), max_parks=2, sleep=naps.append)
        assert outcome.reason == PARKS_EXHAUSTED and Full.calls == 2 and naps == [5.0]

    def test_a_store_that_stays_down_ends_the_wait(self) -> None:
        class Down:
            def wait(self, specs, timeout_s):
                raise ConnectionError("down")

        with pytest.raises(ConnectionError):
            self._run(Down(), max_parks=None, sleep=lambda _s: None)

    def test_a_refusal_is_not_retried(self) -> None:
        from nexus.db.t2.http_tuple_store import SchemaViolationError  # noqa: PLC0415 — test-local

        class Refuses:
            calls = 0

            def wait(self, specs, timeout_s):
                Refuses.calls += 1
                raise SchemaViolationError("since refused")

        with pytest.raises(SchemaViolationError):
            self._run(Refuses(), sleep=lambda _s: None)
        assert Refuses.calls == 1

    def test_an_engine_that_ignores_the_subscriber_is_refused(self) -> None:
        from nexus.db.t2.records import WaitResult  # noqa: PLC0415 — test-local
        from nexus.tuple_wait import EngineTooOldError  # noqa: PLC0415 — test-local

        class Old:
            def wait(self, specs, timeout_s):
                return [WaitResult(subspace="mailbox/sess-a", tuples=[_row(sender_from="p", stamp=None)])]

        with pytest.raises(EngineTooOldError):
            self._run(Old())

    def test_empty_parks_run_out(self) -> None:
        store = _NeverStore()
        outcome = self._run(store)
        assert outcome.reason == PARKS_EXHAUSTED and outcome.lines == []
        assert store.calls == 3


# ── Against a real engine ────────────────────────────────────────────────────


def _token() -> str:
    from nexus.mcp.channel import _mint_waiter_token  # noqa: PLC0415,SLF001 — the one token minter

    return _mint_waiter_token()


def _board_entry(topic: str) -> dict[str, str]:
    return {"subspace": f"board/{topic}", "since": datetime.now(UTC).isoformat()}


def _wait(session_id: str, *, entries=(), park_s: int = 1, max_parks: int = 2, token: str | None = None):
    from nexus.db.t2.http_tuple_store import HttpTupleStore  # noqa: PLC0415 — test-local

    store = HttpTupleStore()
    try:
        return run_wait(
            store, session_id,
            board_entries=lambda: list(entries), alive=lambda: True, current_session=lambda: None,
            waiter_token=token or _token(), park_s=park_s, max_parks=max_parks,
        )
    finally:
        store.close()


def _mail(session_id: str, body: str) -> str:
    from nexus.mcp.core import tuple_out  # noqa: PLC0415 — test-local, mirrors test_mcp_channel

    out = tuple_out(
        f"mailbox/{session_id}", {"to": session_id}, {"from": "peer-session"}, body,
        nonce=uuid.uuid4().hex,
    )
    return _tuple_id(out)


def _post(topic: str, body: str, sender: str) -> str:
    from nexus.mcp.core import tuple_out  # noqa: PLC0415 — test-local

    out = tuple_out(f"board/{topic}", {"topic": topic}, {"from": sender}, body, nonce=uuid.uuid4().hex)
    return _tuple_id(out)


def _tuple_id(out: str) -> str:
    import re  # noqa: PLC0415 — stdlib, test-local

    m = re.search(r"\b([0-9a-f]{32,64})\b", out)
    assert m, f"tuple_out returned no id: {out!r}"
    return m.group(1)


# Each scenario below is an independent signal against the real engine, run by
# one journey test so the whole family costs one tenant (nexus-9edxe ratchet).
# The journey runs every scenario and reports each failure by name, so an early
# failure does not hide a later one.


def _engine_mail_from_another_session_wakes_a_parked_waiter() -> None:
    session_id = str(uuid.uuid4())
    result: dict = {}

    def run() -> None:
        result["outcome"] = _wait(session_id, park_s=5, max_parks=6)

    waiter = threading.Thread(target=run, daemon=True)
    started = time.monotonic()
    waiter.start()
    time.sleep(1.0)  # let the first park reach the engine
    tid = _mail(session_id, "wake up")
    waiter.join(_HANG_S)
    assert not waiter.is_alive(), "the waiter never returned"
    outcome = result["outcome"]
    assert outcome.reason == DELIVERED
    assert len(outcome.lines) == 1
    assert f"subspace mailbox/{session_id}, tuple {tid}" in outcome.lines[0]
    assert "wake up" not in outcome.lines[0], "a notice never carries the body"
    # Woken by the post inside one park, not by running out of parks.
    assert time.monotonic() - started < 5.0 + 5.0


def _engine_a_board_post_from_another_session_wakes_the_waiter() -> None:
    topic = f"t{uuid.uuid4().hex[:12]}"
    tid = _post(topic, "news", "peer-session")
    outcome = _wait(str(uuid.uuid4()), entries=[_board_entry(topic)])
    assert outcome.reason == DELIVERED
    assert outcome.lines == [
        line for line in outcome.lines if f"subspace board/{topic}, tuple {tid}" in line
    ] and len(outcome.lines) == 1


def _engine_a_miss_does_not_wake() -> None:
    outcome = _wait(str(uuid.uuid4()), entries=[_board_entry(f"t{uuid.uuid4().hex[:12]}")])
    assert outcome.reason == PARKS_EXHAUSTED and outcome.lines == []


def _engine_own_posts_do_not_wake() -> None:
    own = os.environ["NX_T1_SESSION_ID"]
    topic = f"t{uuid.uuid4().hex[:12]}"
    entries = [_board_entry(topic)]
    _post(topic, "mine", own)
    _mail(own, "note to self")
    outcome = _wait(own, entries=entries)
    assert outcome.reason == PARKS_EXHAUSTED and outcome.lines == []
    assert outcome.own_dropped == 2, "both own rows reached the waiter and were dropped"


def _engine_a_row_is_delivered_once_so_rearming_does_not_repeat_it() -> None:
    session_id = str(uuid.uuid4())
    _mail(session_id, "once")
    assert _wait(session_id).reason == DELIVERED
    assert _wait(session_id, max_parks=1).reason == PARKS_EXHAUSTED


def _engine_mail_already_claimed_does_not_wake() -> None:
    """A row the drain hook (or the session) holds is not claimable, so it
    is not announced; a plain wait would return it (the channel tests pin
    that wait returns claimed rows)."""
    from nexus.db.t2.http_tuple_store import HttpTupleStore  # noqa: PLC0415 — test-local

    session_id = str(uuid.uuid4())
    _mail(session_id, "being drained")
    store = HttpTupleStore()
    try:
        assert store.in_(f"mailbox/{session_id}", {"to": session_id}, claimant="drain", lease_s=60)
        assert _wait(session_id, max_parks=1).reason == PARKS_EXHAUSTED
    finally:
        store.close()


def _engine_a_board_backlog_is_not_replayed() -> None:
    """A topic starts at its subscribe time (less the channel's skew margin),
    as the channel waiter does: older posts are read with tuple_rd."""
    topic = f"t{uuid.uuid4().hex[:12]}"
    _post(topic, "old news", "peer-session")
    later = {"subspace": f"board/{topic}",
             "since": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}
    assert _wait(str(uuid.uuid4()), entries=[later], max_parks=1).reason == PARKS_EXHAUSTED


def _engine_a_newer_waiter_supersedes_an_older_one() -> None:
    session_id = str(uuid.uuid4())
    older = _token()
    newer = _token()
    assert _wait(session_id, max_parks=1, token=newer).reason == PARKS_EXHAUSTED
    assert _wait(session_id, max_parks=1, token=older).reason == SUPERSEDED


# ── The CLI verb ─────────────────────────────────────────────────────────────


class TestWaitSessionResolution:
    def test_explicit_then_marker_then_env(self, monkeypatch) -> None:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-sid")
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        monkeypatch.setattr("nexus.session_marker._read_session_marker", lambda _d, _p: "marker-sid")
        assert tuple_cmd._resolve_wait_session("given", 99) == "given"
        assert tuple_cmd._resolve_wait_session(None, 99) == "marker-sid"
        monkeypatch.setattr("nexus.session_marker._read_session_marker", lambda _d, _p: None)
        assert tuple_cmd._resolve_wait_session(None, 99) == "env-sid"


class TestWaitCli:
    def test_an_old_engine_exits_five(self, monkeypatch) -> None:
        from nexus.tuple_wait import EngineTooOldError  # noqa: PLC0415 — test-local

        class Store:
            def close(self) -> None:
                pass

        def refuse(*_a, **_k):
            raise EngineTooOldError("no echo")

        monkeypatch.setattr(tuple_cmd, "_store", Store)
        monkeypatch.setattr("nexus.tuple_wait.run_wait", refuse)
        result = CliRunner().invoke(tuple_group, ["wait", "--session", "s1", "--max-parks", "1"])
        assert result.exit_code == tuple_cmd.EXIT_ENGINE_TOO_OLD == 5, result.output
        assert tuple_cmd.STOPPED_HINT in result.output and REARM_HINT not in result.output

    @pytest.mark.parametrize(
        ("reason", "says"),
        [
            (SUPERSEDED, "a newer nx tuple wait for this session took over"),
            (SESSION_CHANGED, "this session was replaced"),
            (CLAUDE_GONE, "nothing new arrived"),
        ],
    )
    def test_every_exit_but_a_delivery_says_not_to_rerun(self, monkeypatch, reason, says) -> None:
        """RDR-233 Risks (wake loops): only a delivery asks for a re-run, in text
        and in --json alike."""
        from nexus.tuple_wait import WaitOutcome  # noqa: PLC0415 — test-local

        self._fake_outcome(monkeypatch, WaitOutcome(reason=reason))
        text = CliRunner().invoke(tuple_group, ["wait", "--session", "s1", "--max-parks", "1"])
        assert text.exit_code == 0, text.output
        assert says in text.output and "do not run it again" in text.output.lower()
        assert REARM_HINT not in text.output
        js = CliRunner().invoke(
            tuple_group, ["wait", "--session", "s1", "--max-parks", "1", "--json"],
        )
        payload = json.loads(js.stdout)
        assert payload["reason"] == reason and payload["rerun"] is False
        assert says in payload["hint"] and REARM_HINT not in payload["hint"]

    def test_a_delivery_under_json_says_to_rerun(self, monkeypatch) -> None:
        from nexus.tuple_wait import WaitOutcome  # noqa: PLC0415 — test-local

        self._fake_outcome(monkeypatch, WaitOutcome(reason=DELIVERED, lines=["tuple 7 arrived"]))
        js = CliRunner().invoke(
            tuple_group, ["wait", "--session", "s1", "--max-parks", "1", "--json"],
        )
        payload = json.loads(js.stdout)
        assert payload == {
            "reason": DELIVERED, "notices": ["tuple 7 arrived"], "rerun": True, "hint": REARM_HINT,
        }

    @staticmethod
    def _fake_outcome(monkeypatch, outcome) -> None:
        class Store:
            def close(self) -> None:
                pass

        monkeypatch.setattr(tuple_cmd, "_store", Store)
        monkeypatch.setattr("nexus.tuple_wait.run_wait", lambda *_a, **_k: outcome)

    def test_refuses_where_the_channel_flag_is_present(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "nexus.channel_launch._claude_ancestor_argv",
            lambda: "claude --channels plugin:conexus@nexus-plugins",
        )
        result = CliRunner().invoke(tuple_group, ["wait", "--session", "s1", "--max-parks", "1"])
        assert result.exit_code == EXIT_CHANNEL_ACTIVE, result.output
        assert "Do not run it again" in result.output and "--force" in result.output


def _cli_delivers_then_says_to_rearm(monkeypatch) -> None:
    monkeypatch.setattr("nexus.channel_launch._claude_ancestor_argv", lambda: "claude --resume x")
    monkeypatch.setattr(tuple_cmd, "_board_entries_reader", lambda _sid: (lambda: []))
    session_id = str(uuid.uuid4())
    tid = _mail(session_id, "cli wake")
    result = CliRunner().invoke(
        tuple_group, ["wait", "--session", session_id, "--timeout-s", "1", "--max-parks", "2"],
    )
    assert result.exit_code == 0, result.output
    assert f"tuple {tid}" in result.output
    assert REARM_HINT in result.output


def _cli_nothing_new_exits_zero_and_says_so(monkeypatch) -> None:
    monkeypatch.setattr(tuple_cmd, "_board_entries_reader", lambda _sid: (lambda: []))
    result = CliRunner().invoke(
        tuple_group,
        ["wait", "--session", str(uuid.uuid4()), "--timeout-s", "1", "--max-parks", "1"],
    )
    assert result.exit_code == 0, result.output
    assert "nothing new arrived" in result.output and "Do not run it again" in result.output
    assert REARM_HINT not in result.output


_ENGINE_SCENARIOS = (
    _engine_mail_from_another_session_wakes_a_parked_waiter,
    _engine_a_board_post_from_another_session_wakes_the_waiter,
    _engine_a_miss_does_not_wake,
    _engine_own_posts_do_not_wake,
    _engine_a_row_is_delivered_once_so_rearming_does_not_repeat_it,
    _engine_mail_already_claimed_does_not_wake,
    _engine_a_board_backlog_is_not_replayed,
    _engine_a_newer_waiter_supersedes_an_older_one,
)
_CLI_SCENARIOS = (_cli_delivers_then_says_to_rearm, _cli_nothing_new_exits_zero_and_says_so)


@pytest.mark.usefixtures("t2_service_env")
def test_wait_against_a_real_engine(monkeypatch) -> None:
    """Every real-engine scenario, each isolated by its own session id or topic.
    Failures are collected by scenario name so all of them show in one run."""
    failures: list[str] = []
    for scenario in _ENGINE_SCENARIOS:
        try:
            scenario()
        except AssertionError as exc:
            failures.append(f"{scenario.__name__}: {exc}")
    for scenario in _CLI_SCENARIOS:
        with monkeypatch.context() as mp:
            try:
                scenario(mp)
            except AssertionError as exc:
                failures.append(f"{scenario.__name__}: {exc}")
    assert not failures, "\n".join(failures)
