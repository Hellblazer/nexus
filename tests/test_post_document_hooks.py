# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the document-grain post-store hook chain (RDR-089 P0.1).

This third hook chain (alongside the single-doc and batch chains) fires
once per *document* after every storage event — MCP ``store_put`` and
every CLI ingest path. Consumers register synchronous callables of shape::

    fn(source_path: str, collection: str, content: str) -> None

Content-sourcing contract (audit F4):

* MCP ``store_put`` passes ``content=<full doc text>`` literally — the
  full text is in scope at the boundary.
* CLI sites that accumulate chunks rather than full documents pass
  ``content=""`` as a contract signal that the hook may need to read
  ``source_path`` itself.
* Hooks treat ``content`` as primary, falling back to file read when
  empty.

Failure isolation (mirrors RDR-070 / RDR-095): per-hook exceptions are
captured, logged, and persisted to T2 ``hook_failures`` with
``chain='document'``. Failures never propagate to the ingest caller.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nexus.db.t2 import T2Database


@pytest.fixture(autouse=True)
def _reset_document_hooks():
    """Clear the document-hook chain between tests."""
    from nexus.mcp_infra import _post_document_hooks
    _post_document_hooks.clear()
    yield
    _post_document_hooks.clear()


# ── Registration + dispatch ──────────────────────────────────────────────────


def test_register_post_document_hook_appends() -> None:
    """register_post_document_hook appends to the module-level list."""
    from nexus.mcp_infra import _post_document_hooks, register_post_document_hook

    def probe(source_path, collection, content):
        return None

    assert _post_document_hooks == []
    register_post_document_hook(probe)
    assert _post_document_hooks == [probe]


def test_fire_post_document_hooks_calls_registered() -> None:
    """fire_post_document_hooks invokes all registered callables in order."""
    from nexus.mcp_infra import (
        fire_post_document_hooks,
        register_post_document_hook,
    )

    seen: list[tuple] = []

    def hook_a(source_path, collection, content):
        seen.append(("a", source_path, collection, content))

    def hook_b(source_path, collection, content):
        seen.append(("b", source_path, collection, content))

    register_post_document_hook(hook_a)
    register_post_document_hook(hook_b)

    fire_post_document_hooks("/path/to/doc.md", "knowledge__delos", "body text")

    assert seen == [
        ("a", "/path/to/doc.md", "knowledge__delos", "body text"),
        ("b", "/path/to/doc.md", "knowledge__delos", "body text"),
    ]


# ── Content-sourcing contract (audit F4) ─────────────────────────────────────


def test_fire_passes_content_through_when_populated() -> None:
    """When ``content`` is non-empty (MCP path), it reaches the hook
    untouched. The hook should NOT need to fall back to reading
    ``source_path`` itself.
    """
    from nexus.mcp_infra import (
        fire_post_document_hooks,
        register_post_document_hook,
    )

    captured: list[str] = []

    def hook(source_path, collection, content):
        # Content is primary: use it directly.
        captured.append(content)

    register_post_document_hook(hook)
    fire_post_document_hooks("/path/x.md", "knowledge__delos", "FULL TEXT")

    assert captured == ["FULL TEXT"]


def test_fire_passes_empty_content_signal_for_cli_path(tmp_path: Path) -> None:
    """CLI sites pass ``content=""`` as the contract signal that the hook
    may need to read ``source_path`` itself. The framework forwards both
    parameters as-is — content-sourcing is hook responsibility, not
    framework responsibility.
    """
    from nexus.mcp_infra import (
        fire_post_document_hooks,
        register_post_document_hook,
    )

    src = tmp_path / "doc.md"
    src.write_text("body read from disk")

    captured: list[str] = []

    def hook(source_path, collection, content):
        # Hook honors the contract: empty content => read source_path.
        if not content:
            captured.append(Path(source_path).read_text())
        else:
            captured.append(content)

    register_post_document_hook(hook)
    fire_post_document_hooks(str(src), "knowledge__delos", "")

    assert captured == ["body read from disk"]


# ── Async/sync contract (RDR-089 load-bearing) ───────────────────────────────


