# SPDX-License-Identifier: AGPL-3.0-or-later
"""F35 (nexus-7t3w1): MinerU's pipeline weights come from nexus's own release assets, not Hugging Face.

Design of record: Sam's rulings T2 ``nexus/sam-ruling-mineru-self-host-2026-11-21`` and ``-publish-2026-11-22``.
These tests swap the 20-file pinned manifest for a few small real files (so every digest and size check is the
production code running on real bytes), put a real Hugging Face cache layout (symlinked snapshot) under a temp
directory, and serve assets from a real local http server for the download path. No network, no 1.2 GB.
"""

from __future__ import annotations

import functools
import http.server
import importlib.util
import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from nexus import _mineru_spawn, mineru_weights as mw
from nexus.db.service_bge_model import file_sha256
from tests._module_seam import setattr_in

#: tests/conftest.py stubs the fetch and the child env for every other module; this one drives them.
REAL_MINERU_WEIGHTS_FETCH = True

REPO_ROOT = Path(__file__).resolve().parent.parent

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


# ── the pin ─────────────────────────────────────────────────────────────────────────────────────


def test_the_real_manifest_is_twenty_unique_pinned_files() -> None:
    real = mw.MANIFEST
    assert len(real) == 20
    assert len({f.asset for f in real}) == 20 and len({f.path for f in real}) == 20
    assert all(f.path.startswith("models/") and len(f.sha256) == 64 and f.size > 0 for f in real)
    assert mw.asset_name("models/Layout/PP-DocLayoutV2/model.safetensors") == "Layout__PP-DocLayoutV2__model.safetensors"
    assert mw.WEIGHTS_ASSET_TAG == "mineru-pipeline-" + mw.WEIGHTS_REVISION[:8]


def test_the_real_manifest_carries_every_file_the_pipeline_loads() -> None:
    """The F34 table (what mineru's loaders open) must be inside the manifest, so a mineru bump that moves a file
    fails here instead of on a user's first parse."""
    from tests._mineru_rearm import mineru_importorskip  # noqa: PLC0415

    mineru_importorskip("mineru.utils.enum_class")
    from mineru.utils.enum_class import ModelPath  # noqa: PLC0415

    have = {f.path for f in mw.MANIFEST}
    for name in ("pp_doclayout_v2", "unimernet_small", "pytorch_paddle", "slanet_plus", "unet_structure",
                 "paddle_table_cls", "paddle_orientation_classification"):
        base = getattr(ModelPath, name).strip("/")
        for f in mw.PIPELINE_FILES[name] or ("",):
            assert (f"{base}/{f}" if f else base) in have, (name, f)


def test_the_default_asset_url_is_the_release_tag_and_the_env_replaces_it(monkeypatch) -> None:
    monkeypatch.delenv(mw.BASE_URL_ENV, raising=False)
    assert mw.asset_base_url() == "https://github.com/Hellblazer/nexus/releases/download/mineru-pipeline-ed6b654c"
    monkeypatch.setenv(mw.BASE_URL_ENV, "http://127.0.0.1:9/assets/")
    assert mw.asset_base_url() == "http://127.0.0.1:9/assets"


# ── a matching Hugging Face cache seeds the root with no download ───────────────────────────────────


def test_a_matching_hf_cache_seeds_the_root_with_no_download(env):
    env.hub.fill(_CONTENT)
    lines, say = _lines()

    assert mw.ensure_pipeline_weights(say, fetch=env.assets) is True

    assert env.assets.urls == []
    root = _root()
    for path, data in _CONTENT.items():
        assert (root / path).read_bytes() == data
    # same filesystem: the blob is hard-linked, not copied
    assert os.stat(root / "models/Layout/PP-DocLayoutV2/config.json").st_nlink >= 2
    assert (root / mw.COMPLETE_MARKER).is_file()
    assert any("seeded 4 file(s)" in ln and "nothing downloaded" in ln for ln in lines)
    assert lines[-1] == "nx: MinerU pipeline weights are ready."
    assert not any("Fetching them now" in ln for ln in lines)
    assert not (root / ".partial").exists()


