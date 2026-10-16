# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-228 P2.7 (nexus-sv2wx.20): the pure parts of the setup journey test.

``tests/e2e/setup-journey.sh`` runs the journey itself, on a test host, because it starts the plugin's
hook entries and a PostgreSQL. This file runs everywhere and starts none of that: it pins what the
journey reads and decides with, so a change to ``hooks.json``, ``.mcp.json`` or the stand-in's wording
fails here, in the fast suite, instead of in a half-hour run. It runs no hook entry.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import urllib.error
import urllib.request
import zipfile
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

    def test_the_not_now_marker_is_the_one_the_setup_script_writes(self, tmp_path: Path) -> None:
        setup = _plugin_module("launch", "nx_setup.py")
        assert sj.NOT_NOW_MARKER == setup._MARKER
        # The journey's B7 reads this file in the setup directory; ``_start`` takes the directory, so the real
        # config directory is untouched.
        directory = tmp_path / "setup"
        setup._start(directory, setup.MODE_NOT_NOW, False)
        assert (directory / sj.NOT_NOW_MARKER).is_file()


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
        done = {"mode": "cli-only", "state": "done", "started_at": "2026-10-15T10:00:00Z"}
        assert sj.run_has_ended(done, "cli-only", None) == "done"
        # The previous run's record (the same started_at) is not this run's, whatever its state, even in the
        # same mode: start() writes a new started_at before it returns.
        assert sj.run_has_ended(done, "cli-only", "2026-10-15T10:00:00Z") is None
        assert sj.run_has_ended(done, "cli-only", "2026-10-15T09:00:00Z") == "done"
        assert sj.run_has_ended(done, "local", None) is None  # another mode's
        assert sj.run_has_ended({**done, "state": "running"}, "cli-only", None) is None
        assert sj.run_has_ended({**done, "state": "failed"}, "cli-only", "2026-10-15T09:00:00Z") == "failed"
        assert sj.run_has_ended(None, "local", None) is None

    def test_a_real_start_gives_the_new_run_a_started_at_the_old_one_lacks(self, tmp_path: Path) -> None:
        """The guard stands on ``start`` writing a fresh ``started_at`` before it returns (and on a status
        record carrying it to the end): pinned in the script, not assumed."""
        setup = _plugin_module("launch", "nx_setup.py")
        text = (REPO / "conexus" / "launch" / "nx_setup.py").read_text(encoding="utf-8")
        assert '"started_at": _now()' in text and "write_status(setup, base)" in text
        assert callable(setup._start)


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
        parts = self._env(tmp_path)["PATH"].split(os.pathsep)
        assert parts == [str(tmp_path / "b"), str(tmp_path / "h" / ".local" / "bin"), "/usr/bin", "/bin"]

    def test_offline_means_uv_offline_and_every_proxy_variable_refuses(self, tmp_path: Path) -> None:
        env = self._env(tmp_path)
        assert env["UV_OFFLINE"] == "1" and env["UV_PYTHON_DOWNLOADS"] == "never"
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            assert env[name] == "http://127.0.0.1:9"
        assert env["NO_PROXY"] == "127.0.0.1,localhost,::1"

    def test_the_seed_run_is_online_and_unproxied(self, tmp_path: Path) -> None:
        env = self._env(tmp_path, offline=False, proxy=None)
        assert "UV_OFFLINE" not in env and "HTTPS_PROXY" not in env


