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

Round 2 (same bead, GH #1533 reopened): the first real-parse probe
resolved a *committed* ``tests/fixtures/bft-to-smr.pdf`` fixture by
walking up from ``doctor.py``. A published wheel never ships ``tests/``,
so that walk always came back empty on an installed box -- exactly the
box the report was filed from -- and the probe silently downgraded to an
informational skip there, unchanged. The probe now synthesizes its own
one-page PDF via ``reportlab`` (a resolved ``mineru[pipeline]``
transitive) into its own ``TemporaryDirectory``, so it has something to
parse on any install, not just a dev/CI checkout.

This suite pins:
  * the import-only branches (import raises / ``do_parse is None``) are
    unchanged -- they still return before the real-parse probe runs;
  * the real-parse probe FAILs and names the exception when the parse
    raises;
  * the real-parse probe reports OK when the parse succeeds;
  * unconfigured model weights render as a skip, not a FAIL -- an
    operator who never ran `mineru-models-download` should not see a red
    X for a download this check never asked for;
  * the probe script itself -- not a monkeypatched stand-in -- actually
    executes in a real subprocess and surfaces both outcomes, against a
    stub `mineru.cli.common` placed ahead of the real package on the
    CHILD's ``sys.path`` via ``PYTHONPATH`` (read only at interpreter
    startup, so the parent test process's own already-resolved `mineru`
    import is untouched).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture()
def _fixture_and_model_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests below want the real-parse probe to actually run --
    force the model-weights gate open so the FAIL/OK cases don't depend
    on this box having downloaded weights. The probe's own fixture PDF is
    always synthesized now (nexus-gqrg0 round 2), so there is no fixture
    gate left to force open here."""
    import nexus.commands.doctor as doctor

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


def test_probe_pdf_is_synthesized_on_demand_inside_any_directory() -> None:
    """Non-vacuity: ``_synthesize_mineru_probe_pdf`` must actually produce a
    real, non-empty PDF file in an arbitrary directory -- proving the
    fixture no longer depends on a committed ``tests/fixtures/`` file that
    a published wheel would never ship."""
    import tempfile

    from nexus.commands.doctor import _synthesize_mineru_probe_pdf

    with tempfile.TemporaryDirectory() as d:
        pdf_path = _synthesize_mineru_probe_pdf(Path(d))
        assert pdf_path.is_file()
        assert pdf_path.suffix == ".pdf"
        assert pdf_path.stat().st_size > 0
        assert pdf_path.read_bytes().startswith(b"%PDF-")


def _write_stub_mineru_common(base: Path, *, raise_type_error: bool) -> None:
    """Write a stub ``mineru.cli.common`` package under *base*, for
    PYTHONPATH-shadowing the real installed ``mineru`` package inside a
    CHILD subprocess only. ``raise_type_error=True`` reproduces the exact
    GH #1533 shape (``TypeError: 'PageChars' object is not iterable``);
    ``False`` is a clean stand-in for the do_parse success path."""
    pkg = base / "mineru" / "cli"
    pkg.mkdir(parents=True)
    (base / "mineru" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    if raise_type_error:
        body = (
            "def do_parse(*args, **kwargs):\n"
            "    raise TypeError(\"'PageChars' object is not iterable\")\n"
        )
    else:
        body = "def do_parse(*args, **kwargs):\n    return None\n"
    (pkg / "common.py").write_text(body)


def test_probe_script_runs_for_real_and_fails_naming_the_pagechars_typeerror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    """No monkeypatch of ``_mineru_parse_fixture_once``: the real function
    runs, spawns a real subprocess, and that subprocess's
    ``from mineru.cli.common import do_parse`` resolves the stub below
    because ``PYTHONPATH`` is read only at CHILD interpreter startup (the
    parent test process's own, already-cached ``mineru`` import is
    untouched by an env var set mid-run). This exercises
    ``_MINERU_DOCTOR_PARSE_SCRIPT`` end to end, reproducing the exact
    GH #1533 TypeError shape through the full doctor rendering path."""
    from nexus.commands.doctor import _run_check_mineru_parse

    stub_root = tmp_path / "stub"
    _write_stub_mineru_common(stub_root, raise_type_error=True)
    monkeypatch.setenv("PYTHONPATH", str(stub_root))

    _run_check_mineru_parse()
    out = capsys.readouterr().out
    parse_line = next(ln for ln in out.splitlines() if "MinerU parse" in ln)
    assert "✗" in parse_line
    assert "TypeError" in parse_line
    assert "'PageChars' object is not iterable" in parse_line


def test_probe_script_runs_for_real_and_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    """Success-path sibling of the test above: same real subprocess, the
    stub ``do_parse`` returns cleanly."""
    from nexus.commands.doctor import _run_check_mineru_parse

    stub_root = tmp_path / "stub"
    _write_stub_mineru_common(stub_root, raise_type_error=False)
    monkeypatch.setenv("PYTHONPATH", str(stub_root))

    _run_check_mineru_parse()
    out = capsys.readouterr().out
    parse_line = next(ln for ln in out.splitlines() if "MinerU parse" in ln)
    assert "✓" in parse_line
    assert "✗" not in parse_line
