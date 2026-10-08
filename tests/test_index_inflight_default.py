# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx index repo`` keeps more than two files' writes in flight by default.

Measured 2026-10-08 on a local engine (900-chunk repo slice, bge-768 ONNX, M4 Max, 16 cores,
baseline engine jar, T2 ``nexus/index-embedding-throughput-2026-10-08``): with the old default
of 2 file workers the run averaged 1.13 requests in flight and nothing was in flight 34% of the
time, because a source file over the 16-chunk local cap is written one request at a time by its
own worker (the ChunkBatcher flush pool never sees it). 4 file workers averaged 2.1 in flight and
finished the same slice 15-30% sooner; 6 and 8 added 12% and 2% more. 4 is the default.

These tests pin the default, the quota arithmetic it rests on, and the loop behaviour at that
width: the workers really overlap, never exceed the cap, and a failure still fails the run. The
last test (engine-backed, ``integration``) pins that a concurrent run writes the same manifest
rows, position for position, as a serial one.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

#: The default this change sets. Stated here as a literal on purpose: the tests below compare the
#: shipped constant against it, so a silent edit of the constant is a failing test, not a quiet
#: change of what a user's index run does to the engine.
EXPECTED_DEFAULT = 4


def _service_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NX_INDEX_CONCURRENCY", raising=False)
    monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
    monkeypatch.delenv("NX_STORAGE_BACKEND_VECTORS", raising=False)
    monkeypatch.delenv("NX_STORAGE_BACKEND_CATALOG", raising=False)


def _files(n: int) -> list[tuple[float, Path]]:
    return [(float(n - i), Path(f"/repo/f{i}.py")) for i in range(n)]


class TestDefaultWidth:
    def test_service_default_is_four(self, monkeypatch):
        from nexus.indexer_utils import resolve_index_concurrency

        _service_env(monkeypatch)
        assert resolve_index_concurrency() == EXPECTED_DEFAULT

    def test_named_constant_carries_the_default(self):
        from nexus.indexer_utils import DEFAULT_SERVICE_INDEX_CONCURRENCY

        assert DEFAULT_SERVICE_INDEX_CONCURRENCY == EXPECTED_DEFAULT

    def test_env_override_still_wins_over_the_default(self, monkeypatch):
        from nexus.indexer_utils import resolve_index_concurrency

        _service_env(monkeypatch)
        monkeypatch.setenv("NX_INDEX_CONCURRENCY", "2")
        assert resolve_index_concurrency() == 2

    def test_default_writers_plus_flush_workers_stay_inside_the_write_quota(self):
        """File workers write oversize files directly while the ChunkBatcher pool flushes: both
        count against the per-collection concurrent-write quota (``nexus.db.limits``)."""
        from nexus.db.limits import QUOTAS
        from nexus.indexer import FLUSH_CONCURRENCY
        from nexus.indexer_utils import DEFAULT_SERVICE_INDEX_CONCURRENCY

        assert (
            DEFAULT_SERVICE_INDEX_CONCURRENCY + FLUSH_CONCURRENCY <= QUOTAS.MAX_CONCURRENT_WRITES
        )

    def test_non_service_backend_still_defaults_to_one(self, monkeypatch):
        from nexus.indexer_utils import resolve_index_concurrency

        monkeypatch.delenv("NX_INDEX_CONCURRENCY", raising=False)
        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
        monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "chroma")
        assert resolve_index_concurrency() == 1


