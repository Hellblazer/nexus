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
    def test_exactly_twentythree_ops(self) -> None:
        # 16 hot-path (P1.0) + 6 admin/maintenance (P1.2) + register_many (nexus-9dvqy).
        assert len(CATALOG_WRITE_OPS) == 23
        # No duplicates.
        assert len(set(CATALOG_WRITE_OPS)) == 23

    def test_op_set_is_the_locked_23(self) -> None:
        assert set(CATALOG_WRITE_OPS) == {
            "register_owner",
            "ensure_owner_for_repo",
            "register",
            "register_many",
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
            "rename_collection",
            "bulk_unlink",
            "update_documents_collection_batch",
            "sync",
            "pull",
            "compact",
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

    def test_decode_parses_list_return_for_register_many(self) -> None:
        # nexus-9dvqy: register_many returns list[Tumbler]; the daemon sends
        # list[str] and the client parses each element back to Tumbler.
        out = decode_return("register_many", ["1.2.3", "1.2.4"])
        assert [type(o) for o in out] == [Tumbler, Tumbler]
        assert [str(o) for o in out] == ["1.2.3", "1.2.4"]

    def test_daemon_serialises_list_of_tumblers_to_str_for_register_many(self) -> None:
        # Round-trip the daemon side: a Catalog method returning list[Tumbler]
        # is serialised to list[str] on the wire, then decode_return above
        # parses it back — so a daemon-routed register_many returns Tumblers.
        def fake_register_many(owner: Tumbler, docs: list) -> list:
            return [Tumbler.parse("1.2.3"), Tumbler.parse("1.2.4")]

        shim = make_write_shim(fake_register_many, "register_many")
        wire = shim(Tumbler.parse("1.2"), [{"title": "a"}, {"title": "b"}])
        assert wire == ["1.2.3", "1.2.4"]  # list[str] on the wire
        assert decode_return("register_many", wire) == [
            Tumbler.parse("1.2.3"), Tumbler.parse("1.2.4"),
        ]


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

    def test_build_dispatch_has_namespaced_whitelist(self) -> None:
        cat = _make_local_catalog()
        table = build_catalog_write_dispatch(cat)
        assert len(table) == 23
        assert all(k.startswith(CATALOG_WRITE_PREFIX) for k in table)
        assert set(table) == {f"{CATALOG_WRITE_PREFIX}{op}" for op in CATALOG_WRITE_OPS}

    def test_bulk_unlink_str_filters_not_coerced_to_tumbler(self) -> None:
        """bulk_unlink's from_t/to_t are PLAIN STR filters (often ""),
        so the per-op shim must NOT Tumbler.parse them (parse("") raises)."""
        captured: dict[str, object] = {}

        def fake_bulk_unlink(
            from_t: str = "", to_t: str = "", link_type: str = "", created_by: str = "",
            created_at_before: str = "", dry_run: bool = False,
        ) -> int:
            captured["from_t"] = from_t
            captured["to_t"] = to_t
            return 5

        shim = make_write_shim(fake_bulk_unlink, "bulk_unlink")
        n = shim(from_t="", to_t="", link_type="cites")
        assert n == 5
        assert captured["from_t"] == "" and captured["to_t"] == ""
        assert isinstance(captured["from_t"], str)

    def test_dispatch_excludes_dataclass_reads(self) -> None:
        cat = _make_local_catalog()
        table = build_catalog_write_dispatch(cat)
        for denied in ("resolve_span", "resolve_chash", "link_audit", "resolve", "links_from"):
            assert f"{CATALOG_WRITE_PREFIX}{denied}" not in table


def _make_local_catalog():
    d = Path(tempfile.mkdtemp(prefix="nxcat-", dir="/tmp"))
    from nexus.catalog.catalog import Catalog

    return Catalog(d, d / ".catalog.db")


# NO Layer 2 / Layer 3 (daemon dispatch composition + end-to-end over sockets):
# both hosted the shim inside a live T2Daemon and drove it through a real
# T2Client. Daemon and client are deleted (nexus-i711w Stage 2 sub-stage B).
#
# The Layer 1 shim unit tests above SURVIVE and are the reason this file does:
# `CATALOG_WRITE_OPS` is still load-bearing for the non-daemon
# `catalog/factory.py` (CatalogWriter and HttpCatalogWriter both enforce the
# whitelist) and for `catalog/catalog_protocol.py`. The RPC-only helpers it
# sits beside (make_write_shim, build_catalog_write_dispatch,
# CATALOG_WRITE_PREFIX, encode_tumbler_args, decode_return) now have no
# production consumer; their removal, and moving the surviving tuple out of
# `daemon/` where it no longer belongs, is tracked separately.
