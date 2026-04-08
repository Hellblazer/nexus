"""Integration tests — require real API keys. Skipped automatically when keys absent.

Run with:
    pytest -m integration
    pytest -m "integration and not t3"
    pytest -m integration --tb=short -v
"""
from __future__ import annotations

import os
import uuid

import pytest
from click.testing import CliRunner

from nexus.cli import main


def _t3_reachable() -> bool:
    if not all([
        os.environ.get("CHROMA_API_KEY"),
        os.environ.get("VOYAGE_API_KEY"),
        os.environ.get("CHROMA_TENANT"),
        os.environ.get("CHROMA_DATABASE"),
    ]):
        return False
    try:
        from nexus.db import make_t3
        make_t3()
        return True
    except Exception:
        return False


_T3_AVAILABLE: bool = _t3_reachable()

requires_t3 = pytest.mark.skipif(
    not _T3_AVAILABLE,
    reason="T3 integration requires CHROMA_API_KEY, VOYAGE_API_KEY, CHROMA_TENANT, CHROMA_DATABASE",
)

_T3_COL = "knowledge__nexus-integration-test"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def scratch_session(isolated_home):
    """Set up T1 scratch with shared session ID."""
    from nexus.session import write_claude_session_id
    write_claude_session_id("integration-test-session")


def _uid(prefix: str = "int") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _store_put(runner, uid, collection=_T3_COL):
    return runner.invoke(main, [
        "store", "put", "-", "--collection", collection,
        "--title", f"{uid}.md", "--ttl", "1d",
    ], input=f"Integration test document with unique token {uid}")


# ── T2: SQLite memory ───────────────────────────────────────────────────────

@pytest.mark.integration
def test_t2_memory_put_get_roundtrip(runner, isolated_home):
    tag = _uid("mem")
    runner.invoke(main, ["memory", "put", f"Integration test content {tag}",
                         "--project", "integration-test", "--title", f"{tag}.md"])
    result = runner.invoke(main, ["memory", "get", "--project", "integration-test",
                                  "--title", f"{tag}.md"])
    assert result.exit_code == 0 and tag in result.output


@pytest.mark.integration
def test_t2_memory_search_roundtrip(runner, isolated_home):
    unique = _uid("fts")
    runner.invoke(main, ["memory", "put", f"Unique token {unique}",
                         "--project", "integration-test", "--title", "search-test.md"])
    result = runner.invoke(main, ["memory", "search", unique])
    assert result.exit_code == 0 and unique in result.output


@pytest.mark.integration
def test_t2_memory_expire_runs(runner, isolated_home):
    unique = _uid("expire")
    runner.invoke(main, ["memory", "put", f"Should expire {unique}",
                         "--project", "expire-test", "--title", "expire-me.md", "--ttl", "1d"])
    result = runner.invoke(main, ["memory", "expire"])
    assert result.exit_code == 0 and "Expired" in result.output


@pytest.mark.integration
def test_t2_memory_list_project(runner, isolated_home):
    runner.invoke(main, ["memory", "put", "Content", "--project", "list-test",
                         "--title", "list-entry.md"])
    result = runner.invoke(main, ["memory", "list", "--project", "list-test"])
    assert result.exit_code == 0 and "list-entry.md" in result.output


# ── T1: session scratch ──────────────────────────────────────────────────────

@pytest.mark.integration
def test_t1_scratch_put_list_clear(runner, scratch_session):
    unique = _uid("scratch")
    result = runner.invoke(main, ["scratch", "put", f"scratch content {unique}"])
    assert result.exit_code == 0

    result = runner.invoke(main, ["scratch", "list"])
    assert result.exit_code == 0 and unique in result.output

    runner.invoke(main, ["scratch", "clear"])
    result = runner.invoke(main, ["scratch", "list"])
    assert unique not in result.output


@pytest.mark.integration
def test_t1_scratch_search(runner, scratch_session):
    unique = _uid("ssearch")
    runner.invoke(main, ["scratch", "put", f"Unique content {unique}"])
    result = runner.invoke(main, ["scratch", "search", unique])
    assert result.exit_code == 0 and unique in result.output


# ── nx doctor ────────────────────────────────────────────────────────────────

@pytest.mark.integration
def test_doctor_runs_and_reports(runner):
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code in (0, 1)
    output = result.output.lower()
    assert "t3 mode" in output
    assert "ripgrep" in output or "rg" in output
    assert "git" in output


# ── T3: ChromaDB + Voyage AI ─────────────────────────────────────────────────

@pytest.mark.integration
@requires_t3
def test_t3_store_put_and_list_roundtrip(runner):
    uid = _uid("store")
    result = _store_put(runner, uid)
    assert result.exit_code == 0
    doc_id = result.output.split("Stored: ")[1].split()[0]
    result = runner.invoke(main, ["store", "get", doc_id, "--collection", _T3_COL])
    assert result.exit_code == 0 and uid in result.output


@pytest.mark.integration
@requires_t3
def test_t3_store_put_search_roundtrip(runner):
    uid = _uid("search")
    _store_put(runner, uid)
    result = runner.invoke(main, [
        "search", uid, "--corpus", _T3_COL, "--n", "5",
        "--where", f"title={uid}.md",
    ])
    assert result.exit_code == 0 and uid in result.output


@pytest.mark.integration
@requires_t3
def test_t3_store_expire_runs_cleanly(runner):
    result = runner.invoke(main, ["store", "expire"])
    assert result.exit_code == 0 and "Expired" in result.output


