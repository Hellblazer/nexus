# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Probe 3a — name-resolution canary for ``nx doctor --check-search``.

RDR-087 Phase 3.2 (nexus-yi4b.3.2). Walks the ``NAME_CANARIES``
fixture through three name-resolution surfaces (``resolve_corpus``,
``rdr_resolve``, ``resolve_span``) and reports one of three outcomes
per dispatch:

- ``matched`` — surface returned a positive result.
- ``empty``   — surface completed cleanly but found nothing
  (``resolve_corpus`` returns empty list, ``rdr_resolve`` raises
  ``ResolutionError``). Informational, NOT a regression.
- ``error``   — surface raised an unexpected exception. Regression.
  This is what nexus-51j (case-sensitive glob pre-PR #173),
  nexus-7ay, and nexus-rc45 looked like in production.

The CLI exits ``2`` when any dispatch produced ``error``, ``0``
otherwise. ``--json`` emits a parseable payload for automation.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import click

_CHASH_SHAPE = re.compile(r"chash:[0-9a-f]{64}(:\d+-\d+)?")


@dataclass(frozen=True)
class ProbeResult:
    name: str
    surface: str
    outcome: str  # "matched" | "empty" | "error"
    error: str | None = None
    shape_note: str = ""


# ── Surface runners (real, production-wired) ────────────────────────────────


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


# ── Core probe ──────────────────────────────────────────────────────────────


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


# ── Output formatters ───────────────────────────────────────────────────────


_GLYPH = {"matched": "[\u2713]", "empty": "[-]", "error": "[\u2717]"}


def format_probe_human(results: list[ProbeResult]) -> str:
    lines = ["name_resolution probe:"]
    for r in results:
        glyph = _GLYPH.get(r.outcome, "[?]")
        suffix = f"  {r.error}" if r.error else ""
        lines.append(
            f"  {glyph} {r.outcome:<7} {r.name} ({r.surface}){suffix}"
        )
    m = sum(1 for r in results if r.outcome == "matched")
    e = sum(1 for r in results if r.outcome == "empty")
    x = sum(1 for r in results if r.outcome == "error")
    lines.append(f"Summary: {m} matched, {e} empty, {x} error.")
    return "\n".join(lines)


def format_probe_json(results: list[ProbeResult]) -> str:
    payload = {
        "probe": "name_resolution",
        "results": [asdict(r) for r in results],
        "summary": {
            "matched": sum(1 for r in results if r.outcome == "matched"),
            "empty": sum(1 for r in results if r.outcome == "empty"),
            "error": sum(1 for r in results if r.outcome == "error"),
        },
    }
    return json.dumps(payload, indent=2)


# ── CLI entry point ─────────────────────────────────────────────────────────


def run_check_search(*, json_out: bool) -> None:
    """Execute probe 3a and exit with status 2 when any regression is seen."""
    canaries = _load_canaries()
    # Look up runners at call time (not via defaults) so tests can
    # monkeypatch the module-level names without patching every caller.
    results = run_name_resolution_probe(
        canaries,
        resolve_corpus_fn=_corpus_runner,
        rdr_resolve_fn=_rdr_runner,
        resolve_span_fn=_span_runner,
    )
    if json_out:
        click.echo(format_probe_json(results))
    else:
        click.echo(format_probe_human(results))
    if any(r.outcome == "error" for r in results):
        raise SystemExit(2)
