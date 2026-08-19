#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Inbound-relay ack gate: find conexus-to-nexus REQUEST/QUESTION/ASK/P1
relays in T2 that never became a nexus bead (nexus-w374z).

The failure class this exists for: conexus wrote a P1 REQUEST relay to T2
on 2026-07-12 (conexus/conexus-to-nexus-REQUEST-nx-mcp-self-minting-client-
gap-2026-07-12 [20682], their bead conexus-bv5z). It was never acknowledged
nor turned into a nexus bead — five weeks and four client releases passed
until conexus re-raised it live and nexus-wrwb7 was filed by hand. The T2
relay bus has no delivery/ack guarantee: a session that is not the one live
at write time never sees an inbound entry, and nothing swept for
unacknowledged inbound requests. This script is that sweep.

Ack convention (agreed with conexus 2026-08-16, verified against the real
nexus-wrwb7/nexus-w374z beads): the receiving side files a nexus bead whose
DESCRIPTION carries the T2 relay's numeric id (e.g. "[20682]") and/or the
relay's full title verbatim (e.g. "conexus-to-nexus-REQUEST-nx-mcp-self-
minting-client-gap-2026-07-12"). A bead carrying either counts as an ack.

Enumeration: ``nx memory list -p conexus`` (unpaginated — verified against
a live 509-entry project population, no truncation footer). Ack check:
``bd search "nexus-" --desc-contains <probe> --status all --json`` (the
literal string "nexus-" is an ID-prefix match that every nexus-* bead id
satisfies, used here only to route around bd's "search query is required"
requirement — the real filter is --desc-contains, which is documented
case-insensitive) plus a title/ID substring probe tried in both original
and case-flipped form (nexus-ayfxh: bd search's title/ID match is
case-sensitive).

Exit codes: 0 = clean (>=1 relay title recognized, 0 unacked-stale
findings); 1 = findings (unacked-stale relay(s), OR the non-vacuity
BLINDSPOT case: enumeration succeeded but recognized zero relay-marker
titles in the whole corpus — never reported as a silent clean); 2 =
cannot-check (nx or bd unreachable/erroring — never a silent all-clear).

Intended surfaces: the release-cadence / shakedown playbook, ad-hoc
grooming. The SessionStart-hook / `nx doctor` surfaces named in the parent
bead (nexus-w374z) are NOT built here — that wiring is conexus-plugin-tree
work outside this script's scope; this script is the executable sweep the
future SessionStart/doctor integration would call. See nexus-w374z comments
for the residue note. Run: python3 scripts/check_inbound_relay_acks.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

INBOUND_PREFIX = "conexus-to-nexus-"
MARKER_TOKENS = {"REQUEST", "QUESTION", "QUESTIONS", "ASK", "P1"}

_MEMORY_LINE_RX = re.compile(
    r"^\[(?P<id>\d+)\]\s+(?P<project>[^/]+)/(?P<title>\S+)\s+"
    r"\((?P<agent>[^,]*),\s*(?P<ts>[^)]+)\)\s*$"
)


class SweepUnrunnableError(RuntimeError):
    """Raised when enumeration or the ack check cannot run at all —
    always maps to exit code 2, never a silent clean pass."""


@dataclass(frozen=True)
class RelayEntry:
    id: str
    project: str
    title: str
    agent: str
    timestamp: str
    marker: str


# ---------------------------------------------------------------------
# Pure parsing / classification (unit-tested with planted fixture text —
# no live nx/bd call in this section)
# ---------------------------------------------------------------------


def classify_relay_title(title: str) -> str | None:
    """Return the marker token (REQUEST/QUESTION/QUESTIONS/ASK/P1) if
    `title` is an inbound conexus-to-nexus relay carrying one; else None.
    """
    if not title.startswith(INBOUND_PREFIX):
        return None
    rest = title[len(INBOUND_PREFIX):]
    token = rest.split("-", 1)[0].upper()
    if token in MARKER_TOKENS:
        return token
    return None


