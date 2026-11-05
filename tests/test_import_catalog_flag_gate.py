# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 1 fix round 3 (nexus-rfvyy.6.20, Sam's 2026-11-05 import ruling): the recovery-bundle restore and
``nx store import`` refuse a flagged document bound for a Voyage collection unless ``--ack-external-embedder``,
and a document the CATALOG already flags (matched by ``source_uri``) is flagged even when the file or bundle
writes it plain: the flag is a one-way property of the document. A catalog that cannot answer reads as flagged.

The bundle target is forced to a Voyage name (``target_collection_for``) because the local substrate embeds with
bge; ``put_note`` is replaced by a recorder, since a Voyage collection cannot be written here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import nexus.catalog.note_write as nw
import nexus.catalog.recovery_bundle as rb
from nexus.catalog.factory import make_catalog_writer
from nexus.catalog.recovery_bundle import ExportSummary, import_bundle, write_bundle
from nexus.cli import main
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction
from nexus.db.t3 import T3Database
from nexus.exporter import import_collection
from nexus.sensitive_write import ExternalEmbedderRefusedError
from tests.conftest import make_vector_test_client
from tests.test_import_sensitive_external_embedder import VOYAGE, _stored, _voyage_file

pytestmark = pytest.mark.t2_tenant

VOYAGE_TARGET = "knowledge__rfvyy620b__voyage-context-3__v1"
BGE_TARGET = "knowledge__rfvyy620b__bge-base-en-v15-768__v1"


@pytest.fixture(autouse=True)
def _no_document_is_sensitive():
    """Redefines tests/conftest.py's fixture of this name: the subject here is the catalog read, so it is real."""
    yield


def _flag_in_catalog(uri: str, title: str, collection: str) -> str:
    writer = make_catalog_writer()
    try:
        owner = writer.register_owner("rfvyy-imp", "curator")
        tumbler = str(writer.register(
            owner=owner, title=title, content_type="prose", file_path=title, physical_collection=collection,
            source_uri=uri))
        out = writer.set_sensitivity([tumbler], True, reason="test: flagged before the import")
        assert out["updated"] == [1], out
        return tumbler
    finally:
        writer.close()


def _rec(title: str, **kw: Any) -> dict:
    return {"record": "knowledge_doc", "source_uri": f"nx-note://{VOYAGE_TARGET}/{title}", "collection": VOYAGE_TARGET,
            "title": title, "tags": "", "category": "", "content": f"rfvyy620b body of {title}", **kw}


