# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-232 section 2a (nexus-4xmpv; Sam, 2026-10-30): a request that names one source reads that source.

A synthesis or ``unknown`` request that names exactly ONE source (:attr:`RequestCoverage.narrows_to_one_source`)
wants that document's parts put together. Before this rule its secondary retrieval (a plan's ``search``, ``query``,
``traverse`` steps, whatever corpus the plan pinned) read every project's collections, and the plans that did it
loudest (citation-traversal, abstract-themes) were kept out by a deny-list of strategies. The deny-list was
default-admit: a plan nobody listed still read the whole library. This module replaces it with the narrowing itself.

**The rule.** Once the one source has resolved to one document, the run carries two internal bindings the runner
already applies to every step:

* ``_nx_scope`` becomes the source's collection (``rdr__1-2__voyage-context-3__v1``). The runner's section 2a
  rule (:func:`nexus.plans.evidence.narrow_corpus`) then narrows every corpus-taking retrieval step to it: a plan
  that pins ``corpus="rdr"`` reads that collection, a step pinned to ``code`` or ``knowledge`` has no overlap and
  is skipped (never run unscoped, disclosed as a scope conflict), a step with no corpus gets the collection. The
  ``traverse`` policy derives from the same scope (``allowed_collections``), and the coverage boundary's repair
  queries and scope check read it too. It holds for every plan: a library plan, the inline planner's and
  ``research:default`` / ``analyze:default`` alike.
* ``_nx_owners`` becomes the source's owner (``1.2``), which narrows the vector corpus by owner and admits only
  that owner's documents in a traversal (:mod:`nexus.plans.owner_narrowing`).

**The caller's scope.** It is only refined, never widened or replaced by something it excludes: no scope, ``all``,
a corpus prefix or collection that admits the source's collection, and a catalog subtree that holds the source's
document are all replaced by the narrower collection. A scope that does NOT admit the source's collection stays as
it is and the narrowing is reported ``applied: false`` with the reason.

**When it does not apply** (each is disclosed, none is silent): the request names no source or several; the kind is
not synthesis or ``unknown``; the source did not resolve to one document (a candidates list, nothing found, a failed
lookup: there is no collection to narrow to, so a plan that reads other documents still runs); the resolved source
has no collection. A document named only by its title counts as no named source, so it never narrows.

**Trade-off, decided by Sam on 2026-10-30:** a request that names one RDR in passing ("how does RDR-084 compare with
how we usually chunk") loses the cross-document context its other retrieval would have found. A lookup or comparison
request, which still reads across documents, is not narrowed.

**Where the reducer's slots go.** The narrowing is what lets the primary stream take 90 of the reducer's 100 input
slots; the arithmetic is stated once, in :mod:`nexus.plans.primary_evidence` (``PrimaryEvidenceCaps``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nexus.plans.evidence import ScopeConstraint
from nexus.plans.owner_narrowing import owner_tumblers

if TYPE_CHECKING:
    from nexus.plans.primary_evidence import NamedSourceResolution
    from nexus.plans.request_coverage import RequestCoverage

__all__ = [
    "SingleSourceNarrowing",
    "derive_single_source_narrowing",
]


@dataclass(frozen=True)
class SingleSourceNarrowing:
    """What the one-source rule did (or why it did not) for one run; the envelope's ``single_source_narrowing``."""

    #: Whether the secondary streams were narrowed.
    applied: bool
    #: The named source (``RDR-084``) the rule is about.
    source: str
    #: The resolved document, its collection and its owner; empty where the source did not resolve.
    tumbler: str = ""
    collection: str = ""
    owner: str = ""
    #: The caller's scope as it was, and the scope the steps ran under (the collection when applied).
    caller_scope: str = ""
    effective_scope: str = ""
    #: Why the rule did not apply; empty when it did.
    reason: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "applied": self.applied, "source": self.source, "tumbler": self.tumbler,
            "collection": self.collection, "owner": self.owner, "caller_scope": self.caller_scope,
            "effective_scope": self.effective_scope, "reason": self.reason,
        }

    def disclosure(self) -> str:
        """The one text-mode notice. It names the source the steps were narrowed to, or why they were not."""
        if self.applied:
            return (
                f"[evidence request: {self.source} is the only source this request names, so its retrieval "
                f"steps (search, query, traverse, whatever corpus the plan pinned) were narrowed to "
                f"collection {self.collection} and owner {self.owner}; other documents were not read, and a "
                "step on another collection was skipped]"
            )
        return (
            f"[evidence request: {self.source} is the only source this request names, but retrieval was not "
            f"narrowed to it: {self.reason}]"
        )


def derive_single_source_narrowing(
    coverage: RequestCoverage, resolution: NamedSourceResolution | None, caller_scope: str,
) -> SingleSourceNarrowing | None:
    """The narrowing for this run, or ``None`` when the rule does not concern the request (it does not narrow to one
    source: :attr:`RequestCoverage.narrows_to_one_source`). A request it concerns always gets a verdict."""
    if not coverage.narrows_to_one_source:
        return None
    ref = coverage.named_sources[0]
    base = {"source": ref.value, "caller_scope": caller_scope, "effective_scope": caller_scope}
    resolved = [s for s in (resolution.sources if resolution is not None else ()) if s.status == "resolved"]
    if len(resolved) != 1:
        state = "more than one document resolved" if resolved else "the source did not resolve to one document"
        return SingleSourceNarrowing(False, reason=f"{state}, so there is no collection to narrow to", **base)
    source = resolved[0]
    collection, tumbler = source.collection, source.tumbler or ""
    owners = owner_tumblers(tumbler)
    if not collection or not owners:
        return SingleSourceNarrowing(
            False, tumbler=tumbler, collection=collection,
            reason="the resolved document has no collection or owner address", **base,
        )
    scope = ScopeConstraint.parse(caller_scope)
    admitted = scope.kind == "none" or scope.value == "all" or scope.admits(collection, tumbler=tumbler)
    if not admitted:
        return SingleSourceNarrowing(
            False, tumbler=tumbler, collection=collection, owner=owners[0],
            reason=f"the caller's scope {caller_scope!r} does not admit {collection}, and a scope is never replaced "
                   "by something it excludes", **base,
        )
    return SingleSourceNarrowing(
        True, tumbler=tumbler, collection=collection, owner=owners[0],
        **{**base, "effective_scope": collection},
    )
