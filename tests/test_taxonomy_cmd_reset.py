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

    def reset_collection(self, collection: str) -> dict[str, int]:
        self.reset_calls.append(collection)
        return {"topics": 2, "assignments": 7, "links": 1, "meta": 1, "centroids": 2}


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


def test_refusal_names_a_verb_that_exists() -> None:
    """THE reachability pin.

    Builds the refusal message the rebuild path raises, extracts the `nx
    taxonomy <verb>` it recommends, and asserts that verb is registered on the
    taxonomy group. Rewording the message to name a different verb, or deleting
    the verb, fails here — which is what the first version of this fix needed
    and did not have.
    """
    import numpy as np

    with pytest.raises(tc.MixedEmbeddingDimensionsError) as exc:
        tc.compute_rebuild_plan(
            "c__migrated",
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
    marker = "nx taxonomy "
    assert marker in msg, msg
    verb = msg.split(marker, 1)[1].split()[0].strip("`.,")
    assert verb in taxonomy.commands, (
        f"the refusal tells the operator to run `nx taxonomy {verb}`, which is "
        f"not a registered verb; available: {sorted(taxonomy.commands)}"
    )


def test_reset_requires_confirmation_and_reports_what_it_will_discard(wired) -> None:
    result = CliRunner().invoke(taxonomy, ["reset", "-c", "c"], input="n\n")
    assert result.exit_code != 0, "declining must not reset"
    assert wired.reset_calls == []
    # The accepted-label count is the thing being weighed, so it is shown
    # BEFORE the prompt rather than left to be inferred.
    assert "1 of them operator-accepted" in result.output, result.output
    assert "documents and chunks are not touched" in result.output, result.output


def test_reset_proceeds_on_yes_and_reports_counts(wired) -> None:
    result = CliRunner().invoke(taxonomy, ["reset", "-c", "c", "--yes"])
    assert result.exit_code == 0, result.output
    assert wired.reset_calls == ["c"]
    assert "2 topics" in result.output and "2 centroids removed" in result.output
    assert "discover --collection c" in result.output, "the way back must be named"


def test_reset_on_a_collection_with_no_taxonomy_is_a_no_op(wired) -> None:
    result = CliRunner().invoke(taxonomy, ["reset", "-c", "empty", "--yes"])
    assert result.exit_code == 0, result.output
    assert wired.reset_calls == []
    assert "No taxonomy to reset" in result.output


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
