# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 3 Step 3.5 (nexus-rfvyy.8.6): ``nx taxonomy discover`` leaves sensitive members out of term
computation, and the Knowledge Map carries no term that only a sensitive document has.

The flow under test (census rows 9 to 11): ``discover_for_collection`` fetches the collection's chunks, the
store clusters them and names each topic by its c-TF-IDF top terms, ``generate_context_l1`` writes the
topic labels into the Knowledge Map cache, and the SubagentStart hook reads that cache file. A term only a
sensitive chunk carries reaches the map only if the chunk reached the vectorizer, so the exclusion sits at
the fetch: ``sensitivity="exclude"`` on the collection read.

The stub below honours ``sensitivity`` the way the engine does (``exclude`` drops a chunk with a live
sensitive owner inside the read, ``include`` returns every chunk). The compute half is the real
``HttpTaxonomyStore`` static, not a fake, so the test sees the labels and terms production would persist.

NON-VACUITY: ``test_the_fixture_puts_the_token_into_the_terms_when_the_chunk_is_not_excluded`` computes over
the full corpus and asserts the token is in a topic's terms and label. A fixture where the token never
reaches the terms would pass every exclusion assertion below whatever the code did.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from nexus.commands.taxonomy_cmd import discover_for_collection
from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore

TOKEN = "zqxsensitivetoken"
COLLECTION = "knowledge__sensfix__bge-base-en-v15-768__v1"
PER_CLUSTER = 12

_A_WORDS = ["turbine", "blade", "rotor", "engine"]
_B_WORDS = ["orchard", "harvest", "apple", "cider"]


def _corpus() -> tuple[list[str], list[str], np.ndarray, set[str]]:
    """Two well-separated blobs of PER_CLUSTER chunks in 3-d; five chunks of blob A are sensitive and carries the
    token. Returns (ids, texts, embeddings, sensitive ids)."""
    rng = np.random.default_rng(243)
    ids: list[str] = []
    texts: list[str] = []
    vecs: list[np.ndarray] = []
    for name, words, center in (("a", _A_WORDS, [10.0, 0.0, 0.0]), ("b", _B_WORDS, [0.0, 10.0, 0.0])):
        for i in range(PER_CLUSTER):
            ids.append(f"{name}{i}")
            texts.append(f"{words[i % 4]} {words[(i + 1) % 4]} {name}notes")
            vecs.append(np.asarray(center) + rng.normal(0.0, 0.05, 3))
    # Five of blob A's twelve chunks, so the token is heavy enough to reach the 3-word label the map prints.
    sensitive = {f"a{i}" for i in range(5)}
    for i in sensitive:
        texts[ids.index(i)] = f"{TOKEN} {TOKEN} {TOKEN} confidential notes"
    return ids, texts, np.asarray(vecs, dtype=np.float32), sensitive


class _Stub:
    """The service collection stub: pages ``get`` and honours ``sensitivity`` like the engine."""

    def __init__(self) -> None:
        self.ids, self.texts, _, self.sensitive = _corpus()
        self.sensitivities: list[str] = []

    def get(self, *, ids: list[str] | None = None, include: list[str] | None = None, limit: int | None = None,
            offset: int = 0, sensitivity: str, **_kw: Any) -> dict[str, Any]:
        assert sensitivity in ("include", "exclude")
        self.sensitivities.append(sensitivity)
        rows = [(i, t) for i, t in zip(self.ids, self.texts)
                if (ids is None or i in ids) and not (sensitivity == "exclude" and i in self.sensitive)]
        rows = rows[offset:] if limit is None else rows[offset:offset + limit]
        return {"ids": [r[0] for r in rows], "documents": [r[1] for r in rows]}


class _T3:
    def __init__(self) -> None:
        self.stub = _Stub()
        self._vec = dict(zip(self.stub.ids, _corpus()[2]))

    def count(self, _collection: str) -> int:
        return len(self.stub.ids)  # the engine's count includes sensitive chunks

    def get_or_create_collection(self, _collection: str) -> _Stub:
        return self.stub

    def get_embeddings(self, _collection: str, ids: list[str], *, on_progress: Any = None) -> np.ndarray:
        return np.asarray([self._vec[i] for i in ids], dtype=np.float32)


