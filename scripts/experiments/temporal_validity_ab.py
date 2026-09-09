# SPDX-License-Identifier: AGPL-3.0-or-later
"""A/B: does a temporal-validity signal cut the stale-answer rate on the
rdr corpus?

Pre-registration: T2 ``nexus/preregistration-temporal-validity-ab-2026-09-08``
(bead nexus-wy6t5). Two arms share ONE retrieval per question; only the
document sort key differs:

* ``A``: nexus ranking as is (top-30 chunks, documents by first appearance).
* ``B``: the same candidates re-sorted with validity-at-reference-time as
  the primary key (valid first) and the original rank as the second key.

Validity comes from the RDR files themselves: ``created_at`` is the git
commit that added the file, ``invalid_at`` the commit that set status to
``superseded`` or ``abandoned``; every other status is valid from creation.
Reference time ``t`` is now for a current question and the midpoint of the
predecessor's window for an as-of question. Everything read is read-only
against the live store; the only writes are files under ``--out``.

Usage (from the checkout)::

    uv run python scripts/experiments/temporal_validity_ab.py windows
    uv run python scripts/experiments/temporal_validity_ab.py gen
    uv run python scripts/experiments/temporal_validity_ab.py run
    uv run python scripts/experiments/temporal_validity_ab.py report
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
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
RDR_DIR = REPO / "docs" / "rdr"
OUT_DEFAULT = REPO / "scripts" / "experiments" / "out" / "temporal_validity_ab_2026-09-08"
SEED = 20260908
GEN_MODEL = "sonnet"
TOP_K = 30
INVALID_STATUSES = frozenset({"superseded", "abandoned"})
CURRENT_PER_RDR = 3
ASOF_PER_PAIR = 2


# ── data shapes ──────────────────────────────────────────────────────────────


@dataclass
class Window:
    rdr: str            # "RDR-014"
    file: str           # "docs/rdr/rdr-014-....md"
    title: str
    status: str
    created_at: str     # ISO date
    invalid_at: str | None
    superseded_by: str  # "RDR-015" or ""
    problem: str        # first ~1200 chars of the Problem section


@dataclass
class Question:
    qid: str
    kind: str           # "current" | "asof"
    rdr: str            # the RDR the question was generated from
    gold: str           # RDR id the right top-1 should be, or ""
    t: str              # reference date, ISO
    question: str


@dataclass
class Retrieval:
    qid: str
    kind: str
    t: str
    gold: str
    latency_ms: int
    ranked: list[dict[str, Any]]   # [{tumbler, rdr, file, valid_at_t, rank}] in arm A order
    a_top1: str
    b_top1: str
    a_top1_valid: bool
    b_top1_valid: bool
    a_gold_rank: int | None
    b_gold_rank: int | None


# ── windows (local, no store access) ─────────────────────────────────────────


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True).stdout.strip()


def _frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    try:
        return yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}


def _problem_section(text: str) -> str:
    m = re.search(r"^##\s+Problem[^\n]*\n(.*?)(?=^##\s|\Z)", text, re.S | re.M)
    body = m.group(1) if m else text.split("\n---", 2)[-1]
    return re.sub(r"\s+", " ", body).strip()[:1200]


def _rdr_ids(v: Any) -> list[str]:
    return re.findall(r"RDR-\d{3}", str(v)) if v is not None else []


def build_windows() -> list[Window]:
    out: list[Window] = []
    for f in sorted(RDR_DIR.glob("rdr-*.md")):
        text = f.read_text(errors="ignore")
        fm = _frontmatter(text)
        num = re.match(r"rdr-(\d+)", f.name)
        if not num:
            continue
        rid = f"RDR-{int(num.group(1)):03d}"
        # Two files can carry one number (docs/rdr has two 049s); keep both
        # under distinct ids so neither window overwrites the other.
        seen = {w.rdr for w in out}
        if rid in seen:
            rid = rid + "b"
        rel = str(f.relative_to(REPO))
        status = str(fm.get("status") or "")
        added = _git("log", "--diff-filter=A", "--format=%cs", "--", rel).splitlines()
        created = added[-1] if added else _git("log", "--format=%cs", "--", rel).splitlines()[-1:] or [""]
        created_at = created if isinstance(created, str) else created[0]
        invalid_at: str | None = None
        if status in INVALID_STATUSES:
            flips = _git("log", "-S", f"status: {status}", "--format=%cs", "--", rel).splitlines()
            invalid_at = flips[-1] if flips else created_at
        sup = _rdr_ids(fm.get("superseded_by") or fm.get("superseded-by"))
        out.append(Window(
            rdr=rid, file=rel, title=str(fm.get("title") or f.stem), status=status,
            created_at=created_at, invalid_at=invalid_at,
            superseded_by=sup[0] if sup else "",
            problem=_problem_section(text),
        ))
    # A successor may name its predecessor only on its own side
    # (``supersedes: [RDR-112]`` on RDR-120, while RDR-112 is ``abandoned``
    # with no ``superseded_by``); fold those in so every pair is seen.
    by_id = {w.rdr: w for w in out}
    for f in sorted(RDR_DIR.glob("rdr-*.md")):
        fm = _frontmatter(f.read_text(errors="ignore"))
        num = re.match(r"rdr-(\d+)", f.name)
        if not num:
            continue
        successor = f"RDR-{int(num.group(1)):03d}"
        for pred in _rdr_ids(fm.get("supersedes")):
            w = by_id.get(pred)
            if w is not None and not w.superseded_by and w.status in INVALID_STATUSES:
                w.superseded_by = successor
    return out


def valid_at(w: Window | None, t: date) -> bool:
    """Unknown documents (no window) count as valid: the signal never
    demotes what it cannot classify."""
    if w is None or not w.created_at:
        return True
    if date.fromisoformat(w.created_at) > t:
        return False
    if w.invalid_at and date.fromisoformat(w.invalid_at) <= t:
        return False
    return True


def cmd_windows(args: argparse.Namespace) -> None:
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    ws = build_windows()
    (out / "windows.json").write_text(json.dumps([asdict(w) for w in ws], indent=1))
    invalid = [w for w in ws if w.status in INVALID_STATUSES]
    pairs = [(w.superseded_by, w.rdr) for w in ws if w.superseded_by]
    print(f"{len(ws)} RDRs, {len(invalid)} invalid, {len(pairs)} supersession pairs -> {out / 'windows.json'}")  # noqa: T201 — experiment script


# ── question generation ──────────────────────────────────────────────────────


GEN_SCHEMA = {
    "type": "object",
    "properties": {"questions": {"type": "array", "items": {"type": "string"}}},
    "required": ["questions"],
}


def _gen_prompt(w: Window, n: int, kind: str, t: str) -> str:
    era = (
        f"It is {t}. Ask as a developer on that day who wants the guidance in force then."
        if kind == "asof"
        else f"It is {t}. Ask as a developer today who wants the CURRENT guidance on this topic."
    )
    return (
        f"You write search queries for a developer using the nexus knowledge base.\n"
        f"Topic, from a design record titled: {w.title}\n"
        f"Problem statement: {w.problem}\n\n"
        f"{era}\n"
        f"Write {n} distinct natural-language search questions about this topic. Rules: do not "
        f"quote any phrase longer than three words from the text above; do not mention any "
        f"RDR number; each question stands alone; 8 to 20 words each."
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
    ws = {w.rdr: w for w in (Window(**d) for d in json.loads((out / "windows.json").read_text()))}
    today = date.today().isoformat()
    questions: list[Question] = []
    qpath = out / "questions.json"
    if qpath.exists():
        questions = [Question(**d) for d in json.loads(qpath.read_text())]
    have = {(q.rdr, q.kind) for q in questions}
    cost_total = 0.0
    invalid = [w for w in ws.values() if w.status in INVALID_STATUSES]
    for w in invalid:
        if (w.rdr, "current") in have:
            continue
        sink: list = []
        res = await _dispatch(_gen_prompt(w, CURRENT_PER_RDR, "current", today), GEN_SCHEMA, GEN_MODEL, sink)
        cost_total += getattr(sink[-1], "cost_usd", 0.0) or 0.0 if sink else 0.0
        for i, q in enumerate(res.get("questions", [])[:CURRENT_PER_RDR]):
            questions.append(Question(
                qid=f"{w.rdr}-cur-{i+1}", kind="current", rdr=w.rdr,
                gold=w.superseded_by, t=today, question=q.strip(),
            ))
        print(f"gen current {w.rdr}: {len(res.get('questions', []))}", file=sys.stderr)  # noqa: T201
    for w in invalid:
        if not w.superseded_by or not w.invalid_at or (w.rdr, "asof") in have:
            continue
        t = _midpoint(w.created_at, w.invalid_at)
        sink = []
        res = await _dispatch(_gen_prompt(w, ASOF_PER_PAIR, "asof", t), GEN_SCHEMA, GEN_MODEL, sink)
        cost_total += getattr(sink[-1], "cost_usd", 0.0) or 0.0 if sink else 0.0
        for i, q in enumerate(res.get("questions", [])[:ASOF_PER_PAIR]):
            questions.append(Question(
                qid=f"{w.rdr}-asof-{i+1}", kind="asof", rdr=w.rdr, gold=w.rdr, t=t, question=q.strip(),
            ))
        print(f"gen asof {w.rdr} (t={t}): {len(res.get('questions', []))}", file=sys.stderr)  # noqa: T201
    (out / "questions.json").write_text(json.dumps([asdict(q) for q in questions], indent=1))
    (out / "gen_meta.json").write_text(json.dumps({
        "seed": SEED, "gen_model": GEN_MODEL, "generated_at": datetime.now(UTC).isoformat(),
        "questions": len(questions), "gen_cost_usd": cost_total,
    }, indent=1))
    print(f"{len(questions)} questions -> {out / 'questions.json'} (gen cost ${cost_total:.3f})")  # noqa: T201


# ── retrieval (one per question, shared by both arms) ────────────────────────


def _rdr_collection() -> str:
    from nexus.db import make_t3  # noqa: PLC0415 — experiment script; deferred import

    names = [c["name"] for c in make_t3().list_collections() if c["name"].startswith("rdr__")]
    if not names:
        raise SystemExit("no rdr__ collection in the store; index the repo first")
    if len(names) > 1:
        print(f"several rdr collections, using the first: {names}", file=sys.stderr)  # noqa: T201
    return sorted(names)[0]


def _ranked_docs(query: str, collection: str, windows_by_file: dict[str, Window]) -> tuple[list[dict], int]:
    from nexus.mcp import core as mcp_core  # noqa: PLC0415 — experiment script; deferred import
    from nexus.mcp_infra import get_catalog  # noqa: PLC0415 — experiment script; deferred import

    t0 = time.monotonic()
    res = mcp_core._search_render(query=query, corpus=collection, limit=TOP_K, offset=0, structured=True)
    latency_ms = int((time.monotonic() - t0) * 1000)
    if not isinstance(res, dict):
        raise RuntimeError(f"search returned text, not a structured result: {str(res)[:200]}")
    chashes = [c for c in (res.get("chunk_text_hash") or []) if c]
    cat = get_catalog()
    if cat is None:
        raise RuntimeError("catalog unavailable; the experiment needs it to map chunks to documents")
    by_chash = cat.docs_for_chashes(chashes) if chashes else {}
    order: list[str] = []
    for c in chashes:
        for doc_id in by_chash.get(c, []) or []:
            if doc_id not in order:
                order.append(doc_id)
    entries = cat.resolve_many(order) if order else {}
    ranked: list[dict] = []
    for rank, doc_id in enumerate(order, 1):
        e = entries.get(doc_id)
        fp = getattr(e, "file_path", "") if e else ""
        rel = fp if fp.startswith("docs/") else next((k for k in windows_by_file if fp.endswith("/" + k) or fp == k), fp)
        w = windows_by_file.get(rel)
        ranked.append({"tumbler": doc_id, "file": rel, "rdr": w.rdr if w else "", "rank": rank})
    return ranked, latency_ms


def _arm_b(ranked: list[dict], windows: dict[str, Window], t: date) -> list[dict]:
    keyed = []
    for r in ranked:
        w = windows.get(r["rdr"]) if r["rdr"] else None
        keyed.append((0 if valid_at(w, t) else 1, r["rank"], r))
    return [r for _, _, r in sorted(keyed, key=lambda x: (x[0], x[1]))]


def cmd_run(args: argparse.Namespace) -> None:
    out = Path(args.out)
    windows = {w.rdr: w for w in (Window(**d) for d in json.loads((out / "windows.json").read_text()))}
    by_file = {w.file: w for w in windows.values()}
    qs = [Question(**d) for d in json.loads((out / "questions.json").read_text())]
    collection = _rdr_collection()
    results_path = out / "retrievals.jsonl"
    done = set()
    if results_path.exists():
        done = {json.loads(l)["qid"] for l in results_path.read_text().splitlines() if l.strip()}
    fh = results_path.open("a")
    for q in qs:
        if q.qid in done:
            continue
        t = date.fromisoformat(q.t)
        ranked, latency_ms = _ranked_docs(q.question, collection, by_file)
        for r in ranked:
            r["valid_at_t"] = valid_at(windows.get(r["rdr"]), t) if r["rdr"] else True
        b = _arm_b(ranked, windows, t)
        a_top, b_top = (ranked[0] if ranked else None), (b[0] if b else None)
        def gold_rank(lst: list[dict]) -> int | None:
            for i, r in enumerate(lst, 1):
                if q.gold and r["rdr"] == q.gold:
                    return i
            return None
        rec = Retrieval(
            qid=q.qid, kind=q.kind, t=q.t, gold=q.gold, latency_ms=latency_ms, ranked=ranked,
            a_top1=a_top["rdr"] if a_top else "", b_top1=b_top["rdr"] if b_top else "",
            a_top1_valid=bool(a_top["valid_at_t"]) if a_top else True,
            b_top1_valid=bool(b_top["valid_at_t"]) if b_top else True,
            a_gold_rank=gold_rank(ranked), b_gold_rank=gold_rank(b),
        )
        fh.write(json.dumps(asdict(rec)) + "\n"); fh.flush()
        print(f"{q.qid:16} {q.kind:7} A={rec.a_top1 or '-':8} {'valid' if rec.a_top1_valid else 'STALE'}  B={rec.b_top1 or '-':8} {'valid' if rec.b_top1_valid else 'STALE'}  {latency_ms}ms", file=sys.stderr)  # noqa: T201
    fh.close()
    (out / "run_meta.json").write_text(json.dumps({
        "collection": collection, "top_k": TOP_K, "ran_at": datetime.now(UTC).isoformat(), "queries": len(qs),
    }, indent=1))
    print(f"retrievals -> {results_path}")  # noqa: T201


# ── report ───────────────────────────────────────────────────────────────────


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    if n == 0:
        return 0.0, 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, max(0.0, c - h), min(1.0, c + h)


def _mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact p for discordant counts b (A stale, B valid) and c (A valid, B stale)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def cmd_report(args: argparse.Namespace) -> None:
    out = Path(args.out)
    recs = [json.loads(l) for l in (out / "retrievals.jsonl").read_text().splitlines() if l.strip()]
    lines: list[str] = []
    def block(name: str, rs: list[dict]) -> None:
        n = len(rs)
        a_stale = sum(not r["a_top1_valid"] for r in rs)
        b_stale = sum(not r["b_top1_valid"] for r in rs)
        disc_b = sum((not r["a_top1_valid"]) and r["b_top1_valid"] for r in rs)
        disc_c = sum(r["a_top1_valid"] and (not r["b_top1_valid"]) for r in rs)
        pa, la, ha = _wilson(a_stale, n); pb, lb, hb = _wilson(b_stale, n)
        lines.append(f"## {name} (n={n})")
        lines.append(f"stale rate A: {a_stale}/{n} = {pa:.3f} [{la:.3f}, {ha:.3f}]")
        lines.append(f"stale rate B: {b_stale}/{n} = {pb:.3f} [{lb:.3f}, {hb:.3f}]")
        lines.append(f"discordant: A stale and B valid = {disc_b}; A valid and B stale = {disc_c}; exact McNemar p = {_mcnemar_exact(disc_b, disc_c):.4f}")
        gold = [r for r in rs if r["gold"]]
        if gold:
            a_acc = sum(r["a_gold_rank"] == 1 for r in gold); b_acc = sum(r["b_gold_rank"] == 1 for r in gold)
            a_mrr = sum(1 / r["a_gold_rank"] for r in gold if r["a_gold_rank"]) / len(gold)
            b_mrr = sum(1 / r["b_gold_rank"] for r in gold if r["b_gold_rank"]) / len(gold)
            found = sum(1 for r in gold if r["a_gold_rank"])
            lines.append(f"gold present in top-{TOP_K}: {found}/{len(gold)}; top-1 accuracy A {a_acc}/{len(gold)}, B {b_acc}/{len(gold)}; MRR A {a_mrr:.3f}, B {b_mrr:.3f}")
        lat = sorted(r["latency_ms"] for r in rs)
        if lat:
            lines.append(f"retrieval latency ms: median {lat[len(lat)//2]}, p90 {lat[int(len(lat)*0.9)-1 if len(lat) > 1 else 0]} (shared by both arms)")
        halved = b_stale * 2 <= a_stale
        lines.append(f"falsifier (B stale <= half of A, p < 0.05): {'PASSED' if halved and a_stale and _mcnemar_exact(disc_b, disc_c) < 0.05 else 'NOT PASSED'}")
        lines.append("")
    block("all queries", recs)
    block("current queries", [r for r in recs if r["kind"] == "current"])
    block("as-of queries", [r for r in recs if r["kind"] == "asof"])
    # Sensitivity cuts, decided after reading the raw rankings and recorded
    # as such: (1) queries whose whole top-30 is invalid at t, where no sort
    # key can help; (2) as-of questions on a predecessor whose window is
    # empty (created and superseded the same day); (3) questions sharing a
    # four-word run with their source text (generation-contract misses).
    windows = {w["rdr"]: w for w in json.loads((out / "windows.json").read_text())}
    qs = {q["qid"]: q for q in json.loads((out / "questions.json").read_text())}
    no_valid = {r["qid"] for r in recs if r["ranked"] and not any(d["valid_at_t"] for d in r["ranked"])}
    empty_window = {
        r["qid"] for r in recs
        if r["kind"] == "asof" and windows.get(r["gold"], {}).get("invalid_at") == windows.get(r["gold"], {}).get("created_at")
    }
    def _toks(x: str) -> list[str]:
        return re.findall(r"[a-z0-9]+", x.lower())
    leaky = set()
    for qid, q in qs.items():
        w = windows.get(q["rdr"]) or {}
        src = _toks(str(w.get("title", "")) + " " + str(w.get("problem", "")))
        grams = {tuple(src[i:i + 4]) for i in range(max(0, len(src) - 3))}
        qt = _toks(q["question"])
        if any(tuple(qt[i:i + 4]) in grams for i in range(max(0, len(qt) - 3))):
            leaky.add(qid)
    lines.append(f"## sensitivity cuts: no valid candidate in top-{TOP_K} = {sorted(no_valid)}; empty-window as-of = {sorted(empty_window)}; four-word-run leakage = {sorted(leaky)}")
    lines.append("")
    block("all queries minus the three cuts", [r for r in recs if r["qid"] not in (no_valid | empty_window | leaky)])
    block("as-of queries minus the empty-window pair", [r for r in recs if r["kind"] == "asof" and r["qid"] not in empty_window])
    # leave-one-RDR-out on the all-queries stale-rate difference
    rdrs = sorted({r["qid"].rsplit("-", 2)[0] for r in recs})
    diffs = []
    for rid in rdrs:
        rest = [r for r in recs if not r["qid"].startswith(rid + "-")]
        if rest:
            diffs.append((rid, (sum(not r["a_top1_valid"] for r in rest) - sum(not r["b_top1_valid"] for r in rest)) / len(rest)))
    if diffs:
        lines.append("## leave-one-RDR-out: A minus B stale-rate difference")
        lines.append(", ".join(f"{rid}: {d:+.3f}" for rid, d in diffs))
        lines.append(f"min {min(d for _, d in diffs):+.3f}, max {max(d for _, d in diffs):+.3f}")
        lines.append("")
    # where did B's top-1 come from
    lines.append("## per-query top-1 (A -> B)")
    for r in recs:
        lines.append(f"{r['qid']:16} {r['kind']:7} t={r['t']} A={r['a_top1'] or '-'}{'' if r['a_top1_valid'] else '*'} -> B={r['b_top1'] or '-'}{'' if r['b_top1_valid'] else '*'} gold={r['gold'] or '-'} goldrank A={r['a_gold_rank']} B={r['b_gold_rank']}")
    lines.append("(* = invalid at t)")
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
