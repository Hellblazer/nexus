# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 3 measurement (nexus-jypmj.3.10): drafter JSON to a drafted oracle label file.

A drafting agent reads the blind packet (:mod:`bench.rdr232_oracle_packet`) and answers, per request, which retrieved
chunk supports or contradicts which golden facet::

    {"<qid>": {"<chunk chash>": {"<facet slug>": "supports" | "contradicts"}}}

A facet the drafter leaves out of a chunk is ``neutral``, and a chunk it leaves out is neutral for every facet. This
module turns one or more such files, plus the packet they were drafted from, into the drafted YAML the loader reads
(``golden_oracle_labels_drafted_v1.yaml`` or ``golden_heldout_oracle_labels_drafted_v1.yaml``): ``provenance: drafted``,
chunk-level items keyed by ``chash``, every readable packet item of a drafted request present and closed (so a chunk
with no label is neutral for every facet).

**Chunks are keyed by chash, never by item id.** A repair round's merged anchors can reuse request-local ids (a held-out
request lists ``e24``..``e33`` twice, each with another chunk), so an item id does not name a chunk. A packet item that
repeats an earlier item's chash is the same chunk and is written once.

It refuses, naming the offender: a draft for a request not in the packet or the set; a request the hand file already
labels (a drafted label never shadows a hand label); a chash that is not one of that request's packet items; a facet
slug that is not one of that request's required facets; a role other than ``supports``, ``contradicts`` or ``neutral``;
a label on a chunk whose text the packet could not fetch (``text: null``: an unread chunk cannot be labelled); and two
draft files that disagree about one (request, chunk, facet). A readable chunk is written; an unread one is left out
and counted (``items_unread_omitted``), never written as neutral. The written file is loaded back through
:func:`bench.rdr232_oracle.load_oracle` before the command succeeds.

Run from the repository root::

    PYTHONPATH=scripts uv run python -m bench.rdr232_oracle_drafts --packet PACKET.json --drafts d1.json d2.json \\
        --set heldout --out bench/queries/golden_heldout_oracle_labels_drafted_v1.yaml
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_BENCH_PARENT = _Path(__file__).resolve().parent.parent
if str(_BENCH_PARENT) not in _sys.path:
    _sys.path.insert(0, str(_BENCH_PARENT))

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import click
import yaml

from bench.rdr232_oracle import GOLDEN_SET_VERSION, ORACLE_VERSION, SET_FILES, OracleSet, load_oracle
from bench.rdr232_oracle_packet import PACKET_VERSION
from bench.schema import Query, load_golden_set

#: The roles a drafter writes; ``neutral`` is the default for anything left out, and is accepted (and dropped) if written.
DRAFT_ROLES: tuple[str, ...] = ("supports", "contradicts")

FILE_HEADER = """\
# RDR-232 oracle labels, DRAFTED provenance (nexus-jypmj.3.10). Written by scripts/bench/rdr232_oracle_drafts.py from a
# drafter's JSON and the blind packet it read; do not edit by hand. Items are keyed by chash; an item with no facets is
# neutral for every facet (the drafter read it and found nothing). Rates over these labels are reported apart from hand
# labels (scripts/bench/rdr232_oracle.py).
"""

Drafts = Mapping[str, Mapping[str, Mapping[str, str]]]