@posix_only("the sandbox links uv into its bin directory with a symlink")
class TestLockstep:
    def _box(self, tmp_path: Path):
        uv = tmp_path / "uv"
        uv.write_text("")
        return sj.Sandbox(name="t", root=tmp_path / "box", uv=str(uv), uv_cache=tmp_path / "c", uv_python_dir="/py",
                          wheels=tmp_path / "w", proxy=None, offline=True)

    def test_the_marker_must_hold_the_plugin_version(self, tmp_path: Path) -> None:
        box = self._box(tmp_path)
        with pytest.raises(sj.JourneyFailure, match="left no lockstep marker"):
            sj.check_lockstep_marker(box)
        box.lockstep_marker.parent.mkdir(parents=True)
        box.lockstep_marker.write_text("0.0.1", encoding="utf-8")
        with pytest.raises(sj.JourneyFailure, match="holds '0.0.1'"):
            sj.check_lockstep_marker(box)
        box.lockstep_marker.write_text(sj.plugin_version() + "\n", encoding="utf-8")
        sj.check_lockstep_marker(box)

    def test_the_marker_is_where_the_cli_and_the_hook_read_it(self, tmp_path: Path) -> None:
        assert self._box(tmp_path).lockstep_marker == tmp_path / "box" / "home" / ".config" / "nexus" / "cli_lockstep_marker"

    def test_a_nudge_in_any_hook_output_fails(self, tmp_path: Path) -> None:
        box = self._box(tmp_path)
        entry = sj.Entry("version_lockstep_hook.py", "uv", ("x",))
        sj.check_no_lockstep_nudge(box, [sj.HookResult(entry, 0, "", "", 0.1)])
        said = '{"additionalContext": "The conexus CLI (nx) may be out of lockstep with plugin v1"}'
        with pytest.raises(sj.JourneyFailure, match="out of lockstep"):
            sj.check_no_lockstep_nudge(box, [sj.HookResult(entry, 0, said, "", 0.1)])


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

    def test_the_tolerance_is_one_probe_per_doctor_run(self) -> None:
        probe = "CONNECT pypi.org:443"
        assert sj.proxy_problems([], 0) == []
        assert sj.proxy_problems([probe], 1) == []
        assert sj.proxy_problems([probe, probe, probe], 3) == []
        # Two probes from one run (or one more client asking pypi.org) is not tolerated.
        assert "2 PyPI probes for 1 nx doctor run" in sj.proxy_problems([probe, probe], 1)[0]
        assert sj.proxy_problems([probe], 0), "a probe with no nx doctor run behind it is not the doctor's"
        assert sj.proxy_problems([probe, "CONNECT github.com:443"], 1) == ["CONNECT github.com:443"]

    def test_an_empty_or_partial_seed_is_a_named_failure(self, tmp_path: Path) -> None:
        problem = sj.seed_problem(tmp_path)
        assert problem is not None and "--seed" in problem and "the engine" in problem and "the two models" in problem
        assert sj.seed_problem(tmp_path / "absent") is not None