def parse_memory_list_output(raw: str) -> list[RelayEntry]:
    """Parse ``nx memory list -p conexus`` line-oriented output into
    structured entries. Lines that don't match the expected shape are
    skipped (not an error — the listing may carry a trailing footer).
    """
    entries: list[RelayEntry] = []
    for line in raw.splitlines():
        line = line.rstrip()
        if not line:
            continue
        m = _MEMORY_LINE_RX.match(line)
        if not m:
            continue
        title = m.group("title")
        marker = classify_relay_title(title)
        if marker is None:
            continue
        entries.append(
            RelayEntry(
                id=m.group("id"),
                project=m.group("project"),
                title=title,
                agent=m.group("agent"),
                timestamp=m.group("ts"),
                marker=marker,
            )
        )
    return entries


def parse_timestamp(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def age_in_days(entry: RelayEntry, now: dt.datetime) -> float:
    return (now - parse_timestamp(entry.timestamp)).total_seconds() / 86400.0


def select_stale(
    entries: list[RelayEntry], max_age_days: int, now: dt.datetime
) -> list[RelayEntry]:
    return [e for e in entries if age_in_days(e, now) > max_age_days]


def build_ack_probes(entry: RelayEntry) -> dict[str, str]:
    """The two independent probes to check a nexus bead's description (or
    title/ID) against: the bare T2 numeric id, and the full relay title
    verbatim — both observed in the real nexus-wrwb7/nexus-w374z acks."""
    return {"id": entry.id, "title": entry.title}


def id_probe_matches(text: str, relay_id: str) -> bool:
    """True iff *text* references *relay_id* in an ANCHORED form: the
    bracketed ``[id]`` convention this repo writes T2 ids in, or the bare
    id at word boundaries. Substantive-critic on nexus-w374z: the original
    bare substring probe was a latent false-ack surface (id ``2137``
    matching inside ``21374``, or a line number) — exactly the silent
    false-ack class this gate exists to prevent, so the match is anchored
    here, client-side, after bd's substring --desc-contains narrows."""
    return re.search(
        rf"\[{re.escape(relay_id)}\]|\b{re.escape(relay_id)}\b", text
    ) is not None


def is_acked(
    entry: RelayEntry,
    desc_search: Callable[[str], bool],
    title_search: Callable[[str], bool],
    desc_id_search: Callable[[str], bool] | None = None,
) -> bool:
    """True if a nexus bead references this relay entry. `desc_search` and
    `title_search` are injected IO boundaries (bd search wrappers in
    production, fakes in unit tests) so this function stays pure over its
    inputs. The production id-probe desc_search applies
    :func:`id_probe_matches` anchoring to the returned rows — see
    ``bd_desc_id_search``."""
    probes = build_ack_probes(entry)
    if (desc_id_search or desc_search)(probes["id"]):
        return True
    if desc_search(probes["title"]):
        return True
    if title_search(probes["id"]):
        return True
    return False


def probe_in_jsonl_text(text: str, probe: str) -> bool:
    """Degraded-mode fallback: does `probe` occur anywhere in the raw
    .beads/issues.jsonl text? Pure function over injected text — no file
    IO — so it's unit-testable without a real .beads directory."""
    return probe in text


# ---------------------------------------------------------------------
# IO boundary (subprocess wrappers) — thin, not unit-tested directly
# ---------------------------------------------------------------------


def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def fetch_memory_listing(project: str) -> str:
    try:
        r = _run(["nx", "memory", "list", "-p", project])
    except FileNotFoundError as exc:
        raise SweepUnrunnableError(f"nx not found on PATH: {exc}") from exc
    if r.returncode != 0:
        raise SweepUnrunnableError(
            f"nx memory list -p {project} rc={r.returncode}: {r.stderr.strip()[:300]}"
        )
    return r.stdout


def _bd_json(args: list[str]) -> list:
    try:
        r = _run(["bd", *args])
    except FileNotFoundError as exc:
        raise SweepUnrunnableError(f"bd not found on PATH: {exc}") from exc
    if r.returncode != 0:
        raise SweepUnrunnableError(
            f"bd {' '.join(args)} rc={r.returncode}: {r.stderr.strip()[:300]}"
        )
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        raise SweepUnrunnableError(
            f"bd {' '.join(args)} returned non-JSON output: {exc}"
        ) from exc
    if isinstance(data, dict) and "error" in data:
        raise SweepUnrunnableError(f"bd {' '.join(args)} error: {data['error']}")
    return data if isinstance(data, list) else []


def bd_desc_search(probe: str) -> bool:
    return len(_bd_json(["search", "nexus-", "--desc-contains", probe, "--status", "all", "--json"])) > 0


def bd_desc_id_search(relay_id: str) -> bool:
    """Numeric-id ack probe: bd's --desc-contains is substring-only, so its
    hits are re-verified client-side with :func:`id_probe_matches` anchoring
    (nexus-w374z critique: unanchored, id 2137 false-acks against 21374)."""
    rows = _bd_json(["search", "nexus-", "--desc-contains", relay_id, "--status", "all", "--json"])
    return any(
        id_probe_matches(str(row.get("description", "")) + " " + str(row.get("title", "")), relay_id)
        for row in rows
        if isinstance(row, dict)
    )


def bd_title_search(probe: str) -> bool:
    for variant in {probe, probe.upper(), probe.lower()}:
        if _bd_json(["search", variant, "--status", "all", "--json"]):
            return True
    return False


def _grep_jsonl_fallback(probe: str) -> bool:
    path = Path(".beads/issues.jsonl")
    if not path.exists():
        return False
    try:
        return probe_in_jsonl_text(path.read_text(errors="replace"), probe)
    except OSError:
        return False


# ---------------------------------------------------------------------
# main
# ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-age-days", type=int, default=7,
        help="Minimum age (days) before an unacked relay is flagged (default 7)",
    )
    parser.add_argument(
        "--project", default="conexus",
        help="T2 project namespace to enumerate inbound relays from (default: conexus)",
    )
    args = parser.parse_args(argv)

    try:
        raw = fetch_memory_listing(args.project)
    except SweepUnrunnableError as exc:
        print(f"SWEEP UNRUNNABLE: {exc}")
        return 2

    total_lines = len([ln for ln in raw.splitlines() if ln.strip()])
    if total_lines == 0:
        print(f"SWEEP UNRUNNABLE: nx memory list -p {args.project} returned no entries at all "
              "— not evidence of cleanliness")
        return 2

    inbound = parse_memory_list_output(raw)
    if not inbound:
        print(
            f"BLINDSPOT: 0 relay titles recognized among {total_lines} T2 entries scanned "
            f"in project '{args.project}' — the sweep found no conexus-to-nexus-"
            f"(REQUEST|QUESTION|ASK|P1)* marker at all. This is NOT confirmed clean; "
            "verify the title-shape assumption still holds."
        )
        return 1

    now = dt.datetime.now(dt.timezone.utc)
    stale = select_stale(inbound, args.max_age_days, now)

    if not stale:
        print(
            f"relay ack sweep: {len(inbound)} relay title(s) recognized in project "
            f"'{args.project}', 0 older than {args.max_age_days}d — clean"
        )
        return 0

    findings: list[str] = []
    try:
        for entry in stale:
            if is_acked(entry, bd_desc_search, bd_title_search, bd_desc_id_search):
                continue
            age = age_in_days(entry, now)
            findings.append(
                f"{entry.id} ({entry.marker}, {age:.1f}d old): {entry.project}/{entry.title} "
                "has no referencing nexus bead — file one and record the T2 id + relay title "
                "in its description as the ack"
            )
    except SweepUnrunnableError as exc:
        print(f"BD-UNAVAILABLE: {exc}")
        print(
            "Attempting degraded .beads/issues.jsonl grep fallback for informational purposes "
            "only — this file is a partial export and a miss here does NOT prove no ack exists."
        )
        # Best-effort degraded check against whatever remains unverified.
        remaining = [e for e in stale]
        any_grep_hit = False
        for entry in remaining:
            probes = build_ack_probes(entry)
            if _grep_jsonl_fallback(probes["id"]) or _grep_jsonl_fallback(probes["title"]):
                any_grep_hit = True
                print(f"  degraded-grep hit for {entry.id} — still UNVERIFIED without bd")
        if not any_grep_hit:
            print("  degraded-grep found no hits for any stale entry — still UNVERIFIED without bd")
        return 2

    if findings:
        for f in findings:
            print(f"RELAY-UNACKED: {f}")
        print(
            f"\n{len(findings)} unacked stale relay(s) of {len(stale)} checked "
            f"({len(inbound)} relay title(s) recognized total). File a nexus bead per finding."
        )
        return 1

    print(
        f"relay ack sweep: {len(inbound)} relay title(s) recognized, {len(stale)} older than "
        f"{args.max_age_days}d, all acked — clean"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
