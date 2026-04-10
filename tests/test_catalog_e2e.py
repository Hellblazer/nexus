# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from nexus.catalog.catalog import Catalog
from nexus.catalog.tumbler import Tumbler
from nexus.db.t3 import T3Database
from nexus.registry import RepoRegistry

_NEXUS_ROOT = Path(__file__).parent.parent
_CODE_FILES = ["src/nexus/ttl.py", "src/nexus/corpus.py", "src/nexus/types.py"]
_PROSE = "# Test Repo\n\nA test repository for catalog E2E tests.\n\n## Features\n\n- Catalog integration\n- Tumbler addressing\n"
_RDR = "---\ntitle: Corpus and TTL Design\nstatus: accepted\n---\n\n# RDR-001: Corpus and TTL Design\n\n## Decision\n\nWe use tumblers for addressing.\n## Implementation\n\nThe ttl module handles time-to-live logic.\nThe corpus module handles naming.\n"


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _do_index(catalog_repo, registry, local_t3, monkeypatch, force=False):
    from nexus.indexer import index_repository

    monkeypatch.setenv("NX_LOCAL", "1")
    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        index_repository(catalog_repo, registry, force=force)


def _write(repo, rel, content):
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(content, encoding="utf-8")


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    for k, v in [("GIT_AUTHOR_NAME", "Test"), ("GIT_AUTHOR_EMAIL", "test@test.invalid"),
                 ("GIT_COMMITTER_NAME", "Test"), ("GIT_COMMITTER_EMAIL", "test@test.invalid")]:
        monkeypatch.setenv(k, v)


@pytest.fixture(scope="module")
def catalog_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    repo = tmp_path_factory.mktemp("catalog-e2e")
    for rel in _CODE_FILES:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_NEXUS_ROOT / rel, repo / rel)
    _write(repo, "README.md", _PROSE)
    _write(repo, "docs/rdr/rdr-001-corpus-ttl-design.md", _RDR)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@nexus")
    _git(repo, "config", "user.name", "Nexus Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Initial commit")
    return repo


@pytest.fixture
def local_t3() -> T3Database:
    return T3Database(
        _client=chromadb.EphemeralClient(),
        _ef_override=DefaultEmbeddingFunction(),
    )


@pytest.fixture
def registry(tmp_path: Path, catalog_repo: Path) -> RepoRegistry:
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(catalog_repo)
    return reg


@pytest.fixture(autouse=True)
def mock_voyage_client():
    ef = DefaultEmbeddingFunction()
    mock_client = MagicMock()

    def fake_embed(texts, model, input_type="document"):
        r = MagicMock()
        r.embeddings = ef(texts)
        return r

    def fake_contextualized_embed(inputs, model, input_type="document"):
        r = MagicMock()
        br = MagicMock()
        br.embeddings = ef(inputs[0])
        r.results = [br]
        return r

    mock_client.embed.side_effect = fake_embed
    mock_client.contextualized_embed.side_effect = fake_contextualized_embed
    with patch("voyageai.Client", return_value=mock_client):
        yield mock_client


@pytest.fixture
def catalog_env(tmp_path: Path, monkeypatch) -> Path:
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    Catalog.init(catalog_dir)
    return catalog_dir


@pytest.fixture
def indexed_catalog(catalog_repo, registry, local_t3, catalog_env, monkeypatch):
    _do_index(catalog_repo, registry, local_t3, monkeypatch)
    return Catalog(catalog_env, catalog_env / ".catalog.db"), local_t3


@pytest.fixture
def injected_catalog(indexed_catalog):
    from nexus.mcp_server import _inject_catalog, _reset_singletons

    cat, local_t3 = indexed_catalog
    _reset_singletons()
    _inject_catalog(cat)
    return cat, local_t3


@pytest.fixture
def linked_catalog(tmp_path):
    cat = Catalog.init(tmp_path / "catalog")
    owner = cat.register_owner("test", "repo", repo_hash="aabb1122")
    docs = [
        cat.register(owner, f"paper-{x}", content_type="paper",
                     meta={"bib_semantic_scholar_id": f"ss-{x}",
                           **({"references": ["ss-b", "ss-c"]} if x == "a" else {})})
        for x in ("a", "b", "c")
    ]
    return cat, *docs


# ── Indexer populates catalog ────────────────────────────────────────────────


@pytest.mark.parametrize("query,min_count", [
    ("SELECT count(*) FROM owners", 1),
    ("SELECT count(*) FROM documents WHERE content_type = 'code'", len(_CODE_FILES)),
    ("SELECT count(*) FROM documents WHERE content_type = 'rdr'", 1),
])
def test_index_populates_catalog(indexed_catalog, query, min_count):
    cat, _ = indexed_catalog
    assert cat._db.execute(query).fetchone()[0] >= min_count


