# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-228 Phase 1: the two hook e2e harnesses read the launcher entry form.

Every conexus hook entry became ``uv tool run ... python launch/nx_launch.py
nx-hook <verb>`` and the ``nexus`` server became the same through ``nx-mcp``.
A read-only run found both harnesses blind to it:

* ``tests/e2e/hook-cli-skew/drive.py`` put no ``uv`` on its PATH and counted
  "command not found" as a pass, so 0 of 15 entries ran and the gate said PASSED.
* ``tests/e2e/hook-surface-shakeout`` did not stage ``launch/``, matched the
  wheel-mutating verbs on ``command == "nx-hook"`` (never true now), labelled all
  seven launcher entries ``nx_launch.py``, and ran the server around the launcher.

These tests pin the repaired reading, with a fake ``uv`` for the driver (a unit
test of its decisions) and the real manifest for the census.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import stat
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))
import _hook_wiring as wiring  # noqa: E402

SKEW = REPO / "tests" / "e2e" / "hook-cli-skew"
SHAKEOUT = REPO / "tests" / "e2e" / "hook-surface-shakeout"
HOOKS_JSON = REPO / "conexus" / "hooks" / "hooks.json"
MCP_JSON = REPO / "conexus" / ".mcp.json"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def drive():
    return _load("skew_drive", SKEW / "drive.py")


@pytest.fixture(scope="module")
def census():
    return _load("hook_census_launcher_form", SHAKEOUT / "hook_census.py")


def _entries() -> list[dict]:
    data = json.loads(HOOKS_JSON.read_text())
    return [e for groups in data["hooks"].values() for g in groups for e in g["hooks"] if e.get("type") == "command"]


def _fake_uv(directory: pathlib.Path, body: str) -> pathlib.Path:
    directory.mkdir(parents=True, exist_ok=True)
    uv = directory / "uv"
    uv.write_text("#!/bin/sh\n" + body)
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    return uv


def _hooks(tmp_path: pathlib.Path, n: int, *, command: str = "uv", args: list[str] | None = None) -> pathlib.Path:
    args = args if args is not None else ["tool", "run", "${CLAUDE_PLUGIN_ROOT}/launch/nx_launch.py", "nx-hook", "preflight"]
    entry = {"type": "command", "command": command, "args": args, "timeout": 5}
    p = tmp_path / "hooks.json"
    p.write_text(json.dumps({"hooks": {"SessionStart": [{"matcher": "", "hooks": [entry] * n}]}}))
    return p


def _drive(drive, tmp_path, monkeypatch, hooks: pathlib.Path, cli_bin: str, *extra: str) -> int:
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return drive.main([str(hooks), str(tmp_path), cli_bin, "t", str(scratch), *extra])


# ---------------------------------------------------------------- hook-cli-skew


