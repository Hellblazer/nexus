# SPDX-License-Identifier: AGPL-3.0-or-later
"""MinerU's pipeline weights: whether they are on this box, and fetching them before any page is timed.

F34 (Sam 2026-11-21, T2 ``nexus/sam-ruling-f34-mineru-weights-8.5.5-2026-11-21``): on a fresh install the
first MinerU parse downloaded these weights (``opendatalab/PDF-Extract-Kit-1.0``, about 1.2 GB) from inside
the per-page budget, so the server was killed mid-download at ``mineru_server_timeout`` and the first pages
came out degraded. :func:`ensure_pipeline_weights` fetches them first, as a step the user sees, from the CLI
gate that every PDF-indexing command already passes (:func:`nexus.pdf_stack.ensure_or_exit`). It fetches
with MinerU's own first-use call, so the files land where the server looks for them.

``nx doctor`` gates its real-parse check on the same probe (:func:`weights_present`)."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

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


def weights_cached() -> bool:
    """Are the weights the English pipeline loads in the Hugging Face cache, found without any network access?

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
