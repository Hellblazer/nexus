# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 item 9 non-stranding guards, client half (P2.1, nexus-p2m1x.6).

KEEP THESE TESTS until P4.4 retires the name-parse fallbacks they guard.
``tests/test_rdr229_non_stranding_inventory.py`` (lint bucket) lists every guard by name and fails when one goes
missing, so a guard is removed on purpose, by editing that list in the P4.4 change, or not at all.

RDR-229 retires name parsing as the way the client learns a collection's content type, owner and model: the
row carries them. Phase 2 repoints the readers to the row. Five code paths still receive a collection NAME that
has no ``catalog_collections`` row on this install, because the name comes from another install or an older
era, and the name-parse fallback is all they have: a recovery-bundle import, ``nx catalog migrate-fallback``,
the indexer's legacy-collection migration, the ``.nxexp`` import (``exporter.import_collection``) and the
engine's ETL import (the engine half is
``service/src/test/java/dev/nexus/service/CatalogEtlImportNonStrandingTest.java``). A reader repoint that
removed the fallback before they had another source would strand a restore or an upgrade. Item 9: a reader may be
repointed only after every migration source it serves has a test that runs a row-less legacy name through it and
passes. These are those tests, written first, green on develop today.

Each test feeds a name with NO row on the target through the real source code path and asserts the target
collection and the attributes the source hands to the next stage: content type, owner, the TARGET install's
active embedding model, and the entity key and TTL where the source carries them. "No row" is stated, not
assumed: an autouse fixture answers none from the row seam (``nexus.mcp_infra.get_collection_row``) and each
catalog fake answers none from ``find_collection``, so a reader that has moved to the row finds no row and has to
fall back, instead of erroring on a fake that has no such method. The names are built here by hand, and a test
may split a name itself (the census scans ``src/``, not ``tests/``); nothing here parses a name for the code
under test. A registration is asserted as an attribute subset, so a label Phase 3 adds does not break a guard.

