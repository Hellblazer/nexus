# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-232 section 2a (nexus-4xmpv, nexus-jypmj; Sam, 2026-10-30): a request that names one source reads that document.

A synthesis or ``unknown`` request that names exactly ONE source (:attr:`RequestCoverage.narrows_to_one_source`)
wants that document's parts put together. Before this rule its secondary retrieval (a plan's ``search``, ``query``,
``traverse`` steps, whatever corpus the plan pinned) read every project's collections, and the plans that did it
loudest were kept out by a deny-list of strategies. The deny-list was default-admit: a plan nobody listed still read
the whole library. Round 3 (nexus-4xmpv) narrowed to the document's collection and owner; re-measure 3 found that
sibling documents of the same collection still entered (1 of 170 secondary anchors lay in the named document), so the
narrowing is now to the DOCUMENT (nexus-jypmj, :mod:`nexus.plans.document_confinement`).

**The rule.** Once the one source has resolved to one document, the run carries these bindings, which the runner
already applies to every step:

* ``_nx_scope`` becomes the source's collection (``rdr__1-2__voyage-context-3__v1``), defense in depth: the runner's
  section 2a rule (:func:`nexus.plans.evidence.narrow_corpus`) narrows every corpus-taking retrieval step to it, a
  step pinned to ``code`` or ``knowledge`` has no overlap and is skipped. A caller scope that is THIS document's
  address stays as it is (a document address is not a corpus, and widening it to the collection would let a
  traversal and the coverage scope check read sibling documents).
