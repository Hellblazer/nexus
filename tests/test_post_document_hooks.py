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

Post-RDR-118-successor refactor: the three hook chains live on
per-invocation ``HookRegistry`` instances (``nexus.hook_registry``)
rather than module-level globals on ``nexus.mcp_infra``. Each test
constructs its own registry and asserts against that instance directly.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from nexus.db.t2 import T2Database
from nexus.hook_registry import HookRegistry


# ── Registration + dispatch ──────────────────────────────────────────────────


def test_register_document_appends() -> None:
    """HookRegistry.register_document appends to the registry's internal list."""
    registry = HookRegistry()

    def probe(source_path, collection, content):
        return None

    assert registry._document == []
    registry.register_document(probe)
    assert registry._document == [probe]


def test_fire_document_calls_registered() -> None:
    """HookRegistry.fire_document invokes all registered callables in order."""
    registry = HookRegistry()
    seen: list[tuple] = []

    def hook_a(source_path, collection, content):
        seen.append(("a", source_path, collection, content))

    def hook_b(source_path, collection, content):
        seen.append(("b", source_path, collection, content))

    registry.register_document(hook_a)
    registry.register_document(hook_b)

    registry.fire_document("/path/to/doc.md", "knowledge__delos", "body text")

    assert seen == [
        ("a", "/path/to/doc.md", "knowledge__delos", "body text"),
        ("b", "/path/to/doc.md", "knowledge__delos", "body text"),
    ]


# ── Content-sourcing contract (audit F4) ─────────────────────────────────────


def test_fire_document_passes_content_through_when_populated() -> None:
    """When ``content`` is non-empty (MCP path), it reaches the hook
    untouched. The hook should NOT need to fall back to reading
    ``source_path`` itself.
    """
    registry = HookRegistry()
    captured: list[str] = []

    def hook(source_path, collection, content):
        # Content is primary: use it directly.
        captured.append(content)

    registry.register_document(hook)
    registry.fire_document("/path/x.md", "knowledge__delos", "FULL TEXT")

    assert captured == ["FULL TEXT"]


def test_fire_document_passes_empty_content_signal_for_cli_path(tmp_path: Path) -> None:
    """CLI sites pass ``content=""`` as the contract signal that the hook
    may need to read ``source_path`` itself. The framework forwards both
    parameters as-is — content-sourcing is hook responsibility, not
    framework responsibility.
    """
    src = tmp_path / "doc.md"
    src.write_text("body read from disk")

    registry = HookRegistry()
    captured: list[str] = []

    def hook(source_path, collection, content):
        # Hook honors the contract: empty content => read source_path.
        if not content:
            captured.append(Path(source_path).read_text())
        else:
            captured.append(content)

    registry.register_document(hook)
    registry.fire_document(str(src), "knowledge__delos", "")

    assert captured == ["body read from disk"]


# ── Async/sync contract (RDR-089 load-bearing) ───────────────────────────────


def test_register_document_rejects_async_hooks() -> None:
    """The dispatcher is synchronous all the way down — RDR-089 load-bearing
    contract. The RDR-118 P2.S1b carryover tightens this contract:
    registration raises ``TypeError`` on coroutine-returning callables.
    The legacy dispatcher accepted async hooks and silently dropped the
    returned coroutine (audit F1 silent-failure mode); registration now
    surfaces the contract violation where the diagnostic points at the
    buggy caller.
    """
    registry = HookRegistry()

    async def async_hook(source_path, collection, content):
        return None

    with pytest.raises(TypeError, match="async callables are not supported"):
        registry.register_document(async_hook)


# ── Failure isolation ────────────────────────────────────────────────────────


def test_fire_document_exception_nonfatal() -> None:
    """A raising hook must not block the next registered hook from firing,
    and the dispatcher itself must never raise.
    """
    registry = HookRegistry()
    survived: list[str] = []

    def raising(source_path, collection, content):
        raise RuntimeError("simulated document-hook failure")

    def survivor(source_path, collection, content):
        survived.append(source_path)

    registry.register_document(raising)
    registry.register_document(survivor)

    registry.fire_document("/path/y.md", "knowledge__delos", "x")

    assert survived == ["/path/y.md"]


