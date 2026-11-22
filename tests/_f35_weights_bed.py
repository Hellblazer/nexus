# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared bed for the F35 (nexus-7t3w1) weight tests: a small real manifest, a Hugging Face cache layout, an asset
stub and the ``env`` fixture that isolates HOME, the HF cache and the network."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from nexus import mineru_weights as mw

#: A manifest of real bytes: three nested paths, one large-ish enough to be a separate file from the small ones.
_CONTENT = {
    "models/Layout/PP-DocLayoutV2/config.json": b'{"layout": true}\n',
    "models/Layout/PP-DocLayoutV2/model.safetensors": b"L" * 4096,
    "models/MFR/unimernet_hf_small_2503/README.md": b"# unimernet\n",
    "models/TabRec/SlanetPlus/slanet-plus.onnx": b"S" * 2048,
}


def _manifest(content: dict[str, bytes]) -> tuple[mw.WeightFile, ...]:
    import hashlib  # noqa: PLC0415

    return tuple(mw.WeightFile(p, len(b), hashlib.sha256(b).hexdigest()) for p, b in content.items())


class _Hub:
    """A Hugging Face cache for the pinned snapshot: ``snapshots/<rev>/<path>`` symlinks into ``blobs/``."""

    def __init__(self, cache: Path):
        self.storage = cache / ("models--" + mw.WEIGHTS_REPO.replace("/", "--"))
        self.snapshot = self.storage / "snapshots" / mw.WEIGHTS_REVISION

    def put(self, path: str, data: bytes) -> None:
        import hashlib  # noqa: PLC0415

        blob = self.storage / "blobs" / hashlib.sha256(data).hexdigest()
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(data)
        link = self.snapshot / path
        link.parent.mkdir(parents=True, exist_ok=True)
        link.unlink(missing_ok=True)
        link.symlink_to(os.path.relpath(blob, link.parent))

    def fill(self, content: dict[str, bytes]) -> None:
        for path, data in content.items():
            self.put(path, data)


class _Assets:
    """The release's assets: ``fetch(url, dest)`` the way ``_httpx_stream`` does (atomic write to dest)."""

    def __init__(self, content: dict[str, bytes]):
        self.by_name = {mw.asset_name(p): b for p, b in content.items()}
        self.urls: list[str] = []
        self.override: dict[str, bytes] = {}

    def __call__(self, url: str, dest: Path) -> None:
        self.urls.append(url)
        name = url.rsplit("/", 1)[1]
        data = self.override.get(name, self.by_name[name])
        tmp = dest.with_name(dest.name + ".dl")
        tmp.write_bytes(data)
        os.replace(tmp, dest)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A temp HOME, an empty HF cache, the small manifest, the default source and a local server."""
    import huggingface_hub  # noqa: PLC0415 — also proves it is importable where the stack is
    import huggingface_hub.constants as hf_constants  # noqa: PLC0415

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cache = tmp_path / "hf-cache"
    cache.mkdir()
    monkeypatch.setattr(hf_constants, "HF_HUB_CACHE", str(cache))
    for name in (
        mw.SKIP_ENV, mw.BASE_URL_ENV, "MINERU_MODEL_SOURCE", "MINERU_FORMULA_CH_SUPPORT", "MINERU_TOOLS_CONFIG_JSON",
        "HF_HUB_OFFLINE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(mw, "MANIFEST", _manifest(_CONTENT))
    monkeypatch.setattr(mw, "_remote_server_configured", lambda: False)

    def _never(*_a, **_k):
        raise AssertionError("Hugging Face was contacted")

    # F35: Hugging Face is read from its local cache only; every network entry point fails the test.
    monkeypatch.setattr(huggingface_hub, "snapshot_download", _never)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", _never)
    return type("Env", (), {"home": home, "hub": _Hub(cache), "assets": _Assets(_CONTENT)})()


def _lines() -> tuple[list[str], Callable[[str], None]]:
    out: list[str] = []
    return out, out.append


def _root() -> Path:
    return mw.weights_root()


def _complete_root(env) -> None:
    mw.ensure_pipeline_weights(lambda _l: None, fetch=env.assets)
    assert mw.nexus_weights_ready()


def _hf_usable(monkeypatch, value: bool = True) -> None:
    monkeypatch.setattr(mw, "_hf_weights_cached", lambda: value)
