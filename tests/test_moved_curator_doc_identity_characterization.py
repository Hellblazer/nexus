# SPDX-License-Identifier: AGPL-3.0-or-later
"""Characterization of what nexus does when a curator-owned PDF is indexed again from a new path after it moved
(GH #1571), and what ``nx catalog owners --execute move`` (RDR-254, 254-P2-S4) does about it.

The registration tests pin the behaviour that is deliberately unchanged: a moved file registered again is a NEW
document, and the engine's unique index still refuses the raw ``update`` repair. The first two tests then run the
move command on the state they pin and assert its result: one live row at the new path, holding the old tumbler.
The third (two byte-identical copies) is unchanged: two files that both exist are two documents.

The tests pin the behaviour; none of the characterization assertions says what it SHOULD be. They drive the
single-file PDF pre-flight (``nexus.doc_indexer._register_or_lookup_doc_id``, the call ``index_pdf`` makes)
against the live test catalog, with no extraction or embedding, and stamp ``meta.content_hash`` the way the
PDF writer does (``stamp_registered_content_hash``) so the file hash is on both rows.

What they show, from the code at 06ff9a75c:

* A curator PDF's identity is its path (``file_path`` and the ``source_uri`` the engine derives from it);
  ``_find_curator_document`` says a moved document is a new document at its new path.
* The registration never receives the file's bytes or hash, so ``meta.content_hash`` plays no part in it.
* ``nx catalog update <old> --source-uri file://<new>`` after the new path has its own row meets the
  partial unique index ``ux_catalog_documents_live_source_uri`` (catalog-016) and the engine answers 409.
* Two byte-identical copies that both exist are two documents.
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from nexus.cli import main
from tests._catalog_fixture_ops import ActiveCatalog

pytestmark = pytest.mark.integration

_COLLECTION = "knowledge__moved-pdf-char__test-embed-768__v1"
_BYTES = b"%PDF-1.4\nsame bytes at every path\n%%EOF\n"
_SHA = hashlib.sha256(_BYTES).hexdigest()
_SEQ = [0]


def _next() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _pdf(root: Path, name: str = "x.pdf") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_BYTES)
    return path


def _register(path: Path, corpus: str) -> tuple[str, bool]:
    """The pre-flight ``index_pdf`` runs: ``(doc_id, created)``, then the file-hash stamp the PDF writer adds."""
    from nexus.catalog.multi_batch_write import stamp_registered_content_hash
    from nexus.doc_indexer import _register_or_lookup_doc_id

    doc_id, created = _register_or_lookup_doc_id(
        path, corpus, content_type="paper", physical_collection=_COLLECTION, with_created=True,
    )
    assert doc_id, "the pre-flight must resolve or mint a document"
    assert stamp_registered_content_hash(ActiveCatalog(), doc_id, _SHA) is True
    return doc_id, bool(created)


def _raw_uri(path: Path) -> str:
    return "file://" + str(path)


def _move(old_id: str, new: Path, *flags: str):
    """The repair: ``nx catalog owners --execute move --document <old> --new-path <new>``."""
    return CliRunner().invoke(
        main, ["catalog", "owners", "--execute", "move", "--document", old_id, "--new-path", str(new), *flags])


def _content_hash(entry) -> str:
    return (getattr(entry, "meta", None) or {}).get("content_hash", "")


@pytest.mark.t2_tenant
def test_a_moved_curator_pdf_registers_as_a_new_document_and_the_move_command_keeps_the_old_tumbler(tmp_path):
    """characterization plus the command: pins current behaviour for the moved-path identity RDR; a change in the
    registration half is a design decision, not a regression.

    /a/x.pdf is registered, the file moves to /b/x.pdf (same bytes), and it is registered again. A second
    document is minted at the new path; the old row keeps its old path, stays live and chunk-bearing in the
    sense that nothing touches it, and both rows carry the same file hash that nothing compared. Then
    ``nx catalog owners --execute move`` leaves ONE live row at the new path, and it is the old tumbler.
    """
    cat = ActiveCatalog()
    corpus = f"moved-pdf-{_next()}"
    old = _pdf(tmp_path / "a")
    old_id, old_created = _register(old, corpus)
    assert old_created is True

    new = tmp_path / "b" / "x.pdf"
    new.parent.mkdir(parents=True)
    shutil.move(str(old), str(new))
    assert not old.exists() and new.read_bytes() == _BYTES

    new_id, new_created = _register(new, corpus)

    assert new_created is True, "a moved file is a NEW document, not a resolve onto the old row"
    assert new_id != old_id
    old_row, new_row = cat.resolve(old_id), cat.resolve(new_id)
    assert old_row.file_path == str(old) and new_row.file_path == str(new)
    assert old_row.source_uri == _raw_uri(old) and new_row.source_uri == _raw_uri(new)
    assert _content_hash(old_row) == _content_hash(new_row) == _SHA, (
        "the same file hash sits on both rows; the registration never looked at it"
    )
    # the old row is not tombstoned, aliased or re-pointed: it is still live at its dead path
    assert not getattr(old_row, "alias_of", "")
    assert not getattr(new_row, "alias_of", "")
    assert str(cat.by_source_uri(_raw_uri(old)).tumbler) == old_id
    assert str(cat.by_source_uri(_raw_uri(new)).tumbler) == new_id
    # re-registering either path resolves to its own row, never to the other
    assert _register(new, corpus) == (new_id, False)

    # the repair (RDR-254): the dry run writes nothing, the write leaves one live row at the new path, the OLD tumbler
    dry = _move(old_id, new)
    assert dry.exit_code == 0, dry.output
    assert cat.resolve(old_id).file_path == str(old) and str(cat.by_source_uri(_raw_uri(new)).tumbler) == new_id
    done = _move(old_id, new, "--no-dry-run", "--confirm")
    assert done.exit_code == 0, done.output
    moved = cat.resolve(old_id)
    assert moved.file_path == str(new) and moved.source_uri == _raw_uri(new)
    assert str(cat.by_source_uri(_raw_uri(new)).tumbler) == old_id, "one live row holds the new path: the old tumbler"
    assert cat.by_source_uri(_raw_uri(old)) is None, "nothing is left at the dead path"
    assert _content_hash(moved) == _SHA
    assert getattr(cat.resolve(new_id, follow_alias=False), "source_uri", "") == "", "the new row was absorbed"


@pytest.mark.t2_tenant
def test_pointing_the_old_row_at_the_new_path_hits_the_unique_index_and_the_move_command_does_it(tmp_path):
    """characterization plus the command: pins current behaviour for the moved-path identity RDR; a change in the
    ``update`` half is a design decision, not a regression.

    After the move has been registered a second time, the obvious repair, ``nx catalog update <old>
    --source-uri file://<new>``, goes through ``writer.update(old, source_uri=...)`` and is refused by the
    engine: the new path already holds the live row for that URI (ux_catalog_documents_live_source_uri). The
    move command does what that update cannot: it absorbs the row at the new path, then re-points the old one.
    """
    cat = ActiveCatalog()
    corpus = f"moved-pdf-{_next()}"
    old = _pdf(tmp_path / "a")
    old_id, _ = _register(old, corpus)
    new = tmp_path / "b" / "x.pdf"
    new.parent.mkdir(parents=True)
    shutil.move(str(old), str(new))
    new_id, _ = _register(new, corpus)

    with pytest.raises(httpx.HTTPStatusError) as raised:
        cat.update(old_id, source_uri=_raw_uri(new))

    assert raised.value.response.status_code == 409, raised.value.response.text
    body = raised.value.response.json()
    assert body == {
        "error": "integrity constraint violation",
        "sqlstate": "23505",
        "constraint": "ux_catalog_documents_live_source_uri",
    }, body
    # nothing moved: both rows keep their own URI
    assert cat.resolve(old_id).source_uri == _raw_uri(old)
    assert cat.resolve(new_id).source_uri == _raw_uri(new)

    # the command succeeds where the raw update was refused: no 409, one live row at the new path, the old tumbler
    done = _move(old_id, new, "--no-dry-run", "--confirm")
    assert done.exit_code == 0, done.output
    assert cat.resolve(old_id).source_uri == _raw_uri(new)
    assert str(cat.by_source_uri(_raw_uri(new)).tumbler) == old_id


@pytest.mark.t2_tenant
def test_two_byte_identical_copies_at_two_paths_are_two_separate_documents(tmp_path):
    """characterization: pins current behaviour for the moved-path identity RDR; a change here is a design decision, not a regression.

    /a/x.pdf and /b/x.pdf both exist with the same bytes. Each registers its own document under the shared
    curator owner (the corpus names the owner); neither is an alias of the other, and the shared file hash
    does not make them one.
    """
    cat = ActiveCatalog()
    corpus = f"moved-pdf-{_next()}"
    a = _pdf(tmp_path / "a")
    b = _pdf(tmp_path / "b")
    assert a.read_bytes() == b.read_bytes()

    a_id, a_created = _register(a, corpus)
    b_id, b_created = _register(b, corpus)

    assert a_created is True and b_created is True
    assert a_id != b_id
    assert a_id.rsplit(".", 1)[0] == b_id.rsplit(".", 1)[0], "both sit under the same curator owner"
    a_row, b_row = cat.resolve(a_id), cat.resolve(b_id)
    assert (a_row.file_path, b_row.file_path) == (str(a), str(b))
    assert (a_row.source_uri, b_row.source_uri) == (_raw_uri(a), _raw_uri(b))
    assert _content_hash(a_row) == _content_hash(b_row) == _SHA
    assert not getattr(a_row, "alias_of", "") and not getattr(b_row, "alias_of", "")
    assert _register(a, corpus) == (a_id, False) and _register(b, corpus) == (b_id, False)
