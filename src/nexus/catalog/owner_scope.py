# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resolve a scope argument that may be an owner NAME to its tumbler prefix.

GH #1527 (nexus-qiah5): every scoped surface (``query(subtree=...)``,
``nx_answer(scope=...)``, ``catalog_resolve(owner=...)``, ``nx search
--repo``) took a dotted tumbler only, so scoping a search to one repository
meant running ``nx catalog owners`` first to find ``1.61``. The owner table
already maps a registered name to its tumbler; this is the one place that
mapping is consulted, so every surface accepts either form and reports the
same errors.
"""

from __future__ import annotations

from typing import Any


class OwnerScopeError(ValueError):
    """The scope names no owner, or more than one."""


_CORPUS_PREFIXES: frozenset[str] = frozenset({"knowledge", "code", "docs", "rdr"})


def _is_corpus_scope(text: str) -> bool:
    """A bare corpus prefix, a ``prefix__`` form, or a full collection name."""
    from nexus.corpus import (  # noqa: PLC0415 — deferred: heavy import, branch-local
        is_conformant_collection_name,
        split_candidate_collection_name,
    )

    # A user-typed scope token (class (a) in the parse census): the first
    # segment of a CANDIDATE string, never a read of a registered
    # collection's attributes.
    first, _rest = split_candidate_collection_name(text)
    head = first or text  # a bare token has no separator: the token IS the head
    return head in _CORPUS_PREFIXES or is_conformant_collection_name(text)


def resolve_owner_scope(cat: Any, raw: str, *, strict: bool = True) -> str:
    """Return *raw* itself when it is a dotted tumbler, else the tumbler of
    the registered owner named *raw*.

    Raises :class:`OwnerScopeError` when several owners have that name (the
    ``(name, owner_type)`` constraint lets a repo and a curator share one);
    the message lists the candidates so the caller can pass the dotted form.
    When no owner has the name: *strict* raises the same error (a
    ``subtree`` is a tumbler or an owner, nothing else); ``strict=False``
    returns *raw* unchanged, for a surface whose scope may also be a corpus
    name (``nx_answer(scope="knowledge")``).
    """
    from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415 — deferred: heavy catalog import, branch-local

    text = (raw or "").strip()
    if not text:
        return text
    try:
        Tumbler.parse(text)
        return text
    except (ValueError, TypeError):
        pass
    if not strict and _is_corpus_scope(text):
        # A corpus prefix or a full collection name is a corpus scope by
        # construction; an owner that happens to share the word (an owner
        # named "knowledge") must not capture it (review Significant).
        return text
    matches = cat.owner_tumblers_by_name(text)
    if not matches:
        if not strict:
            return text
        raise OwnerScopeError(
            f"{text!r} is neither a dotted tumbler (e.g. '1.2') nor the name of a "
            "registered owner. `nx catalog owners` lists the known owners."
        )
    if len(matches) > 1:
        candidates = ", ".join(str(t) for t in matches)
        raise OwnerScopeError(
            f"{text!r} is ambiguous: {len(matches)} owners share this name across "
            f"types ({candidates}). Pass the dotted tumbler."
        )
    return str(matches[0])
