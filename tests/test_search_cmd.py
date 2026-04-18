# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from unittest.mock import MagicMock, patch

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from click import BadParameter
from click.testing import CliRunner

from nexus.cli import main
from nexus.commands.search_cmd import _parse_where
from nexus.db.t3 import T3Database
from nexus.scoring import apply_hybrid_scoring
from nexus.search_engine import search_cross_corpus
from nexus.types import SearchResult

# ── Helpers & fixtures ──────────────────────────────────────────────────────


def _make_result(
    id: str,
    content: str,
    collection: str = "knowledge__test",
    distance: float = 0.1,
    metadata: dict | None = None,
) -> SearchResult:
    return SearchResult(
        id=id, content=content, distance=distance,
        collection=collection, metadata=metadata or {},
    )


def _mock_t3(collections: list[str] | None = None) -> MagicMock:
    mock = MagicMock()
    col_names = collections or ["knowledge__test"]
    mock.list_collections.return_value = [{"name": n} for n in col_names]
    return mock


_CLOUD_ENV = {
    "CHROMA_API_KEY": "k", "VOYAGE_API_KEY": "v",
    "CHROMA_TENANT": "t", "CHROMA_DATABASE": "d",
}
_LOAD_CFG = {"embeddings": {"rerankerModel": "rerank-2.5"}}


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def cloud_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _CLOUD_ENV.items():
        monkeypatch.setenv(k, v)


def _capture_ctx(collections: list[str] | None = None):
    mock = _mock_t3(collections)
    captured: list[dict | None] = []

    def fake(query, cols, n_results, t3, where=None, **kwargs):
        captured.append(where)
        return []

    return mock, captured, fake


@pytest.fixture
def search_ctx(cloud_env):
    mock, captured, fake = _capture_ctx()
    with patch("nexus.commands.search_cmd._t3", return_value=mock), \
         patch("nexus.commands.search_cmd.search_cross_corpus", side_effect=fake), \
         patch("nexus.commands.search_cmd.load_config", return_value=_LOAD_CFG):
        yield mock, captured


@pytest.fixture
def code_search_ctx(cloud_env):
    mock, captured, fake = _capture_ctx(["code__myrepo"])
    with patch("nexus.commands.search_cmd._t3", return_value=mock), \
         patch("nexus.commands.search_cmd.search_cross_corpus", side_effect=fake), \
         patch("nexus.commands.search_cmd.load_config", return_value=_LOAD_CFG):
        yield mock, captured


# ── CLI flag tests ──────────────────────────────────────────────────────────


def test_corpus_short_form_C_is_removed(runner: CliRunner, cloud_env) -> None:
    mock_t3 = _mock_t3()
    mock_t3.search.return_value = []
    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3):
        result = runner.invoke(main, ["search", "query", "-C", "knowledge"])
    assert result.exit_code != 0


def test_corpus_long_form_still_works(runner: CliRunner, cloud_env) -> None:
    mock_t3 = _mock_t3(["knowledge__test"])
    mock_t3.search.return_value = []
    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3), \
         patch("nexus.commands.search_cmd.search_cross_corpus", return_value=[]):
        result = runner.invoke(main, ["search", "query", "--corpus", "knowledge"])
    assert "Error" not in result.output or result.exit_code == 0


def test_m_flag_limits_results(runner: CliRunner, cloud_env) -> None:
    results_pool = [
        _make_result(f"r{i}", f"line {i}", distance=float(i) * 0.1)
        for i in range(10)
    ]
    mock_t3 = _mock_t3()
    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3), \
         patch("nexus.commands.search_cmd.search_cross_corpus", return_value=results_pool), \
         patch("nexus.commands.search_cmd.load_config", return_value=_LOAD_CFG):
        result = runner.invoke(
            main, ["search", "query", "--no-rerank", "-m", "3", "--corpus", "knowledge"],
        )
    assert result.exit_code == 0, result.output
    output_lines = [ln for ln in result.output.splitlines() if ln.strip()]
    assert len(output_lines) <= 3


