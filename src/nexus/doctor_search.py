# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Probes 3a + 3b — ``nx doctor --check-search``.

RDR-087 Phase 3. Two probes running back-to-back under one CLI flag:

**Probe 3a — name resolution** (Phase 3.2, nexus-yi4b.3.2). Walks the
``NAME_CANARIES`` fixture through three name-resolution surfaces
(``resolve_corpus``, ``rdr_resolve``, ``resolve_span``). Outcomes per
dispatch:

- ``matched`` — surface returned a positive result.
- ``empty``   — surface completed cleanly but found nothing.
- ``error``   — surface raised an unexpected exception. Regression.

**Probe 3b — retrieval quality** (Phase 3.3, nexus-yi4b.3.3). One
``search_cross_corpus`` call per registered collection with a canned
query; classifies each as:

- ``matched``        — raw>0 AND kept>0. Healthy.
- ``empty``          — raw==0. Empty or corrupt.
- ``threshold_drop`` — raw>0 AND kept==0. nexus-rc45 class (silent
  threshold-drop). Regression-level signal.
- ``model_drift``    — registered ``embedding_model`` metadata
  disagrees with :func:`corpus.voyage_model_for_collection`.
  Regression.
- ``error``          — unexpected exception during search or
  metadata lookup.

The CLI exits ``2`` when either probe produced any ``error`` /
``model_drift`` outcome, ``0`` otherwise. A ``threshold_drop`` WARNS
(Sam, 2026-09-28, nexus-dhvzx): its row names the nearest real-neighbour
distance, the threshold, and the sample the verdict rests on, so a reader
can judge it.
``--json`` emits a parseable payload covering both probes.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import click

_CHASH_SHAPE = re.compile(r"chash:[0-9a-f]{64}(:\d+-\d+)?")


# Retrieval-quality outcomes that signal a regression (exit 2). A
# threshold_drop warns instead: it rests on one sampled chunk and is often
# borderline (0.655 against 0.65 on the live tenant), so it is shown with
# its evidence and left to the reader (Sam, 2026-09-28, nexus-dhvzx).
_FAIL_OUTCOMES = {"error", "model_drift"}


@dataclass(frozen=True)
class ProbeResult:
    name: str
    surface: str
    outcome: str  # matched | empty | error | threshold_drop | model_drift
    error: str | None = None
    shape_note: str = ""
    # Probe 3b-specific context (None on probe 3a rows).
    raw_count: int | None = None
    kept_count: int | None = None
    expected_model: str | None = None
    actual_model: str | None = None
    # threshold_drop evidence (nexus-dhvzx): the nearest dropped (real)
    # neighbour's distance, the collection's threshold, and how many probe
    # chunks the verdict rests on (0 = the canned query was used).
    nearest_distance: float | None = None
    threshold: float | None = None
    probe_chunks: int | None = None


# ── Surface runners (probe 3a, production-wired) ────────────────────────────


def _default_rdr_dir() -> Path:
    """Locate ``docs/rdr/`` relative to the nearest repo root."""
    cwd = Path.cwd()
    for base in (cwd, *cwd.parents):
        candidate = base / "docs" / "rdr"
        if candidate.is_dir():
            return candidate
    return cwd / "docs" / "rdr"


def _corpus_runner(name: str, all_collections: list[str]) -> list[str]:
    from nexus.corpus import resolve_corpus  # noqa: PLC0415 — deferred import; diagnostic dep only needed in this probe

    return resolve_corpus(name, all_collections)


def _rdr_runner(name: str) -> str:
    from nexus.doc.resolvers import RdrResolver  # noqa: PLC0415 — deferred import; diagnostic dep only needed in this probe

    return RdrResolver(_default_rdr_dir()).resolve(name, field=None, filters={})


def _span_runner(name: str) -> bool:
    return bool(_CHASH_SHAPE.fullmatch(name))


def _load_canaries():
    from nexus.name_canaries import NAME_CANARIES  # noqa: PLC0415 — deferred import; diagnostic dep only needed in this probe

    return NAME_CANARIES


# ── Collection + metadata enumerators (probe 3b, production-wired) ──────────


def _make_t3():
    from nexus.db import make_t3  # noqa: PLC0415 — deferred import; diagnostic dep only needed in this probe

    return make_t3()