def test_fire_document_persist_swallowed_when_store_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nexus-9613q.3: if the telemetry store's record_hook_failure raises
    (e.g. an extreme legacy DB missing the table, or a service 5xx), ingest is
    never blocked — the persist failure is caught (and warned once), the
    original hook exception is not masked, and fire_document does not raise.
    """
    import nexus.hook_registry as hr
    import nexus.mcp_infra as mod
    from contextlib import contextmanager

    hr._hook_failure_drop_warned.discard(("document", "bad"))

    class _BoomTelemetry:
        def record_hook_failure(self, **kwargs):
            raise sqlite3.OperationalError("no such table: hook_failures")

    class _FakeT2:
        telemetry = _BoomTelemetry()

    @contextmanager
    def _fake_t2_ctx():
        yield _FakeT2()

    monkeypatch.setattr(mod, "t2_ctx", _fake_t2_ctx)

    def bad(source_path, collection, content):
        raise RuntimeError("primary failure")

    registry = HookRegistry()
    registry.register_document(bad)
    registry.fire_document("/path/z.md", "knowledge__delos", "x")  # must not raise
    assert ("document", "bad") in hr._hook_failure_drop_warned


def test_fire_document_persist_failure_is_best_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If T2 access itself raises, the dispatcher contract still holds:
    ingest must not be blocked, and no exception propagates.
    """
    import nexus.mcp_infra as mod

    monkeypatch.setattr(mod, "t2_ctx", lambda: (_ for _ in ()).throw(
        RuntimeError("t2 offline"),
    ))

    def bad(source_path, collection, content):
        raise RuntimeError("primary failure")

    registry = HookRegistry()
    registry.register_document(bad)
    registry.fire_document("/path/q.md", "knowledge__delos", "x")  # must not raise


# ── Migration sanity (4.14.2) DELETED (RDR-158 P4 Stage 4, nexus-i711w) ──────
# The four tests here exercised migrate_hook_failures* directly; the
# migration functions died with nexus/db/migrations.py.


# ── nexus-w8lg1: document chain must carry the CATALOG doc_id ────────────────


def test_fire_store_chains_forwards_catalog_doc_id_to_document_chain() -> None:
    """nexus-w8lg1 (6.3.0 live shakeout finding #1): fire_store_chains
    passed the T3 chunk id (chunk_text_hash[:32]) as the document chain's
    doc_id instead of the catalog tumbler. The aspect-enqueue hook then
    shipped that chunk hash to the engine, violating the composite FK
    aspect_extraction_queue(tenant_id, doc_id) -> catalog_documents
    (tenant_id, tumbler) — SQLSTATE 23503, surfaced as a typed 409, and
    the note silently never got aspects."""
    registry = HookRegistry()
    seen: list[dict] = []

    def doc_id_aware(source_path, collection, content, *, doc_id=""):
        seen.append({"source_path": source_path, "doc_id": doc_id})

    registry.register_document(doc_id_aware)

    chunk_id = "bf715bbd" + "0" * 24  # chunk_text_hash[:32] shape
    registry.fire_store_chains(
        [chunk_id], "knowledge__delos", ["note body"],
        catalog_doc_id="1.7.42",
    )

    assert seen == [{"source_path": chunk_id, "doc_id": "1.7.42"}]


def test_fire_store_chains_empty_catalog_doc_id_stays_empty() -> None:
    """No catalog entry (catalog absent) -> doc_id must stay "" so the
    enqueue persists NULL, which the nullable FK accepts."""
    registry = HookRegistry()
    seen: list[str] = []

    def doc_id_aware(source_path, collection, content, *, doc_id=""):
        seen.append(doc_id)

    registry.register_document(doc_id_aware)
    registry.fire_store_chains(["c" * 32], "knowledge__delos", ["body"])
    assert seen == [""]
