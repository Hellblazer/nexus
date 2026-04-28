# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import chromadb
import pytest

from nexus.catalog.catalog import Catalog, _SPAN_PATTERN
from nexus.catalog.tumbler import Tumbler


@pytest.fixture
def cat(tmp_path):
    d = tmp_path / "catalog"
    d.mkdir()
    return Catalog(d, d / ".catalog.db")


@pytest.fixture
def cat_with_owner(cat):
    owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
    return cat, owner


@pytest.fixture
def cat_with_two_docs(cat_with_owner):
    cat, owner = cat_with_owner
    a = cat.register(owner, "a.py", content_type="code", file_path="a.py")
    b = cat.register(owner, "b.py", content_type="code", file_path="b.py")
    return cat, owner, a, b


@pytest.fixture
def span_env(tmp_path):
    d = tmp_path / "catalog"
    d.mkdir()
    cat = Catalog(d, d / ".catalog.db")
    t3 = chromadb.EphemeralClient()
    col_name = f"code__span_{tmp_path.name}"
    col = t3.create_collection(col_name)
    return cat, t3, col_name, col


class TestRegisterOwner:
    def test_first_owner(self, cat):
        assert str(cat.register_owner("nexus", "repo", repo_hash="571b8edd")) == "1.1"

    def test_second_owner(self, cat):
        cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        assert str(cat.register_owner("arcaneum", "repo", repo_hash="aabb1122")) == "1.2"

    def test_owner_for_repo_lookup(self, cat_with_owner):
        cat, _ = cat_with_owner
        assert str(cat.owner_for_repo("571b8edd")) == "1.1"

    def test_owner_for_repo_not_found(self, cat):
        assert cat.owner_for_repo("nonexistent") is None

    def test_curator_owner(self, cat):
        assert str(cat.register_owner("hal-research", "curator")) == "1.1"

    def test_repo_owner_without_repo_hash_rejected(self, cat):
        """nexus-zbne: owner_type='repo' without repo_hash is the shadow-
        registration pathway that produced 83 orphan owners. Reject it."""
        with pytest.raises(ValueError, match="repo_hash"):
            cat.register_owner("nexus", "repo")

    def test_repo_owner_with_whitespace_repo_hash_rejected(self, cat):
        """Empty after strip() — also rejected, since such owners are
        indistinguishable from the missing-hash case at lookup time."""
        with pytest.raises(ValueError, match="repo_hash"):
            cat.register_owner("nexus", "repo", repo_hash="   ")

    def test_curator_owner_without_repo_hash_allowed(self, cat):
        """Curator and custom types don't carry repo_hash — no enforcement."""
        # Covered by test_curator_owner, but make the asymmetry explicit so
        # future refactors don't accidentally enforce repo_hash globally.
        assert str(cat.register_owner("papers", "curator")) == "1.1"
        assert str(cat.register_owner("notes", "corpus")) == "1.2"


class TestRegisterDocument:
    def test_first_document(self, cat_with_owner):
        cat, owner = cat_with_owner
        doc = cat.register(owner, "indexer.py", content_type="code",
                           file_path="src/nexus/indexer.py", physical_collection="code__nexus", chunk_count=10)
        assert str(doc) == "1.1.1"

    def test_auto_increment(self, cat_with_owner):
        cat, owner = cat_with_owner
        cat.register(owner, "a.py", content_type="code", file_path="a.py")
        assert str(cat.register(owner, "b.py", content_type="code", file_path="b.py")) == "1.1.2"

    def test_resolve(self, cat_with_owner):
        cat, owner = cat_with_owner
        doc = cat.register(owner, "indexer.py", content_type="code",
                           file_path="src/nexus/indexer.py", physical_collection="code__nexus", chunk_count=10)
        entry = cat.resolve(doc)
        assert entry is not None and entry.title == "indexer.py" and entry.content_type == "code"


