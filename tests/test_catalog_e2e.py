# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction

from nexus.catalog.tumbler import Tumbler
from nexus.db.t3 import T3Database
from nexus.registry import RepoRegistry
from tests._catalog_fixture_ops import ActiveCatalog, active_reader
from tests.conftest import fake_credentials, make_vector_test_client

_NEXUS_ROOT = Path(__file__).parent.parent
_CODE_FILES = ["src/nexus/ttl.py", "src/nexus/corpus.py", "src/nexus/types.py"]
_PROSE = "# Test Repo\n\nA test repository for catalog E2E tests.\n\n## Features\n\n- Catalog integration\n- Tumbler addressing\n"
_RDR = "---\ntitle: Corpus and TTL Design\nstatus: accepted\n---\n\n# RDR-001: Corpus and TTL Design\n\n## Decision\n\nWe use tumblers for addressing.\n## Implementation\n\nThe ttl module (src/nexus/ttl.py) handles time-to-live logic.\nThe corpus module (src/nexus/corpus.py) handles naming.\n"


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _do_index(catalog_repo, registry, local_t3, monkeypatch, force=False):
    from nexus.indexer import index_repository

    monkeypatch.setenv("NX_LOCAL", "1")
    # nexus-i711w: fake_credentials (not the blanket ``lambda k: "test-key"``
    # stub) — the blanket form answers ``service_url`` too, poisoning the
    # engine-catalog endpoint resolution with a non-URL. Same fix the indexer
    # e2e suite took in 5cbd1f90 (nexus-aqbrk).
    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=fake_credentials()):
        index_repository(catalog_repo, registry, force=force)


def _write(repo, rel, content):
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(content, encoding="utf-8")


# nexus-i711w C-store: the whole-module ``local_catalog_backend`` pin
# (f4030fe5, "local by construction") is replaced by the per-test SUBJECT
# split. The pinned failure mode this file carried — every service request
# dying on a poisoned base_url — was the blanket credential stub in
# ``_do_index``, not the journey's construction: the stub answered
# ``service_url`` with "test-key" (see ``fake_credentials``' docstring for the
# measured mechanism). With the stub fixed, the CATALOG half of the journey
# rides the suite's engine substrate (per-test tenant via ``t2_service_env``)
# while the VECTOR half stays on the test seam (``make_vector_test_client``
# via the ``make_t3`` patch) — only the catalog substrate moves.
#
# Tests whose SUBJECT was the local machinery (JSONL rebuild/compact, the
# local-only ``link_audit(t3=...)`` chash audit) retired WITH the local
# catalog src (nexus-i711w terminal deletion) — see the fixture tombstone
# below for the recorded GAP-CANDIDATEs.


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
        _client=make_vector_test_client(),
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


# nexus-i711w terminal deletion: the DIE-set carrier fixtures
# (catalog_env / indexed_catalog / injected_catalog) retired WITH
# nexus.catalog.catalog and their 6 pinned tests (link-audit, JSONL
# rebuild/compact, chash-span audit roundtrip, compact-leg tumbler
# permanence). GAP-CANDIDATEs recorded here from the dead tests'
# docstrings: the link-audit contract survives (the MCP tool ships in
# service mode) but HttpCatalogClient.link_audit returns {} — nothing
# pins it or the stale-chash audit on the surviving substrate; needs a
# service-side implementation + test, not a conversion.


@pytest.fixture
def indexed_active(catalog_repo, registry, local_t3, monkeypatch):
    """PORT-side journey state: index the repo, return the ACTIVE catalog.

    The catalog hook writes to whichever catalog backend is live — under the
    suite default that is the per-test engine tenant (``t2_service_env``); no
    ``Catalog.init`` is needed because ``_catalog_hook``'s local
    ``is_initialized`` gate does not apply in service mode (indexer.py:933).
    The vector half stays on the test seam (``make_t3`` patched to
    ``local_t3``) per the port contract: only the catalog substrate moves.
    """
    _do_index(catalog_repo, registry, local_t3, monkeypatch)
    return ActiveCatalog(), local_t3


@pytest.fixture
def injected_active(indexed_active):
    """PORT-side sibling of ``injected_catalog``: same singleton seeding, but
    the MCP tools resolve the ACTIVE catalog through the factory (service
    handle under the suite default) instead of a local dir via env."""
    from nexus.mcp_server import _reset_singletons
    from nexus import mcp_infra

    cat, local_t3 = indexed_active
    _reset_singletons()
    mcp_infra._t3_instance = local_t3
    return cat, local_t3


