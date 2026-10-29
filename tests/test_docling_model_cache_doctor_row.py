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


def test_the_row_names_the_size_the_cache_holds(tmp_path: Path) -> None:
    """A sparse 1.5 GB file stands in for the weights: the row reads its size from the one tree
    helper, so a size of zero (or the helper's completeness flag in place of its byte count) fails."""
    hub = tmp_path / ".cache" / "huggingface" / "hub"
    layout = _make_cache(hub, "models--ds4sd--docling-models")
    with open(layout / "weights.safetensors", "wb") as fh:
        fh.truncate(1_500_000_000)

    [row] = health._check_leftover_docling_cache(environ={}, home=tmp_path)

    assert "1.5 GB" in row.detail, row.detail


def test_an_unreadable_docling_tools_directory_does_not_stop_the_doctor(tmp_path: Path, monkeypatch) -> None:
    real_is_dir = Path.is_dir

    def is_dir(self: Path) -> bool:
        if self.name == "models" and self.parent.name == "docling":
            raise PermissionError(13, "Permission denied", str(self))
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    assert health._check_leftover_docling_cache(environ={}, home=tmp_path) == []


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


def test_the_artifacts_directory_the_old_ci_setting_named_is_found_but_never_in_a_delete_command(
    tmp_path: Path,
) -> None:
    """The user chose that directory and may share it: the row names it, and no command deletes it."""
    artifacts = tmp_path / "docling-artifacts"
    artifacts.mkdir()
    (artifacts / "layout.bin").write_bytes(b"x" * 10)
    [row] = health._check_leftover_docling_cache(
        environ={"NEXUS_DOCLING_ARTIFACTS_PATH": str(artifacts)}, home=tmp_path,
    )
    assert str(artifacts) in row.detail
    [suggestion] = row.fix_suggestions
    assert "rm -rf" not in suggestion and "Remove-Item" not in suggestion
    assert "NEXUS_DOCLING_ARTIFACTS_PATH" in suggestion and "directory" in suggestion and "you set" in suggestion
    assert artifacts.is_dir()


def test_a_user_set_path_beside_a_hub_cache_stays_out_of_the_delete_command(tmp_path: Path) -> None:
    hub = tmp_path / ".cache" / "huggingface" / "hub"
    layout = _make_cache(hub, "models--ds4sd--docling-models")
    artifacts = tmp_path / "shared-models"
    artifacts.mkdir()
    [row] = health._check_leftover_docling_cache(
        environ={"NEXUS_DOCLING_ARTIFACTS_PATH": str(artifacts)}, home=tmp_path,
    )
    delete = next(f for f in row.fix_suggestions if f.startswith("Delete it: "))
    assert str(layout) in delete and str(artifacts) not in delete
    assert str(artifacts) in row.detail
    assert any("you set" in f and str(artifacts) in f for f in row.fix_suggestions)


def test_the_artifacts_setting_expands_a_tilde_and_a_variable(tmp_path: Path) -> None:
    (tmp_path / "models").mkdir()
    [row] = health._check_leftover_docling_cache(
        environ={"NEXUS_DOCLING_ARTIFACTS_PATH": "~/models"}, home=tmp_path,
    )
    assert str(tmp_path / "models") in row.detail
    [row] = health._check_leftover_docling_cache(
        environ={"NEXUS_DOCLING_ARTIFACTS_PATH": "$ROOT/models", "ROOT": str(tmp_path)}, home=tmp_path,
    )
    assert str(tmp_path / "models") in row.detail


def test_the_default_directory_docling_tools_downloads_into_is_found(tmp_path: Path) -> None:
    models = tmp_path / ".cache" / "docling" / "models"
    (models / "docling-project--docling-layout-heron").mkdir(parents=True)
    (models / "docling-project--docling-layout-heron" / "w.bin").write_bytes(b"x" * 10)
    [row] = health._check_leftover_docling_cache(environ={}, home=tmp_path)
    assert str(models) in row.detail
    [suggestion] = row.fix_suggestions
    assert suggestion.startswith("Delete it: ") and str(models) in suggestion, (
        "docling-tools made that directory, so the row may offer to delete it"
    )


def test_an_artifacts_setting_that_names_nothing_gets_no_row(tmp_path: Path) -> None:
    assert health._check_leftover_docling_cache(
        environ={"NEXUS_DOCLING_ARTIFACTS_PATH": str(tmp_path / "gone")}, home=tmp_path,
    ) == []


