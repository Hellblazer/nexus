# SPDX-License-Identifier: AGPL-3.0-or-later
"""Temporal validity A/B, version 2 (bead nexus-wy6t5).

Pre-registration: T2 ``nexus/preregistration-temporal-validity-ab-v2-2026-09-09``.
Version 1 (``temporal_validity_ab.py``) is kept for the record; its gate-7
critique (T2 ``nexus/critique-temporal-validity-ab-2026-09-08``) found the
instrument invalid, and this version is its fair rerun:

* windows from frontmatter first (created / date; scrapped_date /
  abandoned_date / superseded_date), git only as the fallback, and the git
  fallback walks the file's history for the first commit whose status left
  the valid set, never a vocabulary rename; an empty window is refused;
* every file under docs/rdr is classified, ids keyed by path;
* three question provenances (S predecessor, T successor, N titles only),
  supersession pairs only, so every question has a gold document;
* four arms over one shared retrieval: A as is, H hard validity sort,
  S soft validity score, E drop-own-source control (S provenance only);
* accuracy and MRR primary, stale rate secondary, as-of leg primary;
* the falsifier is stated on an outcome the treatment can lose.

Everything read is read-only against the live store; the only writes are
files under ``--out``.

Usage::

    uv run python scripts/experiments/temporal_validity_ab_v2.py windows
    uv run python scripts/experiments/temporal_validity_ab_v2.py gen
    uv run python scripts/experiments/temporal_validity_ab_v2.py run
    uv run python scripts/experiments/temporal_validity_ab_v2.py report
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
RDR_DIR = REPO / "docs" / "rdr"
OUT_DEFAULT = REPO / "scripts" / "experiments" / "out" / "temporal_validity_ab_v2_2026-09-09"
SEED = 20260909
GEN_MODEL = "sonnet"
TOP_K_CHUNKS = 60
MIN_DOCS = 5
INVALID_STATUSES = frozenset({"superseded", "abandoned", "scrapped"})
PER_PROVENANCE = 2
SOFT_SIM_WEIGHT = 0.75
SOFT_VALID_WEIGHT = 0.25
SOFT_INVALID_FLOOR = 0.05


@dataclass
class Window:
    id: str             # "RDR-014", "RDR-049b", or "PM:016-ast-chunk..." for a non-numbered file
    file: str
    title: str
    status: str
    created_at: str     # "" when unknown
    invalid_at: str | None
    created_src: str    # "frontmatter" | "git" | ""
    invalid_src: str    # "frontmatter" | "git" | ""
    classified: bool    # False for post-mortem / joint files with no lifecycle frontmatter
    empty_window: bool
    superseded_by: str
    problem: str


@dataclass
class Question:
    qid: str
    kind: str           # "current" | "asof"
    provenance: str     # "S" | "T" | "N"
    pair: str           # "RDR-015<RDR-014"
    source: str         # id of the document the question was generated from ("" for N)
    gold: str
    t: str
    question: str


@dataclass
class Retrieval:
    qid: str
    kind: str
    provenance: str
    pair: str
    source: str
    gold: str
    t: str
    latency_ms: int
    n_docs: int
    excluded_short: bool
    ranked: list[dict[str, Any]]      # arm A order: {id, file, rank, distance, valid_at_t, classified}
    top1: dict[str, str] = field(default_factory=dict)          # arm -> id
    top1_valid: dict[str, bool] = field(default_factory=dict)
    gold_rank: dict[str, int | None] = field(default_factory=dict)
    random_valid_expected_acc: float = 0.0


# ── windows ──────────────────────────────────────────────────────────────────


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True).stdout.strip()


def _frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}
    return fm if isinstance(fm, dict) else {}


def _problem_section(text: str) -> str:
    m = re.search(r"^##\s+Problem[^\n]*\n(.*?)(?=^##\s|\Z)", text, re.S | re.M)
    body = m.group(1) if m else text.split("\n---", 2)[-1]
    return re.sub(r"\s+", " ", body).strip()[:1200]


def _rdr_ids(v: Any) -> list[str]:
    return re.findall(r"RDR-\d{3}", str(v)) if v is not None else []


def _iso(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (date, datetime)):
        return v.date().isoformat() if isinstance(v, datetime) else v.isoformat()
    s = str(v).strip()
    return s if re.match(r"^\d{4}-\d{2}-\d{2}$", s) else ""


def _git_first_invalid_commit_date(rel: str) -> str:
    """Walk the file's history oldest to newest and return the date of the
    first commit whose frontmatter status is in INVALID_STATUSES."""
    log = _git("log", "--reverse", "--format=%H %cs", "--", rel).splitlines()
    for line in log:
        sha, day = line.split()
        blob = subprocess.run(["git", "show", f"{sha}:{rel}"], cwd=REPO, capture_output=True, text=True).stdout
        if str(_frontmatter(blob).get("status") or "") in INVALID_STATUSES:
            return day
    return ""


def build_windows() -> list[Window]:
    out: list[Window] = []
    seen_numbers: dict[str, int] = {}
    for f in sorted(RDR_DIR.rglob("*.md")):
        rel = str(f.relative_to(REPO))
        text = f.read_text(errors="ignore")
        fm = _frontmatter(text)
        num = re.match(r"rdr-(\d+)", f.name)
        if num and f.parent == RDR_DIR:
            base = f"RDR-{int(num.group(1)):03d}"
            n = seen_numbers.get(base, 0)
            seen_numbers[base] = n + 1
            wid = base if n == 0 else base + "abcdefgh"[n - 1]
        else:
            wid = f"{f.parent.name.upper()}:{f.stem}" if f.parent != RDR_DIR else f"FILE:{f.stem}"
        status = str(fm.get("status") or "")
        classified = bool(status) and f.parent == RDR_DIR
        created_at = _iso(fm.get("created")) or _iso(fm.get("date"))
        created_src = "frontmatter" if created_at else ""
        if not created_at:
            added = _git("log", "--diff-filter=A", "--format=%cs", "--", rel).splitlines()
            created_at = added[-1] if added else ""
            created_src = "git" if created_at else ""
        invalid_at: str | None = None
        invalid_src = ""
        if status in INVALID_STATUSES:
            invalid_at = _iso(fm.get("scrapped_date")) or _iso(fm.get("abandoned_date")) or _iso(fm.get("superseded_date"))
            invalid_src = "frontmatter" if invalid_at else ""
            if not invalid_at:
                invalid_at = _git_first_invalid_commit_date(rel) or None
                invalid_src = "git" if invalid_at else ""
        empty = bool(invalid_at and created_at and invalid_at <= created_at)
        sup = _rdr_ids(fm.get("superseded_by") or fm.get("superseded-by"))
        out.append(Window(
            id=wid, file=rel, title=str(fm.get("title") or f.stem), status=status,
            created_at=created_at, invalid_at=invalid_at, created_src=created_src,
            invalid_src=invalid_src, classified=classified, empty_window=empty,
            superseded_by=sup[0] if sup else "", problem=_problem_section(text),
        ))
    by_id = {w.id: w for w in out}
    for w in out:
        for pred in _rdr_ids(_frontmatter((REPO / w.file).read_text(errors="ignore")).get("supersedes")):
            p = by_id.get(pred)
            if p is not None and not p.superseded_by and p.status in INVALID_STATUSES:
                p.superseded_by = w.id
    return out


def valid_at(w: Window | None, t: date) -> bool:
    if w is None or not w.classified or not w.created_at:
        return True
    if date.fromisoformat(w.created_at) > t:
        return False
    if w.invalid_at and date.fromisoformat(w.invalid_at) <= t:
        return False
    return True


def _pairs(ws: dict[str, Window]) -> list[tuple[Window, Window]]:
    """(successor, predecessor) for every predecessor with a named successor."""
    out = []
    for w in ws.values():
        if w.status in INVALID_STATUSES and w.superseded_by and w.superseded_by in ws:
            out.append((ws[w.superseded_by], w))
    return sorted(out, key=lambda p: p[1].id)


def cmd_windows(args: argparse.Namespace) -> None:
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    ws = build_windows()
    (out / "windows.json").write_text(json.dumps([asdict(w) for w in ws], indent=1))
    by_id = {w.id: w for w in ws}
    invalid = [w for w in ws if w.status in INVALID_STATUSES]
    pairs = _pairs(by_id)
    empty = [w.id for w in invalid if w.empty_window]
    asof = [p for p in pairs if not p[1].empty_window]
    print(f"{len(ws)} files ({sum(w.classified for w in ws)} classified), {len(invalid)} invalid, "  # noqa: T201
          f"{len(pairs)} pairs, {len(asof)} as-of pairs; empty windows refused: {empty}")
    for s, p in pairs:
        print(f"  {s.id} supersedes {p.id}: window {p.created_at}..{p.invalid_at} ({p.created_src}/{p.invalid_src}){' EMPTY' if p.empty_window else ''}")  # noqa: T201


# ── question generation ──────────────────────────────────────────────────────


GEN_SCHEMA = {
    "type": "object",
    "properties": {"questions": {"type": "array", "items": {"type": "string"}}},
    "required": ["questions"],
}


def _gen_prompt(kind: str, t: str, provenance: str, pred: Window, succ: Window, n: int) -> str:
    era = (
        f"It is {t}. Ask as a developer on that day who wants the guidance in force then."
        if kind == "asof"
        else f"It is {t}. Ask as a developer today who wants the CURRENT guidance on this topic."
    )
    if provenance == "S":
        material = f"Design record titled: {pred.title}\nProblem statement: {pred.problem}"
    elif provenance == "T":
        material = f"Design record titled: {succ.title}\nProblem statement: {succ.problem}"
    else:
        material = (
            f"Two related design records exist on one topic, titled: {pred.title}; and: {succ.title}. "
            f"Describe the shared topic in your own words before writing the questions; use neither title's wording."
        )
    return (
        f"You write search queries for a developer using the nexus knowledge base.\n{material}\n\n{era}\n"
        f"Write {n} distinct natural-language search questions about this topic. Rules: do not quote any "
        f"phrase longer than three words from the text above; do not mention any RDR number; each question "
        f"stands alone; 8 to 20 words each."
    )


async def _dispatch(prompt: str, schema: dict, model: str, sink: list) -> dict:
    from nexus.operators.dispatch import claude_dispatch  # noqa: PLC0415 — experiment script; deferred import

    return await claude_dispatch(prompt, schema, timeout=240, model=model, usage_sink=sink)


def _midpoint(a: str, b: str) -> str:
    da, db = date.fromisoformat(a), date.fromisoformat(b)
    return (da + (db - da) / 2).isoformat()


async def cmd_gen(args: argparse.Namespace) -> None:
    random.seed(SEED)
    out = Path(args.out)
    ws = {w.id: w for w in (Window(**d) for d in json.loads((out / "windows.json").read_text()))}
    today = date.today().isoformat()
    questions: list[Question] = []
    cost = 0.0
    calls = 0
    for succ, pred in _pairs(ws):
        pair = f"{succ.id}<{pred.id}"
        plan = [("current", today, "S", pred.id, succ.id), ("current", today, "T", succ.id, succ.id), ("current", today, "N", "", succ.id)]
        if not pred.empty_window and pred.invalid_at:
            t = _midpoint(pred.created_at, pred.invalid_at)
            plan += [("asof", t, "S", pred.id, pred.id), ("asof", t, "N", "", pred.id)]
        for kind, t, prov, source, gold in plan:
            sink: list = []
            res = await _dispatch(_gen_prompt(kind, t, prov, pred, succ, PER_PROVENANCE), GEN_SCHEMA, GEN_MODEL, sink)
            calls += 1
            cost += (getattr(sink[-1], "cost_usd", 0.0) or 0.0) if sink else 0.0
            for i, q in enumerate(res.get("questions", [])[:PER_PROVENANCE]):
                questions.append(Question(
                    qid=f"{pred.id}-{kind}-{prov}{i+1}", kind=kind, provenance=prov, pair=pair,
                    source=source, gold=gold, t=t, question=q.strip(),
                ))
            print(f"gen {pair} {kind} {prov}: {len(res.get('questions', []))}", file=sys.stderr)  # noqa: T201
    (out / "questions.json").write_text(json.dumps([asdict(q) for q in questions], indent=1))
    (out / "gen_meta.json").write_text(json.dumps({
        "seed": SEED, "gen_model": GEN_MODEL, "generated_at": datetime.now(UTC).isoformat(),
        "questions": len(questions), "dispatch_calls": calls, "gen_cost_usd": cost,
    }, indent=1))
    print(f"{len(questions)} questions in {calls} calls (${cost:.3f}) -> {out / 'questions.json'}")  # noqa: T201


# ── retrieval and arms ───────────────────────────────────────────────────────


def _rdr_collection() -> str:
    from nexus.db import make_t3  # noqa: PLC0415 — experiment script; deferred import

    names = sorted(c["name"] for c in make_t3().list_collections() if c["name"].startswith("rdr__1-1__"))
    if not names:
        raise SystemExit("no rdr__1-1 collection in the store")
    return names[0]


def _ranked_docs(query: str, collection: str, by_file: dict[str, Window]) -> tuple[list[dict], int]:
    from nexus.mcp import core as mcp_core  # noqa: PLC0415 — experiment script; deferred import
    from nexus.mcp_infra import get_catalog  # noqa: PLC0415 — experiment script; deferred import

    t0 = time.monotonic()
    res = mcp_core._search_render(query=query, corpus=collection, limit=TOP_K_CHUNKS, offset=0, structured=True)
    latency_ms = int((time.monotonic() - t0) * 1000)
    if not isinstance(res, dict):
        raise RuntimeError(f"search returned text: {str(res)[:200]}")
    chashes = list(res.get("chunk_text_hash") or [])
    distances = list(res.get("distances") or [])
    cat = get_catalog()
    if cat is None:
        raise RuntimeError("catalog unavailable")
    by_chash = cat.docs_for_chashes([c for c in chashes if c]) if chashes else {}
    order: list[str] = []
    best_dist: dict[str, float] = {}
    for c, d in zip(chashes, distances):
        for doc_id in by_chash.get(c, []) or []:
            if doc_id not in order:
                order.append(doc_id)
                best_dist[doc_id] = float(d)
    entries = cat.resolve_many(order) if order else {}
    ranked: list[dict] = []
    for rank, doc_id in enumerate(order, 1):
        e = entries.get(doc_id)
        fp = getattr(e, "file_path", "") if e else ""
        rel = fp if fp.startswith("docs/") else next((k for k in by_file if fp.endswith("/" + k)), fp)
        w = by_file.get(rel)
        ranked.append({
            "tumbler": doc_id, "id": w.id if w else "", "file": rel, "rank": rank,
            "distance": best_dist[doc_id], "classified": bool(w and w.classified),
        })
    return ranked, latency_ms


def _arm_orders(ranked: list[dict], source: str) -> dict[str, list[dict]]:
    a = list(ranked)
    h = sorted(a, key=lambda r: (0 if r["valid_at_t"] else 1, r["rank"]))
    dists = [r["distance"] for r in a]
    lo, hi = (min(dists), max(dists)) if dists else (0.0, 1.0)
    def sim(r: dict) -> float:
        return 1.0 if hi == lo else 1.0 - (r["distance"] - lo) / (hi - lo)
    s = sorted(a, key=lambda r: -(SOFT_SIM_WEIGHT * sim(r) + SOFT_VALID_WEIGHT * (1.0 if r["valid_at_t"] else SOFT_INVALID_FLOOR)))
    arms = {"A": a, "H": h, "S": s}
    if source:
        arms["E"] = [r for r in a if r["id"] != source]
    return arms


def cmd_run(args: argparse.Namespace) -> None:
    out = Path(args.out)
    ws = {w.id: w for w in (Window(**d) for d in json.loads((out / "windows.json").read_text()))}
    by_file = {w.file: w for w in ws.values()}
    qs = [Question(**d) for d in json.loads((out / "questions.json").read_text())]
    collection = _rdr_collection()
    path = out / "retrievals.jsonl"
    done = {json.loads(l)["qid"] for l in path.read_text().splitlines() if l.strip()} if path.exists() else set()
    fh = path.open("a")
    for q in qs:
        if q.qid in done:
            continue
        t = date.fromisoformat(q.t)
        ranked, latency_ms = _ranked_docs(q.question, collection, by_file)
        for r in ranked:
            r["valid_at_t"] = valid_at(ws.get(r["id"]), t)
        arms = _arm_orders(ranked, q.source if q.provenance == "S" else "")
        rec = Retrieval(
            qid=q.qid, kind=q.kind, provenance=q.provenance, pair=q.pair, source=q.source, gold=q.gold, t=q.t,
            latency_ms=latency_ms, n_docs=len(ranked), excluded_short=len(ranked) < MIN_DOCS, ranked=ranked,
        )
        for arm, order in arms.items():
            rec.top1[arm] = order[0]["id"] if order else ""
            rec.top1_valid[arm] = bool(order[0]["valid_at_t"]) if order else True
            rec.gold_rank[arm] = next((i for i, r in enumerate(order, 1) if r["id"] == q.gold), None)
        valid_ids = [r["id"] for r in ranked if r["valid_at_t"]]
        rec.random_valid_expected_acc = (sum(1 for i in valid_ids if i == q.gold) / len(valid_ids)) if valid_ids else 0.0
        fh.write(json.dumps(asdict(rec)) + "\n"); fh.flush()
        print(f"{q.qid:22} {q.kind:7} {q.provenance} docs={len(ranked):2} " + " ".join(f"{a}={rec.top1[a] or '-'}{'' if rec.top1_valid[a] else '*'}" for a in rec.top1) + f" gold={q.gold} " + " ".join(f"{a}:{rec.gold_rank[a]}" for a in rec.gold_rank), file=sys.stderr)  # noqa: T201
    fh.close()
    (out / "run_meta.json").write_text(json.dumps({
        "collection": collection, "top_k_chunks": TOP_K_CHUNKS, "min_docs": MIN_DOCS, "ran_at": datetime.now(UTC).isoformat(),
        "queries": len(qs), "soft": {"sim": SOFT_SIM_WEIGHT, "valid": SOFT_VALID_WEIGHT, "floor": SOFT_INVALID_FLOOR},
    }, indent=1))
    print(f"retrievals -> {path}")  # noqa: T201


# ── report ───────────────────────────────────────────────────────────────────


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    if n == 0:
        return 0.0, 0.0, 0.0
    p = k / n; d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, max(0.0, c - h), min(1.0, c + h)


def _mcnemar_exact(b: int, c: int) -> float:
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(0, k + 1)) / 2 ** n)


def _acc(rs: list[dict], arm: str) -> int:
    return sum(1 for r in rs if r["gold_rank"].get(arm) == 1)


def _mrr(rs: list[dict], arm: str) -> float:
    return (sum(1 / r["gold_rank"][arm] for r in rs if r["gold_rank"].get(arm)) / len(rs)) if rs else 0.0


def _stale(rs: list[dict], arm: str) -> int:
    return sum(1 for r in rs if not r["top1_valid"].get(arm, True))


def _discordant(rs: list[dict], x: str, y: str) -> tuple[int, int]:
    """(x right and y wrong, x wrong and y right) on gold top-1."""
    xr = sum(1 for r in rs if r["gold_rank"].get(x) == 1 and r["gold_rank"].get(y) != 1)
    yr = sum(1 for r in rs if r["gold_rank"].get(x) != 1 and r["gold_rank"].get(y) == 1)
    return xr, yr


def cmd_report(args: argparse.Namespace) -> None:
    out = Path(args.out)
    all_recs = [json.loads(l) for l in (out / "retrievals.jsonl").read_text().splitlines() if l.strip()]
    recs = [r for r in all_recs if not r["excluded_short"]]
    lines: list[str] = [
        f"# temporal validity A/B v2 -- {len(all_recs)} queries, {len(all_recs) - len(recs)} excluded for fewer than {MIN_DOCS} documents",
        f"document depth per query: median {sorted(r['n_docs'] for r in all_recs)[len(all_recs)//2]}, min {min(r['n_docs'] for r in all_recs)}, max {max(r['n_docs'] for r in all_recs)}",
        f"unclassified candidate slots: {sum(1 for r in all_recs for d in r['ranked'] if not d['classified'])} of {sum(len(r['ranked']) for r in all_recs)}",
        "",
    ]
    def table(name: str, rs: list[dict], arms: tuple[str, ...]) -> None:
        n = len(rs)
        lines.append(f"## {name} (n={n})")
        lines.append("arm | gold top-1 | MRR | stale top-1")
        for a in arms:
            if not all(a in r["top1"] for r in rs):
                continue
            k = _acc(rs, a); p, lo, hi = _wilson(k, n)
            lines.append(f"{a} | {k}/{n} = {p:.3f} [{lo:.3f}, {hi:.3f}] | {_mrr(rs, a):.3f} | {_stale(rs, a)}/{n}")
        lines.append(f"random-valid replacement baseline (expected gold top-1): {sum(r['random_valid_expected_acc'] for r in rs):.2f}/{n}")
        for x, y in (("A", "S"), ("A", "H"), ("E", "S"), ("E", "H"), ("H", "S")):
            if all(x in r["top1"] and y in r["top1"] for r in rs) and rs:
                xr, yr = _discordant(rs, x, y)
                lines.append(f"{x} vs {y}: {x} right only {xr}, {y} right only {yr}, exact McNemar p = {_mcnemar_exact(xr, yr):.2e}")
        lines.append("")
    cur = [r for r in recs if r["kind"] == "current"]; aso = [r for r in recs if r["kind"] == "asof"]
    table("as-of leg (primary)", aso, ("A", "H", "S"))
    for prov in ("S", "N"):
        table(f"as-of leg, provenance {prov}", [r for r in aso if r["provenance"] == prov], ("A", "H", "S", "E"))
    table("current leg", cur, ("A", "H", "S"))
    for prov in ("S", "T", "N"):
        table(f"current leg, provenance {prov}", [r for r in cur if r["provenance"] == prov], ("A", "H", "S", "E"))
    # falsifier v2
    verdict: list[str] = []
    ok1 = all(_acc(aso, arm) >= _acc(aso, "A") - 1 for arm in ("S", "H")) if aso else False
    verdict.append(f"(1) as-of non-inferiority (S, H lose at most one gold top-1 to A): {'PASS' if ok1 else 'FAIL'} (A {_acc(aso,'A')}, H {_acc(aso,'H')}, S {_acc(aso,'S')} of {len(aso)})")
    curS = [r for r in cur if r["provenance"] == "S"]; curT = [r for r in cur if r["provenance"] == "T"]; curN = [r for r in cur if r["provenance"] == "N"]
    xr, yr = _discordant(cur, "A", "S"); pooled_p = _mcnemar_exact(xr, yr)
    ok2 = (_acc(curS, "S") > _acc(curS, "E")) and (_acc(curT, "S") > _acc(curT, "A")) and (_acc(curN, "S") > _acc(curN, "A")) and pooled_p < 0.05 and yr > xr
    verdict.append(f"(2) current leg: S > E on provenance S ({_acc(curS,'S')} vs {_acc(curS,'E')} of {len(curS)}), S > A on T ({_acc(curT,'S')} vs {_acc(curT,'A')} of {len(curT)}), S > A on N ({_acc(curN,'S')} vs {_acc(curN,'A')} of {len(curN)}), pooled S vs A McNemar p = {pooled_p:.2e}: {'PASS' if ok2 else 'FAIL'}")
    rand = sum(r["random_valid_expected_acc"] for r in recs)
    ok3 = _acc(recs, "S") > rand
    verdict.append(f"(3) S beats the random-valid replacement baseline on gold top-1: {_acc(recs,'S')} vs expected {rand:.2f} of {len(recs)}: {'PASS' if ok3 else 'FAIL'}")
    lines.append("## falsifier v2")
    lines.extend(verdict)
    lines.append(f"overall: {'PASSED' if ok1 and ok2 and ok3 else 'NOT PASSED'}")
    lines.append("")
    pairs = sorted({r["pair"] for r in recs})
    lines.append("## leave-one-pair-out: S minus A gold top-1 accuracy, all queries")
    lines.append(", ".join(f"{p}: {(_acc([r for r in recs if r['pair'] != p], 'S') - _acc([r for r in recs if r['pair'] != p], 'A')) / max(1, len([r for r in recs if r['pair'] != p])):+.3f}" for p in pairs))
    lines.append("")
    lines.append("## per-query top-1 by arm (* = invalid at t) and gold ranks")
    for r in all_recs:
        flag = " EXCLUDED(short)" if r["excluded_short"] else ""
        lines.append(f"{r['qid']:22} {r['kind']:7} {r['provenance']} t={r['t']} docs={r['n_docs']:2} " + " ".join(f"{a}={r['top1'][a] or '-'}{'' if r['top1_valid'][a] else '*'}" for a in r["top1"]) + f" gold={r['gold']} ranks " + " ".join(f"{a}:{r['gold_rank'][a]}" for a in r["gold_rank"]) + flag)
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