def _list_collections() -> list[str]:
    """Return every registered T3 collection name."""
    t3 = _make_t3()
    return [c["name"] for c in t3.list_collections()]


def _collection_metadata(t3, col: str) -> dict[str, Any]:
    """Return the ChromaDB metadata dict for *col* (or empty dict)."""
    return t3.collection_metadata(col)


# ── Probe 3a: name resolution ───────────────────────────────────────────────


def run_name_resolution_probe(
    canaries,
    *,
    resolve_corpus_fn: Callable[[str, list[str]], list[str]] = _corpus_runner,
    rdr_resolve_fn: Callable[[str], str] = _rdr_runner,
    resolve_span_fn: Callable[[str], bool] = _span_runner,
    all_collections: list[str] | None = None,
) -> list[ProbeResult]:
    """Dispatch each canary to every surface in its ``expected_surface`` set.

    Injected runners make the probe testable without live catalog / T3 /
    RDR filesystem state. Defaults wire to the real production surfaces.
    """
    from nexus.doc.resolvers import ResolutionError  # noqa: PLC0415 — deferred import; diagnostic dep only needed in this probe

    cols = all_collections if all_collections is not None else []
    out: list[ProbeResult] = []
    for canary in canaries:
        for surface in sorted(canary.expected_surface):
            outcome: str
            error: str | None = None
            try:
                if surface == "resolve_corpus":
                    matched = bool(resolve_corpus_fn(canary.name, cols))
                    outcome = "matched" if matched else "empty"
                elif surface == "rdr_resolve":
                    try:
                        rdr_resolve_fn(canary.name)
                        outcome = "matched"
                    except ResolutionError:
                        outcome = "empty"
                elif surface == "resolve_span":
                    outcome = "matched" if resolve_span_fn(canary.name) else "empty"
                else:
                    outcome = "error"
                    error = f"unknown surface literal: {surface!r}"
            except Exception as exc:  # noqa: BLE001 — probe captures any failure as ProbeResult outcome=error (diagnostic)
                outcome = "error"
                error = f"{type(exc).__name__}: {exc}"
            out.append(
                ProbeResult(
                    name=canary.name,
                    surface=surface,
                    outcome=outcome,
                    error=error,
                    shape_note=canary.shape_note,
                )
            )
    return out


# ── Probe 3b: retrieval quality ─────────────────────────────────────────────


def _default_search_fn(*args, **kwargs):
    from nexus.search_engine import search_cross_corpus  # noqa: PLC0415 — deferred import; diagnostic dep only needed in this probe

    return search_cross_corpus(*args, **kwargs)


def _default_model_for(col: str) -> str:
    from nexus.corpus import voyage_model_for_collection  # noqa: PLC0415 — deferred import; diagnostic dep only needed in this probe

    return voyage_model_for_collection(col)


#: Probe text drawn from a chunk is cut to this many characters. The whole
#: chunk, not a prefix: a 400-char prefix of a code chunk is mostly the
#: shared SPDX header, which finds every file's header (review, 29fab0df4).
_PROBE_QUERY_CHARS: int = 2000

#: A neighbour closer than this is duplicate text, not a real neighbour: it
#: says nothing about the collection's distance floor.
_DUPLICATE_DISTANCE: float = 0.02


#: Chunks sampled per collection; the verdict is the MEDIAN of their nearest
#: real-neighbour distances. One chunk was too noisy: 2 of 3 live drops sat
#: within 0.005 of the threshold (critique, 29fab0df4).
_PROBE_SAMPLES: int = 3


def _default_probe_for(t3, col: str) -> list[tuple[str, str]]:
    """``[(chunk_text, chunk_id), ...]``: up to ``_PROBE_SAMPLES`` of *col*'s
    own chunks, the queries for :func:`_neighbour_probe`.

    nexus-dhvzx. One canned query ("example test probe") for every
    collection was irrelevant to most of them, so the threshold correctly
    dropped it, 58 of 111 collections read ``threshold_drop``, and the check
    exited 2 on every real tenant. What RDR-087 built this probe to catch is
    a healthy collection whose natural distance floor sits above its
    threshold (docs__art-grossberg-papers, MVV item 2): related text exists
    but every real neighbour lands past the cut. A chunk's own text finds
    the chunk itself, which proves nothing, so the neighbour probe excludes
    it BY ID; a document-title query was tried and rejected on measurement
    (chunk titles are often synthetic, "README.md:chunk-20").
    """
    got = t3.get_or_create_collection(col).get(include=["documents"], limit=_PROBE_SAMPLES)
    out: list[tuple[str, str]] = []
    for cid, doc in zip(got.get("ids") or [], got.get("documents") or []):
        text = " ".join((doc or "").split())[:_PROBE_QUERY_CHARS]
        if text and cid:
            out.append((text, cid))
    return out