* ``_nx_owners`` becomes the source's owner (``1.2``).
* A :class:`~nexus.plans.document_confinement.DocumentConfinement` (a run-scoped context value, not a binding: it
  carries the document's chunk-hash set) confines each secondary step to the document itself: the engine's chunk
  filter on ``search`` and ``query``, the document address on ``traverse``, and no dispatch at all for a step whose
  route takes no filter. On an engine that does not advertise the filter, no secondary step is dispatched.

**An inferred source.** The resolver may pick the document by the session's active project (``inferred``). That
choice narrows only when no other project carries the number (``foreign`` is 0): with a foreign candidate the
inference could be wrong, so the run is not narrowed and says why. A source the resolver reached by elimination
(``fallback``: the active project holds no such number, so another project's document was the only one) is unique
across projects and narrows.

**The caller's scope.** It is only refined, never widened or replaced by something it excludes: no scope, ``all``,
a corpus prefix or collection that admits the source's collection, and a catalog subtree that holds the source's
document are all replaced by the narrower collection, and the document's own address is kept. A scope that does NOT
admit the source's collection stays as it is and the narrowing is reported ``applied: false`` with the reason.

**When it does not apply** (each is disclosed, none is silent): the request names no source or several; the kind is
not synthesis or ``unknown``; the source did not resolve to one document (a candidates list, nothing found, a failed
lookup: there is no document to confine to, so a plan that reads other documents still runs); the resolved source has
no collection; an inferred source with documents of the same number in other projects. A document named only by its
title counts as no named source, so it never narrows.

**Trade-off, decided by Sam on 2026-10-30:** a request that names one RDR in passing ("how does RDR-084 compare with
how we usually chunk") loses the cross-document context its other retrieval would have found, and the disclosure says
so. A lookup or comparison request, which still reads across documents, is not narrowed.

**Where the reducer's slots go.** The confinement is what lets the primary stream take 88 of the reducer's 100 input
slots and the coverage repair the other 12; the arithmetic is stated once, in :mod:`nexus.plans.primary_evidence`
(``PrimaryEvidenceCaps``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from nexus.plans.coverage_repair import MAX_REPAIR_ITEMS
from nexus.plans.evidence import ScopeConstraint
from nexus.plans.owner_narrowing import owner_tumblers

if TYPE_CHECKING:
    from nexus.plans.document_confinement import DocumentConfinement
    from nexus.plans.primary_evidence import NamedSourceResolution, PrimaryEvidence
    from nexus.plans.request_coverage import RequestCoverage

__all__ = [
    "SingleSourceNarrowing",
    "derive_single_source_narrowing",
    "unavailable_single_source_narrowing",
]


@dataclass(frozen=True)
class SingleSourceNarrowing:
    """What the one-source rule did (or why it did not) for one run; the envelope's ``single_source_narrowing``."""

    #: Whether the secondary streams were confined to the document.
    applied: bool
    #: The named source (``RDR-084``) the rule is about.
    source: str
    #: The resolved document, its collection and its owner; empty where the source did not resolve.
    tumbler: str = ""
    collection: str = ""
    owner: str = ""
    #: The caller's scope as it was, and the scope the run reads: the document's tumbler when applied.
    caller_scope: str = ""
    effective_scope: str = ""
    #: Why the rule did not apply; empty when it did.
    reason: str = ""
    #: What the ``_nx_scope`` binding carries when applied: the collection, or the caller's own scope when that was
    #: this document's address. The evidence diagnostics and the coverage boundary read it. Not on the wire.
    step_scope: str = ""
    #: How the secondary steps are confined: ``engine_filter`` (the engine restricts search and query to the
    #: document's chunk hashes), ``steps_not_run`` (the engine has no such filter, so no secondary step is
    #: dispatched), ``None`` when not applied.
    confinement: str | None = None
    #: The primary read's chunks (S) of the document's manifest rows (M), the chunk hashes the filter carries, and
    #: whether that set is a sample of the manifest. Filled once the primary read is in (:meth:`with_primary`).
    read_chunks: int = 0
    manifest_chunks: int = 0
    allow_chunks: int = 0
    allow_sampled: bool = False
    #: The engine advertised the chunk filter. ``confinement`` is ``steps_not_run`` either for lack of it or, with it,
    #: because the manifest gave no hash to send; the disclosure says which. Not on the wire.
    engine_filter_available: bool = False
    #: Chunks of this document a coverage repair may add.
    repair_cap: int = MAX_REPAIR_ITEMS

    def to_wire(self) -> dict[str, Any]:
        return {
            "applied": self.applied, "source": self.source, "tumbler": self.tumbler,
            "document": self.tumbler if self.applied else "",
            "collection": self.collection, "owner": self.owner, "caller_scope": self.caller_scope,
            "effective_scope": self.effective_scope, "reason": self.reason, "confinement": self.confinement,
            "read_chunks": self.read_chunks, "manifest_chunks": self.manifest_chunks,
            "allow_chunks": self.allow_chunks, "allow_sampled": self.allow_sampled,
        }

    def with_confinement(self, mode: str) -> SingleSourceNarrowing:
        """This verdict with the confinement mode the engine's capability allows (``engine_filter`` or ``steps_not_run``)."""
        return replace(self, confinement=mode) if self.applied else self

    def with_primary(
        self, evidence: PrimaryEvidence | None, confinement: DocumentConfinement | None = None,
    ) -> SingleSourceNarrowing:
        """This verdict with the primary read's numbers (S of M) and the confinement the run settled on (a mode
        that needs the chunk set falls back to ``steps_not_run`` when the manifest gave none)."""
        if not self.applied:
            return self
        read = manifest = 0
        if evidence is not None:
            doc = next((d for d in evidence.documents if d.doc_id == self.tumbler), None)
            if doc is not None:
                manifest = doc.manifest_rows
                read = doc.selected_rows
        return replace(
            self, read_chunks=read, manifest_chunks=manifest,
            confinement=confinement.mode if confinement is not None else self.confinement,
            allow_chunks=len(confinement.chashes) if confinement is not None and confinement.sends_filter else 0,
            allow_sampled=confinement.sampled if confinement is not None and confinement.sends_filter else False,
            engine_filter_available=confinement.engine_filter_available if confinement is not None else False,
        )

    def disclosure(self) -> str:
        """The one text-mode notice: the document the answer reads, how the secondary steps were confined to it, the
        primary read's S of M chunks, and whether a repair may add more of its chunks. Or why nothing was narrowed."""
        if not self.applied:
            return (
                f"[evidence request: {self.source} is the only source this request names, but retrieval was not "
                f"narrowed to it: {self.reason}]"
            )
        if self.confinement == "engine_filter":
            held = f"{self.allow_chunks} chunk hashes" + (
                f", a sample of its {self.manifest_chunks} manifest rows" if self.allow_sampled else ""
            )
            how = (
                f"Secondary retrieval was confined to it by the engine (search and query by a filter of {held}; "
                "traversal by the document's address); a step on a route that takes no such filter was not run."
            )
        elif self.engine_filter_available:
            how = (
                "The engine applies the document filter, but the document's manifest gave no chunk hash to filter "
                "by, so no secondary retrieval step (search, query, traverse) was run."
            )
        else:
            how = (
                "The engine does not apply the document filter, so no secondary retrieval step (search, query, "
                "traverse) was run."
            )
        if self.manifest_chunks:
            read = f"The primary read holds {self.read_chunks} of its {self.manifest_chunks} chunks"
        else:
            read = "The primary read holds no chunk count for it"
        more = f"a coverage repair may add up to {self.repair_cap} more chunks of the same document"
        return (
            f"[evidence request: {self.source} is the only source this request names, so the answer reads only "
            f"that document ({self.tumbler}); other documents were not read and their context is not part of the "
            f"answer. {how} {read}; {more}.]"
        )


def derive_single_source_narrowing(
    coverage: RequestCoverage, resolution: NamedSourceResolution | None, caller_scope: str,
) -> SingleSourceNarrowing | None:
    """The narrowing for this run, or ``None`` when the rule does not concern the request (it does not narrow to one
    source: :attr:`RequestCoverage.narrows_to_one_source`). A request it concerns always gets a verdict.

    An applied verdict reports ``steps_not_run`` as its confinement until the caller has asked the engine whether it
    applies the chunk filter (``vector_search_chash_allow_supported``) and the primary read has the manifest's hashes
    (:meth:`SingleSourceNarrowing.with_primary` records the mode the run settled on)."""
    if not coverage.narrows_to_one_source:
        return None
    ref = coverage.named_sources[0]
    base = {"source": ref.value, "caller_scope": caller_scope, "effective_scope": caller_scope}
    resolved = [s for s in (resolution.sources if resolution is not None else ()) if s.status == "resolved"]
    if len(resolved) != 1:
        state = "more than one document resolved" if resolved else "the source did not resolve to one document"
        return SingleSourceNarrowing(False, reason=f"{state}, so there is no document to confine to", **base)
    source = resolved[0]
    collection, tumbler = source.collection, source.tumbler or ""
    owners = owner_tumblers(tumbler)
    if not collection or not owners:
        return SingleSourceNarrowing(
            False, tumbler=tumbler, collection=collection,
            reason="the resolved document has no collection or owner address", **base,
        )
    if source.inferred and source.foreign > 0:
        return SingleSourceNarrowing(
            False, tumbler=tumbler, collection=collection, owner=owners[0],
            reason=(
                f"the session's project was inferred and chose {tumbler}, but {source.foreign} document(s) in other "
                "projects carry the same number, so the choice may be wrong; name the project (the owner binding) to "
                "narrow the answer to one document"
            ), **base,
        )
    scope = ScopeConstraint.parse(caller_scope)
    admitted = scope.kind == "none" or scope.value == "all" or scope.admits(collection, tumbler=tumbler)
    if not admitted:
        return SingleSourceNarrowing(
            False, tumbler=tumbler, collection=collection, owner=owners[0],
            reason=f"the caller's scope {caller_scope!r} does not admit {collection}, and a scope is never replaced "
                   "by something it excludes", **base,
        )
    # A caller scope that is this document's own address is kept: it is already narrower than the collection.
    keeps_caller = scope.kind == "subtree" and scope.value == tumbler
    return SingleSourceNarrowing(
        True, tumbler=tumbler, collection=collection, owner=owners[0],
        step_scope=caller_scope if keeps_caller else collection,
        confinement="steps_not_run",
        **{**base, "effective_scope": tumbler},
    )


def unavailable_single_source_narrowing(coverage: RequestCoverage, caller_scope: str) -> SingleSourceNarrowing | None:
    """The verdict for a request the rule concerns when computing the real one raised: ``applied: false`` with the
    reason, so a faulted derivation is visible in the envelope and not indistinguishable from a request the rule does
    not concern. ``None`` when the rule does not concern the request (or that cannot be told). Never raises."""
    try:
        if not coverage.narrows_to_one_source:
            return None
        source = coverage.named_sources[0].value
    except Exception:  # noqa: BLE001 — the fallback of a fallback: nothing to report
        return None
    return SingleSourceNarrowing(
        False, source, caller_scope=caller_scope, effective_scope=caller_scope,
        reason="the narrowing could not be computed (an internal fault, logged), so retrieval was not narrowed",
    )
