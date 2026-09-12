# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx tuple`` CLI (RDR-205 Phase 2 Step 2, bead nexus-em75s.10), against
the real engine substrate (``t2_service_env``).

Uses the ``mailbox/<address>`` template loaded at engine boot -- see
``tests/db/test_http_tuple_store.py``'s module docstring for the full
template shapes.
"""
from __future__ import annotations

import json
import os
import uuid

from click.testing import CliRunner

from nexus.commands.tuple_cmd import tuple_group
from nexus.db.t2.http_tuple_store import HttpTupleStore, ParkCapExceededError
from nexus.tuple_watch import (
    WatchConfig,
    acquire_watch_locks,
    lock_path,
    preflight,
    resolve_watch_addresses,
    run_watch,
    state_path,
)


def _uniq(label: str) -> str:
    return f"{label}-{uuid.uuid4().hex[:10]}"


def _invoke(argv: list[str]):
    return CliRunner().invoke(tuple_group, argv)


def _last_json_line(output: str):
    """Parse the LAST non-empty line of *output* as JSON.

    A structlog warning line (e.g. the guard_production_write opt-in
    notice) can land on stdout ahead of the command's own JSON line;
    the JSON payload is always the command's final ``click.echo``.
    """
    lines = [line for line in output.splitlines() if line.strip()]
    return json.loads(lines[-1])


class TestTupleOutRd:
    def test_out_then_rd_round_trips(self, t2_service_env) -> None:
        addr = _uniq("addr")
        out = _invoke([
            "out", f"mailbox/{addr}",
            "--key", f"to={addr}", "--dim", "from=sender-a", "--body", "hello",
            "--nonce", _uniq("nonce"),
        ])
        assert out.exit_code == 0, out.output
        tuple_id = [line for line in out.output.splitlines() if line.strip()][-1].strip()
        assert len(tuple_id) == 64

        rd = _invoke(["rd", f"mailbox/{addr}", "--pattern", f"to={addr}", "--json"])
        assert rd.exit_code == 0, rd.output
        rows = _last_json_line(rd.output)
        assert len(rows) == 1
        assert rows[0]["id"] == tuple_id
        assert rows[0]["body"] == "hello"
        assert rows[0]["claim_state"] is None

    def test_rd_no_match_reports_none(self, t2_service_env) -> None:
        addr = _uniq("addr")
        rd = _invoke(["rd", f"mailbox/{addr}", "--pattern", f"to={addr}"])
        assert rd.exit_code == 0, rd.output
        assert "No matching tuples" in rd.output

    def test_rd_against_unknown_subspace_exits_nonzero(self, t2_service_env) -> None:
        rd = _invoke(["rd", "no-such-subspace/xyz"])
        assert rd.exit_code == 1
        assert "Error" in rd.output


class TestTupleInAckNack:
    def test_in_then_ack(self, t2_service_env) -> None:
        addr = _uniq("addr")
        _invoke([
            "out", f"mailbox/{addr}", "--key", f"to={addr}",
            "--dim", "from=sender-b", "--body", "payload", "--nonce", _uniq("nonce"),
        ])
        claimant = _uniq("claimant")
        inr = _invoke([
            "in", f"mailbox/{addr}", "--pattern", f"to={addr}",
            "--claimant", claimant, "--lease-s", "30", "--json",
        ])
        assert inr.exit_code == 0, inr.output
        payload = _last_json_line(inr.output)
        claim_id = payload["claim_id"]
        assert payload["tuple"]["body"] == "payload"

        ack = _invoke(["ack", claim_id, "--claimant", claimant])
        assert ack.exit_code == 0, ack.output
        assert "Acked" in ack.output

    def test_in_probe_miss_exits_nonzero(self, t2_service_env) -> None:
        addr = _uniq("addr")
        inr = _invoke([
            "in", f"mailbox/{addr}", "--pattern", f"to={addr}",
            "--claimant", _uniq("claimant"), "--lease-s", "30",
        ])
        assert inr.exit_code == 1
        assert "No matching tuple" in inr.output

    def test_nack_releases_for_a_retake(self, t2_service_env) -> None:
        addr = _uniq("addr")
        _invoke([
            "out", f"mailbox/{addr}", "--key", f"to={addr}",
            "--dim", "from=sender-c", "--body", "retry-me", "--nonce", _uniq("nonce"),
        ])
        claimant = _uniq("claimant")
        inr = _invoke([
            "in", f"mailbox/{addr}", "--pattern", f"to={addr}",
            "--claimant", claimant, "--lease-s", "30", "--json",
        ])
        claim_id = _last_json_line(inr.output)["claim_id"]

        nack = _invoke(["nack", claim_id, "--claimant", claimant])
        assert nack.exit_code == 0, nack.output
        assert "Nacked" in nack.output

        inr2 = _invoke([
            "in", f"mailbox/{addr}", "--pattern", f"to={addr}",
            "--claimant", _uniq("claimant2"), "--lease-s", "30", "--json",
        ])
        assert inr2.exit_code == 0
        assert _last_json_line(inr2.output)["tuple"]["body"] == "retry-me"

    def test_ack_unknown_claim_exits_nonzero(self, t2_service_env) -> None:
        ack = _invoke(["ack", "0" * 64, "--claimant", "nobody"])
        assert ack.exit_code == 1
        assert "Error" in ack.output


class TestTupleTemplatesListStats:
    def test_templates_lists_the_registered_templates(self, t2_service_env) -> None:
        out = _invoke(["templates", "--json"])
        assert out.exit_code == 0, out.output
        reg = _last_json_line(out.output)
        names = {t.get("name") for t in reg["templates"]}
        assert "mailbox/<address>" in names
        assert "ledger/<session_id>" in names

    def test_list_and_stats_reflect_a_written_tuple(self, t2_service_env) -> None:
        addr = _uniq("addr")
        _invoke([
            "out", f"mailbox/{addr}", "--key", f"to={addr}",
            "--dim", "from=sender-d", "--body", "x", "--nonce", _uniq("nonce"),
        ])

        lst = _invoke(["list", "--prefix", f"mailbox/{addr}", "--json"])
        assert lst.exit_code == 0, lst.output
        rows = _last_json_line(lst.output)
        assert len(rows) == 1
        assert rows[0]["available"] == 1

        stats = _invoke(["stats", f"mailbox/{addr}", "--json"])
        assert stats.exit_code == 0, stats.output
        census = _last_json_line(stats.output)
        assert census["total"] == 1
        assert census["available"] == 1

    def test_stats_on_never_written_subspace_is_zero(self, t2_service_env) -> None:
        addr = _uniq("addr")
        stats = _invoke(["stats", f"mailbox/{addr}", "--json"])
        assert stats.exit_code == 0
        census = _last_json_line(stats.output)
        assert census["total"] == 0
        assert census["available"] == 0


class TestKvParsing:
    def test_out_rejects_malformed_key(self, t2_service_env) -> None:
        out = _invoke(["out", "mailbox/x", "--key", "no-equals-sign"])
        assert out.exit_code != 0


# ── nx tuple watch (MM-1.1, bead nexus-6konb.2) ─────────────────────────────
#
# The watcher's loop is ``nexus.tuple_watch.run_watch``; these tests drive it
# against the real engine substrate with an injected clock and a no-op sleep so
# the 10-minute re-emit window is crossed by moving the clock, not by waiting.
# ``emit`` captures what would be stdout (every line of which is a Monitor
# notification); ``report`` captures what would be stderr.


class _Clock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _watch_env(tmp_path):
    return HttpTupleStore(), WatchConfig(interval_s=1.0, reemit_after_s=600.0, max_emits=3), tmp_path


def _run(store, cfg, state_dir, addr, clock, iterations, lines, reports):
    return run_watch(
        store, [addr], config=cfg, state_dir=state_dir, iterations=iterations,
        emit=lines.append, report=reports.append, now=clock.now, sleep=lambda _s: None,
    )


def _out(store, addr, *, sender="sender-w", body="hi", kind="note"):
    return store.out(
        f"mailbox/{addr}", {"to": addr}, dims={"from": sender, "kind": kind},
        body=body, nonce=_uniq("nonce"),
    )


class TestTupleWatch:
    def test_empty_mailbox_ten_iterations_emits_nothing(self, t2_service_env, tmp_path) -> None:
        store, cfg, sd = _watch_env(tmp_path)
        lines, reports, clock = [], [], _Clock()
        stats = _run(store, cfg, sd, _uniq("addr"), clock, 10, lines, reports)
        assert stats.cycles == 10
        assert lines == []

    def test_one_out_emits_exactly_one_ping_with_sender_and_id(self, t2_service_env, tmp_path) -> None:
        store, cfg, sd = _watch_env(tmp_path)
        addr = _uniq("addr")
        tid = _out(store, addr, sender="alice", kind="request", body="secret-body-xyz")
        lines, reports, clock = [], [], _Clock()
        _run(store, cfg, sd, addr, clock, 1, lines, reports)
        assert len(lines) == 1
        assert f"mailbox/{addr}" in lines[0]
        assert "from=alice" in lines[0]
        assert "kind=request" in lines[0]
        assert tid in lines[0]
        assert "secret-body-xyz" not in lines[0]  # the body never rides the ping
        assert "address-wide" in lines[0]

    def test_same_row_on_next_five_iterations_is_not_re_pinged(self, t2_service_env, tmp_path) -> None:
        store, cfg, sd = _watch_env(tmp_path)
        addr = _uniq("addr")
        _out(store, addr)
        lines, reports, clock = [], [], _Clock()
        for _ in range(6):
            _run(store, cfg, sd, addr, clock, 1, lines, reports)
            clock.advance(cfg.interval_s)
        assert len(lines) == 1

    def test_reemits_after_window_and_caps_at_max_emits(self, t2_service_env, tmp_path) -> None:
        store, cfg, sd = _watch_env(tmp_path)
        addr = _uniq("addr")
        _out(store, addr)
        lines, reports, clock = [], [], _Clock()
        _run(store, cfg, sd, addr, clock, 1, lines, reports)
        assert len(lines) == 1
        clock.advance(cfg.reemit_after_s + 1)
        _run(store, cfg, sd, addr, clock, 1, lines, reports)
        assert len(lines) == 2
        clock.advance(cfg.reemit_after_s + 1)
        _run(store, cfg, sd, addr, clock, 1, lines, reports)
        assert len(lines) == 3
        clock.advance(cfg.reemit_after_s + 1)
        stats = _run(store, cfg, sd, addr, clock, 1, lines, reports)
        assert len(lines) == 3
        assert stats.suppressed == 1

    def test_row_acked_by_another_actor_is_never_pinged_again(self, t2_service_env, tmp_path) -> None:
        store, cfg, sd = _watch_env(tmp_path)
        addr = _uniq("addr")
        _out(store, addr)
        lines, reports, clock = [], [], _Clock()
        _run(store, cfg, sd, addr, clock, 1, lines, reports)
        assert len(lines) == 1
        claimant = _uniq("drainer")
        claimed = store.in_(f"mailbox/{addr}", {"to": addr}, claimant=claimant, lease_s=30)
        assert claimed is not None
        store.ack(claimed[1], claimant)
        clock.advance(cfg.reemit_after_s + 1)
        _run(store, cfg, sd, addr, clock, 3, lines, reports)
        assert len(lines) == 1

    def test_live_claim_is_present_not_consumed(self, t2_service_env, tmp_path) -> None:
        store, cfg, sd = _watch_env(tmp_path)
        addr = _uniq("addr")
        _out(store, addr)
        lines, reports, clock = [], [], _Clock()
        _run(store, cfg, sd, addr, clock, 1, lines, reports)
        assert len(lines) == 1
        claimed = store.in_(f"mailbox/{addr}", {"to": addr}, claimant=_uniq("holder"), lease_s=300)
        assert claimed is not None
        # rd still returns the claimed row (claim is not consumption) ...
        assert [r.id for r in store.rd(f"mailbox/{addr}", {"to": addr}, n=10)] == [claimed[0].id]
        # ... and the watcher neither re-pings it inside the window ...
        for _ in range(3):
            clock.advance(cfg.interval_s)
            _run(store, cfg, sd, addr, clock, 1, lines, reports)
        assert len(lines) == 1
        # ... nor forgets it: past the window it re-pings, because it is still there.
        clock.advance(cfg.reemit_after_s + 1)
        _run(store, cfg, sd, addr, clock, 1, lines, reports)
        assert len(lines) == 2

    def test_seen_set_file_deleted_mid_run_re_pings_without_crashing(self, t2_service_env, tmp_path) -> None:
        store, cfg, sd = _watch_env(tmp_path)
        addr = _uniq("addr")
        _out(store, addr)
        lines, reports, clock = [], [], _Clock()
        _run(store, cfg, sd, addr, clock, 1, lines, reports)
        assert len(lines) == 1
        p = state_path(sd, addr)
        assert p.is_file()
        p.unlink()
        clock.advance(cfg.interval_s)
        _run(store, cfg, sd, addr, clock, 1, lines, reports)
        assert len(lines) == 2
        assert p.is_file()

    def test_multi_row_probe_emits_one_line_per_tuple(self, t2_service_env, tmp_path) -> None:
        store, cfg, sd = _watch_env(tmp_path)
        addr = _uniq("addr")
        ids = {_out(store, addr, sender=f"s{i}") for i in range(3)}
        lines, reports, clock = [], [], _Clock()
        _run(store, cfg, sd, addr, clock, 1, lines, reports)
        assert len(lines) == 3
        assert {next(t for t in ids if t in line) for line in lines} == ids

    def test_burst_beyond_per_cycle_cap_coalesces(self, t2_service_env, tmp_path) -> None:
        store, cfg, sd = _watch_env(tmp_path)
        addr = _uniq("addr")
        for i in range(cfg.max_lines_per_cycle + 4):
            _out(store, addr, sender=f"s{i}")
        lines, reports, clock = [], [], _Clock()
        stats = _run(store, cfg, sd, addr, clock, 1, lines, reports)
        assert len(lines) == cfg.max_lines_per_cycle + 1
        assert "4 more" in lines[-1]
        assert stats.coalesced == 4
        # every row was recorded as pinged: the next cycle inside the window is silent
        clock.advance(cfg.interval_s)
        _run(store, cfg, sd, addr, clock, 1, lines, reports)
        assert len(lines) == cfg.max_lines_per_cycle + 1

    def test_dead_row_at_head_does_not_hide_fresh_mail(self, t2_service_env, tmp_path) -> None:
        store, cfg, sd = _watch_env(tmp_path)
        addr = _uniq("addr")
        sub = f"mailbox/{addr}"
        dead_id = _out(store, addr, sender="poison")
        for _ in range(3):  # mailbox.yaml max_attempts=3: the third nack dead-letters it
            claimant = _uniq("c")
            claimed = store.in_(sub, {"to": addr}, claimant=claimant, lease_s=30)
            assert claimed is not None and claimed[0].id == dead_id
            store.nack(claimed[1], claimant)
        # dead-lettered: still readable, never claimable
        assert store.inp(sub, {"to": addr}, claimant=_uniq("c"), lease_s=30) is None
        fresh_id = _out(store, addr, sender="alice")

        # Non-vacuity: at n=1 the dead row is the whole answer and the fresh row is hidden;
        # without the claim_state filter the dead row would be pinged as mail.
        head = store.rd(sub, {"to": addr}, n=1)
        assert [r.id for r in head] == [dead_id]
        assert head[0].claim_state == "dead"

        lines, reports, clock = [], [], _Clock()
        stats = _run(store, cfg, sd, addr, clock, 1, lines, reports)
        pings = [line for line in lines if "new mail" in line]
        assert len(pings) == 1
        assert fresh_id in pings[0]
        assert dead_id not in pings[0]
        assert stats.dead_seen == 1
        # The watcher never saw this row alive, so its death is news of mail that will
        # never be delivered: it belongs on the stream the Monitor reads, not stderr.
        dead_lines = [line for line in lines if dead_id in line]
        assert len(dead_lines) == 1
        assert "never be claimed" in dead_lines[0]
        assert not [r for r in reports if dead_id in r]
        # reported once, not once per cycle (the re-emit window has not elapsed)
        clock.advance(cfg.interval_s)
        _run(store, cfg, sd, addr, clock, 2, lines, reports)
        assert sum(dead_id in line for line in lines) == 1

    def test_cli_wiring_emits_ping_and_exits_zero(self, t2_service_env, tmp_path) -> None:
        store, _cfg, sd = _watch_env(tmp_path)
        addr = _uniq("addr")
        tid = _out(store, addr, sender="cli-sender")
        res = _invoke([
            "watch", addr, "--iterations", "2", "--interval", "0", "--state-dir", str(sd),
        ])
        assert res.exit_code == 0, res.output
        pings = [line for line in res.output.splitlines() if "nx-tuple-watch:" in line]
        assert len(pings) == 1
        assert tid in pings[0]

    def test_probe_failure_reported_once_then_recovery_once(self, t2_service_env, tmp_path) -> None:
        store, cfg, sd = _watch_env(tmp_path)
        addr = _uniq("addr")

        class _Flaky:
            """Fails the first three probes with the same error, then delegates."""

            def __init__(self, inner, failures: int) -> None:
                self.inner, self.left = inner, failures

            def rd(self, *a, **kw):
                if self.left > 0:
                    self.left -= 1
                    raise RuntimeError("engine unreachable")
                return self.inner.rd(*a, **kw)

        lines, reports, clock = [], [], _Clock()
        stats = _run(_Flaky(store, 3), cfg, sd, addr, clock, 5, lines, reports)
        assert stats.probe_errors == 3
        assert stats.cycles == 5
        # MM-1.2: the outage line goes to STDOUT -- the stream the Monitor watches --
        # because an outage the session cannot see is the silent no-op the failure
        # visibility rule exists to kill. Recovery stays on stderr. No PING is emitted.
        assert not [line for line in lines if "new mail" in line]
        failed = [line for line in lines if "probe failed" in line]
        recovered = [line for line in lines if "probe recovered" in line]
        assert len(failed) == 1 and "engine unreachable" in failed[0]
        # the recovery line shares the outage line's stream: the outage line claims a
        # CONTINUING condition, so a reader who cannot see the end of it is left
        # inferring recovery from silence
        assert len(recovered) == 1
        assert not [r for r in reports if "probe recovered" in r]

    def test_sustained_flood_collapses_to_one_line_per_cycle(self, t2_service_env, tmp_path) -> None:
        store, cfg, sd = _watch_env(tmp_path)
        addr = _uniq("addr")
        per_cycle = 3

        class _Flooding:
            """Three fresh tuples land before every probe: a sustained flood."""

            def __init__(self, inner) -> None:
                self.inner, self.cycle = inner, 0

            def rd(self, *a, **kw):
                clock.advance(cfg.interval_s)  # the clock moves between probes, as in life
                for i in range(per_cycle):
                    _out(self.inner, addr, sender=f"c{self.cycle}s{i}")
                self.cycle += 1
                return self.inner.rd(*a, **kw)

        lines, reports, clock = [], [], _Clock()
        stats = _run(_Flooding(store), cfg, sd, addr, clock, 4, lines, reports)
        # budget_lines=8 per 20 s: cycles 1 and 2 ping per tuple (6 lines), cycles 3
        # and 4 each collapse to one budget line -> 8 lines for 12 tuples.
        assert len(lines) == 8, lines
        assert sum("ping budget reached" in line for line in lines) == 2
        assert "3 new mail" in lines[-1]
        assert stats.pinged == 12 and stats.budget_coalesced == 6
        # every tuple was recorded as pinged: a further cycle inside the window is silent
        _run(store, cfg, sd, addr, clock, 1, lines, reports)
        assert len(lines) == 8
        # the budget is a window, not a lifetime cap: once it slides past the earlier
        # emissions, a new batch pings per tuple again (a non-expiring cap would coalesce)
        clock.advance(cfg.budget_window_s + 1)
        for i in range(per_cycle):
            _out(store, addr, sender=f"late{i}")
        _run(store, cfg, sd, addr, clock, 1, lines, reports)
        assert len(lines) == 11
        assert not any("ping budget reached" in line for line in lines[8:])

    def test_state_save_failure_on_one_address_does_not_stop_the_other(self, t2_service_env, tmp_path) -> None:
        store, cfg, sd = _watch_env(tmp_path)
        bad, good = _uniq("bad"), _uniq("good")
        tid = _out(store, good, sender="alice")
        # make the bad address's state path unwritable: a FILE where its parent dir must be
        (sd / "tuple-watch").mkdir()
        p = state_path(sd, bad)
        p.mkdir()  # a directory where the state file should be: write fails, load treats as empty
        lines, reports, clock = [], [], _Clock()
        stats = run_watch(
            store, [bad, good], config=cfg, state_dir=sd, iterations=2,
            emit=lines.append, report=reports.append, now=clock.now, sleep=lambda _s: None,
        )
        assert stats.cycles == 2
        assert stats.probe_errors == 2
        pings = [line for line in lines if "new mail" in line]
        assert len(pings) == 1 and tid in pings[0]
        # MM-1.2: the bad address's failure is reported on stdout, once per window
        assert sum("probe failed" in line and bad in line for line in lines) == 1


# ── nx tuple watch: preflight, failure visibility, locking (MM-1.2, nexus-6konb.3) ──


class _Boom(Exception):
    """A transport-shaped failure for the preflight and outage tests."""


class TestTupleWatchPreflight:
    def test_unreachable_engine_reports_one_skip_line_and_never_loops(self, tmp_path) -> None:
        class _Down:
            def registry(self):
                raise _Boom("connection refused")

        lines, reports = [], []
        result = preflight(_Down(), ["addr-a"], config=WatchConfig(), emit=lines.append)
        assert result.ok is False
        assert "connection refused" in result.detail
        assert len(lines) == 1
        assert "SKIP" in lines[0]
        assert "connection refused" in lines[0]

    def test_unreadable_mailbox_is_a_skip_naming_the_address(self, tmp_path) -> None:
        """The PER-ADDRESS branch: the registry answers, the mailbox does not."""

        class _HalfUp:
            def registry(self):
                return {"digest": "d", "templates": []}

            def subspace_stats(self, subspace):
                raise _Boom(f"404 no such subspace {subspace}")

        lines = []
        result = preflight(_HalfUp(), ["addr-a", "addr-b"], config=WatchConfig(),
                           emit=lines.append)
        assert result.ok is False
        assert "404" in result.detail
        assert len(lines) == 1  # the FIRST address stops it; no line per address
        assert "SKIP" in lines[0]
        assert "mailbox/addr-a" in lines[0]

    def test_dead_backlog_near_the_probe_cap_warns_with_the_count(self, tmp_path) -> None:
        class _Census:
            dead, total, available, claimed = 9, 10, 1, 0

        class _Loaded:
            def registry(self):
                return {"digest": "d", "templates": []}

            def subspace_stats(self, subspace):
                return _Census()

        lines, reports = [], []
        result = preflight(_Loaded(), ["addr-a"], config=WatchConfig(probe_n=10),
                           emit=lines.append)
        assert result.ok is True
        warn = [line for line in lines if "dead" in line]
        assert len(warn) == 1
        assert "9" in warn[0] and "10" in warn[0]

    def test_healthy_engine_preflights_clean_and_silent(self, t2_service_env, tmp_path) -> None:
        store, cfg, _sd = _watch_env(tmp_path)
        lines, reports = [], []
        result = preflight(store, [_uniq("addr")], config=cfg, emit=lines.append)
        assert result.ok is True
        assert lines == []


class TestTupleWatchFailureVisibility:
    def test_sustained_outage_is_rate_limited_not_silent_and_not_a_line_per_cycle(
        self, tmp_path,
    ) -> None:
        class _Dead:
            def rd(self, *a, **kw):
                raise _Boom("engine went away")

        cfg = WatchConfig(interval_s=1.0, error_report_every_s=300.0)
        lines, reports, clock = [], [], _Clock()

        class _Ticking(_Dead):
            def rd(self, *a, **kw):
                clock.advance(cfg.interval_s)
                return super().rd(*a, **kw)

        stats = run_watch(
            _Ticking(), ["addr-a"], config=cfg, state_dir=tmp_path, iterations=120,
            emit=lines.append, report=reports.append, now=clock.now, sleep=lambda _s: None,
        )
        assert stats.probe_errors == 120
        # 120 cycles x 1 s = 120 s of outage: one line at once, and nothing else
        # inside the 300 s window. Silence would be the bug; a line per cycle would
        # trip the measured auto-stop.
        outage = [line for line in lines if "probe failed" in line]
        assert len(outage) == 1
        assert "engine went away" in outage[0]

    def test_outage_re_reports_once_the_window_passes(self, tmp_path) -> None:
        cfg = WatchConfig(interval_s=1.0, error_report_every_s=60.0)
        lines, reports, clock = [], [], _Clock()

        class _Dead:
            def rd(self, *a, **kw):
                clock.advance(30.0)
                raise _Boom("still down")

        run_watch(
            _Dead(), ["addr-a"], config=cfg, state_dir=tmp_path, iterations=6,
            emit=lines.append, report=reports.append, now=clock.now, sleep=lambda _s: None,
        )
        # 6 cycles x 30 s = 180 s at a 60 s window -> 3 reports, not 6 and not 1
        assert len([line for line in lines if "probe failed" in line]) == 3

    def test_a_changed_error_is_reported_immediately_inside_the_window(self, tmp_path) -> None:
        cfg = WatchConfig(interval_s=1.0, error_report_every_s=3600.0)
        lines, reports, clock = [], [], _Clock()

        class _Changing:
            def __init__(self) -> None:
                self.n = 0

            def rd(self, *a, **kw):
                self.n += 1
                raise _Boom("transport blip" if self.n < 3 else "401 unauthorized")

        run_watch(
            _Changing(), ["addr-a"], config=cfg, state_dir=tmp_path, iterations=4,
            emit=lines.append, report=reports.append, now=clock.now, sleep=lambda _s: None,
        )
        outage = [line for line in lines if "probe failed" in line]
        assert len(outage) == 2, outage
        assert "transport blip" in outage[0]
        assert "401 unauthorized" in outage[1]

    def test_park_cap_exceeded_is_named_because_this_watcher_never_parks(self, tmp_path) -> None:
        class _Parked:
            def rd(self, *a, **kw):
                raise ParkCapExceededError("global park cap reached")

        lines, reports, clock = [], [], _Clock()
        run_watch(
            _Parked(), ["addr-a"], config=WatchConfig(), state_dir=tmp_path, iterations=1,
            emit=lines.append, report=reports.append, now=clock.now, sleep=lambda _s: None,
        )
        outage = [line for line in lines if "probe failed" in line]
        assert len(outage) == 1
        assert "never parks" in outage[0]


class TestTupleWatchLock:
    def test_second_watcher_on_the_same_address_refuses_naming_the_holder(
        self, tmp_path, monkeypatch,
    ) -> None:
        addr = _uniq("addr")
        monkeypatch.setenv("NX_SESSION_ID", "session-one")
        held_lines: list[str] = []
        first = acquire_watch_locks([addr], state_dir=tmp_path, emit=held_lines.append)
        assert first.ok is True
        assert held_lines == []
        try:
            # A DIFFERENT session id: a per-session lock would let this through,
            # which is exactly the /clear case the machine-wide scope exists for.
            monkeypatch.setenv("NX_SESSION_ID", "session-two")
            lines = []
            second = acquire_watch_locks([addr], state_dir=tmp_path, emit=lines.append)
            assert second.ok is False
            assert len(lines) == 1
            assert str(os.getpid()) in lines[0]
            assert "session-one" in lines[0]
        finally:
            first.release()

    def test_lock_released_by_a_dead_holder_is_acquired_not_refused(self, tmp_path) -> None:
        addr = _uniq("addr")
        held_lines: list[str] = []
        first = acquire_watch_locks([addr], state_dir=tmp_path, emit=held_lines.append)
        assert first.ok is True
        assert held_lines == []
        first.release()  # what a dying process's OS-released flock leaves behind
        lines = []
        second = acquire_watch_locks([addr], state_dir=tmp_path, emit=lines.append)
        assert second.ok is True
        assert lines == []
        second.release()
    def test_a_repeated_address_does_not_refuse_itself(self, tmp_path) -> None:
        addr = _uniq("addr")
        lines = []
        locks = acquire_watch_locks([addr, addr], state_dir=tmp_path, emit=lines.append)
        try:
            assert locks.ok is True, lines
            assert lines == []
            assert len(locks.holders) == 1
        finally:
            locks.release()

    def test_refusing_one_address_releases_the_ones_already_taken(self, tmp_path) -> None:
        free, taken = _uniq("free"), _uniq("taken")
        holder = acquire_watch_locks([taken], state_dir=tmp_path, emit=lambda _s: None)
        assert holder.ok is True
        try:
            second = acquire_watch_locks([free, taken], state_dir=tmp_path, emit=lambda _s: None)
            assert second.ok is False
            assert second.refused_address == taken
            # the partial acquisition must not linger: a third watcher gets `free`
            third = acquire_watch_locks([free], state_dir=tmp_path, emit=lambda _s: None)
            assert third.ok is True
            third.release()
        finally:
            holder.release()


class TestWatchAddressIsNeverReResolved:
    def test_the_watched_address_never_re_resolves_mid_run(self, t2_service_env, tmp_path,
                                                           monkeypatch) -> None:
        store, cfg, sd = _watch_env(tmp_path)
        addr = _uniq("addr")
        tid = _out(store, addr, sender="alice")
        cfgdir = tmp_path / "cfg"
        cfgdir.mkdir()
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfgdir))
        (cfgdir / "current_session").write_text("session-before", encoding="utf-8")
        lines, reports, clock = [], [], _Clock()

        class _Clobbering:
            """A peer session rewrites the machine-wide file between probes."""

            def __init__(self, inner) -> None:
                self.inner = inner

            def rd(self, subspace, *a, **kw):
                (cfgdir / "current_session").write_text(_uniq("peer"), encoding="utf-8")
                return self.inner.rd(subspace, *a, **kw)

        run_watch(
            _Clobbering(store), [addr], config=cfg, state_dir=sd, iterations=3,
            emit=lines.append, report=reports.append, now=clock.now, sleep=lambda _s: None,
        )
        assert len(lines) == 1
        assert tid in lines[0]


class TestTupleWatchCliGuards:
    """The three guards through the real command, not the raw functions."""

    def test_cli_skips_with_one_line_when_the_engine_is_unreachable(
        self, t2_service_env, tmp_path, monkeypatch,
    ) -> None:
        monkeypatch.setenv("NX_SERVICE_PORT", "1")  # nothing listens on port 1
        monkeypatch.setenv("NX_SERVICE_URL", "http://127.0.0.1:1")
        res = _invoke([
            "watch", _uniq("addr"), "--iterations", "3", "--interval", "0",
            "--state-dir", str(tmp_path),
        ])
        assert res.exit_code == 0, res.output
        skips = [line for line in res.output.splitlines() if "SKIP" in line]
        assert len(skips) == 1, res.output
        assert not [line for line in res.output.splitlines() if "new mail" in line]

    def test_cli_refuses_when_another_watcher_holds_the_address(
        self, t2_service_env, tmp_path, monkeypatch,
    ) -> None:
        store, _cfg, sd = _watch_env(tmp_path)
        addr = _uniq("addr")
        _out(store, addr, sender="alice")
        monkeypatch.setenv("NX_SESSION_ID", "holder-session")
        holder = acquire_watch_locks([addr], state_dir=sd, emit=lambda _s: None)
        assert holder.ok is True
        try:
            monkeypatch.setenv("NX_SESSION_ID", "second-session")
            res = _invoke([
                "watch", addr, "--iterations", "2", "--interval", "0", "--state-dir", str(sd),
            ])
            assert res.exit_code == 0, res.output
            refusals = [line for line in res.output.splitlines() if "already watched by" in line]
            assert len(refusals) == 1, res.output
            assert "holder-session" in refusals[0]
            # and it did NOT ping, even though real mail is sitting there
            assert not [line for line in res.output.splitlines() if "new mail" in line]
        finally:
            holder.release()

    def test_cli_releases_its_lock_on_exit_so_the_next_watcher_starts(
        self, t2_service_env, tmp_path,
    ) -> None:
        store, _cfg, sd = _watch_env(tmp_path)
        addr = _uniq("addr")
        _out(store, addr, sender="alice")
        first = _invoke([
            "watch", addr, "--iterations", "1", "--interval", "0", "--state-dir", str(sd),
        ])
        assert first.exit_code == 0, first.output
        assert len([line for line in first.output.splitlines() if "new mail" in line]) == 1
        after = acquire_watch_locks([addr], state_dir=sd, emit=lambda _s: None)
        try:
            assert after.ok is True, "the finished watcher must not leave its lock held"
        finally:
            after.release()


# ── nx tuple watch: two addresses per session (MM-1.3, nexus-6konb.4) ──────


class TestWatchAddressResolution:
    """A session has two mailboxes: the session id, which a fresh subprocess can
    resolve from its own env, and the instance name, which has no env var
    anywhere and can only arrive as a literal at arm time."""

    def test_explicit_addresses_are_used_verbatim_with_no_resolution(self) -> None:
        r = resolve_watch_addresses(("a", "b"), instance="", session_id="session-xyz")
        assert r.addresses == ["a", "b"]
        assert r.notices == []
        assert r.error == ""

    def test_no_addresses_resolves_the_session_and_adds_the_instance(self) -> None:
        r = resolve_watch_addresses((), instance="nexus-19", session_id="session-xyz")
        assert r.addresses == ["session-xyz", "nexus-19"]
        assert r.notices == []

    def test_instance_omitted_warns_rather_than_half_watching_in_silence(self) -> None:
        r = resolve_watch_addresses((), instance="", session_id="session-xyz")
        assert r.addresses == ["session-xyz"]
        assert len(r.notices) == 1
        assert "--instance" in r.notices[0]
        assert "session-xyz" not in r.notices[0].split("--instance")[0].split("WARNING")[0]
        assert r.error == ""

    def test_unresolvable_session_and_no_addresses_is_a_skip_not_a_warning(self) -> None:
        r = resolve_watch_addresses((), instance="", session_id=None)
        assert r.addresses == []
        assert r.error
        assert "SKIP" in r.error

    def test_instance_alone_is_enough_when_the_session_does_not_resolve(self) -> None:
        r = resolve_watch_addresses((), instance="nexus-19", session_id=None)
        assert r.addresses == ["nexus-19"]
        assert r.error == ""
        assert len(r.notices) == 1
        assert "session" in r.notices[0]

    def test_an_instance_equal_to_the_session_id_collapses_to_one_address(self) -> None:
        r = resolve_watch_addresses((), instance="same", session_id="same")
        assert r.addresses == ["same"]
        assert r.notices == []  # neither mailbox is unwatched, so there is nothing to warn about

    def test_explicit_addresses_suppress_the_instance_too(self) -> None:
        """The precedence MM-3.1's arming relies on: what it names is what is watched.

        An --instance that leaked into the explicit branch would silently widen the
        watch beyond the caller's list, and take a second lock nobody asked for.
        """
        r = resolve_watch_addresses(("only-this",), instance="nexus-19",
                                    session_id="session-xyz")
        assert r.addresses == ["only-this"]
        assert r.notices == []
        assert r.error == ""


class TestWatchTwoAddresses:
    def test_both_subspaces_are_probed_every_iteration(self, t2_service_env, tmp_path) -> None:
        store, cfg, sd = _watch_env(tmp_path)
        a, b = _uniq("sess"), _uniq("inst")
        probed = []

        class _Recording:
            def __init__(self, inner) -> None:
                self.inner = inner

            def rd(self, subspace, *args, **kw):
                probed.append(subspace)
                return self.inner.rd(subspace, *args, **kw)

        lines, reports, clock = [], [], _Clock()
        run_watch(
            _Recording(store), [a, b], config=cfg, state_dir=sd, iterations=3,
            emit=lines.append, report=reports.append, now=clock.now, sleep=lambda _s: None,
        )
        # both every cycle, and the order rotates so a flood on one cannot starve the other
        assert len(probed) == 6
        assert set(probed[0:2]) == set(probed[2:4]) == set(probed[4:6]) == {
            f"mailbox/{a}", f"mailbox/{b}",
        }
        assert probed[0] == probed[4] != probed[2]

    def test_a_hit_on_either_address_names_which_one_it_arrived_at(
        self, t2_service_env, tmp_path,
    ) -> None:
        store, cfg, sd = _watch_env(tmp_path)
        a, b = _uniq("sess"), _uniq("inst")
        id_a = _out(store, a, sender="from-session")
        id_b = _out(store, b, sender="from-instance")
        lines, reports, clock = [], [], _Clock()
        run_watch(
            store, [a, b], config=cfg, state_dir=sd, iterations=1,
            emit=lines.append, report=reports.append, now=clock.now, sleep=lambda _s: None,
        )
        assert len(lines) == 2
        by_addr = {line.split("mailbox/")[1].split()[0]: line for line in lines}
        assert set(by_addr) == {a, b}
        assert id_a in by_addr[a] and "from-session" in by_addr[a]
        assert id_b in by_addr[b] and "from-instance" in by_addr[b]

    def test_each_address_keeps_its_own_seen_set(self, t2_service_env, tmp_path) -> None:
        store, cfg, sd = _watch_env(tmp_path)
        a, b = _uniq("sess"), _uniq("inst")
        _out(store, a, sender="alice")
        lines, reports, clock = [], [], _Clock()
        run_watch(
            store, [a, b], config=cfg, state_dir=sd, iterations=1,
            emit=lines.append, report=reports.append, now=clock.now, sleep=lambda _s: None,
        )
        assert len(lines) == 1
        # a's state records the pinged id; b's records nothing, because b is empty
        a_seen = json.loads(state_path(sd, a).read_text(encoding="utf-8"))["seen"]
        b_seen = json.loads(state_path(sd, b).read_text(encoding="utf-8"))["seen"]
        assert len(a_seen) == 1
        assert b_seen == {}
        # mail arriving at the OTHER address is new, not shadowed by the first one's state
        _out(store, b, sender="bob")
        clock.advance(cfg.interval_s)
        run_watch(
            store, [a, b], config=cfg, state_dir=sd, iterations=1,
            emit=lines.append, report=reports.append, now=clock.now, sleep=lambda _s: None,
        )
        assert len(lines) == 2
        assert f"mailbox/{b}" in lines[1]

    def test_two_addresses_share_one_emit_budget_rather_than_one_each(
        self, t2_service_env, tmp_path,
    ) -> None:
        """A simultaneous first-arm backlog on both mailboxes must stay under the
        single measured auto-stop ceiling, not double it."""
        store, cfg, sd = _watch_env(tmp_path)
        a, b = _uniq("sess"), _uniq("inst")
        for i in range(6):
            _out(store, a, sender=f"a{i}")
            _out(store, b, sender=f"b{i}")
        lines, reports, clock = [], [], _Clock()
        run_watch(
            store, [a, b], config=cfg, state_dir=sd, iterations=1,
            emit=lines.append, report=reports.append, now=clock.now, sleep=lambda _s: None,
        )
        # cfg.budget_lines is the ceiling for the WHOLE cycle across both addresses
        assert len(lines) <= cfg.budget_lines
        # and nothing was lost: every row counted as pinged, so the next cycle is silent
        clock.advance(cfg.interval_s)
        before = len(lines)
        run_watch(
            store, [a, b], config=cfg, state_dir=sd, iterations=1,
            emit=lines.append, report=reports.append, now=clock.now, sleep=lambda _s: None,
        )
        assert len(lines) == before

    def test_one_address_failing_does_not_stop_the_other(self, t2_service_env, tmp_path) -> None:
        store, cfg, sd = _watch_env(tmp_path)
        a, b = _uniq("sess"), _uniq("inst")
        tid = _out(store, b, sender="bob")

        class _HalfBroken:
            def __init__(self, inner) -> None:
                self.inner = inner

            def rd(self, subspace, *args, **kw):
                if subspace == f"mailbox/{a}":
                    raise _Boom("that one is unreachable")
                return self.inner.rd(subspace, *args, **kw)

        lines, reports, clock = [], [], _Clock()
        stats = run_watch(
            _HalfBroken(store), [a, b], config=cfg, state_dir=sd, iterations=2,
            emit=lines.append, report=reports.append, now=clock.now, sleep=lambda _s: None,
        )
        assert stats.probe_errors == 2
        pings = [line for line in lines if "new mail" in line]
        assert len(pings) == 1 and tid in pings[0]
        assert len([line for line in lines if "probe failed" in line]) == 1


class TestWatchTwoAddressesCli:
    def test_instance_flag_watches_both_mailboxes(self, t2_service_env, tmp_path,
                                                  monkeypatch) -> None:
        store, _cfg, sd = _watch_env(tmp_path)
        sess, inst = _uniq("sess"), _uniq("inst")
        monkeypatch.setenv("NX_SESSION_ID", sess)
        id_a = _out(store, sess, sender="from-session")
        id_b = _out(store, inst, sender="from-instance")
        res = _invoke([
            "watch", "--instance", inst, "--iterations", "1", "--interval", "0",
            "--state-dir", str(sd),
        ])
        assert res.exit_code == 0, res.output
        pings = [line for line in res.output.splitlines() if "new mail" in line]
        assert len(pings) == 2, res.output
        assert any(id_a in p for p in pings) and any(id_b in p for p in pings)
        assert not [line for line in res.output.splitlines() if "WARNING" in line]

    def test_no_instance_flag_warns_once_and_still_watches_the_session(
        self, t2_service_env, tmp_path, monkeypatch,
    ) -> None:
        store, _cfg, sd = _watch_env(tmp_path)
        sess = _uniq("sess")
        monkeypatch.setenv("NX_SESSION_ID", sess)
        tid = _out(store, sess, sender="alice")
        res = _invoke([
            "watch", "--iterations", "1", "--interval", "0", "--state-dir", str(sd),
        ])
        assert res.exit_code == 0, res.output
        warnings = [line for line in res.output.splitlines() if "WARNING" in line]
        assert len(warnings) == 1, res.output
        assert "--instance" in warnings[0]
        pings = [line for line in res.output.splitlines() if "new mail" in line]
        assert len(pings) == 1 and tid in pings[0]

    def test_no_addresses_and_no_resolvable_session_skips(self, t2_service_env, tmp_path,
                                                          monkeypatch) -> None:
        cfgdir = tmp_path / "empty-cfg"
        cfgdir.mkdir()
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfgdir))
        res = _invoke([
            "watch", "--iterations", "1", "--interval", "0", "--state-dir", str(tmp_path),
        ])
        assert res.exit_code == 0, res.output
        skips = [line for line in res.output.splitlines() if "SKIP" in line]
        assert len(skips) == 1, res.output
        assert not [line for line in res.output.splitlines() if "new mail" in line]

    def test_cli_explicit_address_wins_over_both_defaults(self, t2_service_env, tmp_path,
                                                          monkeypatch) -> None:
        store, _cfg, sd = _watch_env(tmp_path)
        named, inst, sess = _uniq("named"), _uniq("inst"), _uniq("sess")
        monkeypatch.setenv("NX_SESSION_ID", sess)
        id_named = _out(store, named, sender="wanted")
        _out(store, inst, sender="not-asked-for")
        _out(store, sess, sender="not-asked-for-either")
        res = _invoke([
            "watch", named, "--instance", inst, "--iterations", "1", "--interval", "0",
            "--state-dir", str(sd),
        ])
        assert res.exit_code == 0, res.output
        pings = [line for line in res.output.splitlines() if "new mail" in line]
        assert len(pings) == 1, res.output
        assert id_named in pings[0]
        assert inst not in res.output and sess not in res.output
        # and only the named address was locked
        assert lock_path(sd, named).is_file()
        assert not lock_path(sd, inst).exists()

    def test_cli_instance_equal_to_the_session_id_watches_once(
        self, t2_service_env, tmp_path, monkeypatch,
    ) -> None:
        """End to end for the collapse: without dedup the second lock on the same
        address would refuse the watcher's own first lock and nothing would be
        watched at all."""
        store, _cfg, sd = _watch_env(tmp_path)
        same = _uniq("same")
        monkeypatch.setenv("NX_SESSION_ID", same)
        tid = _out(store, same, sender="alice")
        res = _invoke([
            "watch", "--instance", same, "--iterations", "1", "--interval", "0",
            "--state-dir", str(sd),
        ])
        assert res.exit_code == 0, res.output
        assert "already watched by" not in res.output
        pings = [line for line in res.output.splitlines() if "new mail" in line]
        assert len(pings) == 1, res.output
        assert tid in pings[0]
        assert not [line for line in res.output.splitlines() if "WARNING" in line]


# ── Phase 1 review fixes (MM-1.4, nexus-6konb.5) ──────────────────────────


class TestDeadLetterReachesTheWatchedStream:
    """The critical the phase review found: a row dead-lettered before the watcher
    ever saw it alive is mail that will never be delivered, and the session had
    heard nothing about it. It must reach stdout and it must heal like a live row."""

    def _dead_row(self, store, addr):
        sub = f"mailbox/{addr}"
        tid = _out(store, addr, sender="poison")
        for _ in range(3):  # mailbox.yaml max_attempts=3
            claimant = _uniq("c")
            claimed = store.in_(sub, {"to": addr}, claimant=claimant, lease_s=30)
            assert claimed is not None
            store.nack(claimed[1], claimant)
        return tid

    def test_first_sight_dead_goes_to_stdout_not_stderr(self, t2_service_env, tmp_path) -> None:
        store, cfg, sd = _watch_env(tmp_path)
        addr = _uniq("addr")
        dead_id = self._dead_row(store, addr)
        lines, reports, clock = [], [], _Clock()
        _run(store, cfg, sd, addr, clock, 1, lines, reports)
        assert [line for line in lines if dead_id in line]
        assert not [r for r in reports if dead_id in r]

    def test_a_lost_first_sight_notice_heals_on_the_re_emit_window(
        self, t2_service_env, tmp_path,
    ) -> None:
        """Unlike every other notice this module emits, the old dead-letter report had
        no retry: one stderr line, a write-once set, and nothing if it was missed."""
        store, cfg, sd = _watch_env(tmp_path)
        addr = _uniq("addr")
        dead_id = self._dead_row(store, addr)
        lines, reports, clock = [], [], _Clock()
        _run(store, cfg, sd, addr, clock, 1, lines, reports)
        assert len([line for line in lines if dead_id in line]) == 1
        clock.advance(cfg.reemit_after_s + 1)
        _run(store, cfg, sd, addr, clock, 1, lines, reports)
        assert len([line for line in lines if dead_id in line]) == 2
        # and it is capped like a live row rather than repeating forever
        for _ in range(4):
            clock.advance(cfg.reemit_after_s + 1)
            _run(store, cfg, sd, addr, clock, 1, lines, reports)
        assert len([line for line in lines if dead_id in line]) == cfg.max_emits

    def test_a_row_pinged_while_alive_reports_its_death_on_stderr(
        self, t2_service_env, tmp_path,
    ) -> None:
        """The other half of the rule: the session already knows this message exists,
        so its death is a status update, not news of mail it never heard about."""
        store, cfg, sd = _watch_env(tmp_path)
        addr = _uniq("addr")
        sub = f"mailbox/{addr}"
        tid = _out(store, addr, sender="alice")
        lines, reports, clock = [], [], _Clock()
        _run(store, cfg, sd, addr, clock, 1, lines, reports)
        assert len([line for line in lines if tid in line]) == 1  # pinged while alive
        for _ in range(3):
            claimant = _uniq("c")
            claimed = store.in_(sub, {"to": addr}, claimant=claimant, lease_s=30)
            assert claimed is not None
            store.nack(claimed[1], claimant)
        clock.advance(cfg.interval_s)
        _run(store, cfg, sd, addr, clock, 1, lines, reports)
        assert len([line for line in lines if tid in line]) == 1  # no new stdout line
        assert [r for r in reports if tid in r and "never be claimed" in r]


class TestWatchFairnessAcrossAddresses:
    """The phase review raised starvation of the second address under a sustained
    asymmetric flood. It does not occur under the current constants, and this pins
    the invariant that prevents it rather than the rotation that merely insures it:
    one address can take at most max_lines_per_cycle + 1 of budget_lines, so the
    other always has room, and once the window saturates both coalesce equally."""

    def test_a_sustained_flood_on_one_address_never_starves_the_other_of_detail(
        self, t2_service_env, tmp_path,
    ) -> None:
        store, cfg, sd = _watch_env(tmp_path)
        loud, quiet = _uniq("loud"), _uniq("quiet")
        lines, reports, clock = [], [], _Clock()

        class _Flooding:
            def __init__(self, inner) -> None:
                self.inner = inner

            def rd(self, subspace, *args, **kw):
                if subspace == f"mailbox/{loud}":
                    for i in range(8):
                        _out(self.inner, loud, sender=f"loud{i}")
                else:
                    _out(self.inner, quiet, sender="quiet-sender")
                return self.inner.rd(subspace, *args, **kw)

        # The window must actually clear between cycles, or every address coalesces and
        # the test proves nothing about fairness between them.
        run_watch(
            _Flooding(store), [loud, quiet], config=cfg, state_dir=sd, iterations=4,
            emit=lines.append, report=reports.append, now=clock.now,
            sleep=lambda _s: clock.advance(cfg.budget_window_s + 1),
        )
        # Detail means a NAMED SENDER: the coalesced budget line carries the address too,
        # so matching on the address alone is satisfied by the starvation being tested for.
        named = [line for line in lines if "from=quiet-sender" in line]
        assert len(named) >= 2, (
            f"the quiet address was named in detail on only {len(named)} cycles; "
            f"one address must never be able to consume the whole budget"
        )

    def test_one_address_cannot_consume_the_whole_shared_budget(self) -> None:
        """The arithmetic the test above depends on, pinned directly so a change to
        either constant fails here and names the reason."""
        cfg = WatchConfig()
        most_one_address_can_take = cfg.max_lines_per_cycle + 1  # head lines + coalesced tail
        assert most_one_address_can_take < cfg.budget_lines, (
            "one address can now take the entire emit budget in a single cycle, so a "
            "sustained flood on it would leave every other address permanently coalesced"
        )


class TestWatcherExitAlwaysSpeaks:
    def test_an_unexpected_failure_says_so_on_stdout_before_exiting(
        self, t2_service_env, tmp_path, monkeypatch,
    ) -> None:
        """A watcher that dies silently is the most complete form of the thing the
        stream rule exists to prevent, and the shared CLI error helper writes to
        stderr."""
        def _explode(*_a, **_kw):
            raise RuntimeError("resolver blew up")

        monkeypatch.setattr("nexus.tuple_watch.preflight", _explode)
        res = _invoke([
            "watch", _uniq("addr"), "--iterations", "1", "--interval", "0",
            "--state-dir", str(tmp_path),
        ])
        assert res.exit_code == 1
        stdout_lines = [line for line in res.output.splitlines() if "the watcher is exiting" in line]
        assert len(stdout_lines) == 1, res.output
        assert "resolver blew up" in stdout_lines[0]