def test_a_cross_device_link_falls_back_to_a_copy(env, monkeypatch):
    env.hub.fill(_CONTENT)

    def _no_link(*_a, **_k):
        raise OSError(18, "Invalid cross-device link")

    setattr_in(monkeypatch, mw, "os.link", _no_link)  # only mineru_weights sees the failing link
    assert mw.ensure_pipeline_weights(lambda _l: None, fetch=env.assets) is True
    assert env.assets.urls == []
    assert (_root() / "models/Layout/PP-DocLayoutV2/model.safetensors").read_bytes() == _CONTENT[
        "models/Layout/PP-DocLayoutV2/model.safetensors"
    ]


def test_a_mismatched_or_missing_hf_file_is_fetched_from_the_asset_and_only_that_one(env):
    wrong = "models/Layout/PP-DocLayoutV2/model.safetensors"
    missing = "models/TabRec/SlanetPlus/slanet-plus.onnx"
    present = {p: d for p, d in _CONTENT.items() if p != missing}
    present[wrong] = b"X" * len(_CONTENT[wrong])  # right size, wrong bytes
    env.hub.fill(present)
    lines, say = _lines()

    assert mw.ensure_pipeline_weights(say, fetch=env.assets) is True

    base = mw.asset_base_url()
    assert sorted(env.assets.urls) == sorted([f"{base}/{mw.asset_name(wrong)}", f"{base}/{mw.asset_name(missing)}"])
    for path, data in _CONTENT.items():
        assert (_root() / path).read_bytes() == data, path
    assert any("fetched from nexus asset: Layout__PP-DocLayoutV2__model.safetensors" in ln for ln in lines)
    assert any("seeded 2 file(s)" in ln for ln in lines)
    assert (_root() / mw.COMPLETE_MARKER).is_file()


def test_an_hf_file_of_the_wrong_size_is_not_trusted(env):
    path = "models/MFR/unimernet_hf_small_2503/README.md"
    env.hub.fill({**_CONTENT, path: b"short"})
    assert mw.ensure_pipeline_weights(lambda _l: None, fetch=env.assets) is True
    assert env.assets.urls == [f"{mw.asset_base_url()}/{mw.asset_name(path)}"]


def test_a_snapshot_at_another_revision_is_not_a_source(env):
    other = env.hub.storage / "snapshots" / ("f" * 40)
    for path, data in _CONTENT.items():
        (other / path).parent.mkdir(parents=True, exist_ok=True)
        (other / path).write_bytes(data)
    assert mw.ensure_pipeline_weights(lambda _l: None, fetch=env.assets) is True
    assert len(env.assets.urls) == len(_CONTENT)


# ── no snapshot: everything comes from the asset, verified before it is placed ──────────────────────


def test_a_fresh_install_fetches_every_file_from_the_asset_with_visible_lines(env):
    lines, say = _lines()
    assert mw.ensure_pipeline_weights(say, fetch=env.assets) is True

    assert len(env.assets.urls) == len(_CONTENT)
    assert "before any page is parsed" in lines[0] and "Fetching them now" in lines[0]
    assert sum("fetched from nexus asset:" in ln for ln in lines) == len(_CONTENT)
    assert lines[-1] == "nx: MinerU pipeline weights are ready."
    assert mw.weights_cached() and mw.weights_present() and mw.nexus_weights_ready()
    assert not (_root() / ".partial").exists()


def test_a_size_mismatch_on_download_fails_and_places_nothing(env):
    bad = mw.asset_name("models/Layout/PP-DocLayoutV2/config.json")
    env.assets.override[bad] = b"{}"
    with pytest.raises(mw.WeightsFetchError) as info:
        mw.ensure_pipeline_weights(lambda _l: None, fetch=env.assets)
    message = str(info.value)
    assert bad in message and "size mismatch" in message and "mineru-models-download" in message
    assert not (_root() / "models/Layout/PP-DocLayoutV2/config.json").exists()
    assert not (_root() / mw.COMPLETE_MARKER).exists()
    assert not mw.nexus_weights_ready()


