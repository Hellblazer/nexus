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
``threshold_drop`` / ``model_drift`` outcome, ``0`` otherwise.
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


# Retrieval-quality outcomes that signal a regression (exit 2).
_FAIL_OUTCOMES = {"error", "threshold_drop", "model_drift"}


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
    from nexus.corpus import resolve_corpus

    return resolve_corpus(name, all_collections)


def _rdr_runner(name: str) -> str:
    from nexus.doc.resolvers import RdrResolver

    return RdrResolver(_default_rdr_dir()).resolve(name, field=None, filters={})


def _span_runner(name: str) -> bool:
    return bool(_CHASH_SHAPE.fullmatch(name))


def _load_canaries():
    from nexus.name_canaries import NAME_CANARIES

    return NAME_CANARIES


# ── Collection + metadata enumerators (probe 3b, production-wired) ──────────


def _make_t3():
    from nexus.db import make_t3

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
    from nexus.doc.resolvers import ResolutionError

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
            except Exception as exc:
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
    from nexus.search_engine import search_cross_corpus

    return search_cross_corpus(*args, **kwargs)


def _default_model_for(col: str) -> str:
    from nexus.corpus import voyage_model_for_collection

    return voyage_model_for_collection(col)


def run_retrieval_quality_probe(
    *,
    t3,
    collections: list[str],
    search_fn: Callable[..., Any] = _default_search_fn,
    model_for: Callable[[str], str] = _default_model_for,
    metadata_fn: Callable[[str], dict[str, Any]] | None = None,
    query: str = "example test probe",
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
        query: canned probe query. Short so we stay inside the budget
            (≤400 ms per A-2 measurement).
        n_results: small probe depth.
    """
    from nexus.search_engine import SearchDiagnostics  # noqa: F401

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
        except Exception as exc:
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

        try:
            diag_list: list[Any] = []
            search_fn(
                query, [col], n_results, t3,
                diagnostics_out=diag_list,
            )
        except Exception as exc:
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
            )
        )
    return out


# ── Output formatters ───────────────────────────────────────────────────────


_GLYPH = {
    "matched": "[\u2713]",
    "empty": "[-]",
    "error": "[\u2717]",
    "threshold_drop": "[\u2717]",
    "model_drift": "[\u2717]",
}


def _format_probe_section(title: str, results: list[ProbeResult]) -> list[str]:
    lines = [f"{title}:"]
    for r in results:
        glyph = _GLYPH.get(r.outcome, "[?]")
        detail_parts: list[str] = []
        if r.raw_count is not None:
            detail_parts.append(f"raw={r.raw_count} kept={r.kept_count}")
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
        f"{rq.get('threshold_drop', 0)} threshold_drop, "
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
    except Exception as exc:
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
            except Exception as exc:
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
                    )
                )

    if json_out:
        click.echo(format_combined_json(name_results, retrieval_results))
    else:
        click.echo(format_combined_human(name_results, retrieval_results))

    combined = name_results + retrieval_results
    if any(r.outcome in _FAIL_OUTCOMES for r in combined):
        raise SystemExit(2)