def test_a_path_without_uv_fails_the_gate_instead_of_passing_it(drive, tmp_path, monkeypatch, capsys) -> None:
    """The 2026-10-13 blindness: no `uv`, every entry "not found", PASSED."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert _drive(drive, tmp_path, monkeypatch, _hooks(tmp_path, 3), "none", "3") == 1
    assert "`uv` is not on PATH" in capsys.readouterr().out


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only: the skew gate is a bash harness that links uv by symlink and writes shebang fakes")
def test_an_entry_whose_command_cannot_start_is_a_failure(drive, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("PATH", str(_fake_uv(tmp_path / "real", "exit 0").parent))
    real_symlink = os.symlink
    monkeypatch.setattr(os, "symlink", lambda _src, dst: real_symlink("/nonexistent/uv", dst))
    assert _drive(drive, tmp_path, monkeypatch, _hooks(tmp_path, 3), "none", "3") == 1
    out = capsys.readouterr().out
    assert out.count("did not start") == 3
    assert "0 started" in out


def test_a_command_absent_by_nobodys_design_is_not_ok(drive, tmp_path, monkeypatch, capsys) -> None:
    """There is no "command not found (non-blocking)" branch any more."""
    monkeypatch.setenv("PATH", str(_fake_uv(tmp_path / "real", "exit 0").parent))
    assert _drive(drive, tmp_path, monkeypatch, _hooks(tmp_path, 1, command="no-such-command-skew", args=[]), "none", "1") == 1
    assert "non-blocking" not in capsys.readouterr().out


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only: the skew gate is a bash harness that links uv by symlink and writes shebang fakes")
def test_fewer_started_than_the_floor_fails_even_with_no_failures(drive, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("PATH", str(_fake_uv(tmp_path / "real", "echo 'conexus: x is not installed' >&2\nexit 0").parent))
    hooks = _hooks(tmp_path, 2)
    assert _drive(drive, tmp_path, monkeypatch, hooks, "none", "2") == 0
    assert _drive(drive, tmp_path, monkeypatch, hooks, "none") == 1  # default floor
    assert "fewer than the floor" in capsys.readouterr().out


def test_the_default_floor_is_the_manifests_command_entry_count(drive) -> None:
    """A floor below the manifest lets entries go unrun; above it fails always.
    Raise DEFAULT_MIN_EXECUTED together with the manifest."""
    assert drive.DEFAULT_MIN_EXECUTED == len(_entries())


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only: the skew gate is a bash harness that links uv by symlink and writes shebang fakes")
def test_driven_env_has_uv_alone_an_empty_tools_dir_and_the_cli_first(drive, tmp_path, monkeypatch) -> None:
    out = tmp_path / "seen"
    real = _fake_uv(tmp_path / "real", f'printf "%s\\n%s\\n" "$NX_TOOLS_DIR" "$PATH" > {out}\nexit 0')
    monkeypatch.setenv("PATH", f"{real.parent}:/usr/bin")
    cli = tmp_path / "clibin"
    cli.mkdir()
    assert _drive(drive, tmp_path, monkeypatch, _hooks(tmp_path, 1), str(cli), "1") == 0
    tools, path = out.read_text().splitlines()
    assert os.listdir(tools) == []
    assert pathlib.Path(tools).is_relative_to(tmp_path / "scratch")
    first, second, *system = path.split(":")
    assert first == str(cli)
    assert sorted(os.listdir(second)) == ["uv"]
    assert system == ["/usr/bin", "/bin"]
    assert str(real.parent) not in path.split(":"), "the caller's own PATH must not reach the driven entries"


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only: the skew gate is a bash harness that links uv by symlink and writes shebang fakes")
def test_a_cli_run_where_the_launcher_found_no_cli_fails(drive, tmp_path, monkeypatch, capsys) -> None:
    """The launcher found this machine's install, or none: the run proves nothing."""
    monkeypatch.setenv("PATH", str(_fake_uv(tmp_path / "real", "echo 'conexus: `nx-hook` is not installed' >&2\nexit 0").parent))
    assert _drive(drive, tmp_path, monkeypatch, _hooks(tmp_path, 1), str(tmp_path), "1") == 1
    assert "did not find the CLI under test" in capsys.readouterr().out


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only: the skew gate is a bash harness that links uv by symlink and writes shebang fakes")
def test_a_no_cli_run_where_the_launcher_was_silent_fails(drive, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("PATH", str(_fake_uv(tmp_path / "real", "exit 0").parent))
    assert _drive(drive, tmp_path, monkeypatch, _hooks(tmp_path, 1), "none", "1") == 1
    assert "did not say so" in capsys.readouterr().out


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only: the skew gate is a bash harness that links uv by symlink and writes shebang fakes")
def test_uv_exiting_2_is_still_a_blocking_failure(drive, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("PATH", str(_fake_uv(tmp_path / "real", "echo 'error: uv could not run' >&2\nexit 2").parent))
    assert _drive(drive, tmp_path, monkeypatch, _hooks(tmp_path, 1), str(tmp_path), "1") == 1
    assert "exit 2 (blocks)" in capsys.readouterr().out


def test_run_sh_passes_the_negative_control_its_own_floor() -> None:
    text = (SKEW / "run.sh").read_text()
    assert '"$BIN57" negctl "$WORK/negctl" 1 >' in text, "the one-entry control needs a floor of 1"
    assert "launcher control" in text, "run.sh must prove the launcher reaches the CLI under test"


# ------------------------------------------------------------ hook-surface-shakeout


def test_the_census_gives_each_launcher_entry_its_own_label(census) -> None:
    labels = [census.label(e) for e in _entries()]
    assert "nx_launch.py" not in labels, labels
    for entry in _entries():
        verb = wiring.command_verb(entry)
        program = wiring.launched_program(entry)
        if program[:1] == ["nx-hook"]:
            assert census.label(entry) == f"nx-hook {verb}"
        elif program:
            assert census.label(entry) == program[0]
    assert "nx-session-end-launcher" in labels
    assert {"nx-hook upgrade-auto", "nx-hook self-gc", "nx-hook preflight"} <= set(labels)
    assert "nx_setup_notice.py" in labels


def test_a_launcher_entry_and_a_direct_entry_for_one_verb_share_a_label(census) -> None:
    direct = {"type": "command", "command": "nx-hook", "args": ["preflight"]}
    launched = next(e for e in _entries() if wiring.command_verb(e) == "preflight")
    assert census.label(direct) == census.label(launched) == "nx-hook preflight"


def test_a_server_launcher_entry_is_labelled_by_its_program(census) -> None:
    entry = {"type": "command", "command": "uv", "args": [*wiring.UV_LAUNCHER_ARGV, wiring.NX_LAUNCH, "nx-mcp"]}
    assert census.label(entry) == "nx-mcp"


def test_the_harness_exclusions_and_silent_set_match_real_entries(census) -> None:
    """A set that matches nothing is the vacuity this census exists to prevent."""
    labels = {census.label(e) for e in _entries()}
    assert census.EXCLUDED_BY_HARNESS == {"nx-hook upgrade-auto", "nx-hook self-gc"}
    assert census.EXCLUDED_BY_HARNESS <= labels
    assert census.SILENT_BY_DESIGN <= labels
    assert {"nx-hook preflight", "nx_setup_notice.py"} <= census.SILENT_BY_DESIGN


def test_the_declared_roster_is_one_row_per_launcher_verb(census) -> None:
    declared = census.declared(HOOKS_JSON)
    assert "nx_launch.py" not in declared
    for name in ("nx-hook session-start", "nx-hook rdr", "nx-hook upgrade-auto", "nx-session-end-launcher", "nx_setup_notice.py"):
        assert name in declared, sorted(declared)


def test_one_fired_launcher_entry_marks_only_itself(census, tmp_path) -> None:
    hooks = tmp_path / "hooks.json"
    hooks.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": "uv", "args": [*wiring.UV_LAUNCHER_ARGV, wiring.NX_LAUNCH, "nx-hook", "preflight"]},
        {"type": "command", "command": "uv", "args": [*wiring.UV_LAUNCHER_ARGV, wiring.NX_LAUNCH, "nx-hook", "rdr"]},
    ]}]}}))
    tsv = tmp_path / "census.tsv"
    tsv.write_text("SessionStart\tnx-hook preflight\t1\t2\n")
    seen, rows = census.command_roster(tsv)
    declared = census.declared(hooks)
    assert rows == 1 and "nx-hook preflight" in seen
    assert [n for n in declared if n not in seen] == ["nx-hook rdr"]