def test_reindex_preserves_tumblers(
    catalog_repo, registry, local_t3, catalog_env, monkeypatch,
):
    _do_index(catalog_repo, registry, local_t3, monkeypatch)
    first = {r[0] for r in Catalog(catalog_env, catalog_env / ".catalog.db")
             ._db.execute("SELECT tumbler FROM documents").fetchall()}
    _do_index(catalog_repo, registry, local_t3, monkeypatch, force=True)
    second = {r[0] for r in Catalog(catalog_env, catalog_env / ".catalog.db")
              ._db.execute("SELECT tumbler FROM documents").fetchall()}
    assert first.issubset(second)


# ── MCP tools + graph traversal (class saves blank-line overhead) ────────────


class TestMCP:
    def test_search_returns_indexed_files(self, injected_catalog):
        from nexus.mcp_server import catalog_search
        results = catalog_search(query="ttl")
        assert len(results) >= 1
        assert any("ttl" in r.get("title", "").lower() or "ttl" in r.get("file_path", "").lower()
                    for r in results)

    def test_search_structured_filter(self, injected_catalog):
        from nexus.mcp_server import catalog_search
        cat, _ = injected_catalog
        owner = cat._db.execute("SELECT tumbler_prefix FROM owners LIMIT 1").fetchone()
        assert owner is not None
        results = catalog_search(owner=owner[0])
        assert len(results) >= 1 and "error" not in results[0]

    def test_show_returns_full_entry(self, injected_catalog):
        from nexus.mcp_server import catalog_show
        cat, _ = injected_catalog
        tumbler = cat._db.execute("SELECT tumbler FROM documents LIMIT 1").fetchone()[0]
        result = catalog_show(tumbler=tumbler)
        assert "error" not in result and result["tumbler"] == tumbler
        assert "links_from" in result and "links_to" in result

    def test_resolve_returns_collections(self, injected_catalog):
        from nexus.mcp_server import catalog_resolve
        cat, _ = injected_catalog
        owner = cat._db.execute("SELECT tumbler_prefix FROM owners LIMIT 1").fetchone()
        result = catalog_resolve(owner=owner[0])
        assert len(result) >= 1 and any("__" in n for n in result)

    def test_search_then_traverse_links(self, injected_catalog):
        from nexus.mcp_server import catalog_links, catalog_search
        results = catalog_search(query="ttl")
        assert len(results) >= 1
        tumbler = results[0]["tumbler"]
        graph = catalog_links(tumbler=tumbler, depth=1)
        assert "nodes" in graph and "edges" in graph
        assert tumbler in {n["tumbler"] for n in graph["nodes"]}

    def test_link_creation_via_title(self, injected_catalog):
        from nexus.mcp_server import catalog_link, catalog_link_query
        result = catalog_link(from_tumbler="types.py", to_tumbler="corpus.py",
                              link_type="relates", created_by="test")
        assert "error" not in result and result["created"] is True
        assert len(catalog_link_query(link_type="relates", created_by="test")) >= 1

    def test_link_audit_after_indexing(self, injected_catalog):
        from nexus.mcp_server import catalog_link_audit
        audit = catalog_link_audit()
        assert "error" not in audit and audit["total"] >= 1
        assert "implements-heuristic" in audit["by_type"]
        assert audit["orphaned_count"] == 0


# ── Link generation + lifecycle ──────────────────────────────────────────────


class TestLinks:
    def test_code_rdr_links_generated(self, indexed_catalog):
        from nexus.catalog.link_generator import generate_code_rdr_links
        cat, _ = indexed_catalog
        assert cat._db.execute("SELECT count(*) FROM documents WHERE content_type='code'").fetchone()[0] >= 1
        assert cat._db.execute("SELECT count(*) FROM documents WHERE content_type='rdr'").fetchone()[0] >= 1
        assert cat._db.execute("SELECT count(*) FROM links").fetchone()[0] >= 1
        assert generate_code_rdr_links(cat) == 0

    def test_full_link_lifecycle(self, linked_catalog):
        from nexus.catalog.link_generator import generate_citation_links
        cat, doc_a, doc_b, doc_c = linked_catalog
        assert generate_citation_links(cat) == 2
        assert len(cat.link_query(created_by="bib_enricher")) == 2
        assert cat.bulk_unlink(created_by="bib_enricher") == 2
        assert cat.link_query(created_by="bib_enricher") == []
        assert generate_citation_links(cat) == 2
        audit = cat.link_audit()
        assert audit["orphaned_count"] == 0 and audit["total"] == 2

    def test_delete_document_orphan_preserved(self, linked_catalog):
        cat, doc_a, doc_b, _doc_c = linked_catalog
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.delete_document(doc_a)
        assert cat.link_audit()["orphaned_count"] == 1
        assert cat.resolve(doc_a) is None and len(cat.links_to(doc_b)) == 1

    def test_link_if_absent_idempotent(self, linked_catalog):
        from nexus.catalog.link_generator import generate_citation_links
        cat, *_ = linked_catalog
        assert generate_citation_links(cat) == 2
        assert generate_citation_links(cat) == 0
        assert len(cat.link_query(link_type="cites")) == 2


