# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 3 measurement (nexus-jypmj.3.10, critique S3 e): the blind drafting packet for oracle labels.

Sam's 2026-10-28 decision: an agent that never sees the assessor's ledger drafts the oracle labels from the run's
retrieved items. An ``nx_answer`` envelope holds the ledger, the anchors' evidence ids, the findings and an answer
generated from the ledger, so "the agent never looks at them" would rest on the agent's discipline. This extractor
makes it hold by construction: from an envelopes file it writes, per request, ONLY

* ``qid`` and ``question`` (the golden request's own text),
* ``facet_slugs`` (the golden ``required_facets``, which the facet map joins the runtime facets to), and
* per retrieved item its ``item_id``, ``chash`` and ``text``.

It reads exactly one thing from an envelope: ``coverage.anchors[].item_id``, ``chash`` and ``collection`` (the last only
to fetch the text; it is not written). Nothing else in the envelope is touched, and ``tests/test_rdr232_measurement_packet.py``
proves it by spying on every key read and by planting a sentinel in every other field. The anchors carry no text (the
ledger's wire form is identity and provenance, never content), so the text comes from a resolver: the T3 store
(``--live``, read-only) or a JSON map of chunk hash to text (``--texts``).

The drafting agent labels each (item, slug) pair ``supports``, ``contradicts`` or ``neutral`` into
``bench/queries/golden_oracle_labels_drafted_v1.yaml``; see ``bench.rdr232_oracle``.

Run from the repository root::

    PYTHONPATH=scripts uv run python -m bench.rdr232_oracle_packet --envelopes ENV.json --set golden --live --out PACKET.json
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_BENCH_PARENT = _Path(__file__).resolve().parent.parent
if str(_BENCH_PARENT) not in _sys.path:
    _sys.path.insert(0, str(_BENCH_PARENT))

import argparse
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import click

from bench.rdr232_envelope import read_envelopes_file
from bench.rdr232_oracle import GOLDEN_PATH, HELDOUT_PATH
from bench.schema import Query, load_golden_set

PACKET_VERSION = 1
#: ``(chash, collection) -> the chunk's text`` (``None``: it cannot be had).
TextOf = Callable[[str, str], "str | None"]


def anchor_refs(envelope: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """``(item_id, chash, collection)`` of each anchor, in ledger order: the only fields read from an envelope."""
    coverage = envelope.get("coverage")
    if not isinstance(coverage, Mapping):
        return []
    anchors = coverage.get("anchors")
    if not isinstance(anchors, list):
        return []
    return [(str(a.get("item_id") or ""), str(a.get("chash") or ""), str(a.get("collection") or "")) for a in anchors if isinstance(a, Mapping)]


def build_packet(queries: Sequence[Query], envelopes: Mapping[str, Mapping[str, Any]], text_of: TextOf) -> dict[str, Any]:
    """The packet for the *queries* that have an envelope in *envelopes* (qid -> envelope). A request without an
    envelope is skipped and counted. An item whose text cannot be had is kept with ``text: null`` and counted; a text
    that does not hash to its chash is kept and counted (``chash_mismatch``), so a wrong text cannot be labelled unseen."""
    requests: list[dict[str, Any]] = []
    unresolved = mismatch = items_total = without = 0
    for q in queries:
        env = envelopes.get(q.qid)
        if env is None:
            without += 1
            continue
        items: list[dict[str, Any]] = []
        for item_id, chash, collection in anchor_refs(env):
            text = text_of(chash, collection)
            if text is None:
                unresolved += 1
            elif hashlib.sha256(text.encode("utf-8")).hexdigest() != chash:
                mismatch += 1
            items.append({"item_id": item_id, "chash": chash, "text": text})
        items_total += len(items)
        requests.append({"qid": q.qid, "question": q.text, "facet_slugs": list(q.required_facets), "items": items})
    return {
        "packet_version": PACKET_VERSION, "requests": requests,
        "meta": {"requests": len(requests), "items": items_total, "unresolved_text": unresolved, "chash_mismatch": mismatch,
                 **({"without_envelope": without} if without else {})},
    }


def texts_file_resolver(path: Path) -> TextOf:
    """A resolver over a JSON object of chunk hash -> text."""
    table = json.loads(path.read_text(encoding="utf-8"))
    return lambda chash, _collection: table.get(chash)


def live_text_resolver(refs: Sequence[tuple[str, str, str]]) -> TextOf:
    """A read-only resolver over the T3 store, fetching every distinct ``(chash, collection)`` of *refs* up front, one
    batch per collection. A fetched body is matched to its chash by hashing it (the anchor's chash is the sha256 of the
    chunk text without its ``[source: ...]`` line), never by position, so a batch that returns fewer bodies or in
    another order cannot hand an item the wrong text."""
    from nexus.mcp import core  # noqa: PLC0415 — heavy; a live read only
    from nexus.plans.coverage_ledger import strip_source_note  # noqa: PLC0415

    by_collection: dict[str, list[str]] = {}
    for _item, chash, collection in refs:
        if chash and collection and chash not in by_collection.setdefault(collection, []):
            by_collection[collection].append(chash)
    found: dict[str, str] = {}
    for collection, chashes in by_collection.items():
        out = core.store_get_many(ids=chashes, collections=collection, structured=True, max_chars_per_doc=1_000_000)
        for body in (out.get("contents") or []) if isinstance(out, Mapping) else []:
            if isinstance(body, str) and body:
                text = strip_source_note(body)
                found[hashlib.sha256(text.encode("utf-8")).hexdigest()] = text
    return lambda chash, _collection: found.get(chash)


def _queries(which: str, qids: Sequence[str], limit: int | None) -> list[Query]:
    queries: list[Query] = []
    if which in ("golden", "both"):
        queries += load_golden_set(GOLDEN_PATH)
    if which in ("heldout", "both"):
        queries += load_golden_set(HELDOUT_PATH)
    if qids:
        queries = [q for q in queries if q.qid in set(qids)]
    return queries[:limit] if limit else queries


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--envelopes", type=Path, required=True, help="an envelopes file written by bench.rdr232_phase3_measure --envelopes-out")
    ap.add_argument("--set", default="golden", choices=("golden", "heldout", "both"))
    ap.add_argument("--qid", action="append", help="only this request (repeatable)")
    ap.add_argument("--limit", type=int, help="only the first N selected requests")
    ap.add_argument("--repeat", type=int, default=1, help="calls per request per arm the run used (a legacy envelopes file only)")
    ap.add_argument("--arm", default="on", help="which arm's retrieved items to draft from (default: on)")
    ap.add_argument("--live", action="store_true", help="fetch item text from the T3 store (read-only)")
    ap.add_argument("--texts", type=Path, help="a JSON object of chunk hash -> text, instead of --live")
    ap.add_argument("--out", type=Path, help="write the packet here (default: stdout)")
    args = ap.parse_args(argv)
    if args.arm not in ("off", "on"):
        click.echo(f"FAIL: --arm must be off or on, got {args.arm!r}", err=True)
        return 2
    if bool(args.live) == bool(args.texts):
        click.echo("FAIL: pass exactly one of --live (read item text from T3) or --texts FILE (chunk hash -> text)", err=True)
        return 2
    queries = _queries(args.set, args.qid or [], args.limit)
    if not queries:
        click.echo("FAIL: no requests selected", err=True)
        return 2
    try:
        records = read_envelopes_file(args.envelopes, [q.qid for q in queries for _ in range(max(1, args.repeat))])
    except ValueError as exc:
        click.echo(f"FAIL: {exc}", err=True)
        return 2
    if args.arm not in records:
        click.echo(f"FAIL: {args.envelopes} has no {args.arm!r} arm", err=True)
        return 2
    envelopes: dict[str, Mapping[str, Any]] = {}
    for r in records[args.arm]:
        if r.envelope is not None and not r.error and r.qid not in envelopes:
            envelopes[r.qid] = r.envelope
    text_of = texts_file_resolver(args.texts) if args.texts else live_text_resolver([ref for e in envelopes.values() for ref in anchor_refs(e)])
    body = json.dumps(build_packet(queries, envelopes, text_of), indent=2) + "\n"
    if args.out:
        args.out.write_text(body, encoding="utf-8")
        click.echo(f"wrote {args.out}", err=True)
    else:
        click.echo(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
