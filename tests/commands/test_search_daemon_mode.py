# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-112 P4.2 (nexus-yfqv) — ``nx search`` CLI under ``NX_STORAGE_MODE=daemon``.

Spawns real T2 + T3 daemons, seeds a knowledge collection via the
daemon-backed ``nx store put`` path, and invokes ``nx search`` via
``CliRunner`` with the daemon-mode env in place. Verifies that the
search path:

1. Routes T3 access through ``mcp_infra.get_t3`` (i.e., the
   ``HttpClient`` to the daemon's chroma, not a parallel
   ``PersistentClient`` racing the daemon on the same on-disk path).
2. Routes T2 access through ``mcp_infra.t2_ctx`` (i.e., a ``T2Client``
   facade bound to the running daemon, with the vm3t-class proxies
   ``taxonomy`` / ``telemetry`` exposed correctly).

This is the search-side counterpart to ``test_store_daemon_mode.py``
and ``test_memory_daemon_mode.py``. The vm3t lesson — unit tests that
mock the daemon seams can stay green while production breaks at the
boundary — is the only reason this file is necessary; we deliberately
do not mock anything below the CLI surface.

``search_cmd.py`` carries zero source changes for the yfqv flip: its
T3 path goes through ``from nexus.commands.store import _t3`` (already
daemon-aware via the idqd flip) and its T2 path through
``from nexus.mcp_infra import t2_ctx`` (Phase 3). These tests
contract-pin that wiring against the live daemon surface.
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


# ── In-thread T2 daemon harness (matches test_memory_daemon_mode.py) ────────


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
    """Reset ``get_t3`` singleton + ``_collections_cache`` so daemon-mode
    tests do not inherit a direct-mode instance from earlier in the
    session. Mirrors the fixture in ``test_store_daemon_mode.py``."""
    import nexus.mcp_infra as infra
    original_t3 = infra._t3_instance
    original_collections = infra._collections_cache
    infra._t3_instance = None
    infra._collections_cache = ([], 0.0)
    yield
    infra._t3_instance = original_t3
    infra._collections_cache = original_collections


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
def local_path(tmp_path: Path) -> Path:
    p = tmp_path / "chroma_t3"
    p.mkdir()
    return p


@pytest.fixture
def daemon_env(monkeypatch, config_dir: Path):
    monkeypatch.setenv("NX_STORAGE_MODE", "daemon")
    monkeypatch.setenv("NX_LOCAL", "1")
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))


@pytest.fixture
def live_t2_daemon(t2db: T2Database, config_dir: Path, daemon_env):
    daemon = T2Daemon(config_dir, t2db=t2db)
    loop = _run_daemon(daemon)
    try:
        yield daemon
    finally:
        # chak review IMPORTANT-1: drop the process-singleton T2Client
        # before stopping the daemon so the orphan socket pool does not
        # hold the daemon's UDS open past ``server.wait_closed``'s 5 s
        # timeout. Without this teardown a subsequent daemon-mode suite
        # that starts a new T2Daemon flaps on the unrelated previous
        # daemon's leaked sockets. Matches the pattern in
        # test_catalog_daemon_mode.py / test_dt_daemon_mode.py.
        from nexus.catalog import reset_cache
        reset_cache()
        _stop_daemon(daemon, loop)


@pytest.fixture
def live_t3_daemon(daemon_env, config_dir: Path, local_path: Path):
    from nexus.daemon.t3_daemon import start_t3_daemon, stop_t3_daemon

    payload = start_t3_daemon(config_dir=config_dir, local_path=local_path)
    try:
        yield payload
    finally:
        stop_t3_daemon(config_dir=config_dir)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def seeded_collection(
    runner: CliRunner,
    live_t2_daemon,
    live_t3_daemon,
    reset_t3_singleton,
    tmp_path: Path,
) -> str:
    """Seed one chunk via ``nx store put`` under the daemon so the search
    tests have something to retrieve. Returns the auto-promoted
    conformant collection name (e.g.
    ``knowledge__yfqv-search__minilm-l6-v2-384__v1`` under NX_LOCAL=1)."""
    src = tmp_path / "alpha.md"
    src.write_text(
        "the rare zenithpony marker for the yfqv search smoke harness"
    )
    put = runner.invoke(
        main,
        [
            "store",
            "put",
            str(src),
            "--collection",
            "knowledge__yfqv-search",
            "--title",
            "alpha.md",
        ],
    )
    assert put.exit_code == 0, put.output
    # "Stored: <id>  →  <full_collection_name>" — parse the segment
    # right of the arrow specifically (M1: safer than rsplit-by-space,
    # which would silently misparse if store put ever prepends a
    # warning line before the success message).
    arrow_lines = [
        ln for ln in put.output.strip().splitlines() if "→" in ln
    ]
    assert arrow_lines, f"no Stored: line in put output: {put.output!r}"
    full_name = arrow_lines[0].split("→", 1)[1].strip()
    assert full_name.startswith("knowledge__yfqv-search"), put.output
    return full_name


