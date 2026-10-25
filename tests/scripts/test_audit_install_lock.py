# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The weekly install-lock audit's verdict (nexus-2k66t.4.5): red only for an
advisory that a lock refresh would clear and nobody acknowledged."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audit_install_lock.py"


def _load():
    spec = importlib.util.spec_from_file_location("audit_install_lock", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_install_lock"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop("audit_install_lock", None)
    return module


def _report(*deps: dict, clean: int = 60) -> dict:
    filler = [{"name": f"pkg{i}", "version": "1.0", "vulns": []} for i in range(clean)]
    return {"dependencies": [*deps, *filler]}


def _vuln(vid: str, *fixes: str) -> dict:
    return {"id": vid, "fix_versions": list(fixes), "aliases": [], "description": ""}


def _run(tmp_path: Path, report: dict, capsys) -> tuple[int, str]:
    path = tmp_path / "audit.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    rc = _load().main(["audit_install_lock.py", str(path)])
    out = capsys.readouterr()
    return rc, out.out + out.err


def test_a_clean_lock_passes(tmp_path, capsys) -> None:
    rc, out = _run(tmp_path, _report(), capsys)
    assert rc == 0, out
    assert "60 packages examined, 0 with advisories, 0 advisories" in out


def test_an_advisory_with_a_fix_fails_and_names_the_package_and_the_fixed_version(tmp_path, capsys) -> None:
    dep = {"name": "aiohttp", "version": "3.13.3", "vulns": [_vuln("PYSEC-1", "3.13.4"), _vuln("PYSEC-2", "3.14.4")]}
    rc, out = _run(tmp_path, _report(dep), capsys)
    assert rc == 1, out
    assert "FIX aiohttp 3.13.3: 2 advisories, fixed by 3.14.4; open 2" in out
    assert "refresh uv.lock" in out


def test_an_advisory_without_a_fix_is_shown_and_never_fails(tmp_path, capsys) -> None:
    dep = {"name": "accelerate", "version": "1.13.0", "vulns": [_vuln("PYSEC-3")]}
    rc, out = _run(tmp_path, _report(dep), capsys)
    assert rc == 0, out
    assert "ok  accelerate 1.13.0: 1 advisories, fixed by -; open 0, acknowledged 0, no fix 1" in out


def test_an_acknowledged_advisory_shows_its_reason_and_does_not_fail(tmp_path, capsys, monkeypatch) -> None:
    module = _load()
    monkeypatch.setattr(module, "ACKNOWLEDGED", {"PYSEC-4": "needs torch 2.9; MinerU pins 2.8 (nexus-mt1tj)"})
    path = tmp_path / "audit.json"
    path.write_text(json.dumps(_report({"name": "torch", "version": "2.8.0", "vulns": [_vuln("PYSEC-4", "2.9.0")]})))
    assert module.main(["x", str(path)]) == 0
    assert "acknowledged PYSEC-4: needs torch 2.9" in capsys.readouterr().out


def test_duplicate_advisory_rows_count_once(tmp_path, capsys) -> None:
    """pip-audit repeats an advisory once per alias source; the count is by id."""
    dep = {"name": "idna", "version": "3.11", "vulns": [_vuln("PYSEC-5", "3.15"), _vuln("PYSEC-5", "3.15")]}
    rc, out = _run(tmp_path, _report(dep), capsys)
    assert rc == 1 and "idna 3.11: 1 advisories" in out


@pytest.mark.parametrize("report", [
    pytest.param({"dependencies": []}, id="nothing-examined"),
    pytest.param(_report(clean=3), id="far-fewer-packages-than-the-lock"),
])
def test_a_vacuous_audit_fails(tmp_path, capsys, report) -> None:
    rc, out = _run(tmp_path, report, capsys)
    assert rc == 2, out
    assert "packages examined (expected at least" in out


def test_an_unreadable_report_fails(tmp_path, capsys) -> None:
    path = tmp_path / "audit.json"
    path.write_text("not json", encoding="utf-8")
    assert _load().main(["x", str(path)]) == 2
    assert "could not read the pip-audit report" in capsys.readouterr().err


def test_every_acknowledgement_has_a_reason() -> None:
    for vid, reason in _load().ACKNOWLEDGED.items():
        assert reason.strip(), f"ACKNOWLEDGED[{vid!r}] has no reason"


def test_a_version_acknowledgement_holds_only_at_that_exact_version(tmp_path, capsys, monkeypatch) -> None:
    module = _load()
    monkeypatch.setattr(module, "ACKNOWLEDGED_AT_VERSION", {"pillow": ("12.3.0", "capped <13 upstream")})
    at = _report({"name": "pillow", "version": "12.3.0", "vulns": [_vuln("PYSEC-9", "13.0.0")]})
    moved = _report({"name": "pillow", "version": "12.4.0", "vulns": [_vuln("PYSEC-9", "13.0.0")]})
    for report, expected in ((at, 0), (moved, 1)):
        path = tmp_path / "audit.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        assert module.main(["x", str(path)]) == expected
    assert "acknowledged at 12.3.0: capped <13 upstream" in capsys.readouterr().out


def test_every_version_acknowledgement_names_a_version_and_a_reason() -> None:
    for name, (version, reason) in _load().ACKNOWLEDGED_AT_VERSION.items():
        assert version.strip() and reason.strip(), name
