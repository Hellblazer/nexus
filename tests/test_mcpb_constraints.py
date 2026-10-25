# SPDX-License-Identifier: AGPL-3.0-or-later
"""The .mcpb Desktop bundle installs under the release's lock (nexus-2k66t.4.6).

The bundle's ``uv sync`` used to resolve every dependency fresh at install time,
so a bad transitive release reached Desktop users, who are the least able to
recover (the nexus-l2ku5 shape: mcp 2.0.0 killed both MCP servers for four
days). The CLI installers already pass ``src/nexus/_install/constraints.txt``
(generated from ``uv.lock``, pinned equal by tests/test_install_constraints_pin.py)
to ``uv pip install --constraints``. ``uv sync`` has no constraints flag and
ignores ``UV_CONSTRAINT``, so the bundle gets the same lock through the one
channel a project sync reads: ``[tool.uv] constraint-dependencies`` in the
bundle's ``pyproject.toml``.

Two halves, both pinned here:

* the BUILD stages the release's file into the bundle
  (``scripts/stage_mcpb_constraints.py``, run by release.yml before
  ``mcpb pack``), byte-equal to the shipped file, so there is no second
  hand-kept lock;
* the LAUNCHER (``mcpb/src/bootstrap.py``) writes it into ``pyproject.toml``
  before ``uv sync``, honours ``NX_INSTALL_UNPINNED=1`` as the opt-out, and
  degrades to today's unpinned install with a stated line when the file is
  absent or unreadable. It never fails the Desktop start over a pin.
"""
from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MCPB = REPO_ROOT / "mcpb"
RELEASE_CONSTRAINTS = REPO_ROOT / "src" / "nexus" / "_install" / "constraints.txt"
STAGER = REPO_ROOT / "scripts" / "stage_mcpb_constraints.py"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"

#: The lock has 219 pins; the stager's floor is 200.
_FLOOR = 200


