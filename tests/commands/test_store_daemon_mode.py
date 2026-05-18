# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-112 P4.1 (nexus-idqd) — ``nx store`` CLI under ``NX_STORAGE_MODE=daemon``.

Spawns a real ``T3Daemon`` chroma subprocess via the discovery file,
invokes each ``nx store`` subcommand via ``CliRunner`` with the
daemon-mode env in place, and verifies the action lands in the daemon's
chroma (by reading back through an independent HttpClient).

Why this file exists: ``nexus.commands.store._t3()`` is the single
factory every store subcommand consumes. Under daemon mode it must
route through the same ``mcp_infra.get_t3()`` seam Phase 3 (nexus-hpxl)
wired up — otherwise we hit the vm3t class of bug, where unit tests
mocking ``_t3`` directly stay green while every real ``nx store ...``
invocation crashes or silently writes to a parallel PersistentClient
racing the daemon.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.cli import main


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def reset_t3_singleton():
    """``get_t3`` caches a process-wide singleton — reset before + after each
    test so the daemon-mode HttpClient does not leak into other tests.

    Also resets ``_collections_cache`` (60s TTL) so a prior direct-mode
    test's collection-name snapshot does not leak into a daemon-mode
    test that runs within the cache window (M1 from the idqd review).
    """
    import nexus.mcp_infra as infra
    original_t3 = infra._t3_instance
    original_collections = infra._collections_cache
    infra._t3_instance = None
    infra._collections_cache = ([], 0.0)
    yield
    infra._t3_instance = original_t3
    infra._collections_cache = original_collections


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
    """Set ``NX_STORAGE_MODE=daemon`` + ``NX_LOCAL=1`` + ``NEXUS_CONFIG_DIR``
    so ``get_t3`` resolves to the live T3 daemon."""
    monkeypatch.setenv("NX_STORAGE_MODE", "daemon")
    monkeypatch.setenv("NX_LOCAL", "1")
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))


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


def _direct_http_client(payload: dict):
    """An independent HttpClient pointed at the same daemon — used to
    verify state without relying on the test's get_t3 singleton."""
    import chromadb
    return chromadb.HttpClient(host=payload["tcp_host"], port=payload["tcp_port"])


# ── store put / list / get / delete / expire ───────────────────────────────


