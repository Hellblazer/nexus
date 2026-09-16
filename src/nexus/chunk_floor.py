# SPDX-License-Identifier: AGPL-3.0-or-later
"""Minimum-chunk-size floor: which spans are too short to stand alone (nexus-x50jb).

A chunker emits spans; some of them are degenerate — a bare ``}``, a lone
docstring delimiter, an SPDX line. Embedding those costs money and index space
for content that can never be a useful retrieval hit, and worse, they WELD THE
CHASH GRAPH TOGETHER: identical chunk text in one collection collapses to a
single ``nexus.chunks`` row by design, so one bare delimiter shared by 463
documents makes those 463 a single connected component under any relation that
follows shared chunks.

Measured on the live code corpus, 2026-09-16 (conexus-2e), top bridges by
documents sharing them: a 3-char docstring delimiter across 463, a 44-char SPDX
line across 207, a single close brace across 192, an open brace across 83, a
bare ``try:`` across 68. Prose barely produces these at all — the docs corpus's
largest component is 18 documents — because prose does not emit braces.

THIS MODULE IS POLICY ONLY. It decides WHICH spans merge into which neighbour
and returns a plan; it never touches a span itself. The three chunkers return
three different types (``chunker.chunk_file`` a ``list[dict]``, ``md_chunker``
a list of ``MarkdownChunk``, ``pdf_chunker`` its own records) and each combines
two adjacent spans differently — line ranges, header stacks, page numbers — so
the mechanics belong to them and only the policy is shared. That split is
deliberate: one definition of the rule, three materializations of it, rather
than three copies of the rule.

MERGE, NEVER DROP (Sam's ruling, 2026-09-16). A short span joins a neighbour
instead of being deleted. Nothing leaves the corpus, manifest positions stay
contiguous with no renumbering, the ``position``-0 invariant
(``_CombinedWritePositionZeroViolation``, ``indexer.py``) is untouched, and the
threshold becomes a cheap decision: getting it wrong costs a slightly longer
neighbouring chunk, never a span nobody can find again. That is why the floor
can sit as high as 64 characters without being reckless.

APPLIES AT INDEX TIME ONLY. A floor changes what gets chunked, so existing
corpora keep their degenerate rows until they are re-indexed. This module does
not clean anything up and is not a migration.
"""

from __future__ import annotations

from collections.abc import Sequence

#: Spans shorter than this many characters merge into a neighbour rather than
#: standing as their own chunk. 64 catches every measured bridge including the
#: bare 44-char SPDX line, while leaving the 102-char SPDX-plus-copyright block
#: — which is a real, findable licence header — as its own chunk. Safe at this
#: height only BECAUSE the disposition is merge: nothing is lost, so an
#: over-eager floor costs a longer neighbour rather than missing content.
MIN_CHUNK_CHARS: int = 64


def plan_merges(
    texts: Sequence[str], min_chars: int = MIN_CHUNK_CHARS
) -> list[list[int]]:
    """Group span indices so no group is a lone span shorter than *min_chars*.

    Returns a PARTITION of ``range(len(texts))`` into contiguous ascending
    runs: every index appears exactly once, in order, and a group is always a
    contiguous run because a span may only merge into an adjacent neighbour.
    ``[[0], [1, 2], [3]]`` means span 2 merges into span 1 and the others stand
    alone. The caller concatenates each group per its own metadata rules.

    The rules, in the order they apply:

    * A span of ``min_chars`` or more stands alone. The floor is inclusive.
    * A shorter span joins the PREVIOUS group — where it came from in the
      source, so a trailing brace stays with the body it closes.
    * A shorter span with no previous group (the first span) merges FORWARD
      into what follows, since merging is not dropping and it has to go
      somewhere.
    * A lone short span stands alone. A document whose entire content is below
      the floor is still a document, and there is no neighbour to merge into.

    A group may remain below ``min_chars`` when every span in the input is
    short — they collapse into one group and that group is the whole document.
    The function makes no attempt to reach the floor by absorbing further, so
    it always terminates and never merges across a long span.

    CODE ONLY, for now. Markdown was wired to this and BACKED OUT TWICE, and the
    second attempt is the instructive one: a length floor collapses a short
    section and destroys its ``header_path``, so the floor was replaced with a
    precise bare-heading predicate — and that broke two further things a length
    floor never would. Merging ``## Parent`` forward into a ``### Child``
    section produced a chunk holding the child's content while claiming
    ``header_path=['Parent']``, and merging any heading into a shared paragraph
    stopped that paragraph being byte-identical across files, breaking the
    cross-file chunk dedup ``test_prose_indexer_collapses_shared_paragraph_
    across_files`` pins. Prose was never where the problem was — its largest
    shared-chash component is 18 documents against code's 919 — so the floor
    stays on the corpus that measured a problem. The history is in git if
    markdown is ever revisited.
    """
    if not texts:
        return []

    plan: list[list[int]] = []
    for i, text in enumerate(texts):
        if not plan:
            plan.append([i])
            continue
        open_group = plan[-1]
        open_len = sum(len(texts[j]) for j in open_group)
        if len(text) < min_chars:
            # Short: join the group before it, where it came from in the source.
            open_group.append(i)
        elif open_len < min_chars:
            # THE FORWARD MERGE. This span is long enough to stand, but the
            # group before it is not — which happens when the FIRST span was
            # short and had no previous to join. Absorb forward rather than
            # leaving that group as a degenerate chunk.
            open_group.append(i)
        else:
            plan.append([i])
    return plan
