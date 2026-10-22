# SPDX-License-Identifier: AGPL-3.0-or-later
"""The fixed known-item query set (``queries.json``).

Twenty queries per corpus, each with the repo path of the one file it is about.
A chunk is relevant to a query when it comes from that file (a chunk whose text
is identical in another file is relevant to both). The set is fixed in the repo
so every run asks the same questions.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from p43_corpus import Corpus

MAX_QUERY_CHARS = 256     # QUOTAS.MAX_QUERY_STRING_CHARS
PER_SET = 20


@dataclass(frozen=True)
class Query:
    id: str
    set: str       # "prose" or "code"
    text: str
    target: str    # repo-relative path of the file the query is about


def load_queries(path: Path) -> list[Query]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return [Query(q["id"], q["set"], q["query"], q["target"]) for q in doc["queries"]]


def validate(queries: list[Query], corpora: dict[str, Corpus]) -> None:
    """Fail loudly on a malformed set: wrong counts, duplicate ids, a query over
    the length limit, or a target that is not in its corpus."""
    ids = [q.id for q in queries]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate query ids")
    for kind in ("prose", "code"):
        mine = [q for q in queries if q.set == kind]
        if len(mine) != PER_SET:
            raise ValueError(f"{kind}: expected {PER_SET} queries, found {len(mine)}")
        if len({q.target for q in mine}) != len(mine):
            raise ValueError(f"{kind}: two queries share a target file")
        for q in mine:
            if len(q.text) > MAX_QUERY_CHARS:
                raise ValueError(f"{q.id}: query is {len(q.text)} chars, limit {MAX_QUERY_CHARS}")
            corpus = corpora.get(kind)
            if corpus is not None and q.target not in corpus.chunks_by_file:
                raise ValueError(f"{q.id}: target {q.target} is not in the {kind} corpus")


def of_set(queries: list[Query], kind: str) -> list[Query]:
    return [q for q in queries if q.set == kind]
