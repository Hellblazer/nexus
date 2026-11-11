# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-243 Phase 1 Step 4c (nexus-rfvyy.6.15): document gates for bib enrichment, the DEVONthink
CrossRef gap-fill and the taxonomy sample names (census rows 7-10).

Against the real engine substrate and the real catalog: documents are seeded with a live manifest
and a chunk carrying a DOI and an arXiv id, one is flagged through ``set_sensitivity``, and each
surface runs over the pair. The outbound side is an ``httpx`` mock transport (Semantic Scholar,
OpenAlex), a recorder at the DEVONthink CrossRef seam and a capturing ``claude_dispatch``. Every
case sends the plain control document through the same surface and asserts it WAS looked up, so a
gate that refuses everything, or a surface that never ran, does not pass.

These gates are document-keyed and run under every posture: Semantic Scholar, OpenAlex and CrossRef
are external whether or not a model is involved.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from nexus.commands import enrich as enrich_mod
from nexus.commands import taxonomy_cmd

pytestmark = pytest.mark.usefixtures("t2_service_env")

MODEL_BGE = "bge-base-en-v15-768"


@pytest.fixture(autouse=True)
def _no_document_is_sensitive() -> None:
    """Overrides tests/conftest.py's fixture of this name: this module's subject is the gates, so it
    reads the real catalog."""


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, level: str):
        return lambda event, **kw: self.events.append((event, kw))

    def named(self, event: str) -> list[dict[str, Any]]:
        return [kw for name, kw in self.events if name == event]


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> _Log:
    log = _Log()
    for target in ("nexus.commands.enrich._log", "nexus.commands.taxonomy_cmd._log", "nexus.doc_sensitivity._log"):
        monkeypatch.setattr(target, log)
    return log


# ── The corpus ─────────────────────────────────────────────────────────────────────────────────


class _Doc(SimpleNamespace):
    tumbler: str
    chash: str
    title: str
    doi: str
    arxiv: str


def _seed(collection: str, label: str, doi: str, arxiv: str, *, sensitive: bool) -> _Doc:
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    from tests._catalog_fixture_ops import give_chunks_a_live_owner
    from tests._chunk_seed import seed_chunks_direct

    title = f"CANARY-TITLE-{label}-{uuid.uuid4().hex[:10]}"
    text = f"{title}. Abstract. We study lease renewal. doi: {doi}. arXiv:{arxiv}. References follow."
    chash = hashlib.sha256(f"{collection}:{text}".encode()).hexdigest()
    seed_chunks_direct(
        collection, ids=[chash], documents=[text],
        metadatas=[{"chunk_text_hash": chash, "title": title}],
    )
    # A paper: `nx enrich bib` looks up paper documents only by default (GH #1611 item 5).
    tumbler = give_chunks_a_live_owner(
        collection, [chash], title=title, owner_name=f"owner-{label}", content_type="paper",
    )
    if sensitive:
        with HttpCatalogClient() as cat:
            assert cat.set_sensitivity([tumbler], True, reason="RDR-243 gate test")["updated"] == [1]
    return _Doc(tumbler=tumbler, chash=chash, title=title, doi=doi, arxiv=arxiv)


@pytest.fixture
def corpus() -> tuple[str, _Doc, _Doc]:
    tag = uuid.uuid4().hex[:8]
    collection = f"knowledge__bibgate-{tag}__{MODEL_BGE}__v1"
    sensitive = _seed(collection, "sens", f"10.5555/sens.{tag}", "2501.11111", sensitive=True)
    control = _seed(collection, "ctrl", f"10.5555/ctrl.{tag}", "2501.22222", sensitive=False)
    return collection, sensitive, control


