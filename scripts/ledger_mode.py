#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One switch for the class-A ledger checks: ``NX_LEDGER_MODE=block|report`` (RDR-258 Phase 4, nexus-2jomg.5).

The plugin ledger (``tests/test_plugin_release_drift_ledger.py``), the changelog fragments (``scripts/changelog_fold.py check``)
and the cloud-leg route ledger (``tests/scripts/test_cloud_leg.py``) read it through this module.

``block`` (the default, also an unset or empty variable): a finding fails the check, exactly as before the switch existed. Nothing
here prints and nothing here changes a check's own assertion or exit code.

``report``: a finding is listed instead of failed. Each finding is one line ``LEDGER-FINDING <ledger>: <text>`` (and, under
GitHub Actions, a ``::warning title=<ledger>::<text>`` annotation), then one summary line
``LEDGER-REPORT <ledger>: <n> finding(s), <m> item(s) examined``. A check that examined nothing still fails, with a
``LEDGER-VACUOUS <ledger>: ...`` line: report mode lowers what a finding costs, never what a pass must have examined. A skip
stays a skip (this module is not reached by a skipped test). Any other value of the variable is an error, never a quiet default.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping

ENV = "NX_LEDGER_MODE"
#: Report mode also appends its lines here when set: the one place they survive a pytest-xdist worker (RDR-258 Phase 4).
REPORT_FILE_ENV = "NX_LEDGER_REPORT_FILE"
BLOCK = "block"
REPORT = "report"
MODES = (BLOCK, REPORT)

FINDING_PREFIX = "LEDGER-FINDING"
SUMMARY_PREFIX = "LEDGER-REPORT"
VACUOUS_PREFIX = "LEDGER-VACUOUS"

#: Where report lines go. ``None`` is standard output; a test module whose output pytest captures installs a sink that
#: writes past the capture (tests/_ledger_terminal.py), so a passing run still shows its findings.
_sink: Callable[[str], None] | None = None


class LedgerModeError(ValueError):
    """``NX_LEDGER_MODE`` holds something other than ``block`` or ``report``."""


def set_sink(sink: Callable[[str], None] | None) -> None:
    global _sink
    _sink = sink


def mode(environ: Mapping[str, str] | None = None) -> str:
    raw = (os.environ if environ is None else environ).get(ENV, "").strip().lower()
    if not raw:
        return BLOCK
    if raw not in MODES:
        raise LedgerModeError(f"{ENV}={raw!r}: must be one of {', '.join(MODES)} (unset means {BLOCK})")
    return raw


def is_report(environ: Mapping[str, str] | None = None) -> bool:
    return mode(environ) == REPORT


def _flat(text: str) -> str:
    return " ".join(text.split())


def _annotation(ledger: str, text: str) -> str:
    esc = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    title = ledger.replace("%", "%25").replace(",", "%2C").replace(":", "%3A")
    return f"::warning title={title}::{esc}"


def _stdout(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def settle(ledger: str, findings: list[str], examined: int, *, environ: Mapping[str, str] | None = None) -> bool:
    """True when the check passes. In block mode that is ``not findings`` and nothing is printed. In report mode it is
    ``examined > 0``, after the findings and the summary are printed (and appended to ``NX_LEDGER_REPORT_FILE`` when set)."""
    env = os.environ if environ is None else environ
    if mode(env) == BLOCK:
        return not findings
    annotate = env.get("GITHUB_ACTIONS") == "true"
    lines: list[str] = []
    for text in findings:
        lines.append(f"{FINDING_PREFIX} {ledger}: {_flat(text)}")
        if annotate:
            lines.append(_annotation(ledger, _flat(text)))
    lines.append(f"{SUMMARY_PREFIX} {ledger}: {len(findings)} finding(s), {examined} item(s) examined (mode={REPORT})")
    if examined <= 0:
        lines.append(f"{VACUOUS_PREFIX} {ledger}: examined 0 items; report mode does not pass a check that examined nothing")
    emit = _sink or _stdout
    for line in lines:
        emit(line)
    path = env.get(REPORT_FILE_ENV, "").strip()
    if path:
        # pytest-xdist workers print to a pipe nothing reads; a file every worker appends to survives them (one short write
        # per line, so concurrent workers do not interleave inside a line). A CI step prints the file after the run.
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return examined > 0
