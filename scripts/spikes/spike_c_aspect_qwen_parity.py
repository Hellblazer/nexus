#!/usr/bin/env -S uv run python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Spike C — aspect_extractor A/B parity harness: claude vs qwen.

Follow-on to PR #780 (``NEXUS_ASPECT_BACKEND={claude,qwen}``). The
production code-path is left untouched; this script toggles the env
var around the existing :func:`extract_aspects` entrypoint, captures
the resulting :class:`AspectRecord` from each engine, and emits a
field-by-field agreement report plus per-engine wall-clock stats.

Backend requirements
--------------------

* ``NEXUS_ASPECT_BACKEND=claude`` leg: ``claude`` CLI on PATH.
* ``NEXUS_ASPECT_BACKEND=qwen`` leg: the qwen-coprocessor-stack
  supervisor must be reachable. Configuration is resolved by
  ``nexus.operators.qwen_dispatch`` at call time — typically from
  ``~/.qwen-coprocessor-stack/config.json`` plus env (``QWEN_STACK_URL``).
  This script does not validate the qwen path itself; an unreachable
  backend surfaces as ``ExtractFail`` / null-fields ``AspectRecord``
  which the parity diff records honestly.

Corpus
------

Input is a list of source URIs. Each "URI" is either:

* an absolute or relative filesystem path to a ``.md`` / ``.txt`` /
  ``.pdf`` file (text extracted client-side and passed to
  ``extract_aspects(content=...)``).
* a ``chroma://<collection>/<source_path>`` URI: the script passes
  ``content=""`` to ``extract_aspects`` and lets it route through
  ``nexus.aspect_readers.read_source`` exactly like production.

Source list is provided via repeated ``--uri`` flags or a JSON
manifest ``[{"uri": "...", "collection": "knowledge__..."}, ...]``.
The ``collection`` is required so the right extractor config is
selected; when reading a local file directly the operator picks the
collection (typically ``knowledge__<name>`` to route the
``scholarly-paper-v1`` LLM extractor).

Field-equality heuristics
-------------------------

``AspectRecord`` carries a mix of free-form prose and structured
fields. The diff function classifies each in one of three buckets:

* **strict** — equality after light normalisation (whitespace
  collapse, case-insensitive). Used for short discrete values:
  ``collection``, ``source_path``, ``extractor_name``,
  ``model_version`` (note: model_version is always pinned by config
  so equality is expected by construction; included for completeness).
* **set** — order-insensitive set equality on string-list fields:
  ``experimental_datasets``, ``experimental_baselines``,
  ``salient_sentences``. Two empty lists agree.
* **prose** — free-form text where exact equality is unreasonable
  across engines. Heuristic: both-non-empty AND length ratio within
  ``PROSE_LEN_TOL`` (default 0.5, i.e. shorter / longer >= 0.5)
  counts as agreement. Either-empty-but-not-both counts as
  disagreement. Both-None counts as agreement. Applies to:
  ``problem_formulation``, ``proposed_method``,
  ``experimental_results``.
* **ignored** — wall-clock metadata not part of semantic equality:
  ``extracted_at``, ``confidence`` (engines self-report;
  cross-engine comparison meaningless), ``doc_id``, ``source_uri``,
  ``extras``.

This heuristic is documented in code so an operator reading the
parity report knows what "agreement" means. Tune ``PROSE_LEN_TOL``
or restrict the diff to specific fields via ``--fields`` to harden
or relax the bar.

Out of scope
------------

* Actually running the bench against a production corpus — operator-
  driven. This script ships the harness; the bench-run command is
  ``uv run python scripts/spikes/spike_c_aspect_qwen_parity.py
  --manifest path/to/manifest.json --out out.jsonl``.
* Cost telemetry beyond wall-clock. PR #776 added cost metrics in
  qwen_dispatch; reading those is a follow-on.

Usage
-----

::

    # Single paper, two backends, 1 run each
    uv run python scripts/spikes/spike_c_aspect_qwen_parity.py \\
        --uri papers/2408.04948.pdf \\
        --collection knowledge__local-spike \\
        --out /tmp/parity.jsonl

    # Manifest with mixed sources, 3 repeats for variance
    uv run python scripts/spikes/spike_c_aspect_qwen_parity.py \\
        --manifest bench_corpus.json --repeat 3 --limit 20 \\
        --out /tmp/parity.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from nexus.aspect_extractor import (  # noqa: E402
    AspectRecord,
    ExtractFail,
    extract_aspects,
)

