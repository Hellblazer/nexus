# SPDX-License-Identifier: AGPL-3.0-or-later
"""A/B: aspect rows versus raw chunks as the reader payload.

Replicates, in nexus's own consumer shape, the experiment arXiv:2608.20845
("RAG Deserves an Index") reports as its central result and admits did not
survive a tool-using agent layer. Two arms answer the same questions with
the same reader model and prompt; only the payload differs:

* ``chunks``: top-k T3 chunks from the collection, packed to a token budget.
* ``aspects``: top documents (by chunk hits, grouped), each rendered from
  its ``document_aspects`` row, packed to the same budget.

Questions come from the raw paper text (pdftotext), not from either arm's
artifacts, so neither arm sees its own output at generation time. A grader
compares each answer to the gold answer and quote. Everything read is
read-only; the only writes are files under ``--out``.

Usage (from the checkout, read-only against the live store)::

    uv run python scripts/experiments/isc_payload_ab.py gen --papers 20 --per-paper 2
    uv run python scripts/experiments/isc_payload_ab.py run --budgets 2000,8000
    uv run python scripts/experiments/isc_payload_ab.py report
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

COLLECTION = "knowledge__dt-papers__voyage-context-3__v1"
CHARS_PER_TOKEN = 4  # the paper reports reader tokens; we approximate by chars/4
READER_MODEL = "haiku"
GRADER_MODEL = "haiku"
GEN_MODEL = "sonnet"
CONCURRENCY = 4
SEED = 20260907


# ── data shapes ──────────────────────────────────────────────────────────────


@dataclass
class Question:
    qid: str
    tumbler: str
    title: str
    kind: str  # "method_result" | "detail"
    question: str
    gold_answer: str
    supporting_quote: str


@dataclass
class Trial:
    qid: str
    arm: str
    budget: int
    payload_tokens: int
    docs_in_payload: list[str]
    gold_doc_in_payload: bool
    answer: str
    reader_cost_usd: float | None
    reader_ms: int | None
    correct: bool | None = None
    grader_reason: str = ""


# ── helpers ──────────────────────────────────────────────────────────────────


def _tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def _catalog_papers() -> list[dict[str, Any]]:
    out = subprocess.run(
        ["nx", "catalog", "list", "--type", "paper", "-n", "400", "--json"],
        capture_output=True, text=True, check=True,
    )
    rows = json.loads(out.stdout)
    return [r for r in rows if r.get("physical_collection") == COLLECTION]


def _raw_text(file_path: str) -> str:
    p = Path(file_path)
    if p.suffix.lower() == ".pdf":
        out = subprocess.run(["pdftotext", "-layout", str(p), "-"], capture_output=True, text=True)
        return out.stdout if out.returncode == 0 else ""
    try:
        return p.read_text(errors="replace")
    except OSError:
        return ""


def _slices(text: str, total_chars: int = 14000) -> str:
    """Head, middle and tail of the paper so questions are not all abstract-shaped."""
    if len(text) <= total_chars:
        return text
    third = total_chars // 3
    mid = len(text) // 2
    return (
        text[:third] + "\n[...]\n" + text[mid - third // 2 : mid + third // 2]
        + "\n[...]\n" + text[-third:]
    )


async def _dispatch(prompt: str, schema: dict, model: str, sink: list) -> dict:
    from nexus.operators.dispatch import claude_dispatch  # noqa: PLC0415

    return await claude_dispatch(prompt, schema, timeout=240, model=model, usage_sink=sink)


def _usage(sink: list) -> tuple[float | None, int | None]:
    if not sink:
        return None, None
    u = sink[-1]
    return getattr(u, "cost_usd", None), getattr(u, "duration_ms", None)


# ── question generation ──────────────────────────────────────────────────────


GEN_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["method_result", "detail"]},
                    "question": {"type": "string"},
                    "gold_answer": {"type": "string"},
                    "supporting_quote": {"type": "string"},
                },
                "required": ["kind", "question", "gold_answer", "supporting_quote"],
            },
        }
    },
    "required": ["questions"],
}


def _gen_prompt(title: str, text: str, per_paper: int) -> str:
    return f"""You are building an evaluation set for a retrieval system over a corpus of research papers.
