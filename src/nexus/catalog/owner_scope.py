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

import structlog

_log = structlog.get_logger(__name__)


class OwnerScopeError(ValueError):
    """The scope names no owner, or more than one."""


_CORPUS_PREFIXES: frozenset[str] = frozenset({"knowledge", "code", "docs", "rdr", "all"})


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
        repo_matches = _repo_owners_among(cat, matches)
        if len(repo_matches) == 1:
            # GH #1544: a repo and the curator behind its knowledge__<name>
            # collection legitimately share one name (the constraint is
            # UNIQUE(name, owner_type)). The name a caller has in hand is
            # the repo's; the curator stays reachable by its tumbler. The
            # write side is the mirror image and not in conflict:
            # store_hook's curator lookup filters by owner_type so a
            # same-named repo cannot capture a knowledge write. Logged so
            # the choice is never silent (substantive-critic, 2026-09-14).
            others = [str(t) for t in matches if t is not repo_matches[0]]
            _log.info(
                "owner_scope_repo_preferred",
                name=text,
                repo=str(repo_matches[0]),
                other_owners=others,
            )
            return str(repo_matches[0])
        candidates = ", ".join(str(t) for t in matches)
        raise OwnerScopeError(
            f"{text!r} is ambiguous: {len(matches)} owners share this name across "
            f"types ({candidates}). Pass the dotted tumbler."
        )
    return str(matches[0])


def _repo_owners_among(cat: Any, tumblers: list[Any]) -> list[Any]:
    """The subset of *tumblers* whose owner row is ``owner_type == "repo"``.

    Read through ``get_owner_by_prefix`` (on the reader protocol already)
    rather than a new by-name-with-type method, so this stays a two-extra-
    round-trip path taken only when a name is shared. An owner row the
    catalog cannot show is not a repo; it never captures the name.
    """
    repos: list[Any] = []
    for t in tumblers:
        row = cat.get_owner_by_prefix(str(t))
        if isinstance(row, dict) and row.get("owner_type") == "repo":
            repos.append(t)
    return repos
