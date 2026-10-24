# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Desktop extension's opt-in PDF support (nexus-dzc61, Sam's ruling of 2026-10-24).

The bundle ships bare ``conexus`` (tests/test_plugin_structure.py pins it). Its
``include_pdf`` setting, off by default, reaches the launcher as
``NX_MCPB_INCLUDE_PDF``; when on, ``mcpb/src/bootstrap.py`` rewrites the bundle's
one conexus requirement to ``conexus[pdf]`` before the sync, and when off it
writes it back, so the exact ``uv sync`` removes the PDF stack again. The extra is
not declared as an optional dependency of the bundle: ``uv sync`` locks every
declared extra on every start, so a declared ``pdf`` extra would make every
Desktop start resolve torch whatever the setting says. A sync that fails with PDF
support on reverts to bare and syncs once more: the setting never takes the
extension down.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MCPB = REPO_ROOT / "mcpb"


@pytest.fixture(scope="module")
def bootstrap():
    spec = importlib.util.spec_from_file_location("mcpb_bootstrap_pdf", MCPB / "src" / "bootstrap.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    """A throwaway copy of the bundle's pyproject, never the repo's own."""
    root = tmp_path / "bundle"
    (root / "src").mkdir(parents=True)
    shutil.copy2(MCPB / "pyproject.toml", root / "pyproject.toml")
    return root


def _conexus_reqs(root: Path) -> list[str]:
    with (root / "pyproject.toml").open("rb") as f:
        deps = tomllib.load(f)["project"]["dependencies"]
    return [d for d in deps if d.lower().startswith("conexus")]


def test_the_manifest_carries_include_pdf_as_an_off_by_default_boolean_setting() -> None:
    manifest = json.loads((MCPB / "manifest.json").read_text(encoding="utf-8"))
    setting = manifest["user_config"]["include_pdf"]
    assert setting["type"] == "boolean" and setting["default"] is False and setting["required"] is False
    assert "PDF" in setting["title"] and "next time the extension starts" in setting["description"]
    env = manifest["server"]["mcp_config"]["env"]
    assert env["NX_MCPB_INCLUDE_PDF"] == "${user_config.include_pdf}"


def test_the_bundle_declares_no_pdf_extra_so_a_bare_start_never_resolves_it() -> None:
    with (MCPB / "pyproject.toml").open("rb") as f:
        project = tomllib.load(f)["project"]
    assert "optional-dependencies" not in project
    reqs = [d for d in project["dependencies"] if d.lower().startswith("conexus")]
    assert len(reqs) == 1 and reqs[0].startswith("conexus>=")


@pytest.mark.parametrize(
    "value, on",
    [("true", True), ("True", True), ("1", True), ("false", False), ("0", False), ("", False),
     ("${user_config.include_pdf}", False), (None, False)],
)
def test_the_setting_value_decides_the_requirement(bootstrap, bundle: Path, value, on: bool) -> None:
    floor = _conexus_reqs(bundle)[0][len("conexus"):]
    environ = {} if value is None else {"NX_MCPB_INCLUDE_PDF": value}
    in_force, line = bootstrap._apply_pdf(str(bundle), environ=environ)
    assert in_force is on
    assert _conexus_reqs(bundle) == [("conexus[pdf]" if on else "conexus") + floor]
    if on:
        assert "PDF support is on" in line


def test_turning_it_off_writes_the_bare_requirement_back_and_says_the_sync_removes_it(bootstrap, bundle: Path) -> None:
    original = (bundle / "pyproject.toml").read_bytes()
    bootstrap._apply_pdf(str(bundle), environ={"NX_MCPB_INCLUDE_PDF": "true"})
    assert (bundle / "pyproject.toml").read_bytes() != original
    in_force, line = bootstrap._apply_pdf(str(bundle), environ={"NX_MCPB_INCLUDE_PDF": "false"})
    assert in_force is False and "off" in line and "removes" in line
    assert (bundle / "pyproject.toml").read_bytes() == original  # byte-identical, comments and all
    # off and already bare: nothing written, nothing said
    assert bootstrap._apply_pdf(str(bundle), environ={}) == (False, "")
    assert (bundle / "pyproject.toml").read_bytes() == original


def test_it_composes_with_the_lock_pin_in_either_order(bootstrap, bundle: Path) -> None:
    (bundle / "constraints.txt").write_text("conexus==7.85.0\nidna==3.10\n", encoding="utf-8")
    bootstrap._apply_pin(str(bundle), environ={})
    bootstrap._apply_pdf(str(bundle), environ={"NX_MCPB_INCLUDE_PDF": "true"})
    bootstrap._apply_pin(str(bundle), environ={})
    with (bundle / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)
    assert data["tool"]["uv"]["constraint-dependencies"] == ["conexus==7.85.0", "idna==3.10"]
    assert _conexus_reqs(bundle)[0].startswith("conexus[pdf]>=")


def test_a_pyproject_without_exactly_one_conexus_requirement_is_left_alone(bootstrap, bundle: Path) -> None:
    path = bundle / "pyproject.toml"
    text = path.read_text(encoding="utf-8")
    req = _conexus_reqs(bundle)[0]
    doubled = text.replace(f'"{req}",', f'"{req}",\n    "{req}",')
    assert doubled != text
    path.write_text(doubled, encoding="utf-8")
    in_force, line = bootstrap._apply_pdf(str(bundle), environ={"NX_MCPB_INCLUDE_PDF": "true"})
    assert in_force is False and "WARNING" in line and "not applied" in line
    assert path.read_text(encoding="utf-8") == doubled
    path.write_text("not = [toml", encoding="utf-8")
    assert bootstrap._apply_pdf(str(bundle), environ={"NX_MCPB_INCLUDE_PDF": "true"})[0] is False
    assert path.read_text(encoding="utf-8") == "not = [toml"


def _main(bootstrap, monkeypatch, bundle: Path, results: list[int], environ: dict) -> tuple[list, list]:
    """Run main() on POSIX with the sync and the launch faked; return (sync calls' pdf state, launch argv)."""
    syncs: list = []
    launched: list = []
    monkeypatch.setattr(bootstrap, "_bundle_dir", lambda: str(bundle))
    for k in ("NX_MCPB_SKIP_RESOLVE_RETRY", "NX_INSTALL_UNPINNED", "NX_MCPB_INSTALL_UNPINNED", "NX_MCPB_INCLUDE_PDF"):
        monkeypatch.delenv(k, raising=False)
    for k, v in environ.items():
        monkeypatch.setenv(k, v)

    def run(cmd, **kw):
        syncs.append(_conexus_reqs(bundle)[0].startswith("conexus[pdf]"))
        rc = results.pop(0)
        return SimpleNamespace(returncode=rc, stdout="", stderr="" if rc == 0 else "error: network unreachable")

    real = bootstrap._sync_with_retry
    monkeypatch.setattr(bootstrap, "_sync_with_retry", lambda d, **kw: real(d, run=run, sleep=lambda s: None))
    monkeypatch.setattr(bootstrap, "_launch", lambda argv, platform=None: launched.append(argv))
    bootstrap.main(platform="linux")
    return syncs, launched


def test_main_syncs_with_pdf_when_the_setting_is_on(bootstrap, monkeypatch, bundle: Path) -> None:
    syncs, launched = _main(bootstrap, monkeypatch, bundle, [0], {"NX_MCPB_INCLUDE_PDF": "true"})
    assert syncs == [True] and launched


def test_a_failed_sync_with_pdf_reverts_to_bare_and_starts(bootstrap, monkeypatch, bundle: Path, capsys) -> None:
    syncs, launched = _main(bootstrap, monkeypatch, bundle, [1, 0], {"NX_MCPB_INCLUDE_PDF": "true"})
    assert syncs == [True, False] and launched
    assert _conexus_reqs(bundle)[0].startswith("conexus>=")
    assert "without PDF support" in capsys.readouterr().err


def test_the_bare_retry_after_a_pdf_failure_runs_under_the_release_lock_again(bootstrap, monkeypatch, bundle: Path) -> None:
    # pdf+pinned fails, the pinned sync's own fallback strips the lock and pdf+unpinned fails too: the bare retry
    # must re-apply the lock, not inherit the strip a PDF-only failure caused
    (bundle / "constraints.txt").write_text("conexus==7.85.0\nidna==3.10\n", encoding="utf-8")
    pinned: list[bool] = []
    real = bootstrap._sync_with_retry

    def sync(d, **kw):
        def run(cmd, **k):
            with (bundle / "pyproject.toml").open("rb") as f:
                pinned.append(bool(tomllib.load(f)["tool"]["uv"].get("constraint-dependencies")))
            rc = 0 if not _conexus_reqs(bundle)[0].startswith("conexus[pdf]") else 1
            return SimpleNamespace(returncode=rc, stdout="", stderr="" if rc == 0 else "error: no wheel for this platform")
        return real(d, run=run, sleep=lambda s: None)

    monkeypatch.setattr(bootstrap, "_bundle_dir", lambda: str(bundle))
    for k in ("NX_MCPB_SKIP_RESOLVE_RETRY", "NX_INSTALL_UNPINNED", "NX_MCPB_INSTALL_UNPINNED"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NX_MCPB_INCLUDE_PDF", "true")
    monkeypatch.setattr(bootstrap, "_sync_with_retry", sync)
    monkeypatch.setattr(bootstrap, "_launch", lambda argv, platform=None: None)
    bootstrap.main(platform="linux")
    assert pinned == [True, False, True]


def test_a_propagation_window_that_outlasts_the_retries_is_not_waited_out_twice(bootstrap, monkeypatch, bundle: Path) -> None:
    calls: list[int] = []
    monkeypatch.setattr(bootstrap, "_bundle_dir", lambda: str(bundle))
    for k in ("NX_MCPB_SKIP_RESOLVE_RETRY", "NX_INSTALL_UNPINNED", "NX_MCPB_INSTALL_UNPINNED"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NX_MCPB_INCLUDE_PDF", "true")
    real = bootstrap._sync_with_retry

    def run(cmd, **k):
        calls.append(1)
        return SimpleNamespace(returncode=1, stdout="",
                               stderr="error: No solution found when resolving dependencies:\n"
                                      "  Because there is no version of conexus==9.9.9 and your project depends on it")

    monkeypatch.setattr(bootstrap, "_sync_with_retry", lambda d, **kw: real(d, run=run, sleep=lambda s: None))
    monkeypatch.setattr(bootstrap, "_launch", lambda argv, platform=None: None)
    with pytest.raises(SystemExit):
        bootstrap.main(platform="linux")
    assert len(calls) == len(bootstrap._RETRY_SLEEPS) + 1  # one schedule, not two
    assert _conexus_reqs(bundle)[0].startswith("conexus[pdf]")  # the setting was not what failed


def test_a_failed_bare_sync_still_fails_the_start(bootstrap, monkeypatch, bundle: Path) -> None:
    with pytest.raises(SystemExit):
        _main(bootstrap, monkeypatch, bundle, [1], {})


def test_the_skip_env_leaves_pdf_as_it_was(bootstrap, monkeypatch, bundle: Path) -> None:
    # NX_MCPB_SKIP_RESOLVE_RETRY: no sync, so no fallback; the requirement is not touched either way
    before = (bundle / "pyproject.toml").read_bytes()
    monkeypatch.setattr(bootstrap, "_bundle_dir", lambda: str(bundle))
    monkeypatch.setenv("NX_MCPB_SKIP_RESOLVE_RETRY", "1")
    monkeypatch.setenv("NX_MCPB_INCLUDE_PDF", "true")
    monkeypatch.setattr(bootstrap, "_launch", lambda argv, platform=None: None)
    bootstrap.main(platform="linux")
    assert (bundle / "pyproject.toml").read_bytes() == before


def test_the_desktop_refusal_names_the_settings_title() -> None:
    # tests/test_pdf_stack.py pins the refusal itself; this keeps it naming the title the manifest shows
    manifest = json.loads((MCPB / "manifest.json").read_text(encoding="utf-8"))
    title = manifest["user_config"]["include_pdf"]["title"]
    assert f"{title} setting" in (REPO_ROOT / "src" / "nexus" / "pdf_stack.py").read_text(encoding="utf-8")
    # the server reads the variable the manifest sets and the launcher passes on
    from nexus import pdf_stack

    assert f"${{user_config.include_pdf}}" == manifest["server"]["mcp_config"]["env"][pdf_stack.MCPB_PDF_SETTING_ENV]
    assert pdf_stack.MCPB_PDF_SETTING_ENV == bootstrap_env()


def bootstrap_env() -> str:
    spec = importlib.util.spec_from_file_location("mcpb_bootstrap_pdf_env", MCPB / "src" / "bootstrap.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._PDF_ENV