def test_a_sha_mismatch_on_download_fails_and_places_nothing(env):
    path = "models/Layout/PP-DocLayoutV2/model.safetensors"
    env.assets.override[mw.asset_name(path)] = b"Z" * len(_CONTENT[path])  # right size, wrong bytes
    with pytest.raises(mw.WeightsFetchError, match="sha256 mismatch"):
        mw.ensure_pipeline_weights(lambda _l: None, fetch=env.assets)
    assert not (_root() / path).exists()
    assert not (_root() / mw.COMPLETE_MARKER).exists()
    assert not list((_root() / ".partial").glob("*")) if (_root() / ".partial").exists() else True


def test_a_failed_run_resumes_keeping_the_files_that_verified(env):
    bad = mw.asset_name("models/TabRec/SlanetPlus/slanet-plus.onnx")
    env.assets.override[bad] = b"nope"
    with pytest.raises(mw.WeightsFetchError):
        mw.ensure_pipeline_weights(lambda _l: None, fetch=env.assets)
    env.assets.override.clear()
    env.assets.urls.clear()
    assert mw.ensure_pipeline_weights(lambda _l: None, fetch=env.assets) is True
    assert env.assets.urls == [f"{mw.asset_base_url()}/{bad}"]
    assert mw.nexus_weights_ready()


def test_an_asset_that_is_absent_from_the_release_is_named(env):
    import httpx  # noqa: PLC0415

    def _404(url: str, dest: Path) -> None:
        raise httpx.HTTPStatusError("not found", request=httpx.Request("GET", url), response=httpx.Response(404))

    with pytest.raises(mw.WeightsFetchError, match="does not exist"):
        mw.ensure_pipeline_weights(lambda _l: None, fetch=_404)


def test_the_default_downloader_fetches_from_a_real_http_server(env, tmp_path, monkeypatch):
    """No injected fetch: the bge asset downloader (httpx) against a real server named by the env var."""
    served = tmp_path / "served"
    served.mkdir()
    for name, data in env.assets.by_name.items():
        (served / name).write_bytes(data)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(served))
    handler.log_message = lambda *a, **k: None  # type: ignore[attr-defined]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)  # port 0: dynamic
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv(mw.BASE_URL_ENV, f"http://127.0.0.1:{server.server_address[1]}")
        assert mw.ensure_pipeline_weights(lambda _l: None) is True
    finally:
        server.shutdown()
        server.server_close()
    assert mw.nexus_weights_ready()
    for path, data in _CONTENT.items():
        assert (_root() / path).read_bytes() == data


# ── a complete root is no work; the user's own local models are left alone ──────────────────────────


def test_a_complete_root_does_no_work(env):
    assert mw.ensure_pipeline_weights(lambda _l: None, fetch=env.assets) is True
    env.assets.urls.clear()
    lines, say = _lines()
    assert mw.ensure_pipeline_weights(say, fetch=env.assets) is False
    assert env.assets.urls == [] and lines == []


def test_a_root_missing_a_file_or_a_marker_is_not_complete(env):
    mw.ensure_pipeline_weights(lambda _l: None, fetch=env.assets)
    (_root() / mw.COMPLETE_MARKER).unlink()
    assert not mw.nexus_weights_ready()
    (_root() / mw.COMPLETE_MARKER).write_text("x")
    assert mw.nexus_weights_ready()
    (_root() / "models/TabRec/SlanetPlus/slanet-plus.onnx").write_bytes(b"truncated")
    assert not mw.nexus_weights_ready()


def test_the_user_choosing_local_leaves_everything_untouched(env, monkeypatch):
    monkeypatch.setenv("MINERU_MODEL_SOURCE", "local")
    assert mw.ensure_pipeline_weights(lambda _l: None, fetch=env.assets) is False
    assert env.assets.urls == [] and not _root().exists()
    assert mw.child_env() == {}