def _default_threshold_for(col: str) -> float | None:
    from nexus.config import load_config  # noqa: PLC0415 — deferred, like this module's other imports
    from nexus.search_engine import _threshold_for_collection  # noqa: PLC0415 — deferred import

    return _threshold_for_collection(col, load_config())


def _neighbour_probe(
    col: str,
    samples: list[tuple[str, str]],
    *,
    t3,
    search_fn: Callable[..., Any],
    n_results: int,
    threshold_for: Callable[[str], float | None] | None,
    expected: str,
    actual: str,
) -> "ProbeResult":
    """Judge *col* by its sample chunks' nearest REAL neighbours.

    Each sample is searched with the threshold off, its own row is removed
    by id and near-duplicate text (distance under ``_DUPLICATE_DISTANCE``)
    is ignored (never assumed: a re-embedded snippet of a context-embedded chunk
    need not land near its stored vector, and counting on the self-hit
    either hid the incident or invented one; critique, 29fab0df4), and the
    nearest remaining distance is taken. The verdict compares the median of
    those against the collection's threshold. No sample with a real
    neighbour reads ``empty``.
    """
    import statistics  # noqa: PLC0415 — probe path only

    threshold = (threshold_for or _default_threshold_for)(col)
    nearest: list[float] = []
    seen = kept = 0
    try:
        for text, self_id in samples:
            rows = search_fn(text, [col], n_results + 1, t3,
                             diagnostics_out=[], threshold_override=float("inf"))
            # search_cross_corpus over-fetches (up to 4x) and returns the
            # whole pool; judge only the n_results nearest real neighbours so
            # the evidence line's count is the window actually used. The
            # nearest distance is the same either way (critique, 634cc2b66).
            real = sorted(r.distance for r in rows or []
                          if r.id != self_id and r.distance >= _DUPLICATE_DISTANCE)[:n_results]
            seen += len(real)
            if threshold is not None:
                kept += sum(1 for d in real if d <= threshold)
            if real:
                nearest.append(min(real))
    except Exception as exc:  # noqa: BLE001 — probe captures any failure as ProbeResult outcome=error (diagnostic)
        return ProbeResult(name=col, surface="retrieval_quality", outcome="error",
                           error=f"{type(exc).__name__}: {exc}",
                           expected_model=expected, actual_model=actual)
    median = statistics.median(nearest) if nearest else None
    if median is None:
        outcome = "empty"
    elif threshold is None or median <= threshold:
        outcome = "matched"
    else:
        outcome = "threshold_drop"
    return ProbeResult(
        name=col, surface="retrieval_quality", outcome=outcome,
        raw_count=seen, kept_count=kept if threshold is not None else seen,
        expected_model=expected, actual_model=actual,
        nearest_distance=median, threshold=threshold, probe_chunks=len(samples),
    )


