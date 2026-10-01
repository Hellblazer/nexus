# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-eoido: a throttled index run, end to end through the CLI.

Real ``nx index repo`` command -> real ``_run_index`` -> a REAL ``ChunkBatcher`` whose writes the
service throttles. (``index_repository`` itself is replaced by a call to ``_run_index``: its lock,
credential and engine-token preamble needs a live service and adds nothing to this path.) A hand-built stats dict (tests/test_4s1ww_chunk_flush_failure_reporting.py)
cannot reach the path that matters: throttled flush -> breaker -> deferred files -> stats -> summary ->
exit code.
"""
from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from nexus.chunk_batcher import ChunkBatcher
from nexus.cli import main
from nexus.db.http_vector_client import HttpVectorClient
from nexus.rate_brake import reset_brake
from tests.test_4s1ww_chunk_flush_failure_reporting import _reg, _service_mode_patches  # noqa: PLC2701 — shared fixture shape, kept in one place


def _throttle_error(status: int, retry_after: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://engine.invalid/v1/vectors/upsert-chunks")
    response = httpx.Response(status, headers={"Retry-After": retry_after}, request=request)
    return httpx.HTTPStatusError(f"{status} from engine", request=request, response=response)


@pytest.fixture
def repo_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / "myrepo"
    d.mkdir()
    (d / ".git").mkdir()
    return d


@pytest.fixture
def mock_reg():
    reg = MagicMock()
    reg.get.return_value = {"collection": "code__myrepo"}
    return reg


def _run_cli(repo_dir, mock_reg, monkeypatch, *, write, files: int, threshold: int):
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
    monkeypatch.setenv("NX_LOCAL", "0")
    reset_brake()
    batchers: list[ChunkBatcher] = []

    def factory(*, flush, **kw):
        kw.update(max_chunks=1, flush_concurrency=1, throttle_breaker_threshold=threshold)
        batcher = ChunkBatcher(flush=write, **kw)
        for i in range(files):
            assert batcher.add(f"f{i}.py", "code__repo", [f"id{i}"], [f"doc {i}"], [{}])
        batchers.append(batcher)
        return batcher

    from nexus.indexer import _run_index

    # The command's own repo dir carries an empty marker directory (enough for the CLI's checks, not
    # for file listing), so the run proper indexes a plain sibling directory, the same shape the
    # sibling test module drives ``_run_index`` with.
    work = repo_dir.parent / "work"
    work.mkdir(exist_ok=True)
    (work / "hello.py").write_text("x = 1\n")

    def run_for_real(_repo, _registry, **_kw):
        return _run_index(work, _reg())

    db = MagicMock(spec=HttpVectorClient)
    with _service_mode_patches(db), patch("nexus.chunk_batcher.ChunkBatcher", factory), patch(
        "nexus.commands.index._registry", return_value=mock_reg,
    ), patch("nexus.indexer.index_repository", side_effect=run_for_real):
        result = CliRunner().invoke(main, ["index", "repo", str(repo_dir)])
    return result, batchers


def test_a_throttled_run_stops_sending_names_the_throttle_and_exits_nonzero(
    repo_dir, mock_reg, monkeypatch,
) -> None:
    sent: list[list[str]] = []

    def throttled_write(_collection, ids, _docs, _metas, _file_contexts=None):
        sent.append(list(ids))
        raise _throttle_error(429, "30")

    result, batchers = _run_cli(repo_dir, mock_reg, monkeypatch, write=throttled_write, files=4, threshold=2)

    assert result.exit_code != 0, result.output
    assert len(sent) == 2, f"two sends should open the breaker, got {len(sent)}: {sent}\n{result.output}\n{result.exception!r}"
    assert batchers[0].throttle_breaker_open
    assert re.search(r"4/\d+ file\(s\) throttled by the service", result.stdout), result.stdout
    assert "30s" in result.stdout and "Retry-After" in result.stdout, result.stdout
    assert "failed to flush chunk uploads" not in result.stdout, result.stdout
    error_line = next(line for line in result.output.splitlines() if line.startswith("Error:"))
    assert "throttled" in error_line and "consecutive throttled flushes" in error_line, error_line


def test_one_throttled_flush_below_the_breaker_still_exits_nonzero_and_other_files_land(
    repo_dir, mock_reg, monkeypatch,
) -> None:
    calls: list[list[str]] = []

    def first_throttled(_collection, ids, _docs, _metas, _file_contexts=None):
        calls.append(list(ids))
        if len(calls) == 1:
            raise _throttle_error(429, "7")

    result, batchers = _run_cli(repo_dir, mock_reg, monkeypatch, write=first_throttled, files=3, threshold=3)

    assert result.exit_code != 0, result.output
    assert len(calls) == 3, "the breaker is not open after one throttled flush: every flush is sent"
    assert not batchers[0].throttle_breaker_open
    assert re.search(r"1/\d+ file\(s\) throttled by the service", result.stdout), result.stdout
    assert "7s" in result.stdout, result.stdout
    assert "consecutive" not in result.stdout, result.stdout