@pytest.fixture
def linked_active():
    """Three registered papers with bib metadata, on the active catalog.

    PORT-VERIFY: ``meta`` carries a LIST value (``references``) — the link
    generator reads it back via ``entry.meta``, so the engine's meta JSON
    round-trip must preserve list-typed values verbatim.
    """
    cat = ActiveCatalog()
    owner = cat.register_owner("test", "repo", repo_hash="aabb1122")
    docs = [
        cat.register(owner, f"paper-{x}", content_type="paper",
                     meta={"bib_semantic_scholar_id": f"ss-{x}",
                           **({"references": ["ss-b", "ss-c"]} if x == "a" else {})})
        for x in ("a", "b", "c")
    ]
    return cat, *docs


# ── Indexer populates catalog ────────────────────────────────────────────────


@pytest.mark.parametrize("kind,min_count", [
    ("owners", 1),
    ("code", len(_CODE_FILES)),
    ("rdr", 1),
])
def test_index_populates_catalog(indexed_active, kind, min_count):
    # nexus-i711w: raw ``SELECT count(*)`` parametrization converted by
    # meaning — owner count via list_owners(), doc counts via
    # by_content_type(), the public reads on both substrates.
    cat, _ = indexed_active
    if kind == "owners":
        assert len(cat.list_owners()) >= min_count
    else:
        assert len(cat.by_content_type(kind)) >= min_count


def test_reindex_preserves_tumblers(
    catalog_repo, registry, local_t3, monkeypatch,
):
    _do_index(catalog_repo, registry, local_t3, monkeypatch)
    first = {str(d.tumbler) for d in active_reader().all_documents()}
    _do_index(catalog_repo, registry, local_t3, monkeypatch, force=True)
    second = {str(d.tumbler) for d in active_reader().all_documents()}
    # nexus-i711w: ``first`` non-emptiness added — the original subset
    # assertion was vacuously true on an empty first index.
    assert first and first.issubset(second)


# ── MCP tools + graph traversal (class saves blank-line overhead) ────────────


class TestMCP:
    def test_search_returns_indexed_files(self, injected_active):
        """nexus-3lswy: expects 2 matches (ttl.py + the RDR doc), not 3.
        Pre-fix, the RDR file was registered TWICE — once under the repo
        owner via the batched _catalog_hook pass, and again under a
        SEPARATE "curator" owner via doc_indexer's _catalog_markdown_hook
        (called from the now-retired _discover_and_index_rdrs path) — two
        Document rows for one physical file, both matching "ttl" (one via
        file_path, one via frontmatter title). Routing RDR files through
        _index_prose_file removes the second, redundant registration."""
        # PORT-VERIFY: exact-count 2 depends on the engine's free-text
        # match over title + file_path agreeing with the local FTS shape.
        from nexus.mcp_server import catalog_search
        results = catalog_search(query="ttl")
        assert len(results) == 2
        assert any("ttl" in r.get("title", "").lower() or "ttl" in r.get("file_path", "").lower()
                    for r in results)

    def test_search_structured_filter(self, injected_active):
        from nexus.mcp_server import catalog_search
        cat, _ = injected_active
        # nexus-i711w: the raw ``SELECT tumbler_prefix FROM owners LIMIT 1``
        # took an ARBITRARY owner; the meaning is "the repo owner that the
        # index run registered" — select it by type so the assertion cannot
        # silently land on a curator owner.
        owners = [o for o in cat.list_owners() if o.get("owner_type") == "repo"]
        assert owners
        # PORT-VERIFY: 5 == 3 code files + README + RDR under the repo owner.
        results = catalog_search(owner=owners[0]["tumbler_prefix"])
        assert len(results) == 5 and "error" not in results[0]

    def test_show_returns_full_entry(self, injected_active):
        from nexus.mcp_server import catalog_show
        cat, _ = injected_active
        docs = list(cat.all_documents())
        assert docs
        tumbler = str(docs[0].tumbler)
        result = catalog_show(tumbler=tumbler)
        assert "error" not in result and result["tumbler"] == tumbler
        assert "links_from" in result and "links_to" in result

    def test_resolve_returns_collections(self, injected_active):
        from nexus.mcp_server import catalog_resolve
        cat, _ = injected_active
        owners = [o for o in cat.list_owners() if o.get("owner_type") == "repo"]
        assert owners
        # PORT-VERIFY: 3 == code/docs/rdr collections registered by the hook.
        result = catalog_resolve(owner=owners[0]["tumbler_prefix"])
        assert len(result) == 3 and any("__" in n for n in result)

    def test_search_then_traverse_links(self, injected_active):
        """nexus-3lswy: 2 matches, not 3 — see test_search_returns_indexed_files
        for why (removal of the RDR double-registration-under-two-owners bug)."""
        from nexus.mcp_server import catalog_links, catalog_search
        results = catalog_search(query="ttl")
        assert len(results) == 2
        tumbler = results[0]["tumbler"]
        graph = catalog_links(tumbler=tumbler, depth=1)
        assert "nodes" in graph and "edges" in graph
        assert tumbler in {n["tumbler"] for n in graph["nodes"]}

    def test_link_creation_via_title(self, injected_active):
        # PORT-VERIFY: title -> tumbler resolution inside catalog_link must
        # find "types.py"/"corpus.py" on the engine's metadata search.
        from nexus.mcp_server import catalog_link, catalog_link_query
        result = catalog_link(from_tumbler="types.py", to_tumbler="corpus.py",
                              link_type="relates", created_by="test")
        assert "error" not in result and result["created"] is True
        # nexus-8g79.23: we just created exactly one link.
        assert len(catalog_link_query(link_type="relates", created_by="test")) == 1


