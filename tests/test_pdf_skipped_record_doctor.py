# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 (nexus-tr1rj.26): the doctor rows that read the held-PDF record
(``pdf-stack-skipped.json``) survive a record nobody can trust, and do not report holds nothing can
clear.

* The file is one a person can edit or a crash can truncate, and ``pdf_stack.skipped_count`` runs
  outside any guard in the "PDF support" and "PDF settings" rows. A malformed record counts as
  nothing and never takes ``nx doctor`` down.
* A hold keyed on a repo directory that no longer exists names a remedy (``nx index repo``) that
  cannot clear it, so both rows ignore it.
* ``_check_pdf_config`` swallows an unreadable config (that is the config rows' finding); a
  ``get_pdf_config`` that raises something other than ``PdfConfigError`` does not stop the sweep.

The sweep tests run the real ``run_health_checks`` with every other ``_check_*`` row stubbed out.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from nexus import health, pdf_stack
from nexus.pdf_stack import InstallPlan, StackState

_PLAN = InstallPlan("generation", True, pdf_stack.INSTALL_COMMAND)
_RECORD = "pdf-stack-skipped.json"


@pytest.fixture(autouse=True)
def _world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    monkeypatch.setattr(pdf_stack, "install_plan", lambda: _PLAN)
    monkeypatch.setattr(pdf_stack, "state", lambda: StackState("absent", "MinerU and torch are not installed"))
    pdf_stack.reset_cache()
    return cfg


def _write_record(cfg: Path, text: str) -> None:
    (cfg / _RECORD).write_text(text, encoding="utf-8")


def _write_pdf_settings(cfg: Path, **pdf) -> None:
    (cfg / "config.yml").write_text(yaml.safe_dump({"pdf": pdf}))


def _only_other_checks_stubbed(monkeypatch: pytest.MonkeyPatch, *keep: str) -> None:
    for name in dir(health):
        if name.startswith("_check_") and name not in keep and callable(getattr(health, name)):
            monkeypatch.setattr(health, name, lambda *a, **k: [])


# A record that is not what record_skipped writes: each is "nothing held" to the count and the rows.
CORRUPT = {
    "paths-null": json.dumps({"/r": {"paths": None, "cause": "stack"}}),
    "paths-number": json.dumps({"/r": {"paths": 5, "cause": "mineru_bounds"}}),
    "paths-object": json.dumps({"/r": {"paths": {"a.pdf": 1}}}),
    "entry-list": json.dumps({"/r": ["a.pdf"]}),
    "entry-number": json.dumps({"/r": 7}),
    "entry-null": json.dumps({"/r": None}),
    "top-list": json.dumps([{"paths": ["a.pdf"]}]),
    "top-number": "3",
    "top-string": '"held"',
    "truncated": '{"/r": {"paths": ["a.pdf"',
    "empty-file": "",
    "nested-too-deep": "[" * 200000,
}


@pytest.mark.parametrize("text", CORRUPT.values(), ids=CORRUPT.keys())
@pytest.mark.parametrize("cause", [None, pdf_stack.SKIP_CAUSE_STACK, pdf_stack.SKIP_CAUSE_BOUNDS])
@pytest.mark.parametrize("existing_only", [False, True])
def test_skipped_count_of_a_corrupt_record_is_zero_and_never_raises(_world, text, cause, existing_only) -> None:
    _write_record(_world, text)
    assert pdf_stack.skipped_count(cause, existing_repos_only=existing_only) == 0


def test_skipped_count_counts_only_the_string_paths_of_a_list(_world, tmp_path) -> None:
    _write_record(_world, json.dumps({str(tmp_path): {"paths": ["a.pdf", 1, None, ["b.pdf"], "c.pdf"]}}))
    assert pdf_stack.skipped_count() == 2


def test_skipped_count_keeps_the_good_entries_beside_a_bad_one(_world, tmp_path) -> None:
    _write_record(_world, json.dumps({
        str(tmp_path): {"paths": ["a.pdf", "b.pdf"], "cause": "stack"},
        "/bad-null": {"paths": None},
        "/bad-entry": "x",
    }))
    assert pdf_stack.skipped_count() == 2
    assert pdf_stack.skipped_count(pdf_stack.SKIP_CAUSE_STACK) == 2


@pytest.mark.parametrize("text", CORRUPT.values(), ids=CORRUPT.keys())
def test_both_rows_survive_a_corrupt_record(_world, text) -> None:
    _write_record(_world, text)
    _write_pdf_settings(_world, mineru_page_timeout_s=100, mineru_page_batch=4)  # the red row must still come

    (support,) = health._check_pdf_support()
    (settings,) = health._check_pdf_config()

    assert support.label == "PDF support" and "PDF(s)" not in support.detail
    assert settings.label == "PDF settings" and settings.ok is False and "PDF(s)" not in settings.detail


@pytest.mark.parametrize("text", CORRUPT.values(), ids=CORRUPT.keys())
def test_the_sweep_survives_a_corrupt_record(monkeypatch, _world, tmp_path, text) -> None:
    """Through ``run_health_checks`` itself: the call sits outside any guard there."""
    _only_other_checks_stubbed(monkeypatch, "_check_pdf_support", "_check_pdf_config")
    monkeypatch.chdir(tmp_path)
    _write_record(_world, text)
    _write_pdf_settings(_world, mineru_page_timeout_s=100, mineru_page_batch=4)

    results, _is_local = health.run_health_checks()

    labels = [r.label for r in results]
    assert "PDF support" in labels and "PDF settings" in labels


# ── a hold whose repo is gone ────────────────────────────────────────────────


def test_the_support_row_ignores_a_stack_hold_in_a_repo_that_no_longer_exists(tmp_path) -> None:
    gone = tmp_path / "deleted-repo"
    pdf_stack.record_skipped([gone / "a.pdf", gone / "b.pdf"], repo=gone)  # never created
    (row,) = health._check_pdf_support()
    assert "PDF(s)" not in row.detail and "waiting for it" not in row.detail


def test_the_support_row_still_counts_a_stack_hold_in_a_repo_that_exists(tmp_path) -> None:
    """The control for the test above: the same record on a directory that is there."""
    pdf_stack.record_skipped([tmp_path / "a.pdf", tmp_path / "b.pdf"], repo=tmp_path)
    (row,) = health._check_pdf_support()
    assert "2 PDF(s)" in row.detail and "waiting for it" in row.detail


def test_the_settings_row_is_silent_for_a_limit_hold_in_a_repo_that_no_longer_exists(_world, tmp_path) -> None:
    """Settings usable, one hold left behind in a deleted repo: the warning's remedy
    (``nx index repo``) cannot clear it, so there is no row."""
    gone = tmp_path / "deleted-repo"
    pdf_stack.record_skipped([gone / "a.pdf"], repo=gone, cause=pdf_stack.SKIP_CAUSE_BOUNDS)
    _write_pdf_settings(_world, mineru_page_timeout_s=60, mineru_page_batch=2)
    assert health._check_pdf_config() == []


def test_the_settings_row_does_not_count_a_deleted_repos_hold_in_its_red_row(_world, tmp_path) -> None:
    gone = tmp_path / "deleted-repo"
    pdf_stack.record_skipped([gone / "a.pdf"], repo=gone, cause=pdf_stack.SKIP_CAUSE_BOUNDS)
    pdf_stack.record_skipped([tmp_path / "b.pdf", tmp_path / "c.pdf"], repo=tmp_path, cause=pdf_stack.SKIP_CAUSE_BOUNDS)
    _write_pdf_settings(_world, mineru_page_timeout_s=100, mineru_page_batch=4)
    (row,) = health._check_pdf_config()
    assert "2 PDF(s)" in row.detail


def test_a_hold_with_no_repo_key_is_never_taken_for_a_deleted_repo(_world) -> None:
    pdf_stack.record_skipped([Path("/anywhere/a.pdf")], repo=None, cause=pdf_stack.SKIP_CAUSE_BOUNDS)
    assert pdf_stack.skipped_count(existing_repos_only=True) == 1


# ── an unreadable config is not the sweep's end ──────────────────────────────


def test_a_config_read_that_raises_something_else_gives_no_settings_row(monkeypatch) -> None:
    """``_check_pdf_config`` answers ``PdfConfigError`` itself and leaves any other failure to the
    config rows: no row, no exception."""

    def boom():
        raise RuntimeError("config.yml unreadable")

    monkeypatch.setattr("nexus.config.get_pdf_config", boom)
    assert health._check_pdf_config() == []


def test_the_sweep_completes_when_get_pdf_config_raises_a_runtime_error(monkeypatch, tmp_path) -> None:
    def boom():
        raise RuntimeError("config.yml unreadable")

    _only_other_checks_stubbed(monkeypatch, "_check_pdf_config")
    monkeypatch.setattr("nexus.config.get_pdf_config", boom)
    monkeypatch.chdir(tmp_path)

    results, _is_local = health.run_health_checks()

    assert not [r for r in results if r.label == "PDF settings"]