def test_server_argv_is_what_the_manifest_runs_with_the_root_substituted(census) -> None:
    argv = census.server_argv(json.loads(MCP_JSON.read_text()), "nexus", "/home/nexus/plugin")
    assert argv[0] == "uv"
    assert argv[-2:] == ["/home/nexus/plugin/launch/nx_launch.py", "nx-mcp"]
    assert not any("${" in a for a in argv)
    catalog = census.server_argv(json.loads(MCP_JSON.read_text()), "nexus-catalog", "/p")
    assert catalog[-1] == "nx-mcp-catalog"


def test_server_argv_refuses_a_server_that_does_not_go_through_the_launcher(census) -> None:
    with pytest.raises(ValueError, match="launcher"):
        census.server_argv({"nexus": {"command": "nx-mcp", "args": []}}, "nexus", "/p")


def test_run_sh_stages_launch_drops_by_label_and_wraps_the_launcher() -> None:
    text = (SHAKEOUT / "run.sh").read_text()
    assert 'cp -R "$ROOT/conexus/launch"' in text, "launch/ never reached the staged plugin"
    assert 'h.get("command") == "nx-hook"' not in text, "the drop rule must not key on a bare nx-hook command"
    assert "EXCLUDED_BY_HARNESS" in text
    assert "dropped != len(EXCLUDED_BY_HARNESS)" in text, "the drop must assert it dropped both entries"
    assert "nxenv/bin/nx-mcp" not in text, "the tee must wrap the launcher form, not exec nx-mcp directly"
    assert "server_argv" in text
    assert "__MCP_SERVER__" in text


