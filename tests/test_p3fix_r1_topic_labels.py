# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 3 fix round r1 (nexus-rfvyy.8), item 5: a pre-flag topic's label is unreachable.

A topic built before a document was flagged keeps a label computed from that document, and the Knowledge Map
(``generate_context_l1``, pasted into every subagent) and the MCP cluster headers print it. Setting the flag now
relabels every topic that holds a chunk of the document (and its ancestors) ``label withheld``, drops the cached
Knowledge Map files and says how to rebuild (``nx taxonomy discover --force``). Fakes stand in for the catalog and
taxonomy store: what is under test is which topics are relabelled and what the surfaces print afterwards.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus import sensitive_topics
from nexus.cli import main
from nexus.sensitive_topics import REDISCOVER_HINT, TOPIC_LABEL_WITHHELD

STALE = "merger acquisition plans"  # words of the sensitive document, in a label computed before it was flagged
CHASH_S1, CHASH_S2, CHASH_C = "a" * 64, "b" * 64, "c" * 64


class _Taxonomy:
    """Topic 10 holds the sensitive document's chunk (child of root 1); topic 20 holds only plain chunks."""

    def __init__(self) -> None:
        self.topics = {
            1: {"id": 1, "label": STALE, "parent_id": None, "collection": "knowledge__c", "doc_count": 9},
            10: {"id": 10, "label": f"{STALE} child", "parent_id": 1, "collection": "knowledge__c", "doc_count": 4},
            20: {"id": 20, "label": "gardening", "parent_id": None, "collection": "knowledge__c", "doc_count": 5},
        }
        self.assignments = {CHASH_S1: 10, CHASH_S2: 10, CHASH_C: 20}
        self.renamed: list[tuple[int, str]] = []

    def get_assignments_for_docs(self, doc_ids):
        return {d: self.assignments[d] for d in doc_ids if d in self.assignments}

    def get_topic_by_id(self, topic_id):
        return self.topics.get(topic_id)

    def update_topic_label(self, topic_id, label):
        self.renamed.append((topic_id, label))
        self.topics[topic_id] = {**self.topics[topic_id], "label": label}

    def get_topics(self, *, parent_id=None):
        return sorted((t for t in self.topics.values() if t["parent_id"] == parent_id), key=lambda t: -t["doc_count"])


class _Catalog:
    def get_manifests(self, tumblers):
        rows = {"1.1.5": [SimpleNamespace(chash=CHASH_S1, collection="knowledge__c"),
                          SimpleNamespace(chash=CHASH_S2, collection="knowledge__c")]}
        return {t: rows.get(t, []) for t in tumblers}


def _route_t2(monkeypatch, tax) -> None:
    """The module reaches T2 through ``nexus.mcp_infra.t2_index_write`` (the storage-boundary lint forbids a
    ``T2Database`` of its own); run each routed function against the fake taxonomy."""
    monkeypatch.setattr("nexus.mcp_infra.t2_index_write", lambda write_fn, **_kw: write_fn(SimpleNamespace(taxonomy=tax)))


def test_the_topics_holding_the_document_and_their_ancestors_are_relabelled_and_no_other(monkeypatch) -> None:
    tax = _Taxonomy()
    _route_t2(monkeypatch, tax)
    assert sensitive_topics.withhold_topic_labels(tax, _Catalog(), ["1.1.5"]) == [1, 10]
    assert sorted(tax.renamed) == [(1, TOPIC_LABEL_WITHHELD), (10, TOPIC_LABEL_WITHHELD)]
    assert tax.topics[20]["label"] == "gardening"


def test_a_document_in_no_topic_relabels_nothing(monkeypatch) -> None:
    tax = _Taxonomy()
    _route_t2(monkeypatch, tax)
    assert sensitive_topics.withhold_topic_labels(tax, _Catalog(), ["1.9.9"]) == []
    assert tax.renamed == []


def test_the_knowledge_map_no_longer_prints_the_stale_label(tmp_path, monkeypatch) -> None:
    from nexus import context

    monkeypatch.setattr(context, "_repo_collections", lambda _repo: None)
    monkeypatch.setattr("nexus.mcp_infra.get_collection_row", lambda _c: {"content_type": "knowledge"})
    tax = _Taxonomy()
    before = context.generate_context_l1(tax, output_path=tmp_path / "before.txt")
    assert STALE in before.read_text(encoding="utf-8")
    _route_t2(monkeypatch, tax)
    sensitive_topics.withhold_topic_labels(tax, _Catalog(), ["1.1.5"])
    after = context.generate_context_l1(tax, output_path=tmp_path / "after.txt")
    text = after.read_text(encoding="utf-8")
    assert STALE not in text and TOPIC_LABEL_WITHHELD in text and "gardening" in text