class TestSourceUriRegistration:
    """RDR-096 P3.1: source_uri at the catalog register boundary.

    Three contracts:
    * Bare ``file_path`` auto-derives ``file://<abspath>`` source_uri.
    * Explicit ``source_uri`` with a recognized scheme stores
      verbatim.
    * Malformed URI (no scheme; unrecognized scheme) raises
      ``ValueError`` at register time — NOT silently persisted.
    """

    def test_register_with_file_path_derives_file_uri(
        self, cat_with_owner,
    ):
        import os.path
        cat, owner = cat_with_owner
        doc = cat.register(
            owner, "indexer.py", content_type="code",
            file_path="src/nexus/indexer.py",
        )
        entry = cat.resolve(doc)
        assert entry is not None
        # Auto-derived: file://<abspath>.
        assert entry.source_uri == "file://" + os.path.abspath("src/nexus/indexer.py")

    def test_register_with_explicit_chroma_uri(self, cat_with_owner):
        cat, owner = cat_with_owner
        explicit = "chroma://knowledge__delos//papers/aleph.pdf"
        doc = cat.register(
            owner, "aleph", content_type="paper",
            file_path="",  # no path; URI is canonical
            source_uri=explicit,
        )
        entry = cat.resolve(doc)
        assert entry is not None
        # Stored verbatim.
        assert entry.source_uri == explicit

    def test_register_with_explicit_uri_overrides_file_path_derivation(
        self, cat_with_owner,
    ):
        """When source_uri is provided, it wins over the file_path
        auto-derivation."""
        cat, owner = cat_with_owner
        explicit = "https://docs.bito.ai/ingest-overview"
        doc = cat.register(
            owner, "bito-ingest", content_type="paper",
            file_path="bito-mirror.md",
            source_uri=explicit,
        )
        entry = cat.resolve(doc)
        assert entry is not None
        assert entry.source_uri == explicit
        # file_path is preserved separately.
        assert entry.file_path == "bito-mirror.md"

    def test_register_accepts_devonthink_uri(self, cat_with_owner):
        """``x-devonthink-item://<UUID>`` is a first-class catalog URI
        scheme (nexus-bqda). Registering with one stores it verbatim;
        the macOS-only osascript bridge runs at extraction time, not
        at register time, so this works on any platform.
        """
        cat, owner = cat_with_owner
        explicit = "x-devonthink-item://8EDC855D-213F-40AD-A9CF-9543CC76476B"
        doc = cat.register(
            owner, "graph-rag", content_type="paper",
            file_path="",
            source_uri=explicit,
        )
        entry = cat.resolve(doc)
        assert entry is not None
        assert entry.source_uri == explicit

    def test_register_rejects_malformed_uri_no_scheme(self, cat_with_owner):
        cat, owner = cat_with_owner
        with pytest.raises(ValueError, match="no scheme"):
            cat.register(
                owner, "broken", content_type="paper",
                source_uri="not-a-uri-just-a-string",
            )

    def test_register_rejects_unknown_scheme(self, cat_with_owner):
        cat, owner = cat_with_owner
        # Future scheme not yet in _KNOWN_URI_SCHEMES (e.g. s3://, ftp://).
        # Adding it requires registering a reader first.
        with pytest.raises(ValueError, match="unknown source_uri scheme"):
            cat.register(
                owner, "premature", content_type="paper",
                source_uri="s3://bucket/key.pdf",
            )

    def test_register_with_no_path_or_uri_stores_empty_uri(
        self, cat_with_owner,
    ):
        """Legacy entries with no identity (synthesized records, ghost
        registrations) get source_uri='' rather than failing.
        """
        cat, owner = cat_with_owner
        doc = cat.register(owner, "ghost", content_type="paper")
        entry = cat.resolve(doc)
        assert entry is not None
        assert entry.source_uri == ""

    def test_known_uri_schemes_table_is_locked_to_planned_set(self):
        """Lock the scheme registry against silent additions OR
        shrinking. Phase 1: ``file`` + ``chroma``. Phase 4:
        ``nx-scratch`` (P4.1) + ``https`` (P4.2). nexus-bqda adds
        ``x-devonthink-item`` (macOS-only DT identity URLs). Plain
        ``http`` is intentionally excluded — Phase 4's https reader
        does NOT cover plain http, so accepting http URIs at register
        would succeed silently and fail at extraction. Adding a new
        scheme requires landing the reader first AND updating this
        lock.
        """
        from nexus.catalog.catalog import _KNOWN_URI_SCHEMES
        assert _KNOWN_URI_SCHEMES == frozenset({
            "file", "chroma", "https", "nx-scratch", "x-devonthink-item",
        })

    def test_register_rejects_http_scheme_until_reader_lands(
        self, cat_with_owner,
    ):
        """``http://`` is not yet in the allowlist (P4.2 only ships
        https). A user who pastes an http URL gets a clear error
        rather than silent register-success-but-extract-failure.
        """
        cat, owner = cat_with_owner
        with pytest.raises(ValueError, match="unknown source_uri scheme"):
            cat.register(
                owner, "http-only", content_type="paper",
                source_uri="http://example.com/paper.pdf",
            )

    def test_update_preserves_source_uri(self, cat_with_owner):
        """RDR-096 P3.1 regression guard: ``update()`` must carry
        ``source_uri`` through unchanged when the caller does not
        pass it. Without the carry-over, every update silently
        clobbered the URI persisted at register time. Caught in code
        review of P3.1.
        """
        cat, owner = cat_with_owner
        explicit = "chroma://knowledge__delos//papers/aleph.pdf"
        doc = cat.register(
            owner, "aleph", content_type="paper",
            source_uri=explicit,
        )
        # Update an unrelated field — title.
        cat.update(doc, title="Aleph BFT (revised)")
        entry = cat.resolve(doc)
        assert entry is not None
        assert entry.title == "Aleph BFT (revised)"
        # source_uri preserved.
        assert entry.source_uri == explicit

    def test_update_with_explicit_source_uri_validates_at_boundary(
        self, cat_with_owner,
    ):
        """When ``update()`` is called with ``source_uri=...``, the
        new URI is validated through the same boundary as register;
        malformed URIs raise rather than silently persist.
        """
        cat, owner = cat_with_owner
        doc = cat.register(
            owner, "valid", content_type="paper",
            source_uri="chroma://knowledge__delos/x",
        )
        with pytest.raises(ValueError, match="no scheme"):
            cat.update(doc, source_uri="not-a-uri")

    def test_update_with_explicit_source_uri_replaces_value(
        self, cat_with_owner,
    ):
        """When the caller passes a valid new ``source_uri``, it
        replaces the previous value.
        """
        cat, owner = cat_with_owner
        doc = cat.register(
            owner, "doc", content_type="paper",
            source_uri="chroma://knowledge__delos/old",
        )
        new_uri = "chroma://knowledge__delos/new"
        cat.update(doc, source_uri=new_uri)
        entry = cat.resolve(doc)
        assert entry is not None
        assert entry.source_uri == new_uri

    def test_by_file_path_returns_source_uri(self, cat_with_owner):
        """The ``by_file_path`` lookup site reads source_uri (covers
        the gap caught in code review where 7 SELECT sites returned
        empty source_uri even when persisted).
        """
        import os.path
        cat, owner = cat_with_owner
        cat.register(
            owner, "indexed", content_type="code",
            file_path="src/x.py",
        )
        entry = cat.by_file_path(owner, "src/x.py")
        assert entry is not None
        assert entry.source_uri == "file://" + os.path.abspath("src/x.py")


