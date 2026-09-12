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
import uuid

from click.testing import CliRunner

from nexus.commands.tuple_cmd import tuple_group
from nexus.db.t2.http_tuple_store import HttpTupleStore
from nexus.tuple_watch import WatchConfig, run_watch, state_path


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
        assert len(lines) == 1
        assert fresh_id in lines[0]
        assert dead_id not in lines[0]
        assert stats.dead_seen == 1
        assert any(dead_id in r for r in reports)
        # the dead row is reported once, not once per cycle
        clock.advance(cfg.interval_s)
        _run(store, cfg, sd, addr, clock, 2, lines, reports)
        assert sum(dead_id in r for r in reports) == 1

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
        assert lines == []
        failed = [r for r in reports if "probe failed" in r]
        recovered = [r for r in reports if "probe recovered" in r]
        assert len(failed) == 1 and "engine unreachable" in failed[0]
        assert len(recovered) == 1

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
        assert [line for line in lines if tid in line] and len(lines) == 1
        assert sum("probe failed" in r and bad in r for r in reports) == 1