class TestStorePut:
    def test_put_routes_through_daemon(
        self,
        runner: CliRunner,
        live_t3_daemon,
        reset_t3_singleton,
        tmp_path: Path,
    ) -> None:
        src = tmp_path / "note.md"
        src.write_text("idqd store put smoke body")
        result = runner.invoke(
            main,
            [
                "store",
                "put",
                str(src),
                "--collection",
                "knowledge__idqd_store_put",
                "--title",
                "note.md",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Stored:" in result.output

        client = _direct_http_client(live_t3_daemon)
        names = {c.name for c in client.list_collections()}
        # ``t3_collection_name`` normalises underscores in the
        # user-supplied portion to dashes (ChromaDB-name discipline).
        assert any(
            n.startswith("knowledge__idqd-store-put") for n in names
        ), f"expected knowledge__idqd-store-put collection, got {names}"


class TestStoreList:
    def test_list_routes_through_daemon(
        self,
        runner: CliRunner,
        live_t3_daemon,
        reset_t3_singleton,
        tmp_path: Path,
    ) -> None:
        # Seed with one entry so list has something to show.
        src = tmp_path / "alpha.md"
        src.write_text("alpha body")
        put = runner.invoke(
            main,
            [
                "store",
                "put",
                str(src),
                "--collection",
                "knowledge__idqd_store_list",
                "--title",
                "alpha.md",
            ],
        )
        assert put.exit_code == 0, put.output

        result = runner.invoke(
            main,
            ["store", "list", "--collection", "knowledge__idqd_store_list"],
        )
        assert result.exit_code == 0, result.output
        assert "alpha.md" in result.output


class TestStoreGet:
    def test_get_by_id_through_daemon(
        self,
        runner: CliRunner,
        live_t3_daemon,
        reset_t3_singleton,
        tmp_path: Path,
    ) -> None:
        src = tmp_path / "beta.md"
        src.write_text("beta content for get smoke")
        put = runner.invoke(
            main,
            [
                "store",
                "put",
                str(src),
                "--collection",
                "knowledge__idqd_store_get",
                "--title",
                "beta.md",
            ],
        )
        assert put.exit_code == 0, put.output

        # Parse the doc id from the "Stored: <id> -> <collection>" line.
        # Example: "Stored: 0a1b2c...32hex  →  knowledge__idqd_store_get__<embedder>__v1"
        first_line = put.output.strip().splitlines()[0]
        doc_id = first_line.split()[1]

        result = runner.invoke(
            main,
            [
                "store",
                "get",
                doc_id,
                "--collection",
                "knowledge__idqd_store_get",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "beta content for get smoke" in result.output


class TestStoreDelete:
    def test_delete_by_id_through_daemon(
        self,
        runner: CliRunner,
        live_t3_daemon,
        reset_t3_singleton,
        tmp_path: Path,
    ) -> None:
        src = tmp_path / "gamma.md"
        src.write_text("gamma rm smoke body")
        put = runner.invoke(
            main,
            [
                "store",
                "put",
                str(src),
                "--collection",
                "knowledge__idqd_store_delete",
                "--title",
                "gamma.md",
            ],
        )
        assert put.exit_code == 0, put.output
        doc_id = put.output.strip().splitlines()[0].split()[1]

        result = runner.invoke(
            main,
            [
                "store",
                "delete",
                "--collection",
                "knowledge__idqd_store_delete",
                "--id",
                doc_id,
                "--yes",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Deleted:" in result.output


class TestStoreExpire:
    def test_expire_routes_through_daemon(
        self,
        runner: CliRunner,
        live_t3_daemon,
        reset_t3_singleton,
    ) -> None:
        # Empty store, expire() returns 0 cleanly.
        result = runner.invoke(main, ["store", "expire"])
        assert result.exit_code == 0, result.output
        assert "Expired" in result.output


# ── store export / import (round-trip via the daemon) ───────────────────────


class TestStoreExportImport:
    def test_export_then_import_through_daemon(
        self,
        runner: CliRunner,
        live_t3_daemon,
        reset_t3_singleton,
        tmp_path: Path,
    ) -> None:
        # nx store put auto-promotes 2-segment "knowledge__name" to the
        # conformant 4-segment "<prefix>__<owner>__<embedder>__v1"; nx
        # store export looks up the literal collection name when the
        # input already contains "__". Read the full name back from the
        # put output ("Stored: <id>  →  <full_collection>") so the export
        # path matches the persisted collection.
        src = tmp_path / "epsilon.md"
        src.write_text("epsilon round-trip body")
        put = runner.invoke(
            main,
            [
                "store", "put", str(src),
                "--collection", "knowledge__idqd-store-export",
                "--title", "epsilon.md",
            ],
        )
        assert put.exit_code == 0, put.output
        # "Stored: <id>  →  <full_collection_name>"
        put_collection = put.output.strip().splitlines()[0].rsplit(maxsplit=1)[-1]
        assert put_collection.startswith("knowledge__idqd-store-export"), put.output

        export_path = tmp_path / "epsilon.nxexp"
        export = runner.invoke(
            main,
            ["store", "export", put_collection, "-o", str(export_path)],
        )
        assert export.exit_code == 0, export.output
        assert export_path.exists()

        import_collection = "knowledge__idqd-store-import"
        import_result = runner.invoke(
            main,
            [
                "store", "import", str(export_path),
                "--collection", import_collection,
            ],
        )
        assert import_result.exit_code == 0, import_result.output
        assert "Imported" in import_result.output

        client = _direct_http_client(live_t3_daemon)
        names = {c.name for c in client.list_collections()}
        assert any(
            n.startswith(import_collection) for n in names
        ), f"expected imported collection in {names}"