def test_reverse_flag_reverses_output_order(runner: CliRunner, cloud_env) -> None:
    results_pool = [
        _make_result("first", "alpha content", distance=0.1,
                     metadata={"source_path": "alpha.py", "line_start": 1}),
        _make_result("second", "beta content", distance=0.2,
                     metadata={"source_path": "beta.py", "line_start": 1}),
    ]
    mock_t3 = _mock_t3()
    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3), \
         patch("nexus.commands.search_cmd.search_cross_corpus", return_value=results_pool), \
         patch("nexus.commands.search_cmd.load_config", return_value=_LOAD_CFG):
        normal = runner.invoke(
            main, ["search", "query", "--no-rerank", "--corpus", "knowledge", "--no-color"],
        )
        reversed_ = runner.invoke(
            main, ["search", "query", "--no-rerank", "--corpus", "knowledge", "--no-color", "--reverse"],
        )
    assert normal.exit_code == 0
    assert reversed_.exit_code == 0
    normal_lines = [ln for ln in normal.output.splitlines() if ln.strip()]
    reversed_lines = [ln for ln in reversed_.output.splitlines() if ln.strip()]
    assert normal_lines != reversed_lines
    assert normal_lines == list(reversed(reversed_lines))


# ── --where metadata filter ─────────────────────────────────────────────────


def test_where_single_filter_passed_to_search(runner: CliRunner, search_ctx) -> None:
    _, captured = search_ctx
    runner.invoke(main, ["search", "query", "--corpus", "knowledge", "--where", "lang=python"])
    assert captured[0] == {"lang": "python"}


def test_where_multiple_filters_anded(runner: CliRunner, search_ctx) -> None:
    _, captured = search_ctx
    runner.invoke(main, [
        "search", "query", "--corpus", "knowledge",
        "--where", "store_type=knowledge", "--where", "status=completed",
    ])
    assert captured[0] == {"store_type": "knowledge", "status": "completed"}


def test_where_no_flag_passes_none(runner: CliRunner, search_ctx) -> None:
    _, captured = search_ctx
    runner.invoke(main, ["search", "query", "--corpus", "knowledge"])
    assert captured[0] is None


# ── -A / -B / -C context lines ──────────────────────────────────────────────


def test_context_A_accepted(runner: CliRunner, cloud_env) -> None:
    content = "line1\nline2\nline3\nline4\nline5"
    results_pool = [_make_result("r1", content, metadata={"source_path": "foo.py", "line_start": 10})]
    mock_t3 = _mock_t3()
    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3), \
         patch("nexus.commands.search_cmd.search_cross_corpus", return_value=results_pool), \
         patch("nexus.commands.search_cmd.load_config", return_value=_LOAD_CFG):
        result = runner.invoke(
            main, ["search", "query", "--no-rerank", "--corpus", "knowledge", "--no-color", "-A", "3"],
        )
    assert result.exit_code == 0, result.output


def test_context_C_integer_accepted(runner: CliRunner, cloud_env) -> None:
    mock_t3 = _mock_t3()
    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3), \
         patch("nexus.commands.search_cmd.search_cross_corpus", return_value=[]), \
         patch("nexus.commands.search_cmd.load_config", return_value=_LOAD_CFG):
        result = runner.invoke(main, ["search", "query", "--corpus", "knowledge", "-C", "5"])
    assert result.exit_code == 0, result.output


def test_context_C_requires_integer(runner: CliRunner, cloud_env) -> None:
    mock_t3 = _mock_t3()
    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3):
        result = runner.invoke(main, ["search", "query", "--corpus", "knowledge", "-C", "notanumber"])
    assert result.exit_code != 0


# ── format_plain context-aware output ───────────────────────────────────────


