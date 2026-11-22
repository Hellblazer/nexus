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
import stat
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from nexus import _mineru_spawn, mineru_weights as mw
from nexus.db.service_bge_model import file_sha256
from tests._f35_weights_bed import (  # noqa: F401 — env is a fixture
    _CONTENT, _Assets, _Hub, _complete_root, _hf_usable, _lines, _manifest, _root, env,
)
from tests._module_seam import setattr_in

#: tests/conftest.py stubs the fetch and the child env for every other module; this one drives them.
REAL_MINERU_WEIGHTS_FETCH = True

REPO_ROOT = Path(__file__).resolve().parent.parent

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


def _other_snapshot(env, revision: str, content: dict[str, bytes]) -> Path:
    other = env.hub.storage / "snapshots" / revision
    for path, data in content.items():
        (other / path).parent.mkdir(parents=True, exist_ok=True)
        (other / path).write_bytes(data)
    return other


def test_a_snapshot_at_another_revision_is_a_source_when_its_bytes_verify(env):
    """An existing install's cache is at whatever revision MinerU last fetched (``main``): its files are as good
    as the pinned snapshot's when the sha256 matches, so nothing is downloaded for them."""
    _other_snapshot(env, "f" * 40, _CONTENT)
    assert mw.ensure_pipeline_weights(lambda _l: None, fetch=env.assets) is True
    assert env.assets.urls == []
    assert mw.nexus_weights_ready()


def test_a_snapshot_at_another_revision_with_other_bytes_is_not_a_source(env):
    wrong = "models/Layout/PP-DocLayoutV2/model.safetensors"
    _other_snapshot(env, "f" * 40, {**_CONTENT, wrong: b"Y" * len(_CONTENT[wrong])})
    assert mw.ensure_pipeline_weights(lambda _l: None, fetch=env.assets) is True
    assert env.assets.urls == [f"{mw.asset_base_url()}/{mw.asset_name(wrong)}"]


def test_a_blob_named_by_its_sha256_is_found_without_a_snapshot_entry(env):
    """``blobs/<sha256>`` is looked up first: a snapshot dir that was pruned or never listed the file still seeds."""
    import hashlib  # noqa: PLC0415

    blobs = env.hub.storage / "blobs"
    blobs.mkdir(parents=True)
    for data in _CONTENT.values():
        (blobs / hashlib.sha256(data).hexdigest()).write_bytes(data)
    assert mw.ensure_pipeline_weights(lambda _l: None, fetch=env.assets) is True
    assert env.assets.urls == []


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


# ── an existing install's usable Hugging Face cache never turns into a hold or a download (review C1) ──────────



def test_under_refuse_a_usable_hf_cache_with_files_left_over_does_nothing_and_does_not_raise(env, monkeypatch):
    """Files nexus cannot seed remain, and MinerU can read its own cache: the machine works as before F35."""
    missing = "models/TabRec/SlanetPlus/slanet-plus.onnx"
    env.hub.fill({p: d for p, d in _CONTENT.items() if p != missing})
    _hf_usable(monkeypatch)
    lines, say = _lines()

    assert mw.ensure_pipeline_weights(say, fetch=env.assets, allow_fetch=False) is False

    assert env.assets.urls == [] and lines == []
    assert not (_root() / mw.COMPLETE_MARKER).exists()
    assert mw.child_env() == {}, "the root is incomplete, so MinerU keeps reading its Hugging Face cache"


def test_under_refuse_with_no_usable_hf_cache_the_missing_files_still_raise(env):
    with pytest.raises(mw.WeightsFetchError, match="does not download them"):
        mw.ensure_pipeline_weights(lambda _l: None, fetch=env.assets, allow_fetch=False)


def test_interactive_download_failure_with_a_usable_hf_cache_warns_and_returns_false(env, monkeypatch):
    missing = "models/TabRec/SlanetPlus/slanet-plus.onnx"
    env.hub.fill({p: d for p, d in _CONTENT.items() if p != missing})
    _hf_usable(monkeypatch)
    env.assets.override[mw.asset_name(missing)] = b"nope"  # size mismatch on download
    lines, say = _lines()

    assert mw.ensure_pipeline_weights(say, fetch=env.assets) is False

    warning = [ln for ln in lines if "warning" in ln]
    assert len(warning) == 1 and mw.asset_name(missing) in warning[0] and "Hugging Face cache" in warning[0]
    assert "size mismatch" in warning[0]
    assert not (_root() / mw.COMPLETE_MARKER).exists() and mw.child_env() == {}


def test_interactive_download_failure_without_a_usable_hf_cache_still_raises(env, monkeypatch):
    _hf_usable(monkeypatch, False)
    env.assets.override[mw.asset_name("models/TabRec/SlanetPlus/slanet-plus.onnx")] = b"nope"
    with pytest.raises(mw.WeightsFetchError, match="size mismatch"):
        mw.ensure_pipeline_weights(lambda _l: None, fetch=env.assets)


