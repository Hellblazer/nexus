# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Unit coverage for the shared Claude Code OAuth credential picker
(``tests/e2e/lib/claude_credentials.py``, nexus-galkv.19).

The module lives under ``tests/e2e/lib/``, which is not on ``pythonpath``
(only ``scripts/`` is, per ``pyproject.toml``'s
``[tool.pytest.ini_options]``), so it is loaded by path via
``importlib.util`` — the same pattern ``tests/test_routing_hooks.py`` uses
for ``conexus/hooks/scripts/routing/_lib.py``.

These tests exercise the pure verdict logic only (no real Keychain access,
no subprocess); the picking/enumeration behavior is exercised end to end by
this box's real macOS Keychain via the agent's verify step
(``python3 tests/e2e/lib/claude_credentials.py pick``), not here.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "tests" / "e2e" / "lib" / "claude_credentials.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("claude_credentials", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Register in sys.modules BEFORE exec: the module uses `@dataclass`
    # under `from __future__ import annotations`, which resolves its
    # fields' string annotations via `sys.modules[cls.__module__]` --
    # without this, that lookup returns None and the class body raises.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cc():
    return _load_module()


def _now_ms() -> int:
    return int(time.time() * 1000)


def test_verdict_rejects_a_token_less_husk(cc) -> None:
    """The exact shape measured on this box (nexus-galkv.19 / nexus-qs1g6):
    an item whose claudeAiOauth carries neither an access nor a refresh
    token, with expiresAt at its zero default."""
    husk = {"claudeAiOauth": {"accessToken": "", "refreshToken": "", "expiresAt": 0}}
    ok, why = cc.verdict(husk)
    assert ok is False
    assert "husk" in why


def test_verdict_rejects_expired_with_no_refresh_token(cc) -> None:
    expired = {
        "claudeAiOauth": {
            "accessToken": "at-stale",
            "refreshToken": "",
            "expiresAt": _now_ms() - 60_000,
        }
    }
    ok, why = cc.verdict(expired)
    assert ok is False
    assert "expired" in why


def test_verdict_accepts_expired_access_token_with_a_refresh_token(cc) -> None:
    """An expired access token is still usable when a refresh token is
    present — the CLI can renew it. This is the case a bare wall-clock
    ``expiresAt > now`` comparison (the pre-fix auth-login.sh cache check)
    could NOT distinguish from a permanently dead credential."""
    renewable = {
        "claudeAiOauth": {
            "accessToken": "at-stale",
            "refreshToken": "rt-live",
            "expiresAt": _now_ms() - 60_000,
        }
    }
    ok, why = cc.verdict(renewable)
    assert ok is True
    assert why == ""


def test_verdict_accepts_a_valid_unexpired_credential(cc) -> None:
    valid = {
        "claudeAiOauth": {
            "accessToken": "at-live",
            "refreshToken": "rt-live",
            "expiresAt": _now_ms() + 3_600_000,
        }
    }
    ok, why = cc.verdict(valid)
    assert ok is True
    assert why == ""


def test_verdict_accepts_a_missing_expiresAt_with_a_live_access_token(cc) -> None:
    """No expiresAt field at all (defaults to 0, the falsy/never-expires
    sentinel this verdict treats as "not proven expired") plus a present
    accessToken is usable."""
    no_expiry = {"claudeAiOauth": {"accessToken": "at-live", "refreshToken": ""}}
    ok, why = cc.verdict(no_expiry)
    assert ok is True
    assert why == ""


def test_verdict_rejects_missing_claudeAiOauth_entirely(cc) -> None:
    ok, why = cc.verdict({})
    assert ok is False
    assert "husk" in why


def test_verdict_rejects_none(cc) -> None:
    ok, why = cc.verdict(None)
    assert ok is False


def test_check_file_accepts_a_usable_credential(cc, tmp_path) -> None:
    valid = tmp_path / "creds.json"
    valid.write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": "at",
            "refreshToken": "rt",
            "expiresAt": _now_ms() + 3_600_000,
        }
    }))
    assert cc._cmd_check(str(valid)) == 0


def test_check_file_rejects_a_husk(cc, tmp_path) -> None:
    husk = tmp_path / "husk.json"
    husk.write_text('{"claudeAiOauth": {"accessToken": "", "refreshToken": "", "expiresAt": 0}}')
    assert cc._cmd_check(str(husk)) == 1


