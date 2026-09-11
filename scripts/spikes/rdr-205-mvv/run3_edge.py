#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-205 Phase 3 Step 3, edge half (bead nexus-em75s.33) — MVV run 3,
wake latency through the PUBLIC EDGE against the DEPLOYED engine.

Unlike MVV run 2 (``harness.py``, engine-direct against a developer engine
this same process boots), this script never boots anything: it is a thin
client-only driver that connects through ``https://api.conexus-nexus.com``
to whatever engine is live, exactly the way the RDR-184 orchestration hooks
and a real parked ``rd``/``in`` consumer would. It measures the sixth MVV
metric the RDR names separately from run 2's five (RDR-205 Phase 3 Step 3;
``nexus_rdr/205-research-12`` and ``.../205-research-13``): wake latency for
a blocking ``in_()`` when the sender and the parker are two independent OS
processes, both going through the edge, at three sender delays --
0.5 s and 3 s (well under the engine's 25 s park cap, CA 3) and 28 s (over
the cap -- the parker must issue a SECOND ``in_()`` call after the first
legitimately times out, proving the client loops past the cap through the
edge rather than blocking on one oversized request; see run1.py's parked-`rd`
verification for the identical pattern applied to `rd` instead of `in_`).

Addressing: a dedicated ``mailbox/<address>`` subspace, never the ledger or
a real agent mailbox -- ``keys={"to": address}``, ``dims={"from":
"mvv-run3", "kind": "probe", "correlation_id": <cycle>}``, matching the
mailbox template's own shape (``tests/db/test_http_tuple_store.py``:
``store.out(f"mailbox/{addr}", {"to": addr}, {"from": "sender-a"}, ...)``).
``address`` defaults to ``mvv-run3-<unix-timestamp-at-start>`` so repeat
runs never collide with each other or with any real mailbox.

