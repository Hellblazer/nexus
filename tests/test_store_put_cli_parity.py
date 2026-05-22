# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-9099: every T3-write CLI path must fire the post-store hook chains.

RDR-095 established the symmetric-fire principle for `nx index *` ingest
paths but `nx store put`, `nx memory promote`, and `nx store import`
were never retrofitted. Symptoms: chash_index missing, taxonomy never
assigned, aspect-extraction queue never enqueues — silent drift between
the catalog row + chroma chunk and the downstream T2 indexes that
operator SQL fast paths depend on.

Post-RDR-118 successor refactor: the hook chains live on per-invocation
``HookRegistry`` instances rather than module-level globals. The CLI
commands construct their own registry per invocation, so end-to-end
parity tests probe the registry that the command code path constructs.
We achieve that by monkeypatching ``HookRegistry`` to a subclass that
appends to test-side lists on every dispatch.

Two kinds of test:

  * Per-path firing tests — each broken CLI path now fires single,
    batch, and document chains in the same shape as MCP ``store_put``.
  * Drift guard — count of ``HookRegistry.fire_store_chains`` /
    ``hooks.fire_store_chains`` call sites in CLI ingest commands must
    not regress (catches future paths added without the chain wiring).
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner


def _make_stub_t3():
    """Stub T3Database for CLI helpers — deterministic doc_id, no chroma."""
    import hashlib

    class _StubT3:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def put(self, *, collection, content, title="", **kwargs):
            # Mirrors T3Database.put: chunk_text_hash[:32] (RDR-108 D1).
            return hashlib.sha256(content.encode()).hexdigest()[:32]

    return lambda: _StubT3()


def _install_recording_registry(monkeypatch):
    """Patch ``HookRegistry`` to record every dispatch on test-side lists.

    Returns ``(single, batch, doc)`` lists the test asserts against.
    """
    from nexus import hook_registry as _hr

    single: list = []
    batch: list = []
    doc: list = []

    class _RecordingRegistry(_hr.HookRegistry):
        def fire_single(self, doc_id, collection, content):  # type: ignore[override]
            single.append(doc_id)
            super().fire_single(doc_id, collection, content)

        def fire_batch(self, doc_ids, collection, contents, embeddings=None,
                       metadatas=None, *, catalog_doc_id=""):  # type: ignore[override]
            batch.append(list(doc_ids))
            super().fire_batch(doc_ids, collection, contents, embeddings,
                               metadatas, catalog_doc_id=catalog_doc_id)

        def fire_document(self, source_path, collection, content, *, doc_id=""):  # type: ignore[override]
            doc.append(source_path)
            super().fire_document(source_path, collection, content, doc_id=doc_id)

    monkeypatch.setattr(_hr, "HookRegistry", _RecordingRegistry)
    return single, batch, doc


# ── nx store put ────────────────────────────────────────────────────────────


class TestStorePutCli:
    """``nx store put`` fires single, batch, document chains identically to MCP."""

    def test_fires_all_three_chains_in_one_invocation(self, monkeypatch, tmp_path):
        from nexus.cli import main

        single, batch, doc = _install_recording_registry(monkeypatch)

        f = tmp_path / "doc.md"
        f.write_text("body for store put parity")

        with patch("nexus.commands.store._t3", _make_stub_t3()):
            runner = CliRunner()
            result = runner.invoke(main, [
                "store", "put", str(f),
                "--collection", "knowledge",
                "--title", "parity-store-put",
            ])

        assert result.exit_code == 0, result.output
        assert len(single) == 1
        assert len(batch) == 1
        assert len(batch[0]) == 1
        assert len(doc) == 1
        # All chains see the same doc_id derived from content.
        assert single[0] == batch[0][0]


# ── nx memory promote ──────────────────────────────────────────────────────


class TestMemoryPromoteCli:
    """``nx memory promote`` fires the chains when promoting T2 → T3."""

    def test_fires_all_three_chains_when_promoting(
        self, monkeypatch, tmp_path,
    ):
        from nexus.cli import main
        from nexus.db.t2 import T2Database

        # T2 environment: a memory entry to promote. ``memory promote``
        # takes the integer entry_id as its positional argument.
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(
            "nexus.config.is_local_mode", lambda: True,
        )
        db_path = tmp_path / "memory.db"
        db = T2Database(db_path)
        entry_id = db.put(
            project="proj-test", title="m-1", content="memory body",
            tags="", ttl=None,
        )

        # RDR-120 P6 follow-up (nexus-w6txl): ``memory promote`` (and
        # every other nx memory command) now routes through
        # ``t2_handle()`` -> ``T2Client``. Tests inject a T2Database
        # by patching the helper to return the test fixture as the
        # context-managed handle.
        single, batch, doc = _install_recording_registry(monkeypatch)

        with (
            patch("nexus.db.make_t3", _make_stub_t3()),
            patch("nexus.commands.memory.t2_handle", return_value=db),
        ):
            runner = CliRunner()
            result = runner.invoke(main, [
                "memory", "promote", str(entry_id),
                "--collection", "knowledge__memory",
            ])

        assert result.exit_code == 0, result.output
        assert len(single) == 1, f"single chain not fired: {result.output}"
        assert len(batch) == 1, f"batch chain not fired: {result.output}"
        assert len(doc) == 1, f"document chain not fired: {result.output}"