def test_check_file_rejects_an_unreadable_path(cc, tmp_path) -> None:
    missing = tmp_path / "nope.json"
    assert cc._cmd_check(str(missing)) == 1


def test_check_file_rejects_invalid_json(cc, tmp_path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    assert cc._cmd_check(str(bad)) == 1


# ===========================================================================
# RDR-219 P1.1: `run [--remote HOST] -- <command>` and `status`
#
# These never touch the real Keychain. A fake `security` (and, for the
# --remote tests, a fake `ssh`) is written to a throwaway directory that is
# prepended to PATH for the duration of each test, exactly as the bead's
# TESTS FIRST section calls for. A fake token, shaped like a real
# `claude setup-token` output (`sk-ant-oat01-...`) but never a real
# credential, stands in for the keychain secret.
# ===========================================================================

_FAKE_TOKEN = "sk-ant-oat01-FAKE00000000000000000000000000000000000000000000"

_FAKE_SECURITY = '''#!/usr/bin/env python3
"""Fake `security` for tests -- never touches the real Keychain."""
import os
import sys

args = sys.argv[1:]
mode = os.environ.get("FAKE_SECURITY_MODE", "present")
if mode == "absent":
    sys.exit(44)
cdat = os.environ.get("FAKE_SECURITY_CDAT", "20260101000000")
token = os.environ.get("FAKE_SECURITY_TOKEN", "sk-ant-oat01-DEFAULTFAKE")
if "-w" in args:
    sys.stdout.write(token + "\\n")
else:
    sys.stdout.write('keychain: "fake"\\n')
    sys.stdout.write("version: 512\\n")
    sys.stdout.write('class: "genp"\\n')
    sys.stdout.write("attributes:\\n")
    sys.stdout.write(f'    "cdat"<timedate>=0x00  "{cdat}Z\\\\000"\\n')
sys.exit(0)
'''

_FAKE_SSH = '''#!/usr/bin/env python3
"""Fake `ssh` for tests -- records its own argv and stdin, connects nowhere."""
import json
import os
import sys

argv_path = os.environ["FAKE_SSH_ARGV_FILE"]
stdin_path = os.environ["FAKE_SSH_STDIN_FILE"]
with open(argv_path, "w") as fh:
    json.dump(sys.argv[1:], fh)
with open(stdin_path, "w") as fh:
    fh.write(sys.stdin.read())
sys.exit(0)
'''


def _write_fake_bin(tmp_path, name: str, script: str):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    path = bin_dir / name
    path.write_text(script)
    path.chmod(0o755)
    return bin_dir


def _prepend_path(monkeypatch, bin_dir) -> None:
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")


def _install_fake_security(monkeypatch, tmp_path, mode="present", cdat="20260101000000", token=_FAKE_TOKEN):
    bin_dir = _write_fake_bin(tmp_path, "security", _FAKE_SECURITY)
    _prepend_path(monkeypatch, bin_dir)
    monkeypatch.setenv("FAKE_SECURITY_MODE", mode)
    monkeypatch.setenv("FAKE_SECURITY_CDAT", cdat)
    monkeypatch.setenv("FAKE_SECURITY_TOKEN", token)


class _ExecSpy:
    """Stands in for `cc._exec`, capturing the argv/env it would have
    handed to `os.execvpe` without ever replacing the test process."""

    def __init__(self, rc: int = 0):
        self.calls: list[tuple[list, dict]] = []
        self.rc = rc

    def __call__(self, command, env):
        self.calls.append((list(command), dict(env)))
        return self.rc


def test_run_absent_token_is_non_zero_and_never_execs(cc, tmp_path, monkeypatch) -> None:
    _install_fake_security(monkeypatch, tmp_path, mode="absent")
    spy = _ExecSpy()
    monkeypatch.setattr(cc, "_exec", spy)
    rc = cc._cmd_run(["true"])
    assert rc != 0
    assert spy.calls == []


def test_run_absent_token_names_the_remedy_on_stderr(cc, tmp_path, monkeypatch, capsys) -> None:
    _install_fake_security(monkeypatch, tmp_path, mode="absent")
    monkeypatch.setattr(cc, "_exec", _ExecSpy())
    cc._cmd_run(["true"])
    captured = capsys.readouterr()
    assert "claude setup-token" in captured.err
    assert cc.AUTOMATION_SERVICE in captured.err
    assert captured.out == ""


def test_run_expired_token_is_non_zero_and_never_execs(cc, tmp_path, monkeypatch, capsys) -> None:
    long_ago = (_NOW() - __import__("datetime").timedelta(days=400)).strftime("%Y%m%d%H%M%S")
    _install_fake_security(monkeypatch, tmp_path, mode="present", cdat=long_ago)
    spy = _ExecSpy()
    monkeypatch.setattr(cc, "_exec", spy)
    rc = cc._cmd_run(["true"])
    assert rc != 0
    assert spy.calls == []
    captured = capsys.readouterr()
    assert "claude setup-token" in captured.err
    assert cc.AUTOMATION_SERVICE in captured.err


def test_run_present_token_execs_command_unchanged_with_token_in_env(cc, tmp_path, monkeypatch) -> None:
    _install_fake_security(monkeypatch, tmp_path, mode="present", cdat=_TODAY_CDAT())
    spy = _ExecSpy(rc=0)
    monkeypatch.setattr(cc, "_exec", spy)
    rc = cc._cmd_run(["mycommand", "arg1", "arg2"])
    assert rc == 0
    assert len(spy.calls) == 1
    argv, env = spy.calls[0]
    assert argv == ["mycommand", "arg1", "arg2"]
    assert _FAKE_TOKEN not in argv
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == _FAKE_TOKEN


def test_run_prints_nothing_of_its_own_on_the_success_path(cc, tmp_path, monkeypatch, capsys) -> None:
    _install_fake_security(monkeypatch, tmp_path, mode="present", cdat=_TODAY_CDAT())
    monkeypatch.setattr(cc, "_exec", _ExecSpy(rc=0))
    cc._cmd_run(["mycommand"])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert _FAKE_TOKEN not in captured.out
    assert _FAKE_TOKEN not in captured.err


def test_run_warns_once_when_anthropic_api_key_is_present(cc, tmp_path, monkeypatch, capsys) -> None:
    """RDR-219 fix round: ANTHROPIC_API_KEY is NOT stripped (harnesses
    that need it pass it through) -- `run` warns exactly once instead,
    since Claude Code's documented auth precedence ranks the key above
    CLAUDE_CODE_OAUTH_TOKEN and a leftover key would silently move the
    child onto API billing."""
    _install_fake_security(monkeypatch, tmp_path, mode="present", cdat=_TODAY_CDAT())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-not-a-real-key")
    spy = _ExecSpy(rc=0)
    monkeypatch.setattr(cc, "_exec", spy)
    cc._cmd_run(["mycommand"])
    _argv, env = spy.calls[0]
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-api03-not-a-real-key"
    err_lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    matching = [line for line in err_lines if "ANTHROPIC_API_KEY" in line]
    assert len(matching) == 1, err_lines
    assert "bills" in matching[0]


def test_run_does_not_warn_when_anthropic_api_key_is_absent(cc, tmp_path, monkeypatch, capsys) -> None:
    _install_fake_security(monkeypatch, tmp_path, mode="present", cdat=_TODAY_CDAT())
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cc, "_exec", _ExecSpy(rc=0))
    cc._cmd_run(["mycommand"])
    assert "ANTHROPIC_API_KEY" not in capsys.readouterr().err


