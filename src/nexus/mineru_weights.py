# SPDX-License-Identifier: AGPL-3.0-or-later
"""MinerU's pipeline weights: whether they are on this box, and fetching them before any page is timed.

F34 (Sam 2026-11-21, T2 ``nexus/sam-ruling-f34-mineru-weights-8.5.5-2026-11-21``): on a fresh install the
first MinerU parse downloaded these weights (``opendatalab/PDF-Extract-Kit-1.0``, about 1.2 GB) from inside
the per-page budget, so the server was killed mid-download at ``mineru_server_timeout`` and the first pages
came out degraded. :func:`ensure_pipeline_weights` fetches them first, as a step the user sees, from the CLI
gate that every PDF-indexing command already passes (:func:`nexus.pdf_stack.ensure_or_exit`).

F35 (Sam 2026-11-21 / 2026-11-22, T2 ``nexus/sam-ruling-mineru-self-host-2026-11-21`` and
``-publish-2026-11-22``): the fetch no longer touches Hugging Face. The 20 files the English pipeline loads
are pinned in :data:`MANIFEST` (upstream revision :data:`WEIGHTS_REVISION`) and published as individual
assets of the immutable release tag :data:`WEIGHTS_ASSET_TAG`, the bge pattern
(:mod:`nexus.db.service_bge_model`). They land under :func:`weights_root` and every MinerU process nexus
starts is pointed at them (:func:`child_env`: ``MINERU_MODEL_SOURCE=local`` plus a nexus-owned
``mineru.json``). A machine that already holds the pinned snapshot in its Hugging Face cache is seeded from
it with no download. The one formula-model variant the manifest does not carry
(``MINERU_FORMULA_CH_SUPPORT``) keeps F34's Hugging Face fetch.

``nx doctor`` gates its real-parse check on the same probe (:func:`weights_present`)."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import structlog

_log = structlog.get_logger(__name__)

#: What the English (``p_lang_list=["en"]``) pipeline loads from each ``ModelPath`` entry, keyed by the
#: attribute name in ``mineru.utils.enum_class.ModelPath``. A tuple is the files that must exist, non-empty,
#: in that directory; ``()`` means the path is itself a file (an ``.onnx``). Directories are downloaded file
#: by file, so an interrupted first-use fetch leaves a directory present and incomplete: each file the load
#: opens is required on its own. The names come from the loaders, in mineru 3.1.11:
#:
#: - ``pp_doclayout_v2``: ``from_pretrained`` reads ``config.json`` and the weights
#:   (``model/layout/pp_doclayoutv2.py`` lines 908-909); ``preprocessor_config.json`` is optional (line 167).
#: - ``unimernet_small``: ``from_pretrained`` reads the config and weights, and ``AutoTokenizer`` the
#:   tokenizer files (``model/mfr/unimernet/unimernet_hf/modeling_unimernet.py`` lines 80, 99).
#: - ``pp_formulanet_plus_m``: opens the ``.pth`` and ``_inference.yml``
#:   (``model/mfr/pp_formulanet_plus_m/predict_formula.py`` lines 29-45).
#: - ``pytorch_paddle``: the detector and recognizer ``models_config.yml`` names for ``lang.en``
#:   (``model/ocr/pytorch_paddle.py`` lines 184-193), plus the ``ch_lite`` recognizer: with tables on, the
#:   orientation classifier builds its own OCR with ``lang="ch_lite"`` whatever the parse language
#:   (``backend/pipeline/model_init.py`` ``img_orientation_cls_model_init``). Other languages are not needed.
#: - the four ``.onnx`` paths are opened as they are (``model/table/...``, ``model/ori_cls/paddle_ori_cls.py``).
#:
#: ``tests/test_mineru_child_dll_dirs.py`` compares this table with ``download_pipeline_models`` (every path
#: it fetches is a key here) and with mineru's own sources, so a mineru bump that moves either side fails
#: there.
PIPELINE_FILES: dict[str, tuple[str, ...]] = {
    "pp_doclayout_v2": ("config.json", "model.safetensors"),
    "unimernet_small": ("config.json", "model.safetensors", "tokenizer.json", "tokenizer_config.json"),
    "pp_formulanet_plus_m": ("PP-FormulaNet_plus-M.pth", "PP-FormulaNet_plus-M_inference.yml"),
    "pytorch_paddle": ("ch_PP-OCRv5_det_infer.pth", "en_PP-OCRv5_rec_infer.pth", "ch_PP-OCRv5_rec_infer.pth"),
    "slanet_plus": (),
    "unet_structure": (),
    "paddle_table_cls": (),
    "paddle_orientation_classification": (),
}

#: ``MINERU_FORMULA_CH_SUPPORT`` values mineru reads as on / off (``backend/pipeline/model_init.py``,
#: ``MFR_MODEL``, case-insensitive); anything else it warns about and treats as off.
FORMULA_CH_ON = ("true", "1", "yes")
FORMULA_CH_OFF = ("false", "0", "no")

#: The ``-s`` choices of ``mineru-models-download`` (``REMOTE_MODEL_SOURCES`` in ``mineru/cli/models_download.py``).
DOWNLOAD_SOURCES = ("huggingface", "modelscope")


def formula_model() -> str:
    """The ``ModelPath`` attribute of the formula (MFR) model mineru loads, per ``MINERU_FORMULA_CH_SUPPORT``."""
    value = os.environ.get("MINERU_FORMULA_CH_SUPPORT", "False").lower()
    return "pp_formulanet_plus_m" if value in FORMULA_CH_ON else "unimernet_small"


# ── F35: the self-hosted copy (nexus-7t3w1) ─────────────────────────────────────────────────────────


class WeightFile(NamedTuple):
    """One pinned file: its path under the weights root (``models/...``), byte size and sha256."""

    path: str
    size: int
    sha256: str

    @property
    def asset(self) -> str:
        """The release asset name: the path under ``models/`` with ``/`` written ``__``."""
        return asset_name(self.path)


def asset_name(path: str) -> str:
    return path.removeprefix("models/").replace("/", "__")


#: The upstream bundle and the revision every file below was measured at (the Hugging Face snapshot dir name).
WEIGHTS_REPO = "opendatalab/PDF-Extract-Kit-1.0"
WEIGHTS_REVISION = "ed6b654c018d742e65a17671e379c5e6ecc87ec9"
#: The immutable release tag on Hellblazer/nexus that carries one asset per :data:`MANIFEST` file, plus
#: ``LICENSE-*``, ``THIRD-PARTY-NOTICES.md`` and ``MANIFEST.tsv`` (``scripts/publish-mineru-weights.py``).
WEIGHTS_ASSET_TAG = "mineru-pipeline-ed6b654c"
#: Replaces the whole tag URL (files are then ``<value>/<asset name>``): the candidate gate serves a staged
#: directory over http, and the unit tests inject through it.
BASE_URL_ENV = "NX_MINERU_WEIGHTS_BASE_URL"
#: Written into the root only after every file is verified.
COMPLETE_MARKER = ".complete"

#: The 20 files the pipeline loads with the ``unimernet_small`` formula model, measured at
#: :data:`WEIGHTS_REVISION`. Single source: the client verifies against it, the publish script stages and checks
#: against it, and ``MANIFEST.tsv`` on the release is generated from it.
MANIFEST: tuple[WeightFile, ...] = (
    WeightFile("models/Layout/PP-DocLayoutV2/config.json", 3787, "18a696b54c64c4fa582afcd3a41407c4b65a99dc7ab187ad2fed8af8e4128ad8"),
    WeightFile("models/Layout/PP-DocLayoutV2/model.safetensors", 214798436, "e60f3725aeedc88fd319416ef166bda79171a41516a301c27cab9132dc2739d2"),
    WeightFile("models/Layout/PP-DocLayoutV2/preprocessor_config.json", 575, "56281a70c931a291dcaf653605fb4df713fd823f65e939aecd6005c26346a103"),
    WeightFile("models/MFR/unimernet_hf_small_2503/config.json", 5094, "64c02e9897410658f7668c6a334a8b306276e4697656cbe06f86d8c4f01fc040"),
    WeightFile("models/MFR/unimernet_hf_small_2503/generation_config.json", 191, "d56ca9d5c5efa4283a2565ae42771bafd02910b56ef9c53e9b441c9c4c896d09"),
    WeightFile("models/MFR/unimernet_hf_small_2503/model.safetensors", 810036696, "9244e2565585c0f89bc3a6eeeea080ef3c588375fc0d536074fe88e80b917cda"),
    WeightFile("models/MFR/unimernet_hf_small_2503/README.md", 1657, "96574f3857e919353024edc423b9165dbf46e902cfe97b5b7ec552283bd744f6"),
    WeightFile("models/MFR/unimernet_hf_small_2503/special_tokens_map.json", 552, "358c249e2fb29060c6b73157d428853b0c48710deffc8ee670ab1013880946c9"),
    WeightFile("models/MFR/unimernet_hf_small_2503/tokenizer_config.json", 4522, "28b99e33895e06389c26c139b1333b82b7f5d8ed5f4fd14998acfd7c20989338"),
    WeightFile("models/MFR/unimernet_hf_small_2503/tokenizer.json", 3581950, "f8e29e3c3a8017f067b62a3d2d9211bb4cebc08a25afe58d3a6069981e3684d6"),
    WeightFile("models/OCR/paddleocr_torch/ch_PP-OCRv4_rec_server_doc_infer.pth", 101167457, "f65e699f4ca792fbce0e92d1df4c9bbdefe3e21bbdb01c3075cc49470b9bc1cc"),
    WeightFile("models/OCR/paddleocr_torch/ch_PP-OCRv5_det_infer.pth", 14506268, "df848ed5060bac4d0f6e58572aea97d92e909a8a87cf292849237b0e84f6ffdb"),
    WeightFile("models/OCR/paddleocr_torch/ch_PP-OCRv5_rec_infer.pth", 32611609, "d20ee8dac2ca63e2d1989b02ecc42595c71d61bf8dd8c8ddc5ad2ee68e7b5be2"),
    WeightFile("models/OCR/paddleocr_torch/ch_PP-OCRv6_small_det_infer.safetensors", 9938124, "89a96a8adc4e9cd0c994098edc76022e496d35844392562b4694c8fbc583f2da"),
    WeightFile("models/OCR/paddleocr_torch/ch_PP-OCRv6_small_rec_infer.safetensors", 21204736, "f65a332afe5aa663f0b9d5706f4ae8457b5b4058a842d5c1eb22df505c27d642"),
    WeightFile("models/OCR/paddleocr_torch/en_PP-OCRv5_rec_infer.pth", 23929399, "259a277836bf0e094910949ef4635927ad3f82c20b37f4010749af968967e282"),
    WeightFile("models/OriCls/paddle_orientation_classification/PP-LCNet_x1_0_doc_ori.onnx", 6787127, "70a8dc44e2de34696cd0c8873beb7c1470187f549d29c60b80d2783b6154975a"),
    WeightFile("models/TabCls/paddle_table_cls/PP-LCNet_x1_0_table_cls.onnx", 6776877, "c84bf1d79c1c74d534b5b12adb14dd12151c42f7ae3e4be4f1042b830f80b949"),
    WeightFile("models/TabRec/SlanetPlus/slanet-plus.onnx", 7758305, "d57a942af6a2f57d6a4a0372573c696a2379bf5857c45e2ac69993f3b334514b"),
    WeightFile("models/TabRec/UnetStructure/unet.onnx", 8335007, "0ea48d3a17e35ef5c2e498a5e799566073234d39b1079ca21d9f4fafe73c6d20"),
)

#: ``(url, dest)`` -> stream ``url`` into ``dest``; the default is the bge asset downloader
#: (:func:`nexus.db.service_bge_model._httpx_stream`: retry with backoff, then the REST asset endpoint).
Fetch = Callable[[str, Path], None]


def weights_root() -> Path:
    """``<cache>/nexus/mineru/pipeline-<rev8>``: ``models/...`` sits directly under it, which is the
    ``models-dir.pipeline`` MinerU's ``local`` source wants (its model paths begin ``models/``)."""
    from nexus.db.onnx_model_root import nexus_cache_root  # noqa: PLC0415 — deferred local import — avoids import-time cost

    return nexus_cache_root() / "mineru" / f"pipeline-{WEIGHTS_REVISION[:8]}"


