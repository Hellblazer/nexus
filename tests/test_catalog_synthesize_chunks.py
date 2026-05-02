# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the RDR-101 Phase 2 PR β chunk synthesis path.

Coverage:
- ``synthesize_t3_chunks(client, document_events)`` resolves doc_ids
  via source_path → file://source_path → source_uri map.
- Title fallback resolves chunks for empty-source_uri documents.
- Unmatched chunks emit ``ChunkIndexed`` with ``doc_id=""`` and
  ``synthesized_orphan=True``.
- Pagination works for >300-chunk collections.
- ``synthesize-log --chunks`` integrates document + chunk synthesis.
- ``synthesize-log --chunks`` reports orphan count in the JSON payload.

Tests use ``chromadb.EphemeralClient`` + ``DefaultEmbeddingFunction`` so
no Cloud credentials are needed (matches the pattern in
``tests/test_t3_prune_stale.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from click.testing import CliRunner

from nexus.catalog import events as ev
from nexus.catalog.catalog import Catalog
from nexus.catalog.event_log import EventLog
from nexus.catalog.synthesizer import synthesize_t3_chunks
from nexus.commands.catalog import synthesize_log_cmd


@pytest.fixture(autouse=True)
def _legacy_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR ζ (nexus-o6aa.9.5) flipped NEXUS_EVENT_SOURCED default to ON.
    The synthesize-log --chunks tests assume the source catalog only
    has legacy JSONL state, otherwise events.jsonl is pre-populated
    and the verb refuses to overwrite without --force. Pin to legacy.
    """
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")


@pytest.fixture()
def isolated_nexus(tmp_path: Path) -> Path:
    return tmp_path / "test-catalog"


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def chroma_client():
    """EphemeralClient gives an in-process ChromaDB with no Cloud creds.

    EphemeralClient is process-singleton-ish — collections from earlier
    tests can leak into the next. Drop everything at fixture build so
    the test sees a truly empty client (matches the pattern in
    ``tests/test_t3_prune_stale.py``).
    """
    client = chromadb.EphemeralClient()
    for col in list(client.list_collections()):
        try:
            client.delete_collection(col.name)
        except Exception:
            pass
    return client


def _seed_collection(
    client, name: str, chunks: list[dict],
) -> None:
    """Seed ``name`` with the given chunks. Each dict carries
    ``id``, ``content``, and a ``metadata`` dict."""
    col = client.get_or_create_collection(
        name=name, embedding_function=DefaultEmbeddingFunction(),
    )
    col.add(
        ids=[c["id"] for c in chunks],
        documents=[c["content"] for c in chunks],
        metadatas=[c["metadata"] for c in chunks],
    )


def _make_doc_events(*specs: dict) -> list[ev.Event]:
    """Build DocumentRegistered events from spec dicts; convenience for
    test setup so we don't have to drive a real Catalog."""
    out: list[ev.Event] = []
    for s in specs:
        out.append(ev.Event(
            type=ev.TYPE_DOCUMENT_REGISTERED, v=0,
            payload=ev.DocumentRegisteredPayload(
                doc_id=s["doc_id"],
                owner_id=s.get("owner_id", "1.1"),
                content_type=s.get("content_type", "code"),
                source_uri=s.get("source_uri", ""),
                coll_id=s.get("coll_id", ""),
                title=s.get("title", ""),
                tumbler=s.get("tumbler", ""),
            ),
            ts="2026-04-30T00:00:00Z",
        ))
    return out


# ── Direct synthesizer tests ─────────────────────────────────────────────


class TestChunkSynthesisHappyPath:
    def test_emits_one_event_per_chunk(self, chroma_client):
        _seed_collection(chroma_client, "code__test", [
            {
                "id": "ch1", "content": "hello",
                "metadata": {
                    "source_path": "/git/x/foo.py",
                    "chunk_text_hash": "abc1",
                    "content_hash": "h1",
                    "chunk_index": 0,
                },
            },
            {
                "id": "ch2", "content": "world",
                "metadata": {
                    "source_path": "/git/x/foo.py",
                    "chunk_text_hash": "abc2",
                    "content_hash": "h1",
                    "chunk_index": 1,
                },
            },
        ])
        doc_events = _make_doc_events({
            "doc_id": "uuid7-foo", "owner_id": "1.1",
            "source_uri": "file:///git/x/foo.py",
            "coll_id": "code__test",
            "tumbler": "1.1.1",
            "title": "foo.py",
        })

        events = list(synthesize_t3_chunks(chroma_client, doc_events))
        assert len(events) == 2
        for e in events:
            assert e.type == ev.TYPE_CHUNK_INDEXED
            assert e.v == 0
            assert e.payload.doc_id == "uuid7-foo"
            assert e.payload.coll_id == "code__test"
            assert e.payload.synthesized_orphan is False
        # Position is preserved from chunk_index.
        positions = sorted(e.payload.position for e in events)
        assert positions == [0, 1]
        # chash propagates.
        assert {e.payload.chash for e in events} == {"abc1", "abc2"}

    def test_resolves_via_title_fallback(self, chroma_client):
        # Document has empty source_uri; chunk has source_path that
        # doesn't match, but title matches.
        _seed_collection(chroma_client, "knowledge__papers", [
            {
                "id": "p1", "content": "abstract",
                "metadata": {
                    "source_path": "/legacy/path/missing",
                    "title": "Some Paper",
                    "chunk_text_hash": "p1hash",
                    "chunk_index": 0,
                },
            },
        ])
        doc_events = _make_doc_events({
            "doc_id": "uuid7-paper", "owner_id": "1.2",
            "content_type": "paper",
            "source_uri": "",
            "coll_id": "knowledge__papers",
            "tumbler": "1.2.42",
            "title": "Some Paper",
        })

        events = list(synthesize_t3_chunks(chroma_client, doc_events))
        assert len(events) == 1
        e = events[0]
        assert e.payload.doc_id == "uuid7-paper"
        assert e.payload.synthesized_orphan is False


class TestChunkSynthesisOrphans:
    def test_unmatched_chunk_is_orphan(self, chroma_client):
        _seed_collection(chroma_client, "code__abandoned", [
            {
                "id": "orphan", "content": "stale",
                "metadata": {
                    "source_path": "/git/deleted/gone.py",
                    "title": "gone.py",
                    "chunk_text_hash": "orphanhash",
                    "chunk_index": 0,
                },
            },
        ])
        # No DocumentRegistered events — every chunk should orphan.
        events = list(synthesize_t3_chunks(chroma_client, []))
        assert len(events) == 1
        e = events[0]
        assert e.payload.doc_id == ""
        assert e.payload.synthesized_orphan is True
        assert e.payload.chash == "orphanhash"


class TestChunkSynthesisPagination:
    """Confirm the synthesizer handles >300 chunks per collection.

    EphemeralClient does not enforce the Cloud 300-row limit but the
    walker still has to call get() with limit/offset; if the loop exit
    condition is wrong we'd loop forever or miss chunks.
    """

    def test_walks_a_400_chunk_collection(self, chroma_client):
        chunks = [
            {
                "id": f"c{i:03d}", "content": f"chunk-{i}",
                "metadata": {
                    "source_path": "/git/big/file.py",
                    "chunk_text_hash": f"h{i:03d}",
                    "chunk_index": i,
                },
            }
            for i in range(400)
        ]
        _seed_collection(chroma_client, "code__big", chunks)
        doc_events = _make_doc_events({
            "doc_id": "uuid7-big",
            "source_uri": "file:///git/big/file.py",
            "coll_id": "code__big",
            "tumbler": "1.1.1",
            "title": "file.py",
        })

        events = list(synthesize_t3_chunks(chroma_client, doc_events))
        assert len(events) == 400
        ids = {e.payload.chunk_id for e in events}
        assert ids == {f"c{i:03d}" for i in range(400)}


# ── synthesize-log --chunks integration ──────────────────────────────────


class TestSynthesizeLogChunksFlag:
    def test_chunks_flag_produces_chunk_events(
        self, isolated_nexus, runner,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # Build a real catalog with one document.
        Catalog.init(isolated_nexus)
        cat = Catalog(isolated_nexus, isolated_nexus / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        cat.register(
            owner, "doc-A.md", content_type="prose",
            file_path="doc-A.md",
            physical_collection="docs__nexus-test",
        )
        cat._db.close()

        # Set up a chromadb instance with chunks for that document.
        # Reset the singleton-ish EphemeralClient so a previous test's
        # collections don't leak in.
        client = chromadb.EphemeralClient()
        for col in list(client.list_collections()):
            try:
                client.delete_collection(col.name)
            except Exception:
                pass
        # Owner repo_root is empty in this fixture; resolve_path
        # falls through, so source_path on the chunks just needs to
        # match what _normalize_source_uri produces. We use the
        # absolute path of doc-A.md inside the test working dir.
        # _normalize_source_uri turns bare relative paths into
        # ``file://<repo_root>/file_path`` when repo_root is set; here
        # it falls back to ``file://<cwd>/file_path`` which matches the
        # path the runner captures. Read the catalog row to learn the
        # actual source_uri it stored.
        cat = Catalog(isolated_nexus, isolated_nexus / ".catalog.db")
        entries = list(cat._db.execute("SELECT source_uri FROM documents").fetchall())
        cat._db.close()
        source_uri = entries[0][0]
        # Strip "file://" → source_path the chunk will carry.
        assert source_uri.startswith("file://")
        source_path = source_uri[len("file://"):]

        _seed_collection(client, "docs__nexus-test", [
            {
                "id": "ch1", "content": "doc-A content",
                "metadata": {
                    "source_path": source_path,
                    "chunk_text_hash": "ahash",
                    "chunk_index": 0,
                },
            },
        ])

        # Patch make_t3 so the verb finds our EphemeralClient.
        class _FakeT3:
            _client = client

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())

        from nexus.commands.catalog import synthesize_log_cmd
        result = runner.invoke(
            synthesize_log_cmd, ["--chunks", "--json"],
        )
        assert result.exit_code == 0, (
            f"verb failed: {result.output}\n{result.exception!r}"
        )
        payload = json.loads(result.output)
        assert payload["chunks_synthesized"] is True
        assert payload["events_by_type"].get("ChunkIndexed", 0) == 1
        assert payload["orphan_chunks"] == 0

        # Confirm events.jsonl on disk has the ChunkIndexed event.
        log = EventLog(isolated_nexus)
        events = list(log.replay())
        chunk_events = [e for e in events if e.type == ev.TYPE_CHUNK_INDEXED]
        assert len(chunk_events) == 1
        assert chunk_events[0].payload.chunk_id == "ch1"
        assert chunk_events[0].payload.synthesized_orphan is False
        # doc_id is the UUID7 the document synthesis minted.
        doc_events = [e for e in events if e.type == ev.TYPE_DOCUMENT_REGISTERED]
        assert chunk_events[0].payload.doc_id == doc_events[0].payload.doc_id

    def test_no_chunks_flag_skips_t3(
        self, isolated_nexus, runner,
        monkeypatch: pytest.MonkeyPatch,
    ):
        Catalog.init(isolated_nexus)
        cat = Catalog(isolated_nexus, isolated_nexus / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="cdcdcd")
        cat.register(owner, "doc-A.md", content_type="prose", file_path="doc-A.md")
        cat._db.close()

        # If the verb tried to access make_t3 we'd find out.
        def _boom():
            raise AssertionError("make_t3 must not be called when --chunks is off")

        monkeypatch.setattr("nexus.db.make_t3", _boom)

        from nexus.commands.catalog import synthesize_log_cmd
        result = runner.invoke(synthesize_log_cmd, ["--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["chunks_synthesized"] is False
        assert "ChunkIndexed" not in payload["events_by_type"]


# ── RDR-102 Phase A / D5: --prefer-live-catalog flag ─────────────────────


class TestPreferLiveCatalogDirect:
    """Direct synthesizer-level coverage for the live-catalog content_hash
    map. Pinned at the synthesize_t3_chunks API so the resolution
    semantics are testable without spinning up the full CLI verb.
    """

    def test_live_catalog_lookup_resolves_orphan_via_content_hash(
        self, chroma_client,
    ):
        """RDR-102 D5: when the synthesized DocumentRegistered events
        do not match a chunk via source_uri or title (the chunk's
        owning Document was registered AFTER synthesize-log already ran
        once), the live catalog lookup map must resolve the chunk's
        content_hash to the live Document tumbler. The chunk emits
        ChunkIndexed with doc_id=tumbler and synthesized_orphan=False.
        """
        # Chunk has no source_uri/title match in the synthesized events,
        # but its content_hash matches a live Document (tumbler 1.7.42).
        _seed_collection(chroma_client, "code__recovered", [
            {
                "id": "rec1", "content": "recovered chunk",
                "metadata": {
                    "source_path": "/git/renamed/old-path.py",
                    "title": "old-title",
                    "chunk_text_hash": "rec1hash",
                    "content_hash": "live-content-hash-abc",
                    "chunk_index": 0,
                },
            },
        ])
        # Live catalog has a Document for the SAME content_hash but
        # under a different source_uri / title (file moved + renamed
        # since the chunk was first indexed).
        live_doc_lookup = {"live-content-hash-abc": "1.7.42"}

        events = list(synthesize_t3_chunks(
            chroma_client, [],  # no synthesized doc_events
            live_doc_lookup=live_doc_lookup,
        ))
        assert len(events) == 1
        e = events[0]
        assert e.payload.doc_id == "1.7.42", (
            f"expected doc_id resolved from live catalog content_hash "
            f"map; got {e.payload.doc_id!r}. The --prefer-live-catalog "
            f"recovery path must consult content_hash before declaring "
            f"the chunk an orphan."
        )
        assert e.payload.synthesized_orphan is False, (
            "live-catalog match must clear the synthesized_orphan flag"
        )

    def test_back_compat_no_lookup_keeps_orphan(self, chroma_client):
        """RDR-102 D5 back-compat: omitting live_doc_lookup keeps the
        existing behaviour. Same chunk + same absent source_uri/title
        match → orphan ChunkIndexed event.
        """
        _seed_collection(chroma_client, "code__recovered_bc", [
            {
                "id": "bc1", "content": "no-recovery chunk",
                "metadata": {
                    "source_path": "/git/renamed/old-path.py",
                    "title": "old-title",
                    "chunk_text_hash": "bc1hash",
                    "content_hash": "live-content-hash-bc",
                    "chunk_index": 0,
                },
            },
        ])
        events = list(synthesize_t3_chunks(
            chroma_client, [],
            # No live_doc_lookup passed — pre-RDR-102 behaviour.
        ))
        assert len(events) == 1
        e = events[0]
        assert e.payload.doc_id == ""
        assert e.payload.synthesized_orphan is True

    def test_synthesized_match_takes_priority_over_live_lookup(
        self, chroma_client,
    ):
        """The live-catalog map is a FALLBACK for chunks that would
        otherwise orphan. When a synthesized DocumentRegistered already
        matches via source_uri, that doc_id wins — we must not silently
        rewrite the chunk's identity to a different live tumbler when
        the synthesized identity is the canonical one for this run.
        """
        _seed_collection(chroma_client, "code__priority", [
            {
                "id": "pri1", "content": "priority chunk",
                "metadata": {
                    "source_path": "/git/x/foo.py",
                    "chunk_text_hash": "pri1hash",
                    "content_hash": "live-h",
                    "chunk_index": 0,
                },
            },
        ])
        doc_events = _make_doc_events({
            "doc_id": "uuid7-foo", "owner_id": "1.1",
            "source_uri": "file:///git/x/foo.py",
            "coll_id": "code__priority",
            "tumbler": "1.1.1",
            "title": "foo.py",
        })
        # Live catalog lookup also has the same content_hash but maps
        # to a DIFFERENT tumbler — the synthesized doc_events match
        # must win for non-orphans.
        live_doc_lookup = {"live-h": "9.9.9"}

        events = list(synthesize_t3_chunks(
            chroma_client, doc_events,
            live_doc_lookup=live_doc_lookup,
        ))
        assert len(events) == 1
        e = events[0]
        assert e.payload.doc_id == "uuid7-foo", (
            f"synthesized source_uri match must take priority over "
            f"live_doc_lookup; got {e.payload.doc_id!r}. The lookup map "
            f"is a fallback for orphans, not an override."
        )


class TestPreferLiveCatalogCli:
    """CLI surface for the --prefer-live-catalog flag on
    synthesize-log. Exercises the integration: real Catalog setup +
    EphemeralClient T3 + Click runner end-to-end.
    """

    def test_prefer_live_catalog_flag_recovers_orphan_via_content_hash(
        self, isolated_nexus, runner,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """End-to-end: a Document is in the live catalog with
        meta={"content_hash": ...}; a T3 chunk carries that
        content_hash but its source_path does not match any catalog
        source_uri AND its title does not match any catalog title
        (e.g. the file was renamed and re-indexed under a different
        title before synthesize-log first ran, leaving the chunk an
        orphan via the source_uri/title paths). With
        --prefer-live-catalog set, the chunk's ChunkIndexed event
        resolves to the live Document's tumbler via content_hash and
        is NOT classified as an orphan.
        """
        Catalog.init(isolated_nexus)
        cat = Catalog(isolated_nexus, isolated_nexus / ".catalog.db")
        owner = cat.register_owner(
            "nexus", "repo", repo_hash="abcd1234",
        )
        # Document with content_hash in meta — mirrors how the repo
        # indexer stamps content_hash via _catalog_hook (indexer.py:335).
        # The Document's title is the CURRENT name; the chunk below
        # carries the OLD name so the title fallback misses and the
        # content_hash recovery actually fires.
        cat.register(
            owner, "current-name.py", content_type="code",
            file_path="current-name.py",
            physical_collection="code__nexus-recover",
            meta={"content_hash": "live-hash-xyz"},
        )
        cat._db.close()

        client = chromadb.EphemeralClient()
        for col in list(client.list_collections()):
            try:
                client.delete_collection(col.name)
            except Exception:
                pass
        # Chunk's source_path AND title differ from the catalog
        # Document (file renamed since first index) but content_hash
        # matches — exactly the orphan-recovery scenario the flag
        # exists to handle.
        _seed_collection(client, "code__nexus-recover", [
            {
                "id": "moved_0", "content": "recovered code",
                "metadata": {
                    "source_path": "/legacy/cwd/old-name.py",
                    "title": "old-name.py",
                    "chunk_text_hash": "moved_h",
                    "content_hash": "live-hash-xyz",
                    "chunk_index": 0,
                },
            },
        ])

        class _FakeT3:
            _client = client
        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())

        result = runner.invoke(
            synthesize_log_cmd,
            ["--chunks", "--prefer-live-catalog", "--json"],
        )
        assert result.exit_code == 0, (
            f"verb failed: {result.output}\n{result.exception!r}"
        )
        payload = json.loads(result.output)
        assert payload["events_by_type"].get("ChunkIndexed", 0) == 1
        assert payload["orphan_chunks"] == 0, (
            f"expected 0 orphans after --prefer-live-catalog recovery; "
            f"got {payload['orphan_chunks']}. The flag must consult the "
            f"live catalog content_hash map and resolve the moved-file "
            f"chunk to the live Document tumbler."
        )

        # Confirm the persisted ChunkIndexed event carries the live
        # Document's tumbler as doc_id.
        log_events = list(EventLog(isolated_nexus).replay())
        chunk_events = [
            e for e in log_events if e.type == ev.TYPE_CHUNK_INDEXED
        ]
        assert len(chunk_events) == 1
        assert chunk_events[0].payload.doc_id == "1.1.1", (
            f"chunk must carry the live Document tumbler (1.1.1); got "
            f"{chunk_events[0].payload.doc_id!r}"
        )
        assert chunk_events[0].payload.synthesized_orphan is False

    def test_no_flag_keeps_orphan_back_compat(
        self, isolated_nexus, runner,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Same setup as the recovery test, but without
        --prefer-live-catalog. The chunk MUST orphan (back-compat: the
        flag is opt-in)."""
        Catalog.init(isolated_nexus)
        cat = Catalog(isolated_nexus, isolated_nexus / ".catalog.db")
        owner = cat.register_owner(
            "nexus", "repo", repo_hash="bcde2345",
        )
        cat.register(
            owner, "moved.py", content_type="code",
            file_path="moved.py",
            physical_collection="code__nexus-bc",
            meta={"content_hash": "bc-hash"},
        )
        cat._db.close()

        client = chromadb.EphemeralClient()
        for col in list(client.list_collections()):
            try:
                client.delete_collection(col.name)
            except Exception:
                pass
        _seed_collection(client, "code__nexus-bc", [
            {
                "id": "bc_0", "content": "bc chunk",
                "metadata": {
                    "source_path": "/legacy/different/moved.py",
                    "title": "moved.py-renamed",
                    "chunk_text_hash": "bc_h",
                    "content_hash": "bc-hash",
                    "chunk_index": 0,
                },
            },
        ])

        class _FakeT3:
            _client = client
        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())

        result = runner.invoke(
            synthesize_log_cmd, ["--chunks", "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["events_by_type"].get("ChunkIndexed", 0) == 1
        assert payload["orphan_chunks"] == 1, (
            f"without --prefer-live-catalog the chunk must orphan "
            f"(content_hash recovery is opt-in); got {payload['orphan_chunks']}"
        )