def test_the_fetch_line_says_how_much_is_downloaded_when_seeding_covers_part(env):
    missing = "models/TabRec/SlanetPlus/slanet-plus.onnx"
    env.hub.fill({p: d for p, d in _CONTENT.items() if p != missing})
    lines, say = _lines()
    mw.ensure_pipeline_weights(say, fetch=env.assets)
    first = lines[0]
    assert "downloading 2.0 KB" in first and "3 file(s)" in first and "taken from the Hugging Face cache" in first


# ── prefetch_is_noop: the one predicate the index gate asks ────────────────────────────────────────────────


def test_prefetch_is_noop_is_true_exactly_when_ensure_would_do_nothing(env, monkeypatch):
    assert not mw.prefetch_is_noop(), "default model, no root: seeding or a download would run"
    env.hub.fill(_CONTENT)
    assert not mw.prefetch_is_noop(), "an HF cache alone is not enough: ensure would still seed nexus's copy"
    mw.ensure_pipeline_weights(lambda _l: None, fetch=env.assets)
    assert mw.prefetch_is_noop()
    for name, value in ((mw.SKIP_ENV, "1"), ("MINERU_MODEL_SOURCE", "local")):
        with monkeypatch.context() as m:
            m.setenv(name, value)
            assert mw.prefetch_is_noop()


def test_prefetch_is_noop_for_a_remote_server_and_before_any_root(env, monkeypatch):
    monkeypatch.setattr(mw, "_remote_server_configured", lambda: True)
    assert mw.prefetch_is_noop()


def test_prefetch_is_noop_with_the_chinese_formula_opt_in_follows_the_hf_cache(env, monkeypatch):
    monkeypatch.setenv("MINERU_FORMULA_CH_SUPPORT", "1")
    _hf_usable(monkeypatch, False)
    assert not mw.prefetch_is_noop()
    _hf_usable(monkeypatch, True)
    assert mw.prefetch_is_noop()
    _hf_usable(monkeypatch, False)
    monkeypatch.setenv("MINERU_MODEL_SOURCE", "modelscope")
    assert mw.prefetch_is_noop(), "modelscope with the opt-in gets no prefetch, and the gate must not wait on one"


# ── the index-repo gate on the DEFAULT formula model ───────────────────────────────────────────────────────


@pytest.fixture
def gate(env, monkeypatch, tmp_path):
    """``ensure_or_exit`` with the stack present, the real prefetch with the asset stub as its downloader."""
    from nexus import pdf_stack  # noqa: PLC0415

    monkeypatch.setattr(pdf_stack, "ensure", lambda entry: pdf_stack.Outcome.PRESENT)
    real = mw.ensure_pipeline_weights
    monkeypatch.setattr(
        mw, "ensure_pipeline_weights",
        lambda say, *, download=None, allow_fetch=True, fetch=None: real(say, allow_fetch=allow_fetch, fetch=env.assets),
    )
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "paper.pdf").write_bytes(b"%PDF-1.4\n")
    env.repo = repo
    return env


def _hold(gate, monkeypatch, *, probe, policy):
    from nexus import pdf_stack  # noqa: PLC0415
    from nexus.indexer import _hold_pdfs_without_stack  # noqa: PLC0415

    monkeypatch.setenv(pdf_stack.POLICY_ENV, policy)  # restored at teardown, never leaked to the next test
    return _hold_pdfs_without_stack([gate.repo / "paper.pdf"], gate.repo, probe=probe, extractor="mineru")


def test_the_index_gate_under_refuse_with_a_usable_hf_cache_holds_nothing_and_downloads_nothing(gate, monkeypatch):
    from nexus import pdf_stack  # noqa: PLC0415

    missing = "models/TabRec/SlanetPlus/slanet-plus.onnx"
    gate.hub.fill({p: d for p, d in _CONTENT.items() if p != missing})
    _hf_usable(monkeypatch)
    held = _hold(gate, monkeypatch, probe=lambda paths: paths, policy=pdf_stack.POLICY_REFUSE)
    assert held == []
    assert gate.assets.urls == []
    assert pdf_stack.skipped_count(pdf_stack.SKIP_CAUSE_WEIGHTS) == 0


def test_the_index_gate_spends_no_seeding_and_no_download_when_every_pdf_is_current(gate, monkeypatch):
    from nexus import pdf_stack  # noqa: PLC0415

    gate.hub.fill(_CONTENT)
    _hf_usable(monkeypatch)  # an existing install: MinerU's own cache is complete, nexus's copy does not exist yet
    held = _hold(gate, monkeypatch, probe=lambda paths: [], policy=pdf_stack.POLICY_INSTALL)
    assert held == []
    assert gate.assets.urls == []
    assert not _root().exists(), "an all-unchanged walk must not even seed nexus's copy"


