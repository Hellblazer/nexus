# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/check_mcpb_resolve.py: the pre-tag resolve gate for the .mcpb bundle (nexus-2k66t.4.6).

The gate stages the release lock into a scratch copy of ``mcpb/``, pins it with
the launcher's own code, points conexus at the checkout or a wheel and runs
``uv lock``. These tests run it against a fake ``uv`` (no network) to pin what
the gate sends to uv and what it concludes from the lock that comes back; the
real resolve is the release workflow's step and the live run recorded in the bead.
"""
from __future__ import annotations

import re

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
pyproject = (bundle / "pyproject.toml").read_text()
sent = {{"argv": args, "pyproject": pyproject, "constraints": (bundle / "constraints.txt").read_text()}}
pathlib.Path(cfg["record"]).write_text(json.dumps(sent))
calls = pathlib.Path(cfg["record"] + ".all")
calls.write_text(json.dumps(json.loads(calls.read_text()) + [sent] if calls.exists() else [sent]))
pdf = '"conexus[pdf]' in pyproject
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
    if pdf and name in cfg.get("drop_in_pdf", ()):
        continue
    if not pdf and name in ("docling-slim", "mineru", "torch") and not cfg.get("stack_in_bare"):
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


def _calls(record: Path) -> list[dict]:
    return json.loads(Path(str(record) + ".all").read_text(encoding="utf-8"))


def _conexus_reqs(pyproject: str) -> list[str]:
    return [d for d in tomllib.loads(pyproject)["project"]["dependencies"] if d.startswith("conexus")]


def test_both_variants_are_locked_bare_then_with_the_pdf_setting_and_both_are_named(tmp_path: Path) -> None:
    # nexus-dzc61: the extension starts bare or, with include_pdf on, as conexus[pdf]; the gate resolves both
    proc, record = _gate(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = _calls(record)
    assert [c["argv"][0] for c in calls] == ["lock", "lock"]
    bare, pdf = (_conexus_reqs(c["pyproject"]) for c in calls)
    assert len(bare) == 1 and bare[0].startswith("conexus>=")
    assert pdf == ["conexus[pdf]" + bare[0][len("conexus"):]]
    for c in calls:  # the pdf lock keeps the pin and the source
        doc = tomllib.loads(c["pyproject"])
        assert "conexus==9.9.9" in doc["tool"]["uv"]["constraint-dependencies"]
        assert Path(doc["tool"]["uv"]["sources"]["conexus"]["path"]) == REPO_ROOT
    verdict = next(ln for ln in proc.stdout.splitlines() if ln.startswith("MCPB RESOLVE GATE PASSED"))
    assert "bare: " in verdict and "pdf: " in verdict and verdict.count("pins held") == 2


@pytest.mark.parametrize("missing", ["torch", "mineru", "docling-slim"])
def test_a_pdf_lock_without_the_pdf_stack_fails(tmp_path: Path, missing: str) -> None:
    proc, _ = _gate(tmp_path, drop_in_pdf=[missing])
    assert proc.returncode == 1 and "pdf: the PDF stack is not in the lock" in proc.stdout and missing in proc.stdout


def test_a_bare_lock_that_carries_the_pdf_stack_fails(tmp_path: Path) -> None:
    proc, _ = _gate(tmp_path, stack_in_bare=True)
    assert proc.returncode == 1 and "bare: the bare bundle locks the PDF stack" in proc.stdout


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


# ── the PR-time run (nexus-zzkko) ────────────────────────────────────────────

#: The inputs of the resolve: a change to any of them can make the staged lock
#: stop resolving, and each must keep the job's filter pointed at a real file.
_FILTER_PATHS = (
    "src/nexus/_install/constraints.txt",
    "uv.lock",
    "pyproject.toml",
    "mcpb/pyproject.toml",
    "mcpb/manifest.json",
    "mcpb/src/bootstrap.py",
    "scripts/check_mcpb_resolve.py",
    "scripts/stage_mcpb_constraints.py",
)


def _ci_job() -> dict:
    import yaml

    doc = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    jobs = [j for j in doc["jobs"].values() if "scripts/check_mcpb_resolve.py" in json.dumps(j)]
    assert len(jobs) == 1, "exactly one ci.yml job must run scripts/check_mcpb_resolve.py"
    return jobs[0]


def _step(job: dict, needle: str) -> dict:
    """The one step whose `run` or `uses` mentions the needle (the filter's path list does not count)."""
    hits = [s for s in job["steps"] if needle in s.get("run", "") + s.get("uses", "") + s.get("name", "")]
    assert len(hits) == 1, f"expected exactly one step mentioning {needle!r}, got {len(hits)}"
    return hits[0]


def test_ci_runs_the_gate_on_prs_and_develop_pushes() -> None:
    import yaml

    doc = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    on = doc.get("on", doc.get(True))
    assert on["pull_request"]["branches"] == ["main", "develop"]
    assert on["push"]["branches"] == ["develop"]
    job = _ci_job()
    # Always runs so a check reports on every PR; the paths filter below decides whether it works.
    assert "if" not in job, "a job-level `if` would leave the check unreported on unrelated PRs"
    assert job["timeout-minutes"] <= 15


def test_ci_gate_resolves_the_checkout_not_a_wheel() -> None:
    run = _step(_ci_job(), "scripts/check_mcpb_resolve.py")
    assert "--conexus" not in run["run"], "the PR-time gate resolves the checkout (the default), not a wheel"
    assert run["if"].strip() == "steps.changes.outputs.mcpb == 'true'"


def test_ci_gate_is_path_filtered_on_every_input_of_the_resolve() -> None:
    import yaml

    filt = _step(_ci_job(), "dorny/paths-filter")
    assert filt["uses"].startswith("dorny/paths-filter@") and len(filt["uses"].split("@")[1].split()[0]) == 40
    listed = yaml.safe_load(filt["with"]["filters"])["mcpb"]
    assert tuple(listed) == _FILTER_PATHS
    for path in _FILTER_PATHS:
        assert (REPO_ROOT / path).is_file(), f"{path} is in the filter but is not a file: the filter would never fire"


def test_ci_gate_fails_loud_on_an_unusable_filter_output() -> None:
    """The non-vacuity assert: an empty or malformed `mcpb` output must fail the job, never skip the resolve."""
    job = _ci_job()
    check = _step(job, "produced no usable mcpb output")
    assert "true|false" in check["run"] and "exit 1" in check["run"]
    assert "if" not in check
    steps = job["steps"]
    assert steps.index(_step(job, "dorny/paths-filter")) < steps.index(check) < steps.index(_step(job, "scripts/check_mcpb_resolve.py"))


def test_every_checkout_in_ci_is_pinned_to_a_sha() -> None:
    """A floating actions/checkout tag is a supply-chain hole the rest of ci.yml closed; the
    mcpb and write-seam gates had copied one (nexus-zzkko review)."""
    text = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    refs = re.findall(r"uses:\s*actions/checkout@(\S+)", text)
    assert refs, "ci.yml names no checkout step"
    floating = [r for r in refs if not re.fullmatch(r"[0-9a-f]{40}", r)]
    assert not floating, f"floating checkout refs in ci.yml: {floating}"


def test_the_resolve_gate_blocks_a_pr_into_main_through_pytest_gate() -> None:
    """Sam, 2026-10-24 (nexus-zzkko): the required fan-in waits on the job and, on a PR into
    main (the release PR), fails on anything but success; elsewhere it only warns."""
    import yaml

    doc = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    gate = doc["jobs"]["pytest-gate"]
    assert "mcpb-resolve-gate" in gate["needs"]
    script = "\n".join(step.get("run", "") for step in gate["steps"])
    assert 'mcpb_result="${{ needs.mcpb-resolve-gate.result }}"' in script
    block = script[script.index("mcpb-resolve-gate result"):]
    main_branch = block[: block.index("elif")]
    assert '"$base_ref" = "main"' in main_branch and '"$mcpb_result" != "success"' in main_branch
    assert "exit 1" in main_branch, "a failed resolve must block the release PR"
    advisory = block[block.index("elif"): block.index("fi", block.index("elif"))]
    assert "::warning::" in advisory and "exit 1" not in advisory, "off the release PR it only warns"
