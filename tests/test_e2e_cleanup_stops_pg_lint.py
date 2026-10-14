# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""An e2e script's ``cleanup()`` stops the service with ``--with-pg``.

A plain ``nx daemon service stop`` leaves the supervisor's PostgreSQL running
by design. ``tests/e2e/local-service-gate.sh``'s cleanup relied on it, then on
a ``pg_ctl`` fallback that needs ``pg_ctl`` on ``PATH``, and deleted the
datadir. On hellmini (no ``pg_ctl`` on ``PATH``) a jar-rebuild failure left a
postmaster running over a deleted datadir (2026-10-14). The mid-run stop in
that gate keeps PostgreSQL up on purpose; only the exit cleanup must not.

Static pin over every ``tests/e2e`` shell script with a ``cleanup()``
function: each ``nx daemon service stop`` command inside it carries
``--with-pg``. Non-vacuity: the local-service gate's cleanup must be among
those scanned and must contain a stop.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent
_E2E = REPO_ROOT / "tests" / "e2e"
_GATE = _E2E / "local-service-gate.sh"

_CLEANUP = re.compile(r"^cleanup\(\)\s*\{\n(.*?)^\}", re.M | re.S)
_STOP = re.compile(r"^\s*[^#\n]*\bnx daemon service stop\b[^\n]*$", re.M)


def _cleanup_stops() -> dict[Path, list[str]]:
    found: dict[Path, list[str]] = {}
    for path in sorted(_E2E.rglob("*.sh")):
        match = _CLEANUP.search(path.read_text(encoding="utf-8", errors="replace"))
        if match:
            found[path] = _STOP.findall(match.group(1))
    return found


def test_scan_is_not_vacuous() -> None:
    found = _cleanup_stops()
    assert found.get(_GATE), (
        f"expected a service stop inside {_GATE.name}'s cleanup(); scanned {[p.name for p in found]}"
    )


def test_every_cleanup_stop_takes_postgres_down() -> None:
    bad = [
        f"{path.relative_to(REPO_ROOT)}: {line.strip()}"
        for path, lines in _cleanup_stops().items()
        for line in lines
        if "--with-pg" not in line
    ]
    assert not bad, "cleanup() stops that leave PostgreSQL running:\n" + "\n".join(bad)