def test_format_plain_with_context_shows_correct_lines() -> None:
    from nexus.formatters import format_plain_with_context
    content = "\n".join(f"line{i}" for i in range(10))
    r = SearchResult(id="x", content=content, distance=0.1, collection="c",
                     metadata={"source_path": "file.py", "line_start": 0})
    lines = format_plain_with_context([r], lines_after=3)
    content_lines = [ln for ln in lines if ln.strip()]
    assert len(content_lines) <= 4


def test_format_plain_with_context_zero_equals_format_plain() -> None:
    from nexus.formatters import format_plain, format_plain_with_context
    content = "alpha\nbeta\ngamma"
    r = SearchResult(id="x", content=content, distance=0.1, collection="c",
                     metadata={"source_path": "file.py", "line_start": 5})
    assert format_plain([r]) == format_plain_with_context([r], lines_after=0)


# ── --hybrid flag triggers ripgrep ──────────────────────────────────────────


def test_hybrid_flag_triggers_ripgrep(runner: CliRunner, cloud_env, tmp_path) -> None:
    cache_file = tmp_path / "myrepo-abcd1234.cache"
    cache_file.write_text("/repo/main.py:1:hello world\n")
    mock_t3 = _mock_t3(["code__myrepo-abcd1234"])
    rg_calls: list[int] = []

    def fake_rg(query, cache_path, *, n_results=50, fixed_strings=True, timeout=10):
        rg_calls.append(1)
        return []

    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3), \
         patch("nexus.commands.search_cmd._CONFIG_DIR", tmp_path), \
         patch("nexus.commands.search_cmd.search_cross_corpus", return_value=[]), \
         patch("nexus.commands.search_cmd.load_config", return_value=_LOAD_CFG), \
         patch("nexus.commands.search_cmd.search_ripgrep", side_effect=fake_rg):
        result = runner.invoke(main, ["search", "query", "--hybrid", "--corpus", "code", "--no-rerank"])
    assert result.exit_code == 0, result.output
    assert len(rg_calls) >= 1


def test_hybrid_results_include_rg_hits(runner: CliRunner, cloud_env, tmp_path) -> None:
    cache_file = tmp_path / "myrepo-abcd1234.cache"
    cache_file.write_text("/repo/main.py:1:hello world\n")
    mock_t3 = _mock_t3(["code__myrepo-abcd1234"])
    rg_hit = {
        "file_path": "/repo/main.py", "line_number": 1,
        "line_content": "hello world", "frecency_score": 0.5,
    }
    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3), \
         patch("nexus.commands.search_cmd._CONFIG_DIR", tmp_path), \
         patch("nexus.commands.search_cmd.search_cross_corpus", return_value=[]), \
         patch("nexus.commands.search_cmd.load_config", return_value=_LOAD_CFG), \
         patch("nexus.commands.search_cmd.search_ripgrep", return_value=[rg_hit]):
        result = runner.invoke(main, ["search", "query", "--hybrid", "--corpus", "code", "--no-rerank"])
    assert result.exit_code == 0, result.output
    assert "/repo/main.py" in result.output


def test_hybrid_without_cache_files_still_works(runner: CliRunner, cloud_env, tmp_path) -> None:
    semantic_result = _make_result(
        "sem1", "semantic content", collection="code__myrepo",
        metadata={"source_path": "file.py", "line_start": 1},
    )
    mock_t3 = _mock_t3(["code__myrepo"])
    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3), \
         patch("nexus.commands.search_cmd._CONFIG_DIR", tmp_path), \
         patch("nexus.commands.search_cmd.search_cross_corpus", return_value=[semantic_result]), \
         patch("nexus.commands.search_cmd.load_config", return_value=_LOAD_CFG):
        result = runner.invoke(main, ["search", "query", "--hybrid", "--corpus", "code", "--no-rerank"])
    assert result.exit_code == 0, result.output
    assert "No results" not in result.output


# ── _parse_where ────────────────────────────────────────────────────────────


def test_parse_where_empty_tuple_returns_none() -> None:
    assert _parse_where(()) is None