PRODUCTION WRITE: every write in this script goes through the real client
against the operator's live store (this box is cloud-mode,
``service_url=https://api.conexus-nexus.com``, ``install.mode=managed`` --
confirmed via ``nexus.config.get_credential``/``is_local_mode`` before this
file was written). ``nexus.db.service_endpoint.guard_production_write``
refuses that from a dev checkout unless ``NX_ALLOW_PROD_WRITE`` carries an
explicit non-boolean reason (nexus-a2qhz); the exact reason string below is
the one the dispatching bead named, set on THIS process's environment
before any store is constructed so both the parker and sender subprocesses
(spawned via ``Popen`` with no explicit ``env=``, hence inheriting this
process's environment) carry it too.

Every tuple this script writes is TAKEN (``in_`` + ``ack``) by the parker
before the next cycle starts, by construction of the park/send handshake
below -- the space is left with the same zero AVAILABLE rows it started
with; the consumed rows remain as ordinary history until the mailbox
template's 7-day retention purges them, which is expected and not
"leftover" in the sense the cleanup instruction means.

Roles (each a fresh OS process; the two-process design is deliberate, not
incidental -- two independent ``HttpTupleStore`` instances, two independent
credential/token resolutions, two independent connections through the
edge):

  parker  -- for each cycle: write a local coordination marker recording
             park-start, then call ``in_()`` in a loop (each call capped at
             the engine's 25 s park limit) until it claims the sender's
             tuple, record wake-time and the number of ``in_()`` calls it
             took, ``ack`` the claim, move to the next cycle.
  sender  -- for each cycle: wait for the parker's marker to appear (i.e.
             the parker has already issued its park call for this cycle --
             a genuine park-then-wake, not a poll-then-see), sleep
             ``--delay-s``, then ``out()`` the probe tuple and record the
             confirmed send time.
  measure -- orchestrator (the default entry point): spawns one parker and
             one sender subprocess for ``--n`` cycles at one ``--delay-s``,
             waits for both, joins their per-cycle records by cycle index,
             and appends one JSON line per cycle to ``--results-file``
             (default ``last-run3-results.jsonl``, not committed).
  report  -- reads ``--results-file``, groups by label, and prints
             p50/p95 of wake latency (wake - send) and end-to-end
             (wake - park_start) per group, plus the count of cycles
             needing >= 2 ``in_()`` calls (the over-the-cap evidence).

A 10-minute wall-clock cap on the tool that drives this script (not a
limit of the script itself) means the 28 s-delay group's 30 cycles
(~850 s) cannot run in one ``measure`` invocation -- run it in batches via
``--start-cycle``/``--n`` (e.g. two batches of 15), all under the SAME
``--label`` so ``report`` aggregates them as one group. ``--run-id`` must
also be held fixed across every invocation of one run (parker and sender
alike) so they address the same mailbox subspace.

Usage::

    RUN_ID="mvv-run3-$(date +%s)"
    uv run python scripts/spikes/rdr-205-mvv/run3_edge.py measure \\
        --run-id "$RUN_ID" --label d0_5 --delay-s 0.5 --n 30
    uv run python scripts/spikes/rdr-205-mvv/run3_edge.py measure \\
        --run-id "$RUN_ID" --label d3 --delay-s 3 --n 30
    uv run python scripts/spikes/rdr-205-mvv/run3_edge.py measure \\
        --run-id "$RUN_ID" --label d28 --delay-s 28 --n 15 --start-cycle 0
    uv run python scripts/spikes/rdr-205-mvv/run3_edge.py measure \\
        --run-id "$RUN_ID" --label d28 --delay-s 28 --n 15 --start-cycle 15
    uv run python scripts/spikes/rdr-205-mvv/run3_edge.py report
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

_SPIKE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SPIKE_DIR.parents[2]

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# nexus-a2qhz: every HTTP write from a dev-checkout process is refused
# unless NX_ALLOW_PROD_WRITE carries an explicit reason. This IS a
# deliberate production write through the public edge, to a dedicated
# mailbox address no real consumer reads -- the exact reason string the
# dispatching bead (nexus-em75s.33) named, set as the default so a bare
# invocation carries it, but never overriding an explicit caller-set value.
os.environ.setdefault(
    "NX_ALLOW_PROD_WRITE",
    "RDR-205 .33 edge wake-latency leg, session nexus-0b, 2026-09-11",
)

from nexus.db.t2.http_tuple_store import HttpTupleStore  # noqa: E402

_CLAIMANT = "mvv-run3-parker"
_PARK_TIMEOUT_S = 25.0  # engine's default park cap, CA 3
_MAX_PARK_LOOPS = 5  # headroom past the 28s delay case (5 * 25s = 125s budget)
_SENDER_MARKER_WAIT_TIMEOUT_S = 90.0


def _address(run_id: str) -> str:
    # run_id IS the mailbox address (caller passes e.g. "mvv-run3-<unix-ts>"
    # per the bead's naming instruction) -- no separate prefixing here, so
    # a run_id already carrying "mvv-run3-" is not doubled into
    # "mailbox/mvv-run3-mvv-run3-<ts>".
    return run_id


def _subspace(run_id: str) -> str:
    return f"mailbox/{_address(run_id)}"


def _marker_path(coord_dir: Path, label: str, cycle: int) -> Path:
    return coord_dir / f"park-{label}-{cycle}.json"


# ── parker role ──────────────────────────────────────────────────────────


def run_parker(run_id: str, label: str, n: int, start_cycle: int, coord_dir: Path, out_path: Path) -> int:
    store = HttpTupleStore()
    subspace = _subspace(run_id)
    address = _address(run_id)
    results = []
    for i in range(n):
        cycle = start_cycle + i
        park_start = time.time()
        marker = _marker_path(coord_dir, label, cycle)
        marker.write_text(json.dumps({"park_start": park_start}))
        calls = 0
        wake_time: float | None = None
        row = None
        claim_id = None
        deadline = park_start + _MAX_PARK_LOOPS * _PARK_TIMEOUT_S
        while time.time() < deadline:
            calls += 1
            got = store.in_(
                subspace, {"to": address}, claimant=_CLAIMANT,
                lease_s=30, timeout_s=_PARK_TIMEOUT_S,
            )
            if got is not None:
                wake_time = time.time()
                row, claim_id = got
                break
        if row is None or claim_id is None or wake_time is None:
            print(f"[parker:{label}] cycle={cycle} FAILED: never woke within "
                  f"{_MAX_PARK_LOOPS * _PARK_TIMEOUT_S:.0f}s budget", file=sys.stderr, flush=True)
            return 1
        store.ack(claim_id, _CLAIMANT)
        results.append({
            "cycle": cycle, "park_start": park_start, "wake_time": wake_time,
            "in_calls": calls, "correlation_id": row.dims.get("correlation_id"),
        })
        print(f"[parker:{label}] cycle={cycle} calls={calls} "
              f"park_to_wake={wake_time - park_start:.3f}s", flush=True)
    out_path.write_text(json.dumps(results))
    return 0


# ── sender role ──────────────────────────────────────────────────────────


def run_sender(run_id: str, label: str, n: int, start_cycle: int, delay_s: float,
                coord_dir: Path, out_path: Path) -> int:
    store = HttpTupleStore()
    subspace = _subspace(run_id)
    address = _address(run_id)
    results = []
    for i in range(n):
        cycle = start_cycle + i
        marker = _marker_path(coord_dir, label, cycle)
        wait_deadline = time.time() + _SENDER_MARKER_WAIT_TIMEOUT_S
        while not marker.exists():
            if time.time() > wait_deadline:
                print(f"[sender:{label}] cycle={cycle} FAILED: parker marker "
                      f"never appeared within {_SENDER_MARKER_WAIT_TIMEOUT_S:.0f}s",
                      file=sys.stderr, flush=True)
                return 1
            time.sleep(0.02)
        time.sleep(delay_s)
        store.out(
            subspace, {"to": address},
            {"from": "mvv-run3", "kind": "probe", "correlation_id": str(cycle)},
            f"probe-{label}-{cycle}",
            nonce=f"{run_id}-{label}-{cycle}",
        )
        send_time = time.time()
        results.append({"cycle": cycle, "send_time": send_time})
        print(f"[sender:{label}] cycle={cycle} sent (confirmed) at t={send_time:.3f}", flush=True)
    out_path.write_text(json.dumps(results))
    return 0


# ── orchestrator (measure) ──────────────────────────────────────────────


def cmd_measure(args: argparse.Namespace) -> int:
    coord_dir = Path(args.coord_dir)
    coord_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = coord_dir / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    parker_out = tmp_dir / f"parker-{args.label}-{args.start_cycle}.json"
    sender_out = tmp_dir / f"sender-{args.label}-{args.start_cycle}.json"

    print(f"[measure] run_id={args.run_id} label={args.label} delay_s={args.delay_s} "
          f"n={args.n} start_cycle={args.start_cycle} subspace={_subspace(args.run_id)}",
          flush=True)

    parker_proc = subprocess.Popen([
        sys.executable, __file__, "parker",
        "--run-id", args.run_id, "--label", args.label,
        "--n", str(args.n), "--start-cycle", str(args.start_cycle),
        "--coord-dir", str(coord_dir), "--out", str(parker_out),
    ])
    sender_proc = subprocess.Popen([
        sys.executable, __file__, "sender",
        "--run-id", args.run_id, "--label", args.label,
        "--n", str(args.n), "--start-cycle", str(args.start_cycle),
        "--delay-s", str(args.delay_s),
        "--coord-dir", str(coord_dir), "--out", str(sender_out),
    ])
    parker_rc = parker_proc.wait()
    sender_rc = sender_proc.wait()
    if parker_rc != 0 or sender_rc != 0:
        print(f"[measure] FAILED: parker_rc={parker_rc} sender_rc={sender_rc}", file=sys.stderr)
        return 1

    parker_records = {r["cycle"]: r for r in json.loads(parker_out.read_text())}
    sender_records = {r["cycle"]: r for r in json.loads(sender_out.read_text())}
    if set(parker_records) != set(sender_records):
        print(f"[measure] FAILED: cycle mismatch parker={sorted(parker_records)} "
              f"sender={sorted(sender_records)}", file=sys.stderr)
        return 1

    results_path = Path(args.results_file)
    with results_path.open("a") as fh:
        for cycle in sorted(parker_records):
            p = parker_records[cycle]
            s = sender_records[cycle]
            record = {
                "run_id": args.run_id,
                "label": args.label,
                "delay_s": args.delay_s,
                "cycle": cycle,
                "park_start": p["park_start"],
                "send_time": s["send_time"],
                "wake_time": p["wake_time"],
                "in_calls": p["in_calls"],
                "wake_latency_s": p["wake_time"] - s["send_time"],
                "end_to_end_s": p["wake_time"] - p["park_start"],
            }
            fh.write(json.dumps(record) + "\n")
    print(f"[measure] wrote {len(parker_records)} cycle records to {results_path}", flush=True)
    return 0


# ── report ────────────────────────────────────────────────────────────


def _pctile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(p * (len(s) - 1)))))
    return s[k]


