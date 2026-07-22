# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""ms-marco cross-encoder ONNX provisioning for the Java service (RDR-188 P1.3).

The CLI fetches the pinned-revision HF artifacts to the path the Java engine's
``CrossEncoderReranker`` reads. Tests inject the downloader — nothing hits the
network. Mirrors ``test_service_bge_model.py``; the shared stream/digest
plumbing is imported from ``service_bge_model`` and covered there.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from nexus.db import service_crossencoder_model as scm

_JAVA_RERANKER = (
    Path(__file__).resolve().parents[2]
    / "service/src/main/java/dev/nexus/service/vectors/CrossEncoderReranker.java"
)


@pytest.fixture()
def ce_dir(tmp_path, monkeypatch):
    d = tmp_path / "onnx"
    monkeypatch.setenv("NX_SERVICE_CROSSENCODER_DIR", str(d))
    # Lower the size floors so tiny fake payloads count as complete; the real
    # floors are exercised by test_truncated_model_triggers_refetch.
    monkeypatch.setattr(scm, "_MIN_MODEL_BYTES", 1)
    monkeypatch.setattr(scm, "_MIN_TOKENIZER_BYTES", 1)
    return d


def _fake_downloader(payloads: dict[str, bytes]):
    def _dl(url: str, dest: Path) -> None:
        for suffix, blob in payloads.items():
            if url.endswith(suffix):
                dest.write_bytes(blob)
                return
        raise AssertionError(f"unexpected download URL: {url}")
    return _dl


def _pin_digests(monkeypatch, *, model: bytes | None = None, tokenizer: bytes | None = None):
    if model is not None:
        monkeypatch.setattr(scm, "_MODEL_SHA256", hashlib.sha256(model).hexdigest())
    if tokenizer is not None:
        monkeypatch.setattr(
            scm, "_TOKENIZER_SHA256", hashlib.sha256(tokenizer).hexdigest()
        )


def test_fetch_lands_model_and_tokenizer(ce_dir, monkeypatch):
    _pin_digests(monkeypatch, model=b"MODEL", tokenizer=b"TOK")
    dl = _fake_downloader({"/model.onnx": b"MODEL", "/tokenizer.json": b"TOK"})
    out = scm.fetch_service_crossencoder_onnx(downloader=dl)
    assert out == ce_dir
    assert (ce_dir / "model.onnx").read_bytes() == b"MODEL"
    assert (ce_dir / "tokenizer.json").read_bytes() == b"TOK"
    assert scm.service_crossencoder_model_present() is True


def test_fetch_is_idempotent_skips_when_present(ce_dir):
    ce_dir.mkdir(parents=True)
    (ce_dir / "model.onnx").write_bytes(b"X")
    (ce_dir / "tokenizer.json").write_bytes(b"Y")

    calls: list[str] = []

    def _dl(url, dest):
        calls.append(url)

    scm.fetch_service_crossencoder_onnx(downloader=_dl)
    assert calls == []


def test_force_redownloads(ce_dir, monkeypatch):
    ce_dir.mkdir(parents=True)
    (ce_dir / "model.onnx").write_bytes(b"OLD")
    (ce_dir / "tokenizer.json").write_bytes(b"OLD")
    _pin_digests(monkeypatch, model=b"NEW", tokenizer=b"NEWTOK")
    dl = _fake_downloader({"/model.onnx": b"NEW", "/tokenizer.json": b"NEWTOK"})
    scm.fetch_service_crossencoder_onnx(force=True, downloader=dl)
    assert (ce_dir / "model.onnx").read_bytes() == b"NEW"


def test_offline_failure_is_loud_and_names_degrade_posture(ce_dir):
    def _dl(url, dest):
        raise OSError("name resolution failed")

    with pytest.raises(RuntimeError) as exc:
        scm.fetch_service_crossencoder_onnx(downloader=_dl)
    msg = str(exc.value)
    # The remedy names the NON-fatal posture (rerank degrades loud, engine still
    # serves) — deliberately different from bge's "will not boot".
    assert "degraded" in msg
    assert str(ce_dir / "model.onnx") in msg
    assert scm.service_crossencoder_model_present() is False


