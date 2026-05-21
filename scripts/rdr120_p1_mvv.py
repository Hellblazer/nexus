#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 P1.C MVV (nexus-aim84) — two-subprocess T3 daemon round trip.

Operator-runnable demo. Spawns a T3 daemon, then two Python subprocesses
that each connect via NX_T3_ADDR using make_t3_client(). Subprocess A
writes documents to a shared collection; subprocess B reads them back.
Validates discovery + transport + daemon supervision end-to-end against
a real subprocess pair (NOT in-process Task dispatches).

The bead description says "two claude -p subprocesses". The substantive
contract is "two separate OS processes both reaching the same daemon
via NX_T3_ADDR"; we use bare ``python -c`` subprocesses because the
``claude`` CLI is not installed in CI and the subprocess-isolation
property is what we are validating, not anything model-specific.

Usage::

    python scripts/rdr120_p1_mvv.py [--keep-daemon]

Exits 0 on success; non-zero with an explanatory line on failure.
``--keep-daemon`` leaves the daemon running for follow-up manual probes.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from textwrap import dedent


WRITER_SCRIPT = dedent("""
    import json, sys
    from nexus.daemon.t3_client import make_t3_client
    t3 = make_t3_client()
    coll = t3._client.get_or_create_collection("rdr120_p1_mvv")
    coll.upsert(
        documents=["alpha from writer", "beta from writer"],
        ids=["alpha", "beta"],
    )
    print(json.dumps({"role": "writer", "count": coll.count()}))
""")

READER_SCRIPT = dedent("""
    import json, sys
    from nexus.daemon.t3_client import make_t3_client
    t3 = make_t3_client()
    coll = t3._client.get_collection("rdr120_p1_mvv")
    res = coll.query(query_texts=["alpha"], n_results=2)
    print(json.dumps({"role": "reader", "ids": res["ids"][0]}))
""")


def _run_subprocess(label: str, script: str, env: dict[str, str]) -> dict:
    """Run *script* via ``python -c`` with *env*; parse stdout as JSON."""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
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
        "--keep-daemon",
        action="store_true",
        help="Leave the T3 daemon running after the demo (default: stop).",
    )
    args = ap.parse_args()

    from nexus.daemon.t3_daemon import start_t3_daemon, stop_t3_daemon

    os.environ["NX_LOCAL"] = "1"
    tmp = Path(tempfile.mkdtemp(prefix="rdr120-mvv-"))
    config_dir = tmp / "config"
    local_path = tmp / "chroma"
    config_dir.mkdir()
    local_path.mkdir()

    print(f"[demo] starting T3 daemon (config={config_dir}, data={local_path})")
    payload = start_t3_daemon(config_dir=config_dir, local_path=local_path)
    addr = f"{payload['tcp_host']}:{payload['tcp_port']}"
    print(f"[demo] daemon listening on {addr} (pid={payload['pid']})")

    sub_env = {
        **os.environ,
        "NX_LOCAL": "1",
        "NX_T3_ADDR": addr,
    }

    failures: list[str] = []
    try:
        print("[demo] subprocess A (writer) connecting via NX_T3_ADDR")
        writer_result = _run_subprocess("writer", WRITER_SCRIPT, sub_env)
        print(f"[demo]   writer -> {writer_result}")
        if writer_result.get("count") != 2:
            failures.append(
                f"writer count={writer_result.get('count')} expected 2"
            )

        print("[demo] subprocess B (reader) connecting via NX_T3_ADDR")
        reader_result = _run_subprocess("reader", READER_SCRIPT, sub_env)
        print(f"[demo]   reader -> {reader_result}")
        ids = reader_result.get("ids") or []
        if sorted(ids) != ["alpha", "beta"]:
            failures.append(
                f"reader ids={ids} expected ['alpha', 'beta']"
            )
    finally:
        if args.keep_daemon:
            print(f"[demo] leaving daemon running; NX_T3_ADDR={addr}")
        else:
            print("[demo] stopping T3 daemon")
            stop_t3_daemon(config_dir=config_dir)

    if failures:
        for f in failures:
            print(f"[demo] FAIL: {f}", file=sys.stderr)
        return 1
    print("[demo] OK — RDR-120 P1.C MVV round trip succeeded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
