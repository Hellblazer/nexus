# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.5a row 14 (engine 0.1.202): a delete the engine refuses with 409 ``collection_busy``.

``nx collection delete``, ``prune`` and ``reindex`` name the job and its state and say what to do for that state;
none of them calls the refusal unconfirmed (the engine removed nothing). The fixtures and helpers are
``tests/test_collection_cmd.py``'s; the delete tests run a real ``HttpCatalogClient`` over an ``httpx.MockTransport``.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.cli import main
from nexus.db.collection_purge import CascadeCounts
from tests.test_collection_cmd import (  # noqa: F401 — the fixtures are the module's
    _ORPHAN, _VOYAGE_MODELS, _invoke, _serve_models, _setup_reindex_mock, env_creds, mock_db, runner,
)


_JOB = "7f3e0000-0000-0000-0000-000000000001"


def _busy_body(state: str | None, name: str = "old") -> dict:
    """The engine's 409 ``collection_busy`` body as 0.1.202 sends it: the state is only in the text."""
    where = f" ({state})" if state is not None else ""
    return {"error": f"cannot delete {name}: re-embed job {_JOB}{where} is moving documents from {name} to docs__t. "
                     f"Abort or finish the job first.",
            "reason": "collection_busy", "collection": name, "job_id": _JOB}


def _busy_engine_catalog(requests: list, *, body: dict | None = None):
    """A real ``HttpCatalogClient`` whose transport answers the delete route as engine 0.1.202 does while a re-embed
    job holds the collection (409 ``collection_busy``); every request is recorded."""
    answer = body if body is not None else {
        "error": "cannot delete old: re-embed job 7f3e0000-0000-0000-0000-000000000001 (running) is moving documents "
                 "from old to docs__1-1__voyage-model-a__v1. Abort or finish the job first.",
        "reason": "collection_busy", "collection": "old", "job_id": "7f3e0000-0000-0000-0000-000000000001",
    }

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path, json.loads(request.content) if request.content else {}))
        return httpx.Response(409, json=answer)

    cat = HttpCatalogClient(base_url="http://mock.test", _token="t")
    cat._client = httpx.Client(base_url="http://mock.test", transport=httpx.MockTransport(handle))
    return cat


def test_delete_of_a_collection_a_reembed_job_holds_names_the_job_and_exits_nonzero(runner, env_creds, mock_db) -> None:
    """RDR-229 P3.5a row 14 (engine 0.1.202): the delete route answers 409 ``collection_busy`` while a re-embed job
    holds the collection, and removes nothing. The CLI says which job, what to do, and does not claim a delete."""
    requests: list = []
    cat = _busy_engine_catalog(requests)
    with patch("nexus.catalog.factory.make_catalog_reader", return_value=cat), \
         patch("nexus.db.service_endpoint.guard_production_write"), \
         patch("nexus.db.collection_purge._purge_pipeline_db") as pipeline:
        result = _invoke(runner, mock_db, ["delete", "old", "--yes"])

    assert result.exit_code != 0, result.output
    out = " ".join(result.output.split())
    assert "old" in out and "7f3e0000-0000-0000-0000-000000000001" in out
    assert "re-embed job" in out and "holds" in out
    assert "wait for it to finish" in out.lower()
    assert "nx collection re-embed --abort 7f3e0000-0000-0000-0000-000000000001" in out
    assert "nothing was deleted" in out
    assert "Deleted:" not in out
    assert "unconfirmed" not in out, "the engine answered: the delete is refused, not unconfirmed"
    assert [(m, p) for m, p, _b in requests] == [("POST", "/v1/catalog/collections/delete")], "one attempt, never resent"
    pipeline.assert_not_called()


_STATE_ADVICE = [
    ("running", ["wait for it to finish", "--status " + _JOB, "--abort " + _JOB], ["--resume"]),
    ("cutover", ["wait for it to finish", "--status " + _JOB, "--abort " + _JOB], ["--resume"]),
    ("planned", ["wait for it to start and finish", "--status " + _JOB, "--abort " + _JOB], ["--resume"]),
    ("paused", ["resume it and let it finish", "--resume " + _JOB, "--abort " + _JOB], ["wait for it"]),
    ("failed", ["cancel it", "--abort " + _JOB, "--resume " + _JOB + "` retries it"], ["wait for it"]),
]