Below is text from the paper "{title}". Write {per_paper} questions a researcher might ask of a corpus that contains this paper, each answerable from the text below and nowhere else in the text you were not shown.

Half the questions must be kind "method_result": about the paper's problem, method, dataset, baseline, or headline result.
Half must be kind "detail": a specific number, definition, parameter, named component, or condition stated in the text.

Rules: the question must not name the paper or its authors, but must contain enough subject matter that a search over a mixed corpus can find this paper. The gold_answer is one or two sentences. The supporting_quote is a verbatim sentence or fragment from the text that establishes the answer. Return JSON only.

PAPER TEXT:
{text}
"""


async def cmd_gen(args: argparse.Namespace) -> None:
    random.seed(SEED)
    papers = [p for p in _catalog_papers() if (p.get("chunk_count") or 0) >= 20]
    random.shuffle(papers)
    picked = papers[: args.papers]
    sem = asyncio.Semaphore(CONCURRENCY)
    questions: list[Question] = []

    async def one(p: dict) -> None:
        text = _slices(_raw_text(p["file_path"]))
        if len(text) < 2000:
            print(f"skip {p['tumbler']}: no text", file=sys.stderr)  # noqa: T201 — experiment script; stdout is its report channel
            return
        async with sem:
            res = await _dispatch(_gen_prompt(p["title"], text, args.per_paper), GEN_SCHEMA, GEN_MODEL, [])
        for i, q in enumerate(res.get("questions", [])[: args.per_paper]):
            questions.append(Question(
                qid=f"{p['tumbler']}-{i}", tumbler=p["tumbler"], title=p["title"],
                kind=q["kind"], question=q["question"], gold_answer=q["gold_answer"],
                supporting_quote=q["supporting_quote"],
            ))
        print(f"gen {p['tumbler']} {p['title'][:60]}", file=sys.stderr)  # noqa: T201 — experiment script; stdout is its report channel

    await asyncio.gather(*(one(p) for p in picked))
    out = Path(args.out) / "questions.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([asdict(q) for q in questions], indent=1))
    print(f"{len(questions)} questions from {len(picked)} papers -> {out}")  # noqa: T201 — experiment script; stdout is its report channel


# ── retrieval arms ───────────────────────────────────────────────────────────


def _render_aspects(title: str, rec: Any) -> str:
    parts = [f"## {title}"]
    for name in ("problem_formulation", "proposed_method", "experimental_results"):
        v = getattr(rec, name, None)
        if v:
            parts.append(f"{name}: {v}")
    if rec.experimental_datasets:
        parts.append("experimental_datasets: " + "; ".join(rec.experimental_datasets))
    if rec.experimental_baselines:
        parts.append("experimental_baselines: " + "; ".join(rec.experimental_baselines))
    if rec.extras:
        parts.append("extras: " + json.dumps(rec.extras, ensure_ascii=False))
    return "\n".join(parts)


def _pack(items: list[tuple[str, str]], budget: int) -> tuple[str, list[str]]:
    """Pack (doc, text) items in order until the budget is spent."""
    used, out, docs = 0, [], []
    for doc, text in items:
        t = _tokens(text)
        if used + t > budget:
            remaining = (budget - used) * CHARS_PER_TOKEN
            if remaining > 400:
                out.append(text[:remaining])
                docs.append(doc)
            break
        out.append(text)
        docs.append(doc)
        used += t
    return "\n\n".join(out), docs


class Arms:
    def __init__(self) -> None:
        from nexus.db import make_t3  # noqa: PLC0415
        from nexus.db.t2.http_document_aspects_store import HttpDocumentAspectsStore  # noqa: PLC0415

        self.db = make_t3()
        self.aspects = HttpDocumentAspectsStore()
        papers = _catalog_papers()
        # Chunk rows carry source_uri (the catalog's document identity) and a
        # title; titles collide (two CacheRAG registrations), so the URI is
        # the primary key and the title is the fallback.
        self.by_uri = {p["source_uri"]: p["tumbler"] for p in papers if p.get("source_uri")}
        self.by_title: dict[str, list[str]] = defaultdict(list)
        for p in papers:
            self.by_title[p["title"]].append(p["tumbler"])
        self.title_of = {p["tumbler"]: p["title"] for p in papers}
        self._aspect_cache: dict[str, Any] = {}

    def _doc(self, row: dict) -> str:
        t = self.by_uri.get(row.get("source_uri") or "")
        if t:
            return t
        cands = self.by_title.get(row.get("title", ""), [])
        return cands[0] if cands else "?"

    def _search(self, question: str, k: int) -> list[dict]:
        rows = self.db.search(question, [COLLECTION], n_results=k, include_source_uri=True)
        return rows if isinstance(rows, list) else list(rows.get("results", []))

    def _aspect_row(self, tumbler: str) -> Any:
        """The aspect row for a document, or a same-title sibling's when the
        catalog holds duplicate registrations and only one was extracted."""
        if tumbler in self._aspect_cache:
            return self._aspect_cache[tumbler]
        rec = self.aspects.get_by_doc_id(tumbler)
        if rec is None:
            for sib in self.by_title.get(self.title_of.get(tumbler, ""), []):
                if sib != tumbler:
                    rec = self.aspects.get_by_doc_id(sib)
                    if rec is not None:
                        break
        self._aspect_cache[tumbler] = rec
        return rec

    def chunks(self, question: str, budget: int) -> tuple[str, list[str]]:
        rows = self._search(question, 40)
        items = [(self._doc(r), f"## {r.get('title','')}\n{r.get('content','')}") for r in rows]
        return _pack(items, budget)

    def aspects_payload(self, question: str, budget: int) -> tuple[str, list[str]]:
        rows = self._search(question, 40)
        seen: list[str] = []
        for r in rows:
            t = self._doc(r)
            if t != "?" and t not in seen:
                seen.append(t)
        items = []
        for t in seen:
            rec = self._aspect_row(t)
            if rec is None:
                continue
            items.append((t, _render_aspects(self.title_of.get(t, t), rec)))
        return _pack(items, budget)


# ── reader and grader ────────────────────────────────────────────────────────


READ_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "found": {"type": "boolean"}},
    "required": ["answer", "found"],
}
GRADE_SCHEMA = {
    "type": "object",
    "properties": {"correct": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["correct", "reason"],
}


def _read_prompt(question: str, payload: str) -> str:
    return f"""Answer the question using ONLY the material below. If the material does not contain the answer, set found=false and say so in one sentence. Be specific: numbers, names, and conditions matter. Two sentences at most.