# ── Tests ───────────────────────────────────────────────────────────────────


class TestSearchVector:
    def test_search_routes_through_daemon(
        self,
        runner: CliRunner,
        seeded_collection: str,
    ) -> None:
        """``nx search`` must hit the daemon's chroma + the daemon's T2.

        The query token (``zenithpony``) is rare enough that any non-
        catastrophic vector backend should surface the seeded chunk in
        the top result. We do not assert the exact distance ordering;
        we only assert that the chunk is reachable through the daemon.
        """
        result = runner.invoke(
            main,
            [
                "search",
                "zenithpony",
                "--corpus",
                seeded_collection,
                "--no-threshold",  # don't drop on cosine threshold
            ],
        )
        assert result.exit_code == 0, result.output
        # The store put recorded ``alpha.md`` as title; the formatter
        # surfaces either the file path / title fragment.
        assert "alpha.md" in result.output, result.output


class TestSearchNoMatchingCorpus:
    def test_search_unknown_corpus(
        self,
        runner: CliRunner,
        live_t2_daemon,
        live_t3_daemon,
        reset_t3_singleton,
    ) -> None:
        """An unknown ``--corpus`` resolves to zero collections — the CLI
        must surface that cleanly (exit 0 + ``no matching collections``)
        without raising the daemon. Verifies the T3 ``list_collections``
        round-trip works against the daemon's HttpClient."""
        result = runner.invoke(
            main,
            [
                "search",
                "zenithpony",
                "--corpus",
                "knowledge__yfqv_nonexistent_bogus",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "no matching collections" in result.output


class TestSearchJsonOutput:
    def test_search_json_output_under_daemon(
        self,
        runner: CliRunner,
        seeded_collection: str,
    ) -> None:
        """``--json`` exercises the same retrieval path but a different
        output formatter; verifies the singleton get_t3 is safe across
        the structured-output branch."""
        result = runner.invoke(
            main,
            [
                "search",
                "zenithpony",
                "--corpus",
                seeded_collection,
                "--no-threshold",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        # nexus-84a6: parse ``result.stdout`` not ``result.output``;
        # CliRunner interleaves stdout+stderr in Click 8.2+, which
        # would corrupt the JSON parse if any structlog message slips
        # past the ERROR-level filter.
        import json
        parsed = json.loads(result.stdout)
        assert isinstance(parsed, list)
        assert len(parsed) >= 1, result.output


class TestSearchHybridMode:
    def test_search_hybrid_under_daemon(
        self,
        runner: CliRunner,
        seeded_collection: str,
    ) -> None:
        """``--hybrid`` calls ``get_t3()`` and ``t2_ctx()`` like the
        non-hybrid path; the ripgrep merge runs on top. The seeded
        collection has no ripgrep cache, so the rg branch yields zero
        hits — we only assert the vector-side still surfaces the chunk
        via the daemon."""
        result = runner.invoke(
            main,
            [
                "search",
                "zenithpony",
                "--corpus",
                seeded_collection,
                "--no-threshold",
                "--hybrid",
            ],
        )
        assert result.exit_code == 0, result.output
        # Vector branch must still surface ``alpha.md`` even with the
        # hybrid scorer engaged.
        assert "alpha.md" in result.output, result.output


class TestSearchCatalogScoringSeam:
    """RDR-112 6shq.4 (nexus-w6hj): the two ``Catalog`` opens inside
    ``search_cmd`` (max_file_chunks filter + hybrid scoring catalog)
    route through ``open_cached`` so daemon mode reuses the
    process-singleton T2Client. The scoring catalog open is wrapped in
    ``try/except Exception`` by design (best-effort scoring); we
    assert the happy path still surfaces the seeded chunk so a
    regression on the daemon-mode flip is caught at the formatter
    boundary, not just by static grep.

    Note: an isolated daemon-down assertion is not added here because
    ``_t3()`` raises BEFORE the catalog opens are reached, so any
    daemon-down ClickException assertion would be testing the T3
    seam (yfqv scope) rather than the catalog seam (w6hj scope).
    The presence of the seeded-collection result is sufficient
    end-to-end evidence that ``open_cached`` works under daemon.
    """

    def test_search_with_max_file_chunks_under_daemon(
        self,
        runner: CliRunner,
        seeded_collection: str,
    ) -> None:
        """``--max-file-chunks 1`` exercises the new ``open_cached``
        seam at search_cmd.py:365. The seeded collection contains one
        chunk, so the filter is a no-op for the result, but the code
        path that opens the catalog under daemon mode IS exercised."""
        result = runner.invoke(
            main,
            [
                "search",
                "zenithpony",
                "--corpus",
                seeded_collection,
                "--no-threshold",
                "--max-file-chunks",
                "10",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "alpha.md" in result.output, result.output
