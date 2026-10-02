# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-ccre5: ``minilm_direct.ensure_artifact`` under concurrent first use.

CI run 36962623595 (the first run of the self-hosted qwen-linux runner, cold
``~/.cache``): eight xdist workers all downloaded into the SAME
``<cache>/onnx.tar.gz`` and extracted into the same directory. A worker that
had verified its own streamed digest then opened a file a peer was truncating,
and ``tar.extractall`` died with ``EOFError: Compressed file ended before the
end-of-stream marker was reached``. Hosted shards hid it by priming the model
through an actions/cache step; real users hit it whenever several ``nx``
processes make first use together.

These tests never touch the real model or S3: the archive is a small fake
whose members are incompressible (so a truncated copy is detectable), served
either by a patched ``httpx.stream`` (threads, with a barrier that forces the
downloads to overlap) or by a real local HTTP server on port 0 (processes).
They own their cache directory through ``CACHE_DIR_ENV`` and so are
independent of the shared suite cache.
"""
from __future__ import annotations

import hashlib
import http.server
import io
import os
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path

import pytest

from nexus.db import minilm_direct

_N_CALLERS = 6
_CHUNK = 64 * 1024


def _fake_archive() -> tuple[bytes, str]:
    """A tar.gz shaped like the real artifact: top-level ``onnx/`` holding
    ``model.onnx`` and ``tokenizer.json``. The model member is pseudo-random
    (deterministic) so gzip cannot shrink it and a short read shows."""
    model = hashlib.shake_256(b"nexus-ccre5").digest(1_500_000)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("onnx/model.onnx", model), ("onnx/tokenizer.json", b"{}")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    blob = buf.getvalue()
    return blob, hashlib.sha256(blob).hexdigest()


class _FakeStream:
    """Stands in for ``httpx.stream(...)``'s context manager. ``on_first_chunk``
    runs after the first chunk is yielded, keeping the download mid-flight."""

    def __init__(self, blob: bytes, on_first_chunk=None) -> None:
        self._blob = blob
        self._on_first_chunk = on_first_chunk

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self, chunk_size: int = _CHUNK):
        first = True
        for start in range(0, len(self._blob), chunk_size):
            yield self._blob[start:start + chunk_size]
            if first:
                first = False
                if self._on_first_chunk is not None:
                    self._on_first_chunk()
            time.sleep(0.001)


def _cache_listing(cache: Path) -> list[str]:
    return sorted(p.name for p in (cache / minilm_direct.MODEL_NAME).iterdir())


@pytest.fixture
def cold_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache = tmp_path / "onnx_models"
    monkeypatch.setenv(minilm_direct.CACHE_DIR_ENV, str(cache))
    return cache


def test_a_peer_starting_a_download_cannot_corrupt_a_caller_about_to_extract(
    cold_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CI failure, forced. Caller 0 downloads and verifies the archive and
    is about to open it; peers then begin their own downloads and are mid-flight
    when caller 0 reads. With one shared ``onnx.tar.gz`` the peers' open(\"wb\")
    has truncated it, and caller 0 dies with EOFError."""
    blob, digest = _fake_archive()
    monkeypatch.setattr(minilm_direct, "_MODEL_SHA256", digest)

    holding = threading.Event()  # caller 0 has verified, is about to extract
    peer_midflight = threading.Event()  # a peer has written its first chunk
    release = threading.Event()  # lets caller 0 open its archive
    first_done = threading.Event()  # caller 0 has returned or raised
    real_open = tarfile.open
    first_open = threading.Lock()  # acquired (never released) by caller 0's open
    downloads: list[int] = []

    def _open(*a, **kw):
        if first_open.acquire(blocking=False):
            holding.set()
            # Tolerant: a lock-based fix keeps the peers out of the download,
            # so peer_midflight never fires; do not deadlock on it.
            release.wait(timeout=10)
        return real_open(*a, **kw)

    def _stream(*_a, **_kw):
        downloads.append(1)
        if len(downloads) == 1:
            return _FakeStream(blob)

        def _peer_hook() -> None:
            # Park mid-download, file truncated and short, until caller 0 has
            # read (or failed to read) its archive.
            peer_midflight.set()
            first_done.wait(timeout=15)

        return _FakeStream(blob, on_first_chunk=_peer_hook)

    monkeypatch.setattr(tarfile, "open", _open)
    monkeypatch.setattr("httpx.stream", _stream)

    results: list[Path | BaseException] = []
    lock = threading.Lock()

    def _call(*, is_first: bool = False) -> None:
        try:
            out: Path | BaseException = minilm_direct.ensure_artifact()
        except BaseException as exc:  # noqa: BLE001 — the test reports it
            out = exc
        if is_first:
            first_done.set()
        with lock:
            results.append(out)

    first = threading.Thread(target=_call, kwargs={"is_first": True})
    first.start()
    assert holding.wait(timeout=30), "caller 0 never reached extraction"
    peers = [threading.Thread(target=_call) for _ in range(_N_CALLERS - 1)]
    for t in peers:
        t.start()
    # A lock-based fix keeps the peers out of the download, so this may time out.
    peer_midflight.wait(timeout=5)
    release.set()
    for t in [first, *peers]:
        t.join(timeout=60)

    assert len(results) == _N_CALLERS
    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, f"concurrent first use failed: {errors!r}"
    artifact = minilm_direct.artifact_dir()
    assert set(results) == {artifact}
    assert (artifact / "model.onnx").stat().st_size == 1_500_000
    assert (artifact / "tokenizer.json").read_bytes() == b"{}"
    assert len(downloads) >= 1  # non-vacuity: the fake served real downloads
    # No temp archive or extract dir survives; only the published artifact.
    assert _cache_listing(cold_cache) == ["onnx"]


