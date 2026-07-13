# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Standard bge ONNX provisioning for the Java service (RDR-160 P3.1, nexus-jknc2).

The CLI fetches the STANDARD (un-fused) export to the path the Java service reads.
These tests inject the downloader so nothing hits the network.

nexus-5votw: the source is the SELF-HOSTED GitHub release asset (the same
``ci-assets-bge-768-v1`` tag CI's prime-bge-onnx action consumes), sha256-pinned,
with retry/backoff in the default downloader. No HuggingFace dependency.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import httpx
import pytest

from nexus.daemon import binary_install
from nexus.db import service_bge_model as sbm

_JAVA_EMBEDDER = (
    Path(__file__).resolve().parents[2]
    / "service/src/main/java/dev/nexus/service/vectors/Bge768Embedder.java"
)
_CI_ACTION = (
    Path(__file__).resolve().parents[2] / ".github/actions/prime-bge-onnx/action.yml"
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


def _pin_digests(monkeypatch, *, model: bytes | None = None, tokenizer: bytes | None = None):
    """Pin the module's expected digests to the fake payloads a test lands, so
    the sha256 gate passes for that content and stays non-vacuous everywhere else."""
    if model is not None:
        monkeypatch.setattr(sbm, "_MODEL_SHA256", hashlib.sha256(model).hexdigest())
    if tokenizer is not None:
        monkeypatch.setattr(
            sbm, "_TOKENIZER_SHA256", hashlib.sha256(tokenizer).hexdigest()
        )


def test_fetch_lands_standard_model_and_tokenizer(bge_dir, monkeypatch):
    _pin_digests(monkeypatch, model=b"MODEL", tokenizer=b"TOK")
    dl = _fake_downloader({"/model.onnx": b"MODEL", "/tokenizer.json": b"TOK"})
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


def test_force_redownloads(bge_dir, monkeypatch):
    bge_dir.mkdir(parents=True)
    (bge_dir / "model.onnx").write_bytes(b"OLD")
    (bge_dir / "tokenizer.json").write_bytes(b"OLD")
    _pin_digests(monkeypatch, model=b"NEW", tokenizer=b"NEWTOK")
    dl = _fake_downloader({"/model.onnx": b"NEW", "/tokenizer.json": b"NEWTOK"})
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

    _pin_digests(monkeypatch, model=b"x" * 200, tokenizer=b"TOK")
    dl = _fake_downloader({"/model.onnx": b"x" * 200, "/tokenizer.json": b"TOK"})
    sbm.fetch_service_bge_onnx(downloader=dl)
    assert (bge_dir / "model.onnx").read_bytes() == b"x" * 200  # re-fetched


def test_partial_failure_cleans_orphan_model(bge_dir, monkeypatch):
    # Model download succeeds, tokenizer download fails: the lone model.onnx must
    # be removed so the dir does not read as a (partial) install.
    _pin_digests(monkeypatch, model=b"MODEL")

    def _dl(url, dest):
        if url.endswith("/model.onnx"):
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

    # tokenizer path too — declared as a separate Java constant, so guard it
    # independently against divergence.
    mt = re.search(
        r'DEFAULT_TOKENIZER_PATH\s*=\s*\n?\s*System\.getProperty\("user\.home"\)\s*\+\s*"([^"]+)"',
        src,
    )
    assert mt is not None, "Java DEFAULT_TOKENIZER_PATH literal not found"
    java_tok = Path.home() / mt.group(1).lstrip("/")
    py_tok = sbm.service_bge_model_dir() / sbm.TOKENIZER_FILENAME
    assert py_tok == java_tok


# ── nexus-5votw: self-hosted asset source + sha256 + retry ────────────────────


def test_urls_are_self_hosted_github_assets(bge_dir, monkeypatch):
    """The 5votw regression test: the install-time fetch must hit the self-hosted
    GitHub release asset, never HuggingFace."""
    _pin_digests(monkeypatch, model=b"M", tokenizer=b"T")
    urls: list[str] = []

    def _dl(url, dest):
        urls.append(url)
        dest.write_bytes(b"M" if url.endswith("/model.onnx") else b"T")

    sbm.fetch_service_bge_onnx(downloader=_dl)
    base = "https://github.com/Hellblazer/nexus/releases/download/ci-assets-bge-768-v1/"
    assert urls == [base + "model.onnx", base + "tokenizer.json"]
    assert not any("huggingface" in u for u in urls)


def test_digest_mismatch_fails_loud_and_leaves_nothing(bge_dir):
    """Wrong bytes (tampered/corrupt asset) against the REAL pinned digests must
    fail loud with a sha256 message and leave no file claiming success."""
    dl = _fake_downloader({"/model.onnx": b"EVIL", "/tokenizer.json": b"TOK"})
    with pytest.raises(RuntimeError) as exc:
        sbm.fetch_service_bge_onnx(downloader=dl)
    assert "sha256" in str(exc.value).lower()
    assert not (bge_dir / "model.onnx").exists()
    assert sbm.service_bge_model_present() is False


def test_tokenizer_digest_mismatch_cleans_both_files(bge_dir, monkeypatch):
    """Model verifies OK, tokenizer digest fails: the tokenizer is removed by the
    verify step AND the (valid) lone model is removed by the orphan cleanup — a
    tampered tokenizer.json is as dangerous as a tampered model.onnx."""
    _pin_digests(monkeypatch, model=b"MODEL")  # tokenizer digest stays the REAL pin
    dl = _fake_downloader({"/model.onnx": b"MODEL", "/tokenizer.json": b"EVIL"})
    with pytest.raises(RuntimeError) as exc:
        sbm.fetch_service_bge_onnx(downloader=dl)
    msg = str(exc.value).lower()
    assert "sha256" in msg
    assert "tokenizer.json" in msg
    assert not (bge_dir / "tokenizer.json").exists()
    assert not (bge_dir / "model.onnx").exists()
    assert sbm.service_bge_model_present() is False


def test_digests_and_tag_match_ci_action():
    """Drift guard: the module's pinned digests + asset tag must equal what the
    CI composite action (.github/actions/prime-bge-onnx) verifies/consumes —
    both sides describe the same immutable release assets."""
    src = _CI_ACTION.read_text(encoding="utf-8")
    m_model = re.search(r"MODEL_SHA256:\s*([0-9a-f]{64})", src)
    m_tok = re.search(r"TOKENIZER_SHA256:\s*([0-9a-f]{64})", src)
    m_tag = re.search(r"default:\s*(ci-assets-bge-768-v\d+)", src)
    assert m_model and m_tok and m_tag, "CI action digests/tag not found"
    assert sbm._MODEL_SHA256 == m_model.group(1)
    assert sbm._TOKENIZER_SHA256 == m_tok.group(1)
    assert sbm.BGE_ASSET_TAG == m_tag.group(1)


def test_asset_base_matches_binary_install_repo():
    """The bge asset and the engine binary must download from the same repo's
    releases — one repo constant class, guarded against silent divergence."""
    assert sbm._ASSET_DOWNLOAD_BASE == binary_install._RELEASE_DOWNLOAD_BASE


def test_default_downloader_retries_transient_then_succeeds(tmp_path, monkeypatch):
    attempts: list[str] = []
    sleeps: list[float] = []

    def _flaky(url, dest):
        attempts.append(url)
        if len(attempts) < 3:
            raise httpx.ConnectError("transient")
        dest.write_bytes(b"OK")

    monkeypatch.setattr(sbm, "_stream_once", _flaky)
    monkeypatch.setattr(sbm, "_retry_sleep", sleeps.append)
    dest = tmp_path / "f"
    sbm._httpx_stream("https://example.invalid/f", dest)
    assert dest.read_bytes() == b"OK"
    assert len(attempts) == 3
    # Exact schedule, not just count: pins the backoff values AND the indexing.
    assert sleeps == [2.0, 4.0]


def test_default_downloader_retries_429_and_5xx(tmp_path, monkeypatch):
    codes = iter([429, 503])
    attempts: list[int] = []

    def _flaky(url, dest):
        code = next(codes, None)
        attempts.append(code or 200)
        if code is not None:
            req = httpx.Request("GET", url)
            raise httpx.HTTPStatusError(
                f"{code}", request=req, response=httpx.Response(code, request=req)
            )
        dest.write_bytes(b"OK")

    monkeypatch.setattr(sbm, "_stream_once", _flaky)
    monkeypatch.setattr(sbm, "_retry_sleep", lambda _s: None)
    dest = tmp_path / "f"
    sbm._httpx_stream("https://example.invalid/f", dest)
    assert dest.read_bytes() == b"OK"
    assert attempts == [429, 503, 200]


def test_default_downloader_does_not_retry_404(tmp_path, monkeypatch):
    attempts: list[str] = []

    def _gone(url, dest):
        attempts.append(url)
        req = httpx.Request("GET", url)
        raise httpx.HTTPStatusError(
            "404", request=req, response=httpx.Response(404, request=req)
        )

    monkeypatch.setattr(sbm, "_stream_once", _gone)
    monkeypatch.setattr(sbm, "_retry_sleep", lambda _s: None)
    with pytest.raises(httpx.HTTPStatusError):
        sbm._httpx_stream("https://example.invalid/f", tmp_path / "f")
    assert len(attempts) == 1  # a hard 404 (bad tag/asset) fails immediately


def test_default_downloader_exhausts_retries_and_raises(tmp_path, monkeypatch):
    attempts: list[str] = []

    def _dead(url, dest):
        attempts.append(url)
        raise httpx.ConnectError("still down")

    monkeypatch.setattr(sbm, "_stream_once", _dead)
    monkeypatch.setattr(sbm, "_retry_sleep", lambda _s: None)
    with pytest.raises(httpx.ConnectError):
        sbm._httpx_stream("https://example.invalid/f", tmp_path / "f")
    assert len(attempts) == sbm._RETRY_ATTEMPTS
