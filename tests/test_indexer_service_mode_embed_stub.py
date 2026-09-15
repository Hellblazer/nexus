# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-fsquc: service-mode embed stub for prose_indexer and code_indexer.

doc_indexer carries the RDR-152 Seam-B stub (service mode is checked FIRST;
"no Python embed" — the JVM embeds server-side and the client's embeddings
argument is discarded). prose_indexer and code_indexer lacked the gate: in
service mode with a Voyage key present they client-embedded every chunk via
the Voyage API, then HttpVectorClient discarded the vectors and the service
embedded AGAIN — double Voyage spend per chunk since RDR-155 P4a (proven by
voyageai contextualized_embed tracebacks in index.log during the 2026-06-11
re-index runs).

These tests pin the stub: in service mode, NO client-side embedding call is
made by either indexer, and the upsert receives placeholder embeddings that
the http client ignores.

nexus-sghyo (2026-08-06): the legacy client-side embedder these tests
originally proved was SKIPPED in service mode (``_embed_with_fallback``,
``ctx.voyage_client.embed``) is now DELETED outright — client-side Voyage
embedding is retired (Hal determination 2026-07-28: "we do no embedding on
the client"). The double-spend class this file guards against is
structurally impossible now, not merely mode-gated; these tests still pin
the substantive placeholder-embedding / force_re_embed behavior.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus.index_context import IndexContext


class _RecordingDb:
    def __init__(self) -> None:
        self.upserts: list[dict] = []

    def upsert_chunks_with_embeddings(
        self, collection_name, ids, documents, embeddings, metadatas,
        *, force_re_embed: bool = False,
    ) -> None:
        self.upserts.append({
            "collection_name": collection_name,
            "ids": ids,
            "documents": documents,
            "embeddings": embeddings,
            "metadatas": metadatas,
            "force_re_embed": force_re_embed,
        })


class _ForbiddenVoyageClient:
    def embed(self, *a, **k):
        raise AssertionError(
            "client-side Voyage embed called in service mode — double spend"
        )


def _make_col() -> MagicMock:
    # force=False exercises the real staleness check (check_staleness ->
    # col.get roundtrip when no staleness_cache is supplied) — empty
    # metadatas means "not stale", so the file still indexes.
    col = MagicMock()
    col.get.return_value = {"metadatas": [], "ids": []}
    return col


def _make_ctx(
    tmp_path: Path,
    db: _RecordingDb,
    corpus: str,
    model: str,
    *,
    force: bool = True,
    force_re_embed: bool = False,
) -> IndexContext:
    return IndexContext(
        col=_make_col(),
        db=db,
        voyage_key="key-present-but-must-not-be-used",
        voyage_client=_ForbiddenVoyageClient(),
        repo_path=tmp_path,
        corpus=corpus,
        embedding_model=model,
        git_meta={},
        now_iso="2026-06-11T00:00:00+00:00",
        force=force,  # bypass staleness — no col roundtrip
        force_re_embed=force_re_embed,
    )


@pytest.fixture(autouse=True)
def _service_mode(monkeypatch):
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True
    )
    # nexus-sghyo (2026-08-06): the legacy client-side embedder
    # (``nexus.doc_indexer._embed_with_fallback``) is DELETED outright —
    # client-side Voyage embedding is retired (Hal determination
    # 2026-07-28: "we do no embedding on the client"). The double-spend
    # this file guards against is now structurally impossible rather
    # than merely skipped: there is no client-side embed call left to
    # make in service mode, so there is nothing left to patch here.


def test_prose_indexer_service_mode_skips_client_embed(tmp_path, monkeypatch):
    from nexus.prose_indexer import index_prose_file

    f = tmp_path / "note.md"
    f.write_text("# Title\n\nSome prose content long enough to chunk.\n")
    db = _RecordingDb()
    # nexus-4jj40: force_re_embed is decoupled from force, so pass it
    # explicitly to exercise the ctx.force_re_embed -> upsert forwarding.
    ctx = _make_ctx(
        tmp_path, db, "rdr__t__minilm-l6-v2-384__v1", "minilm-l6-v2-384",
        force_re_embed=True,
    )

    count = index_prose_file(ctx, f)

    assert count >= 1
    assert len(db.upserts) == 1
    up = db.upserts[0]
    assert len(up["embeddings"]) == len(up["ids"])
    assert all(e == [] for e in up["embeddings"]), (
        "service mode passes placeholder embeddings the http client ignores"
    )
    # Model identity preserved in metadata (no fallback-model rewrite).
    assert all(
        m.get("embedding_model") == "minilm-l6-v2-384" for m in up["metadatas"]
    )
    # RDR-181 §Approach step 3 / nexus-4jj40: --force --re-embed
    # (ctx.force_re_embed=True) must reach force_re_embed=True on the upsert.
    assert up["force_re_embed"] is True


