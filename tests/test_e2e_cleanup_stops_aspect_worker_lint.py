# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""An e2e script's teardown stops the aspect worker before it stops the service.

``nx daemon service stop`` leaves the aspect-worker daemon running by design
(RDR-224: it belongs to the store path). A gate whose ``nx`` calls ran against
its scratch service therefore spawned a worker (spawn-if-absent) that outlived
the gate and its deleted scratch directory: 24 ppid-1 orphans were found on one
laptop, the oldest from Oct 31 (nexus-wl9pk).

Static pin over every ``tests/e2e`` shell script: each teardown, meaning a
``cleanup()`` or ``_stop_service()`` function or an EXIT trap line, that stops
the service first runs ``daemon aspect-worker stop``. The worker goes first so
its final reclaim sweep still has an engine to ask. Non-vacuity: the five
scripts that were leaking must be among the teardowns scanned, with a service
stop in each.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent
_E2E = REPO_ROOT / "tests" / "e2e"

_FUNCTIONS = re.compile(r"^(cleanup|_stop_service)\(\)\s*\{\n(.*?)^\}", re.M | re.S)
_TRAP = re.compile(r"^[^#\n]*\btrap\s+'([^'\n]*daemon service stop[^'\n]*)'\s+EXIT", re.M)
_SERVICE_STOP = "daemon service stop"
_WORKER_STOP = "daemon aspect-worker stop"

#: The scripts that leaked a worker; each must have a scanned teardown.
_MUST_BE_SCANNED = (
    "local-service-gate.sh",
    "dual-embedder-gate.sh",
    "fresh-install-mvv.sh",
    "benchmark-corpus.sh",
    "pdf-on-demand-rehearsal.sh",
)


def _teardowns() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(_E2E.rglob("*.sh")):
        text = path.read_text(encoding="utf-8", errors="replace")
        bodies = [m.group(2) for m in _FUNCTIONS.finditer(text)]
        bodies += [m.group(1) for m in _TRAP.finditer(text)]
        stopping = [b for b in bodies if _SERVICE_STOP in b]
        if stopping:
            found[path.name] = stopping
    return found


def test_the_scan_covers_every_script_that_leaked() -> None:
    found = _teardowns()
    missing = [name for name in _MUST_BE_SCANNED if name not in found]
    assert not missing, f"no service-stopping teardown found in {missing}; scanned {sorted(found)}"


def test_every_teardown_stops_the_worker_before_the_service() -> None:
    bad = []
    for name, bodies in _teardowns().items():
        for body in bodies:
            worker, service = body.find(_WORKER_STOP), body.find(_SERVICE_STOP)
            if worker < 0 or worker > service:
                bad.append(f"{name}: {body.strip().splitlines()[0][:100]}")
    assert not bad, (
        "teardowns that stop the service without first stopping the aspect worker "
        f"(add `nx daemon aspect-worker stop`; nexus-wl9pk):\n" + "\n".join(bad)
    )