# ── Field-equality heuristic config ──────────────────────────────────────────

STRICT_FIELDS: tuple[str, ...] = (
    "collection",
    "source_path",
    "extractor_name",
    "model_version",
)
SET_FIELDS: tuple[str, ...] = (
    "experimental_datasets",
    "experimental_baselines",
    "salient_sentences",
)
PROSE_FIELDS: tuple[str, ...] = (
    "problem_formulation",
    "proposed_method",
    "experimental_results",
)
IGNORED_FIELDS: tuple[str, ...] = (
    "extracted_at",
    "confidence",
    "doc_id",
    "source_uri",
    "extras",
)
ALL_DIFFED_FIELDS: tuple[str, ...] = STRICT_FIELDS + SET_FIELDS + PROSE_FIELDS

PROSE_LEN_TOL: float = 0.5  # shorter/longer must be >= this for agreement


def _norm_strict(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, str):
        return " ".join(v.split()).lower()
    return v


def _agree_strict(a: Any, b: Any) -> bool:
    return _norm_strict(a) == _norm_strict(b)


def _agree_set(a: Any, b: Any) -> bool:
    sa = set(a or [])
    sb = set(b or [])
    return sa == sb


def _agree_prose(a: Any, b: Any, tol: float = PROSE_LEN_TOL) -> bool:
    if a is None and b is None:
        return True
    if not a and not b:  # both empty string
        return True
    if not a or not b:
        return False
    la, lb = len(a), len(b)
    if max(la, lb) == 0:
        return True
    return min(la, lb) / max(la, lb) >= tol


def diff_records(
    claude_rec: AspectRecord | ExtractFail | None,
    qwen_rec: AspectRecord | ExtractFail | None,
) -> dict[str, Any]:
    """Field-by-field agreement dict between two extraction outputs.

    Returns a dict with key ``agreement`` mapping each diffed field
    name to a bool. Adds ``both_ok`` (both produced AspectRecord),
    ``claude_ok``, ``qwen_ok`` summary flags. Non-AspectRecord
    outputs (ExtractFail / None) trivially disagree on all fields
    except where both sides match the same failure shape.
    """
    out: dict[str, Any] = {
        "claude_ok": isinstance(claude_rec, AspectRecord),
        "qwen_ok": isinstance(qwen_rec, AspectRecord),
        "both_ok": (
            isinstance(claude_rec, AspectRecord)
            and isinstance(qwen_rec, AspectRecord)
        ),
        "agreement": {},
    }
    if not out["both_ok"]:
        # Symmetric failure shape (both None, both same ExtractFail.reason)
        # is recorded for transparency but isn't a parity agreement.
        out["claude_kind"] = type(claude_rec).__name__
        out["qwen_kind"] = type(qwen_rec).__name__
        for f in ALL_DIFFED_FIELDS:
            out["agreement"][f] = False
        return out

    assert isinstance(claude_rec, AspectRecord)
    assert isinstance(qwen_rec, AspectRecord)
    for f in STRICT_FIELDS:
        out["agreement"][f] = _agree_strict(
            getattr(claude_rec, f), getattr(qwen_rec, f),
        )
    for f in SET_FIELDS:
        out["agreement"][f] = _agree_set(
            getattr(claude_rec, f), getattr(qwen_rec, f),
        )
    for f in PROSE_FIELDS:
        out["agreement"][f] = _agree_prose(
            getattr(claude_rec, f), getattr(qwen_rec, f),
        )
    return out


# ── Source loading ───────────────────────────────────────────────────────────


def _load_local_text(path: Path) -> str:
    """Read a local source file. ``.md`` / ``.txt`` read directly;
    ``.pdf`` lazy-imports the project's :class:`PDFExtractor`. Any
    other suffix is read as bytes-decoded-utf8 best-effort.
    """
    suffix = path.suffix.lower()
    if suffix in (".md", ".markdown", ".txt", ""):
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".pdf":
        from nexus.pdf_extractor import PDFExtractor
        result = PDFExtractor().extract(path, extractor="auto")
        return result.text
    return path.read_text(encoding="utf-8", errors="replace")


