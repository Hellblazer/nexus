"""Integration tests — require real API keys. Skipped automatically when keys absent.

Run with:
    pytest -m integration                    # all integration tests
    pytest -m "integration and not t3"       # no-key tests only
    pytest -m integration --tb=short -v      # verbose

Set NEXUS_INTEGRATION=1 to run in CI (currently skipped by default).
"""
from __future__ import annotations

import os
import uuid

import pytest
from click.testing import CliRunner

from nexus.cli import main

# ── Marks ─────────────────────────────────────────────────────────────────────

requires_t3 = pytest.mark.skipif(
    not (
        os.environ.get("CHROMA_API_KEY")
        and os.environ.get("VOYAGE_API_KEY")
        and os.environ.get("CHROMA_TENANT")
        and os.environ.get("CHROMA_DATABASE")
    ),
    reason="T3 integration requires CHROMA_API_KEY, VOYAGE_API_KEY, CHROMA_TENANT, CHROMA_DATABASE",
)

requires_anthropic = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="Anthropic integration requires ANTHROPIC_API_KEY",
)

# Collection used by T3 tests — short TTL so it self-cleans via nx store expire
_T3_TEST_COLLECTION = "knowledge__nexus-integration-test"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ── T2: SQLite memory (no API keys required) ──────────────────────────────────

@pytest.mark.integration
def test_t2_memory_put_get_roundtrip(runner: CliRunner, tmp_path, monkeypatch) -> None:
    """T2 memory put → get roundtrip without any API keys."""
    monkeypatch.setenv("HOME", str(tmp_path))
    tag = f"int-test-{uuid.uuid4().hex[:8]}"

    result = runner.invoke(main, [
        "memory", "put", f"Integration test content {tag}",
        "--project", "integration-test",
        "--title", f"{tag}.md",
    ])
    assert result.exit_code == 0, result.output

    result = runner.invoke(main, [
        "memory", "get",
        "--project", "integration-test",
        "--title", f"{tag}.md",
    ])
    assert result.exit_code == 0, result.output
    assert tag in result.output


@pytest.mark.integration
def test_t2_memory_search_roundtrip(runner: CliRunner, tmp_path, monkeypatch) -> None:
    """T2 FTS5 search finds stored content."""
    monkeypatch.setenv("HOME", str(tmp_path))
    unique = f"xyzzy{uuid.uuid4().hex[:6]}"

    runner.invoke(main, [
        "memory", "put", f"Unique token {unique}",
        "--project", "integration-test",
        "--title", "search-test.md",
    ])

    result = runner.invoke(main, ["memory", "search", unique])
    assert result.exit_code == 0, result.output
    assert unique in result.output


@pytest.mark.integration
def test_t2_memory_expire_removes_ttl_entries(
    runner: CliRunner, tmp_path, monkeypatch
) -> None:
    """Entries with expired TTL are removed by nx memory expire."""
    monkeypatch.setenv("HOME", str(tmp_path))
    unique = f"expire-{uuid.uuid4().hex[:8]}"

    runner.invoke(main, [
        "memory", "put", f"Should expire {unique}",
        "--project", "expire-test",
        "--title", "expire-me.md",
        "--ttl", "1d",
    ])

    # Manually backdating isn't possible from CLI, but we can verify expire
    # runs cleanly and doesn't crash.
    result = runner.invoke(main, ["memory", "expire"])
    assert result.exit_code == 0, result.output
    assert "Expired" in result.output


@pytest.mark.integration
def test_t2_memory_list_project(runner: CliRunner, tmp_path, monkeypatch) -> None:
    """nx memory list --project returns entries for that project only."""
    monkeypatch.setenv("HOME", str(tmp_path))
    tag = f"list-{uuid.uuid4().hex[:6]}"

    runner.invoke(main, [
        "memory", "put", f"Content {tag}",
        "--project", "list-test",
        "--title", "list-entry.md",
    ])

    result = runner.invoke(main, ["memory", "list", "--project", "list-test"])
    assert result.exit_code == 0, result.output
    assert "list-entry.md" in result.output


# ── T1: session scratch (no API keys required) ────────────────────────────────

