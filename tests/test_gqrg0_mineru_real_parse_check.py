# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor --check-mineru drives a real one-page parse (nexus-gqrg0, GH #1533).

The bare import check (``from mineru.cli.common import do_parse``) proved
only that ``mineru.cli.common`` resolves — not that the installed mineru and
its transitive dependencies still agree. A fresh resolve could install
pdftext 0.7.1, whose ``PageChars`` dropped ``__iter__`` while mineru's own
``span_pre_proc.py:60`` still iterates ``page_chars['chars']`` as a list:
every import succeeded, ``nx doctor --check-mineru`` reported healthy, and
every real ``nx index pdf`` on a formula PDF still raised ``TypeError:
'PageChars' object is not iterable``.

This suite pins:
  * the import-only branches (import raises / ``do_parse is None``) are
    unchanged -- they still return before the real-parse probe runs;
  * the real-parse probe FAILs and names the exception when the parse
    raises;
  * the real-parse probe reports OK when the parse succeeds;
  * an unavailable fixture or unconfigured model weights render as a
    skip, not a FAIL -- an operator who never ran `mineru-models-download`
    should not see a red X for a download this check never asked for.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture()
def _fixture_and_model_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests below want the real-parse probe to actually run --
    force both gates open with a fake path so the FAIL/OK cases don't
    depend on this box having the fixture PDF or downloaded model
    weights. Not autouse: the non-vacuity test at the bottom deliberately
    exercises the real resolver against this checkout's real fixture."""
    import nexus.commands.doctor as doctor

    monkeypatch.setattr(doctor, "_mineru_doctor_fixture_path", lambda: Path("/fake/bft-to-smr.pdf"))
    monkeypatch.setattr(doctor, "_mineru_pipeline_model_dir_configured", lambda: True)


@pytest.mark.usefixtures("_fixture_and_model_present")
def test_import_failure_is_unchanged_and_never_reaches_the_parse_probe(monkeypatch, capsys):
    """A broken mineru import must still short-circuit before the
    real-parse probe -- there is nothing to parse with."""
    from nexus.commands.doctor import _run_check_mineru

    monkeypatch.setitem(sys.modules, "mineru.cli.common", None)
    _run_check_mineru()
    out = capsys.readouterr().out
    assert "MinerU import" in out
    assert "✗" in out
    assert "MinerU parse" not in out


@pytest.mark.usefixtures("_fixture_and_model_present")
def test_do_parse_none_is_unchanged_and_never_reaches_the_parse_probe(monkeypatch, capsys):
    """A broken import shim (``do_parse is None``) must still
    short-circuit before the real-parse probe."""
    from mineru.cli import common as mineru_common
    from nexus.commands.doctor import _run_check_mineru

    monkeypatch.setattr(mineru_common, "do_parse", None)
    _run_check_mineru()
    out = capsys.readouterr().out
    assert "MinerU import" in out
    assert "do_parse is None" in out
    assert "MinerU parse" not in out


@pytest.mark.usefixtures("_fixture_and_model_present")
def test_real_parse_failure_is_reported_as_fail_naming_the_exception(monkeypatch, capsys):
    """The exact GH #1533 shape: mineru imports cleanly, a real parse
    raises TypeError. Must render as a FAIL naming the exception class
    and message -- not a silent pass."""
    import nexus.commands.doctor as doctor
    from nexus.commands.doctor import _run_check_mineru

    def _raise(*_a, **_kw):
        raise TypeError("'PageChars' object is not iterable")

    monkeypatch.setattr(doctor, "_mineru_parse_fixture_once", _raise)
    _run_check_mineru()
    out = capsys.readouterr().out
    assert "MinerU import" in out
    parse_line = next(ln for ln in out.splitlines() if "MinerU parse" in ln)
    assert "✗" in parse_line
    assert "TypeError" in parse_line
    assert "'PageChars' object is not iterable" in parse_line


@pytest.mark.usefixtures("_fixture_and_model_present")
def test_real_parse_success_is_reported_as_ok(monkeypatch, capsys):
    import nexus.commands.doctor as doctor
    from nexus.commands.doctor import _run_check_mineru

    monkeypatch.setattr(doctor, "_mineru_parse_fixture_once", lambda *a, **kw: None)
    _run_check_mineru()
    out = capsys.readouterr().out
    parse_line = next(ln for ln in out.splitlines() if "MinerU parse" in ln)
    assert "✓" in parse_line
    assert "✗" not in parse_line


@pytest.mark.usefixtures("_fixture_and_model_present")
def test_real_parse_timeout_is_reported_as_fail(monkeypatch, capsys):
    import subprocess

    import nexus.commands.doctor as doctor
    from nexus.commands.doctor import _run_check_mineru

    def _raise_timeout(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd=["mineru"], timeout=180)

    monkeypatch.setattr(doctor, "_mineru_parse_fixture_once", _raise_timeout)
    _run_check_mineru()
    out = capsys.readouterr().out
    assert "MinerU parse" in out
    assert "timed out" in out


def test_missing_fixture_skips_the_parse_probe_without_failing(monkeypatch, capsys):
    """Outside a dev checkout there is no tests/fixtures/ to draw from --
    that must render as an informational skip, never a FAIL."""
    import nexus.commands.doctor as doctor
    from nexus.commands.doctor import _run_check_mineru

    monkeypatch.setattr(doctor, "_mineru_doctor_fixture_path", lambda: None)
    _run_check_mineru()
    out = capsys.readouterr().out
    assert "MinerU parse" not in out
    assert "skipped" in out
    assert "✗" not in out


def test_missing_model_weights_skips_the_parse_probe_without_failing(monkeypatch, capsys):
    """A box that never ran mineru-models-download must not see a FAIL
    for a download this check never asked for."""
    import nexus.commands.doctor as doctor
    from nexus.commands.doctor import _run_check_mineru

    monkeypatch.setattr(doctor, "_mineru_pipeline_model_dir_configured", lambda: False)
    _run_check_mineru()
    out = capsys.readouterr().out
    assert "MinerU parse" not in out
    assert "skipped" in out
    assert "mineru-models-download" in out
    assert "✗" not in out


def test_fixture_path_resolves_inside_this_checkout():
    """Non-vacuity: the resolver must actually find the real fixture in
    THIS checkout (a dev/CI environment), proving the walk-up logic
    works against the real repo layout, not just a monkeypatched stub."""
    from nexus.commands.doctor import _mineru_doctor_fixture_path

    fixture = _mineru_doctor_fixture_path()
    assert fixture is not None, "tests/fixtures/bft-to-smr.pdf not found by the walk-up resolver"
    assert fixture.name == "bft-to-smr.pdf"
    assert fixture.is_file()
