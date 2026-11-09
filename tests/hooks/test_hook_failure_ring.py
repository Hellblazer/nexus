# SPDX-License-Identifier: AGPL-3.0-or-later
"""A swallowed hook crash saves its input to a bounded, redacted local ring, and
``nx hook replay`` runs it again with the crash surfacing (nexus-3ceda.3)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus._hook_runtime import failure_ring
from nexus._hook_runtime._io import HookResult, never_fail
from nexus.commands.hook import hook_group
from nexus.hooks import subagent_stop_t1_nudge as nudge
from nexus.mcp import hooks as _hooks
from tests._child_encoding import PYTHON_CHILD_ENCODING
from tests._module_seam import setattr_in

_CRASHING_VERB = (
    "import os\n"
    "from nexus._hook_runtime._io import HookResult\n"
    "def run(payload):\n"
    "    if payload and payload.get('tool_name') == 'Boom':\n"
    "        raise RuntimeError('crashed on ' + payload['tool_name'])\n"
    "    if payload and payload.get('tool_name') == 'Env':\n"
    "        raise RuntimeError('env ' + os.environ.get('NX_PROBE_MODE', '-') + ' cwd ' + os.getcwd())\n"
    "    return HookResult(stdout='fine')\n"
)


def _boom(payload: dict | None) -> HookResult:
    raise RuntimeError(f"tool-tier crash on {payload!r}")


def _raise() -> HookResult:
    raise RuntimeError("deliberate")


def _rec(directory: Path, payload: object = None, **kw: object) -> Path | None:
    kw.setdefault("module", "nexus.hooks.probe")
    kw.setdefault("env", {})
    return failure_ring.record("probe", payload, RuntimeError("x"), directory=directory, **kw)  # type: ignore[arg-type]


def test_a_recorded_entry_is_redacted_and_private(tmp_path: Path) -> None:
    payload = {
        "prompt": "use token=abcdef123456 please",
        "tool_input": {"command": "ls -la", "api_key": "sk-live-1", "Authorization": "Bearer x"},
        "items": [1, True, None, "password: hunter22"],
    }
    env = {"NX_FOO": "1", "NX_API_KEY": "k", "CLAUDE_PROJECT_DIR": "/p", "NEXUS_TOKEN": "t",
           "HOME": "/home/u", "AWS_SECRET": "s", "NX_DB_PASS": "hunter2xx", "NX_DB_ADMIN_PASS": "adm1npw",
           "NX_DB_URL": "postgresql://u:pw123456@h/db", "NX_DB_DSN": "x"}
    path = failure_ring.record("probe", payload, RuntimeError("secret=abcdef123456 leaked"),
                               module="m.v", env=env, directory=tmp_path, now=1_700_000_000.0)
    assert path is not None and path.parent == tmp_path
    entry = failure_ring.load(path)
    assert entry["hook"] == "probe" and entry["module"] == "m.v"
    assert entry["payload"]["prompt"] == "use token=[redacted] please"
    assert entry["payload"]["tool_input"] == {"command": "ls -la", "api_key": "[redacted]",
                                              "Authorization": "[redacted]"}
    assert entry["payload"]["items"] == [1, True, None, "password: [redacted]"]
    assert entry["env"] == {"CLAUDE_PROJECT_DIR": "/p", "NX_FOO": "1"}
    text = path.read_text(encoding="utf-8")
    for secret in ("abcdef123456", "hunter2xx", "adm1npw", "pw123456"):
        assert secret not in text, secret
    assert entry["error_type"] == "RuntimeError"
    assert entry["error"] == "secret=[redacted] leaked"
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
        assert tmp_path.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(("text", "secret"), [
    ('curl -H "Authorization: Bearer abc123SECRETxyz" https://x', "abc123SECRETxyz"),
    ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
    ("export GITHUB_TOKEN=ghp_abcdefghijkl12345 && make", "ghp_abcdefghijkl12345"),
    ("OPENAI_API_KEY=sk-abcdef123456 python run.py", "sk-abcdef123456"),
    ("NX_VOYAGE_API_KEY=pa-zzzzzzzzzz nx index", "pa-zzzzzzzzzz"),
    ("PGPASSWORD=hunter2hunter2 psql", "hunter2hunter2"),
    ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG", "wJalrXUtnFEMI"),
    ("curl -u admin:s3cretpass https://x", "s3cretpass"),
    ("psql postgres://user:pw123456@host/db", "pw123456"),
    ('{"password": "hunter22hunter"}', "hunter22hunter"),
    ("--db-password=letmein99 --verbose", "letmein99"),
    ("key AKIAABCDEFGHIJKLMNOP in text", "AKIAABCDEFGHIJKLMNOP"),
    ('--password="my pass phrase" --user x', "pass phrase"),
    ("DB_PASSWORD='a b c' ./run", "b c"),
    ('mysqldump --password "s3cret value" db', "s3cret value"),
    ("app --token abcdefghijk --verbose", "abcdefghijk"),
    ('Authorization: "Bearer abcDEF123456xyz"', "abcDEF123456xyz"),
    ("authorization = 'Bearer abcDEF123456'", "abcDEF123456"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEAabc\n-----END RSA PRIVATE KEY-----", "MIIEpAIBAAKCAQEAabc"),
    ("Cookie: a=1; session=secretcookie2", "secretcookie2"),
    ("STRIPE_KEY=sk_live_abcdef ./deploy", "sk_live_abcdef"),
    ("nx config set voyage_api_key pa-abcdefghijklmnopqrstuv", "pa-abcdefghijklmnopqrstuv"),
    ('MY_SECRET="correct horse battery staple"', "horse battery"),
])
def test_credentials_in_free_text_are_masked(text: str, secret: str) -> None:
    out = failure_ring.scrub(text)
    assert secret not in out, out
    assert "[redacted]" in out


@pytest.mark.parametrize("text", [
    "git log --oneline -5 && uv run pytest tests/test_x.py -k session_id",
    "the basic structure of the design is documented",
    "author=Bob passed=yes compass=north max_tokens=100",
])
def test_plain_text_survives_the_scrub(text: str) -> None:
    assert failure_ring.scrub(text) == text


@pytest.mark.parametrize("text", ["x" * 400_000, "token" * 80_000, "a:b@" * 100_000, "http://" * 50_000,
                                  ("p" * 63 + "=") * 5000, "bearer " * 60_000],
                         ids=["word", "token-run", "userinfo-run", "scheme-run", "name-run", "bearer-run"])
def test_the_scrub_is_linear_on_hostile_text(text: str) -> None:
    started = time.perf_counter()
    failure_ring.scrub(text)
    assert time.perf_counter() - started < 2.0, "the scrub backtracks: a hang inside a crash handler"


def test_a_huge_exception_message_and_a_wide_dict_record_quickly(tmp_path: Path) -> None:
    wide = {f"k{i}": "v" for i in range(300_000)}
    exc = RuntimeError("word " * 6_000_000)
    started = time.perf_counter()
    path = failure_ring.record("probe", wide, exc, module="nexus.hooks.probe", env={}, directory=tmp_path)
    assert time.perf_counter() - started < 1.5, "the crash handler scrubbed what it should have cut first"
    assert path is not None and path.stat().st_size <= failure_ring.MAX_ENTRY_BYTES
    entry = failure_ring.load(path)
    assert entry["truncated"] is True and "[cut " in entry["error"]


@pytest.mark.parametrize(("key", "secret"), [
    ("privateKey", True), ("bearer", True), ("jwt", True), ("access_key", True), ("sessionKey", True),
    ("Authorization", True), ("api-key", True), ("author", False), ("passed", False), ("compass", False),
    ("session_id", False), ("max_tokens", False), ("tool_input", False),
])
def test_credential_keys_are_matched_by_whole_token(key: str, secret: bool) -> None:
    assert failure_ring._credential_key(key) is secret


def test_a_scalar_under_a_credential_key_keeps_its_type(tmp_path: Path) -> None:
    path = _rec(tmp_path, {"use_token": True, "token": "abc", "password": None, "auth": {"user": "u"}})
    assert path is not None
    assert failure_ring.load(path)["payload"] == {"use_token": True, "token": "[redacted]", "password": None,
                                                  "auth": "[redacted]"}


def test_the_switch_turns_capture_off(tmp_path: Path) -> None:
    ring = tmp_path / "ring"
    for off in ("0", "false", "OFF", "no"):
        assert _rec(ring, {}, env={failure_ring.DISABLE_ENV: off}) is None
    assert not ring.exists()


def test_an_entry_with_no_module_is_not_recorded(tmp_path: Path) -> None:
    assert _rec(tmp_path, {"a": 1}, module=None) is None
    assert failure_ring.entries(tmp_path) == []


def test_recording_never_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")
    assert _rec(blocker / "ring", {"a": object()}) is None


@pytest.mark.parametrize("payload", [
    {"tool_input": {"content": "x" * (failure_ring.MAX_ENTRY_BYTES * 3)}},
    {"tool_response": {"filenames": [f"/repo/src/pkg/module_{i}.py" for i in range(120_000)]}},
    {"rows": [{"id": i, "name": f"n{i}"} for i in range(3000)]},
    {"ints": list(range(300_000))},
], ids=["one-big-string", "wide-list", "many-dicts", "many-ints"])
def test_an_oversized_entry_is_cut_to_fit_and_survives(tmp_path: Path, payload: dict) -> None:
    older = _rec(tmp_path, {"n": 0}, now=1_700_000_000.0)
    path = _rec(tmp_path, payload, now=1_700_000_001.0)
    assert path is not None and path.exists()
    assert path.stat().st_size <= failure_ring.MAX_ENTRY_BYTES
    assert failure_ring.load(path)["truncated"] is True
    assert failure_ring.entries(tmp_path) == [older, path], "a big entry evicted the ring"


def test_a_cut_list_says_how_much_was_cut(tmp_path: Path) -> None:
    path = _rec(tmp_path, {"files": [f"f{i}" for i in range(50_000)]})
    assert path is not None
    files = failure_ring.load(path)["payload"]["files"]
    assert isinstance(files, list) and files[-1].startswith("...[cut ")


def test_the_ring_keeps_the_newest_within_both_caps(tmp_path: Path) -> None:
    paths = [_rec(tmp_path, {"n": i}, now=1_700_000_000.0 + i) for i in range(5)]
    stranger = tmp_path / "keep-me.json"
    stranger.write_text("{}", encoding="utf-8")
    assert failure_ring.prune(tmp_path, max_entries=3) == 2
    assert failure_ring.entries(tmp_path) == paths[2:]
    one = paths[4].stat().st_size
    assert failure_ring.prune(tmp_path, max_bytes=one) == 2
    assert failure_ring.entries(tmp_path) == paths[4:]
    assert failure_ring.prune(tmp_path, max_bytes=0, keep=paths[4]) == 0, "prune deleted the kept entry"
    assert stranger.exists(), "prune touched a file that is not the ring's"


def test_record_prunes_to_the_entry_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(failure_ring, "MAX_ENTRIES", 2)
    for i in range(4):
        _rec(tmp_path, {"n": i}, now=1_700_000_000.0 + i)
    kept = [failure_ring.load(p)["payload"]["n"] for p in failure_ring.entries(tmp_path)]
    assert kept == [2, 3]


def test_a_failed_write_leaves_no_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_replace(*_: object) -> None:
        raise OSError(28, "No space left on device")

    setattr_in(monkeypatch, failure_ring, "os.replace", no_replace)
    assert _rec(tmp_path, {"n": 1}) is None
    assert list(tmp_path.iterdir()) == []


def test_prune_sweeps_an_abandoned_temp_file_but_not_a_write_in_flight(tmp_path: Path) -> None:
    stale = tmp_path / "hookfail-20260101T000000000000-1-probe.json.tmp"
    fresh = tmp_path / "hookfail-20260101T000000000001-2-probe.json.tmp"
    stale.write_text("partial", encoding="utf-8")
    fresh.write_text("in flight", encoding="utf-8")
    old = stale.stat().st_mtime - 3600
    os.utime(stale, (old, old))
    failure_ring.prune(tmp_path)
    assert not stale.exists() and fresh.exists()


def test_load_refuses_what_is_not_an_entry(tmp_path: Path) -> None:
    other = tmp_path / "x.json"
    other.write_text(json.dumps({"hello": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="not a hook-failure entry"):
        failure_ring.load(other)
    other.write_text(json.dumps({"hook": "h", "payload": {}, "format": 99}), encoding="utf-8")
    with pytest.raises(ValueError, match="format 99"):
        failure_ring.load(other)


def test_never_fail_saves_the_payload_and_names_it_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv(failure_ring.DISABLE_ENV, raising=False)
    result = never_fail(_raise, "probe", payload=lambda: {"session_id": "s-1"}, module="m.v")
    assert result == HookResult(crashed=True)
    (path,) = failure_ring.entries(tmp_path / "hook-failures")
    assert failure_ring.load(path)["payload"] == {"session_id": "s-1"}
    assert f"replay with: nx hook replay {path}" in capsys.readouterr().err


def test_a_crash_while_reading_the_payload_still_fails_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))

    def bad_payload() -> object:
        raise OSError("payload gone")

    assert never_fail(_raise, "probe", payload=bad_payload, module="m.v").crashed
    err = capsys.readouterr().err
    assert "swallowed RuntimeError: deliberate" in err and "replay with" not in err


def test_the_command_tier_records_and_replay_raises_the_same_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "probe_verb.py").write_text(_CRASHING_VERB, encoding="utf-8")
    config = tmp_path / "config"
    env = {**os.environ, "PYTHONPATH": str(tmp_path), "NEXUS_CONFIG_DIR": str(config),
           "_NX_HOOK_TEST_VERB_OVERRIDE": json.dumps({"probe": "probe_verb"})}
    env.pop(failure_ring.DISABLE_ENV, None)
    proc = subprocess.run(
        [sys.executable, "-m", "nexus._hook_runtime.entry", "probe"], capture_output=True, text=True,
        encoding=PYTHON_CHILD_ENCODING, input=json.dumps({"tool_name": "Boom", "session_id": "s-9"}),
        env=env, check=False,
    )
    assert proc.returncode == 0 and proc.stdout == "", proc.stderr
    (path,) = failure_ring.entries(config / "hook-failures")
    entry = failure_ring.load(path)
    assert entry["module"] == "probe_verb"
    assert entry["payload"] == {"tool_name": "Boom", "session_id": "s-9"}
    assert f"nx hook replay {path}" in proc.stderr

    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(RuntimeError, match="crashed on Boom"):
        failure_ring.replay(path, module_roots=("probe_verb",))

    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config))
    listed = CliRunner().invoke(hook_group, ["failures", "--json"])
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output.splitlines()[0])["path"] == str(path)


def test_replay_refuses_a_module_that_is_not_a_hook_verb(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    entry = tmp_path / "hookfail-evil.json"
    entry.write_text(json.dumps({"format": 1, "hook": "x", "module": "subprocess",
                                 "payload": {"/usr/bin/touch": 1, str(marker): 2}}), encoding="utf-8")
    with pytest.raises(failure_ring.ReplayRefused, match="not a hook verb"):
        failure_ring.replay(entry)
    out = CliRunner().invoke(hook_group, ["replay", str(entry)])
    assert out.exit_code == 1 and "not a hook verb" in out.output
    assert not marker.exists()


def test_replay_applies_the_recorded_env_and_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "env_verb.py").write_text(_CRASHING_VERB, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    path = _rec(tmp_path / "ring", {"tool_name": "Env"}, module="env_verb", env={"NX_PROBE_MODE": "strict"})
    assert path is not None
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NX_PROBE_MODE", raising=False)
    with pytest.raises(RuntimeError, match=r"env strict cwd .*work"):
        failure_ring.replay(path, module_roots=("env_verb",), recorded_env=True)
    assert "NX_PROBE_MODE" not in os.environ and Path.cwd() == tmp_path
    with pytest.raises(RuntimeError, match=r"env - cwd "):
        failure_ring.replay(path, module_roots=("env_verb",))  # by default the recorded env is not applied


def test_replay_never_applies_an_env_that_steers_code_or_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "env_guard_verb.py").write_text(
        "import os\n"
        "from nexus._hook_runtime._io import HookResult\n"
        "def run(payload):\n"
        "    keys = ('PATH', 'PYTHONPATH', 'VIRTUAL_ENV', 'NEXUS_CONFIG_DIR', 'NX_SERVICE_HOST', 'NX_DB_PASS', 'NX_MODE')\n"
        "    return HookResult(stdout=repr({k: os.environ.get(k) for k in keys}))\n",
        encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    hostile = {"PATH": "/evil/bin", "PYTHONPATH": "/evil", "VIRTUAL_ENV": "/evil", "NEXUS_CONFIG_DIR": "/evil",
               "NX_SERVICE_HOST": "evil.example", "NX_DB_PASS": "x", "NX_MODE": "strict"}
    entry = tmp_path / "hookfail-handed.json"
    entry.write_text(json.dumps({"format": 1, "hook": "x", "module": "env_guard_verb", "payload": {}, "env": hostile}),
                     encoding="utf-8")
    seen = eval(failure_ring.replay(entry, module_roots=("env_guard_verb",), recorded_env=True).stdout)  # noqa: S307 -- our own repr
    assert seen["NX_MODE"] == "strict"
    for key in ("PATH", "PYTHONPATH", "VIRTUAL_ENV", "NEXUS_CONFIG_DIR", "NX_SERVICE_HOST", "NX_DB_PASS"):
        assert seen[key] != hostile[key], key


def test_replay_of_a_verb_that_no_longer_crashes_prints_its_answer(tmp_path: Path) -> None:
    path = _rec(tmp_path, {}, module="nexus.hooks.mcp_connect_check")
    assert path is not None
    out = CliRunner().invoke(hook_group, ["replay", str(path)])
    assert out.exit_code == 0, out.output
    assert "No crash this time" in out.stderr


def test_replay_warns_about_a_cut_entry(tmp_path: Path) -> None:
    path = _rec(tmp_path, {"ints": list(range(100_000))}, module="nexus.hooks.mcp_connect_check")
    assert path is not None
    out = CliRunner().invoke(hook_group, ["replay", str(path)])
    assert "was cut to fit" in out.stderr


def test_the_tool_tier_records_the_run_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    spec = _hooks.HookToolSpec(name="probe_boom", run=_boom, timeout_s=30.0)
    tool = _hooks._make_tool_function(spec)
    tool(session_id="s-2")
    (path,) = failure_ring.entries(tmp_path / "hook-failures")
    entry = failure_ring.load(path)
    assert entry["hook"] == "hook_probe_boom"
    assert entry["module"] == __name__
    assert entry["payload"] == {"session_id": "s-2"}


def test_the_nudge_verbs_own_boundary_records_a_replayable_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))

    def broken(_payload: object) -> HookResult:
        raise RuntimeError("nudge broke")

    monkeypatch.setattr(nudge, "_decide", broken)
    assert nudge.run({"agent_type": "developer"}).crashed
    (path,) = failure_ring.entries(tmp_path / "hook-failures")
    entry = failure_ring.load(path)
    assert entry["module"] == "nexus.hooks.subagent_stop_t1_nudge"
    assert entry["payload"] == {"agent_type": "developer"}


def test_failures_says_so_when_the_ring_is_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    out = CliRunner().invoke(hook_group, ["failures"])
    assert out.exit_code == 0 and "No saved hook failures" in out.output


@pytest.mark.parametrize("text", [
    "-----BEGIN RSA PRIVATE KEY-----\n" * 30_000,
    "x -----BEGIN EC PRIVATE KEY----- y " * 30_000,
], ids=["begin-lines", "begin-no-end"])
def test_pem_masking_is_linear(text: str) -> None:
    started = time.perf_counter()
    out = failure_ring.scrub(text)
    assert time.perf_counter() - started < 2.0
    assert "[redacted]" in out


def test_a_key_left_open_is_still_masked() -> None:
    out = failure_ring.scrub("before -----BEGIN PRIVATE KEY-----\nMIIEsecretbody\n")
    assert "MIIEsecretbody" not in out and out.startswith("before ")


@pytest.mark.parametrize(("text", "secret"), [
    ('{"sessionKey":"abc123xyz","user":"u"}', "abc123xyz"),
    ("privateKey=zzzz9999 rest", "zzzz9999"),
    ('{"accessToken": "tok tok tok"}', "tok tok tok"),
])
def test_camel_case_credential_keys_in_text_are_masked(text: str, secret: str) -> None:
    assert secret not in failure_ring.scrub(text)


@pytest.mark.parametrize("payload", [
    {"word " * 1_200_000: 1},
    {f"k{i}" + "x" * 1_000_000: i for i in range(20)},
    {"lists": [["y" * 15_000] * 190 for _ in range(10)]},
    {"cube": [[[0] * 200] * 200] * 200},
], ids=["one-huge-key", "many-long-keys", "nested-lists", "wide-number-tree"])
def test_the_pre_scrub_cut_bounds_any_payload_shape(tmp_path: Path, payload: dict) -> None:
    started = time.perf_counter()
    path = _rec(tmp_path, payload)
    assert time.perf_counter() - started < 1.5, "the crash handler walked the whole payload"
    assert path is not None and path.stat().st_size <= failure_ring.MAX_ENTRY_BYTES


def test_the_cli_applies_the_recorded_env_only_with_the_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "cli_env_verb.py").write_text(_CRASHING_VERB, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delenv("NX_PROBE_MODE", raising=False)
    path = _rec(tmp_path / "ring", {"tool_name": "Env"}, module="cli_env_verb", env={"NX_PROBE_MODE": "strict"})
    assert path is not None
    monkeypatch.setattr(failure_ring, "REPLAY_MODULE_ROOTS", ("cli_env_verb",))
    plain = CliRunner().invoke(hook_group, ["replay", str(path)])
    flagged = CliRunner().invoke(hook_group, ["replay", "--recorded-env", str(path)])
    assert "env - cwd" in str(plain.exception)
    assert "env strict cwd" in str(flagged.exception)
