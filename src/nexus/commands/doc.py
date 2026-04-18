# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-082: ``nx doc`` — doc-build token rendering + validation.

Two subcommands share one engine:

* ``nx doc render <path>...`` — parse, resolve, emit
  ``<stem>.rendered.md`` sibling.
* ``nx doc validate <path>...`` — same parse + resolve, no emit; exits
  non-zero on any unresolved token.

Exit contract::

    0  — all tokens resolved (or ``--allow-unresolved`` and nothing
         structural went wrong)
    1  — one or more tokens unresolved (validate mode or strict render)
    2  — argument / IO error (no file, bad flag, etc.)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from nexus.commands._helpers import default_db_path
from nexus.config import load_config
from nexus.db.t2 import T2Database
from nexus.doc.citations import (
    extensions_report,
    grounding_report,
    scan_citations,
)
from nexus.doc.render import RenderError, render_file
from nexus.doc.resolvers import BeadResolver, RdrResolver, ResolverRegistry
from nexus.doc.resolvers_corpus import AnchorResolver


@click.group("doc")
def doc() -> None:
    """Doc-build token rendering (RDR-082)."""


def _default_registry(
    project_root: Path, *, db: T2Database | None = None,
) -> ResolverRegistry:
    """Build the default Resolver registry: bead + RDR system-of-record
    resolvers (RDR-082) plus corpus-evidence resolvers (RDR-083) when
    a T2Database is supplied.

    Callers opening a live T2Database should pass it in so resolvers
    that need taxonomy data see a non-closed connection; without *db*,
    only the bead + RDR resolvers register.
    """
    cfg: dict = {}
    try:
        cfg = load_config(project_root)
    except Exception:
        cfg = {}
    rdr_paths = (cfg.get("indexing") or {}).get("rdr_paths") or ["docs/rdr"]
    rdr_dir = project_root / rdr_paths[0]

    registry = ResolverRegistry({
        "bd": BeadResolver(),
        "rdr": RdrResolver(rdr_dir=rdr_dir),
    })
    if db is not None:
        registry.register("nx-anchor", AnchorResolver(taxonomy=db.taxonomy))
    return registry


@doc.command("render")
@click.argument("paths", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--out-dir", type=click.Path(file_okay=False, path_type=Path),
    help="Write rendered files into this directory (mirrors source names). "
         "Default: sibling '<stem>.rendered.md' next to each source file.",
)
@click.option(
    "--allow-unresolved", is_flag=True,
    help="Preserve unresolved tokens as literal text instead of failing.",
)
@click.option(
    "--expand-citations", is_flag=True,
    help="RDR-086 Phase 4.3: append a footnote block resolving every "
         "chash: citation to its chunk text.",
)
@click.option(
    "--project-root", type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Project root for resolver context (bead DB, rdr_paths). "
         "Default: current working directory.",
)
def render_cmd(
    paths: tuple[Path, ...],
    out_dir: Path | None,
    allow_unresolved: bool,
    expand_citations: bool,
    project_root: Path | None,
) -> None:
    """Render markdown tokens into resolved sidecar files."""
    root = project_root or Path.cwd()

    # Open T2 inside a context manager; resolvers live for the duration
    # of the command. Best-effort — if T2 can't be opened (fresh
    # install, no db), fall back to bead + RDR resolvers only.
    db: T2Database | None = None
    try:
        db = T2Database(default_db_path())
    except Exception:
        db = None

    # RDR-086 Phase 4.3: chash resolver for footnote expansion. Only
    # opened when --expand-citations is set so the default render path
    # stays cheap.
    phase4_trio = None
    if expand_citations:
        try:
            phase4_trio = _phase4_catalog_t3_chash()
        except Exception as exc:
            click.echo(f"--expand-citations cannot open resolver: {exc}", err=True)
            raise click.exceptions.Exit(2)

    total_resolved = 0
    total_misses = 0
    try:
        registry = _default_registry(root, db=db)
        try:
            for path in paths:
                result = render_file(
                    path,
                    registry,
                    out_dir=out_dir,
                    allow_unresolved=allow_unresolved,
                    emit=True,
                    source_root=root,
                )
                total_resolved += result.resolved
                for tok, reason in result.unresolved:
                    click.echo(
                        f"{path}:{tok.lineno}:{tok.col}: unresolved {tok.raw} — {reason}",
                        err=True,
                    )
                    total_misses += 1

                if expand_citations and phase4_trio is not None:
                    _append_chash_footnotes(
                        path, out_dir, phase4_trio,
                    )
        except RenderError as exc:
            click.echo(f"render error: {exc}", err=True)
            raise click.exceptions.Exit(1)
        except OSError as exc:
            click.echo(f"io error: {exc}", err=True)
            raise click.exceptions.Exit(2)
    finally:
        if db is not None:
            db.close()
        if phase4_trio is not None:
            try:
                phase4_trio[2].close()
            except Exception:
                pass

    click.echo(f"rendered {total_resolved} tokens across {len(paths)} file(s)")
    if total_misses:
        click.echo(f"note: {total_misses} unresolved (preserved verbatim)", err=True)