class TestAliasResolution:
    """nexus-s8yz: documents.alias_of column — permanent tumbler aliasing.

    Preserves external reference stability when dedupe-owners (nexus-tmbh)
    consolidates duplicate owner registrations.
    """

    def test_new_document_has_empty_alias(self, cat_with_owner):
        cat, owner = cat_with_owner
        doc = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        entry = cat.resolve(doc, follow_alias=False)
        assert entry is not None and entry.alias_of == ""

    def test_set_alias_redirects_resolve(self, cat_with_owner):
        cat, owner = cat_with_owner
        canonical = cat.register(owner, "canonical.py", content_type="code", file_path="canonical.py")
        alias = cat.register(owner, "alias.py", content_type="code", file_path="alias.py")
        cat.set_alias(alias, canonical)

        # resolve() with default follow_alias=True returns the canonical entry
        entry = cat.resolve(alias)
        assert entry is not None and entry.tumbler == canonical

        # follow_alias=False returns the raw alias row
        raw = cat.resolve(alias, follow_alias=False)
        assert raw is not None and raw.tumbler == alias and raw.alias_of == str(canonical)

    def test_resolve_alias_canonical_returns_self(self, cat_with_owner):
        cat, owner = cat_with_owner
        doc = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        assert cat.resolve_alias(doc) == doc

    def test_resolve_alias_transitive_chain(self, cat_with_owner):
        """A → B → C → canonical. resolve_alias walks the whole chain."""
        cat, owner = cat_with_owner
        c = cat.register(owner, "c.py", content_type="code", file_path="c.py")
        b = cat.register(owner, "b.py", content_type="code", file_path="b.py")
        a = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        cat.set_alias(b, c)
        cat.set_alias(a, b)
        assert cat.resolve_alias(a) == c
        # resolve() with follow_alias=True also follows to terminus
        entry = cat.resolve(a)
        assert entry is not None and entry.tumbler == c

    def test_resolve_alias_cycle_does_not_hang(self, cat_with_owner):
        """Direct cycle A → B → A — walker bails rather than looping forever."""
        cat, owner = cat_with_owner
        a = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        b = cat.register(owner, "b.py", content_type="code", file_path="b.py")
        cat.set_alias(a, b)
        # Bypass set_alias guard to force a cycle — the walker must still
        # terminate instead of looping forever.
        cat._db.execute("UPDATE documents SET alias_of = ? WHERE tumbler = ?",
                         (str(a), str(b)))
        cat._db.commit()
        result = cat.resolve_alias(a)
        assert result in (a, b)

    def test_set_alias_rejects_self_alias(self, cat_with_owner):
        cat, owner = cat_with_owner
        doc = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        with pytest.raises(ValueError, match="self-alias"):
            cat.set_alias(doc, doc)

    def test_dangling_alias_terminates_safely(self, cat_with_owner):
        """If the alias pointer targets a deleted tumbler, the walker
        returns the last valid hop rather than returning None or
        raising. Callers that care can compare to the input."""
        cat, owner = cat_with_owner
        a = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        # Point at a non-existent tumbler directly via SQL (bypassing
        # set_alias' validation, which doesn't verify existence).
        cat._db.execute(
            "UPDATE documents SET alias_of = ? WHERE tumbler = ?",
            ("1.99.99", str(a)),
        )
        cat._db.commit()
        # Walker follows one hop to the dangling target, then stops.
        assert cat.resolve_alias(a) == Tumbler.parse("1.99.99")

    def test_schema_migration_adds_alias_of_to_old_db(self, tmp_path):
        """Older catalog databases without an alias_of column must be
        upgraded silently on open. Simulated by creating a documents
        table with the pre-migration schema and then re-opening via
        Catalog."""
        import sqlite3

        d = tmp_path / "oldcat"
        d.mkdir()
        db_path = d / ".catalog.db"
        # Pre-migration documents schema (no alias_of column)
        with sqlite3.connect(db_path) as conn:
            conn.executescript(
                "CREATE TABLE documents (tumbler TEXT PRIMARY KEY, title TEXT);"
                "INSERT INTO documents VALUES ('1.1.1', 'legacy.md');"
            )

        # Opening via Catalog must add the column (not raise).
        cat = Catalog(d, db_path)
        row = cat._db.execute(
            "SELECT alias_of FROM documents WHERE tumbler = ?", ("1.1.1",)
        ).fetchone()
        assert row is not None and row[0] == ""