def _only_found_in(row_detail: str, winner: Path, *losers: Path) -> None:
    assert str(winner) in row_detail, f"{winner} should have been the cache searched: {row_detail}"
    for loser in losers:
        assert str(loser) not in row_detail, f"{loser} should not have been searched: {row_detail}"


def test_the_hub_cache_location_follows_huggingfaces_own_precedence(tmp_path: Path) -> None:
    moved = tmp_path / "elsewhere"
    _make_cache(moved, "models--ds4sd--docling-models")
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


def test_when_two_variables_are_set_the_higher_precedence_one_names_the_cache_searched(tmp_path: Path) -> None:
    """Each variable points at a DIFFERENT cache holding a leftover, so a reversed order shows the wrong path."""
    hub_cache = tmp_path / "a-hub-cache"
    legacy = tmp_path / "b-legacy"
    hf_home = tmp_path / "c-hf-home"
    xdg = tmp_path / "d-xdg"
    for cache in (hub_cache, legacy, hf_home / "hub", xdg / "huggingface" / "hub",
                  tmp_path / ".cache" / "huggingface" / "hub"):
        _make_cache(cache, "models--ds4sd--docling-models")
    home_default = tmp_path / ".cache" / "huggingface" / "hub"
    everything = {
        "HF_HUB_CACHE": str(hub_cache), "HUGGINGFACE_HUB_CACHE": str(legacy),
        "HF_HOME": str(hf_home), "XDG_CACHE_HOME": str(xdg),
    }

    def searched(**chosen: str) -> str:
        [row] = health._check_leftover_docling_cache(environ=chosen, home=tmp_path)
        return row.detail

    _only_found_in(searched(**everything), hub_cache, legacy, hf_home, xdg, home_default)
    without = {k: v for k, v in everything.items() if k != "HF_HUB_CACHE"}
    _only_found_in(searched(**without), legacy, hub_cache, hf_home, xdg, home_default)
    without.pop("HUGGINGFACE_HUB_CACHE")
    _only_found_in(searched(**without), hf_home / "hub", hub_cache, legacy, xdg, home_default)
    without.pop("HF_HOME")
    _only_found_in(searched(**without), xdg / "huggingface" / "hub", hub_cache, legacy, hf_home, home_default)
    _only_found_in(searched(), home_default, hub_cache, legacy, hf_home, xdg)


def test_the_legacy_cache_variable_alone_is_honoured(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy-hub"
    _make_cache(legacy, "models--docling-project--docling-layout-heron")
    [row] = health._check_leftover_docling_cache(environ={"HUGGINGFACE_HUB_CACHE": str(legacy)}, home=tmp_path)
    assert str(legacy) in row.detail


def test_a_tilde_or_a_variable_in_the_cache_settings_is_expanded(tmp_path: Path) -> None:
    _make_cache(tmp_path / "hf" / "hub", "models--ds4sd--docling-models")
    for env in (
        {"HF_HOME": "~/hf"},
        {"HF_HOME": "$ROOT/hf", "ROOT": str(tmp_path)},
        {"HF_HOME": "${ROOT}/hf", "ROOT": str(tmp_path)},
    ):
        [row] = health._check_leftover_docling_cache(environ=env, home=tmp_path)
        assert str(tmp_path / "hf" / "hub" / "models--ds4sd--docling-models") in row.detail, env
    _make_cache(tmp_path / "direct", "models--ds4sd--docling-models")
    for name in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        [row] = health._check_leftover_docling_cache(environ={name: "~/direct"}, home=tmp_path)
        assert str(tmp_path / "direct" / "models--ds4sd--docling-models") in row.detail, name


def test_the_row_is_part_of_the_default_doctor_run() -> None:
    assert "_check_leftover_docling_cache()" in inspect.getsource(health.run_health_checks)


def test_the_cache_row_shares_the_modules_one_tree_size_helper(tmp_path: Path) -> None:
    """A second ``_tree_bytes`` returning an int once shadowed the Holders row's tuple-returning one, so
    ``_format_held_size`` failed to unpack it. The row reuses the one helper; the Holders row still works."""
    (tmp_path / "f").write_bytes(b"x" * 10)
    assert health._tree_bytes(tmp_path) == (10, True)
    assert health._format_held_size(tmp_path) == "10 B"
