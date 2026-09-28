# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wvyvn: the engine substrate provisions its local ONNX models before
boot, once per box, and fails with a message naming the fix.

On a fresh host with an empty onnx_models root the engine died constructing
``Bge768Embedder`` and the substrate said only "service did not bind port",
so ~23k tests errored at setup with nothing pointing at the model (hellmini,
2026-09-28).
"""
from __future__ import annotations

import contextlib
import threading
import time
from pathlib import Path

import pytest

from nexus.db.service_bge_model import service_bge_engine_dir_mismatch as _REAL_BGE_MISMATCH
from tests import _engine_substrate as es


@pytest.fixture
def models(monkeypatch, tmp_path):
    """Point the ambient onnx_models root at a tmp dir and fake the product's
    presence checks and fetchers; ``state`` records fetch calls."""
    root = tmp_path / "onnx_models"
    monkeypatch.setitem(es._PG_AMBIENT_ENV, "NX_ONNX_MODEL_DIR", str(root))
    monkeypatch.setitem(es._PG_AMBIENT_ENV, "NX_SERVICE_BGE_DIR", None)
    monkeypatch.setitem(es._PG_AMBIENT_ENV, "NX_SERVICE_CROSSENCODER_DIR", None)
    state = {"bge": 0, "ce": 0, "bge_ok": False, "ce_ok": False}

    def _fetch_bge(**_):
        state["bge"] += 1
        time.sleep(0.2)
        state["bge_ok"] = True

    def _fetch_ce(**_):
        state["ce"] += 1
        state["ce_ok"] = True

    monkeypatch.setattr("nexus.db.service_bge_model.service_bge_model_present", lambda: state["bge_ok"])
    monkeypatch.setattr("nexus.db.service_crossencoder_model.service_crossencoder_model_present", lambda: state["ce_ok"])
    monkeypatch.setattr("nexus.db.service_bge_model.fetch_service_bge_onnx", _fetch_bge)
    monkeypatch.setattr("nexus.db.service_bge_model.service_bge_engine_dir_mismatch", lambda: None)
    monkeypatch.setattr(
        "nexus.db.service_crossencoder_model.service_crossencoder_engine_dir_mismatch", lambda: None,
    )
    monkeypatch.setattr("nexus.db.service_crossencoder_model.fetch_service_crossencoder_onnx", _fetch_ce)
    return root, state


def test_missing_models_are_provisioned_and_the_root_is_returned(models) -> None:
    root, state = models
    assert es._ensure_onnx_models({}) == root
    assert (state["bge"], state["ce"]) == (1, 1)


def test_present_models_are_not_fetched(models) -> None:
    root, state = models
    state["bge_ok"] = state["ce_ok"] = True
    assert es._ensure_onnx_models({}) == root
    assert (state["bge"], state["ce"]) == (0, 0)


def test_eight_workers_on_a_cold_root_fetch_once(models, monkeypatch) -> None:
    """The xdist shape: the fetchers stream into the destination file, so two
    concurrent writers would corrupt it. Threads open the lock file
    separately, so they contend on the cross-process flock.

    _ambient_env is bypassed rather than unlocked: with its lock gone, eight
    threads racing its save/restore of os.environ left the test's tmp root in
    the worker's env, and the next engine boot in that worker read it. The
    env is pinned with monkeypatch instead, so nothing but the flock can
    serialise the threads."""
    root, state = models
    monkeypatch.setattr(es, "_ambient_env", contextlib.nullcontext)
    monkeypatch.setenv("NX_ONNX_MODEL_DIR", str(root))
    monkeypatch.delenv("NX_SERVICE_BGE_DIR", raising=False)
    monkeypatch.delenv("NX_SERVICE_CROSSENCODER_DIR", raising=False)
    results: list[Path | None] = []
    threads = [threading.Thread(target=lambda: results.append(es._ensure_onnx_models({}))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert state["bge"] == 1, f"{state['bge']} bge downloads for one cold root"
    assert results == [root] * 8


def test_a_failed_bge_fetch_names_the_model_and_the_fix(models, monkeypatch) -> None:
    root, _ = models

    def _offline(**_):
        raise RuntimeError("failed to provision the standard bge-768 ONNX (offline)")

    monkeypatch.setattr("nexus.db.service_bge_model.fetch_service_bge_onnx", _offline)
    with pytest.raises(RuntimeError) as exc:
        es._ensure_onnx_models({})
    msg = str(exc.value)
    assert "bge-768" in msg and str(root) in msg and "nx init --service" in msg


def test_a_failed_cross_encoder_fetch_only_warns(models, monkeypatch) -> None:
    root, _ = models

    def _offline(**_):
        raise RuntimeError("offline")

    monkeypatch.setattr("nexus.db.service_crossencoder_model.fetch_service_crossencoder_onnx", _offline)
    with pytest.warns(UserWarning, match="cross-encoder"):
        assert es._ensure_onnx_models({}) == root


def test_a_per_model_override_off_the_root_refuses_before_fetching(models, monkeypatch, tmp_path) -> None:
    """NX_SERVICE_BGE_DIR is Python-only: provisioning there would report
    present while the engine crashes reading <root>/<model>/onnx."""
    root, state = models
    monkeypatch.setitem(es._PG_AMBIENT_ENV, "NX_SERVICE_BGE_DIR", str(tmp_path / "off-root"))
    monkeypatch.setattr("nexus.db.service_bge_model.service_bge_engine_dir_mismatch", _REAL_BGE_MISMATCH)
    with pytest.raises(RuntimeError, match="NX_SERVICE_BGE_DIR"):
        es._ensure_onnx_models({})
    assert state["bge"] == 0


def test_a_voyage_engine_loads_no_local_model(models) -> None:
    _, state = models
    assert es._ensure_onnx_models({"NX_VOYAGE_API_KEY": "k"}) is None
    assert (state["bge"], state["ce"]) == (0, 0)


def test_the_root_follows_the_ambient_env_not_a_test_home(models, monkeypatch, tmp_path) -> None:
    """A test that monkeypatches HOME must not move the check away from the
    directory the engine is told to read."""
    root, _ = models
    monkeypatch.setenv("NX_ONNX_MODEL_DIR", str(tmp_path / "elsewhere"))
    assert es._ensure_onnx_models({}) == root


def test_boot_provisions_before_it_spawns_the_engine_and_hands_it_the_root() -> None:
    """The helper is only worth anything if ``_boot`` calls it: before the
    engine is spawned, with its result passed as NX_ONNX_MODEL_DIR."""
    import ast  # noqa: PLC0415 — test-local import
    import inspect  # noqa: PLC0415 — test-local import
    import textwrap  # noqa: PLC0415 — test-local import

    tree = ast.parse(textwrap.dedent(inspect.getsource(es._boot)))
    calls = {
        (n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")): n.lineno
        for n in ast.walk(tree) if isinstance(n, ast.Call)
    }
    assert "_ensure_onnx_models" in calls, "_boot never provisions the ONNX models"
    assert calls["_ensure_onnx_models"] < calls["Popen"], "models are provisioned after the engine is spawned"
    # ...and before PG boots or a port is probed (a probed port is released at once).
    assert calls["_ensure_onnx_models"] < calls["_free_port"]
    assert '"NX_ONNX_MODEL_DIR"' in inspect.getsource(es._boot)
    # The cheapest precondition runs before PG boots or models download.
    assert calls["which"] < calls["_pg_bin"] < calls["_ensure_onnx_models"]