@pytest.mark.parametrize("source", ["huggingface", "modelscope"])
def test_other_sources_are_bypassed_and_our_root_is_used(env, monkeypatch, source):
    monkeypatch.setenv("MINERU_MODEL_SOURCE", source)
    assert mw.ensure_pipeline_weights(lambda _l: None, fetch=env.assets) is True
    assert len(env.assets.urls) == len(_CONTENT)
    assert mw.nexus_weights_ready()
    assert mw.child_env()["MINERU_MODEL_SOURCE"] == "local"


def test_skip_env_and_a_remote_server_do_nothing(env, monkeypatch):
    monkeypatch.setenv(mw.SKIP_ENV, "1")
    assert mw.ensure_pipeline_weights(lambda _l: None, fetch=env.assets) is False
    monkeypatch.delenv(mw.SKIP_ENV)
    monkeypatch.setattr(mw, "_remote_server_configured", lambda: True)
    assert mw.ensure_pipeline_weights(lambda _l: None, fetch=env.assets) is False
    assert env.assets.urls == []


def test_a_background_run_never_downloads_but_still_seeds_from_the_cache(env):
    with pytest.raises(mw.WeightsFetchError, match="does not download them"):
        mw.ensure_pipeline_weights(lambda _l: None, fetch=env.assets, allow_fetch=False)
    assert env.assets.urls == []
    env.hub.fill(_CONTENT)
    assert mw.ensure_pipeline_weights(lambda _l: None, fetch=env.assets, allow_fetch=False) is True
    assert env.assets.urls == []


def test_the_chinese_formula_opt_in_keeps_the_hugging_face_fetch(env, monkeypatch):
    """The manifest does not carry PP-FormulaNet, so that variant takes F34's path (tests/test_f34_...)."""
    monkeypatch.setenv("MINERU_FORMULA_CH_SUPPORT", "1")
    called: list[str] = []
    monkeypatch.setattr(mw, "_ensure_from_huggingface", lambda say, **kw: called.append("hf") or False)
    assert mw.ensure_pipeline_weights(lambda _l: None, fetch=env.assets) is False
    assert called == ["hf"] and env.assets.urls == []
    assert mw.child_env() == {} and not mw.nexus_weights_ready()


# ── every MinerU child nexus starts carries the two variables ───────────────────────────────────────


def _complete_root(env) -> None:
    mw.ensure_pipeline_weights(lambda _l: None, fetch=env.assets)
    assert mw.nexus_weights_ready()


def test_child_env_is_empty_until_the_root_is_complete(env):
    assert mw.child_env() == {}
    assert not mw.mineru_json_path().exists()


def test_child_env_points_mineru_at_a_nexus_mineru_json_copied_from_the_users(env, monkeypatch):
    _complete_root(env)
    users = env.home / "mineru.json"
    users.write_text(json.dumps({"bucket_info": {"[default]": ["ak", "sk", "ep"]}, "config_version": "1.3.1",
                                 "models-dir": {"pipeline": "/their/models", "vlm": "/their/vlm"}}))
    before = users.read_text()

    child = mw.child_env()

    config = Path(child["MINERU_TOOLS_CONFIG_JSON"])
    assert child["MINERU_MODEL_SOURCE"] == "local"
    assert config.is_absolute() and config == mw.mineru_json_path() and config.parent == _root().parent
    written = json.loads(config.read_text())
    assert written["models-dir"] == {"pipeline": str(_root()), "vlm": "/their/vlm"}
    assert written["bucket_info"] == {"[default]": ["ak", "sk", "ep"]} and written["config_version"] == "1.3.1"
    assert users.read_text() == before, "the user's own file is never written"
    # models-dir.pipeline is a root that holds models/..., the way mineru's relative model paths expect
    assert (Path(written["models-dir"]["pipeline"]) / "models/Layout/PP-DocLayoutV2/config.json").is_file()


