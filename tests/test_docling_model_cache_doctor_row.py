# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor's "Docling model cache" row (RDR-237, nexus-tr1rj.5).

Docling is removed; the models it downloaded (about 1.1 GB) stay on a box that ever ran it.
The row reports them with the command that deletes them, as a warning and never red, and it is
not applicable (no row at all) on a box that has none, which is every virgin box. Nothing deletes
the cache. These tests build real directories under tmp_path and pass the environment and home
in, so they depend on nothing on the machine that runs them.
"""
from __future__ import annotations

import inspect
from pathlib import Path

from nexus import health


def _make_cache(hub: Path, name: str, size: int = 2048) -> Path:
    model = hub / name
    (model / "blobs").mkdir(parents=True)
    (model / "blobs" / "weights").write_bytes(b"x" * size)
    return model


def test_a_virgin_box_gets_no_row(tmp_path: Path) -> None:
    assert health._check_leftover_docling_cache(environ={}, home=tmp_path) == []


def test_a_hub_cache_with_only_other_models_gets_no_row(tmp_path: Path) -> None:
    hub = tmp_path / ".cache" / "huggingface" / "hub"
    _make_cache(hub, "models--cross-encoder--ms-marco-MiniLM-L-6-v2")
    _make_cache(hub, "models--opendatalab--PDF-Extract-Kit-1.0")
    assert health._check_leftover_docling_cache(environ={}, home=tmp_path) == []


def test_a_leftover_cache_is_a_warning_with_its_size_its_path_and_the_delete_command(tmp_path: Path) -> None:
    hub = tmp_path / ".cache" / "huggingface" / "hub"
    layout = _make_cache(hub, "models--ds4sd--docling-models", size=3_000_000)
    other = _make_cache(hub, "models--cross-encoder--ms-marco-MiniLM-L-6-v2")

    [row] = health._check_leftover_docling_cache(environ={}, home=tmp_path)

    assert row.label == "Docling model cache"
    assert row.ok is False and row.warn is True, "a warning, never a red failure"
    assert str(layout) in row.detail and str(other) not in row.detail
    assert "0.0 GB" in row.detail
    [suggestion] = row.fix_suggestions
    assert suggestion.startswith("Delete it: ") and str(layout) in suggestion and str(other) not in suggestion
    assert layout.is_dir(), "the doctor reports the cache; it never deletes it"


def test_every_model_directory_the_old_extractor_downloaded_is_found(tmp_path: Path) -> None:
    hub = tmp_path / ".cache" / "huggingface" / "hub"
    names = (
        "models--ds4sd--docling-models", "models--ds4sd--CodeFormula",
        "models--docling-project--docling-layout-heron", "models--docling-project--CodeFormulaV2",
    )
    for name in names:
        _make_cache(hub, name)
    [row] = health._check_leftover_docling_cache(environ={}, home=tmp_path)
    for name in names:
        assert name in row.detail


def test_the_artifacts_directory_the_old_ci_setting_named_is_found_too(tmp_path: Path) -> None:
    artifacts = tmp_path / "docling-artifacts"
    artifacts.mkdir()
    (artifacts / "layout.bin").write_bytes(b"x" * 10)
    [row] = health._check_leftover_docling_cache(
        environ={"NEXUS_DOCLING_ARTIFACTS_PATH": str(artifacts)}, home=tmp_path,
    )
    assert str(artifacts) in row.detail


def test_an_artifacts_setting_that_names_nothing_gets_no_row(tmp_path: Path) -> None:
    assert health._check_leftover_docling_cache(
        environ={"NEXUS_DOCLING_ARTIFACTS_PATH": str(tmp_path / "gone")}, home=tmp_path,
    ) == []


def test_the_hub_cache_location_follows_huggingfaces_own_precedence(tmp_path: Path) -> None:
    moved = tmp_path / "elsewhere"
    _make_cache(moved, "models--ds4sd--docling-models")
    # HF_HUB_CACHE wins over HF_HOME, which wins over XDG_CACHE_HOME, which wins over the home default.
    assert health._check_leftover_docling_cache(environ={"HF_HUB_CACHE": str(moved)}, home=tmp_path)
    home_hub = tmp_path / "hf-home"
    _make_cache(home_hub / "hub", "models--ds4sd--docling-models")
    assert health._check_leftover_docling_cache(environ={"HF_HOME": str(home_hub)}, home=tmp_path)
    xdg = tmp_path / "xdg"
    _make_cache(xdg / "huggingface" / "hub", "models--ds4sd--docling-models")
    assert health._check_leftover_docling_cache(environ={"XDG_CACHE_HOME": str(xdg)}, home=tmp_path)
    # A cache in the default place is not found when HF_HUB_CACHE points somewhere empty.
    _make_cache(tmp_path / ".cache" / "huggingface" / "hub", "models--ds4sd--docling-models")
    empty = tmp_path / "empty-hub"
    empty.mkdir()
    assert health._check_leftover_docling_cache(environ={"HF_HUB_CACHE": str(empty)}, home=tmp_path) == []


def test_the_row_is_part_of_the_default_doctor_run() -> None:
    assert "_check_leftover_docling_cache()" in inspect.getsource(health.run_health_checks)