class TestGhostElement:
    @pytest.mark.parametrize("title,ctype,kwargs,expected_chunks", [
        ("Future Paper", "paper", {"physical_collection": ""}, 0),
        ("Placeholder", "knowledge", {"chunk_count": 0}, 0),
    ])
    def test_ghost(self, cat, title, ctype, kwargs, expected_chunks):
        owner = cat.register_owner("hal-research", "curator")
        entry = cat.resolve(cat.register(owner, title, content_type=ctype, **kwargs))
        assert entry is not None and entry.chunk_count == expected_chunks


class TestIdempotency:
    def test_same_file_path_returns_existing(self, cat_with_owner):
        cat, owner = cat_with_owner
        d1 = cat.register(owner, "a.py", content_type="code", file_path="src/a.py")
        assert cat.register(owner, "a.py", content_type="code", file_path="src/a.py") == d1

    def test_idempotent_no_duplicate_jsonl(self, cat_with_owner):
        cat, owner = cat_with_owner
        cat.register(owner, "a.py", content_type="code", file_path="src/a.py")
        cat.register(owner, "a.py", content_type="code", file_path="src/a.py")
        records = [json.loads(l) for l in (cat._dir / "documents.jsonl").read_text().strip().splitlines()]
        assert len(records) == 1


class TestUpdate:
    def test_update_head_hash(self, cat_with_owner):
        cat, owner = cat_with_owner
        doc = cat.register(owner, "a.py", content_type="code", file_path="src/a.py", head_hash="aaa")
        cat.update(doc, head_hash="bbb")
        assert cat.resolve(doc).head_hash == "bbb"

    def test_update_preserves_tumbler(self, cat_with_owner):
        cat, owner = cat_with_owner
        doc = cat.register(owner, "a.py", content_type="code", file_path="src/a.py")
        cat.update(doc, chunk_count=42)
        assert cat.resolve(doc).tumbler == doc

    def test_update_merges_meta(self, cat_with_owner):
        cat, owner = cat_with_owner
        doc = cat.register(owner, "a.py", content_type="knowledge", meta={"doc_id": "abc123"})
        cat.update(doc, meta={"venue": "NeurIPS", "year_enriched": 2017})
        entry = cat.resolve(doc)
        assert entry.meta["doc_id"] == "abc123" and entry.meta["venue"] == "NeurIPS"

    def test_update_missing_tumbler_raises(self, cat):
        with pytest.raises(KeyError):
            cat.update(Tumbler.parse("1.1.999"), title="x")