def test_run_forces_docker_rm_and_env_flag_when_wrapping_a_bare_docker_run(cc, tmp_path, monkeypatch) -> None:
    _install_fake_security(monkeypatch, tmp_path, mode="present", cdat=_TODAY_CDAT())
    spy = _ExecSpy(rc=0)
    monkeypatch.setattr(cc, "_exec", spy)
    cc._cmd_run(["docker", "run", "myimage"])
    argv, _env = spy.calls[0]
    assert "--rm" in argv
    assert argv.index("--rm") > argv.index("run")
    assert "-e" in argv
    assert "CLAUDE_CODE_OAUTH_TOKEN" in argv
    assert argv[argv.index("-e") + 1] == "CLAUDE_CODE_OAUTH_TOKEN"


def test_run_does_not_duplicate_an_already_present_docker_rm(cc, tmp_path, monkeypatch) -> None:
    _install_fake_security(monkeypatch, tmp_path, mode="present", cdat=_TODAY_CDAT())
    spy = _ExecSpy(rc=0)
    monkeypatch.setattr(cc, "_exec", spy)
    cc._cmd_run(["docker", "run", "--rm", "-e", "CLAUDE_CODE_OAUTH_TOKEN", "myimage"])
    argv, _env = spy.calls[0]
    assert argv.count("--rm") == 1
    assert argv.count("CLAUDE_CODE_OAUTH_TOKEN") == 1