def test_a_peer_publishing_during_our_download_is_success(
    cold_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rename onto an artifact a peer already published must not raise."""
    blob, digest = _fake_archive()
    monkeypatch.setattr(minilm_direct, "_MODEL_SHA256", digest)
    artifact = minilm_direct.artifact_dir()

    class _PeerWins(_FakeStream):
        def iter_bytes(self, chunk_size: int = _CHUNK):
            yield from super().iter_bytes(chunk_size)
            # The peer finishes first: a complete artifact appears now.
            artifact.mkdir(parents=True)
            (artifact / "model.onnx").write_bytes(b"peer")
            (artifact / "tokenizer.json").write_bytes(b"{}")

    monkeypatch.setattr(
        "httpx.stream",
        lambda *_a, **_kw: _PeerWins(blob),
    )
    assert minilm_direct.ensure_artifact() == artifact
    assert (artifact / "model.onnx").read_bytes() == b"peer"
    assert _cache_listing(cold_cache) == ["onnx"]


def test_sha256_mismatch_still_fails_loud_and_leaves_no_temp_files(
    cold_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blob, _ = _fake_archive()
    monkeypatch.setattr(minilm_direct, "_MODEL_SHA256", "0" * 64)
    monkeypatch.setattr(
        "httpx.stream",
        lambda *_a, **_kw: _FakeStream(blob),
    )
    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        minilm_direct.ensure_artifact()
    assert not minilm_direct.artifact_dir().exists()
    assert _cache_listing(cold_cache) == []


_CHILD = """
import sys
from nexus.db import minilm_direct
minilm_direct._MODEL_DOWNLOAD_URL = sys.argv[1]
minilm_direct._MODEL_SHA256 = sys.argv[2]
a = minilm_direct.ensure_artifact()
assert (a / "model.onnx").stat().st_size == 1_500_000, "short model.onnx"
assert (a / "tokenizer.json").is_file()
print("OK")
"""


def test_concurrent_first_use_processes_against_a_local_http_server(
    cold_cache: Path,
) -> None:
    """The real shape: separate processes, a real HTTP stream, port 0."""
    blob, digest = _fake_archive()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — http.server protocol name
            self.send_response(200)
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            try:
                for start in range(0, len(blob), _CHUNK):
                    self.wfile.write(blob[start:start + _CHUNK])
                    self.wfile.flush()
                    time.sleep(0.03)  # keep every caller mid-download together
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_a: object) -> None:
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    serve = threading.Thread(target=server.serve_forever, daemon=True)
    serve.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/onnx.tar.gz"
        env = {**os.environ, minilm_direct.CACHE_DIR_ENV: str(cold_cache)}
        procs = [
            subprocess.Popen(  # noqa: S603 — fixed argv, test-owned child
                [sys.executable, "-c", _CHILD, url, digest],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(_N_CALLERS)
        ]
        outs = [p.communicate(timeout=120) for p in procs]
    finally:
        server.shutdown()
        server.server_close()
        serve.join(timeout=10)

    for p, (out, err) in zip(procs, outs, strict=True):
        lines = out.strip().splitlines()  # structlog also writes to stdout
        assert p.returncode == 0 and lines and lines[-1] == "OK", (
            f"child failed rc={p.returncode}: {out[-800:]} {err[-1500:]}"
        )
    assert _cache_listing(cold_cache) == ["onnx"]
