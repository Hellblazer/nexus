# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-244 Track A: draw the pre-registered query and corpus sample for one CoIR dataset.

Pure: takes the dataset's rows, returns the sample. The rules, in order:

1. Queries: up to ``queries`` ids drawn with ``random.Random(seed)`` from the SORTED ids of the
   test-split qrels (CoIR scores the test split; nexus-2k66t.2.12).
2. A document is unloadable when its text is empty or longer than ``max_bytes`` UTF-8 bytes (the
   note writer refuses over ``QUOTAS.MAX_DOCUMENT_BYTES``; nexus-2k66t.2.6). An unloadable document is
   excluded, and so is every drawn query whose relevant document it is. Both counts are reported.
   The query draw is not redone, so the excluded queries are not replaced. Only positive judgements
   are kept (every CoIR pool score is 1; a score-0 judgement names no relevant document).
3. Corpus: every document relevant to a kept query, then a draw with ``random.Random(seed)`` from
   the remaining loadable documents, sorted by id, up to ``cap`` documents in all. A corpus smaller
   than the cap loads whole.
4. Duplicate text (Sam, ask rdr244-duplicate-text, 2026-11-06: equivalence classes): loaded
   documents with byte-identical text form one class, named by its smallest id. The scorer ranks
   and judges classes, not copies, because any retriever sees identical texts as one document.
   ``duplicate_classes`` lists every class of more than one copy, for the per-dataset report.
5. The manifest hash covers each loaded document's and each kept query's text SHA-256, so a changed
   text (another revision of the data) changes the hash even when the ids do not.
"""
from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cached_property

SEED: int = 2026
QUERIES: int = 50
CAP: int = 5000
MAX_BYTES: int = 16384


@dataclass(frozen=True)
class Sample:
    dataset: str
    query_ids: list[str]
    qrels: dict[str, dict[str, int]]
    corpus_ids: list[str]
    excluded_docs: list[str] = field(default_factory=list)
    excluded_queries: list[str] = field(default_factory=list)
    duplicate_classes: dict[str, list[str]] = field(default_factory=dict)
    query_sha256: dict[str, str] = field(default_factory=dict)
    document_sha256: dict[str, str] = field(default_factory=dict)
    grades: list[int] = field(default_factory=list)

    def class_of(self, corpus_id: str) -> str:
        """The class representative for ``corpus_id``: itself unless it has identical-text twins."""
        return self._rep.get(corpus_id, corpus_id)

    @cached_property
    def _rep(self) -> dict[str, str]:
        return {m: rep for rep, members in self.duplicate_classes.items() for m in members}

    def manifest(self) -> dict:
        body = {"dataset": self.dataset, "query_ids": self.query_ids, "qrels": self.qrels,
                "corpus_ids": self.corpus_ids, "excluded_docs": self.excluded_docs,
                "excluded_queries": self.excluded_queries, "duplicate_classes": self.duplicate_classes,
                "query_sha256": self.query_sha256, "document_sha256": self.document_sha256, "grades": self.grades}
        blob = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        return {**body, "sha256": hashlib.sha256(blob).hexdigest()}


def loadable(text: str | None, *, max_bytes: int = MAX_BYTES) -> bool:
    return bool((text or "").strip()) and len((text or "").encode("utf-8")) <= max_bytes


def _sha(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def draw(dataset: str, corpus: Sequence[dict], qrels_rows: Sequence[dict], *, seed: int = SEED,
         queries: int = QUERIES, cap: int = CAP, max_bytes: int = MAX_BYTES,
         query_text: dict[str, str] | None = None) -> Sample:
    qrels: dict[str, dict[str, int]] = {}
    for r in qrels_rows:
        qrels.setdefault(str(r["query_id"]), {})[str(r["corpus_id"])] = int(r["score"])
    all_qids = sorted(qrels)
    drawn = sorted(random.Random(seed).sample(all_qids, min(queries, len(all_qids))))

    text_of = {str(d["_id"]): d.get("text") for d in corpus}
    bad = {i for i, t in text_of.items() if not loadable(t, max_bytes=max_bytes)}
    kept, dropped = [], []
    for q in drawn:
        rel = [c for c, s in qrels[q].items() if s > 0]
        missing = [c for c in rel if c not in text_of]
        if missing:
            raise ValueError(f"{dataset}: query {q} names corpus ids not in the corpus: {missing[:3]}")
        (dropped if any(c in bad for c in rel) else kept).append(q)
    must = sorted({c for q in kept for c, g in qrels[q].items() if g > 0})
    rest = sorted(i for i in text_of if i not in bad and i not in set(must))
    room = max(0, cap - len(must))
    fill = rest if len(rest) <= room else random.Random(seed).sample(rest, room)
    corpus_ids = sorted(set(must) | set(fill))
    kept_qrels = {q: {c: g for c, g in qrels[q].items() if g > 0} for q in kept}
    return Sample(
        dataset=dataset, query_ids=kept, qrels=kept_qrels,
        corpus_ids=corpus_ids, excluded_docs=sorted(bad), excluded_queries=dropped,
        duplicate_classes=duplicate_classes(corpus_ids, text_of),
        query_sha256={q: _sha(query_text.get(q)) for q in kept} if query_text is not None else {},
        document_sha256={c: _sha(text_of[c]) for c in corpus_ids},
        grades=sorted({g for q in kept for g in kept_qrels[q].values()}),
    )


def duplicate_classes(ids: Sequence[str], text_of: dict[str, str | None]) -> dict[str, list[str]]:
    """Group ``ids`` by byte-identical text; return only groups of two or more, keyed by the smallest id."""
    by_text: dict[str, list[str]] = {}
    for i in sorted(ids):
        by_text.setdefault(text_of[i] or "", []).append(i)
    return {g[0]: g for g in by_text.values() if len(g) > 1}
