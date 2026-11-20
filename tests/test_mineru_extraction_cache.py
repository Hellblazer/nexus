# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shared MinerU extraction cache the PDF indexer tests opt into (Sam 2026-11-19, suite audit item 4, T2
nexus/sam-ruling-suite-audit-and-cleanup-2026-11-19): one REAL MinerU run per distinct input per test session,
so those tests keep proving the MinerU path while not paying for it once per test."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._mineru_cache import CachedMinerU, cache_key


def _pdf(tmp_path: Path, name: str, body: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(body)
    return p


class _Real:
    """Stands in for the real subprocess run: records each call and returns a distinct, JSON-shaped result."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, extractor, pdf_path: Path, start: int, end: int | None, formula_enable: bool = True):
        self.calls.append((pdf_path.name, start, end, formula_enable))
        return (f"text of {pdf_path.name} {start}-{end} f={formula_enable}", [{"page": start}], [{"block": 1}])


def test_an_identical_input_runs_mineru_once_and_returns_the_real_result(tmp_path: Path) -> None:
    real = _Real()
    cache = CachedMinerU(real, tmp_path / "cache", mineru_version="2.0.0")
    a = _pdf(tmp_path, "a.pdf", b"%PDF-1.4 same bytes")
    b = _pdf(tmp_path, "b.pdf", b"%PDF-1.4 same bytes")  # another path, identical content
    first = cache(None, a, 0, None)
    second = cache(None, b, 0, None)
    assert real.calls == [("a.pdf", 0, None, True)]
    assert second == first == ("text of a.pdf 0-None f=True", [{"page": 0}], [{"block": 1}])


def test_any_difference_in_the_input_is_a_real_run(tmp_path: Path) -> None:
    real = _Real()
    cache = CachedMinerU(real, tmp_path / "cache", mineru_version="2.0.0")
    a = _pdf(tmp_path, "a.pdf", b"%PDF-1.4 one")
    other = _pdf(tmp_path, "o.pdf", b"%PDF-1.4 two")
    cache(None, a, 0, None)
    cache(None, a, 0, None, formula_enable=False)  # the ladder's rung 2 is its own run
    cache(None, a, 0, 3)
    cache(None, a, 1, None)
    cache(None, other, 0, None)
    assert len(real.calls) == 5
    CachedMinerU(real, tmp_path / "cache", mineru_version="2.1.0")(None, a, 0, None)  # a MinerU upgrade is a miss
    assert len(real.calls) == 6


def test_a_second_worker_reads_the_first_workers_run(tmp_path: Path) -> None:
    shared = tmp_path / "cache"
    a = _pdf(tmp_path, "a.pdf", b"%PDF-1.4 shared")
    first_real, second_real = _Real(), _Real()
    got = CachedMinerU(first_real, shared, mineru_version="2.0.0")(None, a, 0, None)
    again = CachedMinerU(second_real, shared, mineru_version="2.0.0")(None, a, 0, None)
    assert len(first_real.calls) == 1 and second_real.calls == [] and again == got


def test_a_failed_run_is_not_cached(tmp_path: Path) -> None:
    a = _pdf(tmp_path, "a.pdf", b"%PDF-1.4 fails once")
    attempts = []

    def flaky(extractor, pdf_path, start, end, formula_enable=True):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("MinerU child died")
        return ("ok", [], [])

    cache = CachedMinerU(flaky, tmp_path / "cache", mineru_version="2.0.0")
    with pytest.raises(RuntimeError):
        cache(None, a, 0, None)
    assert cache(None, a, 0, None) == ("ok", [], []) and len(attempts) == 2


def test_a_torn_cache_file_is_a_real_run(tmp_path: Path) -> None:
    real = _Real()
    shared = tmp_path / "cache"
    a = _pdf(tmp_path, "a.pdf", b"%PDF-1.4 torn")
    shared.mkdir()
    (shared / f"{cache_key(a, 0, None, True, '2.0.0')}.json").write_text('{"text": ', encoding="utf-8")
    CachedMinerU(real, shared, mineru_version="2.0.0")(None, a, 0, None)
    assert len(real.calls) == 1
    json.loads((shared / f"{cache_key(a, 0, None, True, '2.0.0')}.json").read_text(encoding="utf-8"))


def test_two_workers_asking_at_once_run_mineru_once(tmp_path: Path) -> None:
    """Single flight: adjacent tests on two xdist workers miss together; the second waits for the first's run."""
    import threading
    import time

    started = threading.Event()
    calls: list[int] = []

    def slow_real(extractor, pdf_path, start, end, formula_enable=True):
        calls.append(1)
        started.set()
        time.sleep(0.3)
        return ("once", [], [])

    shared = tmp_path / "cache"
    a = _pdf(tmp_path, "a.pdf", b"%PDF-1.4 raced")
    got: list[tuple] = []
    first = threading.Thread(target=lambda: got.append(CachedMinerU(slow_real, shared, "2.0.0")(None, a, 0, None)))
    first.start()
    assert started.wait(5)
    got.append(CachedMinerU(slow_real, shared, "2.0.0")(None, a, 0, None))
    first.join(5)
    assert calls == [1] and got == [("once", [], []), ("once", [], [])]