def _run_one(
    uri: str,
    collection: str,
    backend: str,
) -> tuple[AspectRecord | ExtractFail | None, float]:
    """Invoke ``extract_aspects`` for one (uri, collection) under the
    given backend. Returns (record, elapsed_ms). Toggles the env var
    just for this call window.
    """
    os.environ["NEXUS_ASPECT_BACKEND"] = backend
    if uri.startswith("chroma://"):
        # Production path: hand to extract_aspects with empty content,
        # parsed lookup. The chroma:// form is shaped as
        # chroma://<collection>/<source_path>; we already have the
        # collection separately, so source_path is the URI tail.
        # extract_aspects rebuilds the URI from (collection, source_path)
        # internally — the operator must pass a matching collection.
        # Split: ``chroma://<coll>/<rest>``
        rest = uri[len("chroma://"):]
        slash = rest.find("/")
        source_path = rest[slash + 1:] if slash != -1 else rest
        content = ""
    else:
        p = Path(uri)
        if not p.is_absolute():
            p = (REPO_ROOT / p).resolve()
        content = _load_local_text(p)
        source_path = str(p)

    t0 = time.perf_counter()
    try:
        rec = extract_aspects(
            content=content,
            source_path=source_path,
            collection=collection,
        )
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return (
            ExtractFail(
                uri=uri, reason="exception",
                detail=f"{type(exc).__name__}: {exc}",
            ),
            elapsed_ms,
        )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return rec, elapsed_ms


def _record_to_json(rec: AspectRecord | ExtractFail | None) -> Any:
    if rec is None:
        return None
    if isinstance(rec, ExtractFail):
        return {
            "_kind": "ExtractFail",
            "uri": rec.uri,
            "reason": rec.reason,
            "detail": rec.detail,
        }
    return {"_kind": "AspectRecord", **asdict(rec)}


# ── Manifest + CLI ───────────────────────────────────────────────────────────