# ── Link generation + lifecycle ──────────────────────────────────────────────


class TestLinks:
    def test_full_link_lifecycle(self, linked_active):
        from nexus.catalog.link_generator import generate_citation_links
        cat, doc_a, doc_b, doc_c = linked_active
        assert generate_citation_links(cat) == 2
        assert len(cat.link_query(created_by="bib_enricher")) == 2
        assert cat.bulk_unlink(created_by="bib_enricher") == 2
        assert cat.link_query(created_by="bib_enricher") == []
        assert generate_citation_links(cat) == 2
        # nexus-i711w: ``link_audit()`` is unimplemented on the service client
        # (returns {}); the two facts the audit assertion pinned are asserted
        # directly — no dangling link endpoints, and exactly the 2 cite links.
        assert cat.orphaned_links() == []
        assert len(cat.link_query(link_type="cites")) == 2

    def test_delete_document_orphan_preserved(self, linked_active):
        cat, doc_a, doc_b, _doc_c = linked_active
        cat.link(doc_a, doc_b, "cites", created_by="user")
        cat.delete_document(doc_a)
        # nexus-i711w: audit orphaned_count -> orphaned_links() (the reader
        # that backs the same fact on both substrates). PORT-VERIFY: pins the
        # engine's orphan-preservation semantics — catalog_links carries no FK
        # to catalog_documents by design (see orphaned_links docstring), so a
        # cascade-on-delete here is a product regression, not a test artifact.
        assert len(cat.orphaned_links()) == 1
        assert cat.resolve(doc_a) is None and len(cat.links_to(doc_b)) == 1

    def test_link_if_absent_idempotent(self, linked_active):
        from nexus.catalog.link_generator import generate_citation_links
        cat, *_ = linked_active
        assert generate_citation_links(cat) == 2
        assert generate_citation_links(cat) == 0
        assert len(cat.link_query(link_type="cites")) == 2


# ── store_put → catalog ─────────────────────────────────────────────────────


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="nexus-tz1cx: by_doc_id asks a different question per substrate — "
    "the service client does a tumbler resolve instead of the meta.doc_id "
    "lookup, silently returning None for the store_put hook's chunk-id "
    "identity. CONFIRMED LIVE by this port (2026-07-30). Flips with tz1cx.",
)
def test_store_put_registers_in_catalog():
    """RDR-101 Phase 3 PR δ Stage B.5 changed the timing so the catalog
    hook now runs BEFORE the T3 write (so the chunk can carry the
    catalog tumbler as ``doc_id``). The catalog's legacy
    ``meta.doc_id`` lookup field is populated with the deterministic
    ``chunk_chroma_id`` that ``T3Database.put`` derives. RDR-180
    inverts the RDR-108 D1 truncation: the derivation is now the FULL
    ``sha256(content)`` hexdigest, so single-chunk MCP docs land
    directly under their content-addressed natural ID.
    """
    import hashlib as _hl
    from nexus.mcp_server import _reset_singletons, store_put

    # nexus-i711w: no Catalog.init / NEXUS_CATALOG_PATH — in service mode the
    # store_put hook writes to the active (engine) catalog directly.
    ActiveCatalog().register_owner("knowledge", "curator")
    _reset_singletons()
    content = "# Research: Vector Indexing\n\nFindings about HNSW..."
    with patch("nexus.mcp.core._get_t3") as mock_t3:
        mock_db = MagicMock()
        mock_db.put.return_value = "doc-abc123"
        mock_t3.return_value = mock_db
        result = store_put(
            content=content,
            collection="knowledge", title="research-vector-indexing",
            tags="research,embeddings",
        )
    assert "Stored" in result
    # The catalog stores the deterministic chunk_chroma_id derived from
    # content (the natural ID per RDR-108 D1).
    # PORT-VERIFY: by_doc_id must resolve the meta.doc_id lookup field on
    # the engine substrate.
    expected_chunk_chroma_id = _hl.sha256(content.encode()).hexdigest()
    entry = active_reader().by_doc_id(expected_chunk_chroma_id)
    assert entry is not None and entry.title == "research-vector-indexing"


