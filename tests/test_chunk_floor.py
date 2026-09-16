# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-x50jb: the minimum-chunk-size floor, as a merge PLAN.

WHY A PLAN RATHER THAN A FILTER. The three chunkers return three different
types -- ``chunker.chunk_file`` a ``list[dict]``, ``md_chunker`` a list of
``MarkdownChunk`` dataclasses, ``pdf_chunker`` its own records -- and each
combines two adjacent spans differently (line ranges, header stacks, page
numbers). So the shared piece is the POLICY (which spans are too short, and
which neighbour absorbs them), and each chunker materializes the plan with its
own metadata rules. One definition of the policy, three materializations.

WHY MERGE RATHER THAN DROP (Sam, 2026-09-16). Nothing leaves the corpus: a
bare ``}`` is appended to the chunk before it rather than deleted. That keeps
manifest positions contiguous, keeps the ``position``-0 invariant
(``_CombinedWritePositionZeroViolation``) intact without renumbering, and makes
the threshold a cheap decision -- getting it wrong costs a slightly longer
neighbour, never a span that cannot be found again.

WHAT THIS IS FOR. Degenerate chunks are embedded and stored as searchable
vectors today, which is Voyage spend and index space on content that can never
be a useful retrieval hit. They also weld the chash graph together: identical
chunk text in one collection collapses to ONE ``nexus.chunks`` row, so a bare
docstring delimiter shared by 463 documents makes those 463 one connected
component. Measured on the live code corpus 2026-09-16 (conexus-2e): top
bridges were a 3-char docstring delimiter across 463 documents, a 44-char SPDX
line across 207, a single close brace across 192, an 83-document open brace and
a 68-document ``try:``.
"""
from __future__ import annotations

import pytest

from nexus.chunk_floor import MIN_CHUNK_CHARS, plan_merges


def _flatten(plan: list[list[int]]) -> list[int]:
    return [i for group in plan for i in group]


class TestPlanIsAPartition:
    """Structural invariant: a plan never loses, duplicates or reorders a span."""

    @pytest.mark.parametrize(
        "texts",
        [
            [],
            ["x" * 100],
            ["}"],
            ["x" * 100, "}", "y" * 100],
            ["}", "{", "try:"],
            ["x" * 100, "y" * 100, "z" * 100],
            ["}", "x" * 100],
            ["x" * 100, "}"],
            ["a" * 70, "}", "{", "b" * 70, "try:"],
        ],
    )
    def test_every_index_appears_exactly_once_in_order(self, texts: list[str]) -> None:
        plan = plan_merges(texts)
        assert _flatten(plan) == list(range(len(texts))), (
            "a merge plan must be a partition of the input indices in order -- "
            "merging never drops a span (that is the drop design we did not "
            "build), never duplicates one, and never reorders them"
        )

    @pytest.mark.parametrize(
        "texts",
        [["x" * 100, "}"], ["}", "x" * 100], ["a" * 70, "}", "{", "b" * 70]],
    )
    def test_groups_are_contiguous_runs(self, texts: list[str]) -> None:
        for group in plan_merges(texts):
            assert group == list(range(group[0], group[-1] + 1)), (
                "a group must be a contiguous run: a span may only merge into an "
                "ADJACENT neighbour, never a distant one"
            )


class TestPolicy:
    def test_spans_at_or_above_the_floor_stand_alone(self) -> None:
        texts = ["x" * MIN_CHUNK_CHARS, "y" * (MIN_CHUNK_CHARS + 1)]
        assert plan_merges(texts) == [[0], [1]], (
            "the floor is inclusive at MIN_CHUNK_CHARS -- a span of exactly the "
            "threshold is long enough and is not merged"
        )

    def test_a_short_span_merges_into_the_previous(self) -> None:
        texts = ["x" * 100, "}", "y" * 100]
        assert plan_merges(texts) == [[0, 1], [2]], (
            "the close brace joins the chunk before it, which is where it came "
            "from in the source"
        )

    def test_a_short_FIRST_span_merges_forward(self) -> None:
        texts = ["}", "x" * 100]
        assert plan_merges(texts) == [[0, 1]], (
            "the first span has no previous, so it merges FORWARD rather than "
            "being dropped or left as a degenerate chunk"
        )

    def test_a_lone_short_span_stands_alone(self) -> None:
        assert plan_merges(["}"]) == [[0]], (
            "a document whose entire content is below the floor is still a "
            "document -- there is no neighbour to merge into, and merging is not "
            "dropping, so it is indexed as-is"
        )

    def test_consecutive_short_spans_collapse_into_one_group(self) -> None:
        texts = ["x" * 100, "}", "{", "try:", "y" * 100]
        assert plan_merges(texts) == [[0, 1, 2, 3], [4]]

    def test_all_short_collapses_to_a_single_group(self) -> None:
        assert plan_merges(["}", "{", "try:"]) == [[0, 1, 2]]

    def test_empty_input(self) -> None:
        assert plan_merges([]) == []


class TestTheMeasuredBridges:
    """The spans that actually welded the live corpus together."""

    def test_the_real_bridge_texts_are_all_absorbed(self) -> None:
        spdx = "# SPDX-License-Identifier: AGPL-3.0-or-later"
        assert len(spdx) == 44, "fixture drift: the measured SPDX line is 44 chars"
        body = "x" * 200
        texts = [spdx, body, '"""', body, "}", body, "try:"]
        plan = plan_merges(texts)
        # Every degenerate span ends up sharing a group with a real one; none is
        # left as a standalone chunk, so none becomes its own chunks row and none
        # can bridge two documents.
        standalone_degenerate = [
            g[0] for g in plan if len(g) == 1 and len(texts[g[0]]) < MIN_CHUNK_CHARS
        ]
        assert standalone_degenerate == [], (
            "every measured bridge (SPDX 44, docstring delimiter 3, brace 1, "
            f"try: 4) must be absorbed at MIN_CHUNK_CHARS={MIN_CHUNK_CHARS}; "
            f"these were left standalone: {standalone_degenerate}"
        )

    def test_the_spdx_copyright_block_survives_on_its_own(self) -> None:
        block = (
            "# SPDX-License-Identifier: AGPL-3.0-or-later\n"
            "# Copyright (c) 2026 Hal Hildebrand. All rights reserved."
        )
        assert len(block) > MIN_CHUNK_CHARS, "fixture drift: the measured block is 102 chars"
        assert plan_merges([block, "x" * 200]) == [[0], [1]], (
            "the 102-char SPDX+copyright block is ABOVE the floor and stays its "
            "own chunk -- the threshold was chosen to catch the bare 44-char "
            "line, not every licence text"
        )

