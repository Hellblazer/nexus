# SPDX-License-Identifier: AGPL-3.0-or-later
"""A shared cache of REAL MinerU runs for the PDF indexer tests (Sam 2026-11-19, suite audit item 4).

Those tests index a PDF through the default extractor, MinerU, so they prove the indexer on the MinerU path; the
cost is one MinerU child process (about 10 s locally, 50-80 s on a loaded CI shard) per test, and several of them
index the same fixture PDF. ``CachedMinerU`` wraps ``PDFExtractor._mineru_run_subprocess``: the first run of a
given input is the real child, and later runs of the same input in the same test session get that run's result.
Everything after the child (the fallback ladder, the quality gate, the metadata) still runs for real.

Keyed on the PDF's bytes, the page range, the formula flag and the installed MinerU version. Stored as JSON under
a directory that lives for one test session, shared by its xdist workers. A test whose subject is MinerU itself
(its ladder, timeouts, memory ceiling, formula output, or what the child spawns) must not opt in.
Tests: tests/test_mineru_extraction_cache.py.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

RunFn = Callable[..., tuple[str, list[dict], list[dict]]]


def cache_key(pdf_path: Path, start: int, end: int | None, formula_enable: bool, mineru_version: str) -> str:
    digest = hashlib.sha256()
    digest.update(Path(pdf_path).read_bytes())
    digest.update(json.dumps([start, end, bool(formula_enable), mineru_version]).encode())
    return digest.hexdigest()


class CachedMinerU:
    """Call-compatible with ``PDFExtractor._mineru_run_subprocess`` (the extractor comes first)."""

    def __init__(self, real: RunFn, directory: Path, mineru_version: str) -> None:
        self._real = real
        self._dir = Path(directory)
        self._version = mineru_version

    def __call__(self, extractor: Any, pdf_path: Path, start: int, end: int | None,
                 formula_enable: bool = True) -> tuple[str, list[dict], list[dict]]:
        try:
            path = self._dir / f"{cache_key(pdf_path, start, end, formula_enable, self._version)}.json"
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError:  # an unreadable PDF or an unwritable cache: the real run decides, uncached
            return self._real(extractor, pdf_path, start, end, formula_enable)
        if (hit := _read(path)) is not None:
            return hit
        # Single flight: a worker that misses while another runs the same input waits for that run, then reads it.
        with _locked(path.with_suffix(".lock")):
            if (hit := _read(path)) is not None:
                return hit
            result = self._real(extractor, pdf_path, start, end, formula_enable)
            with contextlib.suppress(OSError):  # the cache is an optimisation: a failed write costs a later run
                tmp = path.with_suffix(f".{os.getpid()}.{id(result)}.tmp")
                tmp.write_text(json.dumps(list(result)), encoding="utf-8")
                os.replace(tmp, path)  # whole file or none, for a reader at the same moment
            return result


def _read(path: Path) -> tuple[str, list[dict], list[dict]] | None:
    try:
        text, pages, blocks = json.loads(path.read_text(encoding="utf-8"))
        return text, pages, blocks
    except (OSError, ValueError, TypeError):
        return None  # absent or torn: a real run


@contextlib.contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    """An exclusive flock on *lock_path* (POSIX); on a platform without fcntl, no lock (a race costs a second run)."""
    try:
        import fcntl  # noqa: PLC0415 - POSIX only
    except ImportError:
        yield
        return
    with open(lock_path, "a+b") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def mineru_version() -> str:
    try:
        from importlib.metadata import version  # noqa: PLC0415

        return version("mineru")
    except Exception:  # noqa: BLE001 - no MinerU installed: the tests that need it skip on their own
        return "absent"


@pytest.fixture
def shared_mineru_extraction(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Opt a test into the session's shared real-MinerU cache; import it into the test module to use it."""
    import nexus.pdf_extractor as pe  # noqa: PLC0415

    base = tmp_path_factory.getbasetemp()
    # Under xdist each worker's basetemp is popen-gwN inside the run's own root, which the workers share;
    # without xdist the basetemp itself is this session's.
    root = base.parent if os.environ.get("PYTEST_XDIST_WORKER") else base
    directory = root / "mineru-extraction-cache"
    cache = CachedMinerU(pe.PDFExtractor._mineru_run_subprocess, directory, mineru_version())
    monkeypatch.setattr(pe.PDFExtractor, "_mineru_run_subprocess",
                        lambda self, *a, **k: cache(self, *a, **k))
    return directory