def cmd_report(args: argparse.Namespace) -> int:
    results_path = Path(args.results_file)
    records = [json.loads(line) for line in results_path.read_text().splitlines() if line.strip()]
    by_label: dict[str, list[dict]] = {}
    for r in records:
        by_label.setdefault(r["label"], []).append(r)

    report: dict[str, object] = {"results_file": str(results_path), "groups": {}}
    for label, rows in sorted(by_label.items()):
        rows.sort(key=lambda r: r["cycle"])
        wake = [r["wake_latency_s"] for r in rows]
        e2e = [r["end_to_end_s"] for r in rows]
        multi_call = sum(1 for r in rows if r["in_calls"] >= 2)
        group = {
            "delay_s": rows[0]["delay_s"],
            "n": len(rows),
            "wake_latency_s": {
                "mean": statistics.mean(wake), "p50": _pctile(wake, 0.50),
                "p95": _pctile(wake, 0.95), "max": max(wake),
            },
            "end_to_end_s": {
                "mean": statistics.mean(e2e), "p50": _pctile(e2e, 0.50),
                "p95": _pctile(e2e, 0.95), "max": max(e2e),
            },
            "cycles_with_ge2_in_calls": multi_call,
            "in_calls_distribution": sorted({r["in_calls"] for r in rows}),
        }
        report["groups"][label] = group
        print(f"[report] {label}: n={group['n']} delay_s={group['delay_s']} "
              f"wake_p50={group['wake_latency_s']['p50']:.3f}s "
              f"wake_p95={group['wake_latency_s']['p95']:.3f}s "
              f"e2e_p50={group['end_to_end_s']['p50']:.3f}s "
              f"e2e_p95={group['end_to_end_s']['p95']:.3f}s "
              f"ge2_calls={multi_call}/{group['n']}", flush=True)
    print(json.dumps(report, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="role", required=True)

    p_parker = sub.add_parser("parker")
    p_parker.add_argument("--run-id", required=True)
    p_parker.add_argument("--label", required=True)
    p_parker.add_argument("--n", type=int, required=True)
    p_parker.add_argument("--start-cycle", type=int, default=0)
    p_parker.add_argument("--coord-dir", required=True)
    p_parker.add_argument("--out", required=True)

    p_sender = sub.add_parser("sender")
    p_sender.add_argument("--run-id", required=True)
    p_sender.add_argument("--label", required=True)
    p_sender.add_argument("--n", type=int, required=True)
    p_sender.add_argument("--start-cycle", type=int, default=0)
    p_sender.add_argument("--delay-s", type=float, required=True)
    p_sender.add_argument("--coord-dir", required=True)
    p_sender.add_argument("--out", required=True)

    p_measure = sub.add_parser("measure")
    p_measure.add_argument("--run-id", required=True)
    p_measure.add_argument("--label", required=True)
    p_measure.add_argument("--delay-s", type=float, required=True)
    p_measure.add_argument("--n", type=int, default=30)
    p_measure.add_argument("--start-cycle", type=int, default=0)
    p_measure.add_argument("--coord-dir", default=str(_SPIKE_DIR / "run3-coord"))
    p_measure.add_argument("--results-file", default=str(_SPIKE_DIR / "last-run3-results.jsonl"))

    p_report = sub.add_parser("report")
    p_report.add_argument("--results-file", default=str(_SPIKE_DIR / "last-run3-results.jsonl"))

    args = ap.parse_args()
    if args.role == "parker":
        return run_parker(args.run_id, args.label, args.n, args.start_cycle,
                           Path(args.coord_dir), Path(args.out))
    if args.role == "sender":
        return run_sender(args.run_id, args.label, args.n, args.start_cycle, args.delay_s,
                           Path(args.coord_dir), Path(args.out))
    if args.role == "measure":
        return cmd_measure(args)
    if args.role == "report":
        return cmd_report(args)
    raise AssertionError(f"unreachable role {args.role!r}")


if __name__ == "__main__":
    raise SystemExit(main())