Tenant-free on purpose (``tests/storage_touch_ceiling.toml`` allows python_unit no new tenant test): a T3 with no
collection, a catalog fake, and the writer seams patched. What each guard stubs to prove it guards (a throwaway
pytest plugin kept outside the tree makes each name-parse or name-derivation function at its use site raise,
and the test goes red; the red runs are on the bead's notes): the recovery bundle test
``nexus.corpus.parse_conformant_collection_name`` and ``nexus.corpus.split_candidate_collection_name``, the
migrate-fallback test ``nexus.corpus.voyage_model_for_collection``, the indexer test
``nexus.corpus.is_conformant_collection_name`` (the census's migration-source site on the registration shape),
``nexus.indexer._migration_source_candidates`` (the name-derived discovery of a legacy collection that has no
row to find it by) and ``nexus.corpus.resolve_row_preferred`` (the row-else-name fallback), the ``.nxexp``
test ``nexus.exporter.index_model_for_collection`` and ``nexus.corpus.collection_registration_kwargs``.

The mode lint (RDR-109) reads each test's own source for a Voyage token: the cloud tokens are module constants,
and the tests whose result depends on a cloud mode ask for ``cloud_mode`` (directly, or through the
``install_mode`` fixture, which a test source never spells with a token). The others run in the default
(local) mode, where the target's model is the bge token.
"""
from __future__ import annotations

import gzip
import json
from typing import Any
from unittest.mock import MagicMock, call, patch

import msgpack
import pytest
from click.testing import CliRunner

from nexus.catalog.catalog_protocol import CATALOG_WRITE_OPS
from nexus.catalog.collection_name import CollectionName, owner_segment_for_tumbler
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.note_write import PutNoteOutcome
from nexus.catalog.recovery_bundle import ExportSummary, import_bundle, write_bundle
from nexus.cli import main
from nexus.corpus import resolve_read_embedding_model
from nexus.db.http_vector_client import HttpVectorClient
from nexus.exporter import FORMAT_VERSION, import_collection
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


def _has(actual: dict, expected: dict) -> None:
    """*actual* carries every attribute of *expected* (a registration gains a label in Phase 3)."""
    missing = {k: (v, actual.get(k)) for k, v in expected.items() if actual.get(k) != v}
    assert not missing, f"attributes differ (expected, got): {missing}"


@pytest.fixture(autouse=True)
def _no_row_for_any_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """The row seam answers 'no row' for every name, so each guard's name is row-less on purpose."""
    monkeypatch.setattr("nexus.mcp_infra.get_collection_row", lambda name, **kw: None)


@pytest.fixture(params=["local", "cloud"])
def install_mode(request: pytest.FixtureRequest) -> str:
    """The target install's mode: local (the default test mode) or cloud (``cloud_mode``)."""
    if request.param == "cloud":
        request.getfixturevalue("cloud_mode")
    return request.param


# ── source 1: recovery bundle import ────────────────────────────────────────


class _RowlessT3:
    """A target T3 that holds nothing: the recorded name has no collection, and no row, on this install."""

    def collection_exists(self, name: str) -> bool:
        return False

    def find_collection(self, *args: Any, **kwargs: Any) -> None:
        return None


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
    """Non-stranding guard (RDR-229 item 9; keep until P4.4): source 1, recovery bundle import.

    The bundle carries the NAME the source install recorded and no row (item 10 changes that from Phase 3), and
    the target install has no row for it. The import must still put the note into the collection that name
    means on THIS install: the recorded content type and owner, with the model segment the target's active
    mode writes (the local bge token here), never the recorded model's. A two-segment name, a four-segment
    name of this mode and a four-segment name recorded in the other mode (the cross-mode case) all end in the
    same shape. The entity key and the TTL the bundle carries reach the put; a ``code__`` target takes no key.
    """
    _import(tmp_path, [_bundle_record(recorded, "the note")])

    (call_,) = put_note_calls
    target = call_["collection"]
    assert target == f"{content_type}__{owner}__{_BGE}__v1", (
        f"{recorded!r} restored to {target!r}: the content type, the owner and the target's active model "
        "must all come from the name"
    )
    assert _split_name(target) == (content_type, owner, _BGE, "v1")
    assert call_["entity_key"] == entity_key
    assert call_["ttl_days"] == 30
    assert call_["category"] == "ref"
    assert call_["source_agent"] == "recovery-import"


def test_a_two_segment_and_a_four_segment_bundle_name_restore_with_the_same_attributes(put_note_calls, tmp_path):
    """Non-stranding guard (RDR-229 item 9; keep until P4.4): source 1, the Test Plan scenario.

    A bundle recorded under a legacy two-segment name and one recorded under the four-segment name of the same
    collection restore to ONE target with the same attributes, so a Phase 2 or Phase 3 bead cannot make the two
    diverge."""
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
    """Non-stranding guard (RDR-229 item 9; keep until P4.4): source 1, the cross-mode case the other way.

    A bundle recorded on a local install (bge tokens, and a two-segment legacy name) restores onto a cloud
    install to the CLOUD model, not the recorded bge token, which would leave the note in a collection the cloud
    engine cannot embed for."""
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


def _source_row(source: str, content_type: str, owner: str, model: str) -> dict:
    """A legacy source's catalog row in the shape the engine holds it (hygiene-002-1: owner_id and
    embedding_model are non-empty)."""
    return {
        "name": source, "content_type": content_type, "owner_id": owner, "embedding_model": model,
        "model_version": "v1", "lifecycle_state": "live",
    }


def _invoke_migrate(source: str, row: dict | None, *args: str):
    """Run ``nx catalog migrate-fallback SOURCE`` over a catalog that has a row for the SOURCE (or none) and
    none for any target; the registration the command makes is captured, the catalog and writer are fakes.
    Returns (result, writer, registrations)."""
    entry = MagicMock()
    entry.tumbler = "1.1.1"
    cat = MagicMock(spec=HttpCatalogClient)
    cat.get_collection.return_value = row
    cat.find_collection = MagicMock(return_value=None)  # the row reader P2.2 adds: it finds no row for a target
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


#: (id, legacy source name, its row's content type, owner, the model the target takes from the NAME, which is
#: also the model the row carries here).
_MIGRATE_CASES = [
    ("docs", "docs__nsgdefault", "docs", "nsgdefault", _VOYAGE_CTX),
    ("knowledge", "knowledge__nsgknow", "knowledge", "nsgknow", _VOYAGE_CTX),
    ("rdr", "rdr__nsgrdr", "rdr", "nsgrdr", _VOYAGE_CTX),
    ("code", "code__nsgrepo", "code", "nsgrepo", _VOYAGE_CODE),
]


@pytest.mark.usefixtures("cloud_mode")
@pytest.mark.parametrize(
    "source, content_type, owner, model", [pytest.param(*case[1:], id=case[0]) for case in _MIGRATE_CASES],
)
def test_migrate_fallback_gives_a_row_less_target_the_model_its_legacy_source_name_dispatches_to(
    source, content_type, owner, model,
):
    """Non-stranding guard (RDR-229 item 9; keep until P4.4): source 2, migrate-fallback.

    The shape the code takes: the SOURCE is a registered legacy two-segment collection (the command refuses one
    with no row, below), and it is the TARGET that has no row. With no ``--target-model`` the target's model
    comes from the source's name prefix, ``voyage_model_for_collection``. The migration must still register the
    right target with the right attributes (content type from the row, owner from the document's tumbler, the
    model from the name) and re-point the document. Run in cloud mode, where the install's model is the name's.
    """
    result, writer, registrations = _invoke_migrate(
        source, _source_row(source, content_type, owner, model), "--yes",
    )

    assert result.exit_code == 0, result.output
    target = f"{content_type}__1-1__{model}__v1"
    assert [name for name, _ in registrations] == [target]
    _has(registrations[0][1], {
        "content_type": content_type, "owner_id": "1-1", "embedding_model": model, "model_version": "v1",
    })
    assert _split_name(target) == (content_type, "1-1", model, "v1")
    writer.update_documents_collection_batch.assert_called_once_with([("1.1.1", target)])
    assert f"{source}: 1 doc(s) -> 1 target collection(s)" in result.output


@pytest.mark.usefixtures("cloud_mode")
def test_migrate_fallback_a_source_row_naming_another_model_than_its_name_dispatches_to(monkeypatch):
    """Non-stranding guard (RDR-229 item 9; keep until P4.4): source 2, a source whose row and name disagree.

    The source ``docs__nsgrowbge`` is a legacy name, which dispatches to the voyage model, and its row says bge
    (a local install's collection under a legacy name). TODAY THE NAME WINS: the target is minted under the
    name's model, whatever the row says, because the command reads no model from the row. Phase 2 may flip that
    by design (a row-preferred read would carry bge to the target). If it does, the one line to edit is the
    assertion on ``expected`` below, not the guard: the guard's job is that a row-less target is still
    registered with SOME model and the document is still re-pointed."""
    source = "docs__nsgrowbge"
    result, writer, registrations = _invoke_migrate(
        source, _source_row(source, "docs", "nsgrowbge", _BGE), "--yes",
    )

    assert result.exit_code == 0, result.output
    expected = _VOYAGE_CTX  # today: the name dispatch. The line P2 may change to _BGE.
    assert [name for name, _ in registrations] == [f"docs__1-1__{expected}__v1"]
    _has(registrations[0][1], {"content_type": "docs", "embedding_model": expected})
    writer.update_documents_collection_batch.assert_called_once()


@pytest.mark.usefixtures("cloud_mode")
def test_migrate_fallback_an_explicit_target_model_wins_over_the_name_dispatch():
    """Non-stranding guard (RDR-229 item 9; keep until P4.4): source 2, a control for the guards above.

    ``--target-model`` is read before the name is consulted."""
    source = "docs__nsgexplicit"
    result, writer, registrations = _invoke_migrate(
        source, _source_row(source, "docs", "nsgexplicit", _VOYAGE_CTX), "--yes", "--target-model", _BGE,
    )

    assert result.exit_code == 0, result.output
    assert [name for name, _ in registrations] == [f"docs__1-1__{_BGE}__v1"]
    writer.update_documents_collection_batch.assert_called_once()


@pytest.mark.usefixtures("cloud_mode")
def test_migrate_fallback_a_source_with_no_row_is_refused_not_stranded_silently():
    """Non-stranding guard (RDR-229 item 9; keep until P4.4): source 2, a source with no row.

    A source with no ``catalog_collections`` row is not migrated on a guess from its name: the command names the
    collection, exits non-zero and writes nothing."""
    result, writer, registrations = _invoke_migrate("docs__nsgnorow", None, "--yes")

    assert result.exit_code != 0
    assert "docs__nsgnorow" in result.output and "not registered" in result.output
    assert registrations == []
    writer.update_documents_collection_batch.assert_not_called()


@pytest.mark.usefixtures("cloud_mode")
def test_migrate_fallback_a_conformant_named_source_with_a_row_is_refused_and_nothing_is_written():
    """Non-stranding guard (RDR-229 item 9; keep until P4.4): source 2, the conformant-name ledger site.

    A source whose name is already four-segment is not a fallback, even with a row: the command refuses it
    (the census's ``is_conformant_collection_name(source)`` site) and writes nothing."""
    source = f"docs__nsgconf__{_BGE}__v1"
    result, writer, registrations = _invoke_migrate(
        source, _source_row(source, "docs", "nsgconf", _BGE), "--yes",
    )

    assert result.exit_code != 0
    assert "already conformant" in result.output
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

    def find_collection(self, *args: Any, **kwargs: Any) -> None:
        return None


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


def _write_models(mode: str) -> dict[str, str]:
    """The model the indexer writes each content type with, per install mode."""
    if mode == "local":
        return {"code": _BGE, "docs": _BGE, "rdr": _BGE}
    return {"code": _VOYAGE_CODE, "docs": _VOYAGE_CTX, "rdr": _VOYAGE_CTX}


@pytest.mark.parametrize("shape", ["legacy-two-segment", "phase5-synth"])
@pytest.mark.parametrize("catalog_cls", [_OwnerCat, _RowReadingCat], ids=["row-cache-answers-none", "row-reader-answers-none"])
def test_the_indexer_migrates_a_legacy_collection_with_no_row_to_the_write_models_name(
    shape, catalog_cls, install_mode, tmp_path, monkeypatch,
):
    """Non-stranding guard (RDR-229 item 9; keep until P4.4): source 3, the indexer's legacy-collection migration.

    A repo indexed before RDR-103 holds legacy ``<type>__<basename>-<hash8>`` collections in T3 and no
    ``catalog_collections`` row for them; a repo indexed after the Phase-5 strict flip but before its owner was
    registered holds the path-derived synth ``<type>__<basename>-<hash8>__<model>__v1`` instead (nexus-7vuw),
    with the model token ``resolve_read_embedding_model`` gives in the install's mode. The first index after the
    upgrade must rename each to the conformant name its owner's tumbler and the indexer's CURRENT write model
    give, register that name with those attributes, supersede the old name by the new one, rewrite the registry
    entry and hand the caller the new name: not leave the data under its old name. The target model comes from
    the write model, never from the old name (decision 1 of the migration's design), and a row-less collection
    has no row model to disagree with it (nexus-7f4o4). Run for both install modes.
    """
    repo = tmp_path / "nsgrepo"
    repo.mkdir()
    monkeypatch.setattr("nexus.repo_identity._repo_identity", lambda r: ("nsgrepo", "cafef00d"))
    models = _write_models(install_mode)
    cts = ("code", "docs", "rdr")
    if shape == "legacy-two-segment":
        old = {ct: _legacy_collection_name(repo, ct) for ct in cts}
        assert old["code"] == "code__nsgrepo-cafef00d", "the legacy name is not the shape this guard feeds"
    else:
        old = {ct: f"{ct}__nsgrepo-cafef00d__{resolve_read_embedding_model(ct)}__v1" for ct in cts}
    renames: list[tuple[str, str]] = []
    registered: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "nexus.collection_rename.rename_collection_data_plane",
        lambda o, n, **kw: renames.append((o, n)) or _RENAME_COUNTS,
    )
    monkeypatch.setattr(
        "nexus.corpus.ensure_collection_registered",
        lambda name, *, registrar=None, kwargs=None: registered.append((name, dict(kwargs or {}))),
    )
    writer, registry = MagicMock(), MagicMock()

    result = _migrate_legacy_collections(
        repo, cat=catalog_cls(), t3_db=_LegacyT3(*old.values()), registry=registry, writer=writer,
    )

    targets = {ct: f"{ct}__1-1__{models[ct]}__v1" for ct in cts}
    assert result == targets
    assert renames == [(old[ct], targets[ct]) for ct in cts]
    assert [name for name, _ in registered] == [targets[ct] for ct in cts]
    for ct, (_name, kwargs) in zip(cts, registered):
        _has(kwargs, {"content_type": ct, "owner_id": "1-1", "embedding_model": models[ct], "model_version": "v1"})
    for ct, name in targets.items():
        assert _split_name(name) == (ct, "1-1", models[ct], "v1")
    assert writer.supersede_collection.call_args_list == [
        call(old[ct], targets[ct], reason="rdr-103-phase4-migration") for ct in cts
    ]
    assert registry.update.call_args_list == [call(repo, **{f"{ct}_collection": targets[ct]}) for ct in cts]


# ── source 5: the .nxexp import (exporter.import_collection) ────────────────


class _Captured(Exception):
    """Raised by the recording owner-import stub: carries what the import handed the engine leg."""

    def __init__(self, kwargs: dict[str, Any]) -> None:
        super().__init__("owner import captured")
        self.kwargs = kwargs


class _RecordingOwnerImport:
    """Stands in for the engine leg of the import (``_OwnerImport``): records the constructor's arguments, the
    wire ``embedding_model`` among them, and stops the import there, so nothing reaches an engine."""

    def __init__(self, **kwargs: Any) -> None:
        raise _Captured(kwargs)


def _write_nxexp(path, *, source: str, header_model: str) -> None:
    """A one-record export as an older install wrote it: the header carries *header_model*, which for a legacy
    two-segment collection is only the writer's prefix-based guess."""
    dim = 1024 if header_model.startswith("voyage") else 768
    header = {
        "format_version": FORMAT_VERSION, "collection_name": source, "database_type": source.split("__")[0],
        "embedding_model": header_model, "record_count": 1, "embedding_dim": dim,
        "exported_at": "2025-01-01T00:00:00+00:00", "pipeline_version": "nexus-1",
    }
    with open(path, "wb") as f:
        f.write(json.dumps(header).encode() + b"\n")
        with gzip.GzipFile(fileobj=f, mode="wb") as gz:
            gz.write(msgpack.packb({
                "id": "a" * 64, "document": "hello", "metadata": {}, "embedding": bytes(4 * dim),
            }, use_bin_type=True))


def _engine_leg_arguments(tmp_path, monkeypatch, *, target, header_model, assume_model, row=None) -> dict[str, Any]:
    """Run ``import_collection`` of a one-record file into *target* on a service-backed handle that has *row*
    for it, stopped at the engine leg; returns that leg's constructor arguments."""
    monkeypatch.setattr("nexus.exporter._OwnerImport", _RecordingOwnerImport)
    path = tmp_path / "old.nxexp"
    _write_nxexp(path, source="code__nsgoldexport", header_model=header_model)
    db = object.__new__(HttpVectorClient)  # a service-backed handle: only its row reader is read before the stop
    db._resolve_collection_row = lambda collection, strict=False: row  # type: ignore[method-assign]
    # A P2.2 repoint to a find_collection read sees the same row (or none), not an AttributeError.
    db.find_collection = lambda *a, **kw: row  # type: ignore[method-assign]
    with pytest.raises(_Captured) as stopped:
        import_collection(db, path, target_collection=target, assume_model=assume_model, hooks=MagicMock())
    return stopped.value.kwargs


#: (id, install mode, target, the header's model, --assume-model, the model sent to the engine). A legacy
#: two-segment target's header model is the prefix-based guess; the engine is sent the model its registration
#: derives for this install, not the guess. A four-segment target carries its model.
_NXEXP_CASES = [
    ("legacy-local-assume-absent", "local", "code__nsgexp", _VOYAGE_CODE, None, _BGE),
    ("legacy-local-assume-present", "local", "code__nsgexp", _VOYAGE_CODE, _VOYAGE_CODE, _BGE),
    ("legacy-cloud-assume-absent", "cloud", "code__nsgexp", _VOYAGE_CODE, None, _VOYAGE_CODE),
    ("legacy-cloud-assume-present", "cloud", "code__nsgexp", _VOYAGE_CODE, _VOYAGE_CODE, _VOYAGE_CODE),
    ("four-segment-local", "local", f"code__nsgexp4__{_BGE}__v1", _BGE, None, _BGE),
]


@pytest.mark.parametrize(
    "mode, target, header_model, assume_model, wire_model",
    [pytest.param(*case[1:], id=case[0]) for case in _NXEXP_CASES],
)
def test_an_nxexp_import_into_a_target_with_no_row_names_the_model_the_target_registers_with(
    request, tmp_path, monkeypatch, mode, target, header_model, assume_model, wire_model,
):
    """Non-stranding guard (RDR-229 item 9; keep until P4.4): source 5, ``exporter.import_collection``.

    A ``.nxexp`` file comes from another install or an older era (the census labels its name sites
    migration-source), so the target collection often has no row here yet, and a legacy two-segment target
    carries no model in its name. The header's model is then a prefix-based guess (a Voyage model), which in a
    local install is not what the collection is registered with. The import must still pass its model gate (the
    name-derived expected model, with ``--assume-model`` absent or present) and send the engine the model the
    target's registration derives for this install (``_wire_embedding_model``), or the engine refuses the first
    request. A four-segment target carries its model and sends it. The import is stopped at the engine leg's
    constructor, so nothing reaches an engine; the target collection is checked there too.
    """
    if mode == "cloud":
        request.getfixturevalue("cloud_mode")

    got = _engine_leg_arguments(
        tmp_path, monkeypatch, target=target, header_model=header_model, assume_model=assume_model,
    )

    assert got["collection_name"] == target
    assert got["embedding_model"] == wire_model


def test_an_nxexp_import_into_a_target_that_has_a_row_names_the_rows_model(tmp_path, monkeypatch):
    """Non-stranding guard (RDR-229 item 9; keep until P4.4): source 5, a control.

    The same file into a legacy target that DOES have a row sends the row's model, not the registration's
    derivation."""
    got = _engine_leg_arguments(
        tmp_path, monkeypatch, target="code__nsgexprow", header_model=_VOYAGE_CODE, assume_model=None,
        row={"embedding_model": "minilm-l6-v2-384"},
    )

    assert got["embedding_model"] == "minilm-l6-v2-384"
