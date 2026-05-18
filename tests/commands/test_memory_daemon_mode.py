# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-112 P4.1 (nexus-idqd) — ``nx memory`` CLI under ``NX_STORAGE_MODE=daemon``.

Spawns a real ``T2Daemon`` in-thread (and a real ``T3Daemon`` subprocess
for ``promote``), then invokes each ``nx memory`` subcommand via
``CliRunner`` with the daemon-mode env in place. Verifies the action
lands in the daemon's storage via a direct facade read on the same
``T2Database`` instance the daemon owns.

Why this file exists: the vm3t lesson. Under ``NX_STORAGE_MODE=daemon``
every memory subcommand routes through ``mcp_infra.t2_ctx()`` (and
``promote`` additionally through ``mcp_infra.get_t3()``). Unit tests
that mock those seams cannot detect call-site breakage of the form
"facade method missing on the client class" — only an end-to-end
invocation does. See ``tests/daemon/test_t2_client_facade.py`` for the
matching contract-level guard on the T2Client side.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.daemon.t2_daemon import T2Daemon
from nexus.db.t2 import T2Database


# ── In-thread T2 daemon harness ─────────────────────────────────────────────


def _run_daemon(daemon: T2Daemon) -> asyncio.AbstractEventLoop:
    started = threading.Event()
    loop = asyncio.new_event_loop()

    def _thread() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(daemon.start())
        started.set()
        loop.run_forever()

    t = threading.Thread(target=_thread, daemon=True)
    t.start()
    started.wait(timeout=5.0)
    return loop


def _stop_daemon(daemon: T2Daemon, loop: asyncio.AbstractEventLoop) -> None:
    asyncio.run_coroutine_threadsafe(daemon.stop(), loop).result(timeout=5.0)
    loop.call_soon_threadsafe(loop.stop)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def reset_t3_singleton():
    """``get_t3`` caches a process-wide singleton — reset before + after each
    test so daemon-mode + direct-mode tests don't pollute each other.
    """
    import nexus.mcp_infra as infra
    original = infra._t3_instance
    infra._t3_instance = None
    yield
    infra._t3_instance = original


@pytest.fixture
def t2db(tmp_path: Path) -> T2Database:
    db = T2Database(tmp_path / "memory.db")
    yield db
    db.close()


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    cd = tmp_path / "nexus_config"
    cd.mkdir()
    return cd


@pytest.fixture
def daemon_env(monkeypatch, config_dir: Path):
    """Set ``NX_STORAGE_MODE=daemon`` + ``NEXUS_CONFIG_DIR`` so ``t2_ctx``
    resolves to the live daemon via the discovery file."""
    monkeypatch.setenv("NX_STORAGE_MODE", "daemon")
    monkeypatch.setenv("NX_LOCAL", "1")
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))


@pytest.fixture
def live_t2_daemon(t2db: T2Database, config_dir: Path, daemon_env):
    """Spawn a real T2Daemon bound to ``t2db`` and write the discovery
    file under ``config_dir``. The CLI discovers it via env-var lookup."""
    daemon = T2Daemon(config_dir, t2db=t2db)
    loop = _run_daemon(daemon)
    try:
        yield daemon
    finally:
        _stop_daemon(daemon, loop)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ── memory put / get / search / list / delete / expire ──────────────────────


class TestMemoryPut:
    def test_put_routes_through_daemon(
        self, runner: CliRunner, live_t2_daemon, t2db: T2Database
    ) -> None:
        result = runner.invoke(
            main,
            ["memory", "put", "hello idqd", "-p", "idqd-test", "-t", "note.md"],
        )
        assert result.exit_code == 0, result.output
        assert "Stored: idqd-test/note.md" in result.output
        entry = t2db.get(project="idqd-test", title="note.md")
        assert entry is not None
        assert entry["content"] == "hello idqd"


class TestMemoryGet:
    def test_get_by_project_title(
        self, runner: CliRunner, live_t2_daemon, t2db: T2Database
    ) -> None:
        t2db.put(project="idqd-test", title="alpha.md", content="alpha body")
        result = runner.invoke(
            main, ["memory", "get", "-p", "idqd-test", "-t", "alpha.md"]
        )
        assert result.exit_code == 0, result.output
        assert "alpha body" in result.output

    def test_get_by_id(
        self, runner: CliRunner, live_t2_daemon, t2db: T2Database
    ) -> None:
        row_id = t2db.put(project="idqd-test", title="bravo.md", content="bravo body")
        result = runner.invoke(main, ["memory", "get", str(row_id)])
        assert result.exit_code == 0, result.output
        assert "bravo body" in result.output

    def test_get_unique_prefix_match(
        self, runner: CliRunner, live_t2_daemon, t2db: T2Database
    ) -> None:
        t2db.put(
            project="idqd-test",
            title="088-research-1: full",
            content="prefix body",
        )
        result = runner.invoke(
            main, ["memory", "get", "-p", "idqd-test", "-t", "088-research-1"]
        )
        assert result.exit_code == 0, result.output
        assert "prefix body" in result.output


