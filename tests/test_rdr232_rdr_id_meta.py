# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 named-source sibling rule (nexus-jypmj.7): the RDR frontmatter ``id`` is recorded on the catalog
document at registration, and the resolver reads it first.

Until this bead the rule read each sibling's id from the ``rdr-frontmatter-v1`` aspect row, which the worker
fills asynchronously, then from the file where the host has it. The indexer has the frontmatter when it
registers the document, so it now writes the raw ``id`` into the document's ``meta`` under
``RDR_FRONTMATTER_ID_META_KEY`` and :func:`recorded_frontmatter_id` trusts that key before it asks the aspect
store. This file is substrate-free; ``tests/integration/test_rdr232_rdr_id_meta_engine.py`` runs the same claims against the engine.
"""
from __future__ import annotations

from typing import Any

import pytest

from nexus.aspect_extractor import _parse_rdr_aspects
from nexus.catalog.rdr_canonical import rdr_frontmatter_meta
from nexus.catalog.tumbler import Tumbler
from nexus.catalog.types import RDR_FRONTMATTER_ID_META_KEY, CatalogEntry
from nexus.plans.primary_evidence import (
    FILE_ABSENT,
    NO_ASPECT_ROW,
    FrontmatterId,
    _file_frontmatter_id,
    recorded_frontmatter_id,
)

KEY = RDR_FRONTMATTER_ID_META_KEY
COL = "rdr__1-2__bge-base-en-v15-768__v1"
PATH = "docs/rdr/rdr-200-nx-answer-continuation-mode.md"


def _entry(tumbler: str = "1.2.20", *, meta: dict | None = None, source_uri: str = "") -> CatalogEntry:
    return CatalogEntry(
        tumbler=Tumbler.parse(tumbler), title="t", author="", year=0, content_type="rdr", file_path=PATH, corpus="",
        physical_collection=COL, chunk_count=0, head_hash="", indexed_at="", meta=meta or {}, source_uri=source_uri,
    )


# ── what registration records ───────────────────────────────────────────────────────────────────────────


def test_the_key_is_a_stable_catalog_meta_name() -> None:
    assert KEY == "rdr_frontmatter_id"


@pytest.mark.parametrize("header, stamped", [
    ("---\nid: RDR-200\n---\n# x\n", "RDR-200"),
    ("---\nid: 200\n---\n# x\n", 200),
    ("---\nid: companion-note\n---\n# x\n", "companion-note"),
    ("---\ntitle: y\n---\n# x\n", ""),      # parsed, no id: a recorded absence, not an unknown
    ("# no frontmatter at all\n", ""),       # the file read treats this as parsed-with-no-id too
])
def test_the_registration_meta_carries_the_raw_id_as_the_file_read_would(header: str, stamped: Any) -> None:
    assert rdr_frontmatter_meta(header) == {KEY: stamped}


def test_an_unclosed_fence_stamps_null() -> None:
    """The header did not parse, so nothing is known: a null, which overwrites an earlier id (meta merges and
    never removes a key, nexus-jypmj.20), and the aspect row or the file decide."""
    assert rdr_frontmatter_meta("---\nid: RDR-200\n# body with no closing fence\n") == {KEY: None}


def test_a_header_that_cannot_be_decoded_stamps_null() -> None:
    from nexus.catalog.rdr_canonical import rdr_frontmatter_meta_from_bytes  # noqa: PLC0415

    assert rdr_frontmatter_meta_from_bytes(b"---\nid: RDR-200\n---\n\xff\xfe") == {KEY: None}
    assert rdr_frontmatter_meta_from_bytes(b"---\nid: RDR-200\n---\n") == {KEY: "RDR-200"}


@pytest.mark.parametrize("text", [
    "---\nid: RDR-200\n---\n",
    "---\nid: 040\n---\n",
    "---\nid: [a, b]\n---\n",
    "---\nid: companion-note\n---\n",
    "---\ntitle: z\n---\n",
    "no header\n",
])
def test_the_stamped_id_is_the_id_the_file_fallback_and_the_aspect_row_read(text: str, tmp_path) -> None:
    """One parser, three readers. A stamp that read differently from the file would let the sibling rule pick
    different carriers depending on which source answered."""
    d = tmp_path / "docs" / "rdr"
    d.mkdir(parents=True)
    f = d / "rdr-200-x.md"
    f.write_text(text, encoding="utf-8")
    from_file = _file_frontmatter_id(_entry(source_uri=f.as_uri()))
    stamped = rdr_frontmatter_meta(text)
    assert from_file.known and stamped, "both read this header"
    assert FrontmatterId.of(stamped[KEY]) == from_file
    assert (_parse_rdr_aspects(text)["extras"].get("rdr_id") or "") == (stamped[KEY] or "")


# ── what the resolver reads ─────────────────────────────────────────────────────────────────────────────


def test_the_resolver_reads_the_registration_meta_before_asking_the_aspect_store() -> None:
    asked: list[str] = []

    def aspects(doc_id: str, timeout: float) -> Any:
        asked.append(doc_id)
        raise AssertionError("the aspect store must not be asked when the registration carries the id")

    got = recorded_frontmatter_id(aspects)(_entry(meta={KEY: "RDR-200"}), 1.0, "")
    assert (got.known, got.value) == (True, "RDR-200")
    assert asked == []


def test_a_recorded_absence_in_meta_is_known_and_empty() -> None:
    got = recorded_frontmatter_id(lambda d, t: None)(_entry(meta={KEY: ""}), 1.0, "")
    assert (got.known, got.value) == (True, "")


def test_a_null_stamp_falls_through_to_the_aspect_row_not_the_stale_id() -> None:
    """nexus-jypmj.20: the header stopped parsing, so the indexer overwrote the id with null. The lookup reads
    the next source, an aspect row here, never the id the document carried before."""
    from types import SimpleNamespace  # noqa: PLC0415

    row = SimpleNamespace(extractor_name="rdr-frontmatter-v1", confidence=1.0, extras={"rdr_id": "RDR-300"})
    got = recorded_frontmatter_id(lambda d, t: row)(_entry(meta={KEY: None}), 1.0, "")
    assert (got.known, got.value) == (True, "RDR-300")
    nothing = recorded_frontmatter_id(lambda d, t: None)(_entry(meta={KEY: None}), 1.0, "")
    assert not nothing.known and nothing.reasons == (NO_ASPECT_ROW, "no_file_source")


def test_without_the_key_the_aspect_row_and_the_file_still_decide() -> None:
    """An older registration: the meta has a content hash and nothing else."""
    got = recorded_frontmatter_id(lambda d, t: None)(_entry(meta={"content_hash": "ab" * 32}), 1.0, "")
    assert not got.known and got.reasons == (NO_ASPECT_ROW, "no_file_source")
    gone = recorded_frontmatter_id(None)(_entry(meta={"content_hash": "ab" * 32}, source_uri="file:///nowhere/docs/rdr/x.md"), 1.0, "")
    assert not gone.known and gone.reasons == (FILE_ABSENT,)


def test_a_failing_aspect_store_does_not_matter_when_the_registration_has_the_id() -> None:
    def aspects(doc_id: str, timeout: float) -> Any:
        raise TimeoutError("t2 slow")

    lookup = recorded_frontmatter_id(aspects)
    assert lookup(_entry("1.2.20", meta={KEY: "RDR-200"}), 1.0, "").value == "RDR-200"
    assert lookup(_entry("1.2.21", meta={KEY: "companion-note"}), 1.0, "").value == "companion-note"


# ── nexus-jypmj.20: restamping an existing registration (substrate-free; recording writer double) ───────


class _RecordingWriter:
    """A catalog writer double: records ``update`` calls, and raises *error* from them when one is given."""

    def __init__(self, error: Exception | None = None) -> None:
        self.updates: list[tuple[str, dict]] = []
        self.error = error
        self.closed = False

    def update(self, tumbler: Any, **fields: Any) -> None:
        if self.error is not None:
            raise self.error
        self.updates.append((str(tumbler), fields))

    def close(self) -> None:
        self.closed = True


def _rdr_file(tmp_path, text: str | bytes):
    f = tmp_path / "docs" / "rdr" / "rdr-200-x.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    return f


def _existing(meta: Any, tumbler: str = "1.2.20"):
    from types import SimpleNamespace  # noqa: PLC0415

    return SimpleNamespace(tumbler=tumbler, meta=meta, physical_collection=COL)


def test_restamp_writes_nothing_when_the_stored_stamp_equals_the_files(tmp_path) -> None:
    """A warm ``nx index rdr`` re-run costs no catalog write: the stored stamp already equals the file's. Fails with
    the equality guard removed (it would write the same stamp every run)."""
    from nexus.doc_indexer import _restamp_rdr_frontmatter  # noqa: PLC0415

    f = _rdr_file(tmp_path, "---\nid: RDR-200\n---\n# x\n")
    for stored in ({KEY: "RDR-200", "content_hash": "ab" * 32}, {KEY: "RDR-200"}):
        writer = _RecordingWriter()
        _restamp_rdr_frontmatter(writer, _existing(stored), f, "rdr")
        assert writer.updates == [], stored
    # the same holds for a stored null against a file whose header does not parse
    unparsed = _rdr_file(tmp_path, "---\nid: RDR-200\n# never closes\n")
    writer = _RecordingWriter()
    _restamp_rdr_frontmatter(writer, _existing({KEY: None}), unparsed, "rdr")
    assert writer.updates == [], "a stored null equals the null the file stamps"


@pytest.mark.parametrize("stored", [{KEY: "RDR-100"}, {"content_hash": "ab" * 32}, {KEY: None}, {}, None])
def test_restamp_writes_the_files_stamp_when_the_stored_one_differs_or_is_missing(tmp_path, stored: Any) -> None:
    """The positive control for the no-write test: a different, absent or null stored stamp is brought up to date."""
    from nexus.doc_indexer import _restamp_rdr_frontmatter  # noqa: PLC0415

    f = _rdr_file(tmp_path, "---\nid: RDR-200\n---\n# x\n")
    writer = _RecordingWriter()
    _restamp_rdr_frontmatter(writer, _existing(stored), f, "rdr")
    assert writer.updates == [("1.2.20", {"meta": {KEY: "RDR-200"}})]


def test_restamp_for_another_content_type_or_an_unreadable_file_writes_nothing(tmp_path) -> None:
    from nexus.doc_indexer import _restamp_rdr_frontmatter  # noqa: PLC0415

    f = _rdr_file(tmp_path, "---\nid: RDR-200\n---\n")
    writer = _RecordingWriter()
    _restamp_rdr_frontmatter(writer, _existing({}), f, "prose")
    _restamp_rdr_frontmatter(writer, _existing({}), tmp_path / "missing.md", "rdr")
    assert writer.updates == []


def test_a_failing_restamp_write_is_logged_and_the_registered_document_id_still_comes_back(tmp_path, monkeypatch) -> None:
    """Failure isolation (nexus-jypmj.20): the tumbler is already resolved, so a failed advisory stamp write is
    logged and ``_register_or_lookup_doc_id`` still returns the existing document id. Fails if the write's
    ``except`` is removed: the error would reach the function's catch-all, which returns ``""`` and drops the run
    out of the fence."""
    from structlog.testing import capture_logs  # noqa: PLC0415

    from nexus import doc_indexer  # noqa: PLC0415
    from nexus.catalog import factory  # noqa: PLC0415

    f = _rdr_file(tmp_path, "---\nid: RDR-200\n---\n# x\n")
    existing = _existing({KEY: "RDR-100"})
    writer = _RecordingWriter(error=RuntimeError("catalog write refused"))

    class _Reader:
        def curator_owner_tumbler_by_name(self, name: str) -> Tumbler:
            return Tumbler.parse("1.2")

        def close(self) -> None:
            pass

    monkeypatch.setattr(factory, "make_catalog_reader", lambda *a, **k: _Reader())
    monkeypatch.setattr(factory, "make_catalog_writer", lambda *a, **k: writer)
    monkeypatch.setattr(doc_indexer, "_repo_home_for", lambda *a, **k: None)
    monkeypatch.setattr(doc_indexer, "_find_curator_document", lambda *a, **k: existing)
    with capture_logs() as logs:
        got = doc_indexer._register_or_lookup_doc_id(f, "curator-x", content_type="rdr", physical_collection=COL)
    assert got == "1.2.20", "the resolved tumbler survives a failed stamp write"
    failed = [e for e in logs if e["event"] == "rdr_frontmatter_restamp_failed"]
    assert len(failed) == 1 and failed[0]["tumbler"] == "1.2.20" and failed[0]["log_level"] == "warning"
    assert writer.closed


# ── nexus-jypmj.20: an RDR emptied to zero bytes is stamped the same on both index paths ────────────────


def test_an_empty_file_stamps_the_absent_id_on_both_index_paths(tmp_path) -> None:
    """``nx index repo`` (``_rdr_register_stamp``) used to skip an empty file (``and raw_bytes``) while ``nx index
    rdr`` (``_rdr_frontmatter_stamp``) stamped it, so an RDR truncated to zero bytes kept its old id through the
    repo path. The shared rule reads a file with no header at all as parsed with no id, so both stamp ``""``."""
    from nexus.doc_indexer import _rdr_frontmatter_stamp  # noqa: PLC0415
    from nexus.indexer import _rdr_register_stamp  # noqa: PLC0415

    f = _rdr_file(tmp_path, b"")
    assert _rdr_register_stamp("rdr", b"") == {KEY: ""} == _rdr_frontmatter_stamp(f, "rdr")
    assert _rdr_register_stamp("rdr", b"") == rdr_frontmatter_meta("")
    assert FrontmatterId.of("") == _file_frontmatter_id(_entry(source_uri=f.as_uri())), "the file read agrees"


def test_the_repo_indexer_stamps_what_it_read_and_nothing_for_what_it_could_not_read() -> None:
    from nexus.indexer import _rdr_register_stamp  # noqa: PLC0415

    assert _rdr_register_stamp("rdr", b"---\nid: RDR-200\n---\n") == {KEY: "RDR-200"}
    assert _rdr_register_stamp("rdr", b"---\nid: RDR-200\n# open\n") == {KEY: None}
    assert _rdr_register_stamp("rdr", None) == {}, "an unreadable file says nothing about its header"
    assert _rdr_register_stamp("prose", b"---\nid: RDR-200\n---\n") == {}