@pytest.mark.parametrize("input_,expected", [
    (("lang=python",), {"lang": "python"}),
    (("key=a=b=c",), {"key": "a=b=c"}),
    (("lang=python", "type=code"), {"lang": "python", "type": "code"}),
    (("corpus=42",), {"corpus": "42"}),  # unknown field stays string
])
def test_parse_where_equality(input_: tuple[str, ...], expected: dict) -> None:
    assert _parse_where(input_) == expected


@pytest.mark.parametrize("input_,match", [
    (("key=",), "empty value"),
    (("=value",), ""),
    (("no-equals-here",), "KEY=VALUE"),
])
def test_parse_where_raises(input_: tuple[str, ...], match: str) -> None:
    with pytest.raises(BadParameter, match=match):
        _parse_where(input_)


@pytest.mark.parametrize("input_,expected", [
    (("bib_year>=2024",), {"bib_year": {"$gte": 2024}}),
    (("bib_citation_count<=100",), {"bib_citation_count": {"$lte": 100}}),
    (("page_count>10",), {"page_count": {"$gt": 10}}),
    (("chunk_index<5",), {"chunk_index": {"$lt": 5}}),
    (("chunk_type!=text",), {"chunk_type": {"$ne": "text"}}),
])
def test_parse_where_operators(input_: tuple[str, ...], expected: dict) -> None:
    assert _parse_where(input_) == expected


def test_parse_where_numeric_coercion_int() -> None:
    result = _parse_where(("bib_year=2024",))
    assert result == {"bib_year": 2024}
    assert isinstance(result["bib_year"], int)


def test_parse_where_mixed_operators_uses_and() -> None:
    assert _parse_where(("bib_year>=2020", "bib_year<=2024")) == {
        "$and": [{"bib_year": {"$gte": 2020}}, {"bib_year": {"$lte": 2024}}],
    }


def test_parse_where_equality_plus_operator_uses_and() -> None:
    assert _parse_where(("corpus=knowledge", "bib_year>=2020")) == {
        "$and": [{"corpus": "knowledge"}, {"bib_year": {"$gte": 2020}}],
    }


def test_parse_where_value_containing_gt_not_mismatched() -> None:
    assert _parse_where(("source_path=a>b/file.py",)) == {"source_path": "a>b/file.py"}


@pytest.mark.parametrize("input_", [
    ("bib_year>=notanumber",),
    ("bib_year=notanumber",),
])
def test_parse_where_numeric_field_non_numeric_raises(input_: tuple[str, ...]) -> None:
    with pytest.raises(BadParameter, match="requires a numeric value"):
        _parse_where(input_)


def test_parse_where_empty_value_with_operator_raises() -> None:
    with pytest.raises(BadParameter, match="empty value"):
        _parse_where(("bib_year>=",))


# ── --max-file-chunks ───────────────────────────────────────────────────────


def test_max_file_chunks_builds_chunk_count_filter(runner: CliRunner, code_search_ctx) -> None:
    _, captured = code_search_ctx
    runner.invoke(main, ["search", "query", "--corpus", "code", "--max-file-chunks", "17"])
    assert captured[0] == {"chunk_count": {"$lte": 17}}


def test_max_file_chunks_and_where_merged_with_and(runner: CliRunner, code_search_ctx) -> None:
    _, captured = code_search_ctx
    runner.invoke(main, [
        "search", "query", "--corpus", "code",
        "--max-file-chunks", "17", "--where", "lang=python",
    ])
    w = captured[0]
    assert "$and" in w
    assert {"chunk_count": {"$lte": 17}} in w["$and"]
    assert {"lang": "python"} in w["$and"]


# ── corpus warning ──────────────────────────────────────────────────────────


def test_search_warns_when_corpus_term_unmatched(runner: CliRunner) -> None:
    mock_t3 = _mock_t3(["knowledge__test"])
    mock_t3.search.return_value = []
    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3), \
         patch("nexus.commands.search_cmd.search_cross_corpus", return_value=[]):
        result = runner.invoke(main, [
            "search", "foo", "--corpus", "knowledge", "--corpus", "badcorpus",
        ])
    assert "badcorpus" in result.output


