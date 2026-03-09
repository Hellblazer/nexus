# SPDX-License-Identifier: AGPL-3.0-or-later
"""Snapshot regression tests for search quality.

Uses syrupy to capture search result ordering. When scoring weights,
normalization logic, or context prefix format changes, these tests
detect ranking regressions.

No API keys required — uses EphemeralClient + ONNX MiniLM-L6-v2.
"""
from __future__ import annotations

import chromadb
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

import pytest

from nexus.db.t3 import T3Database
from nexus.search_engine import search_cross_corpus
from nexus.scoring import apply_hybrid_scoring


# ── Corpus fixture ───────────────────────────────────────────────────────────

# Deterministic documents covering different topics for ranking validation.
_CODE_DOCS = [
    ("auth_login", "def login(username, password):\n    user = db.find_user(username)\n    if not user or not verify_hash(password, user.password_hash):\n        raise AuthError('invalid credentials')\n    return create_session(user)"),
    ("auth_logout", "def logout(session_id):\n    session = db.find_session(session_id)\n    if session:\n        db.delete_session(session_id)\n        return True\n    return False"),
    ("auth_register", "def register(username, email, password):\n    if db.find_user(username):\n        raise ValueError('username taken')\n    hashed = hash_password(password)\n    user = User(username=username, email=email, password_hash=hashed)\n    db.save(user)\n    return user"),
    ("db_connection", "class DatabasePool:\n    def __init__(self, dsn, min_conns=5, max_conns=20):\n        self.dsn = dsn\n        self.pool = create_pool(dsn, min_conns, max_conns)\n    def acquire(self):\n        return self.pool.get_connection()\n    def release(self, conn):\n        self.pool.return_connection(conn)"),
    ("db_migration", "def run_migrations(db, migrations_dir):\n    applied = db.get_applied_migrations()\n    pending = discover_pending(migrations_dir, applied)\n    for migration in sorted(pending):\n        db.execute(migration.sql)\n        db.record_migration(migration.version)"),
    ("http_handler", "class RequestHandler:\n    async def handle(self, request):\n        method = request.method\n        path = request.path\n        handler = self.router.match(method, path)\n        if not handler:\n            return Response(status=404)\n        return await handler(request)"),
    ("cache_layer", "class CacheLayer:\n    def __init__(self, backend='redis', ttl=300):\n        self.backend = connect_cache(backend)\n        self.ttl = ttl\n    def get(self, key):\n        return self.backend.get(key)\n    def set(self, key, value):\n        self.backend.set(key, value, ex=self.ttl)"),
    ("search_index", "def build_search_index(documents, embedding_model):\n    vectors = embedding_model.encode(documents)\n    index = create_faiss_index(vectors.shape[1])\n    index.add(vectors)\n    return index"),
    ("config_loader", "def load_config(path='config.yml'):\n    with open(path) as f:\n        raw = yaml.safe_load(f)\n    defaults = get_defaults()\n    return deep_merge(defaults, raw)"),
    ("logging_setup", "def setup_logging(level='INFO', json_output=False):\n    handler = logging.StreamHandler()\n    if json_output:\n        handler.setFormatter(JsonFormatter())\n    else:\n        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))\n    logging.root.addHandler(handler)\n    logging.root.setLevel(level)"),
]

_KNOWLEDGE_DOCS = [
    ("arch_decision_001", "We chose PostgreSQL over MySQL for the main database because it supports JSONB columns, better indexing, and has superior transaction isolation semantics."),
    ("arch_decision_002", "Redis is used as the caching layer with a 5-minute TTL. Memcached was rejected because Redis supports data structures beyond simple key-value pairs."),
    ("deployment_guide", "Deploy the application using Docker Compose. The stack includes nginx as reverse proxy, gunicorn for WSGI, and PostgreSQL for data storage."),
    ("api_design", "All API endpoints follow REST conventions. Authentication uses JWT tokens with a 1-hour expiry. Rate limiting is applied at 100 requests per minute per user."),
    ("security_policy", "All passwords are stored using bcrypt with a work factor of 12. SQL injection is prevented by parameterized queries. CSRF tokens are required for all state-changing operations."),
]


@pytest.fixture(scope="module")
def search_corpus() -> T3Database:
    """Session-scoped T3 with deterministic documents indexed.

    Using module scope ensures the corpus is built once and shared across
    all snapshot tests in this file, keeping tests fast.
    """
    client = chromadb.EphemeralClient()
    ef = DefaultEmbeddingFunction()
    db = T3Database(_client=client, _ef_override=ef)

    # Index code documents
    code_col = db.get_or_create_collection("code__snapshot")
    ids = [f"code-{title}" for title, _ in _CODE_DOCS]
    documents = [text for _, text in _CODE_DOCS]
    metadatas = [
        {
            "title": title,
            "source_path": f"/repo/{title}.py",
            "file_path": f"/repo/{title}.py",
            "line_start": 1,
            "line_end": text.count("\n") + 1,
            "frecency_score": 0.5,
        }
        for title, text in _CODE_DOCS
    ]
    code_col.add(ids=ids, documents=documents, metadatas=metadatas)

    # Index knowledge documents
    know_col = db.get_or_create_collection("knowledge__snapshot")
    ids = [f"know-{title}" for title, _ in _KNOWLEDGE_DOCS]
    documents = [text for _, text in _KNOWLEDGE_DOCS]
    metadatas = [
        {
            "title": title,
            "source_path": f"/docs/{title}.md",
            "file_path": f"/docs/{title}.md",
            "line_start": 1,
            "line_end": 1,
            "frecency_score": 0.5,
        }
        for title, text in _KNOWLEDGE_DOCS
    ]
    know_col.add(ids=ids, documents=documents, metadatas=metadatas)

    return db


# ── Snapshot queries ─────────────────────────────────────────────────────────

_QUERIES = [
    "user authentication login password",
    "database connection pool management",
    "caching strategy with TTL expiry",
    "search and indexing with embeddings",
    "deployment docker configuration",
    "security password hashing bcrypt",
]


def _format_results(results: list) -> str:
    """Format search results into a stable string for snapshot comparison."""
    lines = []
    for r in results:
        path = r.metadata.get("source_path", "?")
        score = f"{r.hybrid_score:.4f}" if r.hybrid_score else f"d={r.distance:.4f}"
        lines.append(f"{score}  {r.collection}  {path}")
    return "\n".join(lines)


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("query", _QUERIES)
def test_search_ranking_snapshot(query: str, search_corpus: T3Database, snapshot) -> None:
    """Search result ranking matches stored snapshot.

    Catches regressions when scoring weights, normalization, or context
    prefix logic changes. Update snapshots with: pytest --snapshot-update
    """
    raw = search_cross_corpus(
        query,
        ["code__snapshot", "knowledge__snapshot"],
        n_results=5,
        t3=search_corpus,
    )
    scored = apply_hybrid_scoring(raw, hybrid=True)
    output = _format_results(scored)
    assert output == snapshot


@pytest.mark.parametrize("query", _QUERIES[:3])
def test_search_single_corpus_snapshot(
    query: str, search_corpus: T3Database, snapshot,
) -> None:
    """Single-corpus search ranking matches stored snapshot."""
    raw = search_cross_corpus(
        query,
        ["code__snapshot"],
        n_results=5,
        t3=search_corpus,
    )
    scored = apply_hybrid_scoring(raw, hybrid=False)
    output = _format_results(scored)
    assert output == snapshot