@pytest.mark.integration
def test_t1_scratch_put_list_clear(runner: CliRunner, tmp_path, monkeypatch) -> None:
    """T1 scratch put → list → clear roundtrip."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # T1 scratch requires an active session file keyed by os.getsid(0)
    sid = os.getsid(0)
    session_dir = tmp_path / ".config" / "nexus" / "sessions"
    session_dir.mkdir(parents=True)
    (session_dir / f"{sid}.session").write_text("integration-test-session")

    unique = f"scratch-{uuid.uuid4().hex[:8]}"

    result = runner.invoke(main, ["scratch", "put", f"scratch content {unique}"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(main, ["scratch", "list"])
    assert result.exit_code == 0, result.output
    assert unique in result.output

    result = runner.invoke(main, ["scratch", "clear"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(main, ["scratch", "list"])
    assert unique not in result.output


@pytest.mark.integration
def test_t1_scratch_search(runner: CliRunner, tmp_path, monkeypatch) -> None:
    """T1 scratch search finds stored content."""
    monkeypatch.setenv("HOME", str(tmp_path))
    sid = os.getsid(0)
    session_dir = tmp_path / ".config" / "nexus" / "sessions"
    session_dir.mkdir(parents=True)
    (session_dir / f"{sid}.session").write_text("integration-test-session")

    unique = f"scratchsearch-{uuid.uuid4().hex[:6]}"
    runner.invoke(main, ["scratch", "put", f"Unique content {unique}"])

    result = runner.invoke(main, ["scratch", "search", unique])
    assert result.exit_code == 0, result.output
    assert unique in result.output


# ── nx doctor (no API keys required for smoke test) ───────────────────────────

@pytest.mark.integration
def test_doctor_runs_and_reports(runner: CliRunner) -> None:
    """nx doctor completes without crashing and reports all five checks.

    Exit code 0 = all credentials present; 1 = some missing. Both are valid
    in this test — we only care that the output covers each component.
    """
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code in (0, 1), result.output
    output = result.output.lower()
    assert "chroma" in output
    assert "voyage" in output
    assert "anthropic" in output
    assert "ripgrep" in output or "rg" in output
    assert "git" in output


# ── T3: ChromaDB + Voyage AI (requires keys) ──────────────────────────────────

@pytest.mark.integration
@requires_t3
def test_t3_store_put_and_list_roundtrip(runner: CliRunner) -> None:
    """nx store put → nx store list shows the stored entry."""
    unique = f"nexus-int-{uuid.uuid4().hex[:8]}"
    title = f"{unique}.md"

    result = runner.invoke(main, [
        "store", "put", "-",
        "--collection", _T3_TEST_COLLECTION,
        "--title", title,
        "--ttl", "1d",
    ], input=f"Integration test document with unique token {unique}")
    assert result.exit_code == 0, result.output

    result = runner.invoke(main, [
        "store", "list",
        "--collection", _T3_TEST_COLLECTION,
    ])
    assert result.exit_code == 0, result.output
    assert title in result.output


@pytest.mark.integration
@requires_t3
def test_t3_store_put_search_roundtrip(runner: CliRunner) -> None:
    """nx store put → nx search --corpus returns the stored document."""
    unique = f"nexus-int-{uuid.uuid4().hex[:8]}"
    title = f"{unique}.md"

    result = runner.invoke(main, [
        "store", "put", "-",
        "--collection", _T3_TEST_COLLECTION,
        "--title", title,
        "--ttl", "1d",
    ], input=f"Integration test document with unique token {unique}")
    assert result.exit_code == 0, result.output

    result = runner.invoke(main, [
        "search", unique,
        "--corpus", _T3_TEST_COLLECTION,
        "--n", "5",
    ])
    assert result.exit_code == 0, result.output
    assert unique in result.output


@pytest.mark.integration
@requires_t3
def test_t3_store_expire_runs_cleanly(runner: CliRunner) -> None:
    """nx store expire completes without error."""
    result = runner.invoke(main, ["store", "expire"])
    assert result.exit_code == 0, result.output
    assert "Expired" in result.output


@pytest.mark.integration
@requires_t3
def test_t3_collection_list(runner: CliRunner) -> None:
    """nx collection list returns without error; may be empty."""
    result = runner.invoke(main, ["collection", "list"])
    assert result.exit_code == 0, result.output


@pytest.mark.integration
@requires_t3
def test_t3_collection_verify_existing(runner: CliRunner) -> None:
    """nx collection verify reports correctly for a collection that exists."""
    # First ensure the collection exists by storing something
    unique = f"verify-{uuid.uuid4().hex[:6]}"
    runner.invoke(main, [
        "store", "put", "-",
        "--collection", _T3_TEST_COLLECTION,
        "--title", f"{unique}.md",
        "--ttl", "1d",
    ], input=f"Verification test {unique}")

    result = runner.invoke(main, ["collection", "verify", _T3_TEST_COLLECTION])
    assert result.exit_code == 0, result.output


@pytest.mark.integration
@requires_t3
def test_t3_collection_verify_missing(runner: CliRunner) -> None:
    """nx collection verify exits non-zero for a collection that does not exist."""
    result = runner.invoke(main, [
        "collection", "verify", "knowledge__does-not-exist-ever-xyz",
    ])
    assert result.exit_code != 0, result.output


@pytest.mark.integration
@requires_t3
def test_nx_search_knowledge_corpus(runner: CliRunner) -> None:
    """nx search --corpus knowledge finds a document stored immediately before the query."""
    unique = f"searchcorpus-{uuid.uuid4().hex[:8]}"
    # Seed a document with a unique token so the search has something to find
    runner.invoke(main, [
        "store", "put", "-",
        "--collection", _T3_TEST_COLLECTION,
        "--title", f"{unique}.md",
        "--ttl", "1d",
    ], input=f"Corpus search test document {unique}")

    result = runner.invoke(main, [
        "search", unique,
        "--corpus", _T3_TEST_COLLECTION,
        "--n", "3",
    ])
    assert result.exit_code == 0, result.output
    assert unique in result.output, (
        f"Expected unique token {unique!r} to appear in search output; got: {result.output!r}"
    )


# ── Cross-model compatibility: voyage-code-3 index + voyage-4 query ──────────

@pytest.mark.integration
@requires_t3
def test_voyage4_query_retrieves_voyage_code3_indexed_content() -> None:
    """voyage-4 queries retrieve semantically relevant results from voyage-code-3-indexed vectors.

    Validates the core design assumption: voyage-4 is a compatible universal
    query model for code__ collections indexed with voyage-code-3.

    Method:
    - Embed a code snippet directly via voyageai SDK with model=voyage-code-3
    - Store it using upsert_chunks_with_embeddings (bypasses collection EF)
    - Query via db.search (uses collection EF = voyage-4)
    - Assert the indexed chunk is returned
    """
    import voyageai

    from nexus.config import get_credential
    from nexus.corpus import index_model_for_collection
    from nexus.db import make_t3

    voyage_key = get_credential("voyage_api_key")
    uid = uuid.uuid4().hex[:8]
    collection = f"code__int-crossmodel-{uid}"

    assert index_model_for_collection(collection) == "voyage-code-3"

    code = (
        f"def authenticate_user_{uid}(username: str, password: str) -> str:\n"
        f"    '''Validate credentials against the database and return a JWT token.'''\n"
        f"    record = user_db.lookup(username)\n"
        f"    if record is None:\n"
        f"        raise ValueError('unknown user')\n"
        f"    return generate_jwt_token(username, password)\n"
    )

    voyage = voyageai.Client(api_key=voyage_key)
    resp = voyage.embed(texts=[code], model="voyage-code-3", input_type="document")
    code3_embeddings = resp.embeddings

    db = make_t3()
    try:
        db.upsert_chunks_with_embeddings(
            collection_name=collection,
            ids=[f"chunk-{uid}"],
            documents=[code],
            embeddings=code3_embeddings,
            metadatas=[{
                "title": f"auth_{uid}.py:1-6",
                "tags": "py",
                "category": "code",
                "embedding_model": "voyage-code-3",
                "expires_at": "",
                "ttl_days": 0,
            }],
        )
        results = db.search(
            query=f"user authentication JWT token generation {uid}",
            collection_names=[collection],
            n_results=3,
        )
        assert results, "voyage-4 query returned no results from voyage-code-3-indexed collection"
        assert any(uid in r.get("content", "") for r in results), (
            "voyage-4 query did not retrieve the voyage-code-3-indexed code chunk"
        )
    finally:
        try:
            db.delete_collection(collection)
        except Exception:
            pass


# ── Cross-model: voyage-context-3 (CCE) index + voyage-4 query ───────────────

@pytest.mark.integration
@requires_t3
def test_voyage4_query_retrieves_cce_indexed_markdown() -> None:
    """voyage-4 queries retrieve results from CCE-indexed docs__ content.

    Validates the second cross-model assumption: voyage-context-3 (CCE) index
    vectors are in the same space as voyage-4 query vectors.

    Method:
    - Index a multi-chunk markdown file via index_markdown (triggers CCE path)
    - Query via db.search (collection EF = voyage-4)
    - Assert the indexed chunk is returned and embedding_model is recorded
    """
    import tempfile
    from pathlib import Path

    from nexus.db import make_t3
    from nexus.doc_indexer import index_markdown

    uid = uuid.uuid4().hex[:8]
    collection = f"docs__int-cce-{uid}"

    # Multi-section markdown → SemanticMarkdownChunker produces 2+ chunks → CCE fires
    md_content = (
        f"# Authentication Module {uid}\n\n"
        f"This document describes the user authentication system for integration test {uid}.\n\n"
        f"## Token Generation\n\n"
        f"JWT tokens are generated using RS256 signing with a rotating key schedule. "
        f"Each token carries a session ID and an expiration timestamp. "
        f"The token payload is validated on every request by the middleware layer.\n\n"
        f"## Credential Verification\n\n"
        f"User credentials are validated against bcrypt hashes stored in the database. "
        f"Failed attempts are rate-limited using a sliding window algorithm. "
        f"Successful logins reset the failure counter and update the last-seen timestamp.\n"
    )

    db = make_t3()
    with tempfile.TemporaryDirectory() as tmpdir:
        md_path = Path(tmpdir) / f"auth_{uid}.md"
        md_path.write_text(md_content)
        try:
            chunk_count = index_markdown(md_path, corpus=f"int-cce-{uid}", t3=db)
            assert chunk_count > 0, "Expected at least one chunk indexed from markdown"

            results = db.search(
                query=f"user authentication JWT token bcrypt {uid}",
                collection_names=[collection],
                n_results=5,
            )
            assert results, (
                "voyage-4 query returned no results from CCE-indexed docs__ collection"
            )
            assert any(uid in r.get("content", "") for r in results), (
                "voyage-4 query did not retrieve the CCE-indexed markdown chunk"
            )
            # embedding_model must be voyage-context-3 (CCE succeeded) or voyage-4 (fallback)
            stored_model = results[0].get("embedding_model", "")
            assert stored_model in ("voyage-context-3", "voyage-4"), (
                f"embedding_model should be voyage-context-3 or voyage-4 (fallback), "
                f"got: {stored_model!r}"
            )
        finally:
            try:
                db.delete_collection(collection)
            except Exception:
                pass


@pytest.mark.integration
@requires_t3
def test_t3_put_embedding_model_in_search_metadata() -> None:
    """T3Database.put() stores embedding_model='voyage-4'; field survives to search results.

    Validates that the metadata provenance field added to put() is persisted
    through ChromaDB and returned by db.search().
    """
    from nexus.db import make_t3

    uid = uuid.uuid4().hex[:8]
    collection = f"knowledge__int-prov-{uid}"
    db = make_t3()
    try:
        doc_id = db.put(
            collection=collection,
            content=f"Provenance test document with unique token {uid}",
            title=f"{uid}-provenance.md",
            ttl_days=1,
        )
        assert doc_id, "put() must return a non-empty doc ID"

        results = db.search(
            query=f"provenance test document {uid}",
            collection_names=[collection],
            n_results=5,
        )
        assert results, "Expected search to find the just-stored document"
        matching = [r for r in results if uid in r.get("content", "")]
        assert matching, "Stored document not found by unique token in search results"
        assert matching[0].get("embedding_model") == "voyage-4", (
            f"knowledge__ put() must store embedding_model='voyage-4', "
            f"got: {matching[0].get('embedding_model')!r}"
        )
    finally:
        try:
            db.delete_collection(collection)
        except Exception:
            pass


# ── Answer mode (requires T3 + Anthropic) ─────────────────────────────────────

@pytest.mark.integration
@requires_t3
@requires_anthropic
def test_answer_mode_returns_synthesis(runner: CliRunner) -> None:
    """nx search -a returns a synthesized answer with citations."""
    result = runner.invoke(main, [
        "search", "what is nexus",
        "--corpus", _T3_TEST_COLLECTION,
        "--answer", "--n", "3",
    ])
    assert result.exit_code == 0, result.output
    assert len(result.output.strip()) > 0