def _append_chash_footnotes(
    src_path: Path,
    out_dir: Path | None,
    phase4_trio: tuple,
) -> None:
    """RDR-086 Phase 4.3: append a footnotes block to the rendered sibling.

    For every unique ``chash:`` span in the source doc, resolve via
    ``Catalog.resolve_chash`` and emit one footnote per chash carrying
    the chunk text (truncated at 500 chars). Unresolvable chash values
    render as ``[unresolved chash: <first 8 chars>…]`` rather than
    crashing — the visible marker is the RDR acceptance criterion.
    """
    cat, t3, chash_index = phase4_trio

    # Locate the rendered sibling the same way ``render_file`` does.
    if out_dir is not None:
        rendered = out_dir / f"{src_path.stem}.rendered.md"
    else:
        rendered = src_path.with_name(f"{src_path.stem}.rendered.md")
    if not rendered.exists():
        return

    source_text = src_path.read_text(errors="replace")
    cites = scan_citations(source_text)
    seen: set[str] = set()
    footnotes: list[str] = []
    for c in cites:
        if c.kind != "chash" or not c.chash or c.chash in seen:
            continue
        seen.add(c.chash)
        try:
            ref = cat.resolve_chash(c.chash, t3, chash_index)
        except Exception:
            ref = None
        short = c.chash[:8]
        if ref is None:
            footnotes.append(f"- `chash:{short}…` — [unresolved chash: {short}…]")
            continue
        text = str(ref.get("chunk_text", "")).strip()
        if len(text) > 500:
            text = text[:500] + "…"
        footnotes.append(f"- `chash:{short}…` — {text}")

    if not footnotes:
        return

    block = "\n\n## Citations\n\n" + "\n".join(footnotes) + "\n"
    with rendered.open("a", encoding="utf-8") as fh:
        fh.write(block)


@doc.command("validate")
@click.argument("paths", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--project-root", type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Project root for resolver context. Default: cwd.",
)
def validate_cmd(paths: tuple[Path, ...], project_root: Path | None) -> None:
    """Parse + resolve without emitting. Exits non-zero on any miss."""
    root = project_root or Path.cwd()

    db: T2Database | None = None
    try:
        db = T2Database(default_db_path())
    except Exception:
        db = None

    total_misses = 0
    total_ok = 0
    try:
        registry = _default_registry(root, db=db)
        for path in paths:
            try:
                result = render_file(
                    path, registry, allow_unresolved=True, emit=False,
                )
            except OSError as exc:
                click.echo(f"io error: {exc}", err=True)
                raise click.exceptions.Exit(2)
            for tok, reason in result.unresolved:
                click.echo(
                    f"{path}:{tok.lineno}:{tok.col}: {tok.raw} — {reason}",
                    err=True,
                )
                total_misses += 1
            total_ok += result.resolved
    finally:
        if db is not None:
            db.close()

    click.echo(
        f"validated {total_ok} tokens across {len(paths)} file(s); "
        f"{total_misses} unresolved"
    )
    if total_misses:
        raise click.exceptions.Exit(1)


# ── RDR-083 validators ───────────────────────────────────────────────────────


