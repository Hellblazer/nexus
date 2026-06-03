# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-146 Phase 1 / bead nexus-5p2ci.20: daemon-hosted rich Catalog +
write-only 16-op whitelist.

Three layers:

1. Shim unit tests (no daemon): the whitelist is exactly 16 ops, every
   op exists on the rich Catalog, Tumbler args/returns coerce correctly,
   the dispatch subset is namespaced under ``catalog_write.``.
2. Dispatch-table composition: hosting the rich Catalog merges exactly
   the 16 write ops and does NOT expose the dataclass-returning reads
   (resolve_span / resolve_chash / link_audit) or the low-level reads.
3. End-to-end over real sockets: drive ``register_owner`` / ``register``
   / ``link`` through a live T2Client, get Tumblers back, and confirm a
   fresh local read sees the daemon-committed writes (RF-8 Q5).
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest

from nexus.catalog.tumbler import Tumbler
from nexus.daemon.catalog_write_shim import (
    CATALOG_WRITE_OPS,
    CATALOG_WRITE_PREFIX,
    TUMBLER_PARAM_NAMES,
    TUMBLER_RETURN_OPS,
    build_catalog_write_dispatch,
    decode_return,
    encode_tumbler_args,
    make_write_shim,
)


@pytest.fixture
def config_dir() -> Path:
    cd = Path(tempfile.mkdtemp(prefix="nxcw-", dir="/tmp"))
    yield cd
    shutil.rmtree(cd, ignore_errors=True)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "memory.db"


# ---------------------------------------------------------------------------
# Layer 1 — shim units
# ---------------------------------------------------------------------------


class TestWhitelistContract:
    def test_exactly_sixteen_ops(self) -> None:
        assert len(CATALOG_WRITE_OPS) == 16
        # No duplicates.
        assert len(set(CATALOG_WRITE_OPS)) == 16

    def test_op_set_is_the_locked_16(self) -> None:
        assert set(CATALOG_WRITE_OPS) == {
            "register_owner",
            "ensure_owner_for_repo",
            "register",
            "update",
            "link",
            "link_if_absent",
            "unlink",
            "delete_document",
            "register_collection",
            "delete_collection_projection",
            "supersede_collection",
            "set_owner_head_hash",
            "write_manifest",
            "append_manifest_chunks",
            "atomic_manifest_replace",
            "resync_chunk_count_cache",
        }

    def test_every_write_op_exists_on_rich_catalog(self) -> None:
        """API-drift guard (workflow lesson: verify the served surface).

        RF-7 was wrong precisely because nobody confirmed the served
        class actually defined the methods. Lock it: every whitelisted op
        must be a callable attribute of the rich Catalog.
        """
        from nexus.catalog.catalog import Catalog

        for op in CATALOG_WRITE_OPS:
            attr = getattr(Catalog, op, None)
            assert attr is not None, f"rich Catalog missing write op {op!r}"
            assert callable(attr), f"rich Catalog.{op} is not callable"

    def test_tumbler_return_ops_are_subset_of_write_ops(self) -> None:
        assert TUMBLER_RETURN_OPS <= set(CATALOG_WRITE_OPS)
        assert TUMBLER_RETURN_OPS == {
            "register_owner",
            "ensure_owner_for_repo",
            "register",
        }

    def test_tumbler_param_names(self) -> None:
        assert TUMBLER_PARAM_NAMES == {"owner", "tumbler", "from_t", "to_t"}


class TestClientEncoding:
    def test_encode_converts_tumbler_args_to_str(self) -> None:
        t = Tumbler.parse("1.2")
        args, kwargs = encode_tumbler_args([t, "title"], {"owner": t, "year": 0})
        assert args == ["1.2", "title"]
        assert kwargs == {"owner": "1.2", "year": 0}

    def test_encode_leaves_non_tumbler_untouched(self) -> None:
        args, kwargs = encode_tumbler_args(["doc1", 3], {"reason": "x"})
        assert args == ["doc1", 3]
        assert kwargs == {"reason": "x"}

    def test_decode_parses_return_for_return_ops(self) -> None:
        out = decode_return("register", "1.2.3")
        assert isinstance(out, Tumbler)
        assert str(out) == "1.2.3"

    def test_decode_passthrough_for_non_return_ops(self) -> None:
        # unlink returns int; delete_document returns bool — never parsed.
        assert decode_return("unlink", 2) == 2
        assert decode_return("delete_document", True) is True