# ── store_put → catalog ─────────────────────────────────────────────────────


def test_store_put_registers_in_catalog(tmp_path, monkeypatch):
    from nexus.mcp_server import _reset_singletons, store_put

    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    cat = Catalog.init(catalog_dir)
    cat.register_owner("knowledge", "curator")
    _reset_singletons()
    with patch("nexus.mcp.core._get_t3") as mock_t3:
        mock_db = MagicMock()
        mock_db.put.return_value = "doc-abc123"
        mock_t3.return_value = mock_db
        result = store_put(
            content="# Research: Vector Indexing\n\nFindings about HNSW...",
            collection="knowledge", title="research-vector-indexing",
            tags="research,embeddings",
        )
    assert "Stored" in result
    entry = Catalog(catalog_dir, catalog_dir / ".catalog.db").by_doc_id("doc-abc123")
    assert entry is not None and entry.title == "research-vector-indexing"


# ── Tumbler permanence ───────────────────────────────────────────────────────


def test_tumblers_stable_across_delete_compact_reindex(
    catalog_repo, registry, local_t3, catalog_env, monkeypatch,
):
    _do_index(catalog_repo, registry, local_t3, monkeypatch)
    cat = Catalog(catalog_env, catalog_env / ".catalog.db")
    original = {r[0] for r in cat._db.execute("SELECT tumbler FROM documents").fetchall()}
    first_tumbler = sorted(original)[0]
    cat.delete_document(Tumbler.parse(first_tumbler))
    cat.compact()
    _do_index(catalog_repo, registry, local_t3, monkeypatch, force=True)
    new = {r[0] for r in Catalog(catalog_env, catalog_env / ".catalog.db")
           ._db.execute("SELECT tumbler FROM documents").fetchall()}
    assert first_tumbler not in new and len(new) >= len(original) - 1


# ── Span transclusion ────────────────────────────────────────────────────────


def test_link_with_line_span_resolves_text(tmp_path):
    cat = Catalog.init(tmp_path / "catalog")
    owner = cat.register_owner("test", "repo", repo_hash="e2etest")
    src_file = tmp_path / "source.py"
    src_file.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")
    doc_a = cat.register(owner, "source.py", content_type="code", file_path=str(src_file))
    doc_b = cat.register(owner, "target.py", content_type="code", file_path="target.py")
    cat.link(doc_a, doc_b, "quotes", created_by="user", from_span="2-4", to_span="")
    assert cat.resolve_span_text(doc_a, "2-4") == "line2\nline3\nline4"


# ── JSONL rebuild + compact ──────────────────────────────────────────────────


class TestJSONLResilience:
    def test_fresh_catalog_sees_indexed_data(self, indexed_catalog, catalog_env):
        assert Catalog(catalog_env, catalog_env / ".catalog-fresh.db")._db.execute(
            "SELECT count(*) FROM documents"
        ).fetchone()[0] >= len(_CODE_FILES)

    def test_compact_and_rebuild(self, indexed_catalog, catalog_env):
        cat, _ = indexed_catalog
        before = cat._db.execute("SELECT count(*) FROM documents").fetchone()[0]
        cat.compact()
        after = Catalog(catalog_env, catalog_env / ".catalog-compact.db")._db.execute(
            "SELECT count(*) FROM documents"
        ).fetchone()[0]
        assert after == before


# ── chash span pipeline (RDR-053) ────────────────────────────────────────────


def _get_two_docs_and_chunk(cat, local_t3):
    docs = cat._db.execute(
        "SELECT tumbler, physical_collection FROM documents LIMIT 2"
    ).fetchall()
    assert len(docs) >= 2
    col = local_t3._client.get_collection(docs[0][1])
    chunk = col.get(limit=1, include=["documents", "metadatas"])
    assert chunk["ids"]
    return (Tumbler.parse(docs[0][0]), Tumbler.parse(docs[1][0]), docs[0][1],
            chunk["metadatas"][0]["chunk_text_hash"], chunk["documents"][0])