def _packet_requests(packet: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    if packet.get("packet_version") != PACKET_VERSION:
        raise ValueError(f"packet_version must be {PACKET_VERSION}, got {packet.get('packet_version')!r}")
    out: dict[str, Mapping[str, Any]] = {}
    for r in packet.get("requests") or []:
        qid = str(r.get("qid"))
        if qid in out:
            raise ValueError(f"the packet lists request {qid} twice")
        out[qid] = r
    return out


def merge_drafts(drafts: Sequence[tuple[str, Drafts]]) -> dict[str, dict[str, dict[str, str]]]:
    """Fold several drafter files (``(name, content)``) into one ``qid -> chash -> slug -> role``. Two files may split a
    request's chunks between them; they may not give one (request, chunk, facet) two roles."""
    merged: dict[str, dict[str, dict[str, str]]] = {}
    for name, content in drafts:
        if not isinstance(content, Mapping):
            raise ValueError(f"{name}: a drafts file is a JSON object of qid -> chash -> facet -> role")
        for qid, by_chash in content.items():
            if not isinstance(by_chash, Mapping):
                raise ValueError(f"{name}: {qid} must map chash -> facet -> role")
            req = merged.setdefault(str(qid), {})
            for chash, facets in by_chash.items():
                if not isinstance(facets, Mapping):
                    raise ValueError(f"{name}: {qid} {chash} must map facet -> role")
                slot = req.setdefault(str(chash), {})
                for slug, role in facets.items():
                    if slug in slot and slot[slug] != role:
                        raise ValueError(f"{name}: {qid} {chash} {slug}: {role!r} contradicts {slot[slug]!r} from an earlier drafts file")
                    slot[str(slug)] = role
    return merged


def convert(
    packet: Mapping[str, Any], drafts: Drafts, queries: Mapping[str, Query], hand: OracleSet,
) -> tuple[dict[str, Any], dict[str, int]]:
    """The drafted label file as a dict, and the counts of what went into it."""
    in_packet = _packet_requests(packet)
    counts = dict.fromkeys((
        "requests", "items", "items_labelled", "items_unlabelled", "items_unread_omitted", "items_duplicate_chash",
        "pairs_supports", "pairs_contradicts",
    ), 0)
    requests: dict[str, Any] = {}
    for qid, by_chash in drafts.items():
        if qid not in in_packet:
            raise ValueError(f"drafts for {qid}, which the packet does not hold")
        if qid not in queries:
            raise ValueError(f"drafts for {qid}, which is not a request of this --set")
        if qid in hand.requests and not hand.requests[qid].todo:
            raise ValueError(f"{qid} is labelled in the hand file; a drafted label never shadows a hand label")
        slugs = set(queries[qid].required_facets)
        packet_slugs = set(in_packet[qid].get("facet_slugs") or [])
        if packet_slugs != slugs:
            raise ValueError(f"{qid}: the packet's facet_slugs {sorted(packet_slugs)} are not the set's required_facets {sorted(slugs)}")
        by_item: dict[str, Mapping[str, Any]] = {}
        for it in in_packet[qid].get("items") or []:
            chash = str(it.get("chash") or "")
            if not chash:
                counts["items_unread_omitted"] += 1
            elif chash in by_item:
                counts["items_duplicate_chash"] += 1
            else:
                by_item[chash] = it
        for chash, facets in by_chash.items():
            if chash not in by_item:
                raise ValueError(f"{qid}: chash {chash} is not one of the request's packet items")
            for slug, role in facets.items():
                if slug not in slugs:
                    raise ValueError(f"{qid} {chash}: facet {slug!r} is not a required facet of the request ({sorted(slugs)})")
                if role not in (*DRAFT_ROLES, "neutral"):
                    raise ValueError(f"{qid} {chash} {slug}: role {role!r} must be one of {list(DRAFT_ROLES)} (neutral is the default: leave it out)")
            if any(r in DRAFT_ROLES for r in facets.values()) and by_item[chash].get("text") is None:
                raise ValueError(f"{qid}: chash {chash} is labelled but the packet holds no text for it (an unread chunk cannot be labelled)")
        items: list[dict[str, Any]] = []
        for chash, it in by_item.items():
            if it.get("text") is None:
                counts["items_unread_omitted"] += 1
                continue
            labels = {s: r for s, r in (by_chash.get(chash) or {}).items() if r in DRAFT_ROLES}
            counts["items"] += 1
            counts["items_labelled" if labels else "items_unlabelled"] += 1
            counts["pairs_supports"] += sum(1 for r in labels.values() if r == "supports")
            counts["pairs_contradicts"] += sum(1 for r in labels.values() if r == "contradicts")
            note = (str(it["text"]).strip().splitlines() or [""])[0][:80]
            items.append({"chash": chash, "closed": True, "note": note, "facets": {s: labels[s] for s in sorted(labels)}})
        if not items:
            raise ValueError(f"{qid}: no readable packet item to draft (a drafted entry labels something)")
        requests[qid] = {"provenance": "drafted", "items": items}
        counts["requests"] += 1
    ordered = {qid: requests[qid] for qid in in_packet if qid in requests}
    return {"oracle_version": ORACLE_VERSION, "golden_set_version": GOLDEN_SET_VERSION, "requests": ordered}, counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--packet", type=Path, required=True, help="the packet the drafter read (bench.rdr232_oracle_packet --out)")
    ap.add_argument("--drafts", type=Path, nargs="+", required=True, help="drafter JSON files: {qid: {chash: {facet_slug: supports|contradicts}}}")
    ap.add_argument("--set", default="golden", choices=tuple(SET_FILES), help="which query set the drafts label (default: golden)")
    ap.add_argument("--hand", type=Path, help="the hand label file (default: the set's committed file)")
    ap.add_argument("--out", type=Path, required=True, help="the drafted YAML to write")
    args = ap.parse_args(argv)
    files = SET_FILES[args.set]
    try:
        packet = json.loads(args.packet.read_text(encoding="utf-8"))
        loaded = [(str(p), json.loads(p.read_text(encoding="utf-8"))) for p in args.drafts]
        queries = load_golden_set(files.queries)
        hand = load_oracle(args.hand or files.hand, queries)
        body, counts = convert(packet, merge_drafts(loaded), {q.qid: q for q in queries}, hand)
    except (ValueError, OSError) as exc:
        click.echo(f"FAIL: {exc}", err=True)
        return 2
    args.out.write_text(FILE_HEADER + yaml.safe_dump(body, sort_keys=False, width=100), encoding="utf-8")
    try:
        merged = load_oracle(args.hand or files.hand, queries, drafted_path=args.out)
    except ValueError as exc:
        args.out.unlink()
        click.echo(f"FAIL: the file written does not load, so it was removed: {exc}", err=True)
        return 2
    click.echo(json.dumps({"out": str(args.out), **counts, "provenance": merged.provenance_summary()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