@doc.command("check-grounding")
@click.argument(
    "paths", nargs=-1, required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--fail-under", type=float, default=None,
    help="Exit non-zero when chash-coverage ratio falls under this value (0.0-1.0).",
)
@click.option(
    "--fail-ungrounded", is_flag=True, default=False,
    help="Exit non-zero when any chash: citation fails to resolve via "
         "Catalog.resolve_chash (RDR-086 Phase 4.1).",
)
@click.option(
    "--format", "output_format", type=click.Choice(["table", "json"]),
    default="table", help="Report format.",
)
def check_grounding_cmd(
    paths: tuple[Path, ...],
    fail_under: float | None,
    fail_ungrounded: bool,
    output_format: str,
) -> None:
    """Report citation-coverage per markdown file.

    Coverage = chash citations / (chash + prose + bracket). Prose and
    bracketed citations are not machine-verifiable — the ratio tells you
    how much of this doc's grounding is upgradeable.

    With ``--fail-ungrounded`` (RDR-086 Phase 4.1), every ``chash:`` span
    is resolved via ``Catalog.resolve_chash``; any miss triggers a
    non-zero exit plus a file:line error report.
    """
    import json

    # RDR-086 Phase 4.1: open Catalog + T3 + ChashIndex only when actually
    # resolving. This keeps the default path fast and free of T2/T3 deps
    # (important for CI on fresh clones with no .nexus/ directory).
    cat = t3 = chash_index = None
    if fail_ungrounded:
        try:
            cat, t3, chash_index = _phase4_catalog_t3_chash()
        except Exception as exc:
            click.echo(
                f"--fail-ungrounded cannot resolve: {exc}", err=True,
            )
            raise click.exceptions.Exit(2)

    results = []
    any_fail = False
    any_unresolved = False
    try:
        for path in paths:
            text = path.read_text(errors="replace")
            cites = scan_citations(text)
            report = grounding_report(cites)
            unresolved_here: list[tuple[int, str]] = []
            if fail_ungrounded and cat is not None:
                for c in cites:
                    if c.kind != "chash" or not c.chash:
                        continue
                    try:
                        ref = cat.resolve_chash(c.chash, t3, chash_index)
                    except Exception:
                        ref = None
                    if ref is None:
                        unresolved_here.append((c.lineno, c.chash))

            if unresolved_here:
                any_unresolved = True
                for lineno, h in unresolved_here:
                    click.echo(
                        f"{path}:{lineno}: unresolved chash:{h[:8]}… "
                        f"(not found in T2 index or T3 fallback scan)",
                        err=True,
                    )

            results.append({
                "path": str(path),
                "total": report.total,
                "chash": report.chash_count,
                "prose": report.prose_count,
                "bracket": report.bracket_count,
                "coverage": round(report.coverage, 4),
                "unresolved": len(unresolved_here),
            })
            if (
                fail_under is not None
                and report.total > 0
                and report.coverage < fail_under
            ):
                any_fail = True
    finally:
        if chash_index is not None:
            try:
                chash_index.close()
            except Exception:
                pass

    if output_format == "json":
        click.echo(json.dumps(results, indent=2))
    else:
        click.echo(f"{'file':<40}{'total':>6}{'chash':>7}{'prose':>7}{'bracket':>9}{'cov':>7}")
        for r in results:
            click.echo(
                f"{r['path'][:38]:<40}"
                f"{r['total']:>6}{r['chash']:>7}"
                f"{r['prose']:>7}{r['bracket']:>9}"
                f"{r['coverage']:>7.2f}"
            )

    if any_fail or (fail_ungrounded and any_unresolved):
        raise click.exceptions.Exit(1)