class TestLoopAtTheDefaultWidth:
    def test_default_width_files_are_in_flight_at_once(self, monkeypatch):
        """Counts simultaneous workers, not exit codes: a barrier that only opens when
        EXPECTED_DEFAULT files are inside ``index_one`` together. At a narrower default the
        barrier times out and the run fails."""
        from nexus.indexer_utils import resolve_index_concurrency, run_file_loop

        _service_env(monkeypatch)
        barrier = threading.Barrier(EXPECTED_DEFAULT, timeout=15)

        def index_one(file, score, timers):
            barrier.wait()
            return 1

        written = run_file_loop(
            _files(EXPECTED_DEFAULT * 3), index_one,
            concurrency=resolve_index_concurrency(),
            on_file=None, on_stage_timers=None,
        )
        assert written == EXPECTED_DEFAULT * 3

    def test_in_flight_never_exceeds_the_default_cap(self, monkeypatch):
        from nexus.indexer_utils import resolve_index_concurrency, run_file_loop

        _service_env(monkeypatch)
        lock = threading.Lock()
        state = {"now": 0, "peak": 0}

        def index_one(file, score, timers):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.02)
            with lock:
                state["now"] -= 1
            return 1

        run_file_loop(
            _files(60), index_one, concurrency=resolve_index_concurrency(),
            on_file=None, on_stage_timers=None,
        )
        assert state["peak"] <= EXPECTED_DEFAULT
        assert state["peak"] >= EXPECTED_DEFAULT - 1, "the workers were not actually used"

    def test_a_failing_file_still_fails_the_run_and_cancels_the_rest(self, monkeypatch):
        from nexus.indexer_utils import resolve_index_concurrency, run_file_loop

        _service_env(monkeypatch)

        class Boom(Exception):
            pass

        started: list[str] = []
        lock = threading.Lock()
        # All EXPECTED_DEFAULT workers are inside together before anything fails, so the failure
        # happens at the full width rather than while the pool is still filling.
        barrier = threading.Barrier(EXPECTED_DEFAULT, timeout=15)

        def index_one(file, score, timers):
            with lock:
                started.append(file.name)
            barrier.wait()
            if file.name == "f1.py":
                raise Boom("flush failed")
            time.sleep(0.05)
            return 1

        total = EXPECTED_DEFAULT * 6
        with pytest.raises(Boom):
            run_file_loop(
                _files(total), index_one, concurrency=resolve_index_concurrency(),
                on_file=None, on_stage_timers=None,
            )
        assert len(started) < total, "pending files must be cancelled once a file fails"


@pytest.mark.integration
def test_concurrent_run_writes_the_same_manifest_as_a_serial_run(
    t2_service_env, tmp_path: Path, monkeypatch,
) -> None:
    """Position-for-position: the (file, position, chash) rows of a default-width run equal a
    serial run's. One file is large enough to take the multi-request write path (more than the
    16-chunk local cap), the others are single-request."""
    import subprocess

    from click.testing import CliRunner

    from nexus.cli import main
    from tests._catalog_fixture_ops import active_reader

    repo = tmp_path / "inflight-parity"
    repo.mkdir()
    # ~40 KB of distinct code: the code chunker cuts about 1.5 KB per chunk, so this is over the
    # 16-chunk local cap and takes the multi-request write path.
    body = "".join(
        f"def f{i}(x):\n    total = x + {i}\n    for k in range({i % 7 + 2}):\n"
        f"        total = total * {i + 3} + k\n    return total - {i * 11}\n\n"
        for i in range(400)
    )
    (repo / "big.py").write_text(body)
    for n in range(4):
        (repo / f"small{n}.py").write_text(
            "".join(f"def g{n}_{i}(y):\n    return y * {i + 1}\n\n" for i in range(12))
        )
    for cmd in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "t@t.invalid"],
        ["git", "config", "user.name", "t"],
        ["git", "add", "."],
        ["git", "commit", "-m", "seed"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)

    def manifest_rows() -> dict[str, list[tuple[int, str]]]:
        reader = active_reader()
        docs = [d for d in reader.all_documents() if str(d.file_path).endswith(".py")]
        by_doc = reader.get_manifests([str(d.tumbler) for d in docs])
        return {
            Path(str(d.file_path)).name: sorted((r.position, r.chash) for r in by_doc[str(d.tumbler)])
            for d in docs
        }

    runner = CliRunner()
    args = ["index", "repo", str(repo), "--no-taxonomy"]

    monkeypatch.setenv("NX_INDEX_CONCURRENCY", "1")
    serial = runner.invoke(main, args)
    assert serial.exit_code == 0, serial.output
    before = manifest_rows()
    assert set(before) == {"big.py", "small0.py", "small1.py", "small2.py", "small3.py"}
    assert len(before["big.py"]) > 16, "the large file must take the multi-request write path"

    monkeypatch.delenv("NX_INDEX_CONCURRENCY")
    wide = runner.invoke(main, [*args, "--force"])
    assert wide.exit_code == 0, wide.output
    after = manifest_rows()

    assert after == before
    assert {c for rows in after.values() for _, c in rows} == {c for rows in before.values() for _, c in rows}