class _ComputingTaxonomy:
    """Runs the REAL compute half on what the discover path hands the store, and records the inputs."""

    def __init__(self) -> None:
        self.inputs: list[tuple[list[str], list[str]]] = []
        self.specs: list[dict[str, Any]] = []

    def get_topics_for_collection(self, *_a: Any, **_kw: Any) -> list:
        return []

    def discover_topics(self, collection, doc_ids, embeddings, texts):  # noqa: ANN001, ANN201
        self.inputs.append((list(doc_ids), list(texts)))
        self.specs = HttpTaxonomyStore.compute_discovered_topics(collection, doc_ids, embeddings, texts)
        return len(self.specs)

    def rebuild_taxonomy(self, collection, doc_ids, embeddings, texts):  # noqa: ANN001, ANN201
        self.inputs.append((list(doc_ids), list(texts)))
        plan = HttpTaxonomyStore.compute_rebuild_plan(
            collection, doc_ids, embeddings, texts, old_centroids=np.empty((0, embeddings.shape[1]), dtype=np.float32),
            old_labels=[], old_review_statuses=[], old_centroid_topic_ids=[], manual_assignments={},
        )
        self.specs = plan["specs"]
        return len(self.specs)


def _all_terms(specs: list[dict[str, Any]]) -> str:
    return " ".join(spec["label"] + " " + " ".join(json.loads(spec["terms"])) for spec in specs)


def test_the_fixture_puts_the_token_into_the_terms_when_the_chunk_is_not_excluded() -> None:
    ids, texts, embeddings, _ = _corpus()
    specs = HttpTaxonomyStore.compute_discovered_topics(COLLECTION, ids, embeddings, texts)
    assert len(specs) == 2, "two blobs, two topics"
    assert TOKEN in _all_terms(specs), "the control: with the sensitive chunk in, its unique token is a term"
    assert any(TOKEN in spec["label"] for spec in specs), "and it reaches a topic LABEL, which is what the map prints"


@pytest.mark.parametrize("force", [False, True], ids=["discover", "discover-force-rebuild"])
def test_discover_terms_and_labels_carry_no_token_only_a_sensitive_chunk_has(force: bool) -> None:
    t3, tax = _T3(), _ComputingTaxonomy()
    created = discover_for_collection(COLLECTION, tax, t3, force=force, quiet=True)
    assert created == 2, "the non-sensitive chunks still form both topics"
    (doc_ids, texts), = tax.inputs
    assert not {f"a{i}" for i in range(5)} & set(doc_ids) and not any(TOKEN in t for t in texts), \
        "no sensitive chunk reached the store"
    assert len(doc_ids) == 2 * PER_CLUSTER - 5
    assert TOKEN not in _all_terms(tax.specs)
    assert sum(spec["doc_count"] for spec in tax.specs) == 2 * PER_CLUSTER - 5, "doc_count does not count them"
    assert set(t3.stub.sensitivities) == {"exclude"}, "every page of the fetch asked for exclude"


def test_the_knowledge_map_cache_and_the_subagent_start_block_carry_no_sensitive_only_term(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from nexus.context import generate_context_l1
    from nexus.hooks.subagent_start import _knowledge_map_section

    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("nexus.mcp_infra.get_collection_row", lambda name: {"content_type": "knowledge"})

    t3, tax = _T3(), _ComputingTaxonomy()
    discover_for_collection(COLLECTION, tax, t3, quiet=True)

    class _Topics:
        def get_topics(self, parent_id=None):  # noqa: ANN001, ANN201
            return [{"collection": COLLECTION, "label": s["label"], "doc_count": s["doc_count"]} for s in tax.specs]

    repo_root = str(tmp_path / "somerepo")
    cache = tmp_path / "context" / f"somerepo-{hashlib.sha1(repo_root.encode()).hexdigest()[:8]}.txt"  # noqa: S324 — the hook's cache key
    assert generate_context_l1(_Topics(), output_path=cache) == cache
    written = cache.read_text(encoding="utf-8")
    assert "## Knowledge Map" in written and "knowledge:" in written, "the map was written, so the absence below is not vacuous"
    block = _knowledge_map_section(repo_root)
    assert "## Knowledge Map" in block
    assert TOKEN not in written and TOKEN not in block


def test_split_fetch_excludes_the_sensitive_member() -> None:
    """The sibling that computes terms the same way: ``split_topic`` reads its members through
    ``_svc_fetch_by_ids``. A sensitive member is left out of the texts; ``split_topic``'s conservation guard
    then refuses the split (the parent's assignments stay untouched) rather than persist a split that dropped
    a member."""
    t3 = _T3()
    ids, texts, embeddings = HttpTaxonomyStore._svc_fetch_by_ids(t3, COLLECTION, ["a5", "a3", "a6"])
    assert ids == ["a5", "a6"] and not any(TOKEN in t for t in texts)
    assert embeddings is not None and len(embeddings) == 2
    assert set(t3.stub.sensitivities) == {"exclude"}