def _load_manifest(path: Path) -> list[dict]:
    """Load a JSON manifest of [{"uri": ..., "collection": ...}, ...].

    A manifest may also override the default collection per-row. Rows
    missing ``collection`` inherit the CLI ``--collection`` value.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"manifest must be a JSON array; got {type(data).__name__}")
    return data


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="A/B parity harness for aspect_extractor (claude vs qwen)",
    )
    p.add_argument(
        "--uri", action="append", default=[],
        help="Source URI: file path or chroma://... (repeatable).",
    )
    p.add_argument(
        "--manifest", type=Path,
        help="JSON manifest: [{uri, collection?}, ...].",
    )
    p.add_argument(
        "--collection", default="knowledge__spike-c",
        help="Default collection for sources without one (default: "
             "knowledge__spike-c — routes scholarly-paper-v1).",
    )
    p.add_argument(
        "--limit", type=int, default=0,
        help="Cap papers (0 = unbounded).",
    )
    p.add_argument(
        "--repeat", type=int, default=1,
        help="Repeat each paper N times for variance (default 1).",
    )
    p.add_argument(
        "--out", type=Path, required=True,
        help="JSONL log output path.",
    )
    p.add_argument(
        "--summary", type=Path,
        help="Optional markdown summary path (default: <out>.md).",
    )
    p.add_argument(
        "--backends", default="claude,qwen",
        help="Comma-separated backends to run (default: claude,qwen).",
    )
    return p.parse_args(argv)


def _summarize(records: list[dict]) -> dict:
    """Aggregate per-field agreement + per-engine ok-rate + median ms."""
    both_ok = [r for r in records if r["both_ok"]]
    field_agree: dict[str, list[bool]] = {f: [] for f in ALL_DIFFED_FIELDS}
    for r in both_ok:
        for f, v in r["agreement"].items():
            field_agree.setdefault(f, []).append(bool(v))

    claude_ms = [r["claude_elapsed_ms"] for r in records if r.get("claude_elapsed_ms") is not None]
    qwen_ms = [r["qwen_elapsed_ms"] for r in records if r.get("qwen_elapsed_ms") is not None]

    def _rate(xs: list[bool]) -> float:
        return (sum(1 for x in xs if x) / len(xs)) if xs else 0.0

    def _med(xs: list[float]) -> float:
        return statistics.median(xs) if xs else 0.0

    return {
        "total": len(records),
        "both_ok": len(both_ok),
        "claude_ok_rate": _rate([r["claude_ok"] for r in records]),
        "qwen_ok_rate": _rate([r["qwen_ok"] for r in records]),
        "field_agreement": {
            f: {"rate": _rate(v), "n": len(v)}
            for f, v in field_agree.items()
        },
        "claude_median_ms": _med(claude_ms),
        "qwen_median_ms": _med(qwen_ms),
        "claude_total_s": sum(claude_ms) / 1000.0,
        "qwen_total_s": sum(qwen_ms) / 1000.0,
    }


def _render_md(summary: dict, out_path: Path) -> str:
    lines = [
        "# Spike C — aspect_extractor A/B parity (claude vs qwen)",
        "",
        f"- Total runs: **{summary['total']}**",
        f"- Both-engine success: **{summary['both_ok']}**",
        f"- Claude ok-rate: **{summary['claude_ok_rate']:.2%}**",
        f"- Qwen ok-rate:   **{summary['qwen_ok_rate']:.2%}**",
        f"- Claude median elapsed: **{summary['claude_median_ms']:.0f} ms**",
        f"- Qwen median elapsed:   **{summary['qwen_median_ms']:.0f} ms**",
        f"- Claude wall-clock total: **{summary['claude_total_s']:.1f} s**",
        f"- Qwen wall-clock total:   **{summary['qwen_total_s']:.1f} s**",
        "",
        "## Per-field agreement (both-ok subset)",
        "",
        "| Field | Bucket | Agreement | N |",
        "|---|---|---|---|",
    ]
    bucket = {f: "strict" for f in STRICT_FIELDS}
    bucket.update({f: "set" for f in SET_FIELDS})
    bucket.update({f: f"prose (len-ratio >= {PROSE_LEN_TOL})" for f in PROSE_FIELDS})
    for f in ALL_DIFFED_FIELDS:
        info = summary["field_agreement"].get(f, {"rate": 0.0, "n": 0})
        lines.append(
            f"| `{f}` | {bucket.get(f, '?')} | {info['rate']:.2%} | {info['n']} |"
        )
    lines.append("")
    lines.append(f"_Raw JSONL: `{out_path}`_")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    sources: list[dict] = []
    for u in args.uri:
        sources.append({"uri": u, "collection": args.collection})
    if args.manifest:
        for entry in _load_manifest(args.manifest):
            sources.append({
                "uri": entry["uri"],
                "collection": entry.get("collection", args.collection),
            })
    if not sources:
        print("error: no sources (pass --uri or --manifest)", file=sys.stderr)
        return 2
    if args.limit:
        sources = sources[: args.limit]

    backends = [b.strip() for b in args.backends.split(",") if b.strip()]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    with args.out.open("w", encoding="utf-8") as fp:
        for repeat_ix in range(args.repeat):
            for src in sources:
                row: dict[str, Any] = {
                    "uri": src["uri"],
                    "collection": src["collection"],
                    "repeat": repeat_ix,
                }
                engine_recs: dict[str, AspectRecord | ExtractFail | None] = {}
                for backend in backends:
                    rec, elapsed_ms = _run_one(
                        src["uri"], src["collection"], backend,
                    )
                    engine_recs[backend] = rec
                    row[f"{backend}_record"] = _record_to_json(rec)
                    row[f"{backend}_elapsed_ms"] = elapsed_ms
                if "claude" in engine_recs and "qwen" in engine_recs:
                    diff = diff_records(engine_recs["claude"], engine_recs["qwen"])
                    row.update(diff)
                else:
                    # Single-backend pass-through — no diff possible.
                    row["claude_ok"] = isinstance(
                        engine_recs.get("claude"), AspectRecord,
                    )
                    row["qwen_ok"] = isinstance(
                        engine_recs.get("qwen"), AspectRecord,
                    )
                    row["both_ok"] = False
                    row["agreement"] = {}
                fp.write(json.dumps(row) + "\n")
                fp.flush()
                records.append(row)
                print(
                    f"[{repeat_ix + 1}/{args.repeat}] {src['uri']}: "
                    f"claude_ok={row['claude_ok']} qwen_ok={row['qwen_ok']} "
                    f"both_ok={row['both_ok']}",
                    file=sys.stderr,
                )

    summary = _summarize(records)
    summary_path = args.summary or args.out.with_suffix(args.out.suffix + ".md")
    summary_path.write_text(_render_md(summary, args.out), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"\nMarkdown summary: {summary_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
