# SPDX-License-Identifier: AGPL-3.0-or-later
"""Print a saved ``taxonomy_ab.py`` leg-1 run against a gated re-run (``--gate-from``), and the gated
run's own paired gated-minus-ungated table. Reads two output files; touches no engine.

    PYTHONPATH=scripts uv run python scripts/bench/taxonomy_ab_compare.py \\
        bench/results/rdr134-taxonomy-ab-leg1-2026-11-16.json \\
        bench/results/rdr134-taxonomy-ab-leg1-gated-2026-11-16.json

Three parts. (1) The two runs must be the same measurement: when the sets, ``S2_collections``, the
question ids or ``n`` differ, the output is ``MISMATCH`` lines and the exit status is 1, with no
table. (2) Saved against gated, per set and arm, over the arms both files hold. Those are two
separate runs, and the ungated arms of a ``--gate-from`` run differ from the saved ones by design
(a collection with no topic is searched unscoped), so read this part as context. (3) The pairing the
verdict rests on: in the gated run, every ``@g`` arm minus its ungated arm, question by question,
counted in questions, then each changed question's top 3 with the collections it moved between.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

FIELDS = ("ndcg3", "recall10", "wins", "losses", "d_ndcg3")
EPS = 1e-9


def _cell(saved: dict[str, Any], gated: dict[str, Any], field: str) -> str:
    s, g = saved.get(field), gated.get(field)
    if s is None or g is None:  # OFF has no wins, losses or deltas
        return "-"
    if field in ("wins", "losses"):
        return f"{s}->{g}"
    return f"{s:.4f}->{g:.4f} ({g - s:+.4f})"


def mismatches(saved: dict[str, Any], gated: dict[str, Any]) -> list[str]:
    """What makes two runs not the same measurement: n, S2_collections, the sets, the question ids."""
    out: list[str] = []
    ss, gs = saved.get("summary", {}), gated.get("summary", {})
    for key in ("n", "S2_collections"):
        if ss.get(key) != gs.get(key):
            out.append(f"{key}: saved {ss.get(key)!r}, gated {gs.get(key)!r}")
    sa, ga = saved.get("sets") or {}, gated.get("sets") or {}
    for name in sorted(set(sa) | set(ga)):
        if sa.get(name) != ga.get(name):
            only_s = sorted(set(sa.get(name) or []) - set(ga.get(name) or []))
            only_g = sorted(set(ga.get(name) or []) - set(sa.get(name) or []))
            out.append(f"set {name}: only in saved {only_s}, only in gated {only_g}")
    qs = [r["qid"] for r in saved.get("results") or []]
    qg = [r["qid"] for r in gated.get("results") or []]
    if qs != qg:
        out.append(f"question ids: saved {len(qs)}, gated {len(qg)}; "
                   f"only in saved {sorted(set(qs) - set(qg))}, only in gated {sorted(set(qg) - set(qs))}"
                   + ("" if set(qs) != set(qg) else "; same ids in a different order"))
    return out


def _saved_vs_gated(saved: dict[str, Any], gated: dict[str, Any]) -> list[str]:
    ssum, gsum = saved["summary"], gated["summary"]
    lines: list[str] = []
    for sname in sorted(set(ssum) | set(gsum)):
        if sname in ("n", "S2_collections"):
            continue
        lines.append(f"{sname}  (saved->gated)")
        for arm in ssum[sname]:
            if arm not in gsum[sname]:
                lines.append(f"  {arm}: only in saved")
                continue
            lines.append(f"  {arm:<10} " + "  ".join(
                f"{f}={_cell(ssum[sname][arm], gsum[sname][arm], f)}" for f in FIELDS))
        lines += [f"  {arm}: only in gated" for arm in gsum[sname]
                  if arm not in ssum[sname] and "@g" not in arm]
    return lines


def paired(gated: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """set -> gated arm -> {better, worse, same, n, mean_d_ndcg3, mean_d_recall10}: each ``@g`` arm
    minus the ungated arm it is built on, per question of the one run, counted in questions."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    results = gated.get("results") or []
    for sname in (gated.get("sets") or {}):
        arms = sorted({k for r in results for k in r.get(sname, {}) if "@g" in k})
        for arm in arms:
            base = arm.split("@")[0]
            dn = [r[sname][arm]["ndcg3"] - r[sname][base]["ndcg3"] for r in results]
            dr = [r[sname][arm]["recall10"] - r[sname][base]["recall10"] for r in results]
            n = len(dn)
            out.setdefault(sname, {})[arm] = {
                "better": sum(x > EPS for x in dn), "worse": sum(x < -EPS for x in dn),
                "same": sum(abs(x) <= EPS for x in dn), "n": n,
                "mean_d_ndcg3": sum(dn) / n if n else 0.0, "mean_d_recall10": sum(dr) / n if n else 0.0}
    return out


