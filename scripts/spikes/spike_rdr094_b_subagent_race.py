#!/usr/bin/env -S uv run python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-094 Spike B: subagent T1 sharing race verification (CA-2).

Verifies the silent-downgrade race described in RDR §Critical
Assumptions CA-2: a subagent dispatched within milliseconds of
top-level MCP startup may observe ``NX_SESSION_ID`` set but find
the parent's session record not yet written, causing a silent
``EphemeralClient`` downgrade in :class:`nexus.db.t1.T1Database`.

## Hypothesis

The subagent's :func:`T1Database.__init__` calls
:func:`find_session_by_id` which reads
``$NEXUS_CONFIG_DIR/sessions/{session_id}.session``. If that file
has not yet been written by the parent's
:func:`write_session_record_by_id` call, the subagent silently falls
through to ``chromadb.EphemeralClient()`` (with a ``warnings.warn``
that is invisible in production stdio transport).

## Protocol

Per cycle:

  1. Set up an isolated ``NEXUS_CONFIG_DIR`` tmpdir.
  2. Spawn nx-mcp with ``NEXUS_MCP_OWNS_T1=1`` and a fresh
     ``NX_SESSION_ID`` (so MCP's ``_t1_chroma_init_if_owner`` runs
     ``start_t1_server`` + ``write_session_record_by_id``).
  3. Wait ``timing_variant_ms`` from the moment nx-mcp was spawned.
     ``timing_variant_ms`` of 0 is "spawn the subagent simultaneously
     with nx-mcp"; 200 is "wait until nx-mcp's session record is
     almost certainly written".
  4. Spawn a child subprocess (the "subagent") that inherits
     ``NX_SESSION_ID`` + ``NEXUS_CONFIG_DIR`` and constructs a fresh
     :class:`T1Database`. The child prints a JSON line to stdout
     reporting whether the inner client is
     ``HttpClient`` (parent's chroma) or ``EphemeralClient``
     (silent downgrade).
  5. Parse the child's output, classify the cycle, and tear both
     processes down.

Default protocol: 10 runs × 4 timing variants (0 / 5 / 50 / 200 ms)
= 40 cycles.

## Decision rule

  * 0 ``ephemeral_downgrade`` events => CA-2 verified, race not
    reproducible. Phase E + F unblocked.
  * >=1 ``ephemeral_downgrade`` event at any timing variant => race
    reproducible. File a follow-up bead to add retry-with-backoff
    in :func:`T1Database.__init__` (or in
    :func:`_resolve_top_level_session_id` per the bead's note).

Each run records: timing_variant_ms, run_id, outcome
(``connected_to_parent`` | ``ephemeral_downgrade`` | ``setup_failed``),
elapsed_ms.

Usage::

    uv run python scripts/spikes/spike_rdr094_b_subagent_race.py [--runs 10]

Output: ``scripts/spikes/spike_rdr094_b_results.jsonl`` (per-run records)
      + ``scripts/spikes/spike_rdr094_b_summary.json`` (aggregate).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional


SCRIPT_DIR = Path(__file__).resolve().parent
RESULTS_PATH = SCRIPT_DIR / "spike_rdr094_b_results.jsonl"
SUMMARY_PATH = SCRIPT_DIR / "spike_rdr094_b_summary.json"

#: Default timing variants in milliseconds. Tests dispatch latency
#: from nx-mcp spawn to subagent T1Database construction.
DEFAULT_TIMINGS_MS: tuple[int, ...] = (0, 5, 50, 200)

#: Wall clock budget for nx-mcp + chroma to come up. The child probe
#: tries the T1Database resolve regardless; setup failure is its own
#: outcome category.
PARENT_SPAWN_TIMEOUT_S: float = 8.0

#: Wall clock budget for the child (subagent) probe to print its
#: outcome line. Only the import + T1Database construction is on
#: the hot path; 5s is generous.
CHILD_TIMEOUT_S: float = 10.0


@dataclass
class RunRecord:
    timing_variant_ms: int
    run_id: int
    outcome: str
    elapsed_ms: float
    setup_failed: bool
    error: Optional[str]
    # Diagnostic tail when the probe failed to produce a parseable
    # outcome line. Empty string when the cycle classified cleanly.
    child_stderr_tail: str = ""


#: Substring that appears in T1Database.__init__'s warning when the
#: subagent falls through to chromadb.EphemeralClient(). The warning
#: is the canonical signal for the silent-downgrade because both
#: HttpClient and EphemeralClient are factory functions returning
#: ``chromadb.api.client.Client``; ``type(client).__name__`` cannot
#: distinguish them.
_DOWNGRADE_WARNING = "falling back to local EphemeralClient"


def _classify_outcome(child_stdout: str, setup_failed: bool) -> str:
    """Classify a cycle outcome from the child's reported state.

    Vocabulary:
      * ``connected_to_parent`` -- the subagent's
        :class:`T1Database` resolved a session record and connected
        via ``HttpClient``. Probe records this as
        ``outcome=connected_to_parent``.
      * ``ephemeral_downgrade`` -- the subagent fell through to
        ``chromadb.EphemeralClient`` (the silent-downgrade signature).
        Probe records this as ``outcome=ephemeral_downgrade``.
      * ``setup_failed`` -- the harness could not get nx-mcp +
        chroma up at all; CA-2 cannot be tested in this cycle.
      * ``unknown`` -- the child never printed a parseable outcome
        line. Treated as inconclusive (probably nx-mcp crash, child
        import error, or timeout).
    """
    if setup_failed:
        return "setup_failed"
    for line in reversed(child_stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Probe-side classification (preferred): the child computed
        # the outcome directly from the warnings stream.
        outcome = obj.get("outcome", "")
        if outcome in ("connected_to_parent", "ephemeral_downgrade"):
            return outcome
        # Fallback for older harness output: warnings inspection.
        for w in obj.get("warnings", []) or []:
            if _DOWNGRADE_WARNING in str(w):
                return "ephemeral_downgrade"
        # Probe ran but produced no diagnostic signal -- keep as unknown
        # rather than silently classifying as connected_to_parent.
    return "unknown"


_SUBAGENT_PROBE = """
# Subagent T1 race probe.
#
# Inherits NX_SESSION_ID + NEXUS_CONFIG_DIR from parent. Constructs a
# fresh T1Database; computes the outcome directly from the captured
# warning stream (chromadb.HttpClient and chromadb.EphemeralClient
# both return chromadb.api.client.Client, so type() cannot
# distinguish them; the T1Database fallback warning is the canonical
# signal).
import json
import os
import sys
import warnings

warnings.simplefilter("always")
captured: list[str] = []

def _capture(message, category, filename, lineno, file=None, line=None):
    captured.append(str(message))

warnings.showwarning = _capture

from nexus.db.t1 import T1Database

t1 = T1Database()
downgraded = any("falling back to local EphemeralClient" in str(w) for w in captured)
print(json.dumps({
    "outcome": "ephemeral_downgrade" if downgraded else "connected_to_parent",
    "client_class": type(t1._client).__name__,
    "session_id": t1._session_id,
    "warnings": captured,
    "nx_session_id_env": os.environ.get("NX_SESSION_ID", ""),
}), flush=True)
sys.exit(0)
"""


def _spawn_nx_mcp(env: dict) -> subprocess.Popen:
    """Spawn nx-mcp under this harness so the parent owns chroma.

    Inherits NEXUS_CONFIG_DIR + NEXUS_MCP_OWNS_T1 + NX_SESSION_ID
    from *env* (built by the per-cycle isolation in :func:`_run_cycle`).
    """
    return subprocess.Popen(
        [sys.executable, "-m", "nexus.mcp.core"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=env,
    )


def _spawn_subagent_probe(env: dict, timeout_s: float) -> tuple[str, str, int]:
    """Run the subagent probe synchronously.

    Returns ``(stdout, stderr, returncode)``. Times out at *timeout_s*
    -- the probe should finish in well under a second when chroma is
    reachable; longer means we hung in the import path.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _SUBAGENT_PROBE],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=env,
        check=False,
    )
    return proc.stdout, proc.stderr, proc.returncode


def _run_cycle(timing_variant_ms: int, run_id: int) -> RunRecord:
    """One race-probe cycle for the given dispatch-delay timing variant."""
    cycle_dir = Path(tempfile.mkdtemp(prefix=f"nx_spike094b_{run_id}_"))
    (cycle_dir / "logs").mkdir(parents=True, exist_ok=True)
    (cycle_dir / "sessions").mkdir(parents=True, exist_ok=True)

    session_id = str(uuid.uuid4())
    env = {
        **os.environ,
        "NEXUS_CONFIG_DIR": str(cycle_dir),
        "NEXUS_MCP_OWNS_T1": "1",
        "NX_SESSION_ID": session_id,
    }

    parent: subprocess.Popen | None = None
    t0 = time.monotonic()
    try:
        parent = _spawn_nx_mcp(env)
        # Wait the requested dispatch delay before the subagent fires.
        # The subagent inherits NX_SESSION_ID; parent's MCP must have
        # written the session record by the time the subagent's
        # T1Database resolves -- if not, the silent downgrade fires.
        if timing_variant_ms > 0:
            time.sleep(timing_variant_ms / 1000.0)

        try:
            stdout, stderr, _rc = _spawn_subagent_probe(env, CHILD_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            return RunRecord(
                timing_variant_ms=timing_variant_ms,
                run_id=run_id, outcome="unknown",
                elapsed_ms=round(elapsed_ms, 2),
                setup_failed=False,
                error="subagent probe timed out",
            )

        elapsed_ms = (time.monotonic() - t0) * 1000.0

        # Parent didn't even reach session-record write if it died
        # early. Distinguish that from an actual race by checking
        # whether the session file exists at all.
        record_path = cycle_dir / "sessions" / f"{session_id}.session"
        record_present = record_path.exists()
        outcome = _classify_outcome(stdout, setup_failed=not record_present and stdout == "")
        # Capture child stderr tail when classification couldn't read
        # a positive signal -- gives the operator something to debug
        # without spelunking through 40 cycles of tmpdirs.
        stderr_tail = ""
        if outcome == "unknown" and stderr:
            stderr_tail = stderr.strip().splitlines()[-1][:300] if stderr.strip() else ""
        return RunRecord(
            timing_variant_ms=timing_variant_ms,
            run_id=run_id, outcome=outcome,
            elapsed_ms=round(elapsed_ms, 2),
            setup_failed=outcome == "setup_failed",
            error=None,
            child_stderr_tail=stderr_tail,
        )
    finally:
        if parent is not None and parent.poll() is None:
            try:
                parent.send_signal(signal.SIGTERM)
                parent.wait(timeout=2)
            except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
                try:
                    parent.kill()
                except Exception:
                    pass


def _aggregate(records: list[RunRecord]) -> dict[str, Any]:
    by_timing: dict[int, list[RunRecord]] = {}
    for r in records:
        by_timing.setdefault(r.timing_variant_ms, []).append(r)

    summary: dict[str, Any] = {"by_timing": {}, "totals": {}}
    grand_total = 0
    grand_downgrade = 0
    grand_connected = 0
    grand_setup_failed = 0
    for timing, recs in by_timing.items():
        n = len(recs)
        connected = sum(1 for r in recs if r.outcome == "connected_to_parent")
        downgrade = sum(1 for r in recs if r.outcome == "ephemeral_downgrade")
        setup_failed = sum(1 for r in recs if r.setup_failed)
        unknown = sum(1 for r in recs if r.outcome == "unknown")
        summary["by_timing"][str(timing)] = {
            "runs": n,
            "connected_to_parent": connected,
            "ephemeral_downgrade": downgrade,
            "setup_failed": setup_failed,
            "unknown": unknown,
        }
        grand_total += n
        grand_connected += connected
        grand_downgrade += downgrade
        grand_setup_failed += setup_failed
    summary["totals"] = {
        "runs": grand_total,
        "connected_to_parent": grand_connected,
        "ephemeral_downgrade": grand_downgrade,
        "setup_failed": grand_setup_failed,
    }
    # Verification needs POSITIVE evidence (>=1 connected_to_parent) AND
    # zero downgrades. All-unknown / all-setup-failed is inconclusive,
    # not verified -- otherwise a probe that always crashes would
    # falsely confirm CA-2.
    if grand_downgrade > 0:
        summary["interpretation"] = "ca2_failed_race_reproducible"
    elif grand_connected > 0:
        summary["interpretation"] = "ca2_verified_race_not_reproducible"
    else:
        summary["interpretation"] = "inconclusive"
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="RDR-094 Spike B: subagent T1 sharing race probe.",
    )
    parser.add_argument(
        "--runs", type=int, default=10,
        help="Runs per timing variant (default 10; RDR target is 10).",
    )
    parser.add_argument(
        "--timings", type=str,
        default=",".join(str(t) for t in DEFAULT_TIMINGS_MS),
        help=(
            "Comma-separated dispatch-delay timing variants in ms. "
            f"Default {','.join(str(t) for t in DEFAULT_TIMINGS_MS)}."
        ),
    )
    args = parser.parse_args()

    timings = [int(t.strip()) for t in args.timings.split(",") if t.strip()]
    if not timings:
        print("no timing variants supplied", file=sys.stderr)
        return 1

    print(
        f"Spike B: {args.runs} runs x {len(timings)} timings = "
        f"{args.runs * len(timings)} cycles. Timings (ms): {timings}",
        flush=True,
    )

    results: list[RunRecord] = []
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS_PATH.open("a") as out:
        for timing in timings:
            for run_id in range(args.runs):
                rec = _run_cycle(timing, run_id)
                out.write(json.dumps(asdict(rec)) + "\n")
                out.flush()
                results.append(rec)
                print(
                    f"[t={timing}ms/run={run_id}] outcome={rec.outcome} "
                    f"elapsed={rec.elapsed_ms:.1f}ms",
                    flush=True,
                )

    summary = _aggregate(results)
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2))
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