@pytest.mark.parametrize(("state", "says", "never"), _STATE_ADVICE)
def test_delete_busy_advice_follows_the_jobs_state(runner, env_creds, mock_db, state, says, never) -> None:
    """The engine's fence holds planned, running, paused, cutover and failed jobs; the way out differs, and the state
    is only in the engine's text, so it is read from there."""
    cat = _busy_engine_catalog([], body=_busy_body(state))
    with patch("nexus.catalog.factory.make_catalog_reader", return_value=cat), \
         patch("nexus.db.service_endpoint.guard_production_write"):
        result = _invoke(runner, mock_db, ["delete", "old", "--yes"])
    out = " ".join(result.output.split())
    assert result.exit_code != 0
    assert f"({state})" in out
    for piece in says:
        assert piece in out.lower(), (piece, out)
    for piece in never:
        assert piece not in out.lower(), (piece, out)


def test_delete_busy_with_a_state_it_cannot_read_prints_the_engines_message_beside_the_advice(
    runner, env_creds, mock_db,
) -> None:
    body = {"error": "re-embed work is under way on old", "reason": "collection_busy", "collection": "old",
            "job_id": _JOB}
    cat = _busy_engine_catalog([], body=body)
    with patch("nexus.catalog.factory.make_catalog_reader", return_value=cat), \
         patch("nexus.db.service_endpoint.guard_production_write"):
        result = _invoke(runner, mock_db, ["delete", "old", "--yes"])
    out = " ".join(result.output.split())
    assert result.exit_code != 0
    assert "The engine said: re-embed work is under way on old" in out
    assert f"--status {_JOB}" in out and f"--abort {_JOB}" in out


def test_prune_names_a_busy_collection_and_does_not_call_it_unconfirmed(
    runner, env_creds, mock_db, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-embed job holds the orphan: the delete is refused (nothing removed), which is neither deleted nor unconfirmed."""
    _serve_models(monkeypatch, _VOYAGE_MODELS)
    mock_db.embedding_mode.return_value = "voyage"
    mock_db.list_collections.return_value = [{"name": _ORPHAN, "count": 1}]
    fake = CascadeCounts(
        failures=["service deleteCollection failed: HTTP 409"], service_delete_failed=True,
        collection_busy={"collection": _ORPHAN, "job_id": _JOB, "state": "paused", "message": "x"},
    )
    with patch("nexus.db.collection_purge.purge_collection_cascade", return_value=fake):
        result = _invoke(runner, mock_db, ["prune", "--yes"])
    out = " ".join(result.output.split())
    assert result.exit_code != 0
    assert "Deleted 0 collection(s)." in out
    assert f"re-embed job {_JOB} (paused) holds collection '{_ORPHAN}'" in out
    assert f"--resume {_JOB}" in out and "nx collection prune --yes" in out
    assert "unconfirmed" not in out and "warn:" not in out


def test_reindex_names_a_busy_collection_and_does_not_say_the_delete_may_have_happened(
    runner, env_creds, mock_db, tmp_path,
) -> None:
    doc_file = tmp_path / "doc.md"
    doc_file.write_text("# Doc\ncontent")
    vr = _setup_reindex_mock(mock_db, [{"source_path": str(doc_file)}], [{"source_path": str(doc_file)}])
    fake = CascadeCounts(
        failures=["service deleteCollection failed: HTTP 409"], service_delete_failed=True,
        collection_busy={"collection": "knowledge__test", "job_id": _JOB, "state": "running", "message": "x"},
    )
    with patch("nexus.commands.collection._t3", return_value=mock_db), \
         patch("nexus.catalog.factory.make_catalog_reader", return_value=MagicMock()), \
         patch("nexus.db.collection_purge.purge_collection_cascade", return_value=fake), \
         patch("nexus.corpus.ensure_collection_registered") as register, \
         patch("nexus.doc_indexer.index_markdown", return_value=1) as index_md, \
         patch("nexus.db.t3.verify_collection_deep", return_value=vr):
        result = runner.invoke(main, ["collection", "reindex", "knowledge__test"])
    out = " ".join(result.output.split())
    assert result.exit_code != 0
    assert f"reindex of 'knowledge__test' stopped: re-embed job {_JOB} (running) holds collection 'knowledge__test'" in out
    assert "nx collection reindex knowledge__test" in out
    assert "may or may not" not in out and "unconfirmed" not in out and "warn:" not in out
    index_md.assert_not_called()
    register.assert_not_called()


def test_delete_busy_is_read_from_the_reason_not_from_any_409(runner, env_creds, mock_db) -> None:
    """A 409 with another body (the label-ambiguous refusal) keeps the unconfirmed-delete answer."""
    cat = _busy_engine_catalog([], body={"error": "collection 'old' is held by 2 live rows",
                                         "reason": "collection_label_ambiguous", "collection": "old"})
    with patch("nexus.catalog.factory.make_catalog_reader", return_value=cat), \
         patch("nexus.db.service_endpoint.guard_production_write"):
        result = _invoke(runner, mock_db, ["delete", "old", "--yes"])

    assert result.exit_code != 0
    assert "re-embed job" not in result.output and "unconfirmed" in result.output
