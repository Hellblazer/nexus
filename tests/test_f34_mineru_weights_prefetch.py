# SPDX-License-Identifier: AGPL-3.0-or-later
"""F34: MinerU's pipeline weights are fetched before any page budget starts, as a step the user sees.

F35 moved the default model's fetch to nexus's own release assets (tests/test_f35_mineru_self_host_weights.py);
this module drives the Hugging Face fetch that stays for ``MINERU_FORMULA_CH_SUPPORT`` (its fixture turns that on).

Sam 2026-11-21 (T2 ``nexus/sam-ruling-f34-mineru-weights-8.5.5-2026-11-21``): on a fresh install the first
``nx index pdf --extractor mineru`` downloaded the weights inside the 180 s per-page budget, the server was
killed mid-download, and pages came out ``mineru+degraded``. ``pdf_stack.ensure_or_exit`` now fetches them
first. These tests run the real probe against a real Hugging Face cache layout under a fresh cache directory;
only the network fetch is a stub that writes the files the way ``snapshot_download`` lays them out.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest

from nexus import mineru_weights, pdf_stack

from tests._mineru_rearm import mineru_importorskip  # noqa: E402

mineru_importorskip("mineru.utils.enum_class")
mineru_importorskip("huggingface_hub")

from mineru.utils.enum_class import ModelPath  # noqa: E402 — after the importorskip guards

#: tests/conftest.py stubs the fetch for every other module; this one drives it.
REAL_MINERU_WEIGHTS_FETCH = True

_COMMIT = "0" * 40


class _FakeHub:
    """A Hugging Face cache for ``opendatalab/PDF-Extract-Kit-1.0``, filled the way ``snapshot_download``
    fills it: ``refs/main`` names a commit and the files sit under ``snapshots/<commit>/``."""

    def __init__(self, cache: Path, *, delay_s: float = 0.0, fail_at: str | None = None, skip: str | None = None):
        self.storage = cache / ("models--" + ModelPath.pipeline_root_hf.replace("/", "--"))
        self.snapshot = self.storage / "snapshots" / _COMMIT
        self.calls: list[tuple[str, list[str]]] = []
        self.delay_s = delay_s
        self.fail_at = fail_at
        self.skip = skip

    def _write(self, name: str) -> None:
        rel = getattr(ModelPath, name).strip("/")
        files = mineru_weights.PIPELINE_FILES[name]
        targets = [self.snapshot / rel / f for f in files] if files else [self.snapshot / rel]
        for target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"weights")
        (self.storage / "refs").mkdir(parents=True, exist_ok=True)
        (self.storage / "refs" / "main").write_text(_COMMIT)

    def fill(self) -> None:
        for name in mineru_weights.selected_models():
            self._write(name)

    def download(self, repo_id: str, *, allow_patterns: list[str]) -> str:
        self.calls.append((repo_id, allow_patterns))
        if self.delay_s:
            time.sleep(self.delay_s)
        rel = allow_patterns[0]
        name = next(n for n in mineru_weights.PIPELINE_FILES if getattr(ModelPath, n).strip("/") == rel)
        if name == self.fail_at:
            raise OSError("Network is unreachable")
        if name != self.skip:
            self._write(name)
        return str(self.snapshot)


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """A fresh, empty Hugging Face cache, no ``mineru.json``, the default source and a local server."""
    import huggingface_hub.constants as hf_constants  # noqa: PLC0415 — the stack's own module
    import mineru.utils.config_reader as config_reader  # noqa: PLC0415 — the stack's own module

    cache = tmp_path / "hf-cache"
    cache.mkdir()
    monkeypatch.setattr(hf_constants, "HF_HUB_CACHE", str(cache))
    monkeypatch.setattr(config_reader, "get_local_models_dir", lambda: None)
    for name in (mineru_weights.SKIP_ENV, "MINERU_MODEL_SOURCE", "MINERU_FORMULA_CH_SUPPORT", "HF_HUB_OFFLINE"):
        monkeypatch.delenv(name, raising=False)
    # F35: the Hugging Face fetch F34 wrote is what runs for the formula model the self-hosted manifest does not
    # carry (the Chinese-formula opt-in); the default model's path is tests/test_f35_mineru_self_host_weights.py.
    monkeypatch.setenv("MINERU_FORMULA_CH_SUPPORT", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(mineru_weights, "_remote_server_configured", lambda: False)
    return _FakeHub(cache)


def _collect() -> tuple[list[str], Callable[[str], None]]:
    lines: list[str] = []
    return lines, lines.append


def test_absent_weights_are_fetched_with_visible_lines_then_found_by_the_probe(hub):
    assert not mineru_weights.weights_present()
    lines, say = _collect()

    assert mineru_weights.ensure_pipeline_weights(say, download=hub.download) is True

    expected = [
        (ModelPath.pipeline_root_hf, [rel, rel + "/*"])
        for rel in (getattr(ModelPath, n).strip("/") for n in mineru_weights.selected_models())
    ]
    assert hub.calls == expected
    assert "before any page is parsed" in lines[0]
    assert lines[-1] == "nx: MinerU pipeline weights are ready."
    assert mineru_weights.weights_cached()
    assert mineru_weights.weights_present()


def test_present_weights_make_no_fetch_and_say_nothing(hub):
    hub.fill()
    lines, say = _collect()

    assert mineru_weights.ensure_pipeline_weights(say, download=hub.download) is False
    assert hub.calls == []
    assert lines == []


def test_a_slow_fetch_runs_to_completion(hub):
    """A throttled network: every model takes a while, and the step still finishes with the weights
    complete. (No page budget can apply: the step runs in the CLI gate, before any extractor exists.)"""
    hub.delay_s = 0.2
    lines, say = _collect()

    started = time.monotonic()
    assert mineru_weights.ensure_pipeline_weights(say, download=hub.download) is True
    assert time.monotonic() - started >= 0.2 * len(mineru_weights.selected_models())
    assert mineru_weights.weights_cached()


def test_a_failed_fetch_is_loud_and_names_the_retry(hub):
    hub.fail_at = "pytorch_paddle"
    with pytest.raises(mineru_weights.WeightsFetchError) as info:
        mineru_weights.ensure_pipeline_weights(lambda _line: None, download=hub.download)
    message = str(info.value)
    assert "pytorch_paddle" in message and "Network is unreachable" in message
    assert "mineru-models-download" in message and "-m pipeline" in message


def test_a_fetch_that_leaves_a_file_missing_is_an_error(hub):
    hub.skip = "slanet_plus"
    with pytest.raises(mineru_weights.WeightsFetchError, match="still missing"):
        mineru_weights.ensure_pipeline_weights(lambda _line: None, download=hub.download)


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param(lambda mp: mp.setenv("MINERU_MODEL_SOURCE", "modelscope"), id="modelscope-source"),
        pytest.param(lambda mp: mp.setenv("MINERU_MODEL_SOURCE", "local"), id="local-source"),
        pytest.param(lambda mp: mp.setenv(mineru_weights.SKIP_ENV, "1"), id="skip-env"),
        pytest.param(
            lambda mp: mp.setattr(mineru_weights, "_remote_server_configured", lambda: True), id="remote-server"
        ),
    ],
)
def test_cases_left_as_they_were_make_no_fetch(hub, monkeypatch, setup):
    setup(monkeypatch)
    assert mineru_weights.ensure_pipeline_weights(lambda _line: None, download=hub.download) is False
    assert hub.calls == []


def test_remote_server_url_is_detected(monkeypatch):
    from nexus import config  # noqa: PLC0415

    class _Pdf:
        mineru_server_url = "http://gpu-box.lan:8010"

    monkeypatch.setattr(config, "get_pdf_config", lambda *a, **k: _Pdf())
    assert mineru_weights._remote_server_configured() is True
    _Pdf.mineru_server_url = "http://127.0.0.1:8010"
    assert mineru_weights._remote_server_configured() is False


# ── the gate every PDF-indexing command passes ──────────────────────────────


@pytest.fixture
def gate(hub, monkeypatch):
    """``ensure_or_exit`` with the stack present, using the stub fetch."""
    monkeypatch.setattr(pdf_stack, "ensure", lambda entry: pdf_stack.Outcome.PRESENT)
    real = mineru_weights.ensure_pipeline_weights
    monkeypatch.setattr(
        mineru_weights,
        "ensure_pipeline_weights",
        lambda say, *, download=None, allow_fetch=True: real(say, download=hub.download, allow_fetch=allow_fetch),
    )
    monkeypatch.setenv(pdf_stack.POLICY_ENV, pdf_stack.POLICY_INSTALL)
    return hub


def test_the_gate_fetches_before_returning(gate, capsys):
    assert pdf_stack.ensure_or_exit("nx index pdf", extractor="mineru") is True
    assert len(gate.calls) == len(mineru_weights.selected_models())
    assert "before any page is parsed" in capsys.readouterr().err


def test_the_gate_uses_the_configured_extractor_when_none_is_given(gate, monkeypatch):
    from nexus import config  # noqa: PLC0415

    monkeypatch.setattr(config, "get_pdf_extractor", lambda *a, **k: "pymupdf")
    assert pdf_stack.ensure_or_exit("nx index pdf", extractor="") is True
    assert gate.calls == []


def test_the_gate_exits_75_on_a_failed_fetch(gate, capsys):
    gate.fail_at = "pp_doclayout_v2"
    with pytest.raises(SystemExit) as info:
        pdf_stack.ensure_or_exit("nx index pdf", extractor="mineru")
    assert info.value.code == pdf_stack.EXIT_NETWORK == 75
    assert "fetching MinerU's pipeline weights failed" in capsys.readouterr().err


def test_the_gate_raises_on_a_failed_fetch_when_asked_to_hold(gate):
    gate.fail_at = "pp_doclayout_v2"
    with pytest.raises(mineru_weights.WeightsFetchError):
        pdf_stack.ensure_or_exit("nx index repo", extractor="mineru", hold_on_refuse=True)


def test_a_mineru_json_models_dir_does_not_stand_in_for_the_hf_cache(hub, tmp_path, monkeypatch):
    """Under the default source the server reads the HF cache and ignores ``mineru.json``: a models
    dir named there (doctor's ``weights_present``) must not skip the fetch."""
    import mineru.utils.config_reader as config_reader  # noqa: PLC0415

    models = tmp_path / "models"
    models.mkdir()
    monkeypatch.setattr(config_reader, "get_local_models_dir", lambda: {"pipeline": str(models)})
    assert mineru_weights.weights_present() and not mineru_weights.weights_cached()
    assert mineru_weights.ensure_pipeline_weights(lambda _line: None, download=hub.download) is True
    assert hub.calls


# ── nx index repo: a failed fetch holds the PDFs back, named as a fetch failure ──


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    root = tmp_path / "repo"
    root.mkdir()
    (root / "paper.pdf").write_bytes(b"%PDF-1.4\n")
    return root


def test_index_repo_holds_pdfs_with_the_fetch_failure_not_a_refused_install(gate, repo):
    from nexus.indexer import _hold_pdfs_without_stack  # noqa: PLC0415

    gate.fail_at = "pp_doclayout_v2"
    phases: list[str] = []
    held = _hold_pdfs_without_stack(
        [repo / "paper.pdf"], repo, on_phase=phases.append, probe=lambda paths: paths, extractor="mineru"
    )
    assert held == [repo / "paper.pdf"]
    assert len(phases) == 1
    assert "fetching MinerU's pipeline weights failed at pp_doclayout_v2" in phases[0]
    assert "not installed" not in phases[0] and "refused" not in phases[0]
    assert pdf_stack.skipped_count(pdf_stack.SKIP_CAUSE_WEIGHTS) == 1
    assert pdf_stack.skipped_count(pdf_stack.SKIP_CAUSE_STACK) == 0


def test_index_repo_fetches_nothing_when_every_pdf_is_current(gate, repo):
    from nexus.indexer import _hold_pdfs_without_stack  # noqa: PLC0415

    assert _hold_pdfs_without_stack([repo / "paper.pdf"], repo, probe=lambda paths: [], extractor="mineru") == []
    assert gate.calls == []


def test_index_repo_fetches_then_goes_on_when_a_pdf_needs_work(gate, repo):
    from nexus.indexer import _hold_pdfs_without_stack  # noqa: PLC0415

    assert _hold_pdfs_without_stack([repo / "paper.pdf"], repo, probe=lambda paths: paths, extractor="mineru") == []
    assert len(gate.calls) == len(mineru_weights.selected_models())


def test_a_background_index_repo_under_refuse_holds_pdfs_and_downloads_nothing(gate, repo, monkeypatch):
    """The post-commit hook runs ``NX_PDF_STACK=refuse nx index repo``: it must never start a 1.2 GB download."""
    from nexus.indexer import _hold_pdfs_without_stack  # noqa: PLC0415

    monkeypatch.setenv(pdf_stack.POLICY_ENV, pdf_stack.POLICY_REFUSE)
    phases: list[str] = []
    held = _hold_pdfs_without_stack(
        [repo / "paper.pdf"], repo, on_phase=phases.append, probe=lambda paths: paths, extractor="mineru"
    )
    assert held == [repo / "paper.pdf"]
    assert gate.calls == []
    assert "does not download them" in phases[0] and "mineru-models-download" in phases[0]
    assert pdf_stack.skipped_count(pdf_stack.SKIP_CAUSE_WEIGHTS) == 1


def test_an_interactive_command_under_refuse_still_fetches(gate, monkeypatch):
    """``refuse`` governs installing the stack and background runs; a command a person runs fetches."""
    monkeypatch.setenv(pdf_stack.POLICY_ENV, pdf_stack.POLICY_REFUSE)
    assert pdf_stack.ensure_or_exit("nx index pdf", extractor="mineru") is True
    assert gate.calls