def test_child_env_reads_the_users_config_named_by_the_env_var_and_works_with_none(env, monkeypatch, tmp_path):
    _complete_root(env)
    assert json.loads(Path(mw.child_env()["MINERU_TOOLS_CONFIG_JSON"]).read_text()) == {
        "models-dir": {"pipeline": str(_root())}
    }
    other = tmp_path / "elsewhere.json"
    other.write_text(json.dumps({"latex-delimiter-config": {"display": {"left": "$$"}}}))
    monkeypatch.setenv("MINERU_TOOLS_CONFIG_JSON", str(other))
    written = json.loads(Path(mw.child_env()["MINERU_TOOLS_CONFIG_JSON"]).read_text())
    assert written["latex-delimiter-config"] == {"display": {"left": "$$"}}
    assert written["models-dir"]["pipeline"] == str(_root())
    assert mw.child_env()["MINERU_TOOLS_CONFIG_JSON"] != str(other)


def test_the_server_spawn_env_carries_both_variables(env, tmp_path, monkeypatch):
    monkeypatch.setenv("MINERU_MODEL_SOURCE", "modelscope")  # the user's own value is replaced
    _complete_root(env)
    server_env = _mineru_spawn._server_env(tmp_path / "out")
    assert server_env["MINERU_MODEL_SOURCE"] == "local"
    assert server_env["MINERU_TOOLS_CONFIG_JSON"] == str(mw.mineru_json_path())
    assert server_env["MINERU_API_OUTPUT_ROOT"] == str(tmp_path / "out")  # the existing keys are intact


def test_the_server_spawn_env_is_untouched_when_the_user_chose_local(env, tmp_path, monkeypatch):
    _complete_root(env)
    monkeypatch.setenv("MINERU_MODEL_SOURCE", "local")
    monkeypatch.setenv("MINERU_TOOLS_CONFIG_JSON", "/their/mineru.json")
    server_env = _mineru_spawn._server_env(tmp_path / "out")
    assert server_env["MINERU_MODEL_SOURCE"] == "local"
    assert server_env["MINERU_TOOLS_CONFIG_JSON"] == "/their/mineru.json"