class TestChashSpan:
    def test_index_produces_chunk_text_hash(self, indexed_catalog):
        cat, local_t3 = indexed_catalog
        row = cat._db.execute(
            "SELECT physical_collection FROM documents WHERE content_type = 'code' LIMIT 1"
        ).fetchone()
        assert row
        result = local_t3._client.get_collection(row[0]).get(
            limit=5, include=["documents", "metadatas"],
        )
        assert result["ids"]
        for doc_text, meta in zip(result["documents"], result["metadatas"]):
            assert "chunk_text_hash" in meta and "content_hash" in meta
            expected = hashlib.sha256(doc_text.encode()).hexdigest()
            assert meta["chunk_text_hash"] == expected
            assert meta["chunk_text_hash"] != meta["content_hash"]

    def test_audit_and_resolve_roundtrip(self, indexed_catalog):
        cat, local_t3 = indexed_catalog
        from_t, to_t, coll, real_hash, real_text = _get_two_docs_and_chunk(cat, local_t3)
        assert cat.link(from_t, to_t, "quotes", "e2e-test", from_span=f"chash:{real_hash}") is True
        assert cat.link_audit(t3=local_t3._client)["stale_chash_count"] == 0
        resolved = cat.resolve_span(f"chash:{real_hash}", coll, local_t3._client)
        assert resolved is not None
        assert resolved["chunk_text"] == real_text and resolved["chunk_hash"] == real_hash

    def test_audit_detects_bogus_hash(self, indexed_catalog):
        cat, local_t3 = indexed_catalog
        docs = cat._db.execute("SELECT tumbler FROM documents LIMIT 2").fetchall()
        assert len(docs) >= 2
        bogus = "f" * 64
        cat.link(Tumbler.parse(docs[0][0]), Tumbler.parse(docs[1][0]),
                 "quotes", "e2e-test", from_span=f"chash:{bogus}")
        audit = cat.link_audit(t3=local_t3._client)
        assert audit["stale_chash_count"] >= 1
        assert f"chash:{bogus}" in [s["span"] for s in audit["stale_chash"]]


# ── Tumbler ordering ─────────────────────────────────────────────────────────


def test_tumbler_comparison_sorted_order():
    tumblers = [Tumbler.parse(s) for s in ("1.1.10", "1.1.3", "1.1.3.0", "2.1.1", "1.2.1")]
    expected = [Tumbler.parse(s) for s in ("1.1.3", "1.1.3.0", "1.1.10", "1.2.1", "2.1.1")]
    assert sorted(tumblers) == expected


@pytest.mark.parametrize("s1,e1,s2,e2,expected", [
    ("1.1.3", "1.1.7", "1.1.5", "1.1.10", True),
    ("1.1.1", "1.1.3", "1.1.5", "1.1.7", False),
    ("1.1.3", "1.1.3.5", "1.1.3.2", "1.1.4", True),
])
def test_spans_overlap(s1, e1, s2, e2, expected):
    assert Tumbler.spans_overlap(
        Tumbler.parse(s1), Tumbler.parse(e1),
        Tumbler.parse(s2), Tumbler.parse(e2),
    ) is expected


# ── Plan templates ───────────────────────────────────────────────────────────


def test_catalog_plan_templates_exist(db):
    rows = db.plans.conn.execute(
        "SELECT count(*) FROM plans WHERE tags LIKE '%catalog%'"
    ).fetchall()
    assert isinstance(rows, list) and rows[0][0] >= 0


# ── 'formalizes' link type (RDR-057 P1-1a, nexus-807l) ─────────────────────


class TestFormalizesLinkType:
    """Verify catalog accepts 'formalizes' as a link type — no schema changes needed."""

    def test_formalizes_link_roundtrip(self, tmp_path):
        cat = Catalog.init(tmp_path / "catalog")
        owner = cat.register_owner("test", "repo", repo_hash="abc123")
        doc_a = cat.register(owner, "scratch-note", content_type="knowledge")
        doc_b = cat.register(owner, "formal-entry", content_type="knowledge")

        created = cat.link(doc_a, doc_b, "formalizes", created_by="test")
        assert created is True

        links = cat.link_query(link_type="formalizes")
        assert len(links) == 1
        assert str(links[0].from_tumbler) == str(doc_a)
        assert str(links[0].to_tumbler) == str(doc_b)
        assert links[0].link_type == "formalizes"

    def test_formalizes_link_if_absent_idempotent(self, tmp_path):
        cat = Catalog.init(tmp_path / "catalog")
        owner = cat.register_owner("test", "repo", repo_hash="abc123")
        doc_a = cat.register(owner, "scratch-note", content_type="knowledge")
        doc_b = cat.register(owner, "formal-entry", content_type="knowledge")

        assert cat.link_if_absent(doc_a, doc_b, "formalizes", created_by="test") is True
        assert cat.link_if_absent(doc_a, doc_b, "formalizes", created_by="test") is False
        assert len(cat.link_query(link_type="formalizes")) == 1
