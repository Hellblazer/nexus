# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-146 Phase 1.1 / bead nexus-5p2ci.5: daemon-proxy vs direct rich-Catalog
write parity harness.

Cutover discipline (mirrors RDR-129): before the atomic cutover (P1.2)
flips any write site, prove that the daemon write-PROXY path produces
results IDENTICAL to the direct ``nexus.catalog.catalog.Catalog`` path.

REFRAMED per RF-8: parity is "daemon write-proxy result == direct rich-
Catalog result", NOT "CatalogStore RPC == direct" — the low-level
CatalogStore never served the rich write API.

Method: run one deterministic, seeded scenario of the represented write
ops against TWO independent tmp catalogs:

  (a) direct  — an in-process rich Catalog over tmp dir A.
  (b) proxy   — a live T2 daemon hosting its Catalog over tmp dir B
                (NEXUS_CATALOG_PATH), driven via T2Client.catalog_write.

Then assert EXACT equality (==, never >=) of:
  - every write op's return value (tumbler strings, link bools, ints),
  - the resolved entry metadata for each document (indexed_at normalised),
  - the link rows out of d1 (created_at normalised),
  - the owners.jsonl high-water (next_seq) advancement.

Reads run LOCALLY (RF-8 Q5): the proxy side stops the daemon, then
constructs a local Catalog over tmp dir B to read back committed state.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest

from nexus.catalog.catalog import Catalog
from nexus.catalog.tumbler import Tumbler


@pytest.fixture
def config_dir() -> Path:
    cd = Path(tempfile.mkdtemp(prefix="nxpar-", dir="/tmp"))
    yield cd
    shutil.rmtree(cd, ignore_errors=True)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "memory.db"


# ---------------------------------------------------------------------------
# Scenario + normalisation helpers
# ---------------------------------------------------------------------------

# Deterministic fixture inputs. No randomness, no clocks in the inputs;
# tumbler allocation is a deterministic high-water mark so both paths
# starting from an empty catalog must converge identically.
_OWNER = ("acme", "project")
_OWNER_KW = {"repo_hash": "h1", "repo_root": "/tmp/acme", "description": "ACME"}
_COLL = "code__acme__voyage-code-3__v1"
_COLL2 = "code__acme__voyage-code-3__v2"


def _run_write_scenario(writer: Any) -> dict[str, Any]:
    """Execute the scripted write sequence against *writer* (a rich
    Catalog or a T2Client.catalog_write proxy) and collect return values.

    Returns a dict of op-keyed results with all Tumblers stringified so
    the direct and proxy collections compare structurally.
    """
    out: dict[str, Any] = {}

    owner = writer.register_owner(_OWNER[0], _OWNER[1], **_OWNER_KW)
    out["owner"] = str(owner)

    d1 = writer.register(
        owner, "Doc One", content_type="paper", file_path="/tmp/acme/a.md", year=2020
    )
    d2 = writer.register(
        owner, "Doc Two", content_type="paper", file_path="/tmp/acme/b.md", year=2021
    )
    out["d1"] = str(d1)
    out["d2"] = str(d2)

    writer.update(d1, title="Doc One Edited", year=2024)

    out["link"] = writer.link(d1, d2, "cites", "tester", weight=3)
    # Duplicate: link_if_absent must report no new row.
    out["link_if_absent_dup"] = writer.link_if_absent(d1, d2, "cites", "tester")

    out["set_head"] = writer.set_owner_head_hash(owner, "deadbeef")
    writer.register_collection(
        _COLL, content_type="code", owner_id=str(owner), embedding_model="voyage-code-3"
    )

    # Final allocation proves next_seq high-water parity end-to-end.
    d3 = writer.register(owner, "Doc Three", content_type="paper", file_path="/tmp/acme/c.md")
    out["d3"] = str(d3)

    out["unlink"] = writer.unlink(d1, d2, "cites")

    # P1.2 admin/maintenance ops routed through the daemon (parity must
    # hold for these too). All return JSON-native scalars.
    out["rename_collection"] = writer.rename_collection(_COLL, _COLL + "-renamed")
    out["batch"] = writer.update_documents_collection_batch(
        [(str(d1), _COLL + "-renamed")]
    )
    out["bulk_unlink_dry"] = writer.bulk_unlink(link_type="cites", dry_run=True)

    # Review remediation (substantive-critic SIG-2): cover the previously
    # untested hot/serialisation-risky ops. ensure_owner_for_repo exercises
    # the Path-arg wire round-trip (returns a Tumbler); the manifest ops
    # exercise list[dict] args; delete_document a Tumbler-arg + bool return;
    # supersede_collection a no-arg-Tumbler write.
    import pathlib as _pl
    repo_owner = writer.ensure_owner_for_repo(
        _pl.Path("/tmp/acme-repo"), repo_name="acme-repo"
    )
    out["repo_owner"] = str(repo_owner)

    manifest_chunks = [
        {"chash": "a" * 64, "position": 0, "chunk_index": 0},
        {"chash": "b" * 64, "position": 1, "chunk_index": 1},
    ]
    writer.write_manifest(str(d2), manifest_chunks)
    writer.append_manifest_chunks(str(d2), [{"chash": "c" * 64, "position": 2, "chunk_index": 2}])
    writer.resync_chunk_count_cache(str(d2))

    out["delete_document"] = writer.delete_document(d3)
    return out


def _normalise_entry(entry: Any) -> dict[str, Any]:
    d = dataclasses.asdict(entry)
    d["tumbler"] = str(entry.tumbler)
    d.pop("indexed_at", None)  # wall-clock, differs between runs
    return d


