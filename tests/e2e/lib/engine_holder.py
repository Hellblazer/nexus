# SPDX-License-Identifier: AGPL-3.0-or-later
# ruff: noqa: T201
"""Boot one loopback engine (tests._engine_substrate), mint tenants, write engine.json, hold until SIGTERM.

Usage (cwd is the repo root, under ``uv run``): engine_holder.py <out.json> [tenant-count]

engine.json (mode 0600): base_url, pid, tenant and token (the first tenant, for callers that want one), and
``tenants``: a list of {tenant, token}. NX_TEST_PG_ROOT belongs to the caller (candidate_sandbox.sh sets a short
/tmp path and removes it); the substrate's own atexit teardown stops the engine when this process exits.
"""
import json
import os
import signal
import sys
import threading
from pathlib import Path

sys.path.insert(0, os.getcwd())
from tests._engine_substrate import ensure_engine, mint_test_tenant  # noqa: E402

out = Path(sys.argv[1])
count = int(sys.argv[2]) if len(sys.argv) > 2 else 1
state = ensure_engine()
assert state["base_url"].startswith("http://127.0.0.1:"), "not a loopback engine"
tenants = []
for _ in range(count):
    tenant, token = mint_test_tenant(state)
    tenants.append({"tenant": tenant, "token": token})
doc = {"base_url": state["base_url"], "pid": os.getpid(), "tenants": tenants,
       "tenant": tenants[0]["tenant"], "token": tenants[0]["token"]}
tmp = out.with_suffix(out.suffix + ".tmp")
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(doc, f)
os.replace(tmp, out)
print(f"engine up {state['base_url']} tenants={[t['tenant'] for t in tenants]} holder_pid={os.getpid()}", flush=True)
stop = threading.Event()
signal.signal(signal.SIGTERM, lambda *_a: stop.set())
signal.signal(signal.SIGINT, lambda *_a: stop.set())
stop.wait()  # a handler runs while the main thread waits; no polling loop
print("holder stopping", flush=True)
sys.exit(0)  # atexit runs the substrate teardown
