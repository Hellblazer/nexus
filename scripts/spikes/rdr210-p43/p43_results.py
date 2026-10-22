# SPDX-License-Identifier: AGPL-3.0-or-later
"""results.json writer and the plain-text summary table."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any


def clean(value: Any) -> Any:
    """JSON-safe copy: NaN and infinities become null, tuples lists, keys strings."""
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value


def write_results(path: Path, doc: dict) -> None:
    """Write *doc* as strict JSON, atomically."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(clean(doc), indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _f(v: Any, nd: int = 3) -> str:
    return "n/a" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.{nd}f}"


def summary_text(doc: dict) -> str:
    """The plain-text table printed at the end of a run."""
    L: list[str] = []
    meta = doc.get("meta", {})
    L.append("RDR-210 P4.3 ranking measurement (nexus-t189m)")
    L.append(f"mode: {meta.get('mode')}  partial: {meta.get('partial', False)}  "
             f"engine: {meta.get('engine', {}).get('embedding_mode')} "
             f"profile={meta.get('engine', {}).get('embedding_profile')} "
             f"reranker={meta.get('engine', {}).get('reranker')}")
    L.append(f"voyage tokens: estimated {doc.get('tokens', {}).get('estimated_tokens_charged', 0):,}  "
             f"engine-reported {doc.get('tokens', {}).get('engine_reported_tokens', 0):,}  "
             f"cap {doc.get('tokens', {}).get('cap_tokens', 0):,}")
    a = doc.get("measurement_a")
    if a:
        L.append("")
        L.append("A. bge calibration factor = 0.45 / bge best-F1 threshold   (expected overlap 1.36-1.48)")
        L.append(f"   {'set':7s} {'bge thr':>8s} {'F1':>6s} {'factor':>7s} {'95% interval':>16s}   {'voyage thr':>10s} {'F1':>6s}   verdict")
        for label in ("prose", "code", "pooled"):
            entry = a["sets"].get(label, {})
            b = entry.get("bge", {}).get("pool_basis")
            v = entry.get("voyage", {}).get("pool_basis")
            if not b:
                continue
            ci = b["factor_ci95"]
            verdict = a["verdict"]["per_set"].get(label, {}).get("result", "")
            L.append(f"   {label:7s} {_f(b['threshold']):>8s} {_f(b['f1'], 2):>6s} {_f(b['factor'], 2):>7s} "
                     f"{_f(ci[0], 2) + ' - ' + _f(ci[1], 2):>16s}   "
                     f"{_f(v['threshold']) if v else 'n/a':>10s} {_f(v['f1'], 2) if v else 'n/a':>6s}   {verdict}")
        L.append(f"   headline (pooled): {a['verdict']['headline']}")
    b = doc.get("measurement_b")
    if b:
        L.append("")
        L.append("B. cross-encoder cross-request rank stability (split-merge vs one request)")
        sm, nf = b["split_merge"], b.get("noise_floor_shuffled_pool") or b["noise_floor_repeat"]
        L.append(f"   {'':22s} {'tau mean':>9s} {'tau min':>8s} {'top1 same':>10s} {'top10 ov':>9s} {'mean |dscore|':>14s}")
        for label, s in (("split-merge vs single", sm), ("noise: shuffled pool", b.get("noise_floor_shuffled_pool")),
                         ("noise: same request again", b["noise_floor_repeat"])):
            if s:
                L.append(f"   {label:22s} {_f(s['tau_mean'], 4):>9s} {_f(s['tau_min'], 4):>8s} "
                         f"{str(s['top1_same']) + '/' + str(s['queries']):>10s} {_f(s['top10_overlap_mean'], 3):>9s} "
                         f"{s['mean_abs_score_diff']:>14.2e}")
        vb = b["verdict"]
        L.append(f"   rerank model(s) seen: {', '.join(b.get('rerank_models_seen', [])) or 'unreported'}")
        L.append(f"   gap to noise floor {_f(vb['gap'], 4)} (rule: <= 0.01) -> {vb['result']}   [floor compared: {'shuffled pool' if b.get('noise_floor_shuffled_pool') else 'repeat'}; {_f(nf['tau_mean'], 4)}]")
    c = doc.get("measurement_c")
    if c:
        L.append("")
        L.append(f"C. cross-family inversions, queries out of 20 (dir1 = irrelevant bge above best relevant Voyage; dir2 = the reverse)")
        L.append(f"   table bge factor in force: {_f(c['calibration_table_bge_factor'], 3)}   "
                 f"research-17: default 12/5, factor 1.0 7/2, 1.15-1.30 3/1; P1.5 bound 3/1")
        if c.get("surface1_stderr_warnings"):
            L.append(f"   WARNING: `nx search` said something on stderr {len(c['surface1_stderr_warnings'])} time(s), "
                     f"e.g. {c['surface1_stderr_warnings'][0]}")
        for name, blk in c["surfaces"].items():
            L.append(f"   {name}")
            for depth, sc in blk["by_depth"].items():
                parts = []
                for kind in ("prose", "code"):
                    s = sc.get(kind)
                    if s:
                        parts.append(f"{kind} {s['dir1_bge_over_voyage']}/{s['dir2_voyage_over_bge']} "
                                     f"(missing {s['bge_relevant_missing']}/{s['voyage_relevant_missing']})")
                bound = blk["within_p1_5_bound"].get(depth, {})
                L.append(f"     top-{depth:<3s} " + "   ".join(parts)
                         + "   within bound: " + ", ".join(f"{k}={'yes' if v else 'NO'}" for k, v in bound.items()))
        depth = str(c["depths"][-1])
        for gname in ("factor_grid_surface_2", "factor_grid_surface_3"):
            grid = c.get(gname)
            if not grid:
                continue
            L.append(f"   {gname} (top-{depth}; prose dir1/dir2 | code dir1/dir2)")
            row = []
            for f, by_depth in grid.items():
                sc = by_depth[depth]
                p, k = sc.get("prose"), sc.get("code")
                row.append(f"{f}: {p['dir1_bge_over_voyage'] if p else '-'}/{p['dir2_voyage_over_bge'] if p else '-'} | "
                           f"{k['dir1_bge_over_voyage'] if k else '-'}/{k['dir2_voyage_over_bge'] if k else '-'}")
            L.extend("     " + r for r in row)
    return "\n".join(L)