@doc.command("check-extensions")
@click.argument(
    "paths", nargs=-1, required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--primary-source", required=True,
    help="Collection whose projection defines 'grounded' "
         "(e.g. docs__art-grossberg-papers).",
)
@click.option(
    "--threshold", type=float, default=0.70,
    help="Projection-similarity cutoff. Docs at-or-above are grounded; "
         "below → author-extension candidates.",
)
@click.option(
    "--format", "output_format", type=click.Choice(["table", "json"]),
    default="table",
)
def check_extensions_cmd(
    paths: tuple[Path, ...],
    primary_source: str,
    threshold: float,
    output_format: str,
) -> None:
    """Flag doc chunks that don't project into a primary source.

    For each ``chash:`` citation in the input docs, resolve the chash
    through the T2 ``chash_index`` to the ChromaDB-scoped ``doc_id``
    (RDR-086 Phase 4.2 caller-side fix), then delegate to
    ``CatalogTaxonomy.chunk_grounded_in`` with the resolved doc_id.
    The taxonomy signature and semantics are unchanged — the fix is
    in the caller, so every other consumer of ``chunk_grounded_in``
    keeps working.

    Docs whose best-projecting chunk scores below ``--threshold`` are
    flagged as author-extension candidates.
    """
    import json

    results = []
    any_candidate = False

    try:
        cat, t3, chash_index = _phase4_catalog_t3_chash()
    except Exception as exc:
        click.echo(f"cannot open chash resolver: {exc}", err=True)
        raise click.exceptions.Exit(2)

    try:
        # Hold the T2 taxonomy open for the whole command, then drop it
        # — ``_phase4_t2_taxonomy`` now returns a context manager so the
        # underlying T2Database closes on exit (review #3).
        try:
            taxonomy_ctx = _phase4_t2_taxonomy()
        except sqlite_errors() as exc:
            click.echo(f"cannot open T2: {exc}", err=True)
            raise click.exceptions.Exit(2)

        with taxonomy_ctx as taxonomy:
            for path in paths:
                text = path.read_text(errors="replace")
                cites = scan_citations(text)

                # RDR-086 Phase 4.2: resolve each chash → ChunkRef → doc_id
                # BEFORE handing to extensions_report. The chunk_grounded_in
                # call underneath receives a Chroma-scoped doc_id that
                # matches ``topic_assignments.doc_id``.
                resolved_doc_ids: list[str] = []
                for c in cites:
                    if c.kind != "chash" or not c.chash:
                        continue
                    try:
                        ref = cat.resolve_chash(c.chash, t3, chash_index)
                    except Exception:
                        ref = None
                    if ref is not None and ref.get("doc_id"):
                        resolved_doc_ids.append(ref["doc_id"])

                # Deduplicate while preserving order (first-resolved wins).
                seen: set[str] = set()
                doc_ids: list[str] = []
                for d in resolved_doc_ids:
                    if d not in seen:
                        seen.add(d)
                        doc_ids.append(d)

                report = extensions_report(
                    doc_ids,
                    primary_source=primary_source,
                    threshold=threshold,
                    taxonomy=taxonomy,
                )
                results.append({
                    "path": str(path),
                    "checked": report.checked,
                    "candidates": [
                        {"doc_id": d, "similarity": s}
                        for d, s in report.candidates
                    ],
                    "no_data": list(report.no_data),
                })
                if report.candidates:
                    any_candidate = True
    finally:
        try:
            chash_index.close()
        except Exception:
            pass

    if output_format == "json":
        click.echo(json.dumps(results, indent=2))
    else:
        click.echo(
            f"{'file':<36}{'checked':>8}{'candidates':>12}{'no-data':>9}"
        )
        for r in results:
            click.echo(
                f"{r['path'][:34]:<36}{r['checked']:>8}"
                f"{len(r['candidates']):>12}{len(r['no_data']):>9}"
            )
            for c in r["candidates"]:
                click.echo(
                    f"  candidate: {c['doc_id'][:40]} "
                    f"(similarity={c['similarity']:.3f})"
                )

    if any_candidate:
        raise click.exceptions.Exit(1)


def sqlite_errors() -> tuple[type[BaseException], ...]:
    """Broad exception tuple for T2 open failures — sqlite3.DatabaseError
    plus anything else the facade may raise."""
    import sqlite3

    return (sqlite3.Error, OSError)


# ── RDR-086 Phase 4 helpers ──────────────────────────────────────────────────
#
# resolve_chash needs three collaborators: a Catalog (JSONL + SQLite cache),
# a T3 client (ChromaDB), and a ChashIndex (T2 lookup table). The three
# nx doc subcommands all need the same trio; this helper centralises
# construction so tests can monkeypatch one call site to inject fakes.