# ── Search ranking snapshots ────────────────────────────────────────────────

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

_QUERIES = [
    "user authentication login password",
    "database connection pool management",
    "caching strategy with TTL expiry",
    "search and indexing with embeddings",
    "deployment docker configuration",
    "security password hashing bcrypt",
]


@pytest.fixture(scope="module")
def search_corpus() -> T3Database:
    client = chromadb.EphemeralClient()
    ef = DefaultEmbeddingFunction()
    db = T3Database(_client=client, _ef_override=ef)

    code_col = db.get_or_create_collection("code__snapshot")
    code_col.add(
        ids=[f"code-{t}" for t, _ in _CODE_DOCS],
        documents=[txt for _, txt in _CODE_DOCS],
        metadatas=[{
            "title": t, "source_path": f"/repo/{t}.py", "file_path": f"/repo/{t}.py",
            "line_start": 1, "line_end": txt.count("\n") + 1, "frecency_score": 0.5,
        } for t, txt in _CODE_DOCS],
    )

    know_col = db.get_or_create_collection("knowledge__snapshot")
    know_col.add(
        ids=[f"know-{t}" for t, _ in _KNOWLEDGE_DOCS],
        documents=[txt for _, txt in _KNOWLEDGE_DOCS],
        metadatas=[{
            "title": t, "source_path": f"/docs/{t}.md", "file_path": f"/docs/{t}.md",
            "line_start": 1, "line_end": 1, "frecency_score": 0.5,
        } for t, txt in _KNOWLEDGE_DOCS],
    )
    return db


def _format_results(results: list) -> str:
    lines = []
    for r in results:
        path = r.metadata.get("source_path", "?")
        score = f"{r.hybrid_score:.4f}" if r.hybrid_score else f"d={r.distance:.4f}"
        lines.append(f"{score}  {r.collection}  {path}")
    return "\n".join(lines)


@pytest.mark.parametrize("query", _QUERIES)
def test_search_ranking_snapshot(query: str, search_corpus: T3Database, snapshot) -> None:
    raw = search_cross_corpus(query, ["code__snapshot", "knowledge__snapshot"], n_results=5, t3=search_corpus)
    scored = apply_hybrid_scoring(raw, hybrid=True)
    assert _format_results(scored) == snapshot


@pytest.mark.parametrize("query", _QUERIES[:3])
def test_search_single_corpus_snapshot(query: str, search_corpus: T3Database, snapshot) -> None:
    raw = search_cross_corpus(query, ["code__snapshot"], n_results=5, t3=search_corpus)
    scored = apply_hybrid_scoring(raw, hybrid=False)
    assert _format_results(scored) == snapshot


# ── Phase 1.1 (RDR-087): --threshold / --no-threshold ───────────────────────


def _capture_ctx_full(collections: list[str] | None = None):
    """Like ``_capture_ctx`` but captures the full kwargs dict."""
    mock = _mock_t3(collections)
    captured: list[dict] = []

    def fake(query, cols, n_results, t3, where=None, **kwargs):
        captured.append({"where": where, **kwargs})
        return []

    return mock, captured, fake


def test_threshold_flag_passes_override_to_engine(runner: CliRunner, cloud_env) -> None:
    mock, captured, fake = _capture_ctx_full(["knowledge__test"])
    with patch("nexus.commands.search_cmd._t3", return_value=mock), \
         patch("nexus.commands.search_cmd.search_cross_corpus", side_effect=fake), \
         patch("nexus.commands.search_cmd.load_config", return_value=_LOAD_CFG):
        result = runner.invoke(
            main, ["search", "query", "--corpus", "knowledge", "--threshold", "0.8"],
        )
    assert result.exit_code == 0, result.output
    assert captured[0].get("threshold_override") == pytest.approx(0.8)


