# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 3 measurement (nexus-jypmj.3.10): runtime facet ids to golden ``required_facets`` slugs.

``nx_answer`` derives its facets from the request (``f1``..``fN``); the golden sets label each request with
stable slugs. They are different lists, and without a join no per-facet number can be scored against the golden
labels. The join is ``bench/queries/golden_facet_map_v1.yaml``, a hand label of facet TEXT (a case-insensitive
substring of the runtime facet's text, so it survives a change in numbering or order), plus one rule that needs no
entry: a request with exactly one runtime facet carries every required slug of that request.

A runtime facet that no entry or rule maps is reported (``unmapped_fids``), and so is a slug no facet carries
(``unmapped_slugs``): the resolution never guesses, and a caller counts both before it quotes a rate.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_BENCH_PARENT = _Path(__file__).resolve().parent.parent
if str(_BENCH_PARENT) not in _sys.path:
    _sys.path.insert(0, str(_BENCH_PARENT))

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from bench.schema import GOLDEN_SET_VERSION, Query

FACET_MAP_VERSION = 1
DEFAULT_MAP_PATH = Path(__file__).resolve().parents[2] / "bench" / "queries" / "golden_facet_map_v1.yaml"

_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub(" ", str(text)).strip().lower()


@dataclass(frozen=True)
class MapEntry:
    """``match`` (normalised substring of a runtime facet's text) carries ``slugs``."""

    match: str
    slugs: tuple[str, ...]


@dataclass(frozen=True)
class FacetResolution:
    """How one request's runtime facets map to its golden slugs.

    ``via`` says which rule mapped each runtime facet: ``entry`` (the map file) or ``sole_facet`` (the request
    derived exactly one facet).
    """

    qid: str
    fid_to_slugs: Mapping[str, tuple[str, ...]]
    slug_to_fids: Mapping[str, tuple[str, ...]]
    unmapped_fids: tuple[str, ...]
    unmapped_slugs: tuple[str, ...]
    via: Mapping[str, str] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        """Every runtime facet maps to a slug and every required slug is carried by a facet."""
        return not self.unmapped_fids and not self.unmapped_slugs


def load_facet_map(path: Path, queries: Sequence[Query]) -> dict[str, tuple[MapEntry, ...]]:
    """Read and validate the map: version stamps, known qids, slugs that the request actually requires."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if data.get("facet_map_version") != FACET_MAP_VERSION:
        raise ValueError(f"{path}: facet_map_version must be {FACET_MAP_VERSION}, got {data.get('facet_map_version')!r}")
    if data.get("golden_set_version") != GOLDEN_SET_VERSION:
        raise ValueError(f"{path}: golden_set_version must be {GOLDEN_SET_VERSION}, got {data.get('golden_set_version')!r}")
    by_qid = {q.qid: q for q in queries}
    out: dict[str, tuple[MapEntry, ...]] = {}
    for qid, rows in (data.get("requests") or {}).items():
        if qid not in by_qid:
            raise ValueError(f"{path}: entry for unknown request {qid!r}")
        entries: list[MapEntry] = []
        for i, row in enumerate(rows or []):
            match, slugs = _norm(row.get("match", "")), tuple(row.get("slugs") or ())
            if not match or not slugs:
                raise ValueError(f"{path}: {qid}[{i}] needs a non-empty match and slugs")
            unknown = sorted(set(slugs) - set(by_qid[qid].required_facets))
            if unknown:
                raise ValueError(f"{path}: {qid}[{i}] names slugs {unknown} the request does not require")
            entries.append(MapEntry(match, slugs))
        out[qid] = tuple(entries)
    return out


def resolve_facets(query: Query, facets: Sequence[Mapping[str, Any]], entries: Sequence[MapEntry] = ()) -> FacetResolution:
    """Map *facets* (``request_coverage.facets`` dicts: ``id`` and, on a traced run, ``text``) to *query*'s slugs."""
    fid_to_slugs: dict[str, tuple[str, ...]] = {}
    via: dict[str, str] = {}
    unmapped: list[str] = []
    for f in facets:
        fid, text = str(f.get("id", "")), _norm(f.get("text") or "")
        slugs: list[str] = []
        for e in entries:
            if text and e.match in text:
                slugs.extend(s for s in e.slugs if s not in slugs)
        if slugs:
            fid_to_slugs[fid], via[fid] = tuple(slugs), "entry"
        elif len(facets) == 1:
            fid_to_slugs[fid], via[fid] = tuple(query.required_facets), "sole_facet"
        else:
            unmapped.append(fid)
    slug_to_fids: dict[str, list[str]] = {s: [] for s in query.required_facets}
    for fid, slugs in fid_to_slugs.items():
        for s in slugs:
            slug_to_fids[s].append(fid)
    return FacetResolution(
        qid=query.qid, fid_to_slugs=fid_to_slugs,
        slug_to_fids={s: tuple(v) for s, v in slug_to_fids.items()},
        unmapped_fids=tuple(unmapped),
        unmapped_slugs=tuple(s for s, v in slug_to_fids.items() if not v),
        via=via,
    )