def _moves(gated: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for r in gated.get("results") or []:
        for sname in (gated.get("sets") or {}):
            for arm in sorted(k for k in r.get(sname, {}) if "@g" in k):
                a, b = r[sname][arm.split("@")[0]], r[sname][arm]
                if a["top3"] == b["top3"] and a.get("top3_coll") == b.get("top3_coll"):
                    continue
                ca, cb = a.get("top3_coll"), b.get("top3_coll")
                moved = ("; collections moved: " + ", ".join(f"{x} -> {y}" for x, y in zip(ca, cb) if x != y)
                         if ca is not None and cb is not None else "; (no per-document collections stored)")
                lines.append(f"  {r['qid']} {sname} {arm}: top3 {a['top3']} -> {b['top3']}{moved}")
    return lines


def compare(saved: dict[str, Any], gated: dict[str, Any]) -> str:
    bad = mismatches(saved, gated)
    if bad:
        return "\n".join(["MISMATCH: these two files are not the same measurement; no comparison printed"]
                         + [f"  {m}" for m in bad])
    lines = [f"n: {gated['summary']['n']} questions, S2 {gated['summary']['S2_collections']} collections"]
    info = gated.get("gate")
    if info:
        unrouted = info["unrouted"]
        for label, names in (unrouted.items() if isinstance(unrouted, dict) else [("gate", unrouted)]):
            lines.append(f"unrouted by the gate at {label} ({len(names)}): " + ", ".join(names))
        if info.get("gate_not_in_sets"):
            lines.append("gated but not in the sets: " + ", ".join(info["gate_not_in_sets"]))
        for key in ("sets_not_in_report", "routed_not_in_report"):
            if info.get(key):
                lines.append("in the sets with no coverage row (stay routed, coverage unknown): "
                             + ", ".join(info[key]))
    for note in gated.get("notes") or []:
        lines.append(f"note: {note}")
    lines.append("")
    lines += _saved_vs_gated(saved, gated)
    pr = paired(gated)
    if pr:
        lines += ["", "paired, gated minus ungated, same run (questions)"]
        for sname, arms in pr.items():
            lines.append(f"{sname}")
            for arm, d in arms.items():
                lines.append(f"  {arm:<16} better {d['better']}, worse {d['worse']}, same {d['same']} of {d['n']}; "
                             f"mean d_ndcg3 {d['mean_d_ndcg3']:+.4f}, mean d_recall10 {d['mean_d_recall10']:+.4f}")
        moves = _moves(gated)
        lines += ["", f"changed questions ({len(moves)})"] + moves
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        sys.stderr.write("usage: taxonomy_ab_compare.py SAVED.json GATED.json\n")
        return 2
    saved, gated = (json.loads(Path(p).read_text()) for p in args)
    text = compare(saved, gated)
    sys.stdout.write(text + "\n")
    return 1 if text.startswith("MISMATCH") else 0


if __name__ == "__main__":
    sys.exit(main())