def run_retrieval_quality_probe(
    *,
    t3,
    collections: list[str],
    search_fn: Callable[..., Any] = _default_search_fn,
    model_for: Callable[[str], str] = _default_model_for,
    metadata_fn: Callable[[str], dict[str, Any]] | None = None,
    query: str = "example test probe",
    probe_for: Callable[[str], list[tuple[str, str]]] | None = None,
    threshold_for: Callable[[str], float | None] | None = None,
    n_results: int = 5,
) -> list[ProbeResult]:
    """Query each registered collection and classify retrieval health.

    Model-drift detection runs *before* the search so a drifted collection
    is flagged even if the query happened to return data (wrong embedding
    model → systematically wrong distances, even when non-empty).

    Args:
        t3: T3Database client. Passed through to ``search_fn``.
        collections: collection names to probe (caller enumerates).
        search_fn: injectable ``search_cross_corpus`` stand-in for tests.
        model_for: maps collection name → expected embedding_model.
        metadata_fn: ``col_name -> metadata dict``. Defaults to
            ``t3.collection_metadata`` when *None*.
        query: canned probe query, used for a collection when
            ``probe_for`` is unset or yields nothing (no readable chunk).
        probe_for: ``col_name -> [(chunk_text, chunk_id), ...]``;
            ``run_check_search`` passes :func:`_default_probe_for`. When it
            yields samples, :func:`_neighbour_probe` decides the verdict.
        threshold_for: ``col_name -> distance threshold`` for the neighbour
            probe; defaults to the search engine's per-collection threshold.
        n_results: small probe depth.
    """
    from nexus.search_engine import SearchDiagnostics  # noqa: F401,PLC0415 — deferred import; presence-probe only needed in this diagnostic

    if metadata_fn is None:
        def metadata_fn(col: str) -> dict[str, Any]:
            return _collection_metadata(t3, col)

    out: list[ProbeResult] = []
    for col in collections:
        expected = ""
        actual = ""
        try:
            expected = model_for(col)
            meta = metadata_fn(col) or {}
            actual = str(meta.get("embedding_model") or "")
        except Exception as exc:  # noqa: BLE001 — probe captures any failure as ProbeResult outcome=error (diagnostic)
            out.append(
                ProbeResult(
                    name=col,
                    surface="retrieval_quality",
                    outcome="error",
                    error=f"{type(exc).__name__}: {exc}",
                    expected_model=expected or None,
                    actual_model=actual or None,
                )
            )
            continue

        if actual and actual != expected:
            out.append(
                ProbeResult(
                    name=col,
                    surface="retrieval_quality",
                    outcome="model_drift",
                    expected_model=expected,
                    actual_model=actual,
                )
            )
            continue

        samples = probe_for(col) if probe_for is not None else None
        if samples:
            out.append(_neighbour_probe(
                col, samples, t3=t3, search_fn=search_fn, n_results=n_results,
                threshold_for=threshold_for, expected=expected, actual=actual,
            ))
            continue

        try:
            diag_list: list[Any] = []
            search_fn(
                query, [col], n_results, t3,
                diagnostics_out=diag_list,
            )
        except Exception as exc:  # noqa: BLE001 — probe captures any failure as ProbeResult outcome=error (diagnostic)
            out.append(
                ProbeResult(
                    name=col,
                    surface="retrieval_quality",
                    outcome="error",
                    error=f"{type(exc).__name__}: {exc}",
                    expected_model=expected,
                    actual_model=actual,
                )
            )
            continue

        if not diag_list:
            out.append(
                ProbeResult(
                    name=col,
                    surface="retrieval_quality",
                    outcome="error",
                    error="search_fn did not populate diagnostics_out",
                    expected_model=expected,
                    actual_model=actual,
                )
            )
            continue

        diag = diag_list[0]
        per_col = diag.per_collection.get(col, (0, 0, None, None))
        raw, dropped = per_col[0], per_col[1]
        kept = raw - dropped
        if raw == 0:
            outcome = "empty"
        elif kept == 0:
            outcome = "threshold_drop"
        else:
            outcome = "matched"
        out.append(
            ProbeResult(
                name=col,
                surface="retrieval_quality",
                outcome=outcome,
                raw_count=raw,
                kept_count=kept,
                expected_model=expected,
                actual_model=actual,
                nearest_distance=per_col[3] if len(per_col) > 3 else None,
                threshold=per_col[2] if len(per_col) > 2 else None,
                probe_chunks=0,
            )
        )
    return out


# ── Output formatters ───────────────────────────────────────────────────────


_GLYPH = {
    "matched": "[\u2713]",
    "empty": "[-]",
    "error": "[\u2717]",
    "threshold_drop": "[!]",
    "model_drift": "[\u2717]",
}