class TestNetworkWitness:
    """The kernel-level witness's reader: what an ``strace -f -e trace=connect`` trace says about the network."""

    TRACE = "\n".join([
        '101 connect(3, {sa_family=AF_UNIX, sun_path="/var/run/nscd/socket"}, 110) = -1 ENOENT (No such file or directory)',
        '101 connect(4, {sa_family=AF_INET, sin_port=htons(8080), sin_addr=inet_addr("127.0.0.1")}, 16) = 0',
        '102 connect(5, {sa_family=AF_INET, sin_port=htons(5432), sin_addr=inet_addr("127.0.0.53")}, 16) = 0',
        '[pid  103] connect(6, {sa_family=AF_INET6, sin6_port=htons(8080), sin6_flowinfo=htonl(0), inet_pton(AF_INET6, "::1", &sin6_addr), sin6_scope_id=0}, 28 <unfinished ...>',
        '103 <... connect resumed>) = 0',
        '104 connect(7, {sa_family=AF_NETLINK, nl_pid=0, nl_groups=00000000}, 12) = 0',
        '105 connect(8, {sa_family=AF_UNSPEC, sa_data="\\0\\0\\0\\0\\0\\0\\0\\0\\0\\0\\0\\0\\0\\0"}, 16) = 0',
        '106 connect(9, {sa_family=AF_INET6, sin6_port=htons(80), sin6_flowinfo=htonl(0), inet_pton(AF_INET6, "::ffff:127.0.0.1", &sin6_addr), sin6_scope_id=0}, 28) = 0',
    ])

    def test_loopback_unix_and_netlink_stay_on_the_machine(self) -> None:
        connects, foreign, tolerated = sj.trace_summary(self.TRACE)
        assert foreign == [] and tolerated == 0
        assert connects == 7  # the "resumed" line is the same call as its unfinished half

    @pytest.mark.parametrize("line", [
        '201 connect(3, {sa_family=AF_INET, sin_port=htons(443), sin_addr=inet_addr("140.82.112.3")}, 16) = -1 EINPROGRESS',
        '201 connect(3, {sa_family=AF_INET, sin_port=htons(53), sin_addr=inet_addr("192.168.1.1")}, 16) = 0',
        '201 connect(3, {sa_family=AF_INET6, sin6_port=htons(443), sin6_flowinfo=htonl(0), inet_pton(AF_INET6, "2606:4700::6810:84e5", &sin6_addr), sin6_scope_id=0}, 28) = -1 ENETUNREACH',
        '201 connect(3, {sa_family=AF_INET6, sin6_port=htons(443), sin6_flowinfo=htonl(0), inet_pton(AF_INET6, "::ffff:140.82.112.3", &sin6_addr), sin6_scope_id=0}, 28) = 0',
        '201 connect(3, {sa_family=AF_PACKET, sll_protocol=htons(ETH_P_ALL)}, 20) = 0',
        '201 connect(3, 0x7ffd1234, 16) = -1 EFAULT',
        '201 connect(3, {sa_family=AF_INET, sin_port=htons(80)}, 16) = 0',
    ])
    def test_anything_else_is_foreign_and_what_cannot_be_read_fails_closed(self, line: str) -> None:
        connects, foreign, _ = sj.trace_summary(self.TRACE + "\n" + line)
        assert connects == 8 and len(foreign) == 1 and "201 connect" in foreign[0]

    def test_the_engines_link_local_probe_is_tolerated_four_times_and_no_more(self) -> None:
        probe = '300 connect(17, {sa_family=AF_INET, sin_port=htons(80), sin_addr=inet_addr("169.254.169.254")}, 16) = -1 EINPROGRESS'
        four = "\n".join([probe] * 4)
        connects, foreign, tolerated = sj.trace_summary(self.TRACE + "\n" + four)
        assert (connects, foreign, tolerated) == (11, [], 4)
        connects, foreign, tolerated = sj.trace_summary(self.TRACE + "\n" + four + "\n" + probe)
        assert connects == 12 and tolerated == 4 and len(foreign) == 1, "a fifth probe is foreign"
        other_port = probe.replace("htons(80)", "htons(8080)")
        other_ip = probe.replace("169.254.169.254", "169.254.169.253")
        assert len(sj.trace_summary(other_port)[1]) == 1 and len(sj.trace_summary(other_ip)[1]) == 1

    def test_the_command_fails_on_a_foreign_connect_and_on_an_empty_trace(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        good = tmp_path / "good.trace"
        good.write_text(self.TRACE, encoding="utf-8")
        assert sj.check_trace_command(good) == 0
        assert "connects=7 foreign=0 tolerated=0" in capsys.readouterr().out
        bad = tmp_path / "bad.trace"
        bad.write_text(self.TRACE + '\n9 connect(3, {sa_family=AF_INET, sin_port=htons(9), sin_addr=inet_addr("192.0.2.1")}, 16) = -1 EINPROGRESS\n', encoding="utf-8")
        assert sj.check_trace_command(bad) == 1
        out = capsys.readouterr().out
        assert "foreign=1" in out and "192.0.2.1" in out
        empty = tmp_path / "empty.trace"
        empty.write_text("+++ exited with 0 +++\n", encoding="utf-8")
        assert sj.check_trace_command(empty) == 1, "a witness that saw no connect proves nothing"
        assert "no connect call at all" in capsys.readouterr().out
        assert sj.check_trace_command(tmp_path / "absent") == 1

    def test_the_driver_answers_the_wrappers_two_commands(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        trace = tmp_path / "t"
        trace.write_text(self.TRACE, encoding="utf-8")
        assert sj.main(["--check-trace", str(trace)]) == 0
        capsys.readouterr()
        assert sj.main(["--witness-control"]) == 0
        out = capsys.readouterr().out.strip()
        assert out.startswith("ext=") and " loop=0" in out  # the loopback half of the control always connects


class TestTreeWheel:
    @staticmethod
    def _record(files: dict[str, str]) -> str:
        return "".join(f"{path},{digest},10\n" for path, digest in files.items()) + "conexus-1.0.dist-info/RECORD,,\n"

    def test_parse_record_keeps_hashed_entries_only(self) -> None:
        parsed = sj.parse_record(self._record({"nexus/a.py": "sha256=AAA", "nexus/b.py": "sha256=BBB"}))
        assert parsed == {"nexus/a.py": "sha256=AAA", "nexus/b.py": "sha256=BBB"}

    def test_one_differing_file_fails_where_a_release_stable_file_would_not(self) -> None:
        tree = {"nexus/engine_version.py": "sha256=SAME", "nexus/self_cmd.py": "sha256=TREE", "conexus-1.0.dist-info/METADATA": "sha256=M"}
        published = {**tree, "nexus/self_cmd.py": "sha256=PUBLISHED"}
        assert sj.record_mismatches(tree, dict(tree)) == []
        assert sj.record_mismatches(tree, published) == ["nexus/self_cmd.py: hash differs"]
        assert sj.record_mismatches(tree, {k: v for k, v in tree.items() if k != "nexus/self_cmd.py"}) == ["nexus/self_cmd.py: not installed"]
        # Files uv installs that the wheel does not list (INSTALLER, direct_url.json) are not mismatches.
        assert sj.record_mismatches(tree, {**tree, "conexus-1.0.dist-info/INSTALLER": "sha256=I"}) == []
        assert sj.record_mismatches({"conexus-1.0.data/scripts/nx": "sha256=X"}, {}) == []

    def test_the_built_wheel_is_read_from_its_own_record(self, tmp_path: Path) -> None:
        wheel = tmp_path / "conexus-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("conexus-1.0.dist-info/RECORD", self._record({"nexus/a.py": "sha256=AAA"}))
            archive.writestr("nexus/a.py", "x")
        assert sj.built_wheel_record(tmp_path) == {"nexus/a.py": "sha256=AAA"}
        with pytest.raises(sj.JourneyFailure, match="expected one conexus wheel"):
            sj.built_wheel_record(tmp_path / "none")
        (tmp_path / "conexus-2.0-py3-none-any.whl").write_bytes(io.BytesIO().getvalue())
        with pytest.raises(sj.JourneyFailure, match="expected one conexus wheel"):
            sj.built_wheel_record(tmp_path)

    def test_the_generations_record_is_found_under_site_packages(self, tmp_path: Path) -> None:
        site = tmp_path / "gen" / "lib" / "python3.12" / "site-packages" / "conexus-1.0.dist-info"
        site.mkdir(parents=True)
        (site / "RECORD").write_text(self._record({"nexus/a.py": "sha256=AAA"}), encoding="utf-8")
        assert sj.installed_record(tmp_path / "gen") == {"nexus/a.py": "sha256=AAA"}
        with pytest.raises(sj.JourneyFailure, match="expected one conexus dist-info RECORD"):
            sj.installed_record(tmp_path / "other")


class TestSeedIsThisTrees:
    def _seed(self, tmp_path: Path, *, tree: str, engine: str | None, held: str) -> Path:
        seed = tmp_path / "seed"
        (seed / "uvcache").mkdir(parents=True)
        (seed / "uvcache" / "CACHEDIR.TAG").write_text("")
        (seed / "service").mkdir()
        (seed / "service" / ("nexus-service.exe" if os.name == "nt" else "nexus-service")).write_text("", encoding="utf-8")
        (seed / "service" / "nexus-pg-linux-amd64.txz").write_text("")
        (seed / "service" / "nexus-service.meta.json").write_text(json.dumps({"version": held}), encoding="utf-8")
        for model in ("bge-base-en-v1.5", "ms-marco-minilm-l6-v2"):
            (seed / "models" / model / "onnx").mkdir(parents=True)
        record = {"tree_version": tree, "service_files": []}
        if engine is not None:
            record["engine_version"] = engine
        (seed / "seed.json").write_text(json.dumps(record), encoding="utf-8")
        return seed

    def test_a_seed_for_this_tree_and_engine_serves(self, tmp_path: Path) -> None:
        seed = self._seed(tmp_path, tree="7.80.0", engine="0.1.161", held="0.1.161")
        assert sj.seed_problem(seed, tree_version="7.80.0", engine_version="0.1.161") is None

    def test_a_stale_seed_fails_and_names_why(self, tmp_path: Path) -> None:
        seed = self._seed(tmp_path, tree="7.79.0", engine="0.1.160", held="0.1.160")
        problem = sj.seed_problem(seed, tree_version="7.80.0", engine_version="0.1.161")
        assert problem and "stale" in problem and "7.79.0" in problem and "0.1.160" in problem and "--seed" in problem
        only_tree = sj.seed_problem(seed, tree_version="7.80.0", engine_version="0.1.160")
        assert only_tree and "conexus '7.79.0'" in only_tree and "engine" not in only_tree.replace("this tree is", "")
        # A record that predates the engine field is stale too, and so is an engine file that disagrees with its record.
        old = self._seed(tmp_path / "old", tree="7.80.0", engine=None, held="0.1.161")
        assert "None" in (sj.seed_problem(old, tree_version="7.80.0", engine_version="0.1.161") or "")
        lying = self._seed(tmp_path / "lying", tree="7.80.0", engine="0.1.161", held="0.1.150")
        assert "the engine it holds is '0.1.150'" in (sj.seed_problem(lying, tree_version="7.80.0", engine_version="0.1.161") or "")

    def test_the_trees_own_versions_are_what_the_default_compares(self, tmp_path: Path) -> None:
        engine = sj.tree_engine_version()
        assert engine.count(".") == 2 and all(part.isdigit() for part in engine.split("."))
        from nexus.engine_version import REQUIRED_ENGINE_VERSION  # noqa: PLC0415

        assert engine == ".".join(str(n) for n in REQUIRED_ENGINE_VERSION)
        seed = self._seed(tmp_path, tree=sj.plugin_version(), engine=engine, held=engine)
        assert sj.seed_problem(seed) is None


class TestLockstepHook:
    def _hook(self, stdout: str):
        entry = sj.Entry("version_lockstep_hook.py", "uv", ("run", "/p/hooks/scripts/version_lockstep_hook.py"))
        return sj.HookResult(entry, 0, stdout, "", 0.1)

    @posix_only("the sandbox links uv into its bin directory with a symlink")
    def test_silence_is_the_dispatch_witness(self, tmp_path: Path) -> None:
        box = TestLockstep()._box(tmp_path)
        sj.check_lockstep_hook_is_silent(box, [self._hook("")])
        with pytest.raises(sj.JourneyFailure, match="dispatched an upgrade"):
            sj.check_lockstep_hook_is_silent(box, [self._hook('{"additionalContext": "nudge"}')])
        with pytest.raises(sj.JourneyFailure, match="expected one"):
            sj.check_lockstep_hook_is_silent(box, [])  # no lockstep entry in hooks.json: vacuous, so a failure

    @posix_only("the sandbox links uv into its bin directory with a symlink")
    def test_a_lockstep_log_at_the_end_means_an_action_ran(self, tmp_path: Path) -> None:
        box = TestLockstep()._box(tmp_path)
        sj.check_no_lockstep_action_ran(box)
        box.config.mkdir(parents=True)
        (box.config / "lockstep.log").write_text("2026 upgrade_attempt\n", encoding="utf-8")
        with pytest.raises(sj.JourneyFailure, match="detached lockstep action ran"):
            sj.check_no_lockstep_action_ran(box)


class TestTheWrapper:
    def test_it_never_skips_and_scrubs_every_sandbox_process(self) -> None:
        text = WRAPPER.read_text(encoding="utf-8")
        for token in ("pytest.skip", "SKIP", "exit 77", "|| true"):
            assert token not in text, f"the wrapper carries {token!r}: a journey that cannot run must fail"
        assert text.count("env -i") >= 3
        assert "NX_NO_TELEMETRY=1" in text and "UV_OFFLINE=1" in text and "UV_PYTHON_DOWNLOADS=never" in text
        assert "SETUP JOURNEY FAILED" in text

    def test_offline_runs_under_a_kernel_witness_that_fails_when_it_is_missing(self) -> None:
        text = WRAPPER.read_text(encoding="utf-8")
        assert "trace=connect" in text and "sandbox-exec" in text and "--check-trace" in text and "--witness-control" in text
        # Each tool's absence, a host with neither, and a witness that fails its control are named failures.
        for message in ("strace is not installed", "/usr/bin/sandbox-exec is not on this host", "no network witness for",
                        "did not flag a connect to 192.0.2.1", "did not cut an outside connect"):
            assert message in text, message
        assert "_fail" in text and "exit 77" not in text

    def test_the_driver_has_no_skip_path_and_fails_on_a_network_attempt(self) -> None:
        text = LIB.read_text(encoding="utf-8")
        assert "pytest.skip" not in text and "SkipTest" not in text
        assert "the network was asked for" in text and "SETUP JOURNEY PASSED" in text