@pytest.fixture
def outbound(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every non-engine HTTP request, as a JSON string (method, url, body). The engine is left alone."""
    import os
    from urllib.parse import urlsplit

    seen: list[str] = []
    mock = httpx.MockTransport(
        lambda request: (
            seen.append(json.dumps({"method": request.method, "url": str(request.url), "body": request.read().decode(errors="replace")}))
            or httpx.Response(200, json={"data": [], "results": [], "total": 0, "meta": {"count": 0}})
        )
    )
    engine = urlsplit(os.environ["NX_SERVICE_URL"]).netloc
    real_send = httpx.Client.send

    def send(self: httpx.Client, request: httpx.Request, **kw: Any) -> httpx.Response:
        if request.url.netloc.decode("ascii") == engine:
            return real_send(self, request, **kw)
        response = mock.handle_request(request)
        response.request = request
        response.read()
        return response

    monkeypatch.setattr(httpx.Client, "send", send)
    return seen


# ── Row 7: nx enrich bib ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("source", ["s2", "openalex"])
def test_enrich_bib_sends_nothing_of_a_sensitive_document_and_counts_the_skip(
    corpus: tuple[str, _Doc, _Doc], outbound: list[str], logs: _Log, source: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    collection, sensitive, control = corpus

    enrich_mod.run_bib_enrichment(collection, delay=0, source=source)

    sent = "\n".join(outbound)
    for secret in (sensitive.title, sensitive.doi, sensitive.arxiv, sensitive.doi.upper()):
        assert secret not in sent, f"{source}: {secret!r} reached the backend"
    assert sensitive.tumbler not in sent
    assert any(control.title in r or control.doi in r or control.arxiv in r for r in outbound), (
        f"{source}: the plain control document WAS looked up (the surface ran)"
    )
    skipped = logs.named("enrich_bib_skipped_sensitive")
    assert [s["tumbler"] for s in skipped] == [sensitive.tumbler] and skipped[0]["surface"] == "enrich_bib"
    out = capsys.readouterr().out
    assert "1 sensitive skipped" in out, out
    assert sensitive.title not in out, "the skip line names the tumbler, never the title"


# ── Row 8: the DEVONthink CrossRef gap-fill ────────────────────────────────────────────────────


def test_dt_crossref_gapfill_skips_a_sensitive_document_loudly(
    corpus: tuple[str, _Doc, _Doc], outbound: list[str], logs: _Log, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    collection, sensitive, control = corpus
    dois: list[str] = []
    monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda: True)
    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_resolve_doi", lambda doi: dois.append(doi) or None)

    enrich_mod.run_bib_enrichment(collection, delay=0, source="dt")

    assert dois == [control.doi], f"CrossRef saw the control DOI only: {dois}"
    assert sensitive.doi not in "\n".join(outbound)
    skipped = logs.named("enrich_dt_crossref_skipped_sensitive")
    assert [s["tumbler"] for s in skipped] == [sensitive.tumbler]
    assert skipped[0]["surface"] == "dt_crossref_gapfill"
    out = capsys.readouterr().out
    assert f"skipped DT-CrossRef gap-fill for sensitive {sensitive.tumbler}" in out, out


# ── Rows 9 and 10: taxonomy sample names ───────────────────────────────────────────────────────


class _Taxonomy:
    """The taxonomy store, with the reads the label and review passes make."""

    def __init__(
        self, topics: list[dict[str, Any]], members: dict[int, list[str]], projection: dict[str, int] | None = None,
    ) -> None:
        self._topics = topics
        self._members = members
        self._projection = projection or {}
        self.labels: dict[int, str] = {}
        self.reviewed: dict[int, str] = {}

    def get_unreviewed_topics(self, collection: str = "", limit: int = 0) -> list[dict[str, Any]]:
        return list(self._topics)

    def get_all_topics(self, collection: str = "") -> list[dict[str, Any]]:
        return list(self._topics)

    def get_topic_doc_ids(self, topic_id: int, *, limit: int = 3) -> list[str]:
        return self._members[topic_id][:limit]

    def get_all_topic_doc_ids(self, topic_id: int) -> list[str]:
        return list(self._members[topic_id])

    def get_projection_counts_by_collection(self) -> dict[str, int]:
        return dict(self._projection)

    def update_topic_label(self, topic_id: int, label: str) -> None:
        self.labels[topic_id] = label

    def rename_topic(self, topic_id: int, label: str) -> None:
        self.labels[topic_id] = label

    def mark_topic_reviewed(self, topic_id: int, status: str) -> None:
        self.reviewed[topic_id] = status


def _topic(topic_id: int, collection: str, term: str) -> dict[str, Any]:
    terms = [f"{term}-a", f"{term}-b", f"{term}-c"]
    return {
        "id": topic_id, "label": " ".join(terms[:3]), "terms": json.dumps(terms),
        "collection": collection, "doc_count": 2, "review_status": "pending",
    }


@pytest.fixture
def taxonomy_world(
    corpus: tuple[str, _Doc, _Doc], monkeypatch: pytest.MonkeyPatch,
) -> tuple[_Taxonomy, list[str], tuple[str, _Doc, _Doc]]:
    """Topic 1 holds the sensitive document's chunk (and the control's); topic 2 holds the control's only.
    Returns the store, the captured model prompts and the corpus."""
    import nexus.mcp_infra as infra

    collection, sensitive, control = corpus
    store = _Taxonomy(
        [_topic(1, collection, "sensterm"), _topic(2, collection, "ctrlterm")],
        {1: [sensitive.chash, control.chash], 2: [control.chash]},
    )
    prompts: list[str] = []

    async def dispatch(prompt: str, schema: dict, **kw: Any) -> dict:
        prompts.append(prompt)
        if "labels" in schema.get("properties", {}):
            n = prompt.count("terms=[")
            return {"labels": [{"idx": i, "label": f"Label number {i}"} for i in range(1, n + 1)]}
        ids = [int(x) for x in __import__("re").findall(r"- id=(\d+)", prompt)]
        return {"verdicts": [{"id": i, "action": "accept"} for i in ids]}

    monkeypatch.setattr("nexus.operators.dispatch.claude_dispatch", dispatch)
    db = SimpleNamespace(taxonomy=store, memory=SimpleNamespace(get=lambda **kw: None))
    monkeypatch.setattr(infra, "t2_index_write", lambda fn, **kw: fn(db))
    monkeypatch.setattr(taxonomy_cmd, "_save_review_cache", lambda *a, **k: None)
    return store, prompts, corpus


def test_label_never_sends_a_topic_with_a_sensitive_member_and_keeps_its_terms_label(
    taxonomy_world: tuple[_Taxonomy, list[str], tuple[str, _Doc, _Doc]], logs: _Log,
) -> None:
    store, prompts, (collection, sensitive, control) = taxonomy_world

    count = taxonomy_cmd.relabel_topics(store, collection=collection, workers=1)

    joined = "\n".join(prompts)
    assert "sensterm" not in joined and sensitive.chash[:25] not in joined, "no term or name of topic 1 was sent"
    assert "ctrlterm" in joined, "the clean topic WAS dispatched (the surface ran)"
    assert count == 1 and set(store.labels) == {2}, f"topic 1 keeps its terms label: {store.labels}"
    skipped = logs.named("taxonomy_label_skipped_sensitive")
    assert [s["topic_id"] for s in skipped] == [1] and skipped[0]["surface"] == "taxonomy_label"
    assert sensitive.title not in json.dumps(skipped) and sensitive.tumbler not in json.dumps(skipped)


def test_review_auto_never_sends_a_topic_with_a_sensitive_member(
    taxonomy_world: tuple[_Taxonomy, list[str], tuple[str, _Doc, _Doc]], logs: _Log,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, prompts, (collection, sensitive, control) = taxonomy_world

    taxonomy_cmd._review_auto(
        SimpleNamespace(taxonomy=store, memory=SimpleNamespace(get=lambda **kw: None)),
        collection, limit=50, yes=True, dry_run=False, batch_size=10,
    )

    joined = "\n".join(prompts)
    assert "sensterm" not in joined and sensitive.chash[:25] not in joined
    assert "ctrlterm" in joined, "the clean topic WAS dispatched"
    assert store.reviewed == {2: "accepted"}, f"topic 1 stays pending: {store.reviewed}"
    skipped = logs.named("taxonomy_review_skipped_sensitive")
    assert [s["topic_id"] for s in skipped] == [1] and skipped[0]["surface"] == "taxonomy_review_auto"
    assert "1 skipped (sensitive member)" in capsys.readouterr().out


def test_taxonomy_fails_closed_when_the_catalog_cannot_say(
    taxonomy_world: tuple[_Taxonomy, list[str], tuple[str, _Doc, _Doc]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, prompts, (collection, _s, _c) = taxonomy_world

    def unreadable() -> Any:
        raise ConnectionError("catalog down")

    monkeypatch.setattr("nexus.doc_sensitivity._catalog_reader", unreadable)
    assert taxonomy_cmd.relabel_topics(store, collection=collection, workers=1) == 0
    assert prompts == [] and store.labels == {}, "nothing was sent on a guess"


# ── Cross-collection projection (fix round 1, nexus-rfvyy.6.20) ─────────────────────────────────


@pytest.mark.parametrize("own_collection_has_a_sensitive_document", [True, False], ids=["own-flagged", "own-clean"])
def test_a_topic_holding_a_sensitive_chunk_projected_from_another_collection_is_not_sent(
    monkeypatch: pytest.MonkeyPatch, logs: _Log, own_collection_has_a_sensitive_document: bool,
) -> None:
    import nexus.mcp_infra as infra

    tag = uuid.uuid4().hex[:8]
    collection = f"knowledge__projdst-{tag}__{MODEL_BGE}__v1"
    source = f"knowledge__projsrc-{tag}__{MODEL_BGE}__v1"
    clean_source = f"knowledge__projclean-{tag}__{MODEL_BGE}__v1"
    control = _seed(collection, "ctrl", f"10.5555/ctrl.{tag}", "2501.33333", sensitive=False)
    if own_collection_has_a_sensitive_document:
        _seed(collection, "own-sens", f"10.5555/own.{tag}", "2501.44444", sensitive=True)
    foreign = _seed(source, "foreign-sens", f"10.5555/foreign.{tag}", "2501.55555", sensitive=True)
    clean_foreign = _seed(clean_source, "foreign-clean", f"10.5555/fclean.{tag}", "2501.66666", sensitive=False)
    # Topic 1 holds a sensitive chunk projected in from `source`; topic 2 only a plain one projected in from a
    # source with no sensitive document; topic 3 only its own plain chunk.
    store = _Taxonomy(
        [_topic(1, collection, "projterm"), _topic(2, collection, "cleanprojterm"), _topic(3, collection, "ctrlterm")],
        {1: [control.chash, foreign.chash], 2: [clean_foreign.chash], 3: [control.chash]},
        projection={source: 1, clean_source: 1},
    )
    prompts: list[str] = []

    async def dispatch(prompt: str, schema: dict, **kw: Any) -> dict:
        prompts.append(prompt)
        n = prompt.count("terms=[")
        return {"labels": [{"idx": i, "label": f"Label number {i}"} for i in range(1, n + 1)]}

    monkeypatch.setattr("nexus.operators.dispatch.claude_dispatch", dispatch)
    db = SimpleNamespace(taxonomy=store, memory=SimpleNamespace(get=lambda **kw: None))
    monkeypatch.setattr(infra, "t2_index_write", lambda fn, **kw: fn(db))

    assert taxonomy_cmd._topics_with_sensitive_members(store, store.get_all_topics()) == {1}

    count = taxonomy_cmd.relabel_topics(store, collection=collection, workers=1)

    joined = "\n".join(prompts)
    assert "projterm" not in joined.replace("cleanprojterm", ""), "the topic with the projected sensitive chunk was sent"
    assert "cleanprojterm" in joined and "ctrlterm" in joined, "the other topics WERE dispatched (the surface ran)"
    assert count == 2 and set(store.labels) == {2, 3}, f"topic 1 keeps its terms label: {store.labels}"
