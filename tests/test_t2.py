"""Tests for T2Database context manager support."""
import sqlite3
from pathlib import Path

import pytest

from nexus.db.t2 import T2Database


def test_t2database_context_manager_closes_on_exit(tmp_path: Path) -> None:
    """T2Database used as a context manager closes the connection on __exit__."""
    db_path = tmp_path / "cm_test.db"
    with T2Database(db_path) as db:
        # Connection is usable inside the block
        row_id = db.put(project="test", title="cm-entry", content="hello context manager")
        assert row_id is not None

    # After the block the connection must be closed; any operation raises ProgrammingError
    with pytest.raises(Exception):
        db.conn.execute("SELECT 1")


def test_t2database_context_manager_closes_on_exception(tmp_path: Path) -> None:
    """T2Database context manager closes the connection even when an exception is raised."""
    db_path = tmp_path / "cm_exc_test.db"
    with pytest.raises(ValueError, match="intentional"):
        with T2Database(db_path) as db:
            # Write something to prove the connection was open
            db.put(project="test", title="exc-entry", content="before error")
            raise ValueError("intentional")

    # Connection must be closed despite the exception
    with pytest.raises(Exception):
        db.conn.execute("SELECT 1")


def test_t2database_context_manager_returns_self(tmp_path: Path) -> None:
    """__enter__ returns the T2Database instance itself."""
    db_path = tmp_path / "cm_self_test.db"
    with T2Database(db_path) as db:
        assert isinstance(db, T2Database)


def test_t2database_context_manager_does_not_suppress_exception(tmp_path: Path) -> None:
    """__exit__ must not suppress exceptions (returns None / falsy)."""
    db_path = tmp_path / "cm_nosuppress.db"
    with pytest.raises(RuntimeError, match="propagated"):
        with T2Database(db_path) as db:
            raise RuntimeError("propagated")
