# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 named-source sibling rule (nexus-jypmj.7) against the engine substrate.

Three claims the substrate-free file (``test_rdr232_rdr_id_meta.py``) cannot make:

* the indexer's catalog hook writes the frontmatter id into the document's ``meta`` at registration, and the
  next hook run fills it onto a registration made before the stamp existed (the re-index backfill);
* the sibling rule then picks the carrier from the catalog alone: no aspect row, no file on this host;
* aspect rows are keyed by the catalog tumbler string, which is what the resolver passes to
  ``get_by_doc_id`` (critique F1 of nexus-xho6c): the key the worker's own doc-id lookup returns, and the FK
  the engine enforces, is that string.
"""
from __future__ import annotations

import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nexus.aspect_extractor import AspectRecord
from nexus.aspect_worker import _resolve_doc_id_for_enqueue
from nexus.catalog import factory
from nexus.catalog.types import RDR_FRONTMATTER_ID_META_KEY as KEY
from nexus.db.t2 import T2Database
from nexus.doc_indexer import _register_or_lookup_doc_id
from nexus.indexer import _catalog_hook
from nexus.plans.evidence import OwnerResolution, SourceRef
from nexus.plans.primary_evidence import recorded_frontmatter_id, resolve_named_sources
from tests._catalog_fixture_ops import ActiveCatalog, active_reader

pytestmark = [pytest.mark.integration, pytest.mark.t2_tenant]

REPO_HASH = "571b8edd"
COLLECTION = "rdr__nexus-571b8edd"
MAIN = "docs/rdr/rdr-200-nx-answer-continuation-mode.md"
COMPANION = "docs/rdr/rdr-200-phase1-prereg.md"
NO_ID = "docs/rdr/rdr-201-no-id.md"
UNCLOSED = "docs/rdr/rdr-202-unclosed.md"
TEXTS = {
    MAIN: "---\ntitle: main\nid: RDR-200\nstatus: accepted\n---\n# RDR-200\n",
    COMPANION: "---\ntitle: prereg\nid: companion-note\n---\n# prereg\n",
    NO_ID: "---\ntitle: no id\nstatus: draft\n---\n# no id\n",
    UNCLOSED: "---\ntitle: broken\nid: RDR-202\n# the fence never closes\n",
}


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


def _write_repo(root: Path) -> list[tuple[Path, str, str]]:
    files = []
    for rel, text in TEXTS.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        files.append((path, "rdr", COLLECTION))
    return files


def _hook(root: Path, files: list[tuple[Path, str, str]], head: str = "aaa") -> None:
    _catalog_hook(repo=root, repo_name="nexus", repo_hash=REPO_HASH, head_hash=head, indexed_files=files)


def _entry(cat: ActiveCatalog, rel: str):
    owner = cat.owner_for_repo(REPO_HASH)
    entry = cat.by_file_path(owner, rel)
    assert entry is not None, rel
    return entry


# ── registration stamps the id ──────────────────────────────────────────────────────────────────────────


def test_registration_records_the_frontmatter_id_on_the_document(tmp_path: Path) -> None:
    files = _write_repo(tmp_path)
    _hook(tmp_path, files)
    cat = ActiveCatalog()
    assert _entry(cat, MAIN).meta[KEY] == "RDR-200"
    assert _entry(cat, COMPANION).meta[KEY] == "companion-note"
    assert _entry(cat, NO_ID).meta[KEY] == "", "a parsed header with no id is a recorded absence"
    unclosed = _entry(cat, UNCLOSED).meta
    assert unclosed.get(KEY, "absent") is None, "a header that did not parse is a null: unknown, not 'no id'"
    assert unclosed.get("content_hash"), "the stamp rides beside the content hash, which is still written"


def test_a_non_rdr_document_is_not_stamped(tmp_path: Path) -> None:
    path = tmp_path / "docs" / "note.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nid: RDR-200\n---\n", encoding="utf-8")
    _hook(tmp_path, [(path, "prose", "docs__nexus")])
    assert KEY not in _entry(ActiveCatalog(), "docs/note.md").meta


# ── the backfill: a registration that predates the stamp ────────────────────────────────────────────────


class _RecordingWriter:
    """The real catalog writer, with the document updates it is asked for recorded."""

    def __init__(self, inner, log: list[tuple[str, int]]) -> None:
        self._inner, self._log = inner, log

    def update_many(self, updates):
        self._log.append(("update_many", len(updates)))
        return self._inner.update_many(updates)

    def update(self, tumbler, **kw):
        self._log.append(("update", 1))
        return self._inner.update(tumbler, **kw)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_the_next_index_run_fills_the_id_onto_an_unstamped_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registration is made as the previous client made it (no stamp), then the file is indexed again
    with the content and the HEAD unchanged. Nothing else is stale, so only the missing stamp can move it."""
    files = _write_repo(tmp_path)
    with monkeypatch.context() as m:
        m.setattr("nexus.catalog.rdr_canonical.rdr_frontmatter_meta", lambda text: {})
        _hook(tmp_path, files)
    cat = ActiveCatalog()
    assert KEY not in _entry(cat, MAIN).meta and _entry(cat, MAIN).meta.get("content_hash")

    _hook(tmp_path, files)
    assert _entry(cat, MAIN).meta[KEY] == "RDR-200"
    assert _entry(cat, COMPANION).meta[KEY] == "companion-note"
    assert _entry(cat, NO_ID).meta[KEY] == ""
    assert _entry(cat, UNCLOSED).meta.get(KEY, "absent") is None, "still unparsed: still unknown"


