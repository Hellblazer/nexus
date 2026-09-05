# SPDX-License-Identifier: AGPL-3.0-or-later
"""Standalone child-process script for
``tests/test_hg2dw_run_dies_before_flush.py::TestHardKillBlastRadius``.

NOT a test module (no ``test_`` prefix — pytest never collects it). Run
as ``python -u tests/_hg2dw_hard_kill_child.py`` in a FRESH subprocess
(never via ``os.fork()`` — this project's test process is multi-threaded
with several C-extension-heavy libraries loaded, e.g. torch/scipy/lxml,
and forking it segfaults; measured directly while developing this test).

Indexes the repo named by ``NX_HG2DW_REPO`` with a real ``index_repository``
pass, but hard-exits via ``os._exit(137)`` (SIGKILL's conventional
wait-status code) from inside the ``NX_HG2DW_KILL_AFTER``'th per-file
fence-begin call — AFTER that real begin call has already landed, so
the catalog genuinely shows 'indexing' for exactly that many documents,
but BEFORE any Python cleanup (``index_repository``'s ``finally``,
hence ``_reconcile_needs_fence``) can possibly run. ``os._exit()``
bypasses every CPython cleanup hook identically to an external SIGKILL
for the purposes of this test, deterministically (no timing race against
a real signal delivery).

Connects to the SAME shared engine substrate the parent pytest process
already booted, via the inherited ``NX_SERVICE_URL`` / ``NX_SERVICE_TOKEN``
/ ``NX_LOCAL`` env vars (set by the parent's ``t2_service_env`` /
``_pin_t2_substrate`` fixtures before this script is spawned). T3 is a
fresh, independent, pure in-memory ``InMemoryVectorClient`` — no
sockets, so no cross-process resource sharing concern at all here
(unlike the catalog, which every fence helper connects to fresh per
call anyway).

Exit codes:
    137  the kill fired as designed (the expected outcome)
    99   index_repository returned/raised WITHOUT the kill ever firing —
         a test-construction bug (e.g. fewer files than kill_after
         needed to be reached), not a pass
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


def main() -> None:
    repo = Path(os.environ["NX_HG2DW_REPO"])
    repos_json = Path(os.environ["NX_HG2DW_REPOS_JSON"])
    kill_after = int(os.environ["NX_HG2DW_KILL_AFTER"])

    # Ensure the repo root is importable exactly like the parent pytest
    # process (this script is invoked with cwd=repo root, but pin it
    # explicitly since a future caller might not).
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction
    from nexus.db.t3 import T3Database
    from nexus.indexer import index_repository
    from nexus.registry import RepoRegistry
    from tests._catalog_fixture_ops import seed_manifest_chunks
    from tests.conftest import fake_credentials, make_vector_test_client
    import nexus.doc_indexer as doc_indexer_mod

    ef = DefaultEmbeddingFunction()
    mock_voyage_client = MagicMock()

    def fake_embed(texts, model, input_type="document"):
        r = MagicMock()
        r.embeddings = ef(texts)
        return r

    def fake_contextualized_embed(inputs, model, input_type="document"):
        r = MagicMock()
        br = MagicMock()
        br.embeddings = ef(inputs[0])
        r.results = [br]
        return r

    mock_voyage_client.embed.side_effect = fake_embed
    mock_voyage_client.contextualized_embed.side_effect = fake_contextualized_embed

    t3 = T3Database(_client=make_vector_test_client(), _ef_override=ef)
    orig_write_batch = t3._write_batch

    def _seeding_write_batch(col, collection_name, ids, documents, metadatas,
                              embeddings=None, **kwargs):
        orig_write_batch(col, collection_name, ids, documents, metadatas,
                          embeddings, **kwargs)
        seed_manifest_chunks(collection_name, ids)

    t3._write_batch = _seeding_write_batch

    real_begin = doc_indexer_mod._fence_begin
    count = {"n": 0}

    def _kill_after_n(doc_id, content_hash, collection):
        real_begin(doc_id, content_hash, collection)
        count["n"] += 1
        if count["n"] >= kill_after:
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(137)

    reg = RepoRegistry(repos_json)
    reg.add(repo)

    with patch("nexus.db.make_t3", return_value=t3), \
         patch("nexus.config.get_credential", side_effect=fake_credentials()), \
         patch("voyageai.Client", return_value=mock_voyage_client), \
         patch.object(doc_indexer_mod, "_fence_begin", side_effect=_kill_after_n):
        try:
            index_repository(repo, reg, force=False)
        except BaseException:  # noqa: BLE001 — child-only script: never let anything else escape
            pass

    # Reached only if the kill never fired — the caller must treat this
    # as a test-construction failure, never as evidence of anything.
    os._exit(99)


if __name__ == "__main__":
    main()
