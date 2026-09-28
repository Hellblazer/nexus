# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wvyvn: the engine substrate provisions its local ONNX models before
boot, once per box, and fails with a message naming the fix.

On a fresh host with an empty onnx_models root the engine died constructing
``Bge768Embedder`` and the substrate said only "service did not bind port",
so ~23k tests errored at setup with nothing pointing at the model (hellmini,
2026-09-28).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

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
    separately, so they contend on the cross-process flock; the in-process
    env-swap lock is replaced with a no-op so it cannot serialise them in
    the flock's place (every thread swaps in the same snapshot)."""
    root, state = models

    class _NoLock:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(es, "_pg_bin_lock", _NoLock())
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
    assert '"NX_ONNX_MODEL_DIR"' in inspect.getsource(es._boot)