# ── nx store import ────────────────────────────────────────────────────────


class TestStoreImportCli:
    """``nx store import`` fires the chains for the imported batch."""

    def test_fires_chains_with_full_batch(self, monkeypatch, tmp_path):
        """A 3-record export imports as a 3-element batch on the chains."""
        import gzip
        import json

        import msgpack

        from nexus.cli import main

        # Synthesize a minimal .nxexp file: one header line + msgpack body.
        export_path = tmp_path / "test.nxexp"
        records = [
            {
                "id": f"id-{i}",
                "document": f"content {i}",
                "metadata": {"source_path": f"/x/doc-{i}.md"},
                # 1024-dim float32 zero embedding (matches voyage-context-3
                # collections' expected dim; size is what _validate uses,
                # not the actual values).
                "embedding": (b"\x00\x00\x00\x00" * 1024),
            }
            for i in range(3)
        ]
        with open(export_path, "wb") as f:
            header = {
                "format_version": 1,
                "collection_name": "knowledge__import_test",
                "embedding_model": "voyage-context-3",
            }
            f.write(json.dumps(header).encode() + b"\n")
            with gzip.GzipFile(fileobj=f, mode="wb") as gz:
                packer = msgpack.Packer()
                for rec in records:
                    gz.write(packer.pack(rec))

        single, batch, doc = _install_recording_registry(monkeypatch)

        # Stub T3.upsert_chunks_with_embeddings so we don't write to chroma.
        upsert_calls: list[dict] = []

        class _StubT3:
            def upsert_chunks_with_embeddings(self, **kwargs):
                upsert_calls.append(kwargs)

        with patch("nexus.commands.store._t3", lambda: _StubT3()):
            runner = CliRunner()
            result = runner.invoke(main, [
                "store", "import", str(export_path),
            ])

        assert result.exit_code == 0, result.output
        assert len(upsert_calls) == 1
        # Every record fires the single + document chains;
        # the batch fires once with all 3.
        assert len(single) == 3
        assert len(batch) == 1 and len(batch[0]) == 3
        assert len(doc) == 3
        # Document chain sees the source_path field, not the doc_id.
        assert sorted(doc) == ["/x/doc-0.md", "/x/doc-1.md", "/x/doc-2.md"]


# ── Drift guard: ensure new T3-write CLI paths don't skip the helper ───────


class TestDriftGuard:
    """AST-level guard: every T3-write CLI command fires the hook chains.

    Catches the regression shape that produced nexus-9099: a new CLI
    command that calls T3.put() / upsert_chunks_with_embeddings() but
    forgets to fire the post-store chains.
    """

    def test_known_t3_write_paths_use_fire_store_chains(self):
        """The three known broken paths now reference fire_store_chains."""
        store_py = Path("src/nexus/commands/store.py").read_text()
        memory_py = Path("src/nexus/commands/memory.py").read_text()
        exporter_py = Path("src/nexus/exporter.py").read_text()

        assert "fire_store_chains" in store_py, (
            "src/nexus/commands/store.py must call HookRegistry.fire_store_chains "
            "from put_cmd (nexus-9099 regression)"
        )
        assert "fire_store_chains" in memory_py, (
            "src/nexus/commands/memory.py must call HookRegistry.fire_store_chains "
            "from promote (nexus-9099 regression)"
        )
        assert "fire_store_chains" in exporter_py, (
            "src/nexus/exporter.py must call HookRegistry.fire_store_chains "
            "from import_collection (nexus-9099 regression)"
        )

    def test_fire_store_chains_called_after_t3_put_in_put_cmd(self):
        """In commands/store.py:put_cmd, fire_store_chains must follow t3.put."""
        src = Path("src/nexus/commands/store.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "put_cmd":
                names = [
                    n.func.attr if isinstance(n.func, ast.Attribute)
                    else (n.func.id if isinstance(n.func, ast.Name) else "")
                    for n in ast.walk(node) if isinstance(n, ast.Call)
                ]
                assert "fire_store_chains" in names, (
                    "put_cmd must call fire_store_chains after t3.put"
                )
                return
        pytest.fail("put_cmd not found in commands/store.py")