def mineru_json_path() -> Path:
    """The nexus-owned ``mineru.json`` the child env points at (the user's own file is never written)."""
    return weights_root().parent / "mineru.json"


def asset_base_url() -> str:
    """Where the asset files are: :data:`BASE_URL_ENV`, else the release tag's download URL."""
    override = os.environ.get(BASE_URL_ENV, "").strip().rstrip("/")
    if override:
        return override
    from nexus.db.service_bge_model import _ASSET_DOWNLOAD_BASE  # noqa: PLC0415 — deferred local import — one release base

    return f"{_ASSET_DOWNLOAD_BASE}/{WEIGHTS_ASSET_TAG}"


def _size_ok(path: Path, size: int) -> bool:
    try:
        return path.is_file() and path.stat().st_size == size
    except OSError:
        return False


def _root_complete(root: Path | None = None) -> bool:
    """The marker is there and every manifest file is present at its pinned size (no re-hash: the digest
    runs when a file is placed, the bge accepted-risk pattern)."""
    root = root or weights_root()
    return (root / COMPLETE_MARKER).is_file() and all(_size_ok(root / f.path, f.size) for f in MANIFEST)


def nexus_weights_ready() -> bool:
    """Is nexus's verified copy in use? Complete, the user has not chosen ``MINERU_MODEL_SOURCE=local`` (their
    own models), and the formula model is the one the manifest carries."""
    return (
        os.environ.get("MINERU_MODEL_SOURCE") != "local"
        and formula_model() == "unimernet_small"
        and _root_complete()
    )


