# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-sjb52 engine-substrate regression: ``nx collection reindex`` must
actually complete, end to end, against the real engine.

Found during the RDR-191 GATE-2 census: ``reindex_cmd``'s delete hop called
``HttpVectorClient.delete_collection``, which is an unconditional
``raise NotImplementedError(...)`` stub in BOTH local and cloud mode since
RDR-155 P4b made ``HttpVectorClient`` the only T3 client. The verb printed
"Deleting collection ..." and then died — on every invocation, in every
mode — and nothing in the suite executed the verb against a real substrate
to notice, because ``tests/test_collection_cmd.py`` mocks ``db`` with
``MagicMock(spec=HttpVectorClient)``: spec'ing a mock only constrains which
attributes exist, it does not run the real method body, so the mock happily
"implemented" ``delete_collection`` and the gap survived every release.

The fix reroutes the delete hop through ``purge_collection_cascade`` — the
same cascade ``nx collection delete`` already uses (RDR-144 P4 follow-up).
This test's regression pin is that it EXECUTES AT ALL: on the pre-fix code,
``CliRunner.invoke`` would capture ``result.exception`` as the raw
``NotImplementedError`` and ``result.exit_code != 0`` after printing only
"Deleting collection '<name>' (N chunks)..." — the assertions below on a
populated after-count and a still-searchable document would never even be
reached.

House pattern: real ``nx`` CLI verbs in-process via ``click.testing.CliRunner``
against the session's real PG-backed engine substrate (``t2_service_env``,
see ``tests/test_scenario_journeys.py``), real indexing path (server-side
bge-768 embeddings under ``NX_LOCAL=1`` — no API keys), catalog assertions via
``tests/_catalog_fixture_ops.py``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.cli import main
from tests._catalog_fixture_ops import documents_by_file_path


@pytest.mark.scenario
def test_collection_reindex_survives_the_delete_hop(t2_service_env, tmp_path: Path) -> None:
    """Index a real markdown doc, then ``nx collection reindex`` it.

    Regression pin: pre-fix, this test would fail at the reindex invocation
    with ``NotImplementedError: delete_collection not implemented in
    HttpVectorClient`` bubbling out of ``CliRunner.invoke`` as
    ``result.exception`` (exit_code != 0). Post-fix, the verb completes and
    the document is still catalogued and searchable afterward — proving the
    ``purge_collection_cascade`` reroute both deletes AND that the
    subsequent re-index path rebuilds what a bare ``nx collection reindex``
    caller actually depends on (a populated, catalogued, searchable
    collection).
    """
    corpus = "sjb52reindex"

    md = tmp_path / "reindex-e2e.md"
    md.write_text(
        "# Reindex Regression Doc\n\n"
        "Consistent hashing distributes keys across a ring of nodes to "
        "minimize rebalancing when nodes join or leave a cluster.\n"
    )

    runner = CliRunner()

    idx = runner.invoke(main, ["index", "md", str(md), "--corpus", corpus])
    assert idx.exit_code == 0, idx.output
    assert "Indexed 1 chunk" in idx.output

    pre_docs = documents_by_file_path(str(md.resolve()))
    assert len(pre_docs) == 1, f"expected exactly one catalog document pre-reindex, got {pre_docs}"
    assert pre_docs[0].chunk_count > 0
    # The real physical collection name -- conformant 4-segment
    # (``docs__<corpus>__<model>__v1``), not the ``docs__<corpus>``
    # 2-segment shorthand `--corpus` alone would suggest (that shorthand
    # only works as a search-side PREFIX filter, not an exact collection
    # name -- `nx collection reindex` needs the real name or it 400s at
    # the engine's four-segment-conformance check before ever reaching
    # the delete hop this test exists to pin).
    collection = pre_docs[0].physical_collection
    assert collection, f"expected a physical_collection on the freshly indexed doc: {pre_docs[0]}"

    # THE regression hop: pre-fix this raised NotImplementedError from
    # db.delete_collection(name) before ever reaching the re-index step.
    reindex = runner.invoke(main, ["collection", "reindex", collection])
    assert reindex.exit_code == 0, (
        f"reindex must complete, not die at the delete hop "
        f"(exception={reindex.exception!r}): {reindex.output}"
    )
    assert "Re-indexed:" in reindex.output
    # The after-count must be non-zero — a reindex that deletes and never
    # rebuilds would report "N -> 0 chunks" and exit 0, which a bare
    # exit-code assertion would not catch.
    assert "-> 0 chunks" not in reindex.output, reindex.output

    search = runner.invoke(main, [
        "search", "consistent hashing ring nodes rebalancing",
        "--corpus", collection, "--json",
    ])
    assert search.exit_code == 0, search.output
    hits = json.loads(search.stdout)
    assert hits, f"expected at least one search hit for the reindexed doc: {search.output}"

    post_docs = documents_by_file_path(str(md.resolve()))
    assert len(post_docs) == 1, (
        f"expected exactly one catalog document post-reindex (not orphaned "
        f"or duplicated by the delete+rebuild cycle), got {post_docs}"
    )
    assert post_docs[0].chunk_count > 0, "catalog row must reflect the rebuilt chunk"