class TestEnsureConsistent:
    def test_malformed_jsonl_no_crash(self, tmp_path):
        d = tmp_path / "catalog"
        d.mkdir(parents=True)
        (d / "owners.jsonl").write_text("NOT-JSON\n")
        (d / "documents.jsonl").touch()
        (d / "links.jsonl").touch()
        assert Catalog(d, d / ".catalog.db").all_documents() == []


class TestCompactReturn:
    def test_compact_returns_removed_counts(self, cat_with_owner):
        cat, owner = cat_with_owner
        doc = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        cat.update(doc, head_hash="new")
        removed = cat.compact()
        assert "documents.jsonl" in removed and removed["documents.jsonl"] >= 1


class TestTumblerPermanence:
    def test_content_hash_dedup(self, cat_with_owner):
        cat, owner = cat_with_owner
        d1 = cat.register(owner, "paper", content_type="paper", head_hash="deadbeef")
        assert cat.register(owner, "paper", content_type="paper", head_hash="deadbeef") == d1


class TestSpanValidation:
    def test_valid_line_span(self, cat_with_two_docs):
        cat, _, a, b = cat_with_two_docs
        cat.link(a, b, "quotes", created_by="user", from_span="10-20", to_span="42-57")
        link = cat.links_from(a)[0]
        assert link.from_span == "10-20" and link.to_span == "42-57"

    def test_valid_chunk_span(self, cat_with_two_docs):
        cat, _, a, b = cat_with_two_docs
        cat.link(a, b, "quotes", created_by="user", to_span="3:100-250")
        assert cat.links_from(a)[0].to_span == "3:100-250"

    def test_invalid_span_rejected(self, cat_with_two_docs):
        cat, _, a, b = cat_with_two_docs
        with pytest.raises(ValueError, match="invalid from_span"):
            cat.link(a, b, "quotes", created_by="user", from_span="garbage")


_H64 = "a" * 64


class TestSpanPattern:
    @pytest.mark.parametrize("span,expected", [
        ("chash:" + _H64, True),
        ("chash:" + "a" * 63, False),
        ("chash:" + "a" * 65, False),
        ("chash:" + "A" * 64, False),
        ("chash:" + "g" * 64, False),
        ("chash:" + _H64 + ":100-250", True),
        ("chash:" + "b" * 64 + ":0-0", True),
        ("chash:" + "g" * 64 + ":100-250", False),
        ("chash:" + _H64 + ":", False),
        ("", True),
        ("42-57", True),
        ("3:100-250", True),
    ])
    def test_span_pattern(self, span, expected):
        assert (_SPAN_PATTERN.match(span) is not None) == expected