def test_a_pdf_needing_work_seeds_from_a_matching_hf_cache_with_zero_downloads(gate, monkeypatch):
    from nexus import pdf_stack  # noqa: PLC0415

    gate.hub.fill(_CONTENT)
    held = _hold(gate, monkeypatch, probe=lambda paths: paths, policy=pdf_stack.POLICY_INSTALL)
    assert held == [] and gate.assets.urls == []
    assert mw.nexus_weights_ready()


def test_under_refuse_a_pdf_needing_work_seeds_from_a_matching_hf_cache_too(gate, monkeypatch):
    """The post-commit hook (``NX_PDF_STACK=refuse``) may seed from a local cache: no network, no hold."""
    from nexus import pdf_stack  # noqa: PLC0415

    gate.hub.fill(_CONTENT)
    held = _hold(gate, monkeypatch, probe=lambda paths: paths, policy=pdf_stack.POLICY_REFUSE)
    assert held == [] and gate.assets.urls == []
    assert mw.nexus_weights_ready()


# ── a second process waits for the first; failures stay named ────────────────────────────────────────────────


def test_two_runs_at_once_download_each_file_once(env):
    results: list[bool] = []

    def slow_fetch(url: str, dest: Path) -> None:
        import time  # noqa: PLC0415

        time.sleep(0.05)
        env.assets(url, dest)

    threads = [
        threading.Thread(target=lambda: results.append(mw.ensure_pipeline_weights(lambda _l: None, fetch=slow_fetch)))
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(results) == [False, True], results
    assert len(env.assets.urls) == len(_CONTENT), "the second run waited on the lock and found the work done"
    assert mw.nexus_weights_ready()


def test_a_marker_that_cannot_be_written_is_a_named_error_not_a_raw_oserror(env):
    (_root() / mw.COMPLETE_MARKER).mkdir(parents=True)  # a directory where the marker file goes
    with pytest.raises(mw.WeightsFetchError, match="could not be written"):
        mw.ensure_pipeline_weights(lambda _l: None, fetch=env.assets)


def test_a_copy_that_does_not_verify_after_the_cross_device_fallback_is_refused(env, monkeypatch):
    env.hub.fill(_CONTENT)

    def _no_link(*_a, **_k):
        raise OSError(18, "Invalid cross-device link")

    def _bad_copy(src, dst):
        Path(dst).write_bytes(b"bit rot")

    setattr_in(monkeypatch, mw, "os.link", _no_link)
    setattr_in(monkeypatch, mw, "shutil.copyfile", _bad_copy)
    with pytest.raises(mw.WeightsFetchError, match="does not match its sha256"):
        mw.ensure_pipeline_weights(lambda _l: None, fetch=env.assets)
    assert not (_root() / mw.COMPLETE_MARKER).exists()
    leftovers = [p for p in _root().rglob("*.part")]
    assert leftovers == [], "no temp file is left behind"


def test_temp_names_are_unique_per_call(env):
    target = _root() / "models" / "x.bin"
    assert mw._unique_sibling(target) != mw._unique_sibling(target)


def test_the_remedy_text_names_the_three_real_remedies(env):
    with pytest.raises(mw.WeightsFetchError) as info:
        mw.ensure_pipeline_weights(lambda _l: None, fetch=lambda url, dest: (_ for _ in ()).throw(OSError("offline")))
    message = str(info.value)
    assert "Run the command again to resume" in message
    assert mw.BASE_URL_ENV in message and "a mirror" in message
    assert "mineru-models-download" in message and "MINERU_MODEL_SOURCE=local" in message and "mineru.json" in message


def test_the_doctor_skip_line_gives_the_same_three_remedies(monkeypatch):
    from nexus.commands import doctor  # noqa: PLC0415

    monkeypatch.delenv("MINERU_FORMULA_CH_SUPPORT", raising=False)
    monkeypatch.delenv("MINERU_MODEL_SOURCE", raising=False)
    line = doctor._mineru_weights_skip_line()
    assert "skipped" in line and "mineru-models-download" in line
    assert "nexus release asset" in line and mw.BASE_URL_ENV in line and "MINERU_MODEL_SOURCE=local" in line


def test_child_env_sets_hf_offline_so_an_accidental_hub_call_fails_loudly(env):
    _complete_root(env)
    assert mw.child_env()["HF_HUB_OFFLINE"] == "1"


def test_the_nexus_mineru_json_is_private_and_an_unparsable_user_config_is_logged(env):
    from structlog.testing import capture_logs  # noqa: PLC0415

    _complete_root(env)
    (env.home / "mineru.json").write_text("{not json")
    with capture_logs() as logs:
        config = Path(mw.child_env()["MINERU_TOOLS_CONFIG_JSON"])
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert any(e.get("event") == "mineru_user_config_unreadable" for e in logs), logs
    assert json.loads(config.read_text()) == {"models-dir": {"pipeline": str(_root())}}
    assert not list(config.parent.glob("*.tmp")), "the unique temp name is renamed away"
