#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-109 Phase 4b: programmatic Q&A generation.

For each content_type's target collection, sample N chunks and derive
a question per chunk via simple template heuristics. The expected
chunk chash is by construction the chunk we drew the question from,
so chashes are verified. Output JSONL matches the harness schema
``{question, expected_chunk_chash, content_type}``.

Determinism: a fixed seed in the sampling pass keeps the output
reproducible. Re-running on the same corpus/seed yields the same QA
file.

Quality caveat: programmatic Q&A produces near-paraphrase questions.
The retrieval baseline will be artificially strong for some items
(distinctive vocabulary -> obvious match) and weak for others (the
generator's question pulled tokens the chunker placed in adjacent
chunks). This is acceptable for a relative comparison across boost
weights; absolute hit-rate numbers should be interpreted accordingly.
Hand-curation is preferred for paper benchmark; see nexus-oxq2
comments.

Usage:

    python scripts/rdr-109-generate-qa.py \\
        --content-type knowledge \\
        --collection knowledge__rag-papers \\
        --count 35
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "calibration" / "rdr-109"

# Sentence-ish split, same heuristic as scripts/rdr_109_salience.py.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[`*_])")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
_CODE_DEF_RE = re.compile(
    r"^\s*(?:async\s+)?(?:def|class|function|fn|public\s+\w+|"
    r"private\s+\w+|protected\s+\w+|static\s+\w+)\s+([A-Za-z_]\w*)",
    re.MULTILINE,
)


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]


def _question_from_prose(text: str) -> str | None:
    """Derive a question from a prose chunk.

    Strategy: prefer a leading markdown heading (which usually names
    the concept the chunk is about). Otherwise pick the first
    informative sentence (40-200 chars). Question template asks
    *about* the heading/sentence rather than restating it verbatim.
    """
    m = _HEADING_RE.search(text)
    if m:
        topic = m.group(1).strip().rstrip(":")
        # Clean noise like leading numbers ("1. Intro").
        topic = re.sub(r"^\d+\.\s*", "", topic)
        return f"What does the section on {topic!r} cover?"
    for s in _split_sentences(text):
        if 40 <= len(s) <= 200:
            # Strip a leading parenthetical reference like "(Smith 2024)".
            clean = re.sub(r"^\([^)]+\)\s*", "", s)
            return f"What discusses: {clean[:160]}?"
    return None


def _question_from_code(text: str) -> str | None:
    """Derive a question from a code chunk: name the first def/class."""
    m = _CODE_DEF_RE.search(text)
    if m:
        symbol = m.group(1)
        return f"How is {symbol!r} implemented or defined?"
    # Fallback: comment / docstring line.
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("//", "#", "/*", "*")) and len(line) > 30:
            return f"What does this comment describe: {line[:120]}?"
    return None


_DERIVERS = {
    "knowledge": _question_from_prose,
    "docs": _question_from_prose,
    "rdr": _question_from_prose,
    "code": _question_from_code,
}


def generate(
    *,
    content_type: str,
    collection: str,
    count: int,
    seed: int,
    out_path: Path,
) -> int:
    from nexus.db import make_t3  # noqa: PLC0415
    t3 = make_t3()
    coll = t3._client.get_collection(collection)
    # Page through up to a reasonable cap; we'll downsample to *count*.
    chunks: list[dict] = []
    offset = 0
    cap = max(count * 5, 200)
    while len(chunks) < cap:
        res = coll.get(limit=300, offset=offset, include=["documents", "metadatas"])
        ids = res.get("ids", []) or []
        if not ids:
            break
        for cid, doc, md in zip(ids, res["documents"] or [], res["metadatas"] or []):
            chash = (md or {}).get("chunk_text_hash") or cid
            chunks.append({"chash": chash, "text": doc or ""})
        if len(ids) < 300:
            break
        offset += 300
    rng = random.Random(seed)
    rng.shuffle(chunks)

    derive = _DERIVERS[content_type]
    out: list[dict] = []
    for chunk in chunks:
        if len(out) >= count:
            break
        question = derive(chunk["text"])
        if not question:
            continue
        # Defensive: skip duplicates and chunks whose text is shorter
        # than a real sentence.
        if any(o["question"] == question for o in out):
            continue
        if len(chunk["text"]) < 80:
            continue
        out.append({
            "question": question,
            "expected_chunk_chash": chunk["chash"],
            "content_type": content_type,
        })

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        f.write(
            f"# RDR-109 Phase 4b generated QA for {content_type} "
            f"from {collection}.\n"
            f"# Seed={seed} count={len(out)}. Programmatic; see "
            f"scripts/rdr-109-generate-qa.py.\n"
        )
        for rec in out:
            f.write(json.dumps(rec) + "\n")
    print(f"wrote {len(out)} items to {out_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--content-type", required=True,
                    choices=["knowledge", "code", "docs", "rdr"])
    ap.add_argument("--collection", required=True)
    ap.add_argument("--count", type=int, default=35)
    ap.add_argument("--seed", type=int, default=109)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    out = args.out or DATA_DIR / f"qa_{args.content_type}.jsonl"
    return generate(
        content_type=args.content_type,
        collection=args.collection,
        count=args.count,
        seed=args.seed,
        out_path=out,
    )


if __name__ == "__main__":
    sys.exit(main())
