# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 item 8 (nexus-rfvyy.6.20, Sam 2026-11-05): ``nx store import`` refuses a document the file flags
sensitive when the target collection embeds with Voyage, unless ``--ack-external-embedder`` is given.

The rule is the index verbs' (``sensitive_write.is_external_embedder``); the flag comes from the file, so
the catalog is never asked. What each group pins:

* the refusal: names the document and the flag, writes nothing (not a chunk, not a catalog row), and
  refuses the whole file when only one of its documents is flagged;
* the exits: a bge target, an unflagged document, and the acknowledgement all import as before, and the
  acknowledgement prints the disclosure the verbs print (local and cloud wording);
* the CLI: ``--ack-external-embedder`` exists on ``nx store import`` and reaches the importer.

The groups run on an in-memory T3 handle (the importer reads collection stats, so the module takes a tenant); the
last test runs on the real engine to show a refused import registers no catalog document.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.catalog.factory import make_catalog_reader
from nexus.cli import main
from nexus.db.http_vector_client import HttpVectorClient
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction
from nexus.db.t3 import T3Database
from nexus.errors import NexusError
from nexus.exporter import import_collection
from nexus.sensitive_write import ExternalEmbedderRefusedError
from tests.conftest import make_vector_test_client
from tests.test_exporter import _write_nxexp_records

pytestmark = pytest.mark.t2_tenant

VOYAGE = "knowledge__rfvyy620__voyage-context-3__v1"
BGE = "knowledge__rfvyy620__bge-base-en-v15-768__v1"


def _owner(title: str, *, sensitive: bool | None) -> dict:
    owner = {"source_uri": f"nx-note://{VOYAGE}/{title}", "title": title, "content_type": "knowledge", "position": 0}
    if sensitive is not None:
        owner["sensitive"] = sensitive
    return owner


def _file(tmp_path, collection: str, *docs: tuple[str, bool | None], model: str, dim: int):
    records = [
        {"id": f"{i:064x}", "document": f"body of {title}", "metadata": {"title": title},
         "owner": _owner(title, sensitive=flag)}
        for i, (title, flag) in enumerate(docs, start=1)
    ]
    path = tmp_path / "f.nxexp"
    _write_nxexp_records(path, collection, records, embedding_model=model, dim=dim)
    return path


def _voyage_file(tmp_path, *docs):
    return _file(tmp_path, VOYAGE, *docs, model="voyage-context-3", dim=1024)


def _bge_file(tmp_path, *docs):
    return _file(tmp_path, BGE, *docs, model="bge-base-en-v15-768", dim=768)


def _stored(db, collection: str) -> int:
    try:
        return len(db.get_collection(collection).get(include=[])["ids"])
    except Exception:  # noqa: BLE001 — a collection that was never created holds nothing
        return 0


@pytest.fixture
def db():
    return T3Database(_client=make_vector_test_client(), _ef_override=MiniLMDirectEmbeddingFunction())


# ── the refusal ─────────────────────────────────────────────────────────────


def test_a_flagged_document_into_a_voyage_collection_is_refused_and_nothing_is_written(db, tmp_path) -> None:
    path = _voyage_file(tmp_path, ("secret memo", True))
    with pytest.raises(ExternalEmbedderRefusedError) as exc:
        import_collection(db=db, input_path=path)
    text = exc.value.message
    assert "secret memo" in text, "the message names the document"
    assert "--ack-external-embedder" in text, "and the flag that accepts Voyage"
    assert VOYAGE in text
    assert "Nothing was written" in text
    assert _stored(db, VOYAGE) == 0


def test_one_flagged_document_refuses_the_whole_file(db, tmp_path) -> None:
    path = _voyage_file(tmp_path, ("plain note", False), ("secret memo", True), ("old export note", None))
    with pytest.raises(ExternalEmbedderRefusedError) as exc:
        import_collection(db=db, input_path=path)
    assert "secret memo" in exc.value.message
    assert "plain note" not in exc.value.message
    assert _stored(db, VOYAGE) == 0, "the unflagged documents of the file are not written either"