def test_no_threshold_flag_disables_filtering(runner: CliRunner, cloud_env) -> None:
    import math
    mock, captured, fake = _capture_ctx_full(["knowledge__test"])
    with patch("nexus.commands.search_cmd._t3", return_value=mock), \
         patch("nexus.commands.search_cmd.search_cross_corpus", side_effect=fake), \
         patch("nexus.commands.search_cmd.load_config", return_value=_LOAD_CFG):
        result = runner.invoke(
            main, ["search", "query", "--corpus", "knowledge", "--no-threshold"],
        )
    assert result.exit_code == 0, result.output
    override = captured[0].get("threshold_override")
    assert override is not None and math.isinf(override)


def test_default_passes_none_override(runner: CliRunner, cloud_env) -> None:
    mock, captured, fake = _capture_ctx_full(["knowledge__test"])
    with patch("nexus.commands.search_cmd._t3", return_value=mock), \
         patch("nexus.commands.search_cmd.search_cross_corpus", side_effect=fake), \
         patch("nexus.commands.search_cmd.load_config", return_value=_LOAD_CFG):
        result = runner.invoke(main, ["search", "query", "--corpus", "knowledge"])
    assert result.exit_code == 0, result.output
    assert captured[0].get("threshold_override") is None


def test_threshold_and_no_threshold_mutually_exclusive(
    runner: CliRunner, cloud_env,
) -> None:
    mock_t3 = _mock_t3(["knowledge__test"])
    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3), \
         patch("nexus.commands.search_cmd.search_cross_corpus", return_value=[]), \
         patch("nexus.commands.search_cmd.load_config", return_value=_LOAD_CFG):
        result = runner.invoke(main, [
            "search", "query", "--corpus", "knowledge",
            "--threshold", "0.7", "--no-threshold",
        ])
    assert result.exit_code != 0
    err = (result.output or "") + (str(result.exception) if result.exception else "")
    assert "threshold" in err.lower() and (
        "mutually exclusive" in err.lower() or "cannot" in err.lower()
    )


# ── Phase 1.2 (RDR-087 / nexus-yi4b.1.2): silent-zero stderr ────────────────


def test_silent_zero_emits_single_stderr_line_when_raw_gt_zero(
    runner: CliRunner, cloud_env, monkeypatch,
) -> None:
    """Zero post-threshold results + raw>0 + drops>0 → exactly one stderr line."""
    import structlog
    from nexus.search_engine import SearchDiagnostics

    mock_t3 = _mock_t3(["knowledge__art"])

    # Use the real engine diagnostics shape — simulate 3 candidates, all dropped,
    # top_distance = 0.70.
    def fake(query, cols, n_results, t3, where=None, **kwargs):
        diag_out = kwargs.get("diagnostics_out")
        if diag_out is not None:
            diag_out.append(SearchDiagnostics(
                per_collection={"knowledge__art": (3, 3, 0.65, 0.70)},
                total_dropped=3,
                total_raw=3,
            ))
        return []

    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3), \
         patch("nexus.commands.search_cmd.search_cross_corpus", side_effect=fake), \
         patch("nexus.commands.search_cmd.load_config", return_value=_LOAD_CFG):
        result = runner.invoke(main, ["search", "query", "--corpus", "knowledge"])
    assert result.exit_code == 0, result.output
    stderr = result.output  # CliRunner merges by default
    assert stderr.count("candidates dropped") == 1
    assert "knowledge__art" in stderr
    assert "threshold" in stderr
    assert "top_distance" in stderr