def test_run_does_not_duplicate_an_already_present_docker_env_flag(cc, tmp_path, monkeypatch) -> None:
    _install_fake_security(monkeypatch, tmp_path, mode="present", cdat=_TODAY_CDAT())
    spy = _ExecSpy(rc=0)
    monkeypatch.setattr(cc, "_exec", spy)
    cc._cmd_run(["docker", "run", "-e", "CLAUDE_CODE_OAUTH_TOKEN", "myimage"])
    argv, _env = spy.calls[0]
    assert argv.count("CLAUDE_CODE_OAUTH_TOKEN") == 1
    assert argv.count("--rm") == 1  # still added, since it was absent


def test_run_leaves_a_non_docker_command_untouched(cc, tmp_path, monkeypatch) -> None:
    _install_fake_security(monkeypatch, tmp_path, mode="present", cdat=_TODAY_CDAT())
    spy = _ExecSpy(rc=0)
    monkeypatch.setattr(cc, "_exec", spy)
    cc._cmd_run(["claude", "--dangerously-skip-permissions"])
    argv, _env = spy.calls[0]
    assert "--rm" not in argv
    assert argv == ["claude", "--dangerously-skip-permissions"]


def test_run_leaves_a_docker_run_nested_in_a_shell_string_untouched(cc, tmp_path, monkeypatch) -> None:
    """A docker invocation hidden inside a shell string is invisible to
    `_ensure_docker_flags` by construction -- command[0] is `bash`, not
    `docker` -- and the docstring says so; this pins that it stays that
    way rather than silently starting to rewrite shell text."""
    _install_fake_security(monkeypatch, tmp_path, mode="present", cdat=_TODAY_CDAT())
    spy = _ExecSpy(rc=0)
    monkeypatch.setattr(cc, "_exec", spy)
    original = ["bash", "-c", "docker run --rm myimage"]
    cc._cmd_run(list(original))
    argv, _env = spy.calls[0]
    assert argv == original


def test_run_unparseable_cdat_is_non_zero_and_never_execs(cc, tmp_path, monkeypatch, capsys) -> None:
    _install_fake_security(monkeypatch, tmp_path, mode="present", cdat="not-a-valid-cdat")
    spy = _ExecSpy()
    monkeypatch.setattr(cc, "_exec", spy)
    rc = cc._cmd_run(["true"])
    assert rc != 0
    assert spy.calls == []
    err = capsys.readouterr().err
    assert "unparseable" in err.lower()
    assert _FAKE_TOKEN not in err


def test_run_missing_security_binary_reports_a_clean_error(cc, tmp_path, monkeypatch, capsys) -> None:
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    spy = _ExecSpy()
    monkeypatch.setattr(cc, "_exec", spy)
    rc = cc._cmd_run(["true"])
    assert rc != 0
    assert spy.calls == []
    err = capsys.readouterr().err
    assert "security" in err.lower()
    assert "not found" in err.lower()


def test_status_missing_security_binary_reports_a_clean_error(cc, tmp_path, monkeypatch, capsys) -> None:
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    rc = cc._cmd_status()
    assert rc != 0
    out = capsys.readouterr().out
    assert "security" in out.lower()
    assert "not found" in out.lower()


def test_status_absent(cc, tmp_path, monkeypatch, capsys) -> None:
    _install_fake_security(monkeypatch, tmp_path, mode="absent")
    rc = cc._cmd_status()
    assert rc == 1
    captured = capsys.readouterr()
    assert "claude setup-token" in captured.out or "claude setup-token" in captured.err
    assert _FAKE_TOKEN not in captured.out
    assert _FAKE_TOKEN not in captured.err