def test_a_changed_header_restamps_and_an_unchanged_one_is_not_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _write_repo(tmp_path)
    _hook(tmp_path, files)
    cat = ActiveCatalog()
    writes: list[tuple[str, int]] = []
    real_make = factory.make_catalog_writer
    with monkeypatch.context() as m:
        m.setattr(factory, "make_catalog_writer", lambda *a, **k: _RecordingWriter(real_make(*a, **k), writes))
        _hook(tmp_path, files)  # unchanged content and HEAD
    assert writes == [], "a stamped, unchanged RDR is not rewritten on a warm run"
    # the id changes in the file
    (tmp_path / MAIN).write_text("---\ntitle: main\nid: RDR-299\n---\n# moved\n", encoding="utf-8")
    with monkeypatch.context() as m:
        m.setattr(factory, "make_catalog_writer", lambda *a, **k: _RecordingWriter(real_make(*a, **k), writes))
        _hook(tmp_path, files)
    assert writes, "the changed file is written"
    assert _entry(cat, MAIN).meta[KEY] == "RDR-299"


# ── the sibling rule needs the catalog only ─────────────────────────────────────────────────────────────


def test_the_sibling_rule_picks_the_carrier_with_no_aspect_row_and_no_file_on_this_host(tmp_path: Path) -> None:
    files = _write_repo(tmp_path)
    _hook(tmp_path, files)
    for path, _ct, _col in files:
        path.unlink()  # the server is remote: the file is not here
    got = resolve_named_sources(
        [SourceRef.parse("RDR-200")], OwnerResolution("none"), cat=active_reader(),
        frontmatter_id=recorded_frontmatter_id(None),
    )
    (src,) = got.sources
    assert src.status == "resolved", src.detail
    assert src.tumbler == str(_entry(ActiveCatalog(), MAIN).tumbler) and src.file_path == MAIN
    assert "its frontmatter id is in" in src.detail, "the carrier was found, not guessed"


# ── critique F1: aspect rows are keyed by the catalog tumbler string ────────────────────────────────────


def test_an_aspect_row_is_keyed_by_the_tumbler_string_the_resolver_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.config.default_db_path", lambda: tmp_path / "t2.db")
    files = _write_repo(tmp_path)
    _hook(tmp_path, files)
    cat = ActiveCatalog()
    entry = _entry(cat, MAIN)

    # the worker's own doc-id source for a (collection, path) is the tumbler string
    worker_doc_id = _resolve_doc_id_for_enqueue(COLLECTION, MAIN)
    assert worker_doc_id == str(entry.tumbler)

    with T2Database(tmp_path / "t2.db") as db:
        db.document_aspects.upsert(AspectRecord(
            collection=COLLECTION, source_path=MAIN, problem_formulation=None, proposed_method=None,
            extras={"rdr_id": "RDR-200", "rdr_status": "accepted"}, confidence=1.0,
            extracted_at=datetime.now(UTC).isoformat(), model_version="deterministic",
            extractor_name="rdr-frontmatter-v1", source_uri=f"file://{tmp_path / MAIN}", doc_id=worker_doc_id,
        ))
        # the resolver's call: get_by_doc_id(str(entry.tumbler)); the row is there, and only there
        row = db.document_aspects.get_by_doc_id(str(entry.tumbler))
        assert row is not None and row.extras["rdr_id"] == "RDR-200"
        assert db.document_aspects.get_by_doc_id(entry.file_path) is None, "the path is not a key"
        assert db.document_aspects.get_by_doc_id(f"{entry.tumbler}.9") is None

        # and the whole lookup, with the registration stamp absent (an older registration), reads that row
        unstamped = replace(entry, meta={k: v for k, v in entry.meta.items() if k != KEY})
        got = recorded_frontmatter_id(lambda d, t: db.document_aspects.get_by_doc_id(d, timeout=t))(unstamped, 5.0, "")
        assert (got.known, got.value) == (True, "RDR-200")


# ── the single-file path (nx index rdr <file>) registers with the stamp too ─────────────────────────────


