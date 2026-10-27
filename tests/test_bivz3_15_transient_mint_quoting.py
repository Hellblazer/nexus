# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-231 (nexus-bivz3.15/.16, code review S3): the five transient ``nx-note://`` mints percent-encode.

``note_uri(..., quote_rest=True)`` is the lookup form: a source path with ``#``, ``?`` or ``%`` must not
become a fragment, a query or a broken escape in the URI handed to ``read_source``. Only
``tests/test_aspect_readers.py`` pinned ``quote_rest`` itself; this file pins each SITE passes
``quote_rest=True``, so dropping it from any one fails here:

* ``aspect_extractor.read_indexed_text``;
* ``aspect_extractor.extract_aspects_batch`` (an empty-content row sourced from its URI);
* the ``nx enrich aspects --dry-run`` read-side prediction;
* the https reader's chroma-first hint (``aspect_readers._read_https_uri``).

(The fifth site, ``extract_aspects`` at ``aspect_extractor.py:1567``, is covered by
``tests/test_aspect_extractor.py``.)
"""
from __future__ import annotations

import pytest

from nexus.aspect_readers import ReadFail, ReadOk

_COLLECTION = "knowledge__quote-pin"
_PATH = "notes/a#b?c%d.md"
_EXPECTED = "nx-note://knowledge__quote-pin/notes/a%23b%3Fc%25d.md"


class _Recorder:
    def __init__(self) -> None:
        self.uris: list[str] = []

    def read(self, uri, **_kw):
        self.uris.append(uri)
        return ReadOk(text="body", metadata={})


@pytest.fixture
def rec() -> _Recorder:
    return _Recorder()


def test_read_indexed_text_encodes_the_path(rec: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus import aspect_extractor as ae

    monkeypatch.setattr(ae, "get_t3", lambda: object())
    monkeypatch.setattr(ae, "read_source", rec.read)
    assert ae.read_indexed_text(collection=_COLLECTION, source_path=_PATH) == "body"
    assert rec.uris == [_EXPECTED]


def test_read_indexed_text_encodes_the_lookup_path_when_one_is_given(rec: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus import aspect_extractor as ae

    monkeypatch.setattr(ae, "get_t3", lambda: object())
    monkeypatch.setattr(ae, "read_source", rec.read)
    ae.read_indexed_text(collection=_COLLECTION, source_path="ignored", lookup_path=_PATH)
    assert rec.uris == [_EXPECTED]


def test_extract_aspects_batch_encodes_the_path_of_an_empty_content_row(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus import aspect_extractor as ae

    seen: list[str] = []

    def _read(uri, **_kw):
        seen.append(uri)
        return ReadFail(reason="empty", detail="pinned: only the URI matters")

    monkeypatch.setattr(ae, "get_t3", lambda: object())
    monkeypatch.setattr(ae, "read_source", _read)
    out = ae.extract_aspects_batch([(_COLLECTION, _PATH, "")])
    assert seen == [_EXPECTED]
    assert len(out) == 1 and getattr(out[0], "uri", None) == _EXPECTED


def test_the_enrich_dry_run_prediction_encodes_the_path(rec: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.commands import enrich as enrich_cmd

    monkeypatch.setattr("nexus.mcp_infra.get_t3", lambda: object())
    monkeypatch.setattr("nexus.aspect_readers.read_source", rec.read)
    monkeypatch.setattr(enrich_cmd, "_build_catalog_doc_id_lookup", lambda: None)
    monkeypatch.setattr(enrich_cmd, "_build_catalog_manifest_lookup", lambda: None)
    monkeypatch.setattr(enrich_cmd, "_chroma_source_id_for_entry", lambda entry: _PATH)

    class _Entry:
        source_uri = ""

    enrich_cmd._dry_run_predict_skips([_Entry()], _COLLECTION)
    assert rec.uris == [_EXPECTED]


def test_the_https_hint_encodes_the_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus import aspect_readers as ar

    seen: list[str] = []

    def _chroma(uri, **_kw):
        seen.append(uri)
        return ReadOk(text="cached", metadata={})

    monkeypatch.setattr(ar, "_read_chroma_uri", _chroma)
    result = ar._read_https_uri("https://example.test/p", t3=object(), chroma_hint=(_COLLECTION, _PATH))
    assert isinstance(result, ReadOk) and result.text == "cached"
    assert seen == [_EXPECTED]