def _phase4_catalog_t3_chash() -> tuple[Any, Any, Any]:
    """Return (Catalog, T3Database, ChashIndex) for chash resolution.

    Forward-ref typed to avoid a top-level import of the three heavy
    modules (``Catalog`` pulls in ``CatalogDB``, ``T3Database`` pulls
    in ``chromadb``) at module load. The tuple shape is a contract
    enforced by the three CLI consumers, documented here:

        cat, t3, chash_index = _phase4_catalog_t3_chash()

    The Catalog is constructed from the conventional catalog path under
    ``default_db_path()``'s parent, matching ``mcp_infra.get_catalog``.
    T3 comes from ``nexus.db.make_t3``. ChashIndex opens the same T2
    path used by every other T2 store.
    """
    from nexus.catalog.catalog import Catalog
    from nexus.db import make_t3
    from nexus.db.t2.chash_index import ChashIndex

    db_path = default_db_path()
    cat_path = db_path.parent / "catalog"
    cat: Any = Catalog(cat_path, cat_path / ".catalog.db")
    t3: Any = make_t3()
    chash_index: Any = ChashIndex(db_path)
    return cat, t3, chash_index


def _phase4_t2_taxonomy():
    """Yield a T2 taxonomy store; closes the underlying T2Database on exit.

    Separate from ``_phase4_catalog_t3_chash`` so tests can patch the
    taxonomy independently of the chash resolver collaborators. Returned
    as a context manager — the previous ``.taxonomy`` attribute-return
    leaked the five-connection T2Database for the command's lifetime
    (review #3).
    """
    from contextlib import contextmanager

    from nexus.db.t2 import T2Database

    @contextmanager
    def _taxonomy_ctx():
        db = T2Database(default_db_path())
        try:
            yield db.taxonomy
        finally:
            db.close()

    return _taxonomy_ctx()


# ── RDR-086 Phase 5: nx doc cite ─────────────────────────────────────────────


def _phase5_search(*, query: str, corpus: str, limit: int) -> dict:
    """Composable indirection over ``nexus.mcp.core.search``.

    A dedicated seam so ``test_phase5_doc_cite`` can replace the wire
    call with a fake that returns a deterministic structured response.
    The MCP ``search`` tool already runs the full search pipeline and
    returns the Phase 3 envelope (``ids``, ``distances``, ``collections``,
    ``chunk_text_hash``, ``tumblers``) — exactly what ``cite`` needs.
    """
    from nexus.mcp.core import search as _mcp_search

    result = _mcp_search(
        query=query, corpus=corpus, limit=limit, structured=True,
    )
    return result if isinstance(result, dict) else {}


def _chash_index_is_empty(chash_index) -> bool:
    """True if no chash_index rows exist anywhere — fresh-install guard.

    Thin wrapper over ``ChashIndex.is_empty()`` that swallows open/IO
    failures so a corrupt or missing T2 file doesn't crash ``nx doc cite``
    before the caller sees the "run backfill" hint.
    """
    try:
        return chash_index.is_empty()
    except Exception:
        return False