def test_status_unparseable_cdat_exits_3(cc, tmp_path, monkeypatch, capsys) -> None:
    _install_fake_security(monkeypatch, tmp_path, mode="present", cdat="not-a-valid-cdat")
    rc = cc._cmd_status()
    assert rc == 3
    out = capsys.readouterr().out
    assert _FAKE_TOKEN not in out


def test_status_present_and_not_expiring_soon(cc, tmp_path, monkeypatch, capsys) -> None:
    _install_fake_security(monkeypatch, tmp_path, mode="present", cdat=_TODAY_CDAT())
    rc = cc._cmd_status()
    assert rc == 0
    captured = capsys.readouterr()
    assert "warning" not in captured.out.lower()
    assert "warning" not in captured.err.lower()
    assert _FAKE_TOKEN not in captured.out
    assert _FAKE_TOKEN not in captured.err


def test_status_warns_at_30_days_but_not_at_31(cc, tmp_path, monkeypatch, capsys) -> None:
    import datetime as _dt

    now = _dt.datetime(2026, 6, 1, tzinfo=_dt.timezone.utc)

    cdat_30 = (now - _dt.timedelta(days=365 - 30)).strftime("%Y%m%d%H%M%S")
    _install_fake_security(monkeypatch, tmp_path, mode="present", cdat=cdat_30)
    rc = cc._cmd_status(now=now)
    assert rc == 0
    out = capsys.readouterr().out
    assert "warning" in out.lower()

    cdat_31 = (now - _dt.timedelta(days=365 - 31)).strftime("%Y%m%d%H%M%S")
    _install_fake_security(monkeypatch, tmp_path, mode="present", cdat=cdat_31)
    rc = cc._cmd_status(now=now)
    assert rc == 0
    out = capsys.readouterr().out
    assert "warning" not in out.lower()


def test_status_reports_expired_as_exit_2(cc, tmp_path, monkeypatch, capsys) -> None:
    import datetime as _dt

    now = _dt.datetime(2026, 6, 1, tzinfo=_dt.timezone.utc)
    long_ago = (now - _dt.timedelta(days=400)).strftime("%Y%m%d%H%M%S")
    _install_fake_security(monkeypatch, tmp_path, mode="present", cdat=long_ago)
    rc = cc._cmd_status(now=now)
    assert rc == 2
    out = capsys.readouterr().out
    assert _FAKE_TOKEN not in out


# ---------------------------------------------------------------------------
# --remote: RDR-219 fix round -- the HELPER's own remote side reads the
# token (Approach rule 2, as written), never a caller-supplied reader.
# ---------------------------------------------------------------------------


def test_remote_stdin_payload_token_appears_once_right_after_the_read_line(cc) -> None:
    payload = cc._remote_stdin_payload(_FAKE_TOKEN)
    lines = payload.splitlines()
    assert lines[0] == cc._REMOTE_READER_READ_LINE
    assert lines[1] == _FAKE_TOKEN
    assert "export CLAUDE_CODE_OAUTH_TOKEN" in payload
    assert 'exec "$@"' in payload
    assert payload.count(_FAKE_TOKEN) == 1


def test_remote_reader_never_echoes(cc) -> None:
    reader_text = cc._REMOTE_READER_READ_LINE + "\n" + cc._REMOTE_READER_TAIL
    assert "echo" not in reader_text
    assert "set -x" not in reader_text
    assert "printf" not in reader_text


#: A tiny Python probe run as the "<command>" under `bash -s --`. Reads
#: CLAUDE_CODE_OAUTH_TOKEN out of its own environment and compares it
#: against EXPECTED_TOKEN (passed via env, never via argv or the payload,
#: so the probe's OWN invocation carries no token-shaped string either);
#: reads whatever is left on its own stdin, which must be empty when the
#: reader worked correctly. Prints only two boolean lines -- never the
#: token itself, real or fake.
_REMOTE_PROBE_CODE = (
    "import os, sys\n"
    "token = os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', '')\n"
    "expected = os.environ.get('EXPECTED_TOKEN', '')\n"
    "remaining = sys.stdin.read()\n"
    "print('TOKEN_MATCH=' + ('yes' if token == expected else 'no'))\n"
    "print('STDIN_EMPTY=' + ('yes' if remaining == '' else 'no'))\n"
)