class TestMemorySearch:
    def test_search_routes_through_daemon(
        self, runner: CliRunner, live_t2_daemon, t2db: T2Database
    ) -> None:
        t2db.put(project="idqd-test", title="charlie.md", content="zenithpony token")
        result = runner.invoke(main, ["memory", "search", "zenithpony"])
        assert result.exit_code == 0, result.output
        assert "charlie.md" in result.output


class TestMemoryList:
    def test_list_routes_through_daemon(
        self, runner: CliRunner, live_t2_daemon, t2db: T2Database
    ) -> None:
        t2db.put(project="idqd-test", title="delta.md", content="d")
        t2db.put(project="idqd-test", title="echo.md", content="e")
        result = runner.invoke(main, ["memory", "list", "-p", "idqd-test"])
        assert result.exit_code == 0, result.output
        assert "delta.md" in result.output
        assert "echo.md" in result.output


class TestMemoryDelete:
    def test_delete_by_id(
        self, runner: CliRunner, live_t2_daemon, t2db: T2Database
    ) -> None:
        row_id = t2db.put(
            project="idqd-test", title="todelete.md", content="bye"
        )
        result = runner.invoke(
            main, ["memory", "delete", "--id", str(row_id), "--yes"]
        )
        assert result.exit_code == 0, result.output
        assert t2db.get(id=row_id) is None

    def test_delete_by_project_title(
        self, runner: CliRunner, live_t2_daemon, t2db: T2Database
    ) -> None:
        t2db.put(project="idqd-test", title="rmv.md", content="bye")
        result = runner.invoke(
            main,
            ["memory", "delete", "-p", "idqd-test", "-t", "rmv.md", "--yes"],
        )
        assert result.exit_code == 0, result.output
        assert t2db.get(project="idqd-test", title="rmv.md") is None


class TestMemoryExpire:
    def test_expire_routes_through_daemon(
        self, runner: CliRunner, live_t2_daemon, t2db: T2Database
    ) -> None:
        # No expirable entries yet; expire() returns 0 cleanly.
        result = runner.invoke(main, ["memory", "expire"])
        assert result.exit_code == 0, result.output
        assert "Expired 0" in result.output


# ── memory promote (T3 too) ────────────────────────────────────────────────


@pytest.fixture
def local_path(tmp_path: Path) -> Path:
    p = tmp_path / "chroma_t3"
    p.mkdir()
    return p


@pytest.fixture
def live_t3_daemon(daemon_env, config_dir: Path, local_path: Path):
    """Spawn the real T3 chroma subprocess; the CLI's promote path
    must route through ``mcp_infra.get_t3()`` -> ``make_t3_client``
    -> ``chromadb.HttpClient`` -> the daemon."""
    from nexus.daemon.t3_daemon import start_t3_daemon, stop_t3_daemon

    payload = start_t3_daemon(config_dir=config_dir, local_path=local_path)
    try:
        yield payload
    finally:
        stop_t3_daemon(config_dir=config_dir)


class TestMemoryPromote:
    def test_promote_routes_t3_through_daemon(
        self,
        runner: CliRunner,
        live_t2_daemon,
        live_t3_daemon,
        reset_t3_singleton,
        t2db: T2Database,
    ) -> None:
        # Seed a T2 entry, then promote it. The T3 write must land in the
        # daemon's chroma instance (HttpClient under the hood), not in a
        # PersistentClient racing the daemon on the same on-disk path.
        row_id = t2db.put(
            project="idqd-test",
            title="promote-me.md",
            content="content for promotion smoke",
        )
        result = runner.invoke(
            main,
            [
                "memory",
                "promote",
                str(row_id),
                "--collection",
                "knowledge__idqd_promote_smoke",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Promoted:" in result.output

        # Verify the chunk is reachable via the daemon's chroma. We connect
        # via HttpClient ourselves (mirrors what make_t3_client does) so
        # the test does not depend on the singleton's mode-cached instance.
        import chromadb

        client = chromadb.HttpClient(
            host=live_t3_daemon["tcp_host"],
            port=live_t3_daemon["tcp_port"],
        )
        collection_names = {c.name for c in client.list_collections()}
        # ``t3_collection_name`` normalises underscores in the
        # user-supplied portion to dashes (ChromaDB-name discipline)
        # and auto-promotes 2-segment "knowledge__name" to a conformant
        # "knowledge__name-with-dashes__<embedder>__v1". Match the
        # normalised stub.
        promoted_match = any(
            name.startswith("knowledge__idqd-promote-smoke")
            for name in collection_names
        )
        assert promoted_match, (
            f"expected a knowledge__idqd-promote-smoke collection in the "
            f"daemon, got {collection_names}"
        )