@doc.command("cite")
@click.argument("claim", type=str)
@click.option(
    "--against", "collection", required=True,
    help="Collection to search for a grounding chunk (e.g. "
         "knowledge__art-grossberg-papers).",
)
@click.option(
    "--limit", type=int, default=5,
    help="Candidate fan-out (default 5). Tied candidates within 0.01 "
         "distance are surfaced in --json; stdout picks the first.",
)
@click.option(
    "--min-similarity", type=float, default=0.30,
    help="Maximum acceptable distance (lower is stricter). Top result "
         "with distance above this is treated as 'no good cite' — exit 1.",
)
@click.option(
    "--json", "as_json", is_flag=True, default=False,
    help="Emit the full candidate schema as JSON instead of a markdown link.",
)
def cite_cmd(
    claim: str,
    collection: str,
    limit: int,
    min_similarity: float,
    as_json: bool,
) -> None:
    """Emit a chash: markdown link grounding *claim* in *collection*.

    Composes ``search(structured=True)`` with the ``chunk_text_hash``
    surface from Phase 3 and resolves the top hash via
    ``Catalog.resolve_chash`` to fetch the chunk excerpt. Default
    stdout is a paste-ready ``[excerpt](chash:<hex>)`` markdown link.

    Exit contract:
      * 0 — cite emitted (JSON mode: ``threshold_met`` reflects --min-similarity)
      * 1 — top distance above --min-similarity; stderr warning, stdout empty
        for the markdown path; JSON still returns candidates with
        ``threshold_met=false``
      * 2 — usage errors: empty chash_index (fresh install), empty
        collection, or unknown collection
    """
    import json as _json

    try:
        cat, t3, chash_index = _phase4_catalog_t3_chash()
    except Exception as exc:
        click.echo(f"cannot open resolver: {exc}", err=True)
        raise click.exceptions.Exit(2)

    try:
        # RDR-086 gate finding S2: empty-index short-circuit.
        if _chash_index_is_empty(chash_index):
            click.echo(
                "chash_index not populated — run 'nx collection backfill-hash "
                "--all' (25-70 min on a 278k-chunk corpus). Re-run after "
                "backfill completes.",
                err=True,
            )
            raise click.exceptions.Exit(2)

        result = _phase5_search(query=claim, corpus=collection, limit=limit)
        ids = result.get("ids") or []
        hashes = result.get("chunk_text_hash") or []
        distances = result.get("distances") or []
        collections = result.get("collections") or [collection]

        if not ids or not hashes:
            click.echo(
                f"no indexed content in {collection}",
                err=True,
            )
            raise click.exceptions.Exit(2)

        # Fetch chunk text for the top candidate.
        top_hash = hashes[0]
        top_distance = float(distances[0]) if distances else 1.0
        threshold_met = top_distance <= min_similarity

        try:
            ref = cat.resolve_chash(top_hash, t3, chash_index)
        except Exception:
            ref = None
        excerpt = ""
        if ref is not None:
            excerpt = str(ref.get("chunk_text", "")).strip()
        # Trim to 200 chars for JSON excerpt + 60 for markdown display.
        excerpt_200 = excerpt[:200]
        display = excerpt[:60] if excerpt else f"chash:{top_hash[:8]}…"
        top_link = f"[{display}](chash:{top_hash})"

        # Tied candidates (within 0.01 distance).
        tied: list[int] = []
        if distances:
            for i, d in enumerate(distances):
                if abs(float(d) - top_distance) <= 0.01:
                    tied.append(i)

        # JSON envelope — always returns candidates, threshold_met flag
        # differentiates above/below min-similarity.
        if as_json:
            candidates = []
            for i in tied if as_json else [0]:
                h = hashes[i] if i < len(hashes) else ""
                d = float(distances[i]) if i < len(distances) else 1.0
                c = collections[0] if collections else collection
                try:
                    r = cat.resolve_chash(h, t3, chash_index) if h else None
                except Exception:
                    r = None
                exc = (str(r.get("chunk_text", "")).strip()[:200]
                       if r is not None else "")
                disp = exc[:60] if exc else f"chash:{h[:8]}…"
                candidates.append({
                    "chash": h,
                    "distance": d,
                    "collection": c,
                    "chunk_excerpt": exc,
                    "markdown_link": f"[{disp}](chash:{h})",
                })
            payload = {
                "query": claim,
                "threshold_met": threshold_met,
                "candidates": candidates,
            }
            click.echo(_json.dumps(payload, indent=2))
            raise click.exceptions.Exit(0 if threshold_met else 1)

        # Markdown-link mode.
        if not threshold_met:
            click.echo(
                f"top candidate distance {top_distance:.3f} "
                f"above threshold {min_similarity:.3f} — no cite emitted. "
                "Try a narrower claim or broaden the collection.",
                err=True,
            )
            raise click.exceptions.Exit(1)

        click.echo(top_link)
        if len(tied) > 1:
            click.echo(
                f"# {len(tied)} candidates tied (see --json)", err=True,
            )
        raise click.exceptions.Exit(0)
    finally:
        try:
            chash_index.close()
        except Exception:
            pass