class TestFind:
    def test_find_by_title(self, cat_with_owner):
        cat, owner = cat_with_owner
        cat.register(owner, "authentication module", content_type="code", file_path="auth.py")
        cat.register(owner, "database schema", content_type="code", file_path="db.py")
        results = cat.find("authentication")
        assert len(results) == 1 and results[0].title == "authentication module"

    def test_find_with_content_type(self, cat_with_owner):
        cat, owner = cat_with_owner
        cat.register(owner, "auth module", content_type="code", file_path="auth.py")
        cat.register(owner, "auth design", content_type="rdr", file_path="auth.md")
        results = cat.find("auth", content_type="rdr")
        assert len(results) == 1 and results[0].content_type == "rdr"


class TestByFilePath:
    def test_lookup(self, cat_with_owner):
        cat, owner = cat_with_owner
        cat.register(owner, "indexer.py", content_type="code", file_path="src/nexus/indexer.py")
        assert cat.by_file_path(owner, "src/nexus/indexer.py").title == "indexer.py"

    def test_not_found(self, cat_with_owner):
        cat, owner = cat_with_owner
        assert cat.by_file_path(owner, "nonexistent.py") is None


class TestByOwner:
    def test_list_all_for_owner(self, cat):
        o1 = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        o2 = cat.register_owner("arcaneum", "repo", repo_hash="aabb1122")
        cat.register(o1, "a.py", content_type="code", file_path="a.py")
        cat.register(o1, "b.py", content_type="code", file_path="b.py")
        cat.register(o2, "c.py", content_type="code", file_path="c.py")
        assert len(cat.by_owner(o1)) == 2


class TestDeleteDocument:
    def test_resolve_returns_none(self, cat_with_owner):
        cat, owner = cat_with_owner
        doc = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        assert cat.delete_document(doc) is True
        assert cat.resolve(doc) is None

    def test_links_preserved(self, cat_with_two_docs):
        cat, _, a, b = cat_with_two_docs
        cat.link(a, b, "cites", created_by="user")
        cat.delete_document(a)
        links = cat.links_from(a)
        assert len(links) == 1 and links[0].link_type == "cites"

    def test_jsonl_tombstone(self, cat_with_owner):
        cat, owner = cat_with_owner
        doc = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        cat.delete_document(doc)
        lines = [json.loads(l) for l in (cat._dir / "documents.jsonl").read_text().strip().splitlines()]
        tombstones = [l for l in lines if l.get("_deleted")]
        assert len(tombstones) == 1 and tombstones[0]["tumbler"] == str(doc)

    def test_rebuild_excludes(self, cat_with_owner):
        cat, owner = cat_with_owner
        doc = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        cat.delete_document(doc)
        cat.rebuild()
        assert cat.resolve(doc) is None

    def test_not_found_returns_false(self, cat):
        assert cat.delete_document(Tumbler.parse("1.1.999")) is False

    def test_fts_index_updated(self, cat_with_owner):
        cat, owner = cat_with_owner
        doc = cat.register(owner, "authentication module", content_type="code", file_path="auth.py")
        cat.delete_document(doc)
        assert len(cat.find("authentication")) == 0


class TestDescendants:
    def test_of_owner(self, cat_with_owner):
        cat, owner = cat_with_owner
        cat.register(owner, "a.py", content_type="code", file_path="a.py")
        cat.register(owner, "b.py", content_type="code", file_path="b.py")
        assert len(cat.descendants("1.1")) == 2

    def test_excludes_prefix_itself(self, cat_with_owner):
        cat, owner = cat_with_owner
        cat.register(owner, "a.py", content_type="code", file_path="a.py")
        assert "1.1" not in [r["tumbler"] for r in cat.descendants("1.1")]

    def test_of_store(self, cat):
        o1 = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
        o2 = cat.register_owner("arcaneum", "repo", repo_hash="aabb1122")
        cat.register(o1, "a.py", content_type="code", file_path="a.py")
        cat.register(o2, "b.py", content_type="code", file_path="b.py")
        assert len(cat.descendants("1")) == 2

    def test_empty(self, cat_with_owner):
        cat, _ = cat_with_owner
        assert cat.descendants("1.1") == []