def test_the_cached_knowledge_map_files_are_dropped(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    ctx = tmp_path / "context"
    ctx.mkdir()
    (ctx / "repo-1234abcd.txt").write_text(f"## Knowledge Map\n\nknowledge: {STALE} (9)\n", encoding="utf-8")
    (tmp_path / "context_l1.txt").write_text(f"{STALE}\n", encoding="utf-8")
    (ctx / "keep.json").write_text("{}", encoding="utf-8")
    assert sensitive_topics.purge_knowledge_map_caches() == 2
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == ["keep.json"]


# ── the verb ─────────────────────────────────────────────────────────────────


class _Writer:
    def __init__(self) -> None:
        self.calls: list = []

    def set_sensitivity(self, batch, flag, *, reason=""):
        self.calls.append((list(batch), flag, reason))
        return {"targets": list(batch), "updated": [1 for _ in batch]}

    def close(self) -> None:
        return None


@pytest.fixture
def verb(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("nexus.catalog.http_catalog_client.require_catalog_sensitivity", lambda *_a, **_k: None)
    cat = SimpleNamespace(resolve=lambda t, follow_alias=True: object())
    cat.get_manifests = _Catalog().get_manifests
    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
    writer = _Writer()
    monkeypatch.setattr("nexus.commands.catalog._get_catalog_writer", lambda: writer)
    tax = _Taxonomy()

    _route_t2(monkeypatch, tax)
    ctx = tmp_path / "context"
    ctx.mkdir()
    (ctx / "r-1.txt").write_text(STALE, encoding="utf-8")

    def run(*args: str):
        return CliRunner().invoke(main, ["catalog", "sensitive", *args])

    run.tax, run.writer, run.ctx = tax, writer, ctx
    return run


def test_flagging_relabels_the_topics_drops_the_caches_and_says_how_to_rebuild(verb) -> None:
    result = verb("1.1.5")
    assert result.exit_code == 0, result.output
    assert verb.tax.topics[1]["label"] == TOPIC_LABEL_WITHHELD and verb.tax.topics[10]["label"] == TOPIC_LABEL_WITHHELD
    assert not list(verb.ctx.glob("*.txt"))
    assert "2 topic(s) hold chunks of these documents" in result.output
    assert "nx taxonomy discover --force" in result.output and REDISCOVER_HINT in result.output


def test_clearing_the_flag_touches_no_topic(verb) -> None:
    result = verb("1.1.5", "--clear", "--reason", "published")
    assert result.exit_code == 0, result.output
    assert verb.tax.renamed == [] and list(verb.ctx.glob("*.txt"))


def test_an_already_flagged_document_touches_no_topic(verb, monkeypatch) -> None:
    monkeypatch.setattr(verb.writer, "set_sensitivity", lambda batch, flag, *, reason="": {"targets": list(batch), "updated": [0]})
    assert verb("1.1.5").exit_code == 0
    assert verb.tax.renamed == []


def test_a_taxonomy_failure_leaves_the_flag_set_and_says_what_to_run(verb, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("taxonomy down")

    monkeypatch.setattr(verb.tax, "update_topic_label", boom)
    result = verb("1.1.5")
    assert result.exit_code == 0, result.output
    assert verb.writer.calls == [(["1.1.5"], True, "")]
    assert "could not be relabelled" in result.output and "nx taxonomy discover --force" in result.output


def test_json_carries_the_relabelled_topics(verb) -> None:
    import json

    result = verb("1.1.5", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["topics_relabelled"] == [1, 10]


# ── every path that flags an EXISTING document relabels its topics (fix r3, N1) ─────────────────────────────────


def _record_withhold(monkeypatch) -> list:
    """Replace the relabel with a recorder, so a path is judged by whether it asks for the relabel, not by the
    taxonomy store (whose behaviour the tests above own)."""
    calls: list = []
    monkeypatch.setattr(sensitive_topics, "withhold_after_flag", lambda cat, tumblers: calls.append(list(tumblers)) or [1])
    return calls


def test_raise_flag_relabels_the_topics_of_the_document_it_flagged(monkeypatch) -> None:
    from nexus.sensitive_write import raise_flag

    calls = _record_withhold(monkeypatch)
    writer = _Writer()
    raise_flag(writer, "1.1.5", what="a --sensitive index")
    assert writer.calls and calls == [["1.1.5"]]


def test_raise_flag_on_an_already_flagged_document_relabels_nothing(monkeypatch) -> None:
    from nexus.sensitive_write import raise_flag

    calls = _record_withhold(monkeypatch)
    writer = _Writer()
    writer.set_sensitivity = lambda batch, flag, *, reason="": {"targets": list(batch), "updated": [0]}
    raise_flag(writer, "1.1.5", what="a --sensitive index")
    assert calls == []


def test_a_relabel_that_fails_does_not_undo_the_flag_or_raise(monkeypatch) -> None:
    from nexus.sensitive_write import raise_flag

    def boom(cat, tumblers):
        raise RuntimeError("taxonomy down")

    monkeypatch.setattr(sensitive_topics, "withhold_after_flag", boom)
    writer = _Writer()
    raise_flag(writer, "1.1.5", what="a --sensitive index")  # does not raise: the flag is set
    assert writer.calls


def test_the_nxexp_import_of_a_sensitive_group_onto_an_existing_document_relabels_its_topics(monkeypatch) -> None:
    from nexus import exporter

    calls = _record_withhold(monkeypatch)
    monkeypatch.setattr("nexus.catalog.http_catalog_client.require_catalog_sensitivity", lambda *_a, **_k: None)
    writer = _Writer()
    exporter._raise_flag_if_needed(SimpleNamespace(tumbler="1.1.5", sensitive=False), True, object(), writer)
    assert writer.calls[0][0] == ["1.1.5"] and calls == [["1.1.5"]]

    # nothing to relabel when the document was already flagged, or the group is not sensitive
    exporter._raise_flag_if_needed(SimpleNamespace(tumbler="1.1.6", sensitive=True), True, object(), writer)
    exporter._raise_flag_if_needed(SimpleNamespace(tumbler="1.1.7", sensitive=False), False, object(), writer)
    assert calls == [["1.1.5"]]


def test_the_nxexp_import_relabel_failing_does_not_fail_the_import(monkeypatch) -> None:
    from nexus import exporter

    def boom(cat, tumblers):
        raise RuntimeError("taxonomy down")

    monkeypatch.setattr(sensitive_topics, "withhold_after_flag", boom)
    monkeypatch.setattr("nexus.catalog.http_catalog_client.require_catalog_sensitivity", lambda *_a, **_k: None)
    writer = _Writer()
    exporter._raise_flag_if_needed(SimpleNamespace(tumbler="1.1.5", sensitive=False), True, object(), writer)
    assert writer.calls
