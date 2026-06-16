# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Standard bge ONNX provisioning for the Java service (RDR-160 P3.1, nexus-jknc2).

The CLI fetches the STANDARD (un-fused) export to the path the Java service reads.
These tests inject the downloader so nothing hits the network.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from nexus.db import service_bge_model as sbm

_JAVA_EMBEDDER = (
    Path(__file__).resolve().parents[2]
    / "service/src/main/java/dev/nexus/service/vectors/Bge768Embedder.java"
)


@pytest.fixture()
def bge_dir(tmp_path, monkeypatch):
    d = tmp_path / "onnx"
    monkeypatch.setenv("NX_SERVICE_BGE_DIR", str(d))
    # Lower the size floors so tiny fake payloads count as complete; the real
    # floors (200 MB / 100 KB) are exercised by test_truncated_model_triggers_refetch.
    monkeypatch.setattr(sbm, "_MIN_MODEL_BYTES", 1)
    monkeypatch.setattr(sbm, "_MIN_TOKENIZER_BYTES", 1)
    return d


def _fake_downloader(payloads: dict[str, bytes]):
    """Returns a downloader that writes a fixed payload keyed by URL suffix."""
    def _dl(url: str, dest: Path) -> None:
        for suffix, blob in payloads.items():
            if url.endswith(suffix):
                dest.write_bytes(blob)
                return
        raise AssertionError(f"unexpected download URL: {url}")
    return _dl


def test_fetch_lands_standard_model_and_tokenizer(bge_dir):
    dl = _fake_downloader({"onnx/model.onnx": b"MODEL", "tokenizer.json": b"TOK"})
    out = sbm.fetch_service_bge_onnx(downloader=dl)
    assert out == bge_dir
    assert (bge_dir / "model.onnx").read_bytes() == b"MODEL"
    assert (bge_dir / "tokenizer.json").read_bytes() == b"TOK"
    assert sbm.service_bge_model_present() is True


def test_fetch_is_idempotent_skips_when_present(bge_dir):
    bge_dir.mkdir(parents=True)
    (bge_dir / "model.onnx").write_bytes(b"X")
    (bge_dir / "tokenizer.json").write_bytes(b"Y")

    calls = []

    def _dl(url, dest):
        calls.append(url)

    sbm.fetch_service_bge_onnx(downloader=_dl)
    assert calls == []  # no re-download when both files already exist


def test_force_redownloads(bge_dir):
    bge_dir.mkdir(parents=True)
    (bge_dir / "model.onnx").write_bytes(b"OLD")
    (bge_dir / "tokenizer.json").write_bytes(b"OLD")
    dl = _fake_downloader({"onnx/model.onnx": b"NEW", "tokenizer.json": b"NEWTOK"})
    sbm.fetch_service_bge_onnx(force=True, downloader=dl)
    assert (bge_dir / "model.onnx").read_bytes() == b"NEW"


def test_offline_failure_is_loud_no_silent_fallback(bge_dir):
    def _dl(url, dest):
        raise OSError("name resolution failed")

    with pytest.raises(RuntimeError) as exc:
        sbm.fetch_service_bge_onnx(downloader=_dl)
    msg = str(exc.value)
    assert "will not boot" in msg
    assert "model_optimized.onnx" in msg  # warns off the wrong (fused) artifact
    assert str(bge_dir / "model.onnx") in msg
    # nothing half-written left claiming success
    assert sbm.service_bge_model_present() is False


def test_truncated_model_triggers_refetch(bge_dir, monkeypatch):
    # A too-small model.onnx (truncated download, or a quantized/wrong substitute)
    # must NOT satisfy idempotency — it is re-fetched, not silently served.
    monkeypatch.setattr(sbm, "_MIN_MODEL_BYTES", 100)
    bge_dir.mkdir(parents=True)
    (bge_dir / "model.onnx").write_bytes(b"x" * 10)        # below floor
    (bge_dir / "tokenizer.json").write_bytes(b"y")
    assert sbm.service_bge_model_present() is False

    dl = _fake_downloader({"onnx/model.onnx": b"x" * 200, "tokenizer.json": b"TOK"})
    sbm.fetch_service_bge_onnx(downloader=dl)
    assert (bge_dir / "model.onnx").read_bytes() == b"x" * 200  # re-fetched


def test_partial_failure_cleans_orphan_model(bge_dir):
    # Model download succeeds, tokenizer download fails: the lone model.onnx must
    # be removed so the dir does not read as a (partial) install.
    def _dl(url, dest):
        if url.endswith("onnx/model.onnx"):
            dest.write_bytes(b"MODEL")
            return
        raise OSError("connection reset")

    with pytest.raises(RuntimeError):
        sbm.fetch_service_bge_onnx(downloader=_dl)
    assert not (bge_dir / "model.onnx").exists()
    assert sbm.service_bge_model_present() is False


def test_env_override_directs_destination(tmp_path, monkeypatch):
    custom = tmp_path / "custom-bge"
    monkeypatch.setenv("NX_SERVICE_BGE_DIR", str(custom))
    assert sbm.service_bge_model_dir() == custom


def test_python_path_matches_java_default(monkeypatch):
    """Cross-language drift guard: the Python destination must equal the Java
    Bge768Embedder.DEFAULT_MODEL_PATH (sans ~ expansion). If the Java constant
    moves, this fails so the fetch target is updated in lockstep."""
    monkeypatch.delenv("NX_SERVICE_BGE_DIR", raising=False)
    src = _JAVA_EMBEDDER.read_text()
    # DEFAULT_MODEL_PATH = System.getProperty("user.home") + "/.cache/.../model.onnx"
    m = re.search(
        r'DEFAULT_MODEL_PATH\s*=\s*\n?\s*System\.getProperty\("user\.home"\)\s*\+\s*"([^"]+)"',
        src,
    )
    assert m is not None, "Java DEFAULT_MODEL_PATH literal not found"
    java_rel = m.group(1).lstrip("/")  # ".cache/nexus/onnx_models/.../onnx/model.onnx"
    java_full = Path.home() / java_rel
    py_model = sbm.service_bge_model_dir() / sbm.MODEL_FILENAME
    assert py_model == java_full
