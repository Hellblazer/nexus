#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 P2 MVV (nexus-ut8zy + nexus-ow1ao soak) — full pytest + integration
suite green under NX_STORAGE_MODE=daemon for T3.

The P2 cutover (nexus-ut8zy) made ``NX_STORAGE_MODE=daemon`` a valid value
for T3 reads/writes. The §Approach Phase 2 MVV is: "Full pytest +
integration green under NX_STORAGE_MODE=daemon for T3". This script is the
operator-runnable validation that closes the P2 soak gate (nexus-ow1ao)
once it passes consistently across the ≥7-day window.

What the script does:

  1. Verifies a T3 daemon is reachable (auto-discovery via NX_T3_ADDR
     env var or ``~/.config/nexus/t3_addr.<uid>`` file). If absent and
     ``--auto-start`` is passed, spawns the daemon to a tmp dir; otherwise
     fails loud with the recovery hint.
  2. Sets ``NX_STORAGE_MODE=daemon`` for the test runs.
  3. Runs ``uv run pytest`` (unit suite).
  4. Runs ``uv run pytest -m integration`` (E2E suite, requires API keys).
  5. Reports pass/fail per suite + the daemon's PID, address, and the
     timing for each suite.
  6. On ``--auto-start`` invocations, stops the daemon afterwards
     (operator-driven daemons left running).

Usage::

    # Against an already-running daemon (operator-installed via
    # `nx daemon t3 install --autostart`):
    python scripts/rdr120_p2_mvv.py

    # Spawn an ad-hoc daemon to a tmp dir for a one-shot validation:
    python scripts/rdr120_p2_mvv.py --auto-start

    # Skip integration (no API keys configured):
    python scripts/rdr120_p2_mvv.py --skip-integration

Exit codes:

    0   Both suites green (or unit-only green when --skip-integration).
    1   No daemon reachable and --auto-start not passed.
    2   Unit suite failed.
    3   Integration suite failed.
    4   Daemon spawn failed.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _daemon_reachable() -> dict | None:
    """Return the resolved discovery payload or None when no daemon is
    reachable. Honours NX_T3_ADDR + discovery file per RDR-120 C2."""
    try:
        from nexus.daemon.discovery import (
            DaemonNotRunningError,
            discovery_resolve,
        )
        return discovery_resolve("t3")
    except DaemonNotRunningError:
        return None


def _spawn_ad_hoc_daemon():
    """Spawn a T3 daemon to a tmp dir; return (payload, config_dir,
    local_path) for later teardown."""
    from nexus.daemon.t3_daemon import start_t3_daemon

    os.environ["NX_LOCAL"] = "1"
    tmp = Path(tempfile.mkdtemp(prefix="rdr120-p2-mvv-"))
    config_dir = tmp / "config"
    local_path = tmp / "chroma"
    config_dir.mkdir()
    local_path.mkdir()
    payload = start_t3_daemon(config_dir=config_dir, local_path=local_path)
    return payload, config_dir, local_path


def _stop_ad_hoc_daemon(config_dir: Path) -> None:
    from nexus.daemon.t3_daemon import stop_t3_daemon
    stop_t3_daemon(config_dir=config_dir)


def _run_suite(label: str, args: list[str], env: dict[str, str]) -> tuple[int, float]:
    """Run a pytest invocation under *env*; return (returncode, seconds)."""
    print(f"\n[demo] {label}: {' '.join(args)}")
    print(f"[demo]   env: NX_STORAGE_MODE={env.get('NX_STORAGE_MODE')} "
          f"NX_T3_ADDR={env.get('NX_T3_ADDR', '<unset; discovery file>')}")
    start = time.monotonic()
    result = subprocess.run(args, env=env, cwd=str(REPO_ROOT))
    elapsed = time.monotonic() - start
    return result.returncode, elapsed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--auto-start",
        action="store_true",
        help="Spawn an ad-hoc T3 daemon to a tmp dir if none reachable. "
        "Stops the daemon at exit. Default: fail loud if no daemon.",
    )
    ap.add_argument(
        "--skip-integration",
        action="store_true",
        help="Skip the `pytest -m integration` suite (no API keys).",
    )
    args = ap.parse_args()

    payload = _daemon_reachable()
    ad_hoc_config_dir: Path | None = None

    if payload is None:
        if not args.auto_start:
            print(
                "[demo] FAIL: No T3 daemon reachable via NX_T3_ADDR or "
                "discovery file. Either install via "
                "`nx daemon t3 install --autostart`, start it via "
                "`nx daemon t3 start`, or pass --auto-start to spawn "
                "an ad-hoc daemon for this run.",
                file=sys.stderr,
            )
            return 1
        try:
            payload, ad_hoc_config_dir, ad_hoc_local_path = _spawn_ad_hoc_daemon()
            print(f"[demo] spawned ad-hoc daemon: pid={payload['pid']} "
                  f"addr={payload['tcp_host']}:{payload['tcp_port']} "
                  f"path={ad_hoc_local_path}")
        except Exception as exc:  # noqa: BLE001
            print(f"[demo] FAIL: daemon spawn failed: {exc}", file=sys.stderr)
            return 4
    else:
        host = payload.get("tcp_host", "?")
        port = payload.get("tcp_port", "?")
        pid = payload.get("pid", "?")
        source = payload.get("source", "?")
        print(f"[demo] daemon reachable: pid={pid} addr={host}:{port} "
              f"(source={source})")

    env = {
        **os.environ,
        "NX_LOCAL": "1",
        "NX_STORAGE_MODE": "daemon",
    }
    # Pin NX_T3_ADDR explicitly so the subprocess tests do not depend on
    # the parent's discovery-file resolution. Cheap belt-and-suspenders.
    host = payload.get("tcp_host")
    port = payload.get("tcp_port")
    if host and port:
        env["NX_T3_ADDR"] = f"{host}:{port}"

    unit_rc = 0
    integ_rc = 0
    unit_secs = 0.0
    integ_secs = 0.0

    try:
        unit_rc, unit_secs = _run_suite(
            "unit suite", ["uv", "run", "pytest"], env,
        )
        if unit_rc != 0:
            print(f"\n[demo] FAIL: unit suite exited {unit_rc} after "
                  f"{unit_secs:.1f}s")
            return 2
        print(f"\n[demo] unit suite OK ({unit_secs:.1f}s)")

        if args.skip_integration:
            print("[demo] integration suite skipped (--skip-integration)")
        else:
            integ_rc, integ_secs = _run_suite(
                "integration suite",
                ["uv", "run", "pytest", "-m", "integration"],
                env,
            )
            if integ_rc != 0:
                print(f"\n[demo] FAIL: integration suite exited {integ_rc} "
                      f"after {integ_secs:.1f}s")
                return 3
            print(f"\n[demo] integration suite OK ({integ_secs:.1f}s)")
    finally:
        if ad_hoc_config_dir is not None:
            print("[demo] stopping ad-hoc daemon")
            _stop_ad_hoc_daemon(ad_hoc_config_dir)

    suffix = "unit only" if args.skip_integration else "unit + integration"
    print(f"\n[demo] OK — RDR-120 P2 MVV passed ({suffix} under "
          f"NX_STORAGE_MODE=daemon)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