@pytest.mark.integration
@requires_t3
def test_t3_collection_list(runner):
    result = runner.invoke(main, ["collection", "list"])
    assert result.exit_code == 0


@pytest.mark.integration
@requires_t3
def test_t3_collection_verify_existing(runner):
    uid = _uid("verify")
    _store_put(runner, uid)
    result = runner.invoke(main, ["collection", "verify", _T3_COL])
    assert result.exit_code == 0


@pytest.mark.integration
@requires_t3
def test_t3_collection_verify_missing(runner):
    result = runner.invoke(main, ["collection", "verify", "knowledge__does-not-exist-ever-xyz"])
    assert result.exit_code != 0


@pytest.mark.integration
@requires_t3
def test_nx_search_knowledge_corpus(runner):
    uid = _uid("searchcorpus")
    _store_put(runner, uid)
    # Search with enough results and filter by title to avoid noise from prior runs
    result = runner.invoke(main, [
        "search", uid, "--corpus", _T3_COL, "--n", "10",
        "--where", f"title={uid}.md",
    ])
    assert result.exit_code == 0 and uid in result.output


# ── Code search: voyage-code-3 ──────────────────────────────────────────────

@pytest.mark.integration
@requires_t3
def test_voyage_code3_index_and_query():
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
        f"    '''Validate credentials and return JWT.'''\n"
        f"    record = user_db.lookup(username)\n"
        f"    if record is None:\n"
        f"        raise ValueError('unknown user')\n"
        f"    return generate_jwt_token(username, password)\n"
    )
    voyage = voyageai.Client(api_key=voyage_key)
    embeddings = voyage.embed(texts=[code], model="voyage-code-3", input_type="document").embeddings

    db = make_t3()
    try:
        db.upsert_chunks_with_embeddings(
            collection_name=collection, ids=[f"chunk-{uid}"], documents=[code],
            embeddings=embeddings,
            metadatas=[{"title": f"auth_{uid}.py:1-6", "tags": "py", "category": "code",
                        "embedding_model": "voyage-code-3", "expires_at": "", "ttl_days": 0}],
        )
        results = db.search(query=f"user authentication JWT {uid}",
                            collection_names=[collection], n_results=3)
        assert results and any(uid in r.get("content", "") for r in results)
    finally:
        try:
            db.delete_collection(collection)
        except Exception:
            pass


# ── CCE: voyage-context-3 ───────────────────────────────────────────────────

@pytest.mark.integration
@requires_t3
def test_cce_query_retrieves_cce_indexed_markdown():
    import tempfile
    from pathlib import Path
    from nexus.db import make_t3
    from nexus.doc_indexer import index_markdown

    uid = uuid.uuid4().hex[:8]
    collection = f"docs__int-cce-{uid}"
    md_content = (
        f"# Authentication Module {uid}\n\n"
        f"This document describes the user authentication system for test {uid}.\n\n"
        f"## Token Generation\n\n"
        f"JWT tokens use RS256 signing with rotating keys. "
        f"Each token carries a session ID and expiration. "
        f"Validated on every request by middleware.\n\n"
        f"## Credential Verification\n\n"
        f"Credentials validated against bcrypt hashes. "
        f"Failed attempts rate-limited via sliding window. "
        f"Success resets failure counter and updates last-seen.\n"
    )
    db = make_t3()
    with tempfile.TemporaryDirectory() as tmpdir:
        md_path = Path(tmpdir) / f"auth_{uid}.md"
        md_path.write_text(md_content)
        try:
            assert index_markdown(md_path, corpus=f"int-cce-{uid}", t3=db) > 0
            results = db.search(query=f"authentication JWT bcrypt {uid}",
                                collection_names=[collection], n_results=5)
            assert results and any(uid in r.get("content", "") for r in results)
            assert results[0].get("embedding_model") == "voyage-context-3"
        finally:
            try:
                db.delete_collection(collection)
            except Exception:
                pass


@pytest.mark.integration
@requires_t3
def test_t3_put_embedding_model_in_search_metadata():
    from nexus.db import make_t3

    uid = uuid.uuid4().hex[:8]
    collection = f"knowledge__int-prov-{uid}"
    db = make_t3()
    try:
        doc_id = db.put(collection=collection,
                        content=f"Provenance test document {uid}",
                        title=f"{uid}-provenance.md", ttl_days=1)
        assert doc_id
        results = db.search(query=f"provenance test {uid}",
                            collection_names=[collection], n_results=5)
        assert results
        matching = [r for r in results if uid in r.get("content", "")]
        assert matching and matching[0].get("embedding_model") == "voyage-context-3"
    finally:
        try:
            db.delete_collection(collection)
        except Exception:
            pass


# ── Migration ────────────────────────────────────────────────────────────────

@pytest.mark.integration
@requires_t3
def test_migrate_t3_ensure_databases_is_idempotent(runner):
    import os
    from nexus.commands._provision import ensure_databases, _cloud_admin_client

    api_key = os.environ.get("CHROMA_API_KEY", "")
    database = os.environ.get("CHROMA_DATABASE", "")
    admin = _cloud_admin_client(api_key)
    first = ensure_databases(admin, base=database)
    second = ensure_databases(admin, base=database)
    assert set(first.keys()) == {database}
    assert all(not v for v in second.values())
