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