def test_the_standalone_rdr_registration_records_the_id_too(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "docs" / "rdr").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    f = root / MAIN
    f.write_text(TEXTS[MAIN], encoding="utf-8")
    prose = root / "docs" / "guide.md"
    prose.write_text(TEXTS[MAIN], encoding="utf-8")  # the same header in a non-RDR file

    doc_id = _register_or_lookup_doc_id(
        f, "curator-jypmj7", content_type="prose", physical_collection="rdr__1-1__test-embed-768__v1",
    )
    cat = ActiveCatalog()
    assert cat.resolve(doc_id).meta[KEY] == "RDR-200"
    other = _register_or_lookup_doc_id(
        prose, "curator-jypmj7", content_type="prose", physical_collection="docs__1-1__test-embed-768__v1",
    )
    assert KEY not in cat.resolve(other).meta


# ── nexus-jypmj.20: a stamp does not outlive the header that gave it ────────────────────────────────────


def test_a_header_that_stops_parsing_overwrites_the_stamp_with_a_null_and_the_lookup_reads_the_next_source(
    tmp_path: Path,
) -> None:
    """The engine merges ``meta`` with ``||`` and never removes a key, so the stamp is overwritten, not deleted:
    the null has to come back from the engine as a JSON null under the key, and the lookup has to treat it as
    unknown. Before the fix the stale ``RDR-001`` won over the file and the aspect row."""
    rel = "docs/rdr/rdr-001-first.md"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_text("---\ntitle: first\nid: RDR-001\n---\n# first\n", encoding="utf-8")
    files = [(path, "rdr", COLLECTION)]
    _hook(tmp_path, files)
    cat = ActiveCatalog()
    assert _entry(cat, rel).meta[KEY] == "RDR-001"

    path.write_text("---\ntitle: first\nid: RDR-001\n# the fence no longer closes\n", encoding="utf-8")
    _hook(tmp_path, files, head="bbb")
    entry = _entry(cat, rel)
    assert KEY in entry.meta and entry.meta[KEY] is None, "the engine keeps the null under the key"

    # the file is the next source: it does not parse, so the id is unknown, and RDR-001 is not read
    got = recorded_frontmatter_id(None)(replace(entry, source_uri=path.as_uri()), 1.0, "")
    assert not got.known and got.value is None
    # with an aspect row, that row is the next source
    row = AspectRecord(
        collection=COLLECTION, source_path=rel, problem_formulation=None, proposed_method=None,
        extras={"rdr_id": "RDR-009"}, confidence=1.0, extracted_at=datetime.now(UTC).isoformat(),
        model_version="deterministic", extractor_name="rdr-frontmatter-v1", source_uri="", doc_id=str(entry.tumbler),
    )
    got = recorded_frontmatter_id(lambda d, t: row)(entry, 1.0, "")
    assert (got.known, got.value) == (True, "RDR-009")

    # a warm run over the same unparseable file does not write the null again
    writes: list[tuple[str, int]] = []
    real_make = factory.make_catalog_writer
    with pytest.MonkeyPatch.context() as m:
        m.setattr(factory, "make_catalog_writer", lambda *a, **k: _RecordingWriter(real_make(*a, **k), writes))
        _hook(tmp_path, files, head="bbb")
    assert writes == [], "a stored null equals the stamp the run computes"


def test_reindexing_an_existing_registration_with_nx_index_rdr_updates_the_stamp(tmp_path: Path) -> None:
    """nexus-jypmj.7 code review M5: the standalone path stamped only when it minted the document. A second
    run of ``nx index rdr <file>`` finds the registration and must bring its stamp up to date: a changed id,
    a header that stops parsing, and a registration that has no stamp at all."""
    root = tmp_path / "repo"
    (root / "docs" / "rdr").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    f = root / MAIN
    f.write_text(TEXTS[MAIN], encoding="utf-8")

    def register() -> str:
        return _register_or_lookup_doc_id(
            f, "curator-jypmj20", content_type="prose", physical_collection="rdr__1-1__test-embed-768__v1",
        )

    cat = ActiveCatalog()
    first = register()
    assert cat.resolve(first).meta[KEY] == "RDR-200"

    f.write_text("---\ntitle: main\nid: RDR-299\n---\n# moved\n", encoding="utf-8")
    assert register() == first, "the same registration, not a second document"
    assert cat.resolve(first).meta[KEY] == "RDR-299"

    f.write_text("---\ntitle: main\nid: RDR-299\n# the fence no longer closes\n", encoding="utf-8")
    assert register() == first
    assert cat.resolve(first).meta[KEY] is None

    # a registration made before the stamp existed: put the key back to null the way an older client left it
    f.write_text(TEXTS[MAIN], encoding="utf-8")
    writer = factory.make_catalog_writer()
    try:
        writer.update(first, meta={KEY: None})
    finally:
        writer.close()
    assert register() == first
    assert cat.resolve(first).meta[KEY] == "RDR-200"
