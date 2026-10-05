# SPDX-License-Identifier: AGPL-3.0-or-later
"""A module that asks ``ps`` or ``/proc`` who a process is must also carry a
Windows branch (RDR-224, nexus-f9bgu.21).

Windows has neither. A new ``["ps", ...]`` call or ``/proc`` read in a module
with no Windows path is a silent "no processes found" there, the fail-open
class the identity work removes. File granularity is deliberate: the guard
sits at the public entry (``all_process_rows``, ``process_command``,
``_ppid_of``), not on each private reader beneath it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_SRC = Path(__file__).resolve().parents[1] / "src" / "nexus"
_PS_CALL = re.compile(r'[\[(]\s*"ps"\s*,')
_PROC_READ = re.compile(r'Path\(\s*f?"/proc')
_WINDOWS_BRANCH = re.compile(r'winproc_core|"win32"')
#: Modules that read the process table only as pure parsers or messages.
_EXEMPT: frozenset[str] = frozenset()


def _sites() -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(_SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if _PS_CALL.search(text) or _PROC_READ.search(text) or "PROCFS_ROOT = Path" in text:
            out[str(path.relative_to(_SRC))] = text
    return out


def test_every_process_table_reader_has_a_windows_branch() -> None:
    offenders = [
        rel for rel, text in _sites().items()
        if rel not in _EXEMPT and not _WINDOWS_BRANCH.search(text)
    ]
    assert offenders == [], (
        "these modules read ps or /proc with no Windows branch (route through "
        "nexus._install.winproc_core): " + ", ".join(offenders)
    )


def test_the_scan_sees_the_modules_it_guards() -> None:
    """Non-vacuity: a scan that found nothing would pass the check above."""
    found = set(_sites())
    for expected in (
        "session.py",
        "daemon/service_registry.py",
        "daemon/aspect_worker_daemon.py",
        "commands/doctor.py",
        "_install/census_core.py",
    ):
        assert expected in found, (expected, sorted(found))