def test_the_in_process_worker_child_gets_the_variables_at_interpreter_start(env, tmp_path, monkeypatch):
    """The per-batch worker is a fresh ``python -c`` child; mineru reads the two variables when it imports, so they
    have to be in its environment from the start. A stub ``mineru.cli.common`` records what the child saw."""
    from unittest.mock import patch  # noqa: PLC0415

    from nexus.pdf_extractor import PDFExtractor  # noqa: PLC0415

    _complete_root(env)
    seen = tmp_path / "seen.json"
    stub = tmp_path / "stub" / "mineru" / "cli"
    stub.mkdir(parents=True)
    (stub.parent / "__init__.py").write_text("")
    (stub / "__init__.py").write_text("")
    (stub / "common.py").write_text(
        "import json, os\n\ndef do_parse(*a, **k):\n"
        f"    open({str(seen)!r}, 'w').write(json.dumps({{k: os.environ.get(k) for k in "
        "('MINERU_MODEL_SOURCE', 'MINERU_TOOLS_CONFIG_JSON')}))\n"
        "    raise RuntimeError('stop')\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "stub"))
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    with patch("nexus.config.get_mineru_page_timeout_s", return_value=120), patch(
        "nexus.config.get_mineru_memory_ceiling_mb", return_value=0
    ), pytest.raises(RuntimeError):
        PDFExtractor()._mineru_run_subprocess(pdf, 0, 1)
    assert json.loads(seen.read_text()) == {
        "MINERU_MODEL_SOURCE": "local",
        "MINERU_TOOLS_CONFIG_JSON": str(mw.mineru_json_path()),
    }


def test_doctors_parse_probe_child_gets_the_variables(env, monkeypatch):
    from nexus.commands import doctor  # noqa: PLC0415

    _complete_root(env)
    captured: dict = {}

    def _fake_run_bounded(argv, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(doctor, "run_bounded", _fake_run_bounded)
    monkeypatch.setattr(doctor, "_synthesize_mineru_probe_pdf", lambda d: d / "probe.pdf")
    doctor._mineru_parse_fixture_once()
    assert captured["env"]["MINERU_MODEL_SOURCE"] == "local"
    assert captured["env"]["MINERU_TOOLS_CONFIG_JSON"] == str(mw.mineru_json_path())


# ── the publish script ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def publish():
    spec = importlib.util.spec_from_file_location("publish_mineru_weights", REPO_ROOT / "scripts" / "publish-mineru-weights.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_repo_license_is_the_agpl_the_script_publishes(publish):
    assert publish.STATIC_ASSETS["LICENSE-AGPL-3.0.txt"].read_text().lstrip().startswith("GNU AFFERO GENERAL PUBLIC LICENSE")
    apache = publish.STATIC_ASSETS["LICENSE-Apache-2.0.txt"].read_text()
    assert "Apache License" in apache and "Version 2.0, January 2004" in apache and len(apache) > 11000
    notices = publish.STATIC_ASSETS["THIRD-PARTY-NOTICES.md"].read_text()
    assert mw.WEIGHTS_REVISION in notices and "agpl-3.0" in notices
    for model in ("PP-DocLayoutV2", "UniMERNet", "PP-OCRv", "PP-LCNet_x1_0_doc_ori", "PP-LCNet_x1_0_table_cls", "SLANet_plus", "unet.onnx"):
        assert model in notices, model


def test_staging_from_the_hf_cache_reproduces_the_manifest_and_verifies(env, publish, tmp_path):
    env.hub.fill(_CONTENT)
    stage = tmp_path / "stage"
    assert publish.main(["--stage-dir", str(stage)]) == 0
    for entry in mw.MANIFEST:
        assert file_sha256(stage / entry.asset) == entry.sha256
    assert (stage / "MANIFEST.tsv").read_text() == "".join(f"{f.path}\t{f.size}\t{f.sha256}\n" for f in mw.MANIFEST)
    for name in ("LICENSE-AGPL-3.0.txt", "LICENSE-Apache-2.0.txt", "THIRD-PARTY-NOTICES.md"):
        assert (stage / name).is_file()
    assert publish.verify_stage(stage) == []
    assert publish.main(["--stage-dir", str(stage), "--verify-only"]) == 0
    assert publish.main(["--stage-dir", str(stage)]) == 0  # idempotent on a staged dir


def test_the_publish_script_refuses_a_mismatching_staged_file(env, publish, tmp_path, capsys):
    env.hub.fill(_CONTENT)
    stage = tmp_path / "stage"
    assert publish.main(["--stage-dir", str(stage)]) == 0
    victim = stage / mw.asset_name("models/Layout/PP-DocLayoutV2/model.safetensors")
    victim.write_bytes(b"Q" * len(_CONTENT["models/Layout/PP-DocLayoutV2/model.safetensors"]))  # right size, wrong bytes

    assert publish.main(["--stage-dir", str(stage)]) == 1
    assert publish.main(["--stage-dir", str(stage), "--verify-only"]) == 1
    err = capsys.readouterr().err
    assert "FAIL" in err and "sha256" in err and "Layout__PP-DocLayoutV2__model.safetensors" in err
    assert victim.read_bytes().startswith(b"Q"), "a mismatching staged file is reported, never silently repaired"


def test_the_publish_script_refuses_a_mismatching_upstream_file_and_stages_nothing_for_it(env, publish, tmp_path, capsys):
    bad = "models/MFR/unimernet_hf_small_2503/README.md"
    env.hub.fill({**_CONTENT, bad: b"X" * len(_CONTENT[bad])})
    stage = tmp_path / "stage"
    assert publish.main(["--stage-dir", str(stage)]) == 1
    assert not (stage / mw.asset_name(bad)).exists()
    assert not (stage / "MANIFEST.tsv").exists(), "nothing is published as a set when a file fails"
    assert "does not match the manifest" in capsys.readouterr().err


def test_a_missing_staged_asset_fails_verification(env, publish, tmp_path):
    env.hub.fill(_CONTENT)
    stage = tmp_path / "stage"
    publish.main(["--stage-dir", str(stage)])
    (stage / mw.asset_name("models/TabRec/SlanetPlus/slanet-plus.onnx")).unlink()
    assert any("missing" in p for p in publish.verify_stage(stage))


def test_the_upload_plan_is_create_all_when_absent_missing_only_when_partial_and_refuses_a_different_asset(publish):
    staged = {"a": "1", "b": "2", "c": "3"}

    def remote(name: str) -> str:
        return {"b": "2"}[name]

    assert publish.plan_upload(staged, None, remote) == (["a", "b", "c"], [])
    assert publish.plan_upload(staged, {"a": "1", "b": None}, remote) == (["c"], [])  # b's digest is fetched
    assert publish.plan_upload(staged, {"a": "1", "b": "2", "c": "3"}, remote) == ([], [])
    to_upload, conflicts = publish.plan_upload(staged, {"a": "9"}, remote)
    assert to_upload == ["b", "c"] and len(conflicts) == 1 and "a:" in conflicts[0]


def test_upload_refuses_a_conflict_and_uploads_nothing(env, publish, tmp_path, monkeypatch):
    env.hub.fill(_CONTENT)
    stage = tmp_path / "stage"
    publish.main(["--stage-dir", str(stage)])
    name = mw.asset_name("models/Layout/PP-DocLayoutV2/config.json")
    monkeypatch.setattr(publish, "release_assets", lambda repo, tag: {name: "0" * 64})
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(publish, "_gh", lambda *a, **k: calls.append(a))
    lines: list[str] = []
    assert publish.upload(stage, "owner/repo", dry_run=False, say=lines.append) == 1
    assert calls == [] and any("REFUSED" in ln and name in ln for ln in lines)


def test_upload_creates_an_absent_release_not_marked_latest_and_uploads_only_missing_assets_otherwise(
    env, publish, tmp_path, monkeypatch
):
    env.hub.fill(_CONTENT)
    stage = tmp_path / "stage"
    publish.main(["--stage-dir", str(stage)])
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(publish, "_gh", lambda *a, **k: calls.append(a))

    monkeypatch.setattr(publish, "release_assets", lambda repo, tag: None)
    assert publish.upload(stage, "owner/repo", dry_run=False, say=lambda _l: None) == 0
    assert calls[0][:3] == ("release", "create", mw.WEIGHTS_ASSET_TAG) and "--latest=false" in calls[0]
    assert len([a for a in calls[0] if a.startswith(str(stage))]) == len(_CONTENT) + 4

    calls.clear()
    have = publish.staged_digests(stage)
    held_back = mw.asset_name("models/TabRec/SlanetPlus/slanet-plus.onnx")
    monkeypatch.setattr(publish, "release_assets", lambda repo, tag: {n: d for n, d in have.items() if n != held_back})
    assert publish.upload(stage, "owner/repo", dry_run=False, say=lambda _l: None) == 0
    assert len(calls) == 1 and calls[0][:3] == ("release", "upload", mw.WEIGHTS_ASSET_TAG)
    assert [a for a in calls[0] if a.startswith(str(stage))] == [str(stage / held_back)]
    assert "--clobber" not in calls[0]

    calls.clear()
    monkeypatch.setattr(publish, "release_assets", lambda repo, tag: dict(have))
    assert publish.upload(stage, "owner/repo", dry_run=False, say=lambda _l: None) == 0
    assert calls == []


def test_dry_run_uploads_nothing(env, publish, tmp_path, monkeypatch):
    env.hub.fill(_CONTENT)
    stage = tmp_path / "stage"
    publish.main(["--stage-dir", str(stage)])
    monkeypatch.setattr(publish, "release_assets", lambda repo, tag: None)
    calls: list = []
    monkeypatch.setattr(publish, "_gh", lambda *a, **k: calls.append(a))
    assert publish.upload(stage, "owner/repo", dry_run=True, say=lambda _l: None) == 0
    assert calls == []