def test_the_refusal_is_a_runtime_error_too(db, tmp_path) -> None:
    """A batch loop that survives a per-record RuntimeError survives it; the CLI turns it into a clean exit."""
    with pytest.raises(RuntimeError):
        import_collection(db=db, input_path=_voyage_file(tmp_path, ("secret memo", True)))


def test_a_collection_override_into_voyage_is_refused_the_same_way(db, tmp_path, cloud_mode) -> None:
    """The gate reads the TARGET, not the file's header: a bge export imported into a Voyage name is Voyage."""
    path = _file(tmp_path, BGE, ("secret memo", True), model="voyage-context-3", dim=1024)
    with pytest.raises(ExternalEmbedderRefusedError):
        import_collection(db=db, input_path=path, target_collection=VOYAGE)
    assert _stored(db, VOYAGE) == 0


# ── the exits ───────────────────────────────────────────────────────────────


def test_a_flagged_document_into_a_bge_collection_imports_normally(db, tmp_path, capsys) -> None:
    result = import_collection(db=db, input_path=_bge_file(tmp_path, ("secret memo", True)))
    assert result["imported_count"] == 1
    assert _stored(db, BGE) == 1
    assert "sensitive:" not in capsys.readouterr().err, "a local embedder prints no disclosure"


def test_an_unflagged_document_into_a_voyage_collection_imports_normally(db, tmp_path) -> None:
    result = import_collection(db=db, input_path=_voyage_file(tmp_path, ("plain note", False), ("old export note", None)))
    assert result["imported_count"] == 2


def test_the_acknowledgement_imports_and_prints_the_local_disclosure(db, tmp_path, capsys) -> None:
    result = import_collection(
        db=db, input_path=_voyage_file(tmp_path, ("secret memo", True)), ack_external_embedder=True)
    assert result["imported_count"] == 1
    assert _stored(db, VOYAGE) == 1
    err = capsys.readouterr().err
    assert "sensitive: acknowledged" in err and "Voyage" in err and VOYAGE in err


def test_the_acknowledgement_in_cloud_mode_prints_the_tenant_disclosure(db, tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
    result = import_collection(
        db=db, input_path=_voyage_file(tmp_path, ("secret memo", True)), ack_external_embedder=True)
    assert result["imported_count"] == 1
    assert "managed cloud tenant" in capsys.readouterr().err


def test_an_ack_with_nothing_flagged_prints_nothing(db, tmp_path, capsys) -> None:
    import_collection(db=db, input_path=_voyage_file(tmp_path, ("plain note", False)), ack_external_embedder=True)
    assert "sensitive:" not in capsys.readouterr().err


# ── the CLI ─────────────────────────────────────────────────────────────────


def test_the_cli_refuses_without_the_flag_and_imports_with_it(db, tmp_path) -> None:
    path = _voyage_file(tmp_path, ("secret memo", True))
    runner = CliRunner()
    with patch("nexus.commands.store._t3", return_value=db):
        refused = runner.invoke(main, ["store", "import", str(path)])
        assert refused.exit_code != 0
        assert "secret memo" in refused.output and "--ack-external-embedder" in refused.output
        assert "Traceback" not in refused.output
        assert _stored(db, VOYAGE) == 0

        accepted = runner.invoke(main, ["store", "import", str(path), "--ack-external-embedder"])
    assert accepted.exit_code == 0, accepted.output
    assert "Imported 1 records" in accepted.output
    assert "sensitive: acknowledged" in accepted.output, "the disclosure reaches the terminal"


# ── on the real engine ──────────────────────────────────────────────────────


def test_a_refused_import_registers_no_catalog_document(t2_service_env, tmp_path) -> None:
    client = HttpVectorClient(tenant=t2_service_env)
    path = _voyage_file(tmp_path, ("rfvyy620 secret memo", True))
    with pytest.raises((ExternalEmbedderRefusedError, NexusError)) as exc:
        import_collection(db=client, input_path=path)
    assert isinstance(exc.value, ExternalEmbedderRefusedError), "refused by the gate, not by a later failure"
    assert make_catalog_reader().by_source_uri(f"nx-note://{VOYAGE}/rfvyy620 secret memo") is None
