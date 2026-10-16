# SPDX-License-Identifier: AGPL-3.0-or-later
"""``web/install.ps1``: the one-command installer for Windows (RDR-228 Phase 3, nexus-sv2wx.26).

The same cases as ``tests/test_install_sh.py``, driven through PowerShell. The script runs
on Windows PowerShell 5.1, PowerShell 7 on Windows, and PowerShell 7 on macOS and Linux; this
file runs on all of them and finds the interpreter in this order: ``NX_TEST_PWSH``, ``pwsh``,
``powershell``. On Windows the stand-ins are ``.cmd`` files; elsewhere they are the shell
stand-ins of the other file.

Nothing here reaches the real user: the script is given a throwaway home directory
(``USERPROFILE`` and ``HOME``), the uv installer is a recording stand-in served over ``file:``,
and no stand-in writes to the registry or the user's PATH.

The terminal cases run two ways. Everywhere, ``NX_INSTALL_ASSUME_TERMINAL=1`` treats standard
input as the terminal and the answers are piped in. Where ``pty`` exists, the script also runs
under a real controlling terminal with no seam, which is the only run that sees the terminal
detection and the order of the question against the first download.

The autostart default (nexus-omlpd) is pinned here as in the shell file: with ``--local`` and
neither autostart flag the script asks when a terminal is attached and passes
``--no-autostart`` when none is.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from tests._installer_standins import (
    BOOTSTRAP,
    CLAUDE_STANDIN as _CLAUDE_STANDIN,
    FOREIGN_NX as _FOREIGN_NX,
    NX_STANDIN as _NX_STANDIN,
    SERVICE_QUESTION,
    UV_STANDIN as _UV_STANDIN,
)

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "web" / "install.ps1"
WINDOWS = os.name == "nt"
EXT = ".cmd" if WINDOWS else ""
# A Windows machine's execution policy can refuse a script file; text run by iex is not a file.
POLICY = ["-ExecutionPolicy", "Bypass"] if WINDOWS else []



def _interpreter() -> str | None:
    explicit = os.environ.get("NX_TEST_PWSH")
    if explicit:
        return explicit
    return shutil.which("pwsh") or shutil.which("powershell")


PWSH = _interpreter()
needs_pwsh = pytest.mark.skipif(PWSH is None, reason="no PowerShell on this machine")

# ---- Windows stand-ins ---------------------------------------------------------------------
_CMD_UV = """@echo off
>> "{rec}" echo uv %*
if "%1"=="tool" if "%2"=="run" (
    if exist "{root}\\fail-bootstrap" (
        echo stand-in uv: the build failed 1>&2
        exit /b 1
    )
    if not exist "%USERPROFILE%\\.local\\bin" mkdir "%USERPROFILE%\\.local\\bin"
    copy /y "{root}\\nx-standin.cmd" "%USERPROFILE%\\.local\\bin\\nx.cmd" >nul
)
exit /b 0
"""
_CMD_NX = """@echo off
if "%1"=="--version" (
    echo nx, version 9.9.9
    exit /b 0
)
>> "{rec}" echo nx %*
if exist "{root}\\fail-nx" exit /b 1
exit /b 0
"""
_CMD_FOREIGN_NX = "@echo off\r\necho 21.0.0\r\n"
_CMD_CLAUDE = """@echo off
>> "{rec}" echo claude %*
if exist "{root}\\fail-claude" exit /b 1
exit /b 0
"""

# The uv installer, served over file: and run by a child PowerShell: it records itself and puts
# the uv stand-in where the real one puts uv.
_UV_INSTALLER = """\
Add-Content -LiteralPath '{rec}' -Value 'uv-installer'
$dir = Join-Path '{home}' '.local'
$dir = Join-Path $dir 'bin'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Copy-Item -LiteralPath '{root}{sep}uv-standin{ext}' -Destination (Join-Path $dir 'uv{ext}')
if ($IsLinux -or $IsMacOS) {{ & chmod +x (Join-Path $dir 'uv') }}
"""


def _executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n" if WINDOWS else "\n").encode())
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class Box:
    """A throwaway machine: a home directory, a stand-in ``PATH`` directory, a record file."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.bin = root / "bin"
        self.rec = root / "rec.log"
        self.home.mkdir()
        self.bin.mkdir()
        (root / "tmp").mkdir()
        fill = {"rec": str(self.rec), "root": str(root)}
        if WINDOWS:
            _executable(root / "uv-standin.cmd", _CMD_UV.format(**fill))
            _executable(root / "nx-standin.cmd", _CMD_NX.format(**fill))
            _executable(root / "claude-standin.cmd", _CMD_CLAUDE.format(**fill))
        else:
            _executable(root / "uv-standin", _UV_STANDIN.format(**fill))
            _executable(root / "nx-standin", _NX_STANDIN.format(**fill))
            _executable(root / "claude-standin", _CLAUDE_STANDIN.format(**fill))
        installer = _UV_INSTALLER.format(
            rec=self.rec, home=self.home, root=root, sep=os.sep, ext=EXT,
        )
        (root / "uv-installer.ps1").write_text(installer)

    # ---- machine state ---------------------------------------------------------------
    def _install(self, standin: str, into: Path, name: str) -> "Box":
        into.mkdir(parents=True, exist_ok=True)
        shutil.copy(self.root / f"{standin}{EXT}", into / f"{name}{EXT}")
        return self

    def with_uv(self) -> "Box":
        return self._install("uv-standin", self.bin, "uv")

    def with_claude(self) -> "Box":
        return self._install("claude-standin", self.bin, "claude")

    def with_nx_on_path(self) -> "Box":
        return self._install("nx-standin", self.bin, "nx")

    def with_foreign_nx_on_path(self) -> "Box":
        _executable(self.bin / f"nx{EXT}", _CMD_FOREIGN_NX if WINDOWS else _FOREIGN_NX)
        return self

    def with_nx_in_local_bin(self) -> "Box":
        return self._install("nx-standin", self.home / ".local" / "bin", "nx")

    def with_nx_in_the_generation_layout(self) -> "Box":
        layout = self.home / ".local" / "share" / "nexus" / "tools" / "current" / "bin"
        return self._install("nx-standin", layout, "nx")

    def failing(self, what: str) -> "Box":
        (self.root / f"fail-{what}").write_text("")
        return self

    # ---- running ---------------------------------------------------------------------
    def env(self, *, assume_terminal: bool = False) -> dict[str, str]:
        if WINDOWS:
            system = os.environ.get("SystemRoot", r"C:\Windows")
            path = ";".join(
                [str(self.bin), rf"{system}\System32", rf"{system}\System32\WindowsPowerShell\v1.0", system]
            )
            env = {"SystemRoot": system, "USERPROFILE": str(self.home), "TEMP": str(self.root / "tmp"),
                   "TMP": str(self.root / "tmp"), "ComSpec": rf"{system}\System32\cmd.exe",
                   "PATHEXT": ".COM;.EXE;.BAT;.CMD"}
        else:
            path = f"{self.bin}:/usr/bin:/bin"
            env = {"TMPDIR": str(self.root / "tmp"), "TERM": "dumb"}
        env.update({
            "PATH": path,
            "HOME": str(self.home),
            "NX_INSTALL_UV_URL": (self.root / "uv-installer.ps1").as_uri(),
            "DOTNET_SYSTEM_GLOBALIZATION_INVARIANT": "1",
            "POWERSHELL_TELEMETRY_OPTOUT": "1",
            "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        })
        if assume_terminal:
            env["NX_INSTALL_ASSUME_TERMINAL"] = "1"
        return env

    def run(
        self, *args: str, stdin: str | None = None, script: Path = SCRIPT,
    ) -> subprocess.CompletedProcess[str]:
        """``powershell -File install.ps1 ...`` with no console. *stdin* is the answers when
        given, and then the terminal seam is on."""
        assert PWSH is not None
        return subprocess.run(
            [PWSH, "-NoProfile", *POLICY, "-File", str(script), *args],
            input=stdin if stdin is not None else "",
            capture_output=True, text=True, env=self.env(assume_terminal=stdin is not None),
            cwd=self.root, timeout=180,
        )

    def run_on_a_terminal(
        self, *args: str, answers: list[tuple[str, str]], before_first_answer=None,
    ) -> tuple[int, str]:
        """The script text piped to ``Invoke-Expression`` under a real controlling terminal,
        the way ``irm | iex`` runs it. POSIX only."""
        import pty  # noqa: PLC0415 -- no pty module on Windows; the import is reached only on POSIX
        import select  # noqa: PLC0415 -- same

        assert PWSH is not None
        command = (
            f"& ([scriptblock]::Create((Get-Content -Raw -LiteralPath '{SCRIPT}'))) "
            f"{' '.join(args)}; exit $LASTEXITCODE"
        )
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(self.root)
            os.execve(PWSH, [PWSH, "-NoProfile", "-Command", command], self.env())
        output = ""

        def pump(until: str | None, timeout: float = 90.0) -> None:
            nonlocal output
            deadline = time.monotonic() + timeout
            while until is None or until not in output:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise AssertionError(f"waited for {until!r}; got {output!r}")
                ready, _, _ = select.select([fd], [], [], min(left, 1.0))
                if not ready:
                    continue
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    if until is not None:
                        raise AssertionError(f"the script ended before {until!r}; got {output!r}")
                    return
                text = chunk.decode(errors="replace")
                # .NET asks the terminal where the cursor is (ESC [ 6 n) before it reads a
                # line, and waits for the answer; a real terminal always gives one.
                for _ in range(text.count("\x1b[6n")):
                    os.write(fd, b"\x1b[1;1R")
                output += text.replace("\x1b[6n", "")

        try:
            for index, (expect, reply) in enumerate(answers):
                pump(expect)
                if index == 0 and before_first_answer is not None:
                    before_first_answer(output)
                time.sleep(0.3)
                os.write(fd, (reply + "\n").encode())
            pump(None)
        finally:
            _, status = os.waitpid(pid, 0)
            os.close(fd)
        return os.waitstatus_to_exitcode(status), output.replace("\r\n", "\n")

    # ---- what happened ---------------------------------------------------------------
    def calls(self) -> list[str]:
        if not self.rec.exists():
            return []
        return [line.strip() for line in self.rec.read_text().splitlines() if line.strip()]


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def _nx_calls(machine: Box) -> list[str]:
    return [c for c in machine.calls() if c.startswith("nx ")]


# ---- the interpreter is there where it has to be ----------------------------------------------


def test_a_powershell_is_present_where_it_must_be() -> None:
    """The cases below skip when there is no PowerShell. On CI and on Windows a skip would turn
    this whole file into a pass that checked nothing."""
    if PWSH is None and (os.environ.get("CI") or WINDOWS):
        pytest.fail("no PowerShell was found; set NX_TEST_PWSH or install pwsh")


# ---- the script's shape (no interpreter needed) -----------------------------------------------


def test_the_script_exists() -> None:
    assert SCRIPT.is_file(), f"{SCRIPT} is the published installer"


def test_everything_is_inside_one_function_called_on_the_last_line() -> None:
    """A download cut short must run nothing: no command sits outside ``Install-Nexus`` and
    the call is the last thing in the file."""
    lines = SCRIPT.read_text().splitlines()
    code = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
    assert code[-1] == "Install-Nexus -ArgumentList $args"
    # Lines at column 0, leaving out the here-string of the help text.
    inside_help = False
    top_level: list[str] = []
    for line in code:
        if line.startswith("        Say @'"):
            inside_help = True
            continue
        if inside_help:
            if line == "'@":
                inside_help = False
            continue
        if not line[0].isspace():
            top_level.append(line)
    assert top_level == ["function Install-Nexus {", "}", "Install-Nexus -ArgumentList $args"], top_level


def test_it_never_exits_unless_it_was_run_as_a_file() -> None:
    """``exit`` in text that ``iex`` runs closes the user's window."""
    exits = [n for n, line in enumerate(SCRIPT.read_text().splitlines(), 1) if line.strip().startswith("if ($PSCommandPath) { exit")]
    assert len(exits) == 1
    assert [line for line in SCRIPT.read_text().splitlines() if line.lstrip().startswith("exit ")] == []


def test_it_does_not_use_the_plugins_setup_process() -> None:
    text = SCRIPT.read_text()
    assert "nx_setup" not in text
    assert "NX_SETUP_SCRIPT" not in text


def test_it_never_edits_the_user_path_itself() -> None:
    """The uv installer and the bootstrap do that. A second edit here would be an unreviewed
    write to the registry."""
    text = SCRIPT.read_text()
    for forbidden in ("SetEnvironmentVariable", "HKCU", "Set-ItemProperty", "[Environment]::SetEnvironment"):
        assert forbidden not in text, forbidden


# ---- help and flags --------------------------------------------------------------------------


@needs_pwsh
def test_help_states_the_autostart_default_and_installs_nothing(box: Box) -> None:
    """nexus-omlpd: the default is stated in the script's own help text."""
    result = box.run("--help")
    assert result.returncode == 0, result.stderr
    text = result.stdout
    for flag in ("--local", "--cloud", "--cli-only", "--autostart", "--no-autostart"):
        assert flag in text, flag
    assert "never registered without an explicit yes" in text
    assert "--no-autostart" in text.rsplit("With no terminal", 1)[1], "the no-terminal default is named"
    assert box.calls() == []


@needs_pwsh
@pytest.mark.parametrize(
    "args",
    [
        ("--local", "--cloud"),
        ("--cloud", "--cli-only"),
        ("--autostart", "--no-autostart"),
        ("--frobnicate",),
    ],
)
def test_bad_flags_stop_before_anything_is_downloaded(box: Box, args: tuple[str, ...]) -> None:
    result = box.run(*args)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert box.calls() == []


@needs_pwsh
def test_single_dash_flags_are_accepted_too(box: Box) -> None:
    """PowerShell users type ``-Local``; the same flags work in either spelling."""
    box.with_uv()
    assert box.run("-Local", "-NoAutostart").returncode == 0
    assert _nx_calls(box) == ["nx init --no-autostart"]


# ---- the terminal, through the seam (every platform) ------------------------------------------


@needs_pwsh
def test_answering_3_builds_the_cli_and_starts_no_service(box: Box) -> None:
    box.with_uv()
    result = box.run(stdin="3\n")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout.count(SERVICE_QUESTION) == 1
    assert BOOTSTRAP in box.calls()
    assert _nx_calls(box) == []


@needs_pwsh
@pytest.mark.parametrize(
    ("answer", "expected"), [("y", "nx init --yes"), ("n", "nx init --no-autostart"), ("", "nx init --no-autostart")],
    ids=["yes", "no", "empty-is-no"],
)
def test_local_answer_then_the_autostart_question(box: Box, answer: str, expected: str) -> None:
    """No login unit without an explicit yes (RDR-174): Enter alone does not register one."""
    box.with_uv()
    result = box.run(stdin=f"1\n{answer}\n")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout.count(SERVICE_QUESTION) == 1
    assert "Start the service at login?" in result.stdout
    assert _nx_calls(box) == [expected], box.calls()


@needs_pwsh
def test_local_alone_with_a_terminal_asks_the_autostart_question_only(box: Box) -> None:
    box.with_uv()
    result = box.run("--local", stdin="y\n")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert SERVICE_QUESTION not in result.stdout
    assert _nx_calls(box) == ["nx init --yes"]


@needs_pwsh
def test_an_autostart_flag_with_a_terminal_asks_nothing_about_it(box: Box) -> None:
    box.with_uv()
    result = box.run("--local", "--autostart", stdin="")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "Start the service at login?" not in result.stdout
    assert _nx_calls(box) == ["nx init --yes"]


@needs_pwsh
def test_cloud_answer_runs_the_config_wizard(box: Box) -> None:
    box.with_uv()
    assert box.run(stdin="2\n").returncode == 0
    assert _nx_calls(box) == ["nx config init"]


@needs_pwsh
def test_a_bad_answer_is_asked_again_and_the_menu_is_not_repeated(box: Box) -> None:
    box.with_uv()
    result = box.run(stdin="banana\n3\n")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout.count(SERVICE_QUESTION) == 1
    assert "Please answer 1, 2 or 3." in result.stdout


@needs_pwsh
def test_cli_only_with_a_terminal_asks_nothing(box: Box) -> None:
    box.with_uv()
    result = box.run("--cli-only", stdin="")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert SERVICE_QUESTION not in result.stdout
    assert _nx_calls(box) == []


# ---- the terminal, for real (POSIX) ----------------------------------------------------------

posix_terminal = pytest.mark.skipif(WINDOWS, reason="pty.fork does not exist on Windows")


@needs_pwsh
@posix_terminal
def test_with_a_real_terminal_it_asks_once_and_before_any_download(box: Box) -> None:
    """uv is absent, so the first download is the uv installer; the question is on the screen
    and nothing has run when it appears. No seam: this is the script's own terminal detection."""
    seen: list[list[str]] = []
    status, output = box.run_on_a_terminal(
        answers=[("Choose 1, 2 or 3 [3]:", "3")], before_first_answer=lambda _out: seen.append(box.calls()),
    )
    assert status == 0, output
    assert seen == [[]], "something ran before the question was answered"
    assert output.count(SERVICE_QUESTION) == 1, output
    assert box.calls()[0] == "uv-installer", box.calls()
    assert BOOTSTRAP in box.calls()
    assert _nx_calls(box) == []


@needs_pwsh
@posix_terminal
def test_with_a_real_terminal_local_asks_the_autostart_question(box: Box) -> None:
    box.with_uv()
    status, output = box.run_on_a_terminal(
        answers=[("Choose 1, 2 or 3 [3]:", "1"), ("[y/N]:", "y")],
    )
    assert status == 0, output
    assert _nx_calls(box) == ["nx init --yes"], box.calls()


@needs_pwsh
@posix_terminal
def test_no_terminal_is_seen_when_input_is_redirected(box: Box) -> None:
    """The unattended default, with the seam off: piped input is not a terminal."""
    box.with_uv()
    result = box.run()
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "command-line tool only" in result.stdout
    assert _nx_calls(box) == []


# ---- with no terminal ------------------------------------------------------------------------


@needs_pwsh
def test_no_terminal_and_no_flag_builds_the_cli_only_and_says_so(box: Box) -> None:
    box.with_uv()
    result = box.run()
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert BOOTSTRAP in box.calls()
    assert _nx_calls(box) == []
    assert "command-line tool only" in result.stdout
    assert "nx init" in result.stdout and "nx config init" in result.stdout


@needs_pwsh
def test_local_with_no_terminal_and_no_autostart_flag_passes_no_autostart(box: Box) -> None:
    """The default of nexus-omlpd. ``--yes`` would register the login unit; it is never the
    default."""
    box.with_uv()
    result = box.run("--local")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _nx_calls(box) == ["nx init --no-autostart"], box.calls()


@needs_pwsh
@pytest.mark.parametrize(
    ("flag", "expected"), [("--no-autostart", "nx init --no-autostart"), ("--autostart", "nx init --yes")],
)
def test_local_with_an_autostart_flag_passes_the_matching_flag(box: Box, flag: str, expected: str) -> None:
    box.with_uv()
    assert box.run("--local", flag).returncode == 0
    assert _nx_calls(box) == [expected]


@needs_pwsh
def test_cloud_runs_the_config_wizard(box: Box) -> None:
    box.with_uv()
    assert box.run("--cloud").returncode == 0
    assert _nx_calls(box) == ["nx config init"]


@needs_pwsh
def test_cli_only_runs_neither(box: Box) -> None:
    box.with_uv()
    assert box.run("--cli-only").returncode == 0
    assert BOOTSTRAP in box.calls()
    assert _nx_calls(box) == []


@needs_pwsh
def test_autostart_flags_are_ignored_with_a_note_outside_local(box: Box) -> None:
    box.with_uv()
    result = box.run("--cli-only", "--autostart")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "only applies to --local" in result.stdout
    assert _nx_calls(box) == []


# ---- uv, the build and the second run --------------------------------------------------------


@needs_pwsh
def test_uv_absent_is_installed_from_its_own_installer(box: Box) -> None:
    result = box.run("--cli-only")
    assert result.returncode == 0, (result.stdout, result.stderr)
    calls = box.calls()
    assert calls[0] == "uv-installer", calls
    assert calls[1] == BOOTSTRAP, calls


@needs_pwsh
def test_uv_present_is_left_alone(box: Box) -> None:
    box.with_uv()
    assert box.run("--cli-only").returncode == 0
    assert "uv-installer" not in box.calls()


@needs_pwsh
def test_uv_already_in_the_local_bin_directory_is_found_without_the_installer(box: Box) -> None:
    box._install("uv-standin", box.home / ".local" / "bin", "uv")
    assert box.run("--cli-only").returncode == 0
    assert "uv-installer" not in box.calls()
    assert BOOTSTRAP in box.calls()


@needs_pwsh
def test_the_second_run_skips_the_build_says_so_and_exits_zero(box: Box) -> None:
    box.with_uv()
    assert box.run("--cli-only").returncode == 0
    assert box.calls().count(BOOTSTRAP) == 1
    second = box.run("--cli-only")
    assert second.returncode == 0, (second.stdout, second.stderr)
    assert box.calls().count(BOOTSTRAP) == 1, "the second run built again"
    assert "already installed" in second.stdout


@needs_pwsh
def test_the_second_run_with_local_still_runs_init(box: Box) -> None:
    box.with_uv()
    assert box.run("--local", "--no-autostart").returncode == 0
    assert box.run("--local", "--no-autostart").returncode == 0
    assert box.calls().count(BOOTSTRAP) == 1
    assert _nx_calls(box) == ["nx init --no-autostart"] * 2


@needs_pwsh
@pytest.mark.parametrize(
    "where", ["with_nx_on_path", "with_nx_in_local_bin", "with_nx_in_the_generation_layout"],
)
def test_a_cli_that_already_resolves_skips_the_build(box: Box, where: str) -> None:
    """On the PATH; in ``.local/bin`` (a ``uv tool`` install) with that directory not on the
    PATH; or in the generation layout."""
    box.with_uv()
    getattr(box, where)()
    result = box.run("--local", "--no-autostart")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert BOOTSTRAP not in box.calls()
    assert "already installed" in result.stdout
    assert _nx_calls(box) == ["nx init --no-autostart"]


@needs_pwsh
def test_an_nx_that_is_not_ours_does_not_count(box: Box) -> None:
    box.with_uv().with_foreign_nx_on_path()
    result = box.run("--cli-only")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert BOOTSTRAP in box.calls()


@needs_pwsh
def test_a_failed_build_stops_the_script_and_starts_no_service(box: Box) -> None:
    box.with_uv().failing("bootstrap")
    result = box.run("--local", "--autostart")
    assert result.returncode != 0
    assert "failed" in result.stderr
    assert _nx_calls(box) == []


@needs_pwsh
def test_a_failed_init_exits_non_zero_and_keeps_the_cli(box: Box) -> None:
    box.with_uv().failing("nx")
    result = box.run("--local", "--no-autostart")
    assert result.returncode != 0
    assert "nx init" in result.stderr
    assert (box.home / ".local" / "bin" / f"nx{EXT}").exists()


# ---- the plugin ------------------------------------------------------------------------------


@needs_pwsh
def test_claude_on_the_path_gets_the_plugin(box: Box) -> None:
    box.with_uv().with_claude()
    assert box.run("--cli-only").returncode == 0
    calls = box.calls()
    assert "claude plugin marketplace add Hellblazer/nexus" in calls
    assert "claude plugin install conexus@nexus-plugins" in calls
    assert calls.index(BOOTSTRAP) < calls.index("claude plugin install conexus@nexus-plugins")


@needs_pwsh
def test_no_claude_means_no_plugin_step_and_a_note(box: Box) -> None:
    box.with_uv()
    result = box.run("--cli-only")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert [c for c in box.calls() if c.startswith("claude")] == []
    assert "/plugin install conexus@nexus-plugins" in result.stdout


@needs_pwsh
def test_a_failing_claude_is_a_warning_not_a_failure(box: Box) -> None:
    box.with_uv().with_claude().failing("claude")
    result = box.run("--local", "--no-autostart")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "/plugin install conexus@nexus-plugins" in result.stdout + result.stderr
    assert _nx_calls(box) == ["nx init --no-autostart"], "the service step still ran"


# ---- conduct ---------------------------------------------------------------------------------


@needs_pwsh
def test_a_download_cut_short_runs_nothing(box: Box, tmp_path: Path) -> None:
    """Cut the script at a line boundary part of the way through, and again just before the
    call: PowerShell reports a parse error, and no stand-in is started."""
    box.with_uv().with_claude()
    lines = SCRIPT.read_text().splitlines(keepends=True)
    for number, cut in enumerate(sorted({len(lines) // 4, len(lines) // 2, 3 * len(lines) // 4, len(lines) - 1})):
        partial = tmp_path / f"cut-{number}.ps1"
        partial.write_text("".join(lines[:cut]))
        box.run("--local", "--no-autostart", script=partial)
        assert box.calls() == [], f"cut at line {cut} ran {box.calls()}"
        assert not (box.home / ".local" / "bin").exists()
        assert not (box.home / ".local" / "share" / "nexus").exists()


@needs_pwsh
def test_run_as_iex_text_it_reports_through_lastexitcode_and_does_not_exit(box: Box) -> None:
    """The ``irm | iex`` shape: the text runs in the caller's session, so it must not call
    ``exit``. A marker printed after it proves the session survived."""
    box.with_uv().failing("bootstrap")
    assert PWSH is not None
    command = (
        f"Invoke-Expression (Get-Content -Raw -LiteralPath '{SCRIPT}'); "
        "Write-Host \"SURVIVED code=$LASTEXITCODE\""
    )
    result = subprocess.run(
        [PWSH, "-NoProfile", "-Command", command], input="", capture_output=True, text=True,
        env=box.env(), cwd=box.root, timeout=180,
    )
    assert "SURVIVED code=1" in result.stdout, (result.stdout, result.stderr)


@needs_pwsh
def test_a_directory_missing_from_the_users_path_is_pointed_out(box: Box) -> None:
    """The script puts uv's directory on its own PATH to carry on; the window the user types
    in afterwards has not got it, and the closing note is about that window."""
    box.with_uv()
    result = box.run("--cli-only")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "is not on the PATH of this window" in result.stdout


@needs_pwsh
def test_the_script_never_writes_outside_the_home_it_was_given(box: Box) -> None:
    box.with_uv().with_claude()
    assert box.run("--local", "--no-autostart").returncode == 0
    written = {p.name for p in box.root.iterdir()}
    allowed_prefixes = ("home", "bin", "tmp", "rec.log", "uv-standin", "nx-standin", "claude-standin", "uv-installer")
    assert all(name.startswith(allowed_prefixes) for name in written), written