def _quarantined_ids(db, sibling: str) -> set[str]:
    """Chashes stored in *sibling*, live or not; empty when it was never registered."""
    try:
        return set(db.get_collection(sibling).get_all_metadata(include_non_live=True)["ids"])
    except Exception:  # noqa: BLE001 — an unregistered sibling holds nothing
        return set()


@pytest.mark.scenario
def test_collection_reindex_leaves_the_origins_quarantine_rows_and_delete_takes_them(
    t2_service_env, tmp_path: Path,
) -> None:
    """nexus-rf87b: ``nx collection reindex`` re-registers the SAME name, so it sends
    ``keep_quarantine=True`` and the engine must leave the origin's rows in its
    ``quarantine-`` sibling (nexus-wbfpw.71). Until now the Python side pinned only the
    mocked call args (``test_collection_cmd.py``, ``test_collection_purge.py``) and the
    engine side was Java-only. This runs the verb against the real engine.

    Setup: index a real doc, seed orphan chunks into the same collection and move them
    into the quarantine sibling the way ``nx index repo``/``nx t3 gc`` do. After the
    reindex the sibling must still hold exactly those chashes. Non-vacuity: the control
    ``nx collection delete`` on the same name must take the rows, so a pass cannot be
    "the engine never deletes quarantine".
    """
    import hashlib

    import nexus.catalog.chunk_quarantine as cq
    import nexus.db.http_vector_client as hvc
    from tests._chunk_seed import seed_chunks_direct
    from tests._reapable_age import age_chunks_past_grace

    md = tmp_path / "reindex-quarantine-e2e.md"
    md.write_text(
        "# Reindex Quarantine Doc\n\n"
        "Rendezvous hashing picks the node with the highest score for a key, "
        "so adding a node moves only the keys that node now wins.\n"
    )
    runner = CliRunner()
    idx = runner.invoke(main, ["index", "md", str(md), "--corpus", "rfq87breindex"])
    assert idx.exit_code == 0, idx.output
    docs = documents_by_file_path(str(md.resolve()))
    assert len(docs) == 1, docs
    collection = docs[0].physical_collection
    assert collection

    db = hvc.HttpVectorClient(tenant=t2_service_env)
    sibling = cq.quarantine_collection_name(collection)

    orphans = [hashlib.sha256(f"{collection}:rf87b-orphan:{i}".encode()).hexdigest() for i in range(3)]
    seed_chunks_direct(
        collection, ids=orphans,
        documents=[f"orphan text {i}, owned by no catalog document\n" for i in range(len(orphans))],
        metadatas=[{"chunk_text_hash": h, "title": f"orphan_{i}.md:1-1"} for i, h in enumerate(orphans)],
    )
    age_chunks_past_grace(collection)
    from datetime import UTC, datetime, timedelta

    long_ago = (datetime.now(UTC) - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
    moved = cq.quarantine_orphans_serverside(db, collection, sibling, long_ago)
    assert moved is not None and moved[0] == len(orphans), moved
    assert _quarantined_ids(db, sibling) == set(orphans), "setup: the orphans sit in the sibling"

    reindex = runner.invoke(main, ["collection", "reindex", collection])
    assert reindex.exit_code == 0, (reindex.exception, reindex.output)
    assert "-> 0 chunks" not in reindex.output, reindex.output

    assert _quarantined_ids(db, sibling) == set(orphans), (
        "nx collection reindex must leave the origin's quarantine rows (keep_quarantine=True); "
        f"the sibling {sibling!r} now holds {sorted(_quarantined_ids(db, sibling))}"
    )

    # Control: the same engine, asked to delete the name WITHOUT keep_quarantine, takes them.
    delete = runner.invoke(main, ["collection", "delete", collection, "--yes"])
    assert delete.exit_code == 0, (delete.exception, delete.output)
    assert _quarantined_ids(db, sibling) == set(), (
        "control: nx collection delete takes the origin's quarantine rows, so the reindex "
        "assertion above is not satisfied by an engine that never deletes them"
    )
