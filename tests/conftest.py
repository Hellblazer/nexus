import pytest
from pathlib import Path

import chromadb
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from nexus.db.t2 import T2Database
from nexus.db.t3 import T3Database


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
