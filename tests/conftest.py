import logging

import pytest
from pathlib import Path

import chromadb
import structlog
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from nexus.db.t2 import T2Database
from nexus.db.t3 import T3Database


def pytest_configure(config):
    """Configure structlog level to match pytest's --log-level.

    Default run: WARNING level — quiet, no clutter.
    Validation run: pytest --log-level=DEBUG — full structlog output to stdout.

    Example:
        uv run pytest                          # quiet (WARNING)
        uv run pytest --log-level=DEBUG        # full debug output
    """
    try:
        level_str = (config.getoption("log_level") or "WARNING").upper()
    except (ValueError, AttributeError):
        level_str = "WARNING"
    level = getattr(logging, level_str, logging.WARNING)
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )


@pytest.fixture(autouse=True)
def _isolate_t1_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect T1 SESSIONS_DIR so tests never discover real live server records.

    Before fixing chroma's --log-level flag, start_t1_server() always failed
    in tests (chroma exited immediately), so no session file was ever written
    and test isolation held by accident.  Now that the server starts cleanly,
    test_session_start_prints_ready_message (and similar) actually write a
    session file to the real SESSIONS_DIR.  Subsequent T1Database() calls in
    the same pytest process find it via PPID chain walk, hijack the session_id,
    and break isolation across unrelated tests.

    Solution: redirect both consumers of SESSIONS_DIR to an empty per-test
    tmp_path so find_ancestor_session() always returns None.  T1Database falls
    back to the process-wide EphemeralClient singleton (isolated by session_id),
    and any session files written by session_start() go to tmp_path, not ~/.
    """
    sessions = tmp_path / ".nexus" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("nexus.db.t1.SESSIONS_DIR", sessions)
    monkeypatch.setattr("nexus.hooks.SESSIONS_DIR", sessions)


@pytest.fixture
def db(tmp_path: Path) -> T2Database:
    """Provide a T2Database backed by a temporary SQLite file."""
    database = T2Database(tmp_path / "memory.db")
    yield database
    database.close()


@pytest.fixture
def local_t3() -> T3Database:
    """T3Database backed by an in-memory EphemeralClient and DefaultEmbeddingFunction.

    Each test gets a fresh, isolated database — no API keys required.
    DefaultEmbeddingFunction uses the bundled ONNX MiniLM-L6-v2 model,
    so semantic similarity works correctly without Voyage AI.
    """
    client = chromadb.EphemeralClient()
    ef = DefaultEmbeddingFunction()
    return T3Database(_client=client, _ef_override=ef)