def test_truncated_model_triggers_refetch(ce_dir, monkeypatch):
    monkeypatch.setattr(scm, "_MIN_MODEL_BYTES", 100)
    ce_dir.mkdir(parents=True)
    (ce_dir / "model.onnx").write_bytes(b"x" * 10)         # below floor
    (ce_dir / "tokenizer.json").write_bytes(b"y")
    assert scm.service_crossencoder_model_present() is False

    _pin_digests(monkeypatch, model=b"x" * 200, tokenizer=b"TOK")
    dl = _fake_downloader({"/model.onnx": b"x" * 200, "/tokenizer.json": b"TOK"})
    scm.fetch_service_crossencoder_onnx(downloader=dl)
    assert (ce_dir / "model.onnx").read_bytes() == b"x" * 200


def test_partial_failure_cleans_orphan_model(ce_dir, monkeypatch):
    _pin_digests(monkeypatch, model=b"MODEL")

    def _dl(url, dest):
        if url.endswith("/model.onnx"):
            dest.write_bytes(b"MODEL")
            return
        raise OSError("connection reset")

    with pytest.raises(RuntimeError):
        scm.fetch_service_crossencoder_onnx(downloader=_dl)
    assert not (ce_dir / "model.onnx").exists()
    assert scm.service_crossencoder_model_present() is False


def test_digest_mismatch_fails_loud_and_removes_file(ce_dir):
    # Real pinned digests in force: a wrong payload must fail and be removed.
    dl = _fake_downloader({"/model.onnx": b"NOT-THE-REAL-MODEL"})
    with pytest.raises(RuntimeError) as exc:
        scm.fetch_service_crossencoder_onnx(downloader=dl)
    assert "sha256 mismatch" in str(exc.value)
    assert not (ce_dir / "model.onnx").exists()


def test_env_override_directs_destination(tmp_path, monkeypatch):
    custom = tmp_path / "custom-ce"
    monkeypatch.setenv("NX_SERVICE_CROSSENCODER_DIR", str(custom))
    assert scm.service_crossencoder_model_dir() == custom


def test_urls_are_pinned_revision_hf_resolves(ce_dir, monkeypatch):
    """The fetch must hit the pinned-revision resolve URLs — an immutable
    content address, never a moving branch like /main/."""
    _pin_digests(monkeypatch, model=b"M", tokenizer=b"T")
    urls: list[str] = []

    def _dl(url, dest):
        urls.append(url)
        dest.write_bytes(b"M" if url.endswith("/model.onnx") else b"T")

    scm.fetch_service_crossencoder_onnx(downloader=_dl)
    base = (
        "https://huggingface.co/cross-encoder/ms-marco-MiniLM-L-6-v2/resolve/"
        "c5ee24cb16019beea0893ab7796b1df96625c6b8/"
    )
    assert urls == [base + "onnx/model.onnx", base + "tokenizer.json"]
    assert not any("/main/" in u for u in urls)


def test_python_path_matches_java_default(monkeypatch):
    """Cross-language drift guard: the Python destination must equal the Java
    CrossEncoderReranker.DEFAULT_MODEL_PATH / DEFAULT_TOKENIZER_PATH."""
    monkeypatch.delenv("NX_SERVICE_CROSSENCODER_DIR", raising=False)
    src = _JAVA_RERANKER.read_text()
    m = re.search(
        r'DEFAULT_MODEL_PATH\s*=\s*\n?\s*System\.getProperty\("user\.home"\)\s*\n?\s*\+\s*"([^"]+)"',
        src,
    )
    assert m is not None, "Java DEFAULT_MODEL_PATH literal not found"
    java_model = Path.home() / m.group(1).lstrip("/")
    assert scm.service_crossencoder_model_dir() / scm.MODEL_FILENAME == java_model

    mt = re.search(
        r'DEFAULT_TOKENIZER_PATH\s*=\s*\n?\s*System\.getProperty\("user\.home"\)\s*\n?\s*\+\s*"([^"]+)"',
        src,
    )
    assert mt is not None, "Java DEFAULT_TOKENIZER_PATH literal not found"
    java_tok = Path.home() / mt.group(1).lstrip("/")
    assert scm.service_crossencoder_model_dir() / scm.TOKENIZER_FILENAME == java_tok