def _format_probe_section(title: str, results: list[ProbeResult]) -> list[str]:
    lines = [f"{title}:"]
    for r in results:
        glyph = _GLYPH.get(r.outcome, "[?]")
        detail_parts: list[str] = []
        if r.raw_count is not None:
            detail_parts.append(f"raw={r.raw_count} kept={r.kept_count}")
        if r.outcome == "threshold_drop":
            near = f"{r.nearest_distance:.3f}" if r.nearest_distance is not None else "?"
            thr = f"{r.threshold:.3f}" if r.threshold is not None else "?"
            basis = (f"{r.probe_chunks} probe chunk(s) and {r.raw_count} neighbour(s)"
                     if r.probe_chunks else "the canned query (no readable chunk)")
            label = "median nearest real neighbour" if r.probe_chunks else "nearest dropped candidate"
            detail_parts.append(
                f"{label} d={near} > threshold {thr}; verdict rests on {basis}"
            )
        if r.outcome == "model_drift":
            detail_parts.append(
                f"expected={r.expected_model} actual={r.actual_model}"
            )
        if r.error:
            detail_parts.append(r.error)
        detail = ("  " + " ".join(detail_parts)) if detail_parts else ""
        lines.append(
            f"  {glyph} {r.outcome:<15} {r.name} ({r.surface}){detail}"
        )
    return lines


def _summary_counts(results: list[ProbeResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1
    return counts


def format_combined_human(
    name_results: list[ProbeResult],
    retrieval_results: list[ProbeResult],
) -> str:
    lines: list[str] = []
    lines.extend(_format_probe_section("name_resolution probe", name_results))
    nr = _summary_counts(name_results)
    lines.append(
        f"Summary: {nr.get('matched', 0)} matched, "
        f"{nr.get('empty', 0)} empty, {nr.get('error', 0)} error."
    )
    lines.append("")
    lines.extend(
        _format_probe_section("retrieval_quality probe", retrieval_results)
    )
    rq = _summary_counts(retrieval_results)
    lines.append(
        f"Summary: {rq.get('matched', 0)} matched, "
        f"{rq.get('empty', 0)} empty, "
        f"{rq.get('threshold_drop', 0)} threshold_drop (warning), "
        f"{rq.get('model_drift', 0)} model_drift, "
        f"{rq.get('error', 0)} error."
    )
    return "\n".join(lines)


def format_combined_json(
    name_results: list[ProbeResult],
    retrieval_results: list[ProbeResult],
) -> str:
    payload = {
        "probes": [
            {
                "probe": "name_resolution",
                "results": [asdict(r) for r in name_results],
                "summary": _summary_counts(name_results),
            },
            {
                "probe": "retrieval_quality",
                "results": [asdict(r) for r in retrieval_results],
                "summary": _summary_counts(retrieval_results),
            },
        ],
    }
    return json.dumps(payload, indent=2)


# ── CLI entry point ─────────────────────────────────────────────────────────


def run_check_search(*, json_out: bool) -> None:
    """Execute both probes and exit 2 when any regression signal is seen."""
    # Probe 3a.
    canaries = _load_canaries()
    name_results = run_name_resolution_probe(
        canaries,
        resolve_corpus_fn=_corpus_runner,
        rdr_resolve_fn=_rdr_runner,
        resolve_span_fn=_span_runner,
    )

    # Probe 3b — enumerate collections via a live T3 client.  Any failure
    # in the enumeration itself is reported as a single error row so the
    # probe stays informative even when T3 is unreachable.
    retrieval_results: list[ProbeResult] = []
    try:
        collections = _list_collections()
    except Exception as exc:  # noqa: BLE001 — probe captures any failure as ProbeResult outcome=error (diagnostic)
        retrieval_results.append(
            ProbeResult(
                name="<enumerate>",
                surface="retrieval_quality",
                outcome="error",
                error=f"{type(exc).__name__}: {exc}",
            )
        )
    else:
        if collections:
            try:
                t3 = _make_t3()
            except Exception as exc:  # noqa: BLE001 — probe captures any failure as ProbeResult outcome=error (diagnostic)
                retrieval_results.append(
                    ProbeResult(
                        name="<t3_client>",
                        surface="retrieval_quality",
                        outcome="error",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
            else:
                retrieval_results.extend(
                    run_retrieval_quality_probe(
                        t3=t3,
                        collections=collections,
                        probe_for=lambda col: _default_probe_for(t3, col),
                    )
                )

    if json_out:
        click.echo(format_combined_json(name_results, retrieval_results))
    else:
        click.echo(format_combined_human(name_results, retrieval_results))

    combined = name_results + retrieval_results
    if any(r.outcome in _FAIL_OUTCOMES for r in combined):
        raise SystemExit(2)
