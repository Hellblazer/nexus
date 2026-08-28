#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deferral-hygiene sweep: find deferred/world-blocked beads whose blocking
condition has gone stale (nexus-<hygiene-bead>, Hal 2026-08-18).

The failure class this exists for: nexus-70r3c.18's OWN NOTES recorded its
world-block as cleared on 2026-06-19 ("xr7.8.9 PASSED+CLOSED 06-12,
re-point owner decision pending") and the bead stayed deferred for two
months through groomings, shakedowns, and a full RDR audit — because
nobody's job was "is this deferral still true." Two more instances found
by the first retro sweep: nexus-whh61.1 (a RELEASE-GATE line contradicted
by its own later deferral note, five releases shipped past it) and
nexus-0x6b (deferral date expired 3 months earlier, never resurfaced).

Checks, per deferred bead (and open beads with world-blocked markers):
  1. EXPIRED DATE: a ``Deferred: YYYY-MM-DD`` date in the past.
  2. CONTRADICTION MARKERS in the bead's full text: phrases that historically
     mean "the block cleared but nobody acted" (STALE-REF, PASSED+CLOSED,
     UN-DEFERRED, "no longer blocked", CLEARED, "gate PASSED" inside a
     deferred bead) or competing directives (RELEASE-GATE inside a deferred
     bead).
  3. STALE TITLE MARKERS: an OPEN bead whose title still says WORLD-BLOCKED.

Exit codes: 0 = clean; 1 = findings (each printed with bead id + reason);
2 = sweep could not run (bd unavailable / zero beads read — an unrunnable
sweep is never a pass, the vacuous-gate lesson).

Intended surfaces: the shakedown playbook's deferral-hygiene row (every
release) and ad-hoc grooming. Run: python3 scripts/check_deferral_staleness.py
"""
from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys

MARKERS = [
    r"STALE-REF",
    r"PASSED\+CLOSED",
    r"UN-DEFERRED",
    r"no longer blocked",
    r"\bCLEARED\b",
    r"gate PASSED",
    r"RELEASE-GATE",
]
MARKER_RX = re.compile("|".join(MARKERS), re.IGNORECASE)
DATE_RX = re.compile(r"Deferred: (\d{4}-\d{2}-\d{2})")
ID_RX = re.compile(r"(nexus-[a-z0-9.]+)")

# A "Deferral-sweep verdict" note suppresses marker findings for its bead --
# but only for VERDICT_TTL_DAYS from the date the note carries. A judgment
# recorded once and honoured forever re-creates, one level up, the exact
# failure this sweep exists for (a condition judged once and never
# re-examined; nexus-arsjx). An undated verdict cannot age, so it does not
# suppress at all.
VERDICT_RX = re.compile(r"Deferral-sweep verdict[^\n]{0,40}?(\d{4}-\d{2}-\d{2})")
VERDICT_TTL_DAYS = 90

# The open-bead scan reads one page. A page that comes back FULL may have
# been truncated, and a truncated scan that exits 0 is the vacuous-gate
# shape (nexus-moht0) inside the script written to prevent it. Sized well
# above the population (415 on 2026-08-27) and asserted below.
OPEN_SCAN_LIMIT = 5000


def _bd(*args: str) -> str:
    r = subprocess.run(["bd", *args], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        print(f"SWEEP UNRUNNABLE: bd {' '.join(args)} rc={r.returncode}: {r.stderr.strip()[:200]}")
        sys.exit(2)
    return r.stdout


def main() -> int:
    today = dt.date.today()
    findings: list[str] = []

    deferred_listing = _bd("list", "--status=deferred")
    deferred_ids = sorted(set(ID_RX.findall(deferred_listing)))
    if not deferred_ids:
        # Zero deferred beads is plausible but suspicious enough to verify:
        # bd must have answered (it did, rc=0). Accept as clean.
        pass

    for bid in deferred_ids:
        body = _bd("show", bid)
        for m in DATE_RX.finditer(body):
            d = dt.date.fromisoformat(m.group(1))
            if d < today:
                findings.append(
                    f"{bid}: deferral date {d} is in the past — expired, never resurfaced"
                )
        # A recorded "Deferral-sweep verdict" note acknowledges marker findings:
        # a human judged the flagged text and recorded VALID/stale. Marker
        # findings are suppressed for that bead until its notes change the
        # verdict; expired-date findings are NEVER suppressed (a date is a
        # promise, not a judgment).
        if "Deferral-sweep verdict" in body:
            m_verdict = VERDICT_RX.search(body)
            verdict_date = dt.date.fromisoformat(m_verdict.group(1)) if m_verdict else None
            if verdict_date and (today - verdict_date).days <= VERDICT_TTL_DAYS:
                continue
            findings.append(
                f"{bid}: Deferral-sweep verdict is "
                + (f"older than {VERDICT_TTL_DAYS} days ({verdict_date})" if verdict_date else "undated")
                + " — re-verify the block and re-record the verdict with today's date"
            )
        for m in MARKER_RX.finditer(body):
            findings.append(
                f"{bid}: deferred bead carries contradiction marker {m.group(0)!r} — "
                "verify the block still holds"
            )

    open_listing = _bd("list", "--status=open", f"--limit={OPEN_SCAN_LIMIT}")
    open_rows = [line for line in open_listing.splitlines() if ID_RX.search(line)]
    if len(open_rows) >= OPEN_SCAN_LIMIT:
        print(
            f"SWEEP UNRUNNABLE: the open-bead listing filled --limit={OPEN_SCAN_LIMIT} "
            "— the WORLD-BLOCKED scan would silently under-scan; raise OPEN_SCAN_LIMIT"
        )
        return 2
    for line in open_listing.splitlines():
        if re.search(r"WORLD-BLOCKED", line, re.IGNORECASE):
            ids = ID_RX.findall(line)
            if ids:
                findings.append(
                    f"{ids[0]}: OPEN bead title still says WORLD-BLOCKED — stale title marker"
                )

    if not deferred_ids and not open_listing.strip():
        print("SWEEP UNRUNNABLE: bd returned no beads at all — not evidence of cleanliness")
        return 2

    if findings:
        # De-dup while preserving order
        seen: set[str] = set()
        for f in findings:
            if f not in seen:
                seen.add(f)
                print(f"DEFERRAL-STALE: {f}")
        print(f"\n{len(seen)} finding(s). Each needs a human verdict: still-valid (re-date "
              "or re-note it) or stale (un-defer / amend). Silence is the only wrong answer.")
        return 1

    print(f"deferral hygiene: {len(deferred_ids)} deferred bead(s) swept, 0 stale conditions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