def test_async_hooks_silently_unsupported_by_dispatcher() -> None:
    """The dispatcher is synchronous all the way down — RDR-089 load-bearing
    contract. Routing through an async hook from this sync chain silently
    drops the returned coroutine (which was the original RDR-089 defect
    caught by audit F1). This test pins that behaviour: an async hook
    registers fine, but its body never runs — calling code should treat
    async hooks as caller-responsibility, not framework support.

    Captures the RuntimeWarning emitted when a coroutine is never awaited
    so the test fails fast if a future contributor reintroduces ``await``
    or ``asyncio.to_thread`` inside ``fire_post_document_hooks``.
    """
    import warnings

    from nexus.mcp_infra import (
        fire_post_document_hooks,
        register_post_document_hook,
    )

    body_ran: list[bool] = []

    async def async_hook(source_path, collection, content):
        body_ran.append(True)

    register_post_document_hook(async_hook)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        fire_post_document_hooks("/p", "knowledge__delos", "x")

    # Coroutine was created but never awaited → body did not run.
    assert body_ran == []
    # Python emits RuntimeWarning("coroutine 'async_hook' was never awaited")
    # — pinned so future awaits trip this test.
    coroutine_warnings = [
        w for w in caught
        if issubclass(w.category, RuntimeWarning) and "never awaited" in str(w.message)
    ]
    assert coroutine_warnings, (
        "Expected a 'coroutine never awaited' RuntimeWarning; the "
        "dispatcher must not await async hooks. If this assertion "
        "fails, the dispatcher likely added await or "
        "asyncio.to_thread — that violates the RDR-089 sync-all-the-"
        "way-down contract (audit F1)."
    )


# ── Failure isolation ────────────────────────────────────────────────────────


def test_fire_post_document_hooks_exception_nonfatal() -> None:
    """A raising hook must not block the next registered hook from firing,
    and the dispatcher itself must never raise.
    """
    from nexus.mcp_infra import (
        fire_post_document_hooks,
        register_post_document_hook,
    )

    survived: list[str] = []

    def raising(source_path, collection, content):
        raise RuntimeError("simulated document-hook failure")

    def survivor(source_path, collection, content):
        survived.append(source_path)

    register_post_document_hook(raising)
    register_post_document_hook(survivor)

    fire_post_document_hooks("/path/y.md", "knowledge__delos", "x")

    assert survived == ["/path/y.md"]


def test_fire_post_document_hooks_persists_failure_to_t2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hook failures land in T2 ``hook_failures`` with ``chain='document'``.

    The new column is added by migration 4.14.2; the running package is
    still 4.14.1 so ``T2Database``'s automatic ``apply_pending`` stops
    one short. Apply the chain migration directly so the write path has
    a target, mirroring the 4.9.10 pattern in
    ``test_post_store_hook.py``.
    """
    import nexus.mcp_infra as mod
    from nexus.db.migrations import migrate_hook_failures_chain_column
    from nexus.mcp_infra import (
        fire_post_document_hooks,
        register_post_document_hook,
    )

    db_path = tmp_path / "doc_hook_failures.db"
    T2Database(db_path).close()  # base migrations through 4.14.1
    raw = sqlite3.connect(str(db_path))
    migrate_hook_failures_chain_column(raw)
    raw.close()
    monkeypatch.setattr(mod, "t2_ctx", lambda: T2Database(db_path))

    def bad_hook(source_path, collection, content):
        raise RuntimeError("doc hook boom")

    register_post_document_hook(bad_hook)
    fire_post_document_hooks("/abs/path/x.md", "knowledge__delos", "x")

    with T2Database(db_path) as db:
        rows = db.taxonomy.conn.execute(
            "SELECT doc_id, collection, hook_name, error, chain "
            "FROM hook_failures"
        ).fetchall()

    assert len(rows) == 1
    doc_id, coll, hook_name, error, chain = rows[0]
    # source_path is stored in doc_id (the column carries 'subject of failure').
    assert doc_id == "/abs/path/x.md"
    assert coll == "knowledge__delos"
    assert hook_name == "bad_hook"
    assert "doc hook boom" in error
    assert chain == "document"


def test_fire_post_document_hooks_falls_back_to_scalar_when_chain_column_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the ``chain`` column is absent (pre-4.14.2 schema, mixed-version
    operator scenario: a write path running fresh code against an older
    T2 schema), the failure capture writes a chain-less row rather than
    crashing.

    Bypasses ``T2Database`` because it would auto-apply migrations
    through 4.14.1 inclusive (the package version stays at 4.14.1 until
    the next release tag — see ``pyproject.toml``). Builds a raw sqlite
    connection at the 4.14.1 schema and mocks ``t2_ctx`` to expose just
    the surface ``_record_document_hook_failure`` reads.
    """
    import threading

    import nexus.mcp_infra as mod
    from nexus.db.migrations import (
        migrate_hook_failures,
        migrate_hook_failures_batch_columns,
    )
    from nexus.mcp_infra import (
        fire_post_document_hooks,
        register_post_document_hook,
    )

    db_path = tmp_path / "pre_4_14_2.db"
    raw = sqlite3.connect(str(db_path), isolation_level=None)
    migrate_hook_failures(raw)
    migrate_hook_failures_batch_columns(raw)  # 4.14.1 — but NOT 4.14.2

    cols = {r[1] for r in raw.execute("PRAGMA table_info(hook_failures)").fetchall()}
    assert "chain" not in cols, (
        "pre-condition: hook_failures must lack the chain column"
    )

    class _FakeTaxonomy:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn = conn
            self._lock = threading.RLock()

    class _FakeT2:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.taxonomy = _FakeTaxonomy(conn)
        def __enter__(self) -> "_FakeT2":
            return self
        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(mod, "t2_ctx", lambda: _FakeT2(raw))

    def raising(source_path, collection, content):
        raise RuntimeError("doc kaboom")

    register_post_document_hook(raising)
    fire_post_document_hooks("/path/missing-chain.md", "knowledge__delos", "x")

    rows = raw.execute(
        "SELECT doc_id, hook_name, error FROM hook_failures"
    ).fetchall()
    raw.close()

    # Fallback path persists scalar fields; the chain marker is dropped
    # silently because the column does not exist on this DB.
    assert len(rows) == 1
    assert rows[0][0] == "/path/missing-chain.md"
    assert rows[0][1] == "raising"
    assert "doc kaboom" in rows[0][2]