class TestDaemonShim:
    def test_str_arg_coerced_to_tumbler(self) -> None:
        seen: dict[str, object] = {}

        def fake_update(tumbler: Tumbler, **fields: object) -> None:
            seen["tumbler"] = tumbler
            seen["fields"] = fields

        shim = make_write_shim(fake_update, "update")
        shim("1.4.2", title="New")
        assert isinstance(seen["tumbler"], Tumbler)
        assert str(seen["tumbler"]) == "1.4.2"
        assert seen["fields"] == {"title": "New"}

    def test_tumbler_return_serialised_to_str(self) -> None:
        def fake_register(owner: Tumbler, title: str) -> Tumbler:
            return Tumbler.parse("1.9.9")

        shim = make_write_shim(fake_register, "register")
        out = shim("1.9", "T")
        assert out == "1.9.9"
        assert isinstance(out, str)

    def test_var_keyword_meta_passed_through(self) -> None:
        captured: dict[str, object] = {}

        def fake_link(
            from_t: Tumbler, to_t: Tumbler, link_type: str, created_by: str, **meta: object
        ) -> bool:
            captured["from_t"] = from_t
            captured["to_t"] = to_t
            captured["meta"] = meta
            return True

        shim = make_write_shim(fake_link, "link")
        ok = shim("1.1", "1.2", "cites", "tester", weight=3)
        assert ok is True
        assert isinstance(captured["from_t"], Tumbler)
        assert isinstance(captured["to_t"], Tumbler)
        assert captured["meta"] == {"weight": 3}

    def test_build_dispatch_has_namespaced_sixteen(self) -> None:
        cat = _make_local_catalog()
        table = build_catalog_write_dispatch(cat)
        assert len(table) == 16
        assert all(k.startswith(CATALOG_WRITE_PREFIX) for k in table)
        assert set(table) == {f"{CATALOG_WRITE_PREFIX}{op}" for op in CATALOG_WRITE_OPS}

    def test_dispatch_excludes_dataclass_reads(self) -> None:
        cat = _make_local_catalog()
        table = build_catalog_write_dispatch(cat)
        for denied in ("resolve_span", "resolve_chash", "link_audit", "resolve", "links_from"):
            assert f"{CATALOG_WRITE_PREFIX}{denied}" not in table


def _make_local_catalog():
    d = Path(tempfile.mkdtemp(prefix="nxcat-", dir="/tmp"))
    from nexus.catalog.catalog import Catalog

    return Catalog(d, d / ".catalog.db")


# ---------------------------------------------------------------------------
# Layer 2 — daemon dispatch composition
# ---------------------------------------------------------------------------


class TestDaemonHostsCatalogWrite:
    def test_start_merges_catalog_write_ops(self, config_dir: Path, db_path: Path) -> None:
        from nexus.daemon.t2_daemon import T2Daemon

        daemon = T2Daemon(config_dir=config_dir, db_path=db_path)

        async def _drive() -> dict[str, object]:
            await daemon.start()
            try:
                return dict(daemon._dispatch_table)
            finally:
                await daemon.stop()

        table = _run(_drive())
        for op in CATALOG_WRITE_OPS:
            assert f"{CATALOG_WRITE_PREFIX}{op}" in table
        # Low-level catalog reads still present under the catalog.* namespace.
        assert any(k.startswith("catalog.") for k in table)


# ---------------------------------------------------------------------------
# Layer 3 — end-to-end over real sockets
# ---------------------------------------------------------------------------


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _run_daemon_in_thread(daemon, ready: threading.Event, stop_evt: threading.Event):
    async def _main() -> None:
        await daemon.start()
        ready.set()
        while not stop_evt.is_set():
            await asyncio.sleep(0.05)
        await daemon.stop()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_main())
    finally:
        loop.close()


class TestEndToEndWrites:
    def test_register_and_link_round_trip_through_daemon(
        self, config_dir: Path, db_path: Path, tmp_path: Path
    ) -> None:
        from nexus.daemon.t2_client import make_t2_client
        from nexus.daemon.t2_daemon import T2Daemon

        daemon = T2Daemon(config_dir=config_dir, db_path=db_path)
        ready, stop_evt = threading.Event(), threading.Event()
        th = threading.Thread(
            target=_run_daemon_in_thread, args=(daemon, ready, stop_evt), daemon=True
        )
        th.start()
        assert ready.wait(timeout=10), "daemon did not start"

        try:
            client = make_t2_client(config_dir=config_dir)
            owner = client.catalog_write.register_owner(
                "acme", "project", repo_hash="h1", repo_root="/tmp/acme"
            )
            assert isinstance(owner, Tumbler)

            d1 = client.catalog_write.register(
                owner, "Doc One", content_type="paper", file_path="/tmp/acme/a.md"
            )
            d2 = client.catalog_write.register(
                owner, "Doc Two", content_type="paper", file_path="/tmp/acme/b.md"
            )
            assert isinstance(d1, Tumbler) and isinstance(d2, Tumbler)
            assert str(d1) != str(d2)

            linked = client.catalog_write.link(d1, d2, "cites", "tester")
            assert linked is True

            client.close()
        finally:
            stop_evt.set()
            th.join(timeout=10)

        # Fresh local read sees the daemon-committed writes (reads stay local).
        from nexus.catalog.catalog import Catalog
        from nexus.config import catalog_path

        cat = Catalog(catalog_path(), catalog_path() / ".catalog.db")
        e1 = cat.resolve(d1)
        assert e1 is not None and e1.title == "Doc One"
        links = cat.links_from(d1)
        assert any(str(getattr(l, "to_tumbler", getattr(l, "to_t", ""))) == str(d2) for l in links)