# ── Tumbler permanence ───────────────────────────────────────────────────────


def test_tumblers_stable_across_delete_reindex(
    catalog_repo, registry, local_t3, monkeypatch,
):
    """Ported meaning of the compact-leg test (nexus-i711w): a deleted
    document's tumbler is never reused when a force re-index re-registers
    the still-present file. The JSONL-compact step has no service
    equivalent by design; tumbler permanence is the surviving contract."""
    # PORT-VERIFY: pins the engine's tumbler non-reuse across delete +
    # re-register (server-side next_seq must not backfill freed tumblers).
    _do_index(catalog_repo, registry, local_t3, monkeypatch)
    cat = ActiveCatalog()
    original = {str(d.tumbler) for d in cat.all_documents()}
    assert original
    first_tumbler = sorted(original)[0]
    cat.delete_document(Tumbler.parse(first_tumbler))
    _do_index(catalog_repo, registry, local_t3, monkeypatch, force=True)
    new = {str(d.tumbler) for d in cat.all_documents()}
    assert first_tumbler not in new and len(new) >= len(original) - 1


# ── Span transclusion ────────────────────────────────────────────────────────


def test_link_with_line_span_resolves_text(tmp_path):
    # PORT-VERIFY: line-range spans resolve CLIENT-side from
    # ``entry.file_path`` (catalog_spans.resolve_span_text_for_entry), so the
    # engine must store and echo the absolute path registered here verbatim.
    cat = ActiveCatalog()
    owner = cat.register_owner("test", "repo", repo_hash="e2etest")
    src_file = tmp_path / "source.py"
    src_file.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")
    doc_a = cat.register(owner, "source.py", content_type="code", file_path=str(src_file))
    doc_b = cat.register(owner, "target.py", content_type="code", file_path="target.py")
    cat.link(doc_a, doc_b, "quotes", created_by="user", from_span="2-4", to_span="")
    assert cat.resolve_span_text(doc_a, "2-4") == "line2\nline3\nline4"


# ── chash span pipeline (RDR-053) ────────────────────────────────────────────


class TestChashSpan:
    def test_index_produces_chunk_text_hash(self, indexed_active):
        cat, local_t3 = indexed_active
        # nexus-i711w: raw ``SELECT physical_collection`` -> public reader.
        code_docs = [d for d in cat.by_content_type("code") if d.physical_collection]
        assert code_docs
        result = local_t3._client.get_collection(code_docs[0].physical_collection).get(
            limit=5, include=["documents", "metadatas"],
        )
        assert result["ids"]
        for chunk_id, doc_text, meta in zip(
            result["ids"], result["documents"], result["metadatas"],
        ):
            assert "chunk_text_hash" in meta and "content_hash" in meta
            expected = hashlib.sha256(doc_text.encode()).hexdigest()
            assert meta["chunk_text_hash"] == expected
            assert meta["chunk_text_hash"] != meta["content_hash"]
            # RDR-180 (nexus-jxizy.3): chunk natural ID is the FULL digest.
            assert chunk_id == expected


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
    # nexus-8g79.23: the original ``rows[0][0] >= 0`` was meaningless — a
    # SQLite COUNT(*) is always non-negative — so it was tightened to "the
    # plan-template SQL is queryable without error".
    #
    # nexus-aqbrk: that phrasing still reached for db.plans.conn, which the
    # service-backed store does not have. list_plans() is the public read on
    # both stores, and it makes the intent stronger rather than weaker: the
    # library answers, and every row it returns is well-formed enough to be
    # filtered on its tags.
    plans = db.plans.list_plans(limit=300)
    assert isinstance(plans, list)
    catalog_tagged = [p for p in plans if "catalog" in (p.get("tags") or "")]
    assert all(isinstance(p.get("id"), int) for p in catalog_tagged)


# ── 'formalizes' link type (RDR-057 P1-1a, nexus-807l) ─────────────────────


class TestFormalizesLinkType:
    """Verify catalog accepts 'formalizes' as a link type — no schema changes needed."""

    def test_formalizes_link_roundtrip(self):
        cat = ActiveCatalog()
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

    def test_formalizes_link_if_absent_idempotent(self):
        cat = ActiveCatalog()
        owner = cat.register_owner("test", "repo", repo_hash="abc123")
        doc_a = cat.register(owner, "scratch-note", content_type="knowledge")
        doc_b = cat.register(owner, "formal-entry", content_type="knowledge")

        assert cat.link_if_absent(doc_a, doc_b, "formalizes", created_by="test") is True
        assert cat.link_if_absent(doc_a, doc_b, "formalizes", created_by="test") is False
        assert len(cat.link_query(link_type="formalizes")) == 1
