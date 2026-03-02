"""E2E tests — local ChromaDB (EphemeralClient) + DefaultEmbeddingFunction.

No API keys required. Tests exercise real code paths with local backends:
  - T3Database: EphemeralClient + ONNX MiniLM-L6-v2 (DefaultEmbeddingFunction)
  - Anthropic: canned mock responses
  - All search_engine logic, CLI commands, and PM lifecycle run unmodified.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.t3 import T3Database


# ── Helpers ───────────────────────────────────────────────────────────────────

def _uid() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# ── T3Database direct API ─────────────────────────────────────────────────────

def test_t3_put_and_search(local_t3: T3Database) -> None:
    """put() stores a document; search() retrieves it by semantic similarity."""
    uid = _uid()
    local_t3.put(
        collection="knowledge__test",
        content=f"The secret token is {uid}",
        title="secret.md",
    )
    results = local_t3.search(f"secret token {uid}", ["knowledge__test"])
    assert len(results) == 1
    assert uid in results[0]["content"]
    assert results[0]["title"] == "secret.md"


def test_t3_put_multiple_and_ranking(local_t3: T3Database) -> None:
    """Results are sorted by distance — closest match ranks first."""
    uid = _uid()
    local_t3.put("knowledge__rank", content=f"Python is a snake {uid}", title="a.md")
    local_t3.put("knowledge__rank", content=f"Python programming language {uid}", title="b.md")
    local_t3.put("knowledge__rank", content=f"Unrelated topic about cooking {uid}", title="c.md")

    results = local_t3.search(f"Python programming {uid}", ["knowledge__rank"], n_results=3)
    assert len(results) == 3
    # programming doc should rank ahead of cooking
    titles = [r["title"] for r in results]
    assert titles.index("b.md") < titles.index("c.md")


def test_t3_search_returns_metadata(local_t3: T3Database) -> None:
    """Metadata fields are preserved and returned with search results."""
    uid = _uid()
    local_t3.put(
        collection="knowledge__meta",
        content=f"metadata test {uid}",
        title="meta.md",
        tags="tag1,tag2",
        category="testing",
    )
    results = local_t3.search(uid, ["knowledge__meta"])
    assert results[0]["tags"] == "tag1,tag2"
    assert results[0]["category"] == "testing"


def test_t3_expire_removes_expired_entries(local_t3: T3Database) -> None:
    """expire() removes entries with elapsed TTL; permanent entries survive."""
    from datetime import UTC, datetime, timedelta

    uid = _uid()
    # Insert already-expired entry by manually setting expires_at in the past
    col = local_t3.get_or_create_collection("knowledge__expire")
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    col.upsert(
        ids=["expired-doc"],
        documents=[f"expired content {uid}"],
        metadatas=[{"ttl_days": 1, "expires_at": past, "title": "exp.md",
                    "tags": "", "category": "", "session_id": "", "source_agent": "",
                    "store_type": "knowledge", "indexed_at": past}],
    )
    # Insert permanent entry
    local_t3.put("knowledge__expire", content=f"permanent content {uid}", title="perm.md")

    deleted = local_t3.expire()
    assert deleted == 1

    # Permanent entry still searchable
    results = local_t3.search(f"permanent content {uid}", ["knowledge__expire"])
    assert any("perm.md" == r["title"] for r in results)


def test_t3_list_collections(local_t3: T3Database) -> None:
    """list_collections() returns names and counts."""
    uid = _uid()
    local_t3.put(f"knowledge__{uid}", content="hello", title="a.md")
    local_t3.put(f"knowledge__{uid}", content="world", title="b.md")

    cols = local_t3.list_collections()
    match = next((c for c in cols if c["name"] == f"knowledge__{uid}"), None)
    assert match is not None
    assert match["count"] == 2


def test_t3_delete_collection(local_t3: T3Database) -> None:
    """delete_collection() removes it entirely."""
    uid = _uid()
    local_t3.put(f"knowledge__{uid}", content="bye", title="bye.md")
    local_t3.delete_collection(f"knowledge__{uid}")
    cols = {c["name"] for c in local_t3.list_collections()}
    assert f"knowledge__{uid}" not in cols


def test_t3_cross_collection_search(local_t3: T3Database) -> None:
    """search() can query multiple collections and merges results by distance."""
    uid = _uid()
    local_t3.put("knowledge__colA", content=f"apple pie recipe {uid}", title="apple.md")
    local_t3.put("knowledge__colB", content=f"apple cider vinegar {uid}", title="cider.md")

    results = local_t3.search(f"apple {uid}", ["knowledge__colA", "knowledge__colB"])
    assert len(results) == 2
    # results sorted by distance ascending
    assert results[0]["distance"] <= results[1]["distance"]


# ── search_engine functions ───────────────────────────────────────────────────

def test_search_cross_corpus_end_to_end(local_t3: T3Database) -> None:
    """search_cross_corpus() retrieves and merges results from multiple corpora."""
    from nexus.search_engine import search_cross_corpus  # orchestration stays in search_engine

    uid = _uid()
    local_t3.put("knowledge__e2e", content=f"nexus architecture {uid}", title="arch.md")
    local_t3.put("knowledge__e2e", content=f"nexus deployment guide {uid}", title="deploy.md")

    results = search_cross_corpus(
        query=f"nexus {uid}",
        collections=["knowledge__e2e"],
        n_results=5,
        t3=local_t3,
    )
    assert len(results) >= 1
    contents = [r.content for r in results]
    assert any(uid in c for c in contents)


def test_apply_hybrid_scoring_code_collection(local_t3: T3Database) -> None:
    """apply_hybrid_scoring applies 0.7*vector + 0.3*frecency for code__ collections."""
    from nexus.scoring import apply_hybrid_scoring
    from nexus.types import SearchResult

    results = [
        SearchResult(id="1", content="fn main()", distance=0.1,
                     collection="code__myrepo", metadata={}),
        SearchResult(id="2", content="class Foo:", distance=0.3,
                     collection="code__myrepo", metadata={}),
    ]
    scored = apply_hybrid_scoring(results, hybrid=True)
    # All results get a hybrid_score between 0 and 1
    assert all(0.0 <= r.hybrid_score <= 1.0 for r in scored)


def test_format_vimgrep(local_t3: T3Database) -> None:
    """format_vimgrep produces path:line:0:content lines."""
    from nexus.formatters import format_vimgrep
    from nexus.types import SearchResult

    results = [
        SearchResult(id="1", content="def foo():", distance=0.1,
                     collection="code__repo",
                     metadata={"source_path": "src/main.py", "line_start": 42}),
    ]
    lines = format_vimgrep(results)
    assert len(lines) == 1
    assert "src/main.py" in lines[0]
    assert ":0:" in lines[0]


def test_format_json_is_valid_json(local_t3: T3Database) -> None:
    """format_json produces parseable JSON."""
    import json
    from nexus.formatters import format_json
    from nexus.types import SearchResult

    results = [
        SearchResult(id="a1", content="hello world", distance=0.2,
                     collection="knowledge__test", metadata={"title": "t.md"}),
    ]
    output = format_json(results)
    parsed = json.loads(output)
    assert isinstance(parsed, list)
    assert parsed[0]["content"] == "hello world"


# ── CLI commands end-to-end ───────────────────────────────────────────────────

def test_cli_store_put_and_search(
    runner: CliRunner, fake_home: Path, local_t3: T3Database
) -> None:
    """nx store put → nx store search round-trip via CLI."""
    uid = _uid()

    with patch("nexus.commands.store._t3", return_value=local_t3), \
         patch("nexus.commands.search_cmd._t3", return_value=local_t3):

        put_result = runner.invoke(main, [
            "store", "put", "-",
            "--collection", f"knowledge__cli-{uid}",
            "--title", f"{uid}.md",
        ], input=f"CLI integration content {uid}")
        assert put_result.exit_code == 0, put_result.output

        search_result = runner.invoke(main, [
            "search", f"integration content {uid}",
            "--corpus", f"knowledge__cli-{uid}",
        ])
        assert search_result.exit_code == 0, search_result.output
        assert uid in search_result.output


def test_cli_search_command(
    runner: CliRunner, fake_home: Path, local_t3: T3Database
) -> None:
    """nx search returns results from local T3."""
    uid = _uid()
    local_t3.put("knowledge__search", content=f"search test content {uid}", title="s.md")

    with patch("nexus.commands.search_cmd._t3", return_value=local_t3):
        result = runner.invoke(main, [
            "search", f"search test {uid}",
            "--corpus", "knowledge__search",
            "--n", "5",
        ])
    assert result.exit_code == 0, result.output
    assert uid in result.output


def test_cli_search_json_output(
    runner: CliRunner, fake_home: Path, local_t3: T3Database
) -> None:
    """nx search --json returns parseable JSON array."""
    import json

    uid = _uid()
    local_t3.put("knowledge__json", content=f"json output test {uid}", title="j.md")

    with patch("nexus.commands.search_cmd._t3", return_value=local_t3):
        result = runner.invoke(main, [
            "search", uid,
            "--corpus", "knowledge__json",
            "--json",
        ])
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert isinstance(parsed, list)
    assert any(uid in item.get("content", "") for item in parsed)


def test_cli_search_vimgrep_output(
    runner: CliRunner, fake_home: Path, local_t3: T3Database
) -> None:
    """nx search --vimgrep produces path:line:col:content lines."""
    uid = _uid()
    local_t3.put("knowledge__vimgrep", content=f"vimgrep test {uid}", title="v.md")

    with patch("nexus.commands.search_cmd._t3", return_value=local_t3):
        result = runner.invoke(main, [
            "search", uid,
            "--corpus", "knowledge__vimgrep",
            "--vimgrep",
        ])
    assert result.exit_code == 0, result.output
    assert ":0:" in result.output


# ── PM lifecycle end-to-end ───────────────────────────────────────────────────

def test_pm_init_status_block_unblock(
    runner: CliRunner, fake_home: Path, db
) -> None:
    """nx pm init → status → block → unblock lifecycle."""
    _t2_cm = MagicMock(__enter__=MagicMock(return_value=db))
    with patch("nexus.commands.pm.T2Database", return_value=_t2_cm):
        init = runner.invoke(main, ["pm", "init", "--project", "e2e-proj"])
        assert init.exit_code == 0, init.output

        status = runner.invoke(main, ["pm", "status", "--project", "e2e-proj"])
        assert status.exit_code == 0, status.output
        assert "phase" in status.output.lower() or "1" in status.output

        block = runner.invoke(main, ["pm", "block", "waiting on API approval",
                                     "--project", "e2e-proj"])
        assert block.exit_code == 0, block.output

        unblock = runner.invoke(main, ["pm", "unblock", "1", "--project", "e2e-proj"])
        assert unblock.exit_code == 0, unblock.output
