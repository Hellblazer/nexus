# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-229 P1.2 (nexus-p2m1x.2), deliverable A: the read-only census of ``catalog_collections`` rows against their names.

One pass over the registry rows ``GET /v1/catalog/collections/list`` returns (every lifecycle state, tombstones
included), answering the five questions of the bead for one tenant. It is a MEASUREMENT: it reads, computes and
reports, and no code path of it writes. The operator runs it once per production tenant (``nx catalog
collections-census``, :mod:`nexus.commands.catalog_cmds.collection_census`) and stores the JSON report; Sam is the only
cloud client, so no agent runs it against a managed tenant.

* **Row versus name (Critical Assumption 1).** For each LIVE row (``superseded_by`` empty) whose name is conformant
  (``<content_type>__<owner>__<model>__v<n>``, or ``quarantine-`` plus one), compare the row's ``content_type``,
  ``embedding_model`` and ``model_version`` with what the name parses to (a quarantine row against the name it
  quarantines: the engine populated its attributes from that name). ``owner_id`` differences are a separate bucket,
  expected (``catalog-044`` gives a legacy slug-named repo collection the documents' owner segment) and not listed as
  disagreements. ``lifecycle_state`` is checked on every live row: ``quarantine`` exactly when the name starts with
  ``quarantine-``. An empty row value is a mismatch. A live name that does not parse is counted and listed
  (``name_not_conformant``) and compared on nothing but lifecycle.
* **Label clash (Critical Assumption 6).** Live names held by more than one live row: what a partial UNIQUE on live
  ``(tenant_id, name)`` (P3.6) would refuse. The registry's primary key makes it zero today; the count is the proof.
* **Multi-candidate tuples.** Live ``(content_type, owner_id, embedding_model)`` tuples (the row's columns) with more
  than one candidate row, where a candidate is what ``CatalogRepository.collectionForTuple`` would consider: not
  superseded, ``lifecycle_state`` not ``quarantine``, and, counted both ways, ``legacy_grandfathered`` false (the filter
  today) and unfiltered (what D5 leaves). A tuple with an empty component is no tuple a caller can ask for and is
  counted apart.
* **Minted-form names.** Live names matching ``^c[0-9]+$``, the form P3.x mints (T2 ``229-research-38``).
* **Dangling tombstones.** Rows whose ``superseded_by`` is set and names no row of the tenant (T2 ``229-research-50``).

PURE: :func:`census` takes the rows and a label and a timestamp and returns one JSON-serialisable dict. The caller
makes the single read.
"""
from __future__ import annotations

import re
from typing import Any

from nexus.corpus import parse_conformant_collection_name

CENSUS_ID = "rdr229-p1.2"

#: ``quarantine-<origin name>``: the shape ``chunk_quarantine.quarantine_collection_name`` renders.
_QUARANTINE_RE = re.compile(r"^quarantine-(?P<origin>.+)$", re.DOTALL)
#: The minted form (T2 ``229-research-38``).
_MINTED_RE = re.compile(r"^c[0-9]+$")


def _is_live(r: dict) -> bool:
    return not (r.get("superseded_by") or "")


def _str(r: dict, key: str) -> str:
    v = r.get(key)
    return "" if v is None else str(v)


def _parse(name: str) -> tuple[dict[str, str] | None, bool]:
    """``(parsed segments, is_quarantine)``; the segments are ``None`` when the (origin) name is not conformant."""
    m = _QUARANTINE_RE.match(name)
    origin = m.group("origin") if m else name
    try:
        return parse_conformant_collection_name(origin), m is not None
    except ValueError:
        return None, m is not None


def _row_vs_name(live: list[dict]) -> dict[str, Any]:
    conformant = quarantine_conformant = 0
    cmp = {"content_type": [0, 0], "embedding_model": [0, 0], "model_version": [0, 0]}
    lifecycle = {"checked": 0, "mismatch": 0, "quarantine_state_without_prefix": 0, "prefix_without_quarantine_state": 0}
    owner_compared = 0
    owner_names: list[str] = []
    not_conformant: list[str] = []
    disagree: dict[str, dict[str, dict[str, str]]] = {}
    for r in live:
        name = r["name"]
        parsed, quarantine = _parse(name)
        diffs: dict[str, dict[str, str]] = {}
        row_state = _str(r, "lifecycle_state")
        lifecycle["checked"] += 1
        if (row_state == "quarantine") != quarantine:
            lifecycle["mismatch"] += 1
            if quarantine:
                lifecycle["prefix_without_quarantine_state"] += 1
            else:
                lifecycle["quarantine_state_without_prefix"] += 1
            diffs["lifecycle"] = {"row": row_state, "name": "quarantine" if quarantine else "live"}
        if parsed is None:
            not_conformant.append(name)
        else:
            if quarantine:
                quarantine_conformant += 1
            else:
                conformant += 1
            for field in cmp:
                cmp[field][0] += 1
                row_v, name_v = _str(r, field), parsed[field]
                if row_v != name_v:
                    cmp[field][1] += 1
                    diffs[field] = {"row": row_v, "name": name_v}
            owner_compared += 1
            if _str(r, "owner_id") != parsed["owner_id"]:
                owner_names.append(name)
        if diffs:
            disagree[name] = diffs
    return {
        "live_rows": len(live),
        "name_conformant": conformant,
        "name_quarantine_conformant": quarantine_conformant,
        "name_not_conformant": len(not_conformant),
        "not_conformant_names": sorted(not_conformant),
        **{f: {"compared": c, "mismatch": m} for f, (c, m) in cmp.items()},
        "lifecycle": lifecycle,
        "owner_differences": {"compared": owner_compared, "differ": len(owner_names), "expected": True,
                              "names": sorted(owner_names)},
        "rows_that_disagree": [{"name": n, "differences": d} for n, d in sorted(disagree.items())],
    }


def _label_clash(live: list[dict]) -> dict[str, Any]:
    held: dict[str, int] = {}
    for r in live:
        held[r["name"]] = held.get(r["name"], 0) + 1
    clashes = [{"name": n, "rows": k} for n, k in sorted(held.items()) if k > 1]
    return {"live_names": len(held), "clashing_names": len(clashes), "clashes": clashes}


def _tuples(live: list[dict]) -> dict[str, Any]:
    """Candidate rows per ``(content_type, owner_id, embedding_model)``, with and without the grandfathered filter.

    The other filters are ``collectionForTuple``'s (CatalogRepository.java, ``SUPERSEDED_BY = ''`` and
    ``LIFECYCLE_STATE <> 'quarantine'``); ``live`` already excludes the superseded rows."""
    incomplete = 0
    with_f: dict[tuple[str, str, str], list[str]] = {}
    without_f: dict[tuple[str, str, str], list[str]] = {}
    for r in live:
        key = (_str(r, "content_type"), _str(r, "owner_id"), _str(r, "embedding_model"))
        if not all(key):
            incomplete += 1
            continue
        if _str(r, "lifecycle_state") == "quarantine":
            continue
        without_f.setdefault(key, []).append(r["name"])
        if not r.get("legacy_grandfathered"):
            with_f.setdefault(key, []).append(r["name"])

    def listing(groups: dict[tuple[str, str, str], list[str]]) -> dict[str, Any]:
        many = [{"content_type": k[0], "owner_id": k[1], "embedding_model": k[2], "candidates": sorted(v)}
                for k, v in sorted(groups.items()) if len(v) > 1]
        return {"count": len(many), "tuples": many}

    only_without = sum(1 for k, v in without_f.items() if len(v) > 1 and len(with_f.get(k, ())) <= 1)
    return {
        "with_grandfathered_filter": listing(with_f),
        "without_grandfathered_filter": listing(without_f),
        "ambiguous_only_without_the_filter": only_without,
        "rows_without_a_complete_tuple": incomplete,
    }


def _dangling(rows: list[dict]) -> dict[str, Any]:
    names = {r["name"] for r in rows}
    tombs = [r for r in rows if not _is_live(r)]
    dangling = [{"name": r["name"], "superseded_by": r["superseded_by"]} for r in sorted(tombs, key=lambda x: x["name"])
                if r["superseded_by"] not in names]
    return {"tombstones": len(tombs), "dangling": len(dangling), "rows": dangling}


def census(rows: list[dict], *, tenant_label: str, generated_at: str) -> dict[str, Any]:
    """The five-part report for *rows* (every registry row of one tenant). Pure and deterministic."""
    live = [r for r in rows if _is_live(r)]
    minted = sorted(r["name"] for r in live if _MINTED_RE.match(r["name"]))
    return {
        "census": CENSUS_ID,
        "tenant_label": tenant_label,
        "generated_at": generated_at,
        "examined_nothing": not rows,
        "registry": {"rows": len(rows), "live": len(live), "tombstones": len(rows) - len(live)},
        "row_vs_name": _row_vs_name(live),
        "label_clash": _label_clash(live),
        "multi_candidate_tuples": _tuples(live),
        "minted_form_names": {"count": len(minted), "names": minted},
        "dangling_tombstones": _dangling(rows),
    }
