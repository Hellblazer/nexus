#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 P3 MVV (nexus-uai7p): two-subprocess T2 daemon round trip.

The §MVV table's P3 line: two ``claude -p`` subprocesses in different
working dirs share ``memory_put`` / ``memory_get`` (cross-process
daemon-mediated state). Validates client-traffic against the daemon;
the global call-site flip is deferred to P4.

The substantive contract is "two separate OS processes sharing T2
state via the daemon RPC"; we use bare ``python -c`` subprocesses
because the ``claude`` CLI is not installed in CI and what's being
validated is OS-level process isolation + daemon-mediated state
sharing.

Usage::

    # Against an already-running T2 daemon (operator-installed via
    # ``nx daemon t2 install --autostart``):
    python scripts/rdr120_p3_mvv.py

    # Spawn an ad-hoc daemon to a tmp dir for one-shot validation:
    python scripts/rdr120_p3_mvv.py --auto-start

Exit codes:

    0   round trip succeeded
    1   no daemon reachable and --auto-start not passed
    2   daemon spawn failed
    3   writer subprocess failed
    4   reader subprocess failed
    5   round-trip assertion failed
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


WRITER_SCRIPT = textwrap.dedent("""
    import json
    from nexus.daemon.t2_client import T2Client
    client = T2Client()
    try:
        row_id = client.memory.put(
            content="rdr-120 p3 mvv: writer subprocess wrote this entry",
            project="nexus_p3_mvv",
            title="p3-mvv-writer",
            tags="rdr-120,p3-mvv",
        )
        rows = client.memory.list_entries(project="nexus_p3_mvv")
        print(json.dumps({
            "role": "writer",
            "row_id": row_id,
            "visible_to_self": len(rows),
        }))
    finally:
        client.close()
""")

READER_SCRIPT = textwrap.dedent("""
    import json
    from nexus.daemon.t2_client import T2Client
    client = T2Client()
    try:
        rows = client.memory.search(
            "writer subprocess wrote", project="nexus_p3_mvv",
        )
        print(json.dumps({
            "role": "reader",
            "count": len(rows),
            "titles": sorted([r.get("title", "") for r in rows]),
        }))
    finally:
        client.close()
""")


def _daemon_reachable() -> dict | None:
    from nexus.daemon.discovery import (
        DaemonNotRunningError,
        discovery_resolve,
    )
    try:
        return discovery_resolve("t2")
    except DaemonNotRunningError:
        return None


def _spawn_ad_hoc_daemon() -> tuple[subprocess.Popen, Path, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="rdr120-p3-mvv-", dir="/tmp"))
    config_dir = tmp / "config"
    db_path = tmp / "memory.db"
    config_dir.mkdir()
    driver = textwrap.dedent(f"""
        from pathlib import Path
        from nexus.daemon.t2_daemon import run_t2_daemon
        run_t2_daemon(
            config_dir=Path({str(config_dir)!r}),
            db_path=Path({str(db_path)!r}),
        )
    """)
    proc = subprocess.Popen(
        [sys.executable, "-c", driver],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    from nexus.daemon.t2_daemon import t2_discovery_path
    disc = t2_discovery_path(config_dir)
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if disc.exists():
            break
        time.sleep(0.1)
    if not disc.exists():
        proc.terminate()
        raise TimeoutError(f"daemon did not start within 15s ({disc})")
    return proc, config_dir, db_path


def _run_subprocess(label: str, script: str, env: dict[str, str]) -> dict:
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{label} subprocess exited {result.returncode}\n"
            f"stdout: {result.stdout!r}\n"
            f"stderr: {result.stderr!r}"
        )
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise RuntimeError(
            f"{label} subprocess produced non-JSON output: "
            f"stdout={result.stdout!r}, err={result.stderr!r}"
        ) from exc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--auto-start",
        action="store_true",
        help="Spawn an ad-hoc T2 daemon to a tmp dir if none reachable.",
    )
    args = ap.parse_args()

    daemon_proc: subprocess.Popen | None = None
    ad_hoc_dir: Path | None = None
    payload = _daemon_reachable()

    if payload is None:
        if not args.auto_start:
            print(
                "[demo] FAIL: No T2 daemon reachable via NX_T2_SOCK / "
                "NX_T2_ADDR or discovery file. Either install via "
                "`nx daemon t2 install --autostart`, start it via "
                "`nx daemon t2 start`, or pass --auto-start to spawn "
                "an ad-hoc daemon for this run.",
                file=sys.stderr,
            )
            return 1
        try:
            daemon_proc, ad_hoc_config, ad_hoc_db = _spawn_ad_hoc_daemon()
            ad_hoc_dir = ad_hoc_config.parent
            # The ad-hoc daemon writes its discovery in ad_hoc_config,
            # NOT in the user's default config_dir. Resolve directly
            # from there (and pass the same path to subprocesses below
            # via NEXUS_CONFIG_DIR).
            from nexus.daemon.discovery import discovery_resolve
            payload = discovery_resolve("t2", config_dir=ad_hoc_config)
            print(
                f"[demo] spawned ad-hoc daemon: pid={payload['pid']} "
                f"uds={payload['uds_path']} "
                f"tcp={payload['tcp_host']}:{payload['tcp_port']}"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[demo] FAIL: daemon spawn failed: {exc}", file=sys.stderr)
            return 2
    else:
        print(
            f"[demo] daemon reachable: pid={payload.get('pid')} "
            f"source={payload.get('source')}"
        )

    sub_env = {**os.environ}
    if payload.get("uds_path"):
        sub_env["NX_T2_SOCK"] = payload["uds_path"]
        sub_env.pop("NX_T2_ADDR", None)
    elif payload.get("tcp_host") and payload.get("tcp_port"):
        sub_env["NX_T2_ADDR"] = f"{payload['tcp_host']}:{payload['tcp_port']}"
        sub_env.pop("NX_T2_SOCK", None)

    failures: list[str] = []
    try:
        print("[demo] subprocess A (writer) connecting via daemon")
        writer = _run_subprocess("writer", WRITER_SCRIPT, sub_env)
        print(f"[demo]   writer -> {writer}")
        if writer.get("visible_to_self", 0) < 1:
            failures.append(
                f"writer self-list returned {writer.get('visible_to_self')}"
            )

        print("[demo] subprocess B (reader) connecting via daemon")
        reader = _run_subprocess("reader", READER_SCRIPT, sub_env)
        print(f"[demo]   reader -> {reader}")
        if reader.get("count", 0) < 1:
            failures.append(
                f"reader saw {reader.get('count')} hits; expected >=1"
            )
        if "p3-mvv-writer" not in (reader.get("titles") or []):
            failures.append(
                f"reader titles {reader.get('titles')!r} missing 'p3-mvv-writer'"
            )
    finally:
        if daemon_proc is not None:
            print("[demo] stopping ad-hoc daemon")
            daemon_proc.terminate()
            try:
                daemon_proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                daemon_proc.kill()
                daemon_proc.wait(timeout=5.0)
        if ad_hoc_dir is not None:
            shutil.rmtree(ad_hoc_dir, ignore_errors=True)

    if failures:
        for f in failures:
            print(f"[demo] FAIL: {f}", file=sys.stderr)
        return 5
    print("[demo] OK: RDR-120 P3 MVV round trip succeeded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
