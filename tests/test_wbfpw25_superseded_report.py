# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-192 Step 13 (nexus-wbfpw.25): ``store_put`` reports the chunks its re-put superseded.

The engine's ``sweep_detail`` entry for a note's own write names the chashes the sweep DELETED
(``swept_chashes``, capped, with ``swept_chashes_truncated``). The note writer carries them to
``NoteWriteResult``; MCP ``store_put`` and ``nx store put`` add a second line,
``Superseded: N chunk(s) removed: [<chash>, ...]``, only when the sweep removed something, so a first
put and an identical re-put read exactly as before. A chunk the sweep kept (another document owns it)
is not reported: the line says what was removed, not what the manifest dropped.

An engine that predates the field answers with counts only; the line then carries the count and no
chashes. The first three tests run the real engine substrate.
"""
from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.catalog.multi_batch_write import write_one_request
from nexus.catalog.note_write import (
    STORED,
    NoteWriteResult,
    PutNoteOutcome,
    superseded_line,
)
from nexus.cli import main
from nexus.corpus import t3_collection_name
from nexus.db.http_vector_client import HttpVectorClient
from nexus.mcp.core import store_put


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _lines(result: str) -> list[str]:
    return result.split("\n")


# ── real engine ───────────────────────────────────────────────────────────────


def test_a_changed_reput_names_the_chunk_it_removed_and_a_first_or_identical_put_do_not(t2_service_env):
    client = HttpVectorClient(tenant=t2_service_env)
    subject, title = "wbfpw25-mcp", "wbfpw25-note"
    v1 = "wbfpw25 version one of the note"
    v2 = "wbfpw25 version TWO of the note, different text"
    with patch("nexus.mcp.core._get_t3", return_value=client):
        first = store_put(content=v1, collection=subject, title=title)
        same = store_put(content=v1, collection=subject, title=title)
        changed = store_put(content=v2, collection=subject, title=title)

    col = t3_collection_name(subject, t3=client)
    assert first == f"Stored: {_chash(v1)} -> {col}", "a first put reads exactly as before"
    assert same == first, "an identical re-put drops nothing and reports nothing"
    lines = _lines(changed)
    assert lines[0] == f"Stored: {_chash(v2)} -> {col}"
    assert lines[1] == f"Superseded: 1 chunk removed: [{_chash(v1)}]"
    assert len(lines) == 2


def test_a_chunk_another_document_owns_is_kept_and_so_not_reported(t2_service_env):
    client = HttpVectorClient(tenant=t2_service_env)
    subject = "wbfpw25-shared"
    shared = "wbfpw25 text two documents share"
    with patch("nexus.mcp.core._get_t3", return_value=client):
        store_put(content=shared, collection=subject, title="owner-a")
        store_put(content=shared, collection=subject, title="owner-b")
        result = store_put(content="wbfpw25 owner-a now says something else", collection=subject, title="owner-a")

    assert "Superseded" not in result, (
        "owner-b still owns the shared chunk, the sweep kept it, and the line reports removals only")
    assert result.startswith("Stored: ")


def test_cli_store_put_prints_the_same_superseded_line(t2_service_env, tmp_path):
    client = HttpVectorClient(tenant=t2_service_env)
    subject, title = "wbfpw25-cli", "wbfpw25-cli-note"
    v1, v2 = "wbfpw25 cli version one", "wbfpw25 cli version two, different"

    def put(body: str):
        f = tmp_path / "note.md"
        f.write_text(body)
        with patch("nexus.commands.store._t3", return_value=client):
            return CliRunner().invoke(main, ["store", "put", str(f), "-c", subject, "-t", title])

    first, changed = put(v1), None
    assert first.exit_code == 0, first.output
    assert "Superseded" not in first.output
    changed = put(v2)
    assert changed.exit_code == 0, changed.output
    assert f"Superseded: 1 chunk removed: [{_chash(v1)}]" in changed.output


# ── the wording, and an engine that predates the field ───────────────────────


def _outcome(**write_kw) -> PutNoteOutcome:
    return PutNoteOutcome(
        status=STORED, collection="knowledge__x__bge-base-en-v15-768__v1",
        write=NoteWriteResult(catalog_doc_id="1.1.1", collection="c", **write_kw))


def test_no_line_when_nothing_was_removed_or_the_write_is_unknown():
    assert superseded_line(_outcome(swept=0, swept_chashes=[])) is None
    assert superseded_line(_outcome(swept=0)) is None
    assert superseded_line(PutNoteOutcome(status=STORED, collection="c")) is None


def test_an_engine_without_the_field_degrades_to_the_count():
    assert superseded_line(_outcome(swept=2, swept_chashes=None)) == "Superseded: 2 chunks removed"
    assert superseded_line(_outcome(swept=1, swept_chashes=None)) == "Superseded: 1 chunk removed"


def test_the_text_line_lists_at_most_three_chashes_and_counts_the_rest():
    ids = [_chash(f"c{i}") for i in range(5)]
    line = superseded_line(_outcome(swept=5, swept_chashes=ids))
    assert line == f"Superseded: 5 chunks removed: [{', '.join(ids[:3])}] (and 2 more)"


def test_a_capped_engine_list_still_reports_the_exact_count():
    ids = [_chash(f"c{i}") for i in range(300)]
    line = superseded_line(_outcome(swept=450, swept_chashes=ids, swept_chashes_truncated=True))
    assert line is not None and line.startswith("Superseded: 450 chunks removed: [") and line.endswith("(and 447 more)")


class _FakeCat:
    def __init__(self, entry: dict) -> None:
        self.entry = entry

    def write_manifest_many(self, docs, **kwargs):
        doc = docs[0][0]
        return {
            "failed_doc_ids": [], "complete_refused": [], "complete_refused_count": 0,
            "swept": self.entry.get("swept", 0), "sweep_skipped": 0, "sweep_detail": [{"doc_id": doc, **self.entry}],
            "dropped_chashes": {doc: []}, "dropped_count": {doc: 0}, "dropped_unknown": [],
            "chunks_written": 0,
        }


def _one_request(entry: dict):
    return write_one_request(
        _FakeCat(entry), doc_id="1.1.1", collection="c", rows=[], chunks=[], dropped="optional")


def test_write_one_request_reads_swept_chashes_from_the_documents_sweep_detail_entry():
    out = _one_request({"swept": 1, "swept_chashes": ["a" * 64], "swept_chashes_truncated": False})
    assert out.swept_chashes == ["a" * 64] and out.swept_chashes_truncated is False
    capped = _one_request({"swept": 9, "swept_chashes": ["a" * 64], "swept_chashes_truncated": True})
    assert capped.swept_chashes_truncated is True


def test_write_one_request_leaves_swept_chashes_unknown_on_an_old_engine():
    out = _one_request({"swept": 1})
    assert out.swept_chashes is None and out.swept == 1


@pytest.mark.parametrize("bad", ["not-a-list", [1, 2], None])
def test_a_malformed_swept_chashes_is_treated_as_absent(bad):
    out = _one_request({"swept": 1, "swept_chashes": bad})
    assert out.swept_chashes is None