class TestResolveChunk:
    def test_parses_document_prefix(self, cat_with_owner):
        cat, owner = cat_with_owner
        cat.register(owner, "a.py", content_type="code", file_path="a.py",
                     physical_collection="code__nexus", chunk_count=5)
        result = cat.resolve_chunk(Tumbler.parse("1.1.1.3"))
        assert result is not None
        assert result["document_tumbler"] == "1.1.1" and result["chunk_index"] == 3

    @pytest.mark.parametrize("tumbler_str,setup_chunks", [
        ("1.1.1", 5),        # 3-segment = document, not chunk
        ("1.1.999.3", None), # non-existent document
        ("1.1.1.10", 5),    # chunk index out of range
    ])
    def test_resolve_chunk_returns_none(self, cat_with_owner, tumbler_str, setup_chunks):
        cat, owner = cat_with_owner
        if setup_chunks is not None:
            cat.register(owner, "a.py", content_type="code", file_path="a.py",
                         physical_collection="code__nexus", chunk_count=setup_chunks)
        assert cat.resolve_chunk(Tumbler.parse(tumbler_str)) is None


class TestLinkAuditStaleSpans:
    def test_stale_span_detected(self, cat_with_two_docs):
        cat, _, a, b = cat_with_two_docs
        cat.link(a, b, "quotes", created_by="user", from_span="10-20")
        cat._db.execute("UPDATE links SET created_at = '2020-01-01T00:00:00Z' WHERE from_tumbler = ?", (str(a),))
        cat._db.commit()
        cat.update(a, head_hash="new-hash")
        audit = cat.link_audit()
        assert audit["stale_span_count"] >= 1
        assert any(s["from"] == str(a) for s in audit["stale_spans"])

    def test_no_stale_span_when_fresh(self, cat_with_two_docs):
        cat, _, a, b = cat_with_two_docs
        cat.link(a, b, "quotes", created_by="user", from_span="10-20")
        assert cat.link_audit()["stale_span_count"] == 0


class TestRebuild:
    def test_rebuild_from_jsonl(self, cat_with_owner):
        cat, owner = cat_with_owner
        doc = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        cat2 = Catalog(cat._dir, cat._dir / ".catalog.db2")
        cat2.rebuild()
        entry = cat2.resolve(doc)
        assert entry is not None and entry.title == "a.py"

    def test_rebuild_excludes_tombstoned(self, cat_with_owner):
        cat, owner = cat_with_owner
        doc = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        tombstone = {"tumbler": str(doc), "_deleted": True, "title": "", "author": "",
                     "year": 0, "content_type": "", "file_path": "a.py", "corpus": "",
                     "physical_collection": "", "chunk_count": 0, "head_hash": "",
                     "indexed_at": "", "meta": {}}
        with (cat._dir / "documents.jsonl").open("a") as f:
            f.write(json.dumps(tombstone) + "\n")
        cat2 = Catalog(cat._dir, cat._dir / ".catalog.db2")
        cat2.rebuild()
        assert cat2.resolve(doc) is None


class TestEnsureConsistentDegradedFlag:
    def test_degraded_false_on_success(self, cat):
        assert cat.degraded is False

    def test_degraded_true_on_rebuild_failure(self, tmp_path):
        d = tmp_path / "catalog"
        d.mkdir()
        (d / "documents.jsonl").write_text("{}\n")
        with patch("nexus.catalog.catalog.CatalogDB.rebuild", side_effect=RuntimeError("disk full")):
            assert Catalog(d, d / ".catalog.db").degraded is True


