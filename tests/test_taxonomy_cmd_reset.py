# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""`nx taxonomy reset` — the exit the cross-space rebuild refusal names.

nexus-dtqd7 follow-up. The refusal in compute_rebuild_plan tells the operator
to either re-embed or discard the taxonomy on purpose. The second half named a
verb that did not exist: HttpTaxonomyStore.purge_collection had zero CLI
callers, and the only reachable "purge" (`nx collection delete`) destroys the
collection's documents. Both reviewers of bad1e8348 caught it independently.

The reachability test below is the point of this file: a refusal that names an
unreachable remedy is a dead end wearing the shape of an exit, and prose in a
review cannot stop that regressing.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from nexus.commands.taxonomy_cmd import taxonomy
from nexus.db.t2 import taxonomy_compute as tc


class _FakeTaxonomy:
    def __init__(self, topics: list[dict]) -> None:
        self._topics = topics
        self.reset_calls: list[str] = []

    def get_all_topics(self, collection: str = "", **_: object) -> list[dict]:
        return [t for t in self._topics if t.get("collection") == collection]

    centroids_left = 2

    def reset_collection(self, collection: str) -> dict[str, int]:
        self.reset_calls.append(collection)
        return {
            "topics": 2, "assignments": 7, "links": 1, "meta": 1,
            "centroids": self.centroids_left,
        }


class _FakeDB:
    def __init__(self, taxonomy_obj: _FakeTaxonomy) -> None:
        self.taxonomy = taxonomy_obj

    def __enter__(self):
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


@pytest.fixture
def wired(monkeypatch):
    fake = _FakeTaxonomy([
        {"id": 1, "collection": "c", "label": "curated", "review_status": "accepted"},
        {"id": 2, "collection": "c", "label": "auto", "review_status": "pending"},
    ])
    import nexus.commands.taxonomy_cmd as mod

    monkeypatch.setattr(mod, "_T2Database", lambda *a, **k: _FakeDB(fake))
    monkeypatch.setattr(mod, "_default_db_path", lambda: "unused")
    monkeypatch.setattr(mod, "_command_shared_t2_client", lambda: None)
    return fake


def test_the_refusals_printed_command_actually_runs(wired) -> None:
    """THE reachability pin, and it must exercise the command, not its name.

    The first version of this test extracted the verb token and asserted it was
    registered on the group. That passed while the printed command was
    `nx taxonomy reset <collection>` as a bare positional — which reset_cmd does
    not accept, since it binds collection through -c/--collection like every
    other taxonomy verb. So the refusal still named something an operator could
    not run, and the test could not see it: checking that a verb exists is not
    checking that the invocation parses (round-3 critique).

    This lifts the command out of the refusal verbatim and runs it.
    """
    import shlex

    import numpy as np

    with pytest.raises(tc.MixedEmbeddingDimensionsError) as exc:
        tc.compute_rebuild_plan(
            "c",
            ["a", "b"],
            np.zeros((2, 384), dtype=np.float32),
            ["x", "y"],
            old_centroids=np.ones((1, 768), dtype=np.float32),
            old_labels=["curated"],
            old_review_statuses=["accepted"],
            old_centroid_topic_ids=[1],
            manual_assignments={},
        )
    msg = str(exc.value)

    # Pull the backticked command out of the message rather than retyping it,
    # so a reworded remedy is exercised as written.
    commands = [seg for seg in msg.split("`") if seg.strip().startswith("nx ")]
    assert commands, f"the refusal names no runnable command: {msg}"

    for cmd in commands:
        argv = shlex.split(cmd)
        assert argv[:2] == ["nx", "taxonomy"], argv
        result = CliRunner().invoke(taxonomy, argv[2:] + ["--yes"])
        # Exit code 2 is click's usage error: unknown verb, bad option, or an
        # argument that does not bind. That is the failure this pins.
        assert result.exit_code != 2, (
            f"the refusal tells the operator to run `{cmd}`, which does not "
            f"parse: {result.output}"
        )


def test_reset_requires_confirmation_and_reports_what_it_will_discard(wired) -> None:
    result = CliRunner().invoke(taxonomy, ["reset", "-c", "c"], input="n\n")
    assert result.exit_code != 0, "declining must not reset"
    assert wired.reset_calls == []
    # The accepted-label count is the thing being weighed, so it is shown
    # BEFORE the prompt rather than left to be inferred.
    assert "1 of them operator-accepted" in result.output, result.output
    assert "documents and chunks are not touched" in result.output, result.output