def _pins(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


@pytest.fixture(scope="module")
def bootstrap():
    spec = importlib.util.spec_from_file_location("mcpb_bootstrap_pin", MCPB / "src" / "bootstrap.py")
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


def _constraint_deps(root: Path) -> list[str] | None:
    with (root / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)["tool"]["uv"].get("constraint-dependencies")


def _stage(root: Path) -> None:
    shutil.copy2(RELEASE_CONSTRAINTS, root / "constraints.txt")


# ── the build stages the release's file ─────────────────────────────────────


def _run_stager(*args: str):
    return subprocess.run(
        [sys.executable, "-I", str(STAGER), *args],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )


def _manifest_version() -> str:
    import json

    return json.loads((MCPB / "manifest.json").read_text(encoding="utf-8"))["version"]


def _pin_file(path: Path, n: int, extra: str = "") -> Path:
    path.write_text("# header\n" + "".join(f"p{i}==1.{i}\n" for i in range(n)) + extra, encoding="utf-8")
    return path


def test_stager_copies_the_release_constraints_and_pins_the_release_itself(tmp_path: Path) -> None:
    dest = tmp_path / "mcpb"
    dest.mkdir()
    proc = _run_stager("--dest", str(dest), "--conexus-version", "9.9.9")
    assert proc.returncode == 0, proc.stderr
    staged = (dest / "constraints.txt").read_bytes()
    # The release's lock byte for byte, then the one line that names the release itself.
    assert staged.startswith(RELEASE_CONSTRAINTS.read_bytes())
    assert staged.splitlines()[-1] == b"conexus==9.9.9"
    assert len(_pins(staged.decode("utf-8"))) == len(_pins(RELEASE_CONSTRAINTS.read_text(encoding="utf-8"))) + 1
    # Non-vacuity: the run states what it staged.
    assert "pins" in proc.stdout


def test_stager_defaults_the_release_to_the_manifest_version(tmp_path: Path) -> None:
    dest = tmp_path / "mcpb"
    dest.mkdir()
    proc = _run_stager("--dest", str(dest))
    assert proc.returncode == 0, proc.stderr
    assert (dest / "constraints.txt").read_text(encoding="utf-8").splitlines()[-1] == f"conexus=={_manifest_version()}"


@pytest.mark.parametrize("version", ["", "7 .1", "7.1;rm", "==7"])
def test_stager_refuses_a_version_that_is_not_one_pin(tmp_path: Path, version: str) -> None:
    dest = tmp_path / "mcpb"
    dest.mkdir()
    proc = _run_stager("--dest", str(dest), "--conexus-version", version)
    assert proc.returncode != 0
    assert not (dest / "constraints.txt").exists()


def test_stager_refuses_a_lock_that_already_pins_a_different_conexus(tmp_path: Path) -> None:
    src = _pin_file(tmp_path / "c.txt", _FLOOR, "conexus==1.0.0\n")
    dest = tmp_path / "mcpb"
    dest.mkdir()
    assert _run_stager("--dest", str(dest), "--source", str(src), "--conexus-version", "2.0.0").returncode != 0
    ok = _run_stager("--dest", str(dest), "--source", str(src), "--conexus-version", "1.0.0")
    assert ok.returncode == 0, ok.stderr
    assert (dest / "constraints.txt").read_text(encoding="utf-8").count("conexus==") == 1


def test_stager_floor_is_200_pins(tmp_path: Path) -> None:
    dest = tmp_path / "mcpb"
    dest.mkdir()
    short = _pin_file(tmp_path / "short.txt", 199)
    assert _run_stager("--dest", str(dest), "--source", str(short), "--conexus-version", "1.0").returncode != 0
    assert not (dest / "constraints.txt").exists()
    enough = _pin_file(tmp_path / "enough.txt", 200)
    proc = _run_stager("--dest", str(dest), "--source", str(enough), "--conexus-version", "1.0")
    assert proc.returncode == 0, proc.stderr
    assert "201 pins" in proc.stdout


@pytest.mark.parametrize("bad", ["six>=1.0\n", "-e .\n", "six==1.0 extra\n", "six\n", "six==1.0\x00\n"])
def test_stager_refuses_any_line_the_launcher_would_not_read_as_a_pin(tmp_path: Path, bad: str) -> None:
    """A file with 'enough' `==` lines but one the launcher rejects would stage a
    bundle that silently installs unpinned."""
    src = _pin_file(tmp_path / "c.txt", _FLOOR + 5, bad)
    dest = tmp_path / "mcpb"
    dest.mkdir()
    proc = _run_stager("--dest", str(dest), "--source", str(src), "--conexus-version", "1.0")
    assert proc.returncode != 0
    assert not (dest / "constraints.txt").exists()


def test_the_real_lock_passes_the_launchers_own_reader(bootstrap) -> None:
    pins, reason = bootstrap._read_constraints(str(RELEASE_CONSTRAINTS))
    assert pins and len(pins) >= _FLOOR, reason


def test_release_pack_step_stages_the_lock_before_packing_and_checks_the_zip() -> None:
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    step = re.search(r"- name: Pack \.mcpb Desktop Extension.*?\n(?=\n      - name: )", text, re.S)
    assert step, "the .mcpb pack step is gone from release.yml"
    body = step.group(0)
    assert "scripts/stage_mcpb_constraints.py" in body
    assert body.index("python3 scripts/stage_mcpb_constraints.py") < body.index("mcpb pack . conexus.mcpb"), (
        "constraints must be staged BEFORE `mcpb pack`, or the bundle ships unpinned"
    )
    # The packed zip is checked to carry the file, not just the staging run.
    assert re.search(r"unzip -l conexus\.mcpb > (\S+)", body)
    listing = re.search(r"unzip -l conexus\.mcpb > (\S+)", body).group(1)
    # A listing file, not `unzip | grep -q`: under the workflow's pipefail an early
    # grep exit SIGPIPEs unzip and fails a bundle that is fine.
    assert re.search(r"grep -q '[^']*constraints[^']*' " + re.escape(listing), body)
    assert "unzip -l conexus.mcpb |" not in body


def test_staged_file_is_packed_and_not_committed() -> None:
    ignore = (MCPB / ".mcpbignore").read_text(encoding="utf-8").splitlines()
    assert "constraints.txt" not in [ln.strip() for ln in ignore], ".mcpbignore would drop the staged lock"
    gitignore = (MCPB / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "constraints.txt" in [ln.strip() for ln in gitignore], (
        "the staged copy must stay out of git: a committed copy is a second hand-kept lock"
    )
    assert not (MCPB / "constraints.txt").exists() or "constraints.txt" in gitignore


def test_the_bundle_pyproject_has_one_tool_uv_header_to_anchor_on() -> None:
    text = (MCPB / "pyproject.toml").read_text(encoding="utf-8")
    assert len(re.findall(r"(?m)^\[tool\.uv\][ \t]*$", text)) == 1


# ── the launcher applies it ─────────────────────────────────────────────────


def test_pin_writes_the_release_lock_into_the_bundle_pyproject(bootstrap, bundle: Path) -> None:
    _stage(bundle)
    line = bootstrap._apply_pin(str(bundle), environ={})
    expected = _pins(RELEASE_CONSTRAINTS.read_text(encoding="utf-8"))
    got = _constraint_deps(bundle)
    assert got is not None and len(got) == len(expected) >= _FLOOR
    # Same pins, same markers (whitespace around ';' is uv's to normalise).
    norm = lambda s: re.sub(r"\s*;\s*", " ; ", s)  # noqa: E731
    assert [norm(g) for g in got] == [norm(e) for e in expected]
    assert "pinned" in line.lower() and str(len(expected)) in line
    # The bundle's own settings survive: the overrides and the workspace table.
    with (bundle / "pyproject.toml").open("rb") as f:
        doc = tomllib.load(f)
    assert doc["tool"]["uv"]["override-dependencies"]
    assert doc["tool"]["uv"]["workspace"] == {"members": []}
    assert doc["tool"]["uv"]["package"] is False
    assert doc["project"]["dependencies"][0].startswith("conexus")


def test_pin_is_idempotent_and_rewrites_a_stale_block(bootstrap, bundle: Path) -> None:
    _stage(bundle)
    bootstrap._apply_pin(str(bundle), environ={})
    once = (bundle / "pyproject.toml").read_bytes()
    bootstrap._apply_pin(str(bundle), environ={})
    assert (bundle / "pyproject.toml").read_bytes() == once
    (bundle / "constraints.txt").write_text("six==1.17.0\n" + "".join(f"p{i}==1.0\n" for i in range(5)), encoding="utf-8")
    bootstrap._apply_pin(str(bundle), environ={})
    assert _constraint_deps(bundle) == ["six==1.17.0"] + [f"p{i}==1.0" for i in range(5)]
    assert (bundle / "pyproject.toml").read_text(encoding="utf-8").count("nx-constraints") == 2


def test_the_opt_out_removes_the_pin_and_says_so(bootstrap, bundle: Path) -> None:
    original = (bundle / "pyproject.toml").read_bytes()
    _stage(bundle)
    bootstrap._apply_pin(str(bundle), environ={})
    assert _constraint_deps(bundle)
    line = bootstrap._apply_pin(str(bundle), environ={"NX_INSTALL_UNPINNED": "1"})
    assert _constraint_deps(bundle) is None
    assert (bundle / "pyproject.toml").read_bytes() == original
    assert "NX_INSTALL_UNPINNED=1" in line


def test_only_the_exact_opt_out_value_counts(bootstrap, bundle: Path) -> None:
    _stage(bundle)
    bootstrap._apply_pin(str(bundle), environ={"NX_INSTALL_UNPINNED": "0"})
    assert _constraint_deps(bundle)


def test_an_absent_constraints_file_degrades_to_the_unpinned_install_with_a_line(bootstrap, bundle: Path) -> None:
    original = (bundle / "pyproject.toml").read_bytes()
    line = bootstrap._apply_pin(str(bundle), environ={})
    assert (bundle / "pyproject.toml").read_bytes() == original
    assert "constraints.txt" in line and "unpinned" in line.lower()


def test_an_absent_file_also_clears_a_pin_from_an_earlier_install(bootstrap, bundle: Path) -> None:
    _stage(bundle)
    bootstrap._apply_pin(str(bundle), environ={})
    (bundle / "constraints.txt").unlink()
    bootstrap._apply_pin(str(bundle), environ={})
    assert _constraint_deps(bundle) is None


@pytest.mark.parametrize(
    "content",
    ["", "# only a comment\n", "six==1.17.0\nnot a pin line\n", "six>=1.0\n", "six==1.0\n\x00\n"],
)
def test_an_unusable_constraints_file_degrades_and_never_raises(bootstrap, bundle: Path, content: str) -> None:
    original = (bundle / "pyproject.toml").read_bytes()
    (bundle / "constraints.txt").write_text(content, encoding="utf-8")
    line = bootstrap._apply_pin(str(bundle), environ={})
    assert (bundle / "pyproject.toml").read_bytes() == original
    assert "unpinned" in line.lower()


def test_an_undecodable_constraints_file_degrades(bootstrap, bundle: Path) -> None:
    (bundle / "constraints.txt").write_bytes(b"six==1.0\n\xff\xfe\n")
    assert "unpinned" in bootstrap._apply_pin(str(bundle), environ={}).lower()


def test_a_pyproject_without_the_anchor_degrades(bootstrap, bundle: Path) -> None:
    (bundle / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0"\n', encoding="utf-8")
    _stage(bundle)
    assert "unpinned" in bootstrap._apply_pin(str(bundle), environ={}).lower()
    assert "constraint" not in (bundle / "pyproject.toml").read_text(encoding="utf-8")


def test_crlf_pyproject_stays_crlf(bootstrap, bundle: Path) -> None:
    text = (bundle / "pyproject.toml").read_text(encoding="utf-8").replace("\n", "\r\n")
    (bundle / "pyproject.toml").write_bytes(text.encode("utf-8"))
    _stage(bundle)
    bootstrap._apply_pin(str(bundle), environ={})
    raw = (bundle / "pyproject.toml").read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    assert _constraint_deps(bundle)


def test_main_applies_the_pin_before_the_sync_and_prints_the_line(bootstrap, monkeypatch, capsys) -> None:
    order: list[str] = []
    monkeypatch.setattr(bootstrap, "_apply_pin", lambda d, environ=None: order.append("pin") or "[conexus-mcpb] LINE")
    monkeypatch.setattr(bootstrap, "_sync_with_retry", lambda d: order.append("sync"))
    monkeypatch.setattr(bootstrap, "_launch", lambda argv, platform=None: order.append("launch") or None)
    monkeypatch.delenv("NX_MCPB_SKIP_RESOLVE_RETRY", raising=False)
    bootstrap.main(platform="linux")
    assert order == ["pin", "sync", "launch"]
    assert "LINE" in capsys.readouterr().err


def test_main_does_not_pin_when_the_sync_retry_is_skipped(bootstrap, bundle: Path, monkeypatch, capsys) -> None:
    """nexus-puzdu: the unpinned fallback lives in `_sync_with_retry`. With it skipped the
    launch `uv run` is exec'd (no process left to retry), so a pin that cannot resolve would
    take the start down with no way back. No pin is applied, and the line says so."""
    order: list[str] = []
    monkeypatch.setattr(bootstrap, "_apply_pin", lambda d, environ=None: order.append("pin") or "x")
    monkeypatch.setattr(bootstrap, "_sync_with_retry", lambda d: order.append("sync"))
    monkeypatch.setattr(bootstrap, "_launch", lambda argv, platform=None: order.append("launch") or None)
    monkeypatch.setattr(bootstrap, "_bundle_dir", lambda: str(bundle))  # never the real mcpb/
    monkeypatch.setenv("NX_MCPB_SKIP_RESOLVE_RETRY", "1")
    bootstrap.main(platform="linux")
    assert order == ["launch"]
    err = capsys.readouterr().err
    assert "NX_MCPB_SKIP_RESOLVE_RETRY" in err and "not pinned" in err


@pytest.mark.parametrize("value", ["0", "false", "", "no"])
def test_a_skip_variable_that_is_not_1_or_true_keeps_the_pin_and_the_sync(
    bootstrap, bundle: Path, monkeypatch, value,
) -> None:
    """Only 1 or true skip: `=0` used to count, and now that skipping also drops the pin,
    a value meant as 'off' must not unpin the extension."""
    order: list[str] = []
    monkeypatch.setattr(bootstrap, "_apply_pin", lambda d, environ=None: order.append("pin") or "x")
    monkeypatch.setattr(bootstrap, "_sync_with_retry", lambda d: order.append("sync"))
    monkeypatch.setattr(bootstrap, "_launch", lambda argv, platform=None: order.append("launch") or None)
    monkeypatch.setattr(bootstrap, "_bundle_dir", lambda: str(bundle))
    monkeypatch.setenv("NX_MCPB_SKIP_RESOLVE_RETRY", value)
    bootstrap.main(platform="linux")
    assert order == ["pin", "sync", "launch"]


def test_skipped_sync_removes_a_pin_left_by_an_earlier_launch(bootstrap, bundle: Path, monkeypatch, capsys) -> None:
    """The pyproject on disk is what `uv run` reads: a block written by a previous
    (non-skipped) launch must not outlive the skip."""
    _stage(bundle)
    bootstrap._apply_pin(str(bundle), environ={})
    assert _constraint_deps(bundle)
    monkeypatch.setattr(bootstrap, "_bundle_dir", lambda: str(bundle))
    monkeypatch.setattr(bootstrap, "_launch", lambda argv, platform=None: None)
    monkeypatch.setattr(bootstrap, "_sync_with_retry", lambda d: pytest.fail("the sync must be skipped"))
    monkeypatch.setenv("NX_MCPB_SKIP_RESOLVE_RETRY", "1")
    bootstrap.main(platform="linux")
    assert not _constraint_deps(bundle)
    assert bootstrap._PIN_BEGIN not in (bundle / "pyproject.toml").read_text(encoding="utf-8")
    assert "not pinned" in capsys.readouterr().err


def test_skipped_sync_with_no_pin_on_disk_leaves_pyproject_alone(bootstrap, bundle: Path, monkeypatch) -> None:
    _stage(bundle)
    before = (bundle / "pyproject.toml").read_bytes()
    monkeypatch.setattr(bootstrap, "_bundle_dir", lambda: str(bundle))
    monkeypatch.setattr(bootstrap, "_launch", lambda argv, platform=None: None)
    monkeypatch.setenv("NX_MCPB_SKIP_RESOLVE_RETRY", "1")
    bootstrap.main(platform="linux")
    assert (bundle / "pyproject.toml").read_bytes() == before


# ── the managed block never yields invalid TOML (critic blocker 1) ──────────


def _valid_toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_the_happy_path_writes_valid_toml(bootstrap, bundle: Path) -> None:
    _stage(bundle)
    bootstrap._apply_pin(str(bundle), environ={})
    assert _valid_toml(bundle / "pyproject.toml")["tool"]["uv"]["constraint-dependencies"]


def test_an_existing_constraint_dependencies_key_is_left_alone_and_reported(bootstrap, bundle: Path) -> None:
    """A hand-written `constraint-dependencies` in [tool.uv] plus our block is a
    duplicate key: uv would refuse the whole pyproject. The file stays as it was."""
    text = (bundle / "pyproject.toml").read_text(encoding="utf-8").replace(
        "[tool.uv]\n", '[tool.uv]\nconstraint-dependencies = ["six==1.17.0"]\n', 1
    )
    (bundle / "pyproject.toml").write_text(text, encoding="utf-8")
    _stage(bundle)
    line = bootstrap._apply_pin(str(bundle), environ={})
    assert (bundle / "pyproject.toml").read_text(encoding="utf-8") == text
    assert _valid_toml(bundle / "pyproject.toml")["tool"]["uv"]["constraint-dependencies"] == ["six==1.17.0"]
    assert "not pinned" in line.lower()
    assert "still in force" in line.lower()


def test_a_begin_marker_without_an_end_marker_is_left_alone(bootstrap, bundle: Path) -> None:
    text = (bundle / "pyproject.toml").read_text(encoding="utf-8").replace(
        "[tool.uv]\n", "[tool.uv]\n" + bootstrap._PIN_BEGIN + '\nconstraint-dependencies = ["six==1.17.0"]\n', 1
    )
    (bundle / "pyproject.toml").write_text(text, encoding="utf-8")
    _stage(bundle)
    line = bootstrap._apply_pin(str(bundle), environ={})
    assert (bundle / "pyproject.toml").read_text(encoding="utf-8") == text
    _valid_toml(bundle / "pyproject.toml")
    assert "not pinned" in line.lower()


def test_an_end_marker_at_eof_without_a_newline_is_replaced_cleanly(bootstrap, bundle: Path) -> None:
    body = '[project]\nname = "x"\nversion = "0"\n\n[tool.uv]\n'
    stale = bootstrap._PIN_BEGIN + '\nconstraint-dependencies = [\n    "six==1.0",\n]\n' + bootstrap._PIN_END
    (bundle / "pyproject.toml").write_text(body + stale, encoding="utf-8", newline="")
    _stage(bundle)
    line = bootstrap._apply_pin(str(bundle), environ={})
    doc = _valid_toml(bundle / "pyproject.toml")
    assert len(doc["tool"]["uv"]["constraint-dependencies"]) >= _FLOOR
    assert "pinned to" in line
    # And the opt-out removes it again from the same shape.
    (bundle / "pyproject.toml").write_text(body + stale, encoding="utf-8", newline="")
    bootstrap._apply_pin(str(bundle), environ={"NX_INSTALL_UNPINNED": "1"})
    assert "constraint-dependencies" not in _valid_toml(bundle / "pyproject.toml")["tool"]["uv"]


def test_a_failed_opt_out_says_the_old_pin_is_still_in_force(bootstrap, bundle: Path) -> None:
    text = (bundle / "pyproject.toml").read_text(encoding="utf-8").replace(
        "[tool.uv]\n", "[tool.uv]\n" + bootstrap._PIN_BEGIN + '\nconstraint-dependencies = ["six==1.17.0"]\n', 1
    )
    (bundle / "pyproject.toml").write_text(text, encoding="utf-8")
    line = bootstrap._apply_pin(str(bundle), environ={"NX_INSTALL_UNPINNED": "1"})
    # The block has no END, so it is not ours to strip: say so rather than "unpinned".
    assert (bundle / "pyproject.toml").read_text(encoding="utf-8") == text
    assert "still in force" in line.lower()


def test_an_unparseable_original_is_left_alone(bootstrap, bundle: Path) -> None:
    (bundle / "pyproject.toml").write_text("[tool.uv]\nbroken = \n", encoding="utf-8")
    _stage(bundle)
    line = bootstrap._apply_pin(str(bundle), environ={})
    assert (bundle / "pyproject.toml").read_text(encoding="utf-8") == "[tool.uv]\nbroken = \n"
    assert "not pinned" in line.lower()


# ── tmp file: pid-unique, always removed (residual 4) ───────────────────────


def test_a_failed_replace_leaves_no_tmp_file(bootstrap, tmp_path: Path) -> None:
    """os.replace onto a directory fails on every platform; the scratch file must not outlive it."""
    target = tmp_path / "pyproject.toml"
    target.mkdir()
    with pytest.raises(OSError):
        bootstrap._write_pyproject(str(target), "x = 1\n")
    assert [p.name for p in tmp_path.iterdir()] == ["pyproject.toml"]


def test_the_tmp_name_carries_the_pid(bootstrap, tmp_path: Path) -> None:
    """A directory sitting at the pid-named scratch path makes the write fail, which
    pins the name without patching os.replace process-wide."""
    import os

    target = tmp_path / "pyproject.toml"
    target.write_text("a = 1\n", encoding="utf-8")
    (tmp_path / f"pyproject.toml.nx-tmp.{os.getpid()}").mkdir()
    with pytest.raises(OSError):
        bootstrap._write_pyproject(str(target), "b = 2\n")
    assert target.read_text(encoding="utf-8") == "a = 1\n"


def test_a_write_failure_in_apply_pin_degrades_with_a_not_pinned_line(bootstrap, bundle: Path) -> None:
    import os

    _stage(bundle)
    original = (bundle / "pyproject.toml").read_bytes()
    (bundle / f"pyproject.toml.nx-tmp.{os.getpid()}").mkdir()
    line = bootstrap._apply_pin(str(bundle), environ={})
    assert (bundle / "pyproject.toml").read_bytes() == original
    assert "not pinned" in line.lower() and "unpinned" in line.lower()


# ── a pin that breaks the resolve falls back to unpinned (critic blocker 2) ──


def _pinned_bundle(bootstrap, bundle: Path, extra: str = "") -> None:
    (bundle / "constraints.txt").write_text(
        "".join(f"p{i}==1.0\n" for i in range(5)) + extra, encoding="utf-8"
    )
    bootstrap._apply_pin(str(bundle), environ={})
    assert bootstrap._PIN_BEGIN in (bundle / "pyproject.toml").read_text(encoding="utf-8")


class _Proc:
    def __init__(self, rc: int, err: str = "") -> None:
        self.returncode, self.stdout, self.stderr = rc, "", err


def test_a_pin_induced_resolve_failure_syncs_once_unpinned_without_sleeping(bootstrap, bundle: Path, capsys) -> None:
    _pinned_bundle(bootstrap, bundle, "conexus==9.9.9\n")
    calls: list[bool] = []
    sleeps: list[int] = []

    # Not the propagation class (that one keeps its retry, below).
    def run_other(cmd, **kw):
        pinned = bootstrap._PIN_BEGIN in (bundle / "pyproject.toml").read_text(encoding="utf-8")
        calls.append(pinned)
        return _Proc(1, "error: Failed to build `foo==1.0`\n") if pinned else _Proc(0)

    bootstrap._sync_with_retry(str(bundle), run=run_other, sleep=sleeps.append)
    assert calls == [True, False]
    assert sleeps == []
    assert bootstrap._PIN_BEGIN not in (bundle / "pyproject.toml").read_text(encoding="utf-8")
    _valid_toml(bundle / "pyproject.toml")
    err = capsys.readouterr().err
    assert "unpinned" in err.lower() and "pin" in err.lower()


def test_a_pinned_wheel_the_platform_cannot_take_falls_back_at_once_not_after_fifteen_minutes(
    bootstrap, bundle: Path,
) -> None:
    """uv's 'No solution found' names conexus for EVERY resolver failure (it is the root
    dependency), so the broad propagation class matched a pin-caused conflict and held it
    on the long retry. With the pin in force only 'the pinned conexus is not served yet'
    keeps the retry; this one strips the pin and syncs once unpinned."""
    _pinned_bundle(bootstrap, bundle, "conexus==9.9.9\n")
    sleeps: list[int] = []
    calls: list[bool] = []
    shape = ("error: No solution found when resolving dependencies:\n  Because onnxruntime==1.24.2 has no "
             "wheels with a matching platform tag and conexus==9.9.9 depends on onnxruntime==1.24.2, we can "
             "conclude that conexus==9.9.9 cannot be used.")

    def run(cmd, **kw):
        pinned = bootstrap._PIN_BEGIN in (bundle / "pyproject.toml").read_text(encoding="utf-8")
        calls.append(pinned)
        return _Proc(1, shape) if pinned else _Proc(0)

    bootstrap._sync_with_retry(str(bundle), run=run, sleep=sleeps.append)
    assert calls == [True, False], "one pinned attempt, then one unpinned"
    assert sleeps == [], "no propagation wait for a pin-caused conflict"


def test_the_propagation_class_with_a_conexus_pin_keeps_its_retry_and_its_pin(bootstrap, bundle: Path) -> None:
    _pinned_bundle(bootstrap, bundle, "conexus==9.9.9\n")
    sleeps: list[int] = []
    outcomes = [
        _Proc(1, "error: No solution found when resolving dependencies: conexus==9.9.9 was not found in the package registry"),
        _Proc(0),
    ]
    seen_pinned: list[bool] = []

    def run(cmd, **kw):
        seen_pinned.append(bootstrap._PIN_BEGIN in (bundle / "pyproject.toml").read_text(encoding="utf-8"))
        return outcomes.pop(0)

    bootstrap._sync_with_retry(str(bundle), run=run, sleep=sleeps.append, sleeps=(7, 8))
    assert sleeps == [7]
    assert seen_pinned == [True, True]


def test_the_propagation_class_without_a_conexus_pin_falls_back_to_unpinned(bootstrap, bundle: Path) -> None:
    """The bundle floor `conexus>=N` against a lock that has no conexus pin: the
    resolver says 'No solution found' naming conexus, which is not propagation."""
    _pinned_bundle(bootstrap, bundle)  # no conexus pin
    sleeps: list[int] = []
    calls: list[bool] = []

    def run(cmd, **kw):
        pinned = bootstrap._PIN_BEGIN in (bundle / "pyproject.toml").read_text(encoding="utf-8")
        calls.append(pinned)
        return _Proc(1, "No solution found: conexus>=7.86 requires mcp>=2 but mcp==1.27.1") if pinned else _Proc(0)

    bootstrap._sync_with_retry(str(bundle), run=run, sleep=sleeps.append)
    assert calls == [True, False] and sleeps == []


def test_a_failure_that_survives_the_unpin_still_ends_the_start(bootstrap, bundle: Path) -> None:
    _pinned_bundle(bootstrap, bundle)
    calls: list[int] = []

    def run(cmd, **kw):
        calls.append(1)
        return _Proc(3, "error: network unreachable")

    with pytest.raises(SystemExit) as exc:
        bootstrap._sync_with_retry(str(bundle), run=run, sleep=lambda s: pytest.fail("no sleep"))
    assert exc.value.code == 3
    assert len(calls) == 2  # pinned, then exactly one unpinned retry


def test_a_bundle_that_was_never_pinned_does_not_retry_unpinned(bootstrap, bundle: Path) -> None:
    calls: list[int] = []

    def run(cmd, **kw):
        calls.append(1)
        return _Proc(2, "error: network unreachable")

    with pytest.raises(SystemExit):
        bootstrap._sync_with_retry(str(bundle), run=run, sleep=lambda s: None)
    assert len(calls) == 1


# ── the opt-out is reachable from Desktop (residual 5) ──────────────────────


def test_the_manifest_carries_the_opt_out_as_a_boolean_user_setting() -> None:
    import json

    manifest = json.loads((MCPB / "manifest.json").read_text(encoding="utf-8"))
    setting = manifest["user_config"]["install_unpinned"]
    assert setting["type"] == "boolean" and setting["default"] is False and setting["required"] is False
    # The env overlay names a variable of its own, so it cannot shadow a host-level NX_INSTALL_UNPINNED.
    env = manifest["server"]["mcp_config"]["env"]
    # (and include_pdf has its own, tests/test_mcpb_pdf_setting.py)
    assert env == {"NX_MCPB_INSTALL_UNPINNED": "${user_config.install_unpinned}",
                   "NX_MCPB_INCLUDE_PDF": "${user_config.include_pdf}"}


@pytest.mark.parametrize(
    "environ, pinned",
    [
        ({"NX_MCPB_INSTALL_UNPINNED": "true"}, False),
        ({"NX_MCPB_INSTALL_UNPINNED": "True"}, False),
        ({"NX_INSTALL_UNPINNED": "true"}, False),
        ({"NX_INSTALL_UNPINNED": "1", "NX_MCPB_INSTALL_UNPINNED": "false"}, False),
        ({"NX_MCPB_INSTALL_UNPINNED": "false"}, True),
        ({"NX_MCPB_INSTALL_UNPINNED": "${user_config.install_unpinned}"}, True),
        ({"NX_INSTALL_UNPINNED": "yes"}, True),
        ({"NX_INSTALL_UNPINNED": "0"}, True),
        ({}, True),
    ],
)
def test_opt_out_values(bootstrap, bundle: Path, environ: dict, pinned: bool) -> None:
    _stage(bundle)
    line = bootstrap._apply_pin(str(bundle), environ=environ)
    assert bool(_constraint_deps(bundle)) is pinned
    if not pinned:
        assert "unpinned" in line.lower() and "=" in line
