# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 item 9 non-stranding guards, client half (P2.1, nexus-p2m1x.6).

KEEP THESE TESTS until P4.4 retires the name-parse fallbacks they guard.

RDR-229 retires name parsing as the way the client learns a collection's content type, owner and model: the
row carries them. Phase 2 repoints the readers to the row. Four code paths still receive a collection NAME that
has no ``catalog_collections`` row on this install, because the name comes from another install or an older
era, and the name-parse fallback is all they have: a recovery-bundle import, ``nx catalog migrate-fallback``,
the indexer's legacy-collection migration and the engine's ETL import (the engine half is
``service/src/test/java/dev/nexus/service/CatalogEtlImportNonStrandingTest.java``). A reader repoint that
removed the fallback before they had another source would strand a restore or an upgrade. Item 9: a reader may be
repointed only after every migration source it serves has a test that runs a row-less legacy name through it and
passes. These are those tests, written first, green on develop today.

Each test feeds a name with NO row on the target through the real source code path and asserts the target
collection and the attributes the source hands to the next stage: content type, owner, the TARGET install's
active embedding model, and the entity key and TTL where the source carries them. The names are built here by
hand, and a test may split a name itself (the census scans ``src/``, not ``tests/``); nothing here parses a name
for the code under test.

Tenant-free on purpose (``tests/storage_touch_ceiling.toml`` allows python_unit no new tenant test): a T3 with no
collection, a catalog fake, and the writer seams patched. What each guard stubs to prove it guards (a throwaway
pytest plugin kept outside the tree makes each name-parse or name-derivation function at its use site raise,
and the test goes red; the red runs are on the bead's notes): the recovery bundle test
``nexus.corpus.parse_conformant_collection_name`` and ``nexus.corpus.split_candidate_collection_name``, the
migrate-fallback test ``nexus.corpus.voyage_model_for_collection``, the indexer test
``nexus.indexer._migration_source_candidates`` (the name-derived discovery of a legacy collection that has no
row to find it by) and ``nexus.corpus.resolve_row_preferred`` (the row-else-name fallback).

The mode lint (RDR-109) reads each test's own source for a Voyage token: the cloud tokens are module constants,
and the two tests whose result depends on a cloud mode ask for ``cloud_mode``. The others run in the default
(local) mode, where the target's model is the bge token.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import nexus.catalog.recovery_bundle as rb
from nexus.catalog.catalog_protocol import CATALOG_WRITE_OPS
from nexus.catalog.collection_name import CollectionName, owner_segment_for_tumbler
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.note_write import PutNoteOutcome
from nexus.catalog.recovery_bundle import ExportSummary, import_bundle, write_bundle
from nexus.cli import main
from nexus.indexer import _legacy_collection_name, _migrate_legacy_collections

#: The local embedder's token: the target install of the default (local) test mode.
_BGE = "bge-base-en-v15-768"
#: The cloud embedders' tokens, as another install in cloud mode recorded them.
_VOYAGE_CTX = "voyage-context-3"
_VOYAGE_CODE = "voyage-code-3"


def _split_name(name: str) -> tuple[str, str, str, str]:
    """A four-segment collection name's parts. Tests only; the code under test never calls this."""
    content_type, owner, model, version = name.split("__")
    return content_type, owner, model, version


# ── source 1: recovery bundle import ────────────────────────────────────────


class _RowlessT3:
    """A target T3 that holds nothing: the recorded name has no collection, and no row, on this install."""

    def collection_exists(self, name: str) -> bool:
        return False


@pytest.fixture
def put_note_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The note writer's seam, patched: every ``put_note`` call the import makes, recorded, answering 'stored'."""
    calls: list[dict[str, Any]] = []

    def _put_note(**kw: Any) -> PutNoteOutcome:
        calls.append(kw)
        return PutNoteOutcome(
            status="stored", collection=kw["collection"], pieces=["body"],
            manifest_metadatas=[{"chunk_text_hash": "h" * 64, "chunk_index": 0}],
            chunk_ids=["h" * 64], catalog_doc_id="1.7.7", minted=True,
        )

    monkeypatch.setattr("nexus.catalog.note_write.put_note", _put_note)
    monkeypatch.setattr("nexus.catalog.note_write.fire_note_chains", lambda outcome, content: None)
    monkeypatch.setattr("nexus.catalog.note_write.stamp_note", lambda outcome: outcome)
    return calls


def _bundle_record(recorded: str, title: str, *, entity_key: str = "CUST-0018", ttl_days: int | None = 30) -> dict:
    return {
        "record": "knowledge_doc", "source_uri": "", "collection": recorded, "title": title,
        "tags": "t1,t2", "category": "ref", "content": f"body of {title}",
        "entity_key": entity_key, "ttl_days": ttl_days,
    }


def _import(tmp_path, recs: list[dict]) -> None:
    path = tmp_path / "bundle.jsonl"
    write_bundle(path, recs, [], ExportSummary())
    summary = import_bundle(None, None, _RowlessT3(), path)
    assert (summary.docs_imported, summary.docs_failed, summary.docs_uncertain) == (len(recs), 0, 0), (
        "the import itself failed: every assertion about its put_note calls would be vacuous"
    )


#: (id, recorded name, content type, owner, the entity key a put takes there). A ``code__`` target takes none.
_BUNDLE_CASES = [
    ("knowledge-two-segment", "knowledge__nsgbundle2", "knowledge", "nsgbundle2", "CUST-0018"),
    ("knowledge-four-segment", f"knowledge__nsgbundle4__{_BGE}__v1", "knowledge", "nsgbundle4", "CUST-0018"),
    ("knowledge-cross-mode", f"knowledge__nsgcross__{_VOYAGE_CTX}__v1", "knowledge", "nsgcross", "CUST-0018"),
    ("docs-two-segment", "docs__nsgdocs2", "docs", "nsgdocs2", "CUST-0018"),
    ("docs-cross-mode", f"docs__nsgdocs4__{_VOYAGE_CTX}__v1", "docs", "nsgdocs4", "CUST-0018"),
    ("code-two-segment", "code__nsgcode2", "code", "nsgcode2", ""),
    ("code-cross-mode", f"code__nsgcode4__{_VOYAGE_CODE}__v1", "code", "nsgcode4", ""),
]


@pytest.mark.parametrize(
    "recorded, content_type, owner, entity_key",
    [pytest.param(*case[1:], id=case[0]) for case in _BUNDLE_CASES],
)
def test_a_bundle_note_recorded_under_a_name_with_no_row_restores_to_the_targets_collection(
    put_note_calls, tmp_path, recorded, content_type, owner, entity_key,
):
    """RDR-229 item 9 non-stranding guard (source 1: recovery bundle import); keep until P4.4 retires the
    fallback.

    The bundle carries the NAME the source install recorded and no row (item 10 changes that from Phase 3), and
    the target install has no row for it. The import must still put the note into the collection that name
    means on THIS install: the recorded content type and owner, with the model segment the target's active
    mode writes (the local bge token here), never the recorded model's. A two-segment name, a four-segment
    name of this mode and a four-segment name recorded in the other mode (the cross-mode case) all end in the
    same shape. The entity key and the TTL the bundle carries reach the put; a ``code__`` target takes no key.
    """
    _import(tmp_path, [_bundle_record(recorded, "the note")])

    (call,) = put_note_calls
    target = call["collection"]
    assert target == f"{content_type}__{owner}__{_BGE}__v1", (
        f"{recorded!r} restored to {target!r}: the content type, the owner and the target's active model "
        "must all come from the name"
    )
    assert _split_name(target) == (content_type, owner, _BGE, "v1")
    assert call["entity_key"] == entity_key
    assert call["ttl_days"] == 30
    assert call["category"] == "ref"
    assert call["source_agent"] == "recovery-import"


def test_a_two_segment_and_a_four_segment_bundle_name_restore_with_the_same_attributes(put_note_calls, tmp_path):
    """The Test Plan scenario of RDR-229: a bundle recorded under a legacy two-segment name and one recorded
    under the four-segment name of the same collection restore to ONE target with the same attributes, so a
    Phase 2 or Phase 3 bead cannot make the two diverge."""
    _import(tmp_path, [
        _bundle_record("knowledge__nsgpair", "legacy note"),
        _bundle_record(f"knowledge__nsgpair__{_BGE}__v1", "conformant note"),
        _bundle_record(f"knowledge__nsgpair__{_VOYAGE_CTX}__v1", "cross-mode note"),
    ])

    assert len(put_note_calls) == 3
    assert {c["collection"] for c in put_note_calls} == {f"knowledge__nsgpair__{_BGE}__v1"}
    for key in ("entity_key", "ttl_days", "category", "tags"):
        assert len({repr(c[key]) for c in put_note_calls}) == 1, f"{key} differs across the three recorded names"


@pytest.mark.usefixtures("cloud_mode")
def test_a_bundle_recorded_in_the_other_mode_restores_to_a_cloud_targets_model(put_note_calls, tmp_path, monkeypatch):
    """The cross-mode case the other way: a bundle recorded on a local install (bge tokens, and a two-segment
    legacy name) restores onto a cloud install to the CLOUD model, not the recorded bge token, which would
    leave the note in a collection the cloud engine cannot embed for."""
    monkeypatch.setattr("nexus.sensitive_write.catalog_flags_uri", lambda *a, **kw: False)  # a Voyage target asks the catalog
    _import(tmp_path, [
        _bundle_record(f"knowledge__nsgcloud__{_BGE}__v1", "from a local install"),
        _bundle_record("docs__nsgcloud2", "legacy name"),
        _bundle_record(f"code__nsgcloud3__{_BGE}__v1", "code, from a local install", entity_key=""),
    ])

    assert [c["collection"] for c in put_note_calls] == [
        f"knowledge__nsgcloud__{_VOYAGE_CTX}__v1",
        f"docs__nsgcloud2__{_VOYAGE_CTX}__v1",
        f"code__nsgcloud3__{_VOYAGE_CODE}__v1",
    ]


# ── source 2: nx catalog migrate-fallback ───────────────────────────────────


def _invoke_migrate(source: str, row: dict | None, *args: str):
    """Run ``nx catalog migrate-fallback SOURCE`` over a catalog that has a row for the SOURCE (or none) and
    none for any target; the registration the command makes is captured, the catalog and writer are fakes.
    Returns (result, writer, registrations)."""
    entry = MagicMock()
    entry.tumbler = "1.1.1"
    cat = MagicMock(spec=HttpCatalogClient)
    cat.get_collection.return_value = row
    cat.list_by_collection.return_value = [entry]
    writer = MagicMock(spec=[*CATALOG_WRITE_OPS, "close"])
    registrations: list[tuple[str, dict]] = []

    def _register(name, *, registrar=None, kwargs=None):
        registrations.append((name, dict(kwargs or {})))

    with patch("nexus.commands.catalog._get_catalog", return_value=cat), \
            patch("nexus.commands.catalog._get_catalog_writer", return_value=writer), \
            patch("nexus.corpus.ensure_collection_registered", _register):
        result = CliRunner().invoke(main, ["catalog", "migrate-fallback", source, *args])
    return result, writer, registrations


#: (id, legacy source name, its row's content type, the model the target takes from the NAME).
_MIGRATE_CASES = [
    ("docs", "docs__nsgdefault", "docs", _VOYAGE_CTX),
    ("knowledge", "knowledge__nsgknow", "knowledge", _VOYAGE_CTX),
    ("rdr", "rdr__nsgrdr", "rdr", _VOYAGE_CTX),
    ("code", "code__nsgrepo", "code", _VOYAGE_CODE),
]


@pytest.mark.parametrize(
    "source, content_type, model", [pytest.param(*case[1:], id=case[0]) for case in _MIGRATE_CASES],
)
def test_migrate_fallback_gives_a_row_less_target_the_model_its_legacy_source_name_dispatches_to(
    source, content_type, model,
):
    """RDR-229 item 9 non-stranding guard (source 2: migrate-fallback); keep until P4.4 retires the fallback.

    The shape the code takes: the SOURCE is a registered legacy two-segment collection (the command refuses one
    with no row, below), and it is the TARGET that has no row. With no ``--target-model`` the target's model
    comes from the source's name prefix, ``voyage_model_for_collection``: a legacy source carries no model in its
    row to read instead. The migration must still register the right target with the right attributes (content
    type from the row, owner from the document's tumbler, the model from the name) and re-point the document.
    """
    result, writer, registrations = _invoke_migrate(
        source, {"name": source, "content_type": content_type}, "--yes",
    )

    assert result.exit_code == 0, result.output
    target = f"{content_type}__1-1__{model}__v1"
    assert registrations == [(target, {
        "content_type": content_type, "owner_id": "1-1", "embedding_model": model, "model_version": "v1",
    })]
    assert _split_name(target) == (content_type, "1-1", model, "v1")
    writer.update_documents_collection_batch.assert_called_once_with([("1.1.1", target)])
    assert f"{source}: 1 doc(s) -> 1 target collection(s)" in result.output


def test_migrate_fallback_an_explicit_target_model_wins_over_the_name_dispatch():
    """Control for the guard above: ``--target-model`` is read before the name is consulted."""
    result, writer, registrations = _invoke_migrate(
        "docs__nsgexplicit", {"name": "docs__nsgexplicit", "content_type": "docs"},
        "--yes", "--target-model", _BGE,
    )

    assert result.exit_code == 0, result.output
    assert [name for name, _ in registrations] == [f"docs__1-1__{_BGE}__v1"]
    writer.update_documents_collection_batch.assert_called_once()


def test_migrate_fallback_a_source_with_no_row_is_refused_not_stranded_silently():
    """A source with no ``catalog_collections`` row is not migrated on a guess from its name: the command
    names the collection, exits non-zero and writes nothing."""
    result, writer, registrations = _invoke_migrate("docs__nsgnorow", None, "--yes")

    assert result.exit_code != 0
    assert "docs__nsgnorow" in result.output and "not registered" in result.output
    assert registrations == []
    writer.update_documents_collection_batch.assert_not_called()


# ── source 3: the indexer's legacy-collection migration ─────────────────────

_RENAME_COUNTS = {"tax_topics": 0, "tax_assignments": 0, "tax_meta": 0, "chash": 0, "catalog_docs": 0}


class _OwnerCat:
    """``owner_for_repo`` and ``collection_for``, rendering through the real ``CollectionName``. It has no
    ``get_collection``: the migration then asks the row cache, which answers none."""

    def owner_for_repo(self, repo_hash: str) -> str:
        return "1.1"

    def collection_for(self, content_type, owner, embedding_model, *, bump=False):
        return CollectionName(
            content_type=content_type, owner_id=owner_segment_for_tumbler(owner),
            embedding_model=embedding_model, model_version=1,
        )


class _RowReadingCat(_OwnerCat):
    """The same, with a row reader that finds no row for the legacy collection."""

    def get_collection(self, name: str) -> None:
        return None


class _LegacyT3:
    """A T3 holding exactly the named collections: the legacy ones the repo indexed before RDR-103."""

    def __init__(self, *names: str) -> None:
        self.names = set(names)

    def collection_exists(self, name: str) -> bool:
        return name in self.names


@pytest.mark.usefixtures("cloud_mode")
@pytest.mark.parametrize("catalog_cls", [_OwnerCat, _RowReadingCat], ids=["row-cache-answers-none", "row-reader-answers-none"])
def test_the_indexer_migrates_a_legacy_collection_with_no_row_to_the_write_models_name(
    catalog_cls, tmp_path, monkeypatch,
):
    """RDR-229 item 9 non-stranding guard (source 3: the indexer's legacy-collection migration); keep until
    P4.4 retires the fallback.

    A repo indexed before RDR-103 holds legacy ``<type>__<basename>-<hash8>`` collections in T3 and no
    ``catalog_collections`` row for them. The first index after the upgrade must rename each to the conformant
    name its owner's tumbler and the indexer's CURRENT write model give (voyage-context-3 for docs and rdr,
    voyage-code-3 for code, in the cloud mode this test sets), register that name with those attributes, and
    hand the caller the new name: not leave the data under its legacy name. The target model comes from the
    write model, never from the legacy name (decision 1 of the migration's design), and a row-less legacy
    collection has no row model to disagree with it (nexus-7f4o4).
    """
    repo = tmp_path / "nsgrepo"
    repo.mkdir()
    monkeypatch.setattr("nexus.repo_identity._repo_identity", lambda r: ("nsgrepo", "cafef00d"))
    expected_models = {"code": _VOYAGE_CODE, "docs": _VOYAGE_CTX, "rdr": _VOYAGE_CTX}
    legacy = {ct: _legacy_collection_name(repo, ct) for ct in expected_models}
    assert legacy["code"] == "code__nsgrepo-cafef00d", "the legacy name is not the shape this guard feeds"
    renames: list[tuple[str, str]] = []
    registered: list[tuple[str, dict]] = []
    monkeypatch.setattr("nexus.mcp_infra.get_collection_row", lambda name, **kw: None)
    monkeypatch.setattr(
        "nexus.collection_rename.rename_collection_data_plane",
        lambda old, new, **kw: renames.append((old, new)) or _RENAME_COUNTS,
    )
    monkeypatch.setattr(
        "nexus.corpus.ensure_collection_registered",
        lambda name, *, registrar=None, kwargs=None: registered.append((name, dict(kwargs or {}))),
    )

    result = _migrate_legacy_collections(
        repo, cat=catalog_cls(), t3_db=_LegacyT3(*legacy.values()), registry=MagicMock(), writer=MagicMock(),
    )

    targets = {ct: f"{ct}__1-1__{model}__v1" for ct, model in expected_models.items()}
    assert result == targets
    assert sorted(renames) == sorted((legacy[ct], targets[ct]) for ct in targets)
    assert sorted(registered, key=lambda r: r[0]) == sorted(
        ((targets[ct], {
            "content_type": ct, "owner_id": "1-1", "embedding_model": expected_models[ct], "model_version": "v1",
        }) for ct in targets),
        key=lambda r: r[0],
    )
    for ct, name in targets.items():
        assert _split_name(name) == (ct, "1-1", expected_models[ct], "v1")