def _user_mineru_json() -> Path:
    """The user's MinerU config file, resolved the way mineru does (``MINERU_TOOLS_CONFIG_JSON``, a relative
    name under the home directory, default ``~/mineru.json``)."""
    name = os.environ.get("MINERU_TOOLS_CONFIG_JSON", "").strip() or "mineru.json"
    path = Path(name)
    return path if path.is_absolute() else Path(os.path.expanduser("~")) / name


def write_mineru_json() -> Path:
    """Write the nexus ``mineru.json``: the user's file (when readable) with ``models-dir.pipeline`` set to
    :func:`weights_root`. Rewritten only when the content changes. Never touches the user's file."""
    config: dict = {}
    try:
        loaded = json.loads(_user_mineru_json().read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            config = loaded
    except (OSError, ValueError):
        pass
    models_dir = config.get("models-dir")
    config["models-dir"] = {**(models_dir if isinstance(models_dir, dict) else {}), "pipeline": str(weights_root())}
    text = json.dumps(config, indent=4) + "\n"
    target = mineru_json_path()
    try:
        if target.read_text(encoding="utf-8") == text:
            return target
    except OSError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    return target


def child_env() -> dict[str, str]:
    """The two variables every MinerU process nexus starts gets when nexus's verified copy is in use, else ``{}``.

    ``MINERU_MODEL_SOURCE=local`` makes MinerU read ``models-dir.pipeline`` from the JSON at
    ``MINERU_TOOLS_CONFIG_JSON`` (an absolute path; MinerU reads it when ``mineru.utils.config_reader`` is
    imported, so it has to be in the child's environment from the start). Hugging Face and ModelScope are not
    contacted. A user who set ``MINERU_MODEL_SOURCE=local`` themselves is left alone."""
    if not nexus_weights_ready():
        return {}
    try:
        config = write_mineru_json()
    except OSError as exc:
        _log.warning("mineru_json_write_failed", error=str(exc), error_type=type(exc).__name__)
        return {}
    return {"MINERU_MODEL_SOURCE": "local", "MINERU_TOOLS_CONFIG_JSON": str(config)}


def weights_cached() -> bool:
    """Are the weights the English pipeline loads on this machine, found without any network access?

    True for nexus's own verified copy (:func:`nexus_weights_ready`) and for MinerU's own first-use copy in the
    Hugging Face cache (:func:`_hf_weights_cached`)."""
    return nexus_weights_ready() or _hf_weights_cached()


def _hf_weights_cached() -> bool:
    """Are the weights the English pipeline loads in the Hugging Face cache?

    MinerU's own first-use download (``nx index pdf``) writes no ``mineru.json``: it calls
    ``huggingface_hub.snapshot_download(ModelPath.pipeline_root_hf, allow_patterns=...)`` and uses the
    returned snapshot folder (``mineru/utils/models_download_utils.py``). The same call with
    ``local_files_only=True`` returns that folder from the cache or raises, and downloads and looks up
    nothing. Under it, every file in :data:`PIPELINE_FILES` must exist and be non-empty, with the
    one formula model ``MINERU_FORMULA_CH_SUPPORT`` selects. Only ``huggingface`` (MinerU's default
    source) is probed; no other source's cache layout is guessed."""
    if os.environ.get("MINERU_MODEL_SOURCE", "huggingface") != "huggingface":
        return False
    try:
        from huggingface_hub import snapshot_download  # noqa: PLC0415 — optional/heavy dependency deferred (mineru's)
        from mineru.utils.enum_class import ModelPath  # noqa: PLC0415 — optional/heavy dependency deferred (mineru)

        unselected = ({"unimernet_small", "pp_formulanet_plus_m"} - {formula_model()}).pop()
        root = Path(snapshot_download(ModelPath.pipeline_root_hf, local_files_only=True))
        for name, files in PIPELINE_FILES.items():
            if name == unselected:
                continue
            base = root / getattr(ModelPath, name)
            for path in (base / f for f in files) if files else (base,):
                if not path.is_file() or path.stat().st_size == 0:
                    return False
        return True
    except Exception:  # noqa: BLE001 — best-effort probe; an absent cache or a mineru that moved ModelPath reads as unprovisioned
        return False


def weights_present() -> bool:
    """Best-effort: are the pipeline weights on this box? Either ``mineru-models-download`` ran
    (``mineru.json`` names a pipeline models dir) or MinerU fetched them itself on first use (the
    Hugging Face cache). Gates the real-parse check so an operator who has never provisioned
    pipeline weights sees a skip, not a FAIL for a model download this check never asked for."""
    try:
        from mineru.utils.config_reader import get_local_models_dir  # noqa: PLC0415 — optional/heavy dependency deferred (mineru)

        pipeline_dir = (get_local_models_dir() or {}).get("pipeline")
    except Exception:  # noqa: BLE001 — best-effort probe; an unreadable config reads as unconfigured
        return weights_cached()
    if pipeline_dir and Path(pipeline_dir).is_dir():
        return True
    return weights_cached()


#: Set to ``1`` to skip the fetch (the test suite sets it, so an ``nx`` child it starts never downloads).
SKIP_ENV = "NX_MINERU_WEIGHTS_FETCH_SKIP"

#: Hosts that put the MinerU server on this machine. A server anywhere else has its own weights.
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: What one model's fetch is called with: MinerU's own first-use call for the ``huggingface`` source
#: (``mineru/utils/models_download_utils.py`` ``auto_download_and_get_model_root_path``, pipeline mode):
#: ``snapshot_download(ModelPath.pipeline_root_hf, allow_patterns=[path, path + "/*"])``.
Download = Callable[..., str]


class WeightsFetchError(RuntimeError):
    """The weights were missing and fetching them failed. The message names the retry."""


def selected_models() -> list[str]:
    """The :data:`PIPELINE_FILES` keys the English pipeline loads: every one but the formula model
    ``MINERU_FORMULA_CH_SUPPORT`` does not select."""
    unselected = ({"unimernet_small", "pp_formulanet_plus_m"} - {formula_model()}).pop()
    return [name for name in PIPELINE_FILES if name != unselected]


def manual_command() -> str:
    """The command a user can paste to fetch the pipeline weights by hand (``nx doctor`` names it too)."""
    from nexus._mineru_spawn import mineru_console_command  # noqa: PLC0415 — deferred import (startup cost)

    return mineru_console_command("mineru-models-download", "-s", "huggingface", "-m", "pipeline")


def _remote_server_configured() -> bool:
    """Is ``pdf.mineru_server_url`` a server on another host? Its weights are not this machine's."""
    import urllib.parse  # noqa: PLC0415 — deferred; only needed on this path

    from nexus.config import get_pdf_config  # noqa: PLC0415 — deferred local import — avoids circular deps

    try:
        host = urllib.parse.urlparse(get_pdf_config().mineru_server_url).hostname
    except Exception:  # noqa: BLE001 — an unreadable URL is the server's own problem, reported where it is used
        return False
    return host is not None and host not in _LOCAL_HOSTS


def ensure_pipeline_weights(
    say: Callable[[str], None],
    *,
    download: Download | None = None,
    allow_fetch: bool = True,
    fetch: Fetch | None = None,
) -> bool:
    """Put MinerU's pipeline weights on this machine now when they are not, saying so on *say*.

    Returns True when work ran (a download or a seeding) and False when none was needed. Raises
    :class:`WeightsFetchError` when work was needed and did not complete. Does nothing, and touches no
    network, when :data:`SKIP_ENV` is ``1``, when ``pdf.mineru_server_url`` names another host (that server has
    its own weights), or when the user set ``MINERU_MODEL_SOURCE=local`` (their own models). Any other source
    setting (unset, ``huggingface``, ``modelscope``) is bypassed: nexus's copy is used, and neither is contacted.

    Order of work (F35): nexus's root complete -> nothing. Otherwise per manifest file: already in place with the
    right size and sha256 -> keep; the pinned Hugging Face snapshot in the local cache has it with the right
    sha256 -> link or copy it in (no download); else download the release asset, check its size and sha256, and
    only then rename it into place. The ``.complete`` marker is written after the last file. *fetch*
    replaces the downloader in tests. With *allow_fetch* False (a background run under ``NX_PDF_STACK=refuse``)
    a download that would be needed raises :class:`WeightsFetchError` naming the command instead; seeding from a
    local cache still runs.

    With ``MINERU_FORMULA_CH_SUPPORT`` on, the formula model is one the manifest does not carry: F34's
    Hugging Face fetch (:func:`_ensure_from_huggingface`, with *download* standing in for
    ``huggingface_hub.snapshot_download``) runs as before."""
    if os.environ.get(SKIP_ENV) == "1":
        return False
    if _remote_server_configured():
        return False
    if os.environ.get("MINERU_MODEL_SOURCE") == "local":
        return False
    if formula_model() != "unimernet_small":
        return _ensure_from_huggingface(say, download=download, allow_fetch=allow_fetch)
    return _ensure_nexus_root(say, fetch=fetch, allow_fetch=allow_fetch)


def _hf_snapshot_dir() -> Path | None:
    """The pinned snapshot's directory in the Hugging Face cache, found with no network, or None."""
    try:
        import huggingface_hub.constants as hf_constants  # noqa: PLC0415 — optional dependency deferred (mineru's)

        snapshot = (
            Path(hf_constants.HF_HUB_CACHE)
            / ("models--" + WEIGHTS_REPO.replace("/", "--"))
            / "snapshots"
            / WEIGHTS_REVISION
        )
        return snapshot if snapshot.is_dir() else None
    except Exception:  # noqa: BLE001 — no huggingface_hub or an unreadable cache is just "nothing to seed from"
        return None


def _has_verified(path: Path, entry: WeightFile) -> bool:
    from nexus.db.service_bge_model import file_sha256  # noqa: PLC0415 — deferred local import — one hashing helper

    return _size_ok(path, entry.size) and file_sha256(path) == entry.sha256


def _place(src: Path, dest: Path) -> None:
    """Hard-link *src* to *dest*, or copy it when the two are on different filesystems; atomic by rename."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.unlink(missing_ok=True)
    try:
        try:
            os.link(src, tmp)
        except OSError:
            shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def _seed_from_hf(entry: WeightFile, snapshot: Path, dest: Path) -> bool:
    """Place the snapshot's copy of *entry* at *dest* when it has the pinned size and sha256."""
    src = snapshot / entry.path
    try:
        blob = src.resolve(strict=True)  # the snapshot entry is a symlink to blobs/<hash>; link the blob itself
    except OSError:
        return False
    if not _has_verified(blob, entry):
        return False
    _place(blob, dest)
    return True


def _download_asset(entry: WeightFile, root: Path, base: str, fetch: Fetch) -> None:
    """Fetch *entry*'s asset next to the root, check its size and sha256, then rename it into place.
    Nothing is left at the destination on a failed or mismatching download."""
    from nexus.db import service_bge_model as bge  # noqa: PLC0415 — deferred local import — the asset downloader and digest helper

    part = root / ".partial" / entry.asset
    part.parent.mkdir(parents=True, exist_ok=True)
    try:
        try:
            fetch(f"{base}/{entry.asset}", part)
        except Exception as exc:
            if bge._is_absent(exc):
                raise RuntimeError(f"the release asset {entry.asset} does not exist (HTTP 404) at {base}") from exc
            raise
        size = part.stat().st_size
        if size != entry.size:
            raise RuntimeError(f"size mismatch for {entry.asset}: expected {entry.size} bytes, got {size}")
        digest = bge.file_sha256(part)
        if digest != entry.sha256:
            raise RuntimeError(f"sha256 mismatch for {entry.asset}: expected {entry.sha256}, got {digest}")
        dest = root / entry.path
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(part, dest)
    finally:
        part.unlink(missing_ok=True)


def _human(nbytes: int) -> str:
    if nbytes >= 1e9:
        return f"{nbytes / 1e9:.1f} GB"
    return f"{nbytes / 1e6:.1f} MB" if nbytes >= 1e6 else f"{max(nbytes / 1e3, 0.1):.1f} KB"


def _ensure_nexus_root(say: Callable[[str], None], *, fetch: Fetch | None, allow_fetch: bool) -> bool:
    root = weights_root()
    if _root_complete(root):
        return False
    snapshot = _hf_snapshot_dir()
    seeded: list[WeightFile] = []
    todo: list[WeightFile] = []
    try:
        root.mkdir(parents=True, exist_ok=True)
        for entry in MANIFEST:
            dest = root / entry.path
            if _has_verified(dest, entry):
                continue
            if snapshot is not None and _seed_from_hf(entry, snapshot, dest):
                seeded.append(entry)
            else:
                todo.append(entry)
    except OSError as exc:
        _log.warning("mineru_weights_seed_failed", error=str(exc), error_type=type(exc).__name__)
        raise WeightsFetchError(
            f"preparing MinerU's pipeline weights under {root} failed: {type(exc).__name__}: {exc}. "
            f"Run the command again, or fetch them with `{manual_command()}`."
        ) from exc
    if todo and not allow_fetch:
        raise WeightsFetchError(
            "MinerU's pipeline weights are not on this machine yet, and this run does not download them "
            "(NX_PDF_STACK=refuse or pdf_stack.auto_install: false). Fetch them by running `nx index pdf` on a "
            f"PDF, or with `{manual_command()}`."
        )
    if todo:
        say(
            "nx: MinerU's pipeline weights are not on this machine yet. Fetching them now, before any page is "
            f"parsed (first use only, about {_human(sum(e.size for e in todo))}; several minutes on a slow network)."
        )
        _log.info("mineru_weights_fetch_start", files=len(todo), seeded=len(seeded), base=asset_base_url())
        from nexus.db import service_bge_model as bge  # noqa: PLC0415 — deferred local import — the default asset downloader

        downloader = fetch or bge._httpx_stream
        base = asset_base_url()
        for entry in todo:
            try:
                _download_asset(entry, root, base, downloader)
            except Exception as exc:  # noqa: BLE001 — any fetch failure is the same retry for the user
                _log.warning("mineru_weights_fetch_failed", asset=entry.asset, error=str(exc), error_type=type(exc).__name__)
                raise WeightsFetchError(
                    f"fetching MinerU's pipeline weights failed at {entry.asset}: {type(exc).__name__}: {exc}. "
                    f"Run the command again to resume the download, or fetch them with `{manual_command()}`."
                ) from exc
            say(f"nx:   fetched from nexus asset: {entry.asset} ({_human(entry.size)})")
    if seeded:
        say(
            f"nx:   seeded {len(seeded)} file(s) ({_human(sum(e.size for e in seeded))}) from the Hugging Face "
            "cache; nothing downloaded for them."
        )
    if not all(_size_ok(root / e.path, e.size) for e in MANIFEST):
        _log.warning("mineru_weights_fetch_incomplete")
        raise WeightsFetchError(
            "the MinerU pipeline weights were fetched but some files are still missing. "
            f"Run the command again, or fetch them with `{manual_command()}`."
        )
    (root / COMPLETE_MARKER).write_text(WEIGHTS_REVISION + "\n", encoding="utf-8")
    shutil.rmtree(root / ".partial", ignore_errors=True)
    _log.info("mineru_weights_fetch_done", downloaded=len(todo), seeded=len(seeded))
    say("nx: MinerU pipeline weights are ready.")
    return True


def _ensure_from_huggingface(
    say: Callable[[str], None], *, download: Download | None = None, allow_fetch: bool = True
) -> bool:
    """Fetch MinerU's pipeline weights now when this machine lacks them, saying so on *say*.

    Returns True when a fetch ran and False when none was needed. Raises :class:`WeightsFetchError`
    when one was needed and did not complete. Does nothing, and touches no network, when the weights
    are in the Hugging Face cache (:func:`weights_cached`), when ``MINERU_MODEL_SOURCE`` is set to anything but
    ``huggingface`` (``local`` reads ``mineru.json``; ``modelscope`` keeps MinerU's own first-use
    fetch, unchanged by F34), when ``pdf.mineru_server_url`` names another host, or when MinerU or
    ``huggingface_hub`` cannot be imported (the stack gate and the extractor report that). *download*
    replaces ``huggingface_hub.snapshot_download`` in tests. :data:`SKIP_ENV` set to ``1`` skips it.
    With *allow_fetch* False (a background run under ``NX_PDF_STACK=refuse``), weights that would be
    fetched raise :class:`WeightsFetchError` naming the command instead: such a run never downloads."""
    if os.environ.get(SKIP_ENV) == "1":
        return False
    if os.environ.get("MINERU_MODEL_SOURCE", "huggingface") != "huggingface":
        return False
    if _remote_server_configured():
        return False
    try:
        from mineru.utils.enum_class import ModelPath  # noqa: PLC0415 — optional/heavy dependency deferred (mineru)

        if download is None:
            from huggingface_hub import snapshot_download as download  # noqa: PLC0415 — optional/heavy dependency deferred (mineru's)
    except Exception as exc:  # noqa: BLE001 — an unimportable stack is reported by the stack gate and the extractor
        _log.warning("mineru_weights_unprobeable", error=str(exc), error_type=type(exc).__name__)
        return False
    # The HF cache itself, not :func:`weights_present`: under this source the server reads that cache
    # and ignores a ``mineru.json`` models dir (MinerU reads it only for ``MINERU_MODEL_SOURCE=local``).
    if weights_cached():
        return False
    if not allow_fetch:
        raise WeightsFetchError(
            "MinerU's pipeline weights are not on this machine yet, and this run does not download them "
            "(NX_PDF_STACK=refuse or pdf_stack.auto_install: false). Fetch them by running `nx index pdf` on a "
            f"PDF, or with `{manual_command()}`."
        )
    say(
        "nx: MinerU's pipeline weights are not on this machine yet. Fetching them now, before any page is "
        "parsed (first use only, about 1.2 GB; several minutes on a slow network)."
    )
    _log.info("mineru_weights_fetch_start", models=selected_models())
    for name in selected_models():
        say(f"nx:   {name}")
        try:
            path = getattr(ModelPath, name).strip("/")
            download(ModelPath.pipeline_root_hf, allow_patterns=[path, path + "/*"])
        except Exception as exc:  # noqa: BLE001 — any fetch failure is the same retry for the user
            _log.warning("mineru_weights_fetch_failed", model=name, error=str(exc), error_type=type(exc).__name__)
            raise WeightsFetchError(
                f"fetching MinerU's pipeline weights failed at {name}: {type(exc).__name__}: {exc}. "
                f"Run the command again to resume the download, or fetch them with `{manual_command()}`."
            ) from exc
    if not weights_cached():
        _log.warning("mineru_weights_fetch_incomplete")
        raise WeightsFetchError(
            "the MinerU pipeline weights were fetched but some files are still missing or empty. "
            f"Run the command again, or fetch them with `{manual_command()}`."
        )
    _log.info("mineru_weights_fetch_done")
    say("nx: MinerU pipeline weights are ready.")
    return True