def test_reset_no_longer_reaches_beyond_this_collection(wired) -> None:
    """nexus-0v0nj: reset now purges with scope="taxonomy_only"
    (HttpTaxonomyStore.reset_collection -> engine's purgeCollection with
    PURGE_SCOPE_TAXONOMY_ONLY), which omits the SOURCE_COLLECTION delete —
    so it no longer removes this collection's documents' projections onto
    OTHER collections' topics, and the disclosure that used to warn about
    that widening is gone with it. This replaces
    test_reset_discloses_that_it_reaches_beyond_this_collection, which
    pinned the OLD (strictly-more-destructive-than-a-rebuild) behavior this
    bead fixed.
    """
    result = CliRunner().invoke(taxonomy, ["reset", "-c", "c"], input="n\n")
    assert "cross-collection projections" not in result.output, result.output
    assert "other collections' topics" not in result.output, result.output


def test_reset_proceeds_on_yes_and_reports_counts(wired) -> None:
    result = CliRunner().invoke(taxonomy, ["reset", "-c", "c", "--yes"])
    assert result.exit_code == 0, result.output
    assert wired.reset_calls == ["c"]
    assert "2 topics" in result.output and "2 centroids removed" in result.output
    assert "discover --collection c" in result.output, "the way back must be named"


def test_reset_with_no_topics_still_discloses_and_prompts(wired) -> None:
    """The no-topics path is NOT a quiet sweep.

    Round 3 gave it its own branch: reset_collection called unconditionally,
    no disclosure, no prompt (--yes had nothing to skip), centroids-only
    reporting. Pre-nexus-0v0nj, purgeCollection ALSO deleted topic_assignments
    by SOURCE_COLLECTION whether or not this collection owned topics, so a
    collection with zero topics could still have outbound projections to
    destroy — and that branch destroyed them silently, reproducing in new
    code the defect the rest of this command exists to fix (round-4 review).
    One path now, and it still prompts even with nothing of its own to show:
    an interrupted prior reset can leave partial state behind regardless of
    topic count.

    nexus-0v0nj: the SOURCE_COLLECTION widening this test's docstring
    described is gone (reset purges with scope="taxonomy_only" now), so the
    disclosure it used to require no longer applies here either.
    """
    result = CliRunner().invoke(taxonomy, ["reset", "-c", "empty"], input="n\n")
    assert result.exit_code != 0, "declining must not reset"
    assert wired.reset_calls == [], "a declined reset must touch nothing"
    assert "owns no topics" in result.output, result.output
    assert "cross-collection projections" not in result.output, result.output


def test_reset_with_no_topics_reports_every_count_not_just_centroids(wired) -> None:
    """"Nothing to reset" was printable after destroying assignments, because
    the branch reported centroids alone."""
    result = CliRunner().invoke(taxonomy, ["reset", "-c", "empty", "--yes"])
    assert result.exit_code == 0, result.output
    assert wired.reset_calls == ["empty"]
    out = result.output
    assert "assignments" in out and "links" in out and "centroids" in out, out
    assert "Nothing to reset" not in out, out


def test_reset_collection_clears_both_halves() -> None:
    """The store-level composition: T2 rows AND centroids.

    purge_collection alone leaves the centroids, and a reset that leaves them
    silently does not take -- the next rebuild reads them back.
    """
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore

    calls: list[str] = []

    class _Centroid:
        def purge(self, collection: str) -> int:
            calls.append(f"centroid:{collection}")
            return 3

    class _Store(HttpTaxonomyStore):
        # _centroid is a property on the real class, so it is overridden here
        # rather than assigned; the point of the test is the composition
        # reset_collection performs, not how the port is obtained.
        @property
        def _centroid(self):  # type: ignore[override]
            return _Centroid()

        def purge_collection(self, collection: str, *, scope: str = "full") -> dict[str, int]:
            calls.append(f"t2:{collection}:{scope}")
            return {"topics": 1, "assignments": 2, "links": 0, "meta": 1}

    store = object.__new__(_Store)
    out = store.reset_collection("c")
    # nexus-0v0nj: reset_collection purges with the taxonomy-only scope, not
    # the (pre-existing) full default -- it must not reach beyond this
    # collection's own topics.
    assert calls == ["t2:c:taxonomy_only", "centroid:c"], calls
    assert out["centroids"] == 3
    assert out["topics"] == 1