@pytest.fixture
def puts(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Every put_note call, recorded; the call then stops, which the import counts as the record's failure."""
    seen: list[dict] = []

    def fake(**kw: Any):
        seen.append(kw)
        raise RuntimeError("stop: recorded")

    monkeypatch.setattr(nw, "put_note", fake)
    monkeypatch.setattr(rb, "target_collection_for", lambda recorded, t3: VOYAGE_TARGET)
    return seen


def _bundle(tmp_path: Path, *recs: dict) -> Path:
    path = tmp_path / "b.jsonl"
    write_bundle(path, list(recs), [], ExportSummary())
    return path


# ── the recovery bundle ─────────────────────────────────────────────────────

def test_a_flagged_record_into_a_voyage_target_is_refused_without_the_ack_and_put_note_never_runs(
    t2_service_env, tmp_path: Path, puts: list[dict],
) -> None:
    summary = import_bundle(None, None, None, _bundle(tmp_path, _rec("secret one", sensitive=True)))

    assert (summary.docs_imported, summary.docs_failed) == (0, 1)
    assert "--ack-external-embedder" in summary.doc_failures[0]["error"]
    assert "nx catalog import refused" in summary.doc_failures[0]["error"]
    assert puts == [], "the refusal comes before the write"


def test_the_ack_lets_a_flagged_record_through_to_put_note_flagged(
    t2_service_env, tmp_path: Path, puts: list[dict], capsys: pytest.CaptureFixture[str],
) -> None:
    import_bundle(None, None, None, _bundle(tmp_path, _rec("secret two", sensitive=True)), ack_external_embedder=True)

    assert [p["sensitive"] for p in puts] == [True] and puts[0]["collection"] == VOYAGE_TARGET
    assert "sensitive: acknowledged" in capsys.readouterr().err


def test_a_plain_record_for_a_voyage_target_is_not_gated(t2_service_env, tmp_path: Path, puts: list[dict]) -> None:
    import_bundle(None, None, None, _bundle(tmp_path, _rec("plain one")))
    assert [p["sensitive"] for p in puts] == [False]


def test_a_record_the_catalog_already_flags_is_flagged_though_the_bundle_says_nothing(
    t2_service_env, tmp_path: Path, puts: list[dict],
) -> None:
    rec = _rec("catalog flagged")
    _flag_in_catalog(rec["source_uri"], "catalog flagged", VOYAGE_TARGET)
    path = _bundle(tmp_path, rec)

    refused = import_bundle(None, None, None, path)
    assert (refused.docs_imported, refused.docs_failed) == (0, 1) and puts == []
    assert "--ack-external-embedder" in refused.doc_failures[0]["error"]

    import_bundle(None, None, None, path, ack_external_embedder=True)
    assert [p["sensitive"] for p in puts] == [True], "with the ack the put is flagged, so the re-put gate does not refuse it"


def test_a_catalog_that_cannot_answer_reads_the_record_as_flagged(
    t2_service_env, tmp_path: Path, puts: list[dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreadable() -> Any:
        raise RuntimeError("catalog unreachable")

    monkeypatch.setattr("nexus.doc_sensitivity._catalog_reader", unreadable)
    summary = import_bundle(None, None, None, _bundle(tmp_path, _rec("unknown one")))
    assert summary.docs_failed == 1 and puts == []


def test_the_cli_option_reaches_the_importer(t2_service_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_import(reader, writer, t3, bundle, **kw):
        seen.update(kw)
        return rb.ImportSummary()

    monkeypatch.setattr(rb, "import_bundle", fake_import)
    path = _bundle(tmp_path, _rec("cli one"))
    runner = CliRunner()
    assert runner.invoke(main, ["catalog", "import", str(path)]).exit_code == 0
    assert seen == {}
    assert runner.invoke(main, ["catalog", "import", str(path), "--ack-external-embedder"]).exit_code == 0
    assert seen == {"ack_external_embedder": True}


# ── nx store import ─────────────────────────────────────────────────────────

@pytest.fixture
def db():
    return T3Database(_client=make_vector_test_client(), _ef_override=MiniLMDirectEmbeddingFunction())


def test_a_document_the_catalog_flags_is_refused_into_voyage_though_the_file_does_not_flag_it(db, tmp_path: Path) -> None:
    path = _voyage_file(tmp_path, ("catalog secret", None), ("plain note", False))
    _flag_in_catalog(f"nx-note://{VOYAGE}/catalog secret", "catalog secret", VOYAGE)

    with pytest.raises(ExternalEmbedderRefusedError) as exc:
        import_collection(db=db, input_path=path)
    assert "catalog secret" in exc.value.message and "plain note" not in exc.value.message
    assert "--ack-external-embedder" in exc.value.message
    assert _stored(db, VOYAGE) == 0

    result = import_collection(db=db, input_path=path, ack_external_embedder=True)
    assert result["imported_count"] == 2


def test_a_store_import_whose_catalog_cannot_answer_refuses_for_voyage_and_not_for_bge(
    db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_import_sensitive_external_embedder import _bge_file

    def unreadable() -> Any:
        raise RuntimeError("catalog unreachable")

    monkeypatch.setattr("nexus.doc_sensitivity._catalog_reader", unreadable)
    with pytest.raises(ExternalEmbedderRefusedError):
        import_collection(db=db, input_path=_voyage_file(tmp_path, ("unknown note", False)))
    assert import_collection(db=db, input_path=_bge_file(tmp_path, ("unknown note", False)))["imported_count"] == 1
