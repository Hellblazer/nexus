# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-147 Step 3 (nexus-v9v5r): resolve key tokens to catalog documents by exact title.

``resolve_keys`` is the core of the runner-native ``resolve`` plan step, kept a plain function over a catalog
client so the resolution repair in the coverage boundary (RDR-147 Step 4) can call it directly. The runner's
dispatcher wraps it; there is no MCP tool and no model call, and it costs no embedding.

For each DISTINCT token, in first-seen order, one ``find_by_title_exact`` call:

* no live document: the token is in ``missing``;
* several: ``ambiguous[token]`` lists every candidate tumbler, and none of them is resolved (a first-match pick
  would answer about a record nobody chose);
* exactly one: its tumbler, plus the chunk hashes of its manifest (one batched ``get_manifests`` for all the
  resolved documents) as ``ids``.

The result fits the standard retrieval step-output contract, so ``$stepN.ids`` / ``$stepN.tumblers`` resolve and
a following ``store_get_many`` (or an operator's auto-hydration) reads it unchanged. ``tumblers`` has one entry
per resolved DOCUMENT; ``collections`` and ``chunk_collections`` have one entry per ``ids`` entry, because
``store_get_many`` reads a ``collections`` list the length of ``ids`` as per-id routing and the evidence
diagnostics read ``chunk_collections`` the same way.

``find_by_title_exact`` answers live documents only, so a trashed document is ``missing``. The match is exact:
case, whitespace and punctuation count, so ``cust-0018`` does not find ``CUST-0018``.
"""
from __future__ import annotations

from typing import Any, Protocol

__all__ = ["BY_VALUES", "resolve_keys"]

#: The ``by`` values the step accepts. Only the title: a chash or a tumbler is already an address.
BY_VALUES: tuple[str, ...] = ("title",)


class _Catalog(Protocol):
    def find_by_title_exact(self, title: str, *, content_type: str | None = ..., owner: Any = ...) -> list[Any]: ...

    def get_manifests(self, doc_ids: list[str]) -> dict[str, list[Any]]: ...


def resolve_keys(
    catalog: _Catalog,
    tokens: list[str],
    *,
    by: str = "title",
    content_type: str | None = None,
    owner: Any = None,
) -> dict[str, Any]:
    """Resolve *tokens* against *catalog*; see the module docstring for the result.

    Raises ``ValueError`` for a *by* other than ``"title"`` and for a blank token, ``TypeError`` for *tokens*
    that is not a list of strings: a plan-authoring mistake fails loud here and does not become a ``missing``
    answer. *content_type* and *owner* (a tumbler prefix) narrow every lookup, which is how a caller turns an
    ambiguous token into a single match.
    """
    if by not in BY_VALUES:
        raise ValueError(f"resolve: by={by!r} is not supported; the only value is {BY_VALUES[0]!r}")
    if not isinstance(tokens, (list, tuple)):
        raise TypeError(f"resolve: tokens must be a list of strings, got {type(tokens).__name__}")
    for token in tokens:
        if not isinstance(token, str):
            raise TypeError(f"resolve: every token must be a string, got {type(token).__name__}: {token!r}")
        if not token.strip():
            raise ValueError("resolve: a blank token cannot name a document")

    missing: list[str] = []
    ambiguous: dict[str, list[str]] = {}
    resolved: list[Any] = []
    for token in dict.fromkeys(tokens):
        docs = catalog.find_by_title_exact(token, content_type=content_type, owner=owner)
        if not docs:
            missing.append(token)
        elif len(docs) > 1:
            ambiguous[token] = [str(d.tumbler) for d in docs]
        else:
            resolved.append(docs[0])

    tumblers = [str(d.tumbler) for d in resolved]
    manifests = catalog.get_manifests(tumblers) if tumblers else {}
    ids: list[str] = []
    collections: list[str] = []
    seen: set[tuple[str, str]] = set()
    for doc, tumbler in zip(resolved, tumblers, strict=True):
        for row in sorted(manifests.get(tumbler) or [], key=lambda r: r.position):
            collection = row.collection or doc.physical_collection or ""
            if (row.chash, collection) in seen:
                continue
            seen.add((row.chash, collection))
            ids.append(row.chash)
            collections.append(collection)
    return {
        "tumblers": tumblers,
        "ids": ids,
        "collections": collections,
        "chunk_collections": list(collections),
        "missing": missing,
        "ambiguous": ambiguous,
    }