QUESTION: {question}

MATERIAL:
{payload}
"""


def _grade_prompt(q: Question, answer: str) -> str:
    return f"""You are grading a short answer against a gold answer. Mark correct=true only if the answer states the same fact as the gold answer (same number, name, or condition, allowing paraphrase). An answer that says the material does not contain the answer is incorrect. Give a one-sentence reason.

QUESTION: {q.question}
GOLD ANSWER: {q.gold_answer}
SUPPORTING QUOTE: {q.supporting_quote}
ANSWER UNDER TEST: {answer}
"""


async def cmd_run(args: argparse.Namespace) -> None:
    qs = [Question(**d) for d in json.loads((Path(args.out) / "questions.json").read_text())]
    budgets = [int(b) for b in args.budgets.split(",")]
    arms = Arms()
    sem = asyncio.Semaphore(CONCURRENCY)
    results_path = Path(args.out) / "results.jsonl"
    done = set()
    if results_path.exists():
        for line in results_path.read_text().splitlines():
            d = json.loads(line)
            done.add((d["qid"], d["arm"], d["budget"]))
    fh = results_path.open("a")

    async def one(q: Question, arm: str, budget: int) -> None:
        if (q.qid, arm, budget) in done:
            return
        payload, docs = (arms.chunks if arm == "chunks" else arms.aspects_payload)(q.question, budget)
        sink: list = []
        async with sem:
            t0 = time.monotonic()
            try:
                res = await _dispatch(_read_prompt(q.question, payload), READ_SCHEMA, READER_MODEL, sink)
                answer = res.get("answer", "")
            except Exception as exc:  # noqa: BLE001 — record and move on
                answer = f"[dispatch failed: {type(exc).__name__}]"
            cost, ms = _usage(sink)
            ms = ms or int((time.monotonic() - t0) * 1000)
        trial = Trial(
            qid=q.qid, arm=arm, budget=budget, payload_tokens=_tokens(payload), docs_in_payload=docs,
            gold_doc_in_payload=any(d in docs for d in arms.by_title.get(q.title, [q.tumbler])),
            answer=answer, reader_cost_usd=cost, reader_ms=ms,
        )
        gsink: list = []
        async with sem:
            try:
                g = await _dispatch(_grade_prompt(q, answer), GRADE_SCHEMA, GRADER_MODEL, gsink)
                trial.correct = bool(g.get("correct"))
                trial.grader_reason = g.get("reason", "")
            except Exception as exc:  # noqa: BLE001
                trial.grader_reason = f"[grader failed: {type(exc).__name__}]"
        fh.write(json.dumps(asdict(trial)) + "\n")
        fh.flush()
        print(f"{q.qid} {arm:8} b={budget:5} tok={trial.payload_tokens:5} gold_in={trial.gold_doc_in_payload} correct={trial.correct}", file=sys.stderr)  # noqa: T201 — experiment script; stdout is its report channel

    await asyncio.gather(*(one(q, arm, b) for q in qs for arm in ("chunks", "aspects") for b in budgets))
    fh.close()
    print(f"results -> {results_path}")  # noqa: T201 — experiment script; stdout is its report channel


# ── report ───────────────────────────────────────────────────────────────────


def cmd_report(args: argparse.Namespace) -> None:
    qs = {d["qid"]: d for d in json.loads((Path(args.out) / "questions.json").read_text())}
    rows = [json.loads(l) for l in (Path(args.out) / "results.jsonl").read_text().splitlines()]
    cells: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for r in rows:
        cells[(r["arm"], r["budget"])].append(r)
    lines = ["| arm | budget | n | accuracy | gold doc in payload | mean payload tokens | mean reader cost | detail acc | method acc |",
             "|---|---|---|---|---|---|---|---|---|"]
    for (arm, budget), rs in sorted(cells.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        n = len(rs)
        acc = sum(1 for r in rs if r["correct"]) / n
        gold = sum(1 for r in rs if r["gold_doc_in_payload"]) / n
        tok = sum(r["payload_tokens"] for r in rs) / n
        costs = [r["reader_cost_usd"] for r in rs if r["reader_cost_usd"] is not None]
        cost = sum(costs) / len(costs) if costs else float("nan")
        det = [r for r in rs if qs[r["qid"]]["kind"] == "detail"]
        met = [r for r in rs if qs[r["qid"]]["kind"] == "method_result"]
        dacc = sum(1 for r in det if r["correct"]) / len(det) if det else float("nan")
        macc = sum(1 for r in met if r["correct"]) / len(met) if met else float("nan")
        lines.append(f"| {arm} | {budget} | {n} | {acc:.1%} | {gold:.1%} | {tok:.0f} | ${cost:.4f} | {dacc:.1%} | {macc:.1%} |")
    # paired counts per budget
    for budget in sorted({r["budget"] for r in rows}):
        a = {r["qid"]: r["correct"] for r in rows if r["arm"] == "chunks" and r["budget"] == budget}
        b = {r["qid"]: r["correct"] for r in rows if r["arm"] == "aspects" and r["budget"] == budget}
        both = [q for q in a if q in b]
        cw = sum(1 for q in both if a[q] and not b[q])
        aw = sum(1 for q in both if b[q] and not a[q])
        lines.append(f"\nbudget {budget}: chunks-only-correct={cw}, aspects-only-correct={aw}, both={sum(1 for q in both if a[q] and b[q])}, neither={sum(1 for q in both if not a[q] and not b[q])}")
    text = "\n".join(lines)
    (Path(args.out) / "summary.md").write_text(text + "\n")
    print(text)  # noqa: T201 — experiment script; stdout is its report channel


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="scripts/experiments/out/isc_payload_ab")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen")
    g.add_argument("--papers", type=int, default=20)
    g.add_argument("--per-paper", type=int, default=2)
    r = sub.add_parser("run")
    r.add_argument("--budgets", default="2000,8000")
    sub.add_parser("report")
    args = ap.parse_args()
    if args.cmd == "gen":
        asyncio.run(cmd_gen(args))
    elif args.cmd == "run":
        asyncio.run(cmd_run(args))
    else:
        cmd_report(args)


if __name__ == "__main__":
    main()