def test_prose_indexer_force_alone_does_not_force_re_embed(tmp_path, monkeypatch):
    """nexus-4jj40 sibling (shakedown 2026-09-15): the prose per-file fallback
    passed ctx.force as force_re_embed, so a plain --force paid for a full
    re-embed on this path."""
    from nexus.prose_indexer import index_prose_file

    f = tmp_path / "note3.md"
    f.write_text("# Title\n\nProse content that a plain --force re-sends.\n")
    db = _RecordingDb()
    ctx = _make_ctx(
        tmp_path, db, "rdr__t__minilm-l6-v2-384__v1", "minilm-l6-v2-384",
        force=True, force_re_embed=False,
    )

    count = index_prose_file(ctx, f)

    assert count >= 1
    assert len(db.upserts) == 1
    assert db.upserts[0]["force_re_embed"] is False


def test_index_prose_file_wrapper_threads_force_re_embed(tmp_path, monkeypatch):
    """Review finding on the shakedown fix (T2 nexus/nexus-4jj40-prose-rdr-
    force-re-embed-wrapper-gap): indexer._index_prose_file builds the prose
    IndexContext for nx index repo's docs and rdr loops. Without a
    force_re_embed parameter there, --re-embed never reached the per-file
    fallback, whatever prose_indexer did with the context."""
    from nexus.indexer import _index_prose_file

    flags = []
    for force_re_embed in (False, True):
        f = tmp_path / f"wrap-{force_re_embed}.md"
        f.write_text("# Title\n\nWrapper-level prose content for the fallback path.\n")
        db = _RecordingDb()
        _index_prose_file(
            f, tmp_path, "rdr__t__minilm-l6-v2-384__v1", "minilm-l6-v2-384",
            _make_col(), db, "key-present-but-must-not-be-used", {},
            "2026-06-11T00:00:00+00:00", 0.0,
            force=True, force_re_embed=force_re_embed,
        )
        flags.append([u["force_re_embed"] for u in db.upserts])
    assert flags == [[False], [True]]


def test_prose_indexer_force_false_does_not_set_force_re_embed(tmp_path, monkeypatch):
    from nexus.prose_indexer import index_prose_file

    f = tmp_path / "note2.md"
    f.write_text("# Title\n\nSome more prose content long enough to chunk.\n")
    db = _RecordingDb()
    ctx = _make_ctx(
        tmp_path, db, "rdr__t__minilm-l6-v2-384__v1", "minilm-l6-v2-384", force=False,
    )

    count = index_prose_file(ctx, f)

    assert count >= 1
    assert len(db.upserts) == 1
    assert db.upserts[0]["force_re_embed"] is False


def test_code_indexer_service_mode_skips_client_embed(tmp_path, monkeypatch):
    from nexus.code_indexer import index_code_file

    f = tmp_path / "mod.py"
    f.write_text(
        "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n"
    )
    db = _RecordingDb()
    # nexus-4jj40 round 5: force_re_embed is decoupled from force (a plain
    # --force reclassify pass no longer pays for a re-embed) — pass it
    # explicitly to exercise the ctx.force_re_embed -> upsert forwarding
    # code_indexer.py actually performs.
    ctx = _make_ctx(
        tmp_path, db, "code__t__minilm-l6-v2-384__v1", "minilm-l6-v2-384",
        force_re_embed=True,
    )

    count = index_code_file(ctx, f)

    assert count >= 1
    assert len(db.upserts) == 1
    up = db.upserts[0]
    assert len(up["embeddings"]) == len(up["ids"])
    assert all(e == [] for e in up["embeddings"])
    # RDR-181 §Approach step 3 / nexus-4jj40 round 5: --force --re-embed
    # (ctx.force_re_embed=True) must reach force_re_embed=True on the
    # upsert call.
    assert up["force_re_embed"] is True


def test_code_indexer_force_false_does_not_set_force_re_embed(tmp_path, monkeypatch):
    from nexus.code_indexer import index_code_file

    f = tmp_path / "mod2.py"
    f.write_text(
        "def gamma():\n    return 3\n\n\ndef delta():\n    return 4\n"
    )
    db = _RecordingDb()
    ctx = _make_ctx(
        tmp_path, db, "code__t__minilm-l6-v2-384__v1", "minilm-l6-v2-384", force=False,
    )

    count = index_code_file(ctx, f)

    assert count >= 1
    assert len(db.upserts) == 1
    assert db.upserts[0]["force_re_embed"] is False
