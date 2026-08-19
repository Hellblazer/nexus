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
minting-client-gap-2026-07-12"). A bead carrying either counts as an ack —
UNLESS it is a bulk enumeration: one bead referencing BULK_ACK_THRESHOLD+
of the current stale set is a sweep artifact, reported but never counted
as per-relay acks (T2 [22836]).

ANSWER-ack arm (protocol of record 2026-08-18, T2 conexus [22834] QUESTION
-> [22835] ANSWER, option (a) amended): QUESTION/QUESTIONS relays also
close via a T2 ANSWER entry whose BODY references the question title
verbatim (anchored). A grandfathered exact title-suffix-pair form exists
for pre-protocol history only (questions stamped before PROTOCOL_CUTOVER).
REQUEST/ASK/P1 always require a bead, id recorded back to the sender
(conexus-bgpi).

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


#: Question-shaped markers the ANSWER-ack protocol applies to. Protocol of
#: record (2026-08-18, T2 conexus [22834] QUESTION -> [22835] ANSWER, option
#: (a) amended): a QUESTION relay closes via a T2 ANSWER entry whose BODY
#: references the QUESTION title (match by reference, not title convention);
#: no bead required unless the answer spawned real work (then the ANSWER
#: names the bead). REQUEST (and the request-shaped ASK/P1) relays ALWAYS
#: require a bead, id recorded back to the sender (conexus-bgpi).
ANSWERABLE_MARKERS = {"QUESTION", "QUESTIONS"}

#: The marker token must occupy the MARKER POSITION (immediately after the
#: direction prefix), mirroring classify_relay_title's anchoring — an
#: unanchored "-ANSWER-" scan misclassified titles that merely CONTAIN the
#: word (e.g. a QUESTION about the nx_answer tool), feeding false answer
#: candidates into the body matcher. REPLY/RESPONSE are live answer-shaped
#: variants in the corpus (e.g. conexus-to-nexus-REPLY-BUG-0148-...).
_ANSWER_TITLE_RX = re.compile(
    r"^[a-z]+-to-[a-z]+-(?:ANSWER|ANSWERS|REPLY|RESPONSE)-", re.IGNORECASE
)


def is_answer_title(title: str) -> bool:
    """True for relay-shaped titles whose MARKER slot carries an
    answer-shaped token, either direction (conexus-to-nexus-ANSWER-*
    answers our questions; the protocol reply itself arrived that way)."""
    return _ANSWER_TITLE_RX.match(title) is not None


def parse_answer_titles(raw: str) -> list[tuple[str, str]]:
    """(project, title) pairs for every ANSWER-marked relay title in a
    ``nx memory list`` output."""
    out: list[tuple[str, str]] = []
    for line in raw.splitlines():
        m = _MEMORY_LINE_RX.match(line.rstrip())
        if not m:
            continue
        if is_answer_title(m.group("title")):
            out.append((m.group("project"), m.group("title")))
    return out


def answer_acks_question(answer_body: str, question_title: str) -> bool:
    """The protocol's match-by-reference rule: the ANSWER's body must carry
    the QUESTION title verbatim, ANCHORED — the title must not continue
    into more title characters (a title that is a PREFIX of another title,
    e.g. ``...-2026-06-25`` vs ``...-2026-06-25-r2``, must not be acked by
    an answer that references only the longer one). Same standing policy
    as :func:`id_probe_matches`: every string-containment ack check in this
    file anchors; unanchored substring matching is the false-ack class the
    gate exists to prevent."""
    return re.search(re.escape(question_title) + r"(?![A-Za-z0-9-])", answer_body) is not None


def _relay_suffix(title: str) -> str | None:
    """The part after the direction prefix and marker token:
    ``conexus-to-nexus-QUESTION-foo-bar-2026-07-06`` -> ``foo-bar-2026-07-06``.
    None when the title is not marker-shaped."""
    m = re.match(r"^[a-z]+-to-[a-z]+-([A-Z0-9]+)-(.+)$", title)
    return m.group(2) if m else None


#: The ack protocol's agreement date ([22834] -> [22835]). QUESTION entries
#: STAMPED BEFORE this date may be acked by the grandfathered suffix-pair
#: form below; entries from the protocol era onward must use the mandated
#: body reference — the counterparty's ANSWER explicitly rejected title
#: convention as the standing match key ("titles drift, references don't"),
#: and ordinary future exchanges often reuse the suffix across the
#: direction swap, so an ungated pair-check would silently bypass the
#: reference check for the common case (substantive-critic, T2 [22836]).
PROTOCOL_CUTOVER = dt.datetime(2026, 8, 19, tzinfo=dt.timezone.utc)


def answer_title_pairs_question(
    answer_title: str, question_title: str, question_ts: str = "",
) -> bool:
    """GRANDFATHERED ack form for PRE-PROTOCOL history only (2026-07 era):
    an ANSWER whose title suffix EXACTLY equals the question's title suffix
    (``...-QUESTION-nexus-ehc4q-status-2026-07-06`` answered by
    ``...-ANSWER-nexus-ehc4q-status-2026-07-06``). Exact equality only —
    never fuzzy — and ONLY for questions stamped before PROTOCOL_CUTOVER;
    post-cutover questions require the body reference (see
    PROTOCOL_CUTOVER's comment). Historical answers reference the SUBJECT,
    not the title, which is the sole reason this form exists."""
    if question_ts:
        try:
            if parse_timestamp(question_ts) >= PROTOCOL_CUTOVER:
                return False
        except ValueError:
            return False  # unparseable stamp: no grandfather, safe direction
    if not is_answer_title(answer_title):
        return False
    qs, ans = _relay_suffix(question_title), _relay_suffix(answer_title)
    return qs is not None and qs == ans


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