def test_silent_zero_omitted_when_raw_is_zero(
    runner: CliRunner, cloud_env,
) -> None:
    """No stderr note when there were no raw candidates to begin with."""
    from nexus.search_engine import SearchDiagnostics

    mock_t3 = _mock_t3(["knowledge__art"])

    def fake(query, cols, n_results, t3, where=None, **kwargs):
        diag_out = kwargs.get("diagnostics_out")
        if diag_out is not None:
            diag_out.append(SearchDiagnostics(
                per_collection={"knowledge__art": (0, 0, 0.65, None)},
                total_dropped=0,
                total_raw=0,
            ))
        return []

    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3), \
         patch("nexus.commands.search_cmd.search_cross_corpus", side_effect=fake), \
         patch("nexus.commands.search_cmd.load_config", return_value=_LOAD_CFG):
        result = runner.invoke(main, ["search", "query", "--corpus", "knowledge"])
    assert result.exit_code == 0, result.output
    assert "candidates dropped" not in result.output


def test_silent_zero_suppressed_by_quiet_flag(
    runner: CliRunner, cloud_env,
) -> None:
    """--quiet suppresses the note even when drops fired."""
    from nexus.search_engine import SearchDiagnostics

    mock_t3 = _mock_t3(["knowledge__art"])

    def fake(query, cols, n_results, t3, where=None, **kwargs):
        diag_out = kwargs.get("diagnostics_out")
        if diag_out is not None:
            diag_out.append(SearchDiagnostics(
                per_collection={"knowledge__art": (3, 3, 0.65, 0.70)},
                total_dropped=3,
                total_raw=3,
            ))
        return []

    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3), \
         patch("nexus.commands.search_cmd.search_cross_corpus", side_effect=fake), \
         patch("nexus.commands.search_cmd.load_config", return_value=_LOAD_CFG):
        result = runner.invoke(
            main, ["search", "query", "--corpus", "knowledge", "--quiet"],
        )
    assert result.exit_code == 0, result.output
    assert "candidates dropped" not in result.output


def test_silent_zero_end_to_end_real_engine(
    runner: CliRunner, cloud_env, monkeypatch, tmp_path,
) -> None:
    """nexus-vwvx — real ``search_cross_corpus`` → stderr note round-trip.

    Every other silent-zero test in this file stubs ``search_cross_corpus``
    with a side_effect that manually injects a pre-built
    ``SearchDiagnostics``. That covers the CLI-side consumer
    (``_maybe_emit_silent_zero_note``) but leaves the engine's
    diagnostics-population path unverified by the CLI layer.

    This test plugs a real ``EphemeralClient`` T3 into the CLI and lets
    the real ``_search_collection`` loop compute raw/dropped counts from
    known distances. The ``--threshold`` flag's ``threshold_override`` path
    forces ``apply_thresholds=True`` regardless of ``_voyage_client``
    presence, so seeded docs get filtered.

    Stability bar per bead: re-runs in loop must produce identical stderr
    (no timing noise, no distance jitter). Driven by the ONNX MiniLM EF
    which is deterministic for a given input.

    Note on stderr capture (Reviewer C/S-1): Click's ``CliRunner`` mixes
    stderr into ``result.output`` by default (the ``mix_stderr=True``
    constructor flag). The silent-zero note is emitted via
    ``click.echo(..., err=True)``, so an assertion on ``result.output``
    that looks like a stdout check is actually exercising both streams.
    Do not split to ``mix_stderr=False`` without switching the
    assertions to ``result.stderr`` simultaneously.
    """
    import chromadb
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

    coll_name = "knowledge__e2e_silentzero"
    ef = DefaultEmbeddingFunction()
    client = chromadb.EphemeralClient()
    real_t3 = T3Database(_client=client, _ef_override=ef)

    # Seed a collection with three documents whose raw distances will
    # exceed a tiny threshold. MiniLM distances are deterministic for
    # the same (query, corpus) pair — stability follows.
    col = real_t3.get_or_create_collection(coll_name)
    col.add(
        ids=["d1", "d2", "d3"],
        documents=[
            "meditations on the architecture of cathedrals",
            "notes on medieval stained-glass iconography",
            "survey of vaulting techniques in Gothic nave design",
        ],
        metadatas=[
            {"source_path": "a.md"},
            {"source_path": "b.md"},
            {"source_path": "c.md"},
        ],
    )

    # Threshold far below any plausible MiniLM distance for a loosely-
    # related query → every raw candidate drops → silent-zero fires.
    query = "quantum field theory in curved spacetime"
    threshold = "0.01"

    # Isolate T2 writes (taxonomy lookup opens a T2Database) into tmp_path
    # so the test doesn't touch the developer's prod memory.db.
    sandbox_db = tmp_path / "memory.db"
    monkeypatch.setattr(
        "nexus.commands._helpers.default_db_path",
        lambda: sandbox_db,
    )

    with patch("nexus.commands.search_cmd._t3", return_value=real_t3), \
         patch("nexus.commands.search_cmd.load_config", return_value=_LOAD_CFG):
        invoke = lambda: runner.invoke(
            main,
            [
                "search", query,
                "--corpus", coll_name,
                "--threshold", threshold,
            ],
        )
        results = [invoke() for _ in range(3)]

    for i, result in enumerate(results):
        assert result.exit_code == 0, f"run {i}: {result.output}"
        assert result.output.count("candidates dropped") == 1, (
            f"run {i}: expected exactly one silent-zero line, got:\n{result.output}"
        )
        assert coll_name in result.output
        assert "threshold 0.010" in result.output  # rendered as 3-decimal

    # Stability bar: stderr line identical across runs (no distance jitter).
    lines = [
        next(line for line in r.output.splitlines() if "candidates dropped" in line)
        for r in results
    ]
    assert lines[0] == lines[1] == lines[2], (
        f"silent-zero stderr differed across runs:\n" + "\n---\n".join(lines)
    )