def test_fire_post_document_hooks_persist_swallowed_when_table_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the hook_failures table is absent (extreme legacy DB), ingest
    is never blocked — the persist failure is caught silently.
    """
    import nexus.mcp_infra as mod
    from nexus.mcp_infra import (
        fire_post_document_hooks,
        register_post_document_hook,
    )

    db_path = tmp_path / "no_table.db"
    raw = sqlite3.connect(str(db_path))
    raw.close()  # empty DB — no hook_failures

    class _NoTaxonomy:
        class _Tax:
            class _Conn:
                def execute(self, *a, **kw):
                    raise sqlite3.OperationalError("no such table: hook_failures")
                def commit(self):
                    pass
            conn = _Conn()
            class _Lock:
                def __enter__(self):
                    return None
                def __exit__(self, *a):
                    return False
            _lock = _Lock()
        taxonomy = _Tax()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mod, "t2_ctx", lambda: _NoTaxonomy())

    def bad(source_path, collection, content):
        raise RuntimeError("primary failure")

    register_post_document_hook(bad)
    fire_post_document_hooks("/path/z.md", "knowledge__delos", "x")  # must not raise


def test_fire_post_document_hooks_persist_failure_is_best_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If T2 access itself raises, the dispatcher contract still holds:
    ingest must not be blocked, and no exception propagates.
    """
    import nexus.mcp_infra as mod
    from nexus.mcp_infra import (
        fire_post_document_hooks,
        register_post_document_hook,
    )

    monkeypatch.setattr(mod, "t2_ctx", lambda: (_ for _ in ()).throw(
        RuntimeError("t2 offline"),
    ))

    def bad(source_path, collection, content):
        raise RuntimeError("primary failure")

    register_post_document_hook(bad)
    fire_post_document_hooks("/path/q.md", "knowledge__delos", "x")  # must not raise


# ── Migration sanity (4.14.2) ────────────────────────────────────────────────


def test_migration_4_14_2_adds_chain_column(tmp_path: Path) -> None:
    """After migrate_hook_failures_chain_column runs, ``hook_failures.chain``
    is present, NOT NULL, and defaults to 'single'.

    Applies the migration directly because the running package is still
    4.14.1 so T2Database's auto ``apply_pending`` stops one short.
    """
    from nexus.db.migrations import migrate_hook_failures_chain_column

    db_path = tmp_path / "post_migrate.db"
    T2Database(db_path).close()  # base schema through 4.14.1
    raw = sqlite3.connect(str(db_path))
    migrate_hook_failures_chain_column(raw)
    cols = {r[1]: r for r in raw.execute("PRAGMA table_info(hook_failures)").fetchall()}
    raw.close()
    assert "chain" in cols
    # cid, name, type, notnull, dflt_value, pk
    assert cols["chain"][2] == "TEXT"
    assert cols["chain"][3] == 1  # NOT NULL
    # Default literal in PRAGMA may be quoted: 'single' or "single"
    assert cols["chain"][4].strip("'\"") == "single"


