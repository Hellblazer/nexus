# SPDX-License-Identifier: AGPL-3.0-or-later
"""Temporal validity A/B, version 3 (bead nexus-wy6t5).

Pre-registration: T2 ``nexus/preregistration-temporal-validity-ab-v3-2026-09-09``.
The gate-7 critique of v2 (T2 ``nexus/critique-temporal-validity-ab-v2-2026-09-09``)
found a falsifier that could not fail, could not pass, and was cleared by the
control. v3 is the population on which the treatment can lose:

* only RDRs whose invalidity date is in frontmatter; unclassified files are
  removed from every candidate list;
* as-of leg only, two classes: F (in force at t, gold valid) and R
  (retrospective at t = today, gold invalid);
* arms A, H (treatment), S42 (soft at the weight v2 measured), D
  (edge-only demote-and-annotate); random-valid baseline; no arm E, since
  gold == source everywhere.

Windows, retrieval and statistics are imported from v2 unchanged. Reads are
read-only against the live store; the only writes are files under ``--out``.

Usage::

    uv run python scripts/experiments/temporal_validity_ab_v3.py windows
    uv run python scripts/experiments/temporal_validity_ab_v3.py gen
    uv run python scripts/experiments/temporal_validity_ab_v3.py run
    uv run python scripts/experiments/temporal_validity_ab_v3.py report
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import temporal_validity_ab_v2 as v2  # noqa: E402

REPO = v2.REPO
OUT_DEFAULT = REPO / "scripts" / "experiments" / "out" / "temporal_validity_ab_v3_2026-09-09"
SEED = 20260910
GEN_MODEL = "sonnet"
PER_PROVENANCE = 3
SOFT_SIM_WEIGHT = 0.58
SOFT_VALID_WEIGHT = 0.42
SOFT_INVALID_FLOOR = 0.05
TODAY = "2026-09-09"
MIX_F = 0.5          # pre-registered pooled mix of class F versus R
KEEP_FRACTION = 0.75  # falsifier (2): D keeps at least this share of A's class-R gold top-1s
MIN_DISCORDANT = 6    # falsifier (1): at least this many one-way discordant queries


@dataclass
class Question:
    qid: str
    cls: str            # "F" | "R"
    provenance: str     # "S" | "N"
    rdr: str            # predecessor id
    successor: str      # "" when none
    gold: str
    t: str
    question: str


@dataclass
class Retrieval:
    qid: str
    cls: str
    provenance: str
    rdr: str
    successor: str
    gold: str
    t: str
    latency_ms: int
    n_docs: int
    n_removed_unclassified: int
    excluded_short: bool
    ranked: list[dict[str, Any]]
    top1: dict[str, str] = field(default_factory=dict)
    top1_valid: dict[str, bool] = field(default_factory=dict)
    gold_rank: dict[str, int | None] = field(default_factory=dict)
    random_valid_expected_acc: float = 0.0


# ── population ───────────────────────────────────────────────────────────────


def _population(ws: dict[str, v2.Window]) -> list[v2.Window]:
    """Invalid RDRs whose invalidity date is in frontmatter, with a non-empty window."""
    return sorted(
        (w for w in ws.values() if w.status in v2.INVALID_STATUSES and w.invalid_src == "frontmatter" and not w.empty_window),
        key=lambda w: w.id,
    )


def _f_time(pred: v2.Window, succ: v2.Window | None) -> str:
    end = pred.invalid_at or ""
    if succ is not None and succ.created_at and succ.created_at < end:
        end = succ.created_at
    return v2._midpoint(pred.created_at, end)


def cmd_windows(args: argparse.Namespace) -> None:
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    ws = v2.build_windows()
    (out / "windows.json").write_text(json.dumps([asdict(w) for w in ws], indent=1))
    by_id = {w.id: w for w in ws}
    pop = _population(by_id)
    print(f"{len(ws)} files ({sum(w.classified for w in ws)} classified); population {len(pop)} frontmatter-dated invalid RDRs")  # noqa: T201
    for p in pop:
        s = by_id.get(p.superseded_by) if p.superseded_by else None
        print(f"  {p.id:9} {p.status:10} {p.created_at}..{p.invalid_at} F-t={_f_time(p, s)} successor={p.superseded_by or '-'}{'' if s is None else ' created ' + s.created_at}")  # noqa: T201


# ── question generation ──────────────────────────────────────────────────────


def _gen_prompt(cls: str, t: str, provenance: str, pred: v2.Window, n: int) -> str:
    if cls == "F":
        era = f"It is {t}. Ask as a developer on that day who wants the guidance in force on that day."
    else:
        era = (
            f"It is {t}. The design record below was later dropped or replaced. Ask as a developer today who wants "
            f"to understand that PAST decision itself: what it proposed, why it was set aside, what it was replaced by."
        )
    if provenance == "S":
        material = f"Design record titled: {pred.title}\nProblem statement: {pred.problem}"
    else:
        material = (
            f"A design record exists titled: {pred.title}. "
            f"Describe its topic in your own words before writing the questions; use none of the title's wording."
        )
    return (
        f"You write search queries for a developer using the nexus knowledge base.\n{material}\n\n{era}\n"
        f"Write {n} distinct natural-language search questions. Rules: do not quote any phrase longer than three "
        f"words from the text above; do not mention any RDR number; each question stands alone; 8 to 20 words each."
    )


async def cmd_gen(args: argparse.Namespace) -> None:
    random.seed(SEED)
    out = Path(args.out)
    ws = {w.id: w for w in (v2.Window(**d) for d in json.loads((out / "windows.json").read_text()))}
    questions: list[Question] = []
    cost = 0.0
    calls = 0
    for pred in _population(ws):
        succ = ws.get(pred.superseded_by) if pred.superseded_by else None
        for cls, t in (("F", _f_time(pred, succ)), ("R", TODAY)):
            for prov in ("S", "N"):
                sink: list = []
                res = await v2._dispatch(_gen_prompt(cls, t, prov, pred, PER_PROVENANCE), v2.GEN_SCHEMA, GEN_MODEL, sink)
                calls += 1
                cost += (getattr(sink[-1], "cost_usd", 0.0) or 0.0) if sink else 0.0
                for i, q in enumerate(res.get("questions", [])[:PER_PROVENANCE]):
                    questions.append(Question(
                        qid=f"{pred.id}-{cls}-{prov}{i+1}", cls=cls, provenance=prov, rdr=pred.id,
                        successor=succ.id if succ else "", gold=pred.id, t=t, question=q.strip(),
                    ))
                print(f"gen {pred.id} {cls} {prov}: {len(res.get('questions', []))}", file=sys.stderr)  # noqa: T201
    (out / "questions.json").write_text(json.dumps([asdict(q) for q in questions], indent=1))
    (out / "gen_meta.json").write_text(json.dumps({
        "seed": SEED, "gen_model": GEN_MODEL, "generated_at": datetime.now(UTC).isoformat(),
        "questions": len(questions), "dispatch_calls": calls, "gen_cost_usd": cost,
    }, indent=1))
    print(f"{len(questions)} questions in {calls} calls (${cost:.3f}) -> {out / 'questions.json'}")  # noqa: T201


# ── arms ─────────────────────────────────────────────────────────────────────


def _edges(ws: dict[str, v2.Window]) -> set[tuple[str, str]]:
    """(predecessor, successor) pairs from frontmatter, both directions looked up by the caller."""
    return {(w.id, w.superseded_by) for w in ws.values() if w.superseded_by and w.superseded_by in ws}


def arm_d(ranked: list[dict], edges: set[tuple[str, str]]) -> list[dict]:
    """Edge-only demotion: for each supersedes edge with both endpoints retrieved, the endpoint
    invalid at t goes directly below the other. Nothing else moves. Processed in edge order,
    stable with respect to arm A."""
    order = list(ranked)
    pos = {r["id"]: r for r in order if r["id"]}
    for a, b in sorted(edges):
        if a not in pos or b not in pos:
            continue
        ra, rb = pos[a], pos[b]
        if ra["valid_at_t"] == rb["valid_at_t"]:
            continue
        invalid, valid = (ra, rb) if not ra["valid_at_t"] else (rb, ra)
        if order.index(invalid) < order.index(valid):
            order.remove(invalid)
            order.insert(order.index(valid) + 1, invalid)
    return order


def _arm_orders(ranked: list[dict], edges: set[tuple[str, str]]) -> dict[str, list[dict]]:
    a = list(ranked)
    h = sorted(a, key=lambda r: (0 if r["valid_at_t"] else 1, r["rank"]))
    dists = [r["distance"] for r in a]
    lo, hi = (min(dists), max(dists)) if dists else (0.0, 1.0)
    def sim(r: dict) -> float:
        return 1.0 if hi == lo else 1.0 - (r["distance"] - lo) / (hi - lo)
    s = sorted(a, key=lambda r: -(SOFT_SIM_WEIGHT * sim(r) + SOFT_VALID_WEIGHT * (1.0 if r["valid_at_t"] else SOFT_INVALID_FLOOR)))
    return {"A": a, "H": h, "S42": s, "D": arm_d(a, edges)}


def cmd_run(args: argparse.Namespace) -> None:
    out = Path(args.out)
    ws = {w.id: w for w in (v2.Window(**d) for d in json.loads((out / "windows.json").read_text()))}
    by_file = {w.file: w for w in ws.values()}
    edges = _edges(ws)
    qs = [Question(**d) for d in json.loads((out / "questions.json").read_text())]
    collection = v2._rdr_collection()
    path = out / "retrievals.jsonl"
    done = {json.loads(l)["qid"] for l in path.read_text().splitlines() if l.strip()} if path.exists() else set()
    fh = path.open("a")
    for q in qs:
        if q.qid in done:
            continue
        t = date.fromisoformat(q.t)
        full, latency_ms = v2._ranked_docs(q.question, collection, by_file)
        ranked = [r for r in full if r["classified"]]
        for i, r in enumerate(ranked, 1):
            r["rank"] = i
            r["valid_at_t"] = v2.valid_at(ws.get(r["id"]), t)
        arms = _arm_orders(ranked, edges)
        rec = Retrieval(
            qid=q.qid, cls=q.cls, provenance=q.provenance, rdr=q.rdr, successor=q.successor, gold=q.gold, t=q.t,
            latency_ms=latency_ms, n_docs=len(ranked), n_removed_unclassified=len(full) - len(ranked),
            excluded_short=len(ranked) < v2.MIN_DOCS, ranked=ranked,
        )
        for arm, order in arms.items():
            rec.top1[arm] = order[0]["id"] if order else ""
            rec.top1_valid[arm] = bool(order[0]["valid_at_t"]) if order else True
            rec.gold_rank[arm] = next((i for i, r in enumerate(order, 1) if r["id"] == q.gold), None)
        valid_ids = [r["id"] for r in ranked if r["valid_at_t"]]
        rec.random_valid_expected_acc = (sum(1 for i in valid_ids if i == q.gold) / len(valid_ids)) if valid_ids else 0.0
        fh.write(json.dumps(asdict(rec)) + "\n"); fh.flush()
        print(f"{q.qid:16} {q.cls} {q.provenance} docs={len(ranked):2} " + " ".join(f"{a}={rec.top1[a] or '-'}{'' if rec.top1_valid[a] else '*'}" for a in rec.top1) + f" gold={q.gold} " + " ".join(f"{a}:{rec.gold_rank[a]}" for a in rec.gold_rank), file=sys.stderr)  # noqa: T201
    fh.close()
    (out / "run_meta.json").write_text(json.dumps({
        "collection": collection, "top_k_chunks": v2.TOP_K_CHUNKS, "min_docs": v2.MIN_DOCS, "ran_at": datetime.now(UTC).isoformat(),
        "queries": len(qs), "soft": {"sim": SOFT_SIM_WEIGHT, "valid": SOFT_VALID_WEIGHT, "floor": SOFT_INVALID_FLOOR},
        "edges": sorted(edges), "today": TODAY,
    }, indent=1))
    print(f"retrievals -> {path}")  # noqa: T201


# ── report ───────────────────────────────────────────────────────────────────


ARMS = ("A", "H", "S42", "D")


def _pooled(cls_f: list[dict], cls_r: list[dict], arm: str, mix_f: float, metric) -> float:
    """Mix-weighted per-query mean of a metric (top-1 indicator or reciprocal rank)."""
    mf = metric(cls_f, arm) / len(cls_f) if cls_f else 0.0
    mr = metric(cls_r, arm) / len(cls_r) if cls_r else 0.0
    return mix_f * mf + (1 - mix_f) * mr


def _rr_sum(rs: list[dict], arm: str) -> float:
    return sum(1 / r["gold_rank"][arm] for r in rs if r["gold_rank"].get(arm))


def _breakeven(cls_f: list[dict], cls_r: list[dict], arm: str) -> float | None:
    """Mix fraction of F at which the arm's pooled gold top-1 equals A's; None when it never crosses."""
    af = v2._acc(cls_f, "A") / len(cls_f); ar = v2._acc(cls_r, "A") / len(cls_r)
    xf = v2._acc(cls_f, arm) / len(cls_f); xr = v2._acc(cls_r, arm) / len(cls_r)
    df, dr = xf - af, xr - ar
    if df == dr:
        return None
    m = -dr / (df - dr)
    return m if 0.0 <= m <= 1.0 else None


def cmd_report(args: argparse.Namespace) -> None:
    out = Path(args.out)
    all_recs = [json.loads(l) for l in (out / "retrievals.jsonl").read_text().splitlines() if l.strip()]
    recs = [r for r in all_recs if not r["excluded_short"]]
    slots = sum(r["n_docs"] + r["n_removed_unclassified"] for r in all_recs)
    lines: list[str] = [
        f"# temporal validity A/B v3 -- {len(all_recs)} queries, {len(all_recs) - len(recs)} excluded for fewer than {v2.MIN_DOCS} documents",
        f"document depth per query after removal: median {sorted(r['n_docs'] for r in all_recs)[len(all_recs)//2]}, min {min(r['n_docs'] for r in all_recs)}, max {max(r['n_docs'] for r in all_recs)}",
        f"unclassified candidate slots removed before ranking: {sum(r['n_removed_unclassified'] for r in all_recs)} of {slots}",
        "",
    ]
    def table(name: str, rs: list[dict]) -> None:
        n = len(rs)
        lines.append(f"## {name} (n={n})")
        lines.append("arm | gold top-1 | MRR | stale top-1")
        for a in ARMS:
            k = v2._acc(rs, a); p, lo, hi = v2._wilson(k, n)
            lines.append(f"{a} | {k}/{n} = {p:.3f} [{lo:.3f}, {hi:.3f}] | {v2._mrr(rs, a):.3f} | {v2._stale(rs, a)}/{n}")
        lines.append(f"random-valid replacement baseline (expected gold top-1): {sum(r['random_valid_expected_acc'] for r in rs):.2f}/{n}")
        for x, y in (("A", "H"), ("A", "S42"), ("A", "D"), ("H", "S42"), ("H", "D")):
            if rs:
                xr, yr = v2._discordant(rs, x, y)
                lines.append(f"{x} vs {y}: {x} right only {xr}, {y} right only {yr}, exact McNemar p = {v2._mcnemar_exact(xr, yr):.2e}")
        lines.append("")
    F = [r for r in recs if r["cls"] == "F"]; R = [r for r in recs if r["cls"] == "R"]
    table("class F, in force (gold valid at t)", F)
    for prov in ("S", "N"):
        table(f"class F, provenance {prov}", [r for r in F if r["provenance"] == prov])
    table("class R, retrospective (gold invalid at t)", R)
    for prov in ("S", "N"):
        table(f"class R, provenance {prov}", [r for r in R if r["provenance"] == prov])
    table(f"class R, RDRs with a successor", [r for r in R if r["successor"]])
    table(f"class R, RDRs without a successor", [r for r in R if not r["successor"]])
    lines.append(f"## pooled at the pre-registered mix (F {MIX_F:.2f} / R {1-MIX_F:.2f}), per-query means")
    lines.append("arm | gold top-1 | MRR | breakeven F fraction vs A")
    for a in ARMS:
        be = _breakeven(F, R, a) if F and R else None
        lines.append(f"{a} | {_pooled(F, R, a, MIX_F, v2._acc):.3f} | {_pooled(F, R, a, MIX_F, _rr_sum):.3f} | {'-' if be is None else f'{be:.3f}'}")
    lines.append("")
    # falsifier v3
    xr, yr = v2._discordant(F, "A", "H"); p1 = v2._mcnemar_exact(xr, yr)
    ok1 = yr > xr and p1 < 0.05 and (xr + yr) >= MIN_DISCORDANT
    a_r = v2._acc(R, "A"); d_r_kept = sum(1 for r in R if r["gold_rank"].get("A") == 1 and r["gold_rank"].get("D") == 1)
    h_r_kept = sum(1 for r in R if r["gold_rank"].get("A") == 1 and r["gold_rank"].get("H") == 1)
    ok2 = a_r > 0 and d_r_kept >= KEEP_FRACTION * a_r
    def ok3(arm: str) -> bool:
        return _pooled(F, R, arm, MIX_F, v2._acc) >= _pooled(F, R, "A", MIX_F, v2._acc) and _pooled(F, R, arm, MIX_F, _rr_sum) >= _pooled(F, R, "A", MIX_F, _rr_sum)
    lines.append("## falsifier v3")
    lines.append(f"(1) class F, H beats A on gold top-1 (A right only {xr}, H right only {yr}, p = {p1:.2e}, one-way discordant {xr+yr} >= {MIN_DISCORDANT}): {'PASS' if ok1 else 'FAIL'}")
    lines.append(f"(2) class R, D keeps >= {KEEP_FRACTION:.2f} of A's gold top-1s (A {a_r}, D keeps {d_r_kept}; H keeps {h_r_kept}): {'PASS' if ok2 else 'FAIL'}")
    lines.append(f"(3) pooled at {MIX_F:.2f}/{1-MIX_F:.2f}, arm >= A on gold top-1 and MRR: H {'PASS' if ok3('H') else 'FAIL'}, S42 {'PASS' if ok3('S42') else 'FAIL'}, D {'PASS' if ok3('D') else 'FAIL'}")
    if ok1 and ok2 and ok3("D"):
        verdict = "H passes (1), D passes (2) and (3): the edge-aware shape D is the supported client-side re-rank; H is not."
    elif ok1 and not ok2:
        verdict = "H passes (1), D fails (2): no validity re-rank ships without a query-time intent signal."
    elif not ok1:
        verdict = "H fails (1): the signal is absent on this corpus."
    else:
        verdict = "H passes (1), D passes (2) but fails (3): D does not beat A pooled; no re-rank is supported."
    lines.append(f"verdict: {verdict}")
    lines.append("in every outcome: no engine retrieval parameter is licensed (shared candidate set, recall unmeasured)")
    lines.append("")
    rdrs = sorted({r["rdr"] for r in recs})
    for arm in ("H", "D"):
        lines.append(f"## leave-one-RDR-out: {arm} minus A gold top-1, class F | class R")
        lines.append(", ".join(
            f"{x}: {(v2._acc([r for r in F if r['rdr'] != x], arm) - v2._acc([r for r in F if r['rdr'] != x], 'A'))}|{(v2._acc([r for r in R if r['rdr'] != x], arm) - v2._acc([r for r in R if r['rdr'] != x], 'A'))}"
            for x in rdrs))
        lines.append("")
    lines.append("## per-query top-1 by arm (* = invalid at t) and gold ranks")
    for r in all_recs:
        flag = " EXCLUDED(short)" if r["excluded_short"] else ""
        lines.append(f"{r['qid']:16} {r['cls']} {r['provenance']} t={r['t']} docs={r['n_docs']:2} " + " ".join(f"{a}={r['top1'][a] or '-'}{'' if r['top1_valid'][a] else '*'}" for a in r["top1"]) + f" gold={r['gold']} ranks " + " ".join(f"{a}:{r['gold_rank'][a]}" for a in r["gold_rank"]) + flag)
    report = "\n".join(lines)
    (out / "report.md").write_text(report)
    print(report)  # noqa: T201


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["windows", "gen", "run", "report"])
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    args = ap.parse_args()
    if args.cmd == "windows":
        cmd_windows(args)
    elif args.cmd == "gen":
        asyncio.run(cmd_gen(args))
    elif args.cmd == "run":
        cmd_run(args)
    else:
        cmd_report(args)


if __name__ == "__main__":
    main()