_HAS_BASH = shutil.which("bash") is not None


def _run_remote_probe(payload: str, env_extra: dict | None = None):
    env = dict(os.environ)
    env["EXPECTED_TOKEN"] = _FAKE_TOKEN
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", "-s", "--", sys.executable, "-c", _REMOTE_PROBE_CODE],
        input=payload, text=True, capture_output=True, env=env, timeout=10,
    )


@pytest.mark.skipif(not _HAS_BASH, reason="bash not on PATH")
def test_remote_reader_real_shell_delivers_token_and_leaves_child_stdin_empty(cc) -> None:
    """No ssh, no fake process -- a REAL `bash -s --` reads
    `_remote_stdin_payload`'s exact bytes off its stdin and execs the
    probe. Proves the reader mechanism itself (not just its construction)
    with a fake token: the child sees CLAUDE_CODE_OAUTH_TOKEN equal to the
    fake value, and the child's OWN stdin is empty (the token line never
    reaches it) -- the "not polluted" claim in the `run` docstring, shown
    end to end rather than merely asserted about the payload string."""
    payload = cc._remote_stdin_payload(_FAKE_TOKEN)
    proc = _run_remote_probe(payload)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert "TOKEN_MATCH=yes" in lines, lines
    assert "STDIN_EMPTY=yes" in lines, lines
    # Nothing token-shaped leaked anywhere except the deliberate boolean line.
    assert _FAKE_TOKEN not in proc.stdout
    assert _FAKE_TOKEN not in proc.stderr


@pytest.mark.skipif(not _HAS_BASH, reason="bash not on PATH")
def test_remote_reader_broken_order_never_delivers_the_token_and_leaks_it_onto_stdin(cc) -> None:
    """Falsifier for the test above: swap the reader's own order (the
    'export/exec' tail BEFORE the 'read' line and the token -- e.g. what a
    scratch edit reordering `_REMOTE_READER_READ_LINE`/`_REMOTE_READER_TAIL`
    would produce). `exec "$@"` fires immediately, before anything reads
    the token off stdin, so the probe inherits it as unconsumed stdin
    instead of as its environment: the token is NEVER set (TOKEN_MATCH=no)
    and the child's stdin is NOT empty (STDIN_EMPTY=no) -- broken order
    doesn't merely fail to help, it actively leaks the token onto the
    child's stdin, which is exactly the failure mode the correct order
    prevents."""
    broken_payload = cc._REMOTE_READER_TAIL + _FAKE_TOKEN + "\n" + cc._REMOTE_READER_READ_LINE + "\n"
    assert broken_payload != cc._remote_stdin_payload(_FAKE_TOKEN)
    proc = _run_remote_probe(broken_payload)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert "TOKEN_MATCH=no" in lines, lines
    assert "STDIN_EMPTY=no" in lines, lines


def test_remote_ssh_argv_uses_default_remote_shell_excludes_token(cc, tmp_path, monkeypatch) -> None:
    argv_file = tmp_path / "ssh_argv.json"
    stdin_file = tmp_path / "ssh_stdin.txt"
    bin_dir = _write_fake_bin(tmp_path, "ssh", _FAKE_SSH)
    _prepend_path(monkeypatch, bin_dir)
    monkeypatch.setenv("FAKE_SSH_ARGV_FILE", str(argv_file))
    monkeypatch.setenv("FAKE_SSH_STDIN_FILE", str(stdin_file))

    rc = cc._run_remote("fake-host.example", ["echo", "hello"], _FAKE_TOKEN)

    assert rc == 0
    argv = json.loads(argv_file.read_text())
    assert argv == ["fake-host.example", "bash", "-s", "--", "echo", "hello"]
    assert _FAKE_TOKEN not in argv
    for token in argv:
        assert "read" not in token and "export" not in token  # no reader body on argv
    stdin_content = stdin_file.read_text()
    assert stdin_content == cc._remote_stdin_payload(_FAKE_TOKEN)
    assert stdin_content.count(_FAKE_TOKEN) == 1