def test_the_mcp_override_still_names_the_tee_that_wraps_the_launcher() -> None:
    text = (SHAKEOUT / "shakeout_in_container.sh").read_text()
    assert "nxenv/bin/nx-mcp" not in text
    assert "/home/nexus/mcp_tee.sh" in text


# The staging blocks of run.sh are embedded Python; running them is the only way to
# see what they stage. Block order in run.sh: .mcp.json, tee bake, hooks.json.
def _run_sh_blocks() -> list[str]:
    import re

    return re.findall(r"<<'PY'\n(.*?)\nPY\n", (SHAKEOUT / "run.sh").read_text(), re.S)


def _run_block(index: int, tmp_path: pathlib.Path, *args: str):
    import subprocess

    blocks = _run_sh_blocks()
    assert len(blocks) == 3, "run.sh's embedded staging blocks moved; update this test"
    return subprocess.run([sys.executable, "-", *args], input=blocks[index], text=True, capture_output=True, cwd=tmp_path)


def _stage_hooks(tmp_path: pathlib.Path, src: pathlib.Path = HOOKS_JSON):
    out = tmp_path / "hooks.staged.json"
    shims = tmp_path / "shims"
    shims.mkdir(exist_ok=True)
    r = _run_block(2, tmp_path, str(src), str(out), str(shims), str(SHAKEOUT / "hook_census.py"))
    return r, out, shims


def test_staging_drops_exactly_the_two_wheel_mutating_entries_and_shims_the_rest(census, tmp_path) -> None:
    r, out, shims = _stage_hooks(tmp_path)
    assert r.returncode == 0, r.stderr
    staged = json.loads(out.read_text())
    entries = [e for groups in staged["hooks"].values() for g in groups for e in g["hooks"]]
    labels = {census.label(e) for e in entries if e["type"] == "command"}
    assert not labels & census.EXCLUDED_BY_HARNESS
    assert "2 dropped" in r.stdout
    commands = [e["command"] for e in entries if e["type"] == "command"]
    assert all(c.startswith("/home/nexus/shims/") for c in commands), commands
    assert len(list(shims.glob("shim*.sh"))) == len(commands) == len(_entries()) - 2


def test_staging_refuses_when_the_drop_rule_matches_nothing(tmp_path) -> None:
    """The original bug: entries moved behind the launcher and the drop quietly
    matched none, so `upgrade-auto` would have replaced the wheel under test."""
    data = json.loads(HOOKS_JSON.read_text())
    for groups in data["hooks"].values():
        for g in groups:
            for e in g["hooks"]:
                if e.get("type") == "command":
                    e["args"] = [a.replace("upgrade-auto", "renamed-verb").replace("self-gc", "renamed-gc") for a in e["args"]]
    src = tmp_path / "renamed.json"
    src.write_text(json.dumps(data))
    r, _out, _shims = _stage_hooks(tmp_path, src)
    assert r.returncode != 0
    assert "the drop rule no longer matches the manifest" in r.stderr


def test_the_tee_child_is_the_launcher_form_of_the_manifest(tmp_path) -> None:
    text = (SHAKEOUT / "run.sh").read_text()
    start = text.index("cat > \"$STAGE/mcp_tee.sh\" <<'SH'\n") + len("cat > \"$STAGE/mcp_tee.sh\" <<'SH'\n")
    tee = tmp_path / "mcp_tee.sh"
    tee.write_text(text[start : text.index("\nSH\n", start) + 1])
    r = _run_block(1, tmp_path, str(MCP_JSON), str(tee), str(SHAKEOUT))
    assert r.returncode == 0, r.stderr
    last = tee.read_text().splitlines()[-1]
    assert last.startswith("exec tee -a /home/nexus/run/mcp-stdin.jsonl | uv tool run --directory /home/nexus/plugin ")
    assert last.endswith("/home/nexus/plugin/launch/nx_launch.py nx-mcp")
    assert "__MCP_SERVER__" not in tee.read_text() and "${" not in last
