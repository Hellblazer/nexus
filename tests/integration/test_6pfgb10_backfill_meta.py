# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.10 (RDR-236 Phase 1) against the real engine: ``nx catalog backfill-meta``.

One journey over notes and indexed documents whose catalog entries predate the Phase 1 homes (their
``meta`` holds no note key / no extraction key) while their chunk rows hold the values:

* note A: a single owner, written by itself (stamp AND title): takes ttl_days, put_at, tags (as an array),
  category and source_agent from its row;
* notes B1 and B2: identical text, one shared row that B2 wrote: B2 takes the row, B1 is skipped and counted;
* note C: a curated category and no put_at: the category survives, put_at is filled, tags are not;
* note D: an unstamped legacy row it solely owns: tags and category, put_at, and ttl_days 0 (never the row's 30);
* papers (never stamped with tags or category by this verb; source_format is derived and stamped on every one,
  shared chunks included) P1 (sole owner of rows that carry extraction, override True, frecency, author), P2 (curated author
  and extraction_method survive), P3 and P4 (one shared chunk: skipped), P5 (override False is never written),
  P6 (its two rows disagree: residue), P7 (its row is stamped after the document: not attributable);

The dry run writes nothing, ``--apply`` writes, and a second ``--apply`` stamps nothing.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.aspect_readers import uri_for
from nexus.catalog import store_hook
from nexus.catalog.factory import make_catalog_reader, make_catalog_writer
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.note_write import STORED, put_note
from nexus.cli import main
from nexus.commands.enrich import enrich
from nexus.corpus import t3_collection_name
from nexus.db.http_vector_client import HttpVectorClient
from tests._chunk_seed import seed_chunks_direct

pytestmark = pytest.mark.integration

DOCS = "docs__6pfgb10__bge-base-en-v15-768__v1"


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _keyless_put(**kw):
    """``put_note`` as a client before nexus-6pfgb.5 wrote it: no note key reaches the entry's meta."""
    with patch.object(store_hook, "note_catalog_meta", lambda **_k: ({}, {})):
        out = put_note(**kw)
    assert out.status == STORED, out.reason
    return out


def _run(*args: str) -> dict:
    result = CliRunner().invoke(main, ["catalog", "backfill-meta", "--json", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output[result.output.index("{"):])


def test_backfill_meta_journey(t2_service_env):
    client = HttpVectorClient(tenant=t2_service_env)
    notes = t3_collection_name("6pfgb10", t3=client)
    reader = make_catalog_reader()
    assert reader is not None

    # ── notes ──
    _keyless_put(content="6pfgb10 note A body", collection=notes, title="A", tags="x,y", category="design",
                 ttl_days=7, source_agent="agent-a")
    _keyless_put(content="6pfgb10 shared body", collection=notes, title="B1", tags="b1", category="one")
    _keyless_put(content="6pfgb10 shared body", collection=notes, title="B2", tags="b2", category="two", ttl_days=3)
    c = _keyless_put(content="6pfgb10 note C body", collection=notes, title="C", tags="stale", category="stale")
    d = _keyless_put(content="6pfgb10 note D body", collection=notes, title="D", tags="legacy", category="finding", ttl_days=30)
    writer = make_catalog_writer()
    writer.update(reader.by_source_uri(uri_for(notes, "C")).tumbler, meta={"category": "curated"})
    # D's row as a pre-stamp client left it: it names nobody (row_written_by refuses it), so only sole
    # ownership lets it give tags, and the row's ttl_days 30 must never reach the entry.
    seed_chunks_direct(notes, ids=[d.chunk_ids[0]], documents=["6pfgb10 note D body"], embed=True,
                       metadatas=[{"catalog_doc_id": "", "title": ""}])

    # ── indexed documents ──
    def paper(cat, owner, title, *, author="", meta=None):
        return str(cat.register(owner, title, content_type="paper", file_path=f"{title}.pdf", physical_collection=DOCS,
                                chunk_count=1, author=author, meta=meta or {}))

    earlier = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    rows = {
        "p1": {"extraction_method": "mineru", "extraction_source": "file", "quality_gate_overridden": True,
               "frecency_score": 0.4, "source_author": "Ann Lee", "indexed_at": earlier},
        "p2": {"extraction_method": "row-method", "source_author": "Row Author", "indexed_at": earlier},
        "shared": {"extraction_method": "pymupdf", "indexed_at": earlier},
        "p5": {"extraction_method": "pymupdf", "quality_gate_overridden": False, "indexed_at": earlier},
        "d1": {"extraction_method": "mineru", "indexed_at": earlier},
        "d2": {"extraction_method": "pymupdf", "indexed_at": earlier},
        # nexus-6pfgb.28: batch-path --enrich rows carry year/authors/venue/citation_count and no backend id,
        # and name their document by its file hash
        "p8": {"bib_year": 2019, "bib_authors": "Dana", "bib_venue": "OSDI", "bib_citation_count": 31,
               "content_hash": "hash-p8", "indexed_at": earlier},
        "p9": {"bib_year": 2020, "bib_authors": "Eli", "bib_venue": "Row Venue", "bib_citation_count": 5,
               "content_hash": "hash-p9", "indexed_at": earlier},
        "late": {"extraction_method": "mineru", "indexed_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
    }
    ch = {k: _chash(f"6pfgb10-{k}") for k in rows}
    seed_chunks_direct(DOCS, ids=list(ch.values()), documents=[f"6pfgb10 {k} text" for k in rows], embed=True,
                       metadatas=[{**rows[k], "chunk_text_hash": ch[k]} for k in rows])
    with HttpCatalogClient() as cat:
        owner = cat.register_owner("6pfgb10-owner", "repo", repo_hash="6pfgb10-hash")
        p = {
            "p1": paper(cat, owner, "P1"),
            "p2": paper(cat, owner, "P2", author="Curated Author", meta={"extraction_method": "curated-method"}),
            "p3": paper(cat, owner, "P3"),
            "p4": paper(cat, owner, "P4"),
            "p5": paper(cat, owner, "P5"),
            "p6": paper(cat, owner, "P6"),
            "p7": paper(cat, owner, "P7"),
            "p8": paper(cat, owner, "P8", meta={"content_hash": "hash-p8"}),
            "p9": paper(cat, owner, "P9", meta={"content_hash": "hash-p9"}),
        }
        held = {"p1": ["p1"], "p2": ["p2"], "p3": ["shared"], "p4": ["shared"], "p5": ["p5"], "p6": ["d1", "d2"], "p7": ["late"], "p8": ["p8"], "p9": ["p9"]}
        for doc, chunks in held.items():
            cat.write_manifest(p[doc], [{"chash": ch[c], "position": i} for i, c in enumerate(chunks)], collection=DOCS)

    def meta_of(tumbler) -> dict:
        return dict(reader.resolve(str(tumbler)).meta or {})

    def note(title):
        return reader.by_source_uri(uri_for(notes, title))

    writer.update(p["p9"], bib_venue="Curated Venue")

    # ── the dry run counts and writes nothing ──
    before = {t: meta_of(note(t).tumbler) for t in ("A", "B1", "B2", "C", "D")}
    dry = _run()
    assert dry["applied"] is False and dry["documents_examined"] >= 10
    assert {t: meta_of(note(t).tumbler) for t in before} == before
    assert dry["keys"]["note.ttl_days"]["stamped"] == 3 and dry["keys"]["note.put_at"]["skipped_shared"] == 1
    assert dry["keys"]["doc.extraction_method"]["skipped_shared"] == 2
    # nexus-6pfgb.23: source_format is derived from the entry, so every paper counts, shared chunks or not
    assert dry["keys"]["doc.source_format"]["stamped"] == 9 and dry["keys"]["doc.source_format"]["residue"] == 0

    # ── apply ──
    applied = _run("--apply")
    assert applied["writes_failed"] == 0 and applied["documents_written"] == applied["documents_to_stamp"]

    a = meta_of(note("A").tumbler)
    assert (a["ttl_days"], a["tags"], a["category"], a["source_agent"]) == (7, ["x", "y"], "design", "agent-a")
    assert a["put_at"], "the note's own clock is its row's indexed_at"
    assert meta_of(note("B2").tumbler)["tags"] == ["b2"] and meta_of(note("B2").tumbler)["ttl_days"] == 3
    assert meta_of(note("B1").tumbler) == before["B1"], "B1's only chunk is shared and the row is B2's: skipped"
    cm = meta_of(note("C").tumbler)
    assert cm["category"] == "curated" and "tags" not in cm and cm["put_at"], "a curated key survives; put_at is filled"
    dm = meta_of(note("D").tumbler)
    assert (dm["tags"], dm["category"], dm["ttl_days"]) == (["legacy"], "finding", 0) and dm["put_at"]

    p1 = meta_of(p["p1"])
    assert (p1["extraction_method"], p1["extraction_source"], p1["quality_gate_overridden"]) == ("mineru", "file", True)
    assert p1["frecency_score"] == 0.4 and p1["frecency_computed_at"] == earlier
    assert "tags" not in p1 and "category" not in p1, "the verb does not backfill an indexed document's tags and category"
    assert {meta_of(t)["source_format"] for t in p.values()} == {"pdf"}, "derived from (paper, *.pdf), shared chunks included"
    assert reader.resolve(p["p1"]).author == "Ann Lee"
    p2 = meta_of(p["p2"])
    assert p2["extraction_method"] == "curated-method" and reader.resolve(p["p2"]).author == "Curated Author"
    for shared in ("p3", "p4"):
        assert "extraction_method" not in meta_of(p[shared]), "a chunk two documents hold decides nothing"
    assert "quality_gate_overridden" not in meta_of(p["p5"]), "a False row never writes False"
    assert meta_of(p["p5"])["extraction_method"] == "pymupdf"
    assert "extraction_method" not in meta_of(p["p6"]), "single-owner rows that disagree are residue, not a guess"
    assert "extraction_method" not in meta_of(p["p7"]), "a row stamped after the document is another writer's"

    # ── nexus-6pfgb.28: id-less bib rows fill the empty bib columns, fill-only, with no id ──
    bib_run = CliRunner().invoke(enrich, ["bib", DOCS, "--backfill-catalog"])
    assert bib_run.exit_code == 0 and "Backfilled 2 documents" in bib_run.output, bib_run.output
    p8 = reader.resolve(p["p8"])
    assert (p8.bib_year, p8.bib_authors, p8.bib_venue, p8.bib_citation_count) == (2019, "Dana", "OSDI", 31)
    assert p8.bib_enriched_at and p8.bib_semantic_scholar_id == "" and p8.bib_openalex_id == ""
    p9 = reader.resolve(p["p9"])
    assert (p9.bib_year, p9.bib_venue, p9.bib_citation_count) == (2020, "Curated Venue", 5), "a held column survives"
    again = CliRunner().invoke(enrich, ["bib", DOCS, "--backfill-catalog"])
    assert "Backfilled 0 documents" in again.output, again.output

    # ── a second run stamps nothing ──
    second = _run("--apply")
    assert sum(k["stamped"] for k in second["keys"].values()) == 0
    assert second["documents_to_stamp"] == 0 and second["documents_written"] == 0
    assert c.status == STORED
