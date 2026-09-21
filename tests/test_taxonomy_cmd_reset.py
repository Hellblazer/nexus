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


def test_reset_discloses_that_it_reaches_beyond_this_collection(wired) -> None:
    """The engine's purge deletes topic_assignments by SOURCE_COLLECTION as well
    as by topic id (TaxonomyRepository.purgeCollection), so it removes this
    collection's documents' projections onto OTHER collections' topics — which a
    rebuild would have kept. reset is therefore strictly more destructive than
    the operation it is offered as the alternative to, and the operator has to
    be told BEFORE the prompt, not discover it from the counts afterwards.
    """
    result = CliRunner().invoke(taxonomy, ["reset", "-c", "c"], input="n\n")
    assert "cross-collection projections" in result.output, result.output
    assert "other collections' topics" in result.output, result.output
    # and the way to rebuild them, since naming a loss without a remedy is the
    # dead end this whole verb exists to avoid
    assert "nx taxonomy project" in result.output, result.output


def test_reset_proceeds_on_yes_and_reports_counts(wired) -> None:
    result = CliRunner().invoke(taxonomy, ["reset", "-c", "c", "--yes"])
    assert result.exit_code == 0, result.output
    assert wired.reset_calls == ["c"]
    assert "2 topics" in result.output and "2 centroids removed" in result.output
    assert "discover --collection c" in result.output, "the way back must be named"


def test_reset_with_no_topics_still_sweeps_orphaned_centroids(wired) -> None:
    """The recovery case, and the reason this is not an early return.

    reset_collection is two calls — the T2 purge then the centroid purge — so a
    failure between them leaves topics gone and centroids behind. Gating the
    no-op on topics alone made the one verb built to remove those centroids
    answer "nothing to do", leaving them unreachable through it (round-3
    review). It must fall through and finish its own interrupted work.
    """
    wired.centroids_left = 3
    result = CliRunner().invoke(taxonomy, ["reset", "-c", "empty", "--yes"])
    assert result.exit_code == 0, result.output
    assert wired.reset_calls == ["empty"], "the sweep must actually run"
    assert "Swept 3 orphaned centroid(s)" in result.output, result.output


def test_reset_on_a_genuinely_clean_collection_says_so(wired) -> None:
    wired.centroids_left = 0
    result = CliRunner().invoke(taxonomy, ["reset", "-c", "empty", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Nothing to reset" in result.output, result.output


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

        def purge_collection(self, collection: str) -> dict[str, int]:
            calls.append(f"t2:{collection}")
            return {"topics": 1, "assignments": 2, "links": 0, "meta": 1}

    store = object.__new__(_Store)
    out = store.reset_collection("c")
    assert calls == ["t2:c", "centroid:c"], calls
    assert out["centroids"] == 3
    assert out["topics"] == 1