def test_silent_zero_suppressed_by_config(runner: CliRunner, cloud_env) -> None:
    """``telemetry.stderr_silent_zero = false`` in config disables the note."""
    from nexus.search_engine import SearchDiagnostics

    mock_t3 = _mock_t3(["knowledge__art"])

    def fake(query, cols, n_results, t3, where=None, **kwargs):
        diag_out = kwargs.get("diagnostics_out")
        if diag_out is not None:
            diag_out.append(SearchDiagnostics(
                per_collection={"knowledge__art": (3, 3, 0.65, 0.70)},
                total_dropped=3,
                total_raw=3,
            ))
        return []

    cfg_with_opt_out = {
        **_LOAD_CFG,
        "telemetry": {"stderr_silent_zero": False},
    }
    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3), \
         patch("nexus.commands.search_cmd.search_cross_corpus", side_effect=fake), \
         patch("nexus.commands.search_cmd.load_config", return_value=cfg_with_opt_out):
        result = runner.invoke(main, ["search", "query", "--corpus", "knowledge"])
    assert result.exit_code == 0, result.output
    assert "candidates dropped" not in result.output


def test_silent_zero_picks_worst_offender_across_collections(
    runner: CliRunner, cloud_env,
) -> None:
    """With full-drops on two collections, worst = highest top_distance."""
    from nexus.search_engine import SearchDiagnostics

    mock_t3 = _mock_t3(["code__a", "knowledge__b"])

    def fake(query, cols, n_results, t3, where=None, **kwargs):
        diag_out = kwargs.get("diagnostics_out")
        if diag_out is not None:
            diag_out.append(SearchDiagnostics(
                per_collection={
                    "code__a": (2, 2, 0.45, 0.50),
                    "knowledge__b": (2, 2, 0.65, 0.82),
                },
                total_dropped=4,
                total_raw=4,
            ))
        return []

    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3), \
         patch("nexus.commands.search_cmd.search_cross_corpus", side_effect=fake), \
         patch("nexus.commands.search_cmd.load_config", return_value=_LOAD_CFG):
        result = runner.invoke(
            main, ["search", "query", "--corpus", "code", "--corpus", "knowledge"],
        )
    assert result.exit_code == 0, result.output
    assert "knowledge__b" in result.output
    # Must NOT report the other collection's threshold as if it were worst.
    assert "code__a" not in result.output.split("knowledge__b")[0] or \
        "top_distance 0.82" in result.output