def test_remote_ssh_argv_honours_a_custom_remote_shell(cc, tmp_path, monkeypatch) -> None:
    """The qwentescence shape from the Phase 0 spike (T2
    nexus_rdr/219-spike-script), expressed as a --remote-shell entry."""
    argv_file = tmp_path / "ssh_argv.json"
    stdin_file = tmp_path / "ssh_stdin.txt"
    bin_dir = _write_fake_bin(tmp_path, "ssh", _FAKE_SSH)
    _prepend_path(monkeypatch, bin_dir)
    monkeypatch.setenv("FAKE_SSH_ARGV_FILE", str(argv_file))
    monkeypatch.setenv("FAKE_SSH_STDIN_FILE", str(stdin_file))

    rc = cc._run_remote(
        "qwentescence",
        ["claude", "--dangerously-skip-permissions"],
        _FAKE_TOKEN,
        remote_shell="wsl -d Ubuntu -u nexus --exec /bin/bash -s --",
    )

    assert rc == 0
    argv = json.loads(argv_file.read_text())
    assert argv == [
        "qwentescence", "wsl", "-d", "Ubuntu", "-u", "nexus", "--exec",
        "/bin/bash", "-s", "--", "claude", "--dangerously-skip-permissions",
    ]
    assert _FAKE_TOKEN not in argv


def test_run_dash_dash_remote_routes_through_run_remote_with_remote_shell(cc, tmp_path, monkeypatch) -> None:
    _install_fake_security(monkeypatch, tmp_path, mode="present", cdat=_TODAY_CDAT())
    calls = []

    def fake_run_remote(host, command, token, remote_shell=None):
        calls.append((host, list(command), token, remote_shell))
        return 0

    monkeypatch.setattr(cc, "_run_remote", fake_run_remote)
    monkeypatch.setattr(cc, "_exec", _ExecSpy())
    rc = cc._cmd_run(["echo", "hi"], remote="fake-host.example", remote_shell="sh -s --")
    assert rc == 0
    assert calls == [("fake-host.example", ["echo", "hi"], _FAKE_TOKEN, "sh -s --")]


def test_parse_run_args_local() -> None:
    cc = _load_module()
    assert cc._parse_run_args(["--", "cmd", "arg"]) == (None, None, ["cmd", "arg"])


def test_parse_run_args_remote() -> None:
    cc = _load_module()
    assert cc._parse_run_args(["--remote", "host1", "--", "cmd", "arg"]) == (
        "host1", None, ["cmd", "arg"],
    )


def test_parse_run_args_remote_with_remote_shell() -> None:
    cc = _load_module()
    assert cc._parse_run_args(
        ["--remote", "host1", "--remote-shell", "sh -s --", "--", "cmd", "arg"]
    ) == ("host1", "sh -s --", ["cmd", "arg"])


def test_parse_run_args_rejects_missing_separator() -> None:
    cc = _load_module()
    assert cc._parse_run_args(["cmd", "arg"]) is None


def test_parse_run_args_rejects_empty_command() -> None:
    cc = _load_module()
    assert cc._parse_run_args(["--"]) is None


def test_parse_run_args_rejects_remote_without_host() -> None:
    cc = _load_module()
    assert cc._parse_run_args(["--remote"]) is None


def test_parse_run_args_rejects_remote_shell_without_value() -> None:
    cc = _load_module()
    assert cc._parse_run_args(["--remote", "h", "--remote-shell"]) is None


def test_main_dispatches_run(cc, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        cc, "_cmd_run",
        lambda command, remote=None, remote_shell=None: calls.append(
            (command, remote, remote_shell)
        ) or 5,
    )
    rc = cc.main([
        "claude_credentials.py", "run", "--remote", "h", "--remote-shell", "sh -s --",
        "--", "cmd", "a",
    ])
    assert rc == 5
    assert calls == [(["cmd", "a"], "h", "sh -s --")]


def test_main_dispatches_status(cc, monkeypatch) -> None:
    monkeypatch.setattr(cc, "_cmd_status", lambda: 7)
    rc = cc.main(["claude_credentials.py", "status"])
    assert rc == 7


def _NOW():
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc)


def _TODAY_CDAT() -> str:
    return _NOW().strftime("%Y%m%d%H%M%S")
