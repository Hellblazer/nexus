# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/check_mcpb_resolve.py: the pre-tag resolve gate for the .mcpb bundle (nexus-2k66t.4.6).

The gate stages the release lock into a scratch copy of ``mcpb/``, pins it with
the launcher's own code, points conexus at the checkout or a wheel and runs
``uv lock``. These tests run it against a fake ``uv`` (no network) to pin what
the gate sends to uv and what it concludes from the lock that comes back; the
real resolve is the release workflow's step and the live run recorded in the bead.
"""
from __future__ import annotations

import json
import stat
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE = REPO_ROOT / "scripts" / "check_mcpb_resolve.py"

_FAKE_UV = '''#!{python}
import json, pathlib, sys
cfg = json.loads(pathlib.Path({cfg!r}).read_text())
args = sys.argv[1:]
bundle = pathlib.Path(args[args.index("--directory") + 1])
pathlib.Path(cfg["record"]).write_text(json.dumps({{
    "argv": args,
    "pyproject": (bundle / "pyproject.toml").read_text(),
    "constraints": (bundle / "constraints.txt").read_text(),
}}))
if cfg.get("exit"):
    print("error: No solution found when resolving dependencies", file=sys.stderr)
    sys.exit(cfg["exit"])
pins = [l.strip() for l in (bundle / "constraints.txt").read_text().splitlines() if l.strip() and not l.startswith("#")]
rows = []
for pin in pins[: cfg.get("limit", 10**9)]:
    name, version = pin.split("==", 1)
    if ";" in version:
        version = version.split(";")[0].strip()
    if name in cfg.get("move", {{}}):
        version = cfg["move"][name]
    if name == "conexus" and cfg.get("drop_conexus"):
        continue
    rows.append((name, version))
(bundle / "uv.lock").write_text("version = 1\\n" + "".join(
    '[[package]]\\nname = "%s"\\nversion = "%s"\\n\\n' % row for row in rows))
'''


def _fake_uv(tmp: Path, **cfg) -> tuple[Path, Path]:
    record = tmp / "record.json"
    cfg_path = tmp / "cfg.json"
    cfg_path.write_text(json.dumps({"record": str(record), **cfg}), encoding="utf-8")
    exe = tmp / "fake-uv"
    exe.write_text(_FAKE_UV.format(python=sys.executable, cfg=str(cfg_path)), encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe, record


def _gate(tmp: Path, *extra: str, **cfg):
    exe, record = _fake_uv(tmp, **cfg)
    proc = subprocess.run(
        [sys.executable, "-I", str(GATE), "--uv", str(exe), "--conexus-version", "9.9.9", *extra],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    return proc, record


def test_a_lock_that_resolves_passes_and_the_verdict_names_what_it_examined(tmp_path: Path) -> None:
    proc, record = _gate(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    verdict = [ln for ln in proc.stdout.splitlines() if ln.startswith("MCPB RESOLVE GATE PASSED")]
    assert len(verdict) == 1 and "conexus 9.9.9" in verdict[0] and "pins held" in verdict[0]
    sent = json.loads(record.read_text(encoding="utf-8"))
    # uv saw the pinned bundle: the launcher's block, the release pin and conexus pointed at the checkout.
    doc = tomllib.loads(sent["pyproject"])
    assert "conexus==9.9.9" in doc["tool"]["uv"]["constraint-dependencies"]
    assert len(doc["tool"]["uv"]["constraint-dependencies"]) >= 200
    assert Path(doc["tool"]["uv"]["sources"]["conexus"]["path"]) == REPO_ROOT
    assert doc["tool"]["uv"]["override-dependencies"], "the bundle's own overrides must reach the resolve"
    assert sent["argv"][0] == "lock" and "--no-config" in sent["argv"]


def test_a_wheel_is_resolved_instead_of_the_checkout(tmp_path: Path) -> None:
    wheel = tmp_path / "conexus-9.9.9-py3-none-any.whl"
    wheel.write_bytes(b"")
    proc, record = _gate(tmp_path, "--conexus", str(wheel))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    doc = tomllib.loads(json.loads(record.read_text(encoding="utf-8"))["pyproject"])
    assert Path(doc["tool"]["uv"]["sources"]["conexus"]["path"]) == wheel.resolve()


def test_the_checkout_itself_is_not_edited(tmp_path: Path) -> None:
    staged = REPO_ROOT / "mcpb" / "constraints.txt"
    existed = staged.exists()
    before = (REPO_ROOT / "mcpb" / "pyproject.toml").read_bytes()
    _gate(tmp_path)
    assert (REPO_ROOT / "mcpb" / "pyproject.toml").read_bytes() == before
    assert staged.exists() == existed, "the gate stages into a scratch copy, never into mcpb/"


def test_a_resolve_failure_fails_the_gate_and_shows_uvs_output(tmp_path: Path) -> None:
    proc, _ = _gate(tmp_path, exit=2)
    assert proc.returncode == 1
    assert "MCPB RESOLVE GATE FAILED" in proc.stdout and "PASSED" not in proc.stdout
    assert "No solution found" in proc.stdout + proc.stderr


def test_a_pin_that_moved_in_the_lock_fails(tmp_path: Path) -> None:
    pins = (REPO_ROOT / "src" / "nexus" / "_install" / "constraints.txt").read_text(encoding="utf-8")
    victim = next(ln.split("==")[0] for ln in pins.splitlines() if "==" in ln and ";" not in ln and not ln.startswith("#"))
    proc, _ = _gate(tmp_path, move={victim: "0.0.0"})
    assert proc.returncode == 1 and "not honoured" in proc.stdout


def test_a_lock_without_conexus_at_the_release_fails(tmp_path: Path) -> None:
    proc, _ = _gate(tmp_path, drop_conexus=True)
    assert proc.returncode == 1 and "conexus is not locked" in proc.stdout


def test_a_near_empty_lock_fails_instead_of_passing_over_nothing(tmp_path: Path) -> None:
    proc, _ = _gate(tmp_path, limit=5)
    assert proc.returncode == 1 and "FAILED" in proc.stdout


def test_a_missing_uv_is_a_failure_not_a_skip(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-I", str(GATE), "--uv", str(tmp_path / "no-such-uv"), "--conexus-version", "9.9.9"],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 1 and "MCPB RESOLVE GATE FAILED" in proc.stdout


def test_the_release_workflow_runs_the_gate_before_publishing() -> None:
    text = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    step = text[text.index("- name: Pack .mcpb Desktop Extension") : text.index("- name: Publish to PyPI")]
    assert "scripts/check_mcpb_resolve.py" in step
    assert step.index("scripts/check_mcpb_resolve.py") < step.index("mcpb pack . conexus.mcpb")
    # The freshly built wheel is what is resolved: the release is not on the index yet.
    assert "--conexus" in step and "dist/conexus-" in step
    assert text.index("- name: Build wheel and sdist") < text.index("- name: Pack .mcpb Desktop Extension")