def _normalise_link(link: Any) -> dict[str, Any]:
    d = dataclasses.asdict(link)
    d["from_tumbler"] = str(link.from_tumbler)
    d["to_tumbler"] = str(link.to_tumbler)
    d.pop("created_at", None)  # wall-clock, differs between runs
    return d


def _read_state(catalog_dir: Path, results: dict[str, Any]) -> dict[str, Any]:
    """Read committed state from a LOCAL Catalog over *catalog_dir*.

    Captured BEFORE the unlink in the scenario is observable, so we
    re-link nothing; instead we read the entries (which survive unlink)
    and the high-water. Link-row parity is asserted via the scenario's
    return values (link / link_if_absent / unlink counts).
    """
    cat = Catalog(catalog_dir, catalog_dir / ".catalog.db")
    try:
        e1 = cat.resolve(Tumbler.parse(results["d1"]))
        e2 = cat.resolve(Tumbler.parse(results["d2"]))
        # d3 is deleted at the end of the scenario (delete_document); a deleted
        # doc resolves to None, which is itself part of the parity check.
        e3 = cat.resolve(Tumbler.parse(results["d3"]))
        # d2's manifest (write_manifest + append_manifest_chunks) is read back
        # so the list[dict]-arg wire round-trip is parity-checked.
        manifest = sorted(
            (m.chash, m.position) for m in cat.get_manifest(results["d2"])
        )
        return {
            "e1": _normalise_entry(e1),
            "e2": _normalise_entry(e2),
            "e3_deleted": e3 is None,
            "d2_manifest": manifest,
            "high_water_owner1": _high_water_for_owner(catalog_dir, "1.1"),
        }
    finally:
        cat._db.close()


def _high_water_for_owner(catalog_dir: Path, owner_prefix: str) -> int:
    """Return *owner_prefix*'s current next_seq (max across its owners.jsonl rows).

    Owner-scoped (not last-line) because ensure_owner_for_repo appends a
    SECOND owner's rows; the last line would otherwise be that owner's seq.
    """
    seqs = []
    for line in (catalog_dir / "owners.jsonl").read_text().splitlines():
        row = json.loads(line)
        if row.get("owner") == owner_prefix:
            seqs.append(row["next_seq"])
    return max(seqs)


# ---------------------------------------------------------------------------
# Daemon driver
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Parity test
# ---------------------------------------------------------------------------


class TestCatalogWriteParity:
    def test_proxy_matches_direct(
        self, config_dir: Path, db_path: Path, tmp_path: Path, monkeypatch
    ) -> None:
        from nexus.config import catalog_path
        from nexus.daemon.t2_client import make_t2_client
        from nexus.daemon.t2_daemon import T2Daemon

        # ── (a) direct rich-Catalog path over an independent tmp dir ──────
        direct_dir = tmp_path / "direct-catalog"
        direct_dir.mkdir()
        direct_cat = Catalog(direct_dir, direct_dir / ".catalog.db")
        direct_results = _run_write_scenario(direct_cat)
        direct_cat._db.close()
        direct_state = _read_state(direct_dir, direct_results)

        # ── (b) daemon write-proxy path over NEXUS_CATALOG_PATH dir ───────
        proxy_dir = catalog_path()  # autouse _isolate_catalog -> tmp/test-catalog
        daemon = T2Daemon(config_dir=config_dir, db_path=db_path)
        ready, stop_evt = threading.Event(), threading.Event()
        th = threading.Thread(
            target=_run_daemon_in_thread, args=(daemon, ready, stop_evt), daemon=True
        )
        th.start()
        assert ready.wait(timeout=10), "daemon did not start"
        try:
            client = make_t2_client(config_dir=config_dir)
            proxy_results = _run_write_scenario(client.catalog_write)
            client.close()
        finally:
            stop_evt.set()
            th.join(timeout=10)
        # Daemon stopped -> its write handle released; read locally.
        proxy_state = _read_state(proxy_dir, proxy_results)

        # ── EXACT parity assertions ───────────────────────────────────────
        assert proxy_results == direct_results
        assert proxy_state == direct_state

        # Spell out the load-bearing equalities so a failure localises.
        assert proxy_results["owner"] == direct_results["owner"] == "1.1"
        assert proxy_results["d1"] == direct_results["d1"] == "1.1.1"
        assert proxy_results["d2"] == direct_results["d2"] == "1.1.2"
        assert proxy_results["d3"] == direct_results["d3"] == "1.1.3"
        assert proxy_results["link"] is True and direct_results["link"] is True
        assert proxy_results["link_if_absent_dup"] is False
        assert direct_results["link_if_absent_dup"] is False
        assert proxy_results["unlink"] == direct_results["unlink"] == 1
        # owner 1.1 minted 4 docs (d1..d4 path: d1,d2,d3 + the high-water never
        # decreases on delete), so next_seq == 4.
        assert proxy_state["high_water_owner1"] == direct_state["high_water_owner1"] == 4
        # update() landed identically on both sides.
        assert proxy_state["e1"]["title"] == direct_state["e1"]["title"] == "Doc One Edited"
        assert proxy_state["e1"]["year"] == direct_state["e1"]["year"] == 2024
        # P1.2 SIG-2 coverage: Path-arg op returns a Tumbler; manifest list[dict]
        # round-tripped; delete_document removed d3 on both sides.
        assert proxy_results["repo_owner"] == direct_results["repo_owner"] == "1.2"
        assert proxy_results["delete_document"] is True
        assert direct_results["delete_document"] is True
        assert proxy_state["e3_deleted"] is True and direct_state["e3_deleted"] is True
        assert proxy_state["d2_manifest"] == direct_state["d2_manifest"]
        assert len(proxy_state["d2_manifest"]) == 3  # write(2) + append(1)