def test_migration_4_14_2_backfills_historical_batch_rows(tmp_path: Path) -> None:
    """The data migration sets chain='batch' WHERE is_batch=1 — historical
    batch failures captured under RDR-095 (4.14.1 schema) are correctly
    classified after 4.14.2 lands.
    """
    from nexus.db.migrations import (
        migrate_hook_failures,
        migrate_hook_failures_batch_columns,
        migrate_hook_failures_chain_column,
    )

    db_path = tmp_path / "backfill.db"
    raw = sqlite3.connect(str(db_path))
    migrate_hook_failures(raw)
    migrate_hook_failures_batch_columns(raw)

    # Insert a historical batch row at the 4.14.1 schema.
    raw.execute(
        "INSERT INTO hook_failures "
        "(doc_id, collection, hook_name, error, batch_doc_ids, is_batch) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        ("d1", "code__nexus", "old_hook", "old failure", '["d1","d2"]'),
    )
    # Insert a historical scalar (single) row.
    raw.execute(
        "INSERT INTO hook_failures "
        "(doc_id, collection, hook_name, error) VALUES (?, ?, ?, ?)",
        ("d3", "code__nexus", "old_single", "scalar failure"),
    )
    raw.commit()

    # Apply the new migration.
    migrate_hook_failures_chain_column(raw)

    rows = dict(raw.execute(
        "SELECT doc_id, chain FROM hook_failures"
    ).fetchall())
    raw.close()
    assert rows == {"d1": "batch", "d3": "single"}


def test_migration_4_14_2_idempotent(tmp_path: Path) -> None:
    """Re-applying the migration must not raise (no duplicate ADD COLUMN)."""
    from nexus.db.migrations import (
        migrate_hook_failures,
        migrate_hook_failures_batch_columns,
        migrate_hook_failures_chain_column,
    )

    db_path = tmp_path / "idempotent.db"
    raw = sqlite3.connect(str(db_path))
    migrate_hook_failures(raw)
    migrate_hook_failures_batch_columns(raw)
    migrate_hook_failures_chain_column(raw)
    # Second invocation must be a no-op.
    migrate_hook_failures_chain_column(raw)
    raw.close()


def test_migration_4_14_2_no_op_when_table_missing(tmp_path: Path) -> None:
    """If hook_failures has not been created (impossible in practice given
    migration order, but defensively), the migration must not raise."""
    from nexus.db.migrations import migrate_hook_failures_chain_column

    db_path = tmp_path / "no_table.db"
    raw = sqlite3.connect(str(db_path))
    migrate_hook_failures_chain_column(raw)
    raw.close()


# ── Existing chains write 'chain' value ──────────────────────────────────────


def test_record_hook_failure_writes_chain_single(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The single-doc chain's failure-capture path now populates
    ``chain='single'`` alongside the existing scalar columns.

    Applies migration manually for the same reason as the document-chain
    test above (package version still 4.14.1).
    """
    import nexus.mcp_infra as mod
    from nexus.db.migrations import migrate_hook_failures_chain_column
    from nexus.mcp_infra import fire_post_store_hooks, register_post_store_hook

    db_path = tmp_path / "single_chain.db"
    T2Database(db_path).close()
    raw = sqlite3.connect(str(db_path))
    migrate_hook_failures_chain_column(raw)
    raw.close()
    monkeypatch.setattr(mod, "t2_ctx", lambda: T2Database(db_path))

    # Clean existing single-chain registrations from prior test imports.
    mod._post_store_hooks.clear()

    def bad(doc_id, collection, content):
        raise RuntimeError("single boom")

    register_post_store_hook(bad)
    fire_post_store_hooks("doc-1", "knowledge__delos", "content")

    with T2Database(db_path) as db:
        row = db.taxonomy.conn.execute(
            "SELECT doc_id, chain FROM hook_failures"
        ).fetchone()
    assert row == ("doc-1", "single")
    mod._post_store_hooks.clear()


def test_record_batch_hook_failure_writes_chain_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The batch chain's failure-capture path now populates
    ``chain='batch'`` alongside ``is_batch=1`` (dual-write for back-compat).
    """
    import nexus.mcp_infra as mod
    from nexus.db.migrations import migrate_hook_failures_chain_column
    from nexus.mcp_infra import (
        fire_post_store_batch_hooks,
        register_post_store_batch_hook,
    )

    db_path = tmp_path / "batch_chain.db"
    T2Database(db_path).close()
    raw = sqlite3.connect(str(db_path))
    migrate_hook_failures_chain_column(raw)
    raw.close()
    monkeypatch.setattr(mod, "t2_ctx", lambda: T2Database(db_path))

    mod._post_store_batch_hooks.clear()

    def bad(doc_ids, collection, contents, embeddings, metadatas):
        raise RuntimeError("batch boom")

    register_post_store_batch_hook(bad)
    fire_post_store_batch_hooks(
        ["d1", "d2"], "knowledge__delos", ["c1", "c2"], None, None,
    )

    with T2Database(db_path) as db:
        row = db.taxonomy.conn.execute(
            "SELECT doc_id, batch_doc_ids, is_batch, chain FROM hook_failures"
        ).fetchone()
    import json as _json
    assert row[0] == "d1"
    # Decoupled from json serialization whitespace: parse and compare the
    # list contents rather than the raw string form.
    assert _json.loads(row[1]) == ["d1", "d2"]
    assert row[2] == 1
    assert row[3] == "batch"
    mod._post_store_batch_hooks.clear()