class TestResolveSpan:
    def test_resolve_chash_found(self, span_env):
        cat, t3, col_name, col = span_env
        h = "a" * 64
        col.add(ids=["id1"], documents=["hello world"], metadatas=[{"chunk_text_hash": h, "source": "test.py"}])
        result = cat.resolve_span(f"chash:{h}", col_name, t3)
        assert result is not None and result["chunk_text"] == "hello world"
        assert result["chunk_hash"] == h and result["metadata"]["source"] == "test.py"

    def test_resolve_chash_with_char_range(self, span_env):
        cat, t3, col_name, col = span_env
        h = "c" * 64
        col.add(ids=["id1"], documents=["def hello(): return 'world'"], metadatas=[{"chunk_text_hash": h}])
        result = cat.resolve_span(f"chash:{h}:4-9", col_name, t3)
        assert result is not None and result["chunk_text"] == "hello" and result["char_range"] == (4, 9)

    def test_resolve_chash_range_out_of_bounds(self, span_env):
        cat, t3, col_name, col = span_env
        h = "d" * 64
        col.add(ids=["id1"], documents=["short"], metadatas=[{"chunk_text_hash": h}])
        result = cat.resolve_span(f"chash:{h}:2-999", col_name, t3)
        assert result is not None and result["chunk_text"] == "ort"

    @pytest.mark.parametrize("span", [
        "chash:" + "b" * 64,  # not found
        "",                    # empty
        "42-57",               # legacy
    ])
    def test_resolve_span_returns_none(self, span_env, span):
        cat, t3, col_name, col = span_env
        assert cat.resolve_span(span, col_name, t3) is None


class TestLinkChashSpans:
    @pytest.mark.parametrize("from_span,to_span", [
        ("chash:" + "a" * 64, ""),
        ("", "chash:" + "b" * 64),
        ("chash:" + "a" * 64, "chash:" + "b" * 64),
        ("", ""),
    ])
    def test_link_with_chash_spans(self, cat, from_span, to_span):
        owner = cat.register_owner("nexus", "repo", repo_hash="abc123")
        a = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        b = cat.register(owner, "b.py", content_type="code", file_path="b.py")
        kwargs = {"from_span": from_span} if from_span else {}
        if to_span:
            kwargs["to_span"] = to_span
        assert cat.link(a, b, "cites", "test-agent", **kwargs) is True

    @pytest.mark.parametrize("bad_span", [
        "chash:" + "z" * 64,   # non-hex
        "chash:" + "a" * 63,   # too short
    ])
    def test_link_rejects_invalid_chash(self, cat_with_two_docs, bad_span):
        cat, _, a, b = cat_with_two_docs
        with pytest.raises(ValueError, match="invalid"):
            cat.link(a, b, "cites", "test-agent", from_span=bad_span)


class TestLinkChashValidation:
    def _setup_val_env(self, cat, tmp_path):
        t3 = chromadb.EphemeralClient()
        col_name = f"code__val_{tmp_path.name}"
        col = t3.create_collection(col_name)
        owner = cat.register_owner("nexus", "repo", repo_hash="abc123")
        a = cat.register(owner, "a.py", content_type="code", file_path="a.py", physical_collection=col_name)
        b = cat.register(owner, "b.py", content_type="code", file_path="b.py", physical_collection=col_name)
        return t3, col_name, col, a, b

    def test_rejects_unresolvable_chash_span(self, cat, tmp_path):
        t3, col_name, col, a, b = self._setup_val_env(cat, tmp_path)
        mock_t3 = MagicMock()
        mock_t3._client = t3
        with patch("nexus.db.make_t3", return_value=mock_t3):
            with pytest.raises(ValueError, match="unresolvable span"):
                cat.link(a, b, "cites", "test-agent", from_span="chash:" + "a" * 64)

    def test_accepts_resolvable_chash_span(self, cat, tmp_path):
        t3, col_name, col, a, b = self._setup_val_env(cat, tmp_path)
        h = "b" * 64
        col.add(ids=["c1"], documents=["some code"], metadatas=[{"chunk_text_hash": h}])
        mock_t3 = MagicMock()
        mock_t3._client = t3
        with patch("nexus.db.make_t3", return_value=mock_t3):
            assert cat.link(a, b, "cites", "test-agent", from_span=f"chash:{h}") is True

    def test_allow_dangling_skips_chash_validation(self, cat_with_two_docs):
        cat, _, a, b = cat_with_two_docs
        assert cat.link(a, b, "cites", "test-agent",
                        from_span="chash:" + "a" * 64, allow_dangling=True) is True
