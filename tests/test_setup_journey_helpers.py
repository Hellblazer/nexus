# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-228 P2.7 (nexus-sv2wx.20): the pure parts of the setup journey test.

``tests/e2e/setup-journey.sh`` runs the journey itself, on a test host, because it starts the plugin's
hook entries and a PostgreSQL. This file runs everywhere and starts none of that: it pins what the
journey reads and decides with, so a change to ``hooks.json``, ``.mcp.json`` or the stand-in's wording
fails here, in the fast suite, instead of in a half-hour run. It runs no hook entry.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests._platform import posix_only

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "tests" / "e2e" / "lib" / "setup_journey.py"
WRAPPER = REPO / "tests" / "e2e" / "setup-journey.sh"
HOOKS = REPO / "conexus" / "hooks" / "hooks.json"
MCP = REPO / "conexus" / ".mcp.json"


def _load():
    spec = importlib.util.spec_from_file_location("setup_journey", LIB)
    module = importlib.util.module_from_spec(spec)
    sys.modules["setup_journey"] = module
    spec.loader.exec_module(module)
    return module


sj = _load()


def _plugin_module(*parts: str):
    path = REPO.joinpath("conexus", *parts)
    spec = importlib.util.spec_from_file_location(f"journey_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent))
    return module


class TestEntries:
    def test_the_session_start_entries_are_hooks_json_as_written(self) -> None:
        hooks = json.loads(HOOKS.read_text(encoding="utf-8"))
        entries = sj.hook_entries(hooks, "SessionStart", "startup", "/plugin", "/project")
        written = [h for g in hooks["hooks"]["SessionStart"] for h in g["hooks"]]
        assert len(entries) == len(written) >= 9
        assert entries[0].args[-1] == "/plugin/hooks/scripts/nx_setup_notice.py"
        for entry in entries:
            assert entry.command == "uv"
            assert not any("${" in a for a in entry.args), f"{entry.name} keeps a placeholder"
            assert "/plugin/" in " ".join(entry.args)
            assert entry.timeout > 0

    def test_the_matcher_is_honoured(self) -> None:
        hooks = {"hooks": {"SessionStart": [
            {"matcher": "startup", "hooks": [{"type": "command", "command": "uv", "args": ["a.py"]}]},
            {"matcher": "resume", "hooks": [{"type": "command", "command": "uv", "args": ["b.py"]}]},
        ]}}
        assert [e.name for e in sj.hook_entries(hooks, "SessionStart", "startup", "/p", "/q")] == ["a.py"]
        assert [e.name for e in sj.hook_entries(hooks, "SessionStart", "resume", "/p", "/q")] == ["b.py"]

    def test_the_nexus_server_entry_runs_the_launcher_for_nx_mcp(self) -> None:
        entry = sj.server_entry(json.loads(MCP.read_text(encoding="utf-8")), "nexus", "/plugin", "/project")
        assert entry.command == "uv"
        assert "/plugin/launch/nx_launch.py" in entry.args and entry.args[-1] == "nx-mcp"
        assert not any("${" in a for a in entry.args)

    def test_the_journey_names_the_stand_in_words_the_plugin_uses(self) -> None:
        standin = _plugin_module("launch", "nx_standin.py")
        notice = _plugin_module("hooks", "scripts", "nx_setup_notice.py")
        notice.load_siblings()
        assert sj.BROKEN_PHRASE == standin.BROKEN_PHRASE
        assert sj.NOT_INSTALLED_PHRASE in notice.not_installed_text(("x",), "https://example.test")
        assert "ASK THE USER FIRST" in standin.ASK_FIRST
        # The names the journey expects of each stand-in are the ones the module builds.
        assert standin.stand_in("nx-mcp").name in sj.STAND_IN_NAMES
        assert standin.stand_in("nx-mcp", "why").name in sj.STAND_IN_NAMES
        assert standin.stand_in("nx-mcp-catalog").name in sj.STAND_IN_NAMES
        assert {t["name"] for t in standin.SETUP_TOOLS} == set(sj.SETUP_TOOL_NAMES)
        # The quiet lines the journey asserts at the end of B and C.
        assert "the user chose to leave it so" in notice.quiet_down_text()
        assert "no Nexus service is set up, as the user chose" in notice.quiet_no_service_text()


class TestOutcome:
    @pytest.mark.parametrize("text,expected", [
        ("Setup is done.\nNext: reconnect", "done"),
        ("Setup failed.\nLast stage: build.", "failed"),
        ("Setup finished, but nx doctor reported problems.", "failed"),
        ("Setup cannot run on this host.", "failed"),
        ("A setup run is in progress (mode local).\nStage: build.", None),
        ("Nexus is installed but cannot start; setup repairs it. ...", None),
        ("", None),
    ])
    def test_setup_outcome(self, text: str, expected: str | None) -> None:
        assert sj.setup_outcome(text) == expected

    def test_a_record_ends_a_run_only_when_it_is_this_runs(self) -> None:
        done = {"mode": "cli-only", "state": "done", "pid": 7}
        assert sj.run_has_ended(done, "cli-only", None) == "done"
        assert sj.run_has_ended(done, "cli-only", 7) is None  # the previous run's record
        assert sj.run_has_ended(done, "local", None) is None  # another mode's
        assert sj.run_has_ended({**done, "state": "running"}, "cli-only", None) is None
        assert sj.run_has_ended({**done, "state": "failed"}, "cli-only", 3) == "failed"
        assert sj.run_has_ended(None, "local", None) is None


class TestSandbox:
    def _env(self, tmp_path: Path, **over):
        args = dict(
            home=tmp_path / "h", project=tmp_path / "p", bin_dir=tmp_path / "b", tmp=tmp_path / "t",
            uv_cache=tmp_path / "c", uv_python_dir="/py", plugin_root=tmp_path / "plugin",
            find_links=tmp_path / "w", proxy="http://127.0.0.1:9", offline=True,
        )
        args.update(over)
        return sj.sandbox_env(**args)

    def test_nothing_ambient_reaches_a_sandbox_process(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("NX_SERVICE_TOKEN", "NX_SERVICE_URL", "VOYAGE_API_KEY", "NEXUS_CONFIG_DIR", "NX_TOOLS_DIR",
                     "UV_TOOL_DIR", "UV_INDEX_URL", "XDG_CONFIG_HOME", "ANTHROPIC_API_KEY"):
            monkeypatch.setenv(name, "ambient")
        env = self._env(tmp_path)
        assert not [k for k in env if k.startswith(("NEXUS_", "XDG_", "VOYAGE", "ANTHROPIC"))]
        assert [k for k in env if k.startswith("NX_")] == ["NX_NO_TELEMETRY"]
        assert set(k for k in env if k.startswith("UV_")) == {
            "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_PYTHON_DOWNLOADS", "UV_NO_PROGRESS", "UV_FIND_LINKS", "UV_OFFLINE",
        }
        assert "ambient" not in env.values()

    def test_the_path_holds_only_the_sandbox_and_the_system(self, tmp_path: Path) -> None:
        parts = self._env(tmp_path)["PATH"].split(":")
        assert parts == [str(tmp_path / "b"), str(tmp_path / "h" / ".local" / "bin"), "/usr/bin", "/bin"]

    def test_offline_means_uv_offline_and_every_proxy_variable_refuses(self, tmp_path: Path) -> None:
        env = self._env(tmp_path)
        assert env["UV_OFFLINE"] == "1" and env["UV_PYTHON_DOWNLOADS"] == "never"
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            assert env[name] == "http://127.0.0.1:9"
        assert env["NO_PROXY"] == "127.0.0.1,localhost,::1"

    def test_the_lockstep_marker_is_the_one_steering_variable_and_only_when_asked(self, tmp_path: Path) -> None:
        env = self._env(tmp_path, lockstep_marker=tmp_path / "marker")
        assert env["NX_LOCKSTEP_MARKER"] == str(tmp_path / "marker")
        assert "NX_LOCKSTEP_MARKER" not in self._env(tmp_path)

    def test_the_seed_run_is_online_and_unproxied(self, tmp_path: Path) -> None:
        env = self._env(tmp_path, offline=False, proxy=None)
        assert "UV_OFFLINE" not in env and "HTTPS_PROXY" not in env


class TestPrune:
    @posix_only("a pruned venv's python links dangle")
    def test_a_pruned_generation_is_the_one_the_launcher_calls_cannot_start(self, tmp_path: Path) -> None:
        lookup = _plugin_module("hooks", "scripts", "_cli_lookup.py")
        venv = tmp_path / "gen"
        (venv / "bin").mkdir(parents=True)
        real = tmp_path / "real-python"
        real.mkdir()
        (venv / "pyvenv.cfg").write_text(f"home = {real}\nversion = 3.12.1\n", encoding="utf-8")
        for name in ("python", "python3"):
            (venv / "bin" / name).symlink_to(real / name)
        for name in ("python", "python3"):
            (real / name).write_text("", encoding="utf-8")
            (real / name).chmod(0o755)
        nx = venv / "bin" / "nx"
        nx.write_text(f"#!{venv}/bin/python\n", encoding="utf-8")
        nx.chmod(0o755)
        assert lookup.cannot_start(str(nx)) is None  # positive control: the unpruned venv starts
        sj.prune_python(venv, tmp_path / "gone")
        reason = lookup.cannot_start(str(nx))
        assert reason is not None and "gone" in reason

    @posix_only("a pruned venv's python links dangle")
    def test_a_gone_home_alone_is_not_a_prune(self, tmp_path: Path) -> None:
        lookup = _plugin_module("hooks", "scripts", "_cli_lookup.py")
        venv = tmp_path / "gen"
        (venv / "bin").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text(f"home = {tmp_path / 'gone'}\n", encoding="utf-8")
        nx = venv / "bin" / "nx"
        nx.write_text("#!/bin/sh\n", encoding="utf-8")
        nx.chmod(0o755)
        assert lookup.cannot_start(str(nx)) is None

    def test_generations_are_the_receipted_directories(self, tmp_path: Path) -> None:
        (tmp_path / "gen-20260101T000000Z").mkdir()
        (tmp_path / "gen-20260101T000000Z" / "nexus-install.json").write_text("{}")
        (tmp_path / "gen-20260102T000000Z").mkdir()  # no receipt: not a generation
        (tmp_path / "current").mkdir()
        assert [g.name for g in sj.generations(tmp_path)] == ["gen-20260101T000000Z"]
        assert sj.generations(tmp_path / "none") == []


class TestWitness:
    def test_the_refusing_proxy_records_what_a_client_asks_for(self) -> None:
        with sj.RefusingProxy() as proxy:
            assert proxy.seen == []
            handler = urllib.request.ProxyHandler({"http": proxy.url})
            opener = urllib.request.build_opener(handler)
            with pytest.raises(urllib.error.HTTPError) as caught:
                opener.open("http://pypi.example.invalid/simple/conexus/", timeout=10)
            assert caught.value.code == 403
            assert proxy.seen == ["GET http://pypi.example.invalid/simple/conexus/"]

    def test_only_the_doctors_pypi_probe_is_tolerated(self) -> None:
        assert sj.unexpected_requests([]) == []
        assert sj.unexpected_requests(["CONNECT pypi.org:443", "CONNECT pypi.org:443"]) == []
        assert sj.unexpected_requests(["CONNECT pypi.org:443", "CONNECT github.com:443"]) == ["CONNECT github.com:443"]
        assert sj.unexpected_requests(["GET http://pypi.org/simple/conexus/"]) == ["GET http://pypi.org/simple/conexus/"]

    def test_an_empty_or_partial_seed_is_a_named_failure(self, tmp_path: Path) -> None:
        problem = sj.seed_problem(tmp_path)
        assert problem is not None and "--seed" in problem and "the engine" in problem and "the two models" in problem
        assert sj.seed_problem(tmp_path / "absent") is not None


class TestTheWrapper:
    def test_it_never_skips_and_scrubs_every_sandbox_process(self) -> None:
        text = WRAPPER.read_text(encoding="utf-8")
        for token in ("pytest.skip", "SKIP", "exit 77", "|| true"):
            assert token not in text, f"the wrapper carries {token!r}: a journey that cannot run must fail"
        assert text.count("env -i") >= 3
        assert "NX_NO_TELEMETRY=1" in text and "UV_OFFLINE=1" in text and "UV_PYTHON_DOWNLOADS=never" in text
        assert "SETUP JOURNEY FAILED" in text

    def test_the_driver_has_no_skip_path_and_fails_on_a_network_attempt(self) -> None:
        text = LIB.read_text(encoding="utf-8")
        assert "pytest.skip" not in text and "SkipTest" not in text
        assert "the network was asked for" in text and "SETUP JOURNEY PASSED" in text