def fetch_memory_body(project: str, title: str) -> str:
    """Body text of one T2 entry, or "" when unfetchable (the caller treats
    an unfetchable ANSWER candidate as not-an-ack — the flag stays up, the
    safe over-inclusive direction)."""
    try:
        r = _run(["nx", "memory", "get", "-p", project, "-t", title])
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def bd_desc_search(probe: str) -> bool:
    return len(_bd_json(["search", "nexus-", "--desc-contains", probe, "--status", "all", "--json"])) > 0


def bd_desc_id_search(relay_id: str) -> bool:
    """Numeric-id ack probe: bd's --desc-contains is substring-only, so its
    hits are re-verified client-side with :func:`id_probe_matches` anchoring
    (nexus-w374z critique: unanchored, id 2137 false-acks against 21374)."""
    return bool(bd_desc_id_ack_beads(relay_id))


def bd_desc_id_ack_beads(relay_id: str) -> set[str]:
    """Bead ids whose description/title carries an ANCHORED reference to
    *relay_id* — the id-granular form the bulk-ack demotion needs."""
    rows = _bd_json(["search", "nexus-", "--desc-contains", relay_id, "--status", "all", "--json"])
    return {
        str(row.get("id", ""))
        for row in rows
        if isinstance(row, dict)
        and id_probe_matches(str(row.get("description", "")) + " " + str(row.get("title", "")), relay_id)
        and row.get("id")
    }


#: A single bead referencing this many (or more) of the CURRENT stale relay
#: set is an enumeration artifact (a sweep report pasted into one umbrella
#: bead — e.g. this sweep's own first triage bead, nexus-g03an), not N
#: per-relay acks. Counting it as acks let the sweep print clean the same
#: day it shipped while zero of the six relays had been triaged — a silent
#: all-clear manufactured by the tool's own remedy text (substantive-critic
#: ship-blocker, T2 [22836]). Bulk trackers are REPORTED, and the relays
#: they cover stay findings until each gets a dedicated ack or the bulk
#: bead is split.
BULK_ACK_THRESHOLD = 3


def demote_bulk_ack_beads(
    acks_by_relay: dict[str, set[str]], threshold: int = BULK_ACK_THRESHOLD,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Split per-relay bead acks into (dedicated, bulk). A bead appearing in
    *threshold*+ relays' ack sets is bulk; it is removed from every relay's
    dedicated set and reported separately. Pure — unit-testable."""
    counts: dict[str, int] = {}
    for beads in acks_by_relay.values():
        for b in beads:
            counts[b] = counts.get(b, 0) + 1
    bulk_beads = {b for b, n in counts.items() if n >= threshold}
    dedicated = {
        rid: {b for b in beads if b not in bulk_beads}
        for rid, beads in acks_by_relay.items()
    }
    bulk = {
        rid: {b for b in beads if b in bulk_beads}
        for rid, beads in acks_by_relay.items()
    }
    return dedicated, bulk


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

    # ANSWER-ack arm (protocol of record, [22834]->[22835]): QUESTION-shaped
    # relays close via an ANSWER entry whose body references the question
    # title. Candidate bodies are fetched lazily and cached; an unfetchable
    # body is not an ack.
    answer_candidates = parse_answer_titles(raw)
    _body_cache: dict[tuple[str, str], str] = {}

    def _answered(entry: RelayEntry) -> bool:
        if entry.marker not in ANSWERABLE_MARKERS:
            return False
        for proj, title in answer_candidates:
            if answer_title_pairs_question(title, entry.title, entry.timestamp):
                return True  # grandfathered exact suffix pair (pre-cutover only)
            key = (proj, title)
            if key not in _body_cache:
                _body_cache[key] = fetch_memory_body(proj, title)
            if answer_acks_question(_body_cache[key], entry.title):
                return True
        return False

    findings: list[str] = []
    try:
        # Pass 1: collect id-granular bead acks for every stale entry, then
        # demote enumeration/bulk beads (BULK_ACK_THRESHOLD) — a sweep
        # report pasted into one umbrella bead must not blanket-clear its
        # whole list (T2 [22836] ship-blocker).
        acks_by_relay: dict[str, set[str]] = {
            e.id: bd_desc_id_ack_beads(e.id) for e in stale
        }
        dedicated, bulk = demote_bulk_ack_beads(acks_by_relay)

        for entry in stale:
            if dedicated.get(entry.id):
                continue
            if is_acked(entry, bd_desc_search, bd_title_search, lambda _p: False):
                # title-verbatim / title-in-bead-title forms (id form is
                # handled granularly above; the injected no-op keeps
                # is_acked from re-running the un-demoted id probe).
                continue
            if _answered(entry):
                continue
            age = age_in_days(entry, now)
            bulk_note = ""
            if bulk.get(entry.id):
                bulk_note = (
                    f" (bulk-tracking bead(s) {sorted(bulk[entry.id])} reference it "
                    "as part of an enumeration — NOT counted as a per-relay ack; "
                    "triage it there or give it a dedicated ack)"
                )
            remedy = (
                "answer it with a T2 ANSWER entry referencing this title in its body, "
                "or file a bead if it spawned work"
                if entry.marker in ANSWERABLE_MARKERS
                else "file a bead and record the T2 id + relay title in its description as the ack"
            )
            findings.append(
                f"{entry.id} ({entry.marker}, {age:.1f}d old): {entry.project}/{entry.title} "
                f"has no dedicated ack — {remedy}{bulk_note}"
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
