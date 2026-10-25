# SPDX-License-Identifier: AGPL-3.0-or-later
"""``web/install.sh``: the one-command installer for macOS and Linux (RDR-228 Phase 3,
nexus-sv2wx.26).

The script is driven the way a user drives it, ``curl ... | sh``: the script is on the shell's
standard input, so the only terminal it can ask on is ``/dev/tty``. A test with a terminal runs
it under ``pty.fork`` (a real controlling terminal); a test without one runs it in a new
session, where ``/dev/tty`` cannot be opened.

Everything the script runs is a stand-in on a temporary ``PATH`` that records its arguments in
one file: ``uv`` (and the installer that creates it), ``nx`` (the shim the bootstrap writes),
``claude``. The tests set a temporary ``HOME`` and never read or print the real one.

The autostart default (nexus-omlpd) is pinned here: with ``--local`` and neither autostart
flag the script asks when a terminal is attached and passes ``--no-autostart`` when none is. It
never lets ``nx init --yes`` register the login unit by default.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from tests._installer_standins import (
    AUTOSTART_QUESTION,
    BOOTSTRAP,
    BOOTSTRAP_SLIM,
    check_the_record_quiets_the_hook,
    CLAUDE_STANDIN as _CLAUDE_STANDIN,
    FOREIGN_NX as _FOREIGN_NX,
    NX_STANDIN as _NX_STANDIN,
    SERVICE_QUESTION,
    UV_STANDIN as _UV_STANDIN,
)

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "web" / "install.sh"

pytestmark = pytest.mark.skipif(os.name == "nt", reason="install.sh is a POSIX shell script")

# Served over file:// as the uv project's own installer: it puts the uv stand-in in the user's
# local bin directory, the place the real one does.
_UV_INSTALLER = """#!/bin/sh
printf 'uv-installer\\n' >> "{rec}"
printf '%s\\n' "$0" >> "{root}/uv-installer-path.log"
ls -l "$0" | cut -c1-10 >> "{root}/uv-installer-mode.log"
if [ -e "{root}/hang-installer" ]; then
    : > "{root}/installer-started"
    sleep 30
fi
if [ -e "{root}/fail-uv-installer" ]; then
    exit 3
fi
dir="${{UV_INSTALL_DIR:-$HOME/.local/bin}}"
mkdir -p "$dir"
cp "{root}/uv-standin" "$dir/uv"
chmod +x "$dir/uv"
"""


def _executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class Box:
    """A throwaway machine: a home directory, a stand-in ``PATH`` directory, a record file."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.bin = root / "bin"
        self.rec = root / "rec.log"
        self.extra_env: dict[str, str] = {}
        self.home.mkdir()
        self.bin.mkdir()
        (root / "tmp").mkdir()
        fill = {"rec": str(self.rec), "root": str(root)}
        _executable(root / "uv-standin", _UV_STANDIN.format(**fill))
        _executable(root / "nx-standin", _NX_STANDIN.format(**fill))
        _executable(root / "claude-standin", _CLAUDE_STANDIN.format(**fill))
        _executable(root / "uv-installer.sh", _UV_INSTALLER.format(**fill))

    # ---- machine state -------------------------------------------------------------------
    def with_uv(self) -> "Box":
        shutil.copy(self.root / "uv-standin", self.bin / "uv")
        return self

    def with_claude(self) -> "Box":
        shutil.copy(self.root / "claude-standin", self.bin / "claude")
        return self

    def with_nx_on_path(self) -> "Box":
        shutil.copy(self.root / "nx-standin", self.bin / "nx")
        return self

    def with_foreign_nx_on_path(self) -> "Box":
        _executable(self.bin / "nx", _FOREIGN_NX)
        return self

    def with_nx_in_local_bin(self) -> "Box":
        """What ``uv tool install conexus`` leaves: ``~/.local/bin/nx``, not on this ``PATH``."""
        _executable(self.home / ".local" / "bin" / "nx", (self.root / "nx-standin").read_text())
        return self

    def with_nx_in_the_generation_layout(self) -> "Box":
        _executable(
            self.home / ".local" / "share" / "nexus" / "tools" / "current" / "bin" / "nx",
            (self.root / "nx-standin").read_text(),
        )
        return self

    def failing(self, what: str) -> "Box":
        (self.root / f"fail-{what}").write_text("")
        return self

    def with_uname(self, system: str) -> "Box":
        """A ``uname`` that says *system*: the platform is a fact the script reads, and a
        stand-in is how a Mac run sees the Linux branch."""
        _executable(self.bin / "uname", f"#!/bin/sh\necho {system}\n")
        return self

    def env_log(self, name: str) -> list[str]:
        path = self.root / name
        return path.read_text().splitlines() if path.exists() else []

    # ---- running -------------------------------------------------------------------------
    def env(self) -> dict[str, str]:
        return {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.home),
            "TMPDIR": str(self.root / "tmp"),
            "TERM": "dumb",
            "NX_INSTALL_UV_URL": f"file://{self.root}/uv-installer.sh",
            **self.extra_env,
        }

    def run(self, *args: str, script: Path = SCRIPT) -> subprocess.CompletedProcess[str]:
        """Piped into ``sh`` with no controlling terminal: the unattended case."""
        with script.open("rb") as stdin:
            return subprocess.run(
                ["sh", "-s", "--", *args], stdin=stdin, capture_output=True, text=True,
                env=self.env(), cwd=self.root, start_new_session=True, timeout=60,
            )

    def run_on_a_terminal(
        self, *args: str, answers: list[tuple[str, str]], before_first_answer=None,
    ) -> tuple[int, str]:
        """Piped into ``sh`` with a controlling terminal, answering each prompt in turn.

        *answers* is ``[(text to wait for, line to send), ...]``. *before_first_answer* is
        called with the output so far, when the first prompt has appeared and before the
        answer is sent: the moment to look for a download that has already started.
        """
        import pty  # noqa: PLC0415 -- no pty module on Windows; the import is reached only on POSIX
        import select  # noqa: PLC0415 -- same

        pid, fd = pty.fork()
        if pid == 0:  # the child: its controlling terminal is the other end of the pty
            os.chdir(self.root)
            os.execve(
                "/bin/sh",
                ["/bin/sh", "-c", 'cat "$0" | sh -s -- "$@"', str(SCRIPT), *args],
                self.env(),
            )
        output = ""

        def pump(until: str | None, timeout: float = 30.0) -> None:
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
                    chunk = b""  # EIO: the child closed its end
                if not chunk:
                    if until is not None:
                        raise AssertionError(f"the script ended before {until!r}; got {output!r}")
                    return
                output += chunk.decode(errors="replace")

        try:
            for index, (expect, reply) in enumerate(answers):
                pump(expect)
                if index == 0 and before_first_answer is not None:
                    before_first_answer(output)
                os.write(fd, (reply + "\n").encode())
            pump(None)
        finally:
            _, status = os.waitpid(pid, 0)
            os.close(fd)
        return os.waitstatus_to_exitcode(status), output.replace("\r\n", "\n")

    # ---- what happened -------------------------------------------------------------------
    def calls(self) -> list[str]:
        return self.rec.read_text().splitlines() if self.rec.exists() else []


@pytest.fixture
def box(tmp_path: Path) -> Box:
    machine = Box(tmp_path)
    # Hermetic: the stand-ins are the only uv, nx and claude the script can find.
    for name in ("uv", "nx", "claude"):
        for directory in ("/usr/bin", "/bin"):
            assert not (Path(directory) / name).exists(), f"{directory}/{name} would leak in"
    return machine


def _nx_calls(machine: Box) -> list[str]:
    return [c for c in machine.calls() if c.startswith("nx ")]


# ---- the script's shape ----------------------------------------------------------------------


def test_the_script_exists() -> None:
    assert SCRIPT.is_file(), f"{SCRIPT} is the published installer"


def test_everything_is_inside_one_function_called_on_the_last_line() -> None:
    """Build item 9. A download cut short must run nothing, so no command sits outside
    ``main`` and the call is the last thing in the file."""
    lines = SCRIPT.read_text().splitlines()
    code = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
    assert code[-1] == 'main "$@"'
    outside = [line for line in code if not line[0].isspace()]
    assert outside == ["main() {", "}", 'main "$@"'], outside


@pytest.mark.parametrize("fraction", [1, 2, 3, 5, 7, 9])
def test_a_download_cut_short_runs_nothing(box: Box, tmp_path: Path, fraction: int) -> None:
    """Cut the script at a line boundary a fraction of the way through, and again just before
    the call: the shell may complain, but no stand-in is started and no file is written."""
    box.with_uv().with_claude()
    lines = SCRIPT.read_text().splitlines(keepends=True)
    cuts = {max(1, len(lines) * fraction // 10), len(lines) - 1}
    for number, cut in enumerate(sorted(cuts)):
        partial = tmp_path / f"cut-{fraction}-{number}.sh"
        partial.write_text("".join(lines[:cut]))
        box.run("--local", "--no-autostart", script=partial)
        assert box.calls() == [], f"cut at line {cut} ran {box.calls()}"
        assert not (box.home / ".local").exists()


def test_shellcheck_is_clean() -> None:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        if os.environ.get("CI"):
            pytest.fail("shellcheck is missing on CI; install it so this gate is not vacuous")
        pytest.skip("shellcheck is not installed")
    result = subprocess.run(
        [shellcheck, "--shell=sh", str(SCRIPT)], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ---- help and flags --------------------------------------------------------------------------


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
    assert not (box.home / ".local").exists()


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


# ---- with a terminal -------------------------------------------------------------------------


def test_with_a_terminal_it_asks_once_and_before_any_download(box: Box) -> None:
    """uv is absent, so the first download is the uv installer; the question is on the screen
    and no stand-in has run when it appears."""
    seen_before_answer: list[list[str]] = []
    status, output = box.run_on_a_terminal(
        answers=[("Choose 1, 2 or 3", "3")],
        before_first_answer=lambda _out: seen_before_answer.append(box.calls()),
    )
    assert status == 0, output
    assert seen_before_answer == [[]], "something ran before the question was answered"
    assert output.count(SERVICE_QUESTION) == 1, output
    assert box.calls()[0] == "uv-installer", box.calls()
    assert _nx_calls(box) == [], "answering 3 starts no service setup"


def test_a_terminal_answer_of_local_asks_the_autostart_question_and_passes_yes(box: Box) -> None:
    box.with_uv()
    status, output = box.run_on_a_terminal(
        answers=[("Choose 1, 2 or 3", "1"), (AUTOSTART_QUESTION, "y")],
    )
    assert status == 0, output
    assert output.count(SERVICE_QUESTION) == 1
    assert output.count(AUTOSTART_QUESTION) == 1
    assert _nx_calls(box) == ["nx init --yes --no-beads-prime"], box.calls()


def test_declining_the_autostart_question_passes_no_autostart(box: Box) -> None:
    box.with_uv()
    status, output = box.run_on_a_terminal(
        answers=[("Choose 1, 2 or 3", "1"), (AUTOSTART_QUESTION, "n")],
    )
    assert status == 0, output
    assert _nx_calls(box) == ["nx init --no-autostart --no-beads-prime"], box.calls()


def test_an_empty_autostart_answer_is_a_no(box: Box) -> None:
    """No login unit without an explicit yes (RDR-174): Enter alone does not register one."""
    box.with_uv()
    status, output = box.run_on_a_terminal(
        answers=[("Choose 1, 2 or 3", "1"), (AUTOSTART_QUESTION, "")],
    )
    assert status == 0, output
    assert _nx_calls(box) == ["nx init --no-autostart --no-beads-prime"], box.calls()


def test_local_with_an_autostart_flag_on_a_terminal_does_not_ask_that_question(box: Box) -> None:
    box.with_uv()
    status, output = box.run_on_a_terminal("--local", "--autostart", answers=[])
    assert status == 0, output
    assert AUTOSTART_QUESTION not in output
    assert SERVICE_QUESTION not in output
    assert _nx_calls(box) == ["nx init --yes --no-beads-prime"], box.calls()


def test_local_alone_on_a_terminal_asks_the_autostart_question_only(box: Box) -> None:
    box.with_uv()
    status, output = box.run_on_a_terminal("--local", answers=[(AUTOSTART_QUESTION, "y")])
    assert status == 0, output
    assert SERVICE_QUESTION not in output
    assert _nx_calls(box) == ["nx init --yes --no-beads-prime"], box.calls()


def test_cli_only_on_a_terminal_asks_nothing(box: Box) -> None:
    box.with_uv()
    status, output = box.run_on_a_terminal("--cli-only", answers=[])
    assert status == 0, output
    assert SERVICE_QUESTION not in output
    assert AUTOSTART_QUESTION not in output
    assert _nx_calls(box) == []


def test_cloud_answer_runs_the_config_wizard(box: Box) -> None:
    box.with_uv()
    status, output = box.run_on_a_terminal(answers=[("Choose 1, 2 or 3", "2")])
    assert status == 0, output
    assert _nx_calls(box) == ["nx config init"], box.calls()


def test_a_bad_answer_is_asked_again_and_the_menu_is_not_repeated(box: Box) -> None:
    box.with_uv()
    status, output = box.run_on_a_terminal(
        answers=[("Choose 1, 2 or 3", "banana"), ("Please answer 1, 2 or 3", "3")],
    )
    assert status == 0, output
    assert output.count(SERVICE_QUESTION) == 1
    assert _nx_calls(box) == []


# ---- with no terminal ------------------------------------------------------------------------


def test_no_terminal_and_no_flag_builds_the_cli_only_and_says_so(box: Box) -> None:
    box.with_uv()
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert BOOTSTRAP in box.calls()
    assert _nx_calls(box) == []
    assert "command-line tool only" in result.stdout
    assert "nx init" in result.stdout and "nx config init" in result.stdout


def test_local_with_no_terminal_and_no_autostart_flag_passes_no_autostart(box: Box) -> None:
    """The default of nexus-omlpd. ``--yes`` would register the login unit; it is never the
    default."""
    box.with_uv()
    result = box.run("--local")
    assert result.returncode == 0, result.stderr
    assert _nx_calls(box) == ["nx init --no-autostart --no-beads-prime"], box.calls()


def test_local_no_autostart_passes_the_matching_flag(box: Box) -> None:
    box.with_uv()
    assert box.run("--local", "--no-autostart").returncode == 0
    assert _nx_calls(box) == ["nx init --no-autostart --no-beads-prime"]


def test_local_autostart_passes_the_matching_flag(box: Box) -> None:
    """``nx init`` has no ``--autostart``; its ``--yes`` is the explicit yes."""
    box.with_uv()
    assert box.run("--local", "--autostart").returncode == 0
    assert _nx_calls(box) == ["nx init --yes --no-beads-prime"]


def test_cloud_runs_the_config_wizard(box: Box) -> None:
    """With no terminal the wizard can only skip its prompts, which it does for what the
    environment already holds."""
    box.with_uv()
    box.extra_env.update({"NX_SERVICE_URL": "https://managed.example.test", "NX_SERVICE_TOKEN": "t"})
    assert box.run("--cloud").returncode == 0
    assert _nx_calls(box) == ["nx config init"]


def test_cli_only_runs_neither(box: Box) -> None:
    box.with_uv()
    result = box.run("--cli-only")
    assert result.returncode == 0, result.stderr
    assert BOOTSTRAP in box.calls()
    assert _nx_calls(box) == []


# ---- the CLI-only record (nexus-sv2wx.31) ----------------------------------------------------
# Whoever ends CLI only leaves the plugin's setup record, so Claude Code's session-start hook does
# not ask about a service at every session. The record is the file the plugin's own setup writes
# for its cli-only mode: setup/status.json under the config directory.


def _config(machine: Box) -> Path:
    return machine.home / ".config" / "nexus"


def test_cli_only_records_the_choice_the_way_the_plugin_does(box: Box) -> None:
    box.with_uv()
    result = box.run("--cli-only")
    assert result.returncode == 0, result.stderr
    setup = _config(box) / "setup"
    assert sorted(p.name for p in setup.iterdir()) == ["status.json"], "no temporary file is left"
    assert stat.S_IMODE(os.stat(setup / "status.json").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(setup).st_mode) == 0o700
    record = json.loads((setup / "status.json").read_text(encoding="utf-8"))
    assert record["message"] == "The Nexus CLI is installed. No service is set up."
    assert record["autostart"] is False


def test_the_record_keeps_the_session_start_hook_quiet(box: Box, tmp_path: Path) -> None:
    """The point of the record: read the way the plugin reads it, and run the real hook."""
    box.with_uv()
    assert box.run("--cli-only").returncode == 0
    check_the_record_quiets_the_hook(_config(box), tmp_path / "hook")


def test_no_terminal_and_no_flag_records_it_too(box: Box) -> None:
    box.with_uv()
    assert box.run().returncode == 0
    assert (_config(box) / "setup" / "status.json").is_file()


def test_pressing_enter_at_the_question_records_it(box: Box) -> None:
    box.with_uv()
    code, _ = box.run_on_a_terminal(answers=[(SERVICE_QUESTION, "")])
    assert code == 0
    record = json.loads((_config(box) / "setup" / "status.json").read_text(encoding="utf-8"))
    assert record["mode"] == "cli-only"


def test_local_and_cloud_record_nothing(box: Box) -> None:
    """A service run leaves the record to the plugin's setup or to nothing at all: it is not
    the CLI-only choice."""
    box.with_uv()
    box.extra_env.update({"NX_SERVICE_URL": "https://managed.example.test", "NX_SERVICE_TOKEN": "t"})
    assert box.run("--local", "--no-autostart").returncode == 0
    assert box.run("--cloud").returncode == 0
    assert not (_config(box) / "setup").exists()


def test_the_record_removes_the_not_now_marker_as_the_plugin_does(box: Box) -> None:
    box.with_uv()
    setup = _config(box) / "setup"
    setup.mkdir(parents=True)
    (setup / "not-now").write_text("2026-01-01T00:00:00Z", encoding="utf-8")
    assert box.run("--cli-only").returncode == 0
    assert not (setup / "not-now").exists()
    assert (setup / "status.json").is_file()


def test_the_record_goes_where_the_plugin_reads_it(box: Box) -> None:
    """NEXUS_CONFIG_DIR first, then NX_CONFIG_DIR, as the plugin's config_dir does; a relative
    value is read against the current directory."""
    box.with_uv()
    box.extra_env.update({"NEXUS_CONFIG_DIR": str(box.root / "first"), "NX_CONFIG_DIR": str(box.root / "second")})
    assert box.run("--cli-only").returncode == 0
    assert (box.root / "first" / "setup" / "status.json").is_file()
    assert not (box.root / "second").exists() and not _config(box).exists()
    box.extra_env.pop("NEXUS_CONFIG_DIR")
    assert box.run("--cli-only").returncode == 0
    assert (box.root / "second" / "setup" / "status.json").is_file()
    box.extra_env.update({"NX_CONFIG_DIR": "relative-config"})
    assert box.run("--cli-only").returncode == 0
    assert (box.root / "relative-config" / "setup" / "status.json").is_file()  # cwd is the sandbox root


def test_a_live_setup_run_keeps_its_record(box: Box) -> None:
    box.with_uv()
    setup = _config(box) / "setup"
    (setup / "lock").mkdir(parents=True)
    (setup / "status.json").write_text('{"state": "running", "mode": "local"}', encoding="utf-8")
    result = box.run("--cli-only")
    assert result.returncode == 0, result.stderr
    assert (setup / "status.json").read_text(encoding="utf-8") == '{"state": "running", "mode": "local"}'


def test_a_record_that_cannot_be_written_is_a_note_not_a_failure(box: Box) -> None:
    """The config directory is a file: nothing can be made under it. The install is done."""
    box.with_uv()
    blocker = box.root / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    box.extra_env["NEXUS_CONFIG_DIR"] = str(blocker)
    result = box.run("--cli-only")
    assert result.returncode == 0, result.stderr
    assert "could not record that no service is set up" in result.stderr
    assert "Done." in result.stdout


def test_autostart_flags_are_ignored_with_a_note_outside_local(box: Box) -> None:
    box.with_uv()
    result = box.run("--cli-only", "--autostart")
    assert result.returncode == 0, result.stderr
    assert "only applies to --local" in result.stdout
    assert _nx_calls(box) == []


# ---- uv, the build and the second run --------------------------------------------------------


def test_uv_absent_is_installed_from_its_own_installer(box: Box) -> None:
    result = box.run("--cli-only")
    assert result.returncode == 0, result.stderr
    calls = box.calls()
    assert calls[0] == "uv-installer", calls
    assert calls[1] == BOOTSTRAP, calls


def test_uv_present_is_left_alone(box: Box) -> None:
    box.with_uv()
    assert box.run("--cli-only").returncode == 0
    assert "uv-installer" not in box.calls()


def test_uv_already_in_the_local_bin_directory_is_found_without_the_installer(box: Box) -> None:
    """uv's installer puts it in ~/.local/bin, which a shell started before the install does not
    have on its PATH."""
    _executable(box.home / ".local" / "bin" / "uv", (box.root / "uv-standin").read_text())
    assert box.run("--cli-only").returncode == 0
    assert "uv-installer" not in box.calls()
    assert BOOTSTRAP in box.calls()


def test_the_second_run_skips_the_build_says_so_and_exits_zero(box: Box) -> None:
    box.with_uv()
    first = box.run("--cli-only")
    assert first.returncode == 0, first.stderr
    assert box.calls().count(BOOTSTRAP) == 1
    second = box.run("--cli-only")
    assert second.returncode == 0, second.stderr
    assert box.calls().count(BOOTSTRAP) == 1, "the second run built again"
    assert "already installed" in second.stdout


def test_the_second_run_with_local_still_runs_init(box: Box) -> None:
    box.with_uv()
    assert box.run("--local", "--no-autostart").returncode == 0
    assert box.run("--local", "--no-autostart").returncode == 0
    assert box.calls().count(BOOTSTRAP) == 1
    assert _nx_calls(box) == ["nx init --no-autostart --no-beads-prime"] * 2


@pytest.mark.parametrize(
    "where", ["with_nx_on_path", "with_nx_in_local_bin", "with_nx_in_the_generation_layout"],
)
def test_a_cli_that_already_resolves_skips_the_build(box: Box, where: str) -> None:
    """On the PATH; in ``~/.local/bin`` (a ``uv tool`` install, or the shim directory) with that
    directory not on the PATH; or in the generation layout."""
    box.with_uv()
    getattr(box, where)()
    result = box.run("--local", "--no-autostart")
    assert result.returncode == 0, result.stderr
    assert BOOTSTRAP not in box.calls()
    assert "already installed" in result.stdout
    assert _nx_calls(box) == ["nx init --no-autostart --no-beads-prime"], "init ran on the CLI that was found"


def test_an_nx_that_is_not_ours_does_not_count(box: Box) -> None:
    """Another program called nx (a build tool) is on the PATH; it does not answer like ours."""
    box.with_uv().with_foreign_nx_on_path()
    result = box.run("--cli-only")
    assert result.returncode == 0, result.stderr
    assert BOOTSTRAP in box.calls()


def test_a_failed_build_stops_the_script_and_starts_no_service(box: Box) -> None:
    box.with_uv().failing("bootstrap")
    result = box.run("--local", "--autostart")
    assert result.returncode != 0
    assert "failed" in result.stderr
    assert _nx_calls(box) == []


def test_a_failed_init_exits_non_zero_and_keeps_the_cli(box: Box) -> None:
    box.with_uv().failing("nx")
    result = box.run("--local", "--no-autostart")
    assert result.returncode != 0
    assert "nx init" in result.stderr
    assert (box.home / ".local" / "bin" / "nx").exists()


# ---- the plugin ------------------------------------------------------------------------------


def test_claude_on_the_path_gets_the_plugin(box: Box) -> None:
    box.with_uv().with_claude()
    assert box.run("--cli-only").returncode == 0
    calls = box.calls()
    assert "claude plugin marketplace add Hellblazer/nexus" in calls
    assert "claude plugin install conexus@nexus-plugins" in calls
    assert calls.index(BOOTSTRAP) < calls.index("claude plugin install conexus@nexus-plugins")


def test_no_claude_means_no_plugin_step_and_a_note(box: Box) -> None:
    box.with_uv()
    result = box.run("--cli-only")
    assert result.returncode == 0, result.stderr
    assert [c for c in box.calls() if c.startswith("claude")] == []
    assert "/plugin install conexus@nexus-plugins" in result.stdout


def test_a_failing_claude_is_a_warning_not_a_failure(box: Box) -> None:
    box.with_uv().with_claude().failing("claude")
    result = box.run("--local", "--no-autostart")
    assert result.returncode == 0, result.stderr
    assert "/plugin install conexus@nexus-plugins" in result.stdout + result.stderr
    assert _nx_calls(box) == ["nx init --no-autostart --no-beads-prime"], "the service step still ran"


# ---- the script's own conduct ---------------------------------------------------------------


def test_a_bin_directory_missing_from_the_users_path_is_pointed_out(box: Box) -> None:
    """The script puts uv's directory on its own PATH to carry on; the user's shell has not
    got it, and the closing note is about the user's shell."""
    box.with_uv()
    result = box.run("--cli-only")
    assert result.returncode == 0, result.stderr
    assert f"{box.home}/.local/bin is not on the PATH of this shell" in result.stdout


def test_the_script_never_writes_outside_the_home_it_was_given(box: Box) -> None:
    """Walks the whole sandbox, not only its top level: a file that leaks into ``tmp`` or into
    the home directory outside ``.local`` is a file the script left behind."""
    box.with_uv().with_claude()
    assert box.run("--local", "--no-autostart").returncode == 0
    ours = {
        "home", "bin", "tmp", "rec.log", "uv-standin", "nx-standin", "claude-standin", "uv-installer.sh",
        "uv-env.log", "nx-env.log", "uv-installer-path.log", "uv-installer-mode.log",
    }
    top = {p.relative_to(box.root).parts[0] for p in box.root.iterdir()}
    assert top <= ours, top
    assert list((box.root / "tmp").iterdir()) == [], "the script left a temporary file in TMPDIR"
    in_home = {p.relative_to(box.home).parts[0] for p in box.home.rglob("*")}
    assert in_home <= {".local"}, in_home
    # Everything under bin is a stand-in this test put there; the script added nothing.
    assert sorted(p.name for p in (box.root / "bin").rglob("*")) == ["claude", "uv"]


def test_it_does_not_use_the_plugins_setup_process() -> None:
    """Build item 8."""
    text = SCRIPT.read_text()
    assert "nx_setup" not in text
    assert "NX_SETUP_SCRIPT" not in text


# ---- the CPU build of torch on Linux (nexus-sv2wx.28) --------------------------------------------


def test_on_linux_the_build_asks_for_the_cpu_build_of_torch(box: Box) -> None:
    """Without it uv installs PyPI's CUDA torch, 14 nvidia-* packages and triton, about 4.5 GB,
    into the throwaway environment (conexus/launch/nx_setup.py build_env, measured 2026-10-15)."""
    box.with_uv().with_uname("Linux")
    assert box.run("--cli-only").returncode == 0
    assert box.env_log("uv-env.log") == ["UV_TORCH_BACKEND=cpu"]


def test_nx_torch_backend_wins_over_cpu_on_linux(box: Box) -> None:
    box.with_uv().with_uname("Linux")
    box.extra_env["NX_TORCH_BACKEND"] = "cu121"
    assert box.run("--cli-only").returncode == 0
    assert box.env_log("uv-env.log") == ["UV_TORCH_BACKEND=cu121"]


def test_an_empty_nx_torch_backend_is_cpu(box: Box) -> None:
    box.with_uv().with_uname("Linux")
    box.extra_env["NX_TORCH_BACKEND"] = ""
    assert box.run("--cli-only").returncode == 0
    assert box.env_log("uv-env.log") == ["UV_TORCH_BACKEND=cpu"]


def test_an_ambient_uv_torch_backend_is_replaced_on_linux(box: Box) -> None:
    """Only NX_TORCH_BACKEND overrides, as in build_env: the throwaway environment and the
    generation must get the same torch."""
    box.with_uv().with_uname("Linux")
    box.extra_env["UV_TORCH_BACKEND"] = "cu121"
    assert box.run("--cli-only").returncode == 0
    assert box.env_log("uv-env.log") == ["UV_TORCH_BACKEND=cpu"]


@pytest.mark.parametrize("system", ["Darwin", "FreeBSD"])
def test_off_linux_the_script_sets_no_torch_backend(box: Box, system: str) -> None:
    box.with_uv().with_uname(system)
    assert box.run("--cli-only").returncode == 0
    assert box.env_log("uv-env.log") == ["UV_TORCH_BACKEND=<unset>"]


# ---- the build asks for the newest conexus ----------------------------------------------------


def test_the_build_requirement_is_conexus_at_latest(box: Box) -> None:
    """A bare ``--from conexus`` reuses the environment uv cached on an earlier run, which can
    predate ``--bootstrap``."""
    box.with_uv()
    assert box.run("--cli-only").returncode == 0
    (build,) = [c for c in box.calls() if c.startswith("uv tool run")]
    assert "--from conexus@latest" in build
    assert build == BOOTSTRAP


# ---- the PDF stack is installed by default; --slim opts out (nexus-2k66t.4.9) -----------------


def test_the_default_build_asks_for_the_pdf_extra(box: Box) -> None:
    """Sam, 2026-10-23: install everything by default. The throwaway environment stays slim
    (it only runs ``--bootstrap``); the GENERATION gets the extra, so a fresh install has PDF
    support as 7.85.0 did."""
    box.with_uv()
    assert box.run("--cli-only").returncode == 0
    (build,) = [c for c in box.calls() if c.startswith("uv tool run")]
    assert build.endswith("nx self install --bootstrap --extras pdf")
    assert build == BOOTSTRAP


@pytest.mark.parametrize("mode", ["--cli-only", "--local", "--cloud"])
def test_every_mode_builds_with_the_pdf_extra_by_default(box: Box, mode: str) -> None:
    box.with_uv().with_claude()
    box.extra_env.update({"NX_SERVICE_URL": "https://example.invalid", "NX_SERVICE_TOKEN": "x"})
    assert box.run(mode).returncode == 0
    assert BOOTSTRAP in box.calls()
    assert BOOTSTRAP_SLIM not in box.calls()


def test_slim_builds_without_the_extra(box: Box) -> None:
    box.with_uv()
    result = box.run("--cli-only", "--slim")
    assert result.returncode == 0, result.stderr
    (build,) = [c for c in box.calls() if c.startswith("uv tool run")]
    assert build == BOOTSTRAP_SLIM
    assert "pdf" not in build


@pytest.mark.parametrize("mode", ["--local", "--cloud"])
def test_slim_is_independent_of_the_service_mode(box: Box, mode: str) -> None:
    box.with_uv()
    box.extra_env.update({"NX_SERVICE_URL": "https://example.invalid", "NX_SERVICE_TOKEN": "x"})
    assert box.run(mode, "--slim").returncode == 0
    assert BOOTSTRAP_SLIM in box.calls() and BOOTSTRAP not in box.calls()


def test_slim_on_a_terminal_asks_the_same_one_question(box: Box) -> None:
    box.with_uv()
    status, output = box.run_on_a_terminal("--slim", answers=[(SERVICE_QUESTION, "3")])
    assert status == 0, output
    assert BOOTSTRAP_SLIM in box.calls()


def test_slim_on_linux_still_asks_for_the_cpu_build_of_torch(box: Box) -> None:
    box.with_uv().with_uname("Linux")
    assert box.run("--cli-only", "--slim").returncode == 0
    assert box.env_log("uv-env.log") == ["UV_TORCH_BACKEND=cpu"]
    assert BOOTSTRAP_SLIM in box.calls()


def test_help_names_slim_and_states_the_default_size(box: Box) -> None:
    text = box.run("--help").stdout
    assert "--slim" in text
    assert "PDF" in text
    assert "0.8 GB" in text and "0.9 GB" in text, "the default local size, macOS and Linux"
    assert "0.45 GB" in text and "0.6 GB" in text, "the default CLI-only and cloud size"
    assert "1.3 GB" not in text and "700 MB" not in text, "the 7.81.0 sizes"
    assert "measured on 7.85.0" in text and "derived" not in text, "the sizes say they were measured, and are not a gate"


def test_the_menu_states_the_default_local_size(box: Box) -> None:
    status, output = box.run_on_a_terminal(answers=[(SERVICE_QUESTION, "3")])
    assert status == 0, output
    assert "about 0.8 GB" in output and "1.3 GB" not in output


# ---- --local means local ----------------------------------------------------------------------


def test_local_forces_local_mode_on_the_one_nx_init_call(box: Box) -> None:
    """``nx init`` is mode-detecting: a configured service address sends it to the managed arm.
    NX_LOCAL=1 is its documented override (init.py _resolve_init_mode)."""
    box.with_uv()
    assert box.run("--local", "--no-autostart").returncode == 0
    assert box.env_log("nx-env.log") == ["init NX_LOCAL=1"]


def test_an_ambient_nx_local_of_zero_does_not_defeat_local(box: Box) -> None:
    box.with_uv()
    box.extra_env["NX_LOCAL"] = "0"
    assert box.run("--local", "--no-autostart").returncode == 0
    assert box.env_log("nx-env.log") == ["init NX_LOCAL=1"]


def test_cloud_does_not_set_nx_local(box: Box) -> None:
    box.with_uv()
    box.extra_env.update({"NX_SERVICE_URL": "https://managed.example.test", "NX_SERVICE_TOKEN": "t"})
    assert box.run("--cloud").returncode == 0
    assert box.env_log("nx-env.log") == ["config NX_LOCAL=<unset>"]


# ---- --cloud with no terminal ----------------------------------------------------------------


def test_cloud_with_no_terminal_and_no_credentials_stops_before_anything_is_downloaded(box: Box) -> None:
    """The wizard would end on EOF after the whole build. Say so first, and say what to set."""
    result = box.run("--cloud")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "NX_SERVICE_URL" in result.stderr and "NX_SERVICE_TOKEN" in result.stderr
    assert box.calls() == [], "something ran before the refusal"


def test_cloud_with_an_address_but_no_token_is_refused_too(box: Box) -> None:
    box.extra_env["NX_SERVICE_URL"] = "https://managed.example.test"
    result = box.run("--cloud")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert box.calls() == []


def test_cloud_with_a_mint_credential_in_the_environment_goes_ahead(box: Box) -> None:
    box.with_uv()
    box.extra_env.update({"NX_SERVICE_URL": "https://managed.example.test", "NX_MINT_TOKEN": "t"})
    assert box.run("--cloud").returncode == 0
    assert _nx_calls(box) == ["nx config init"]


# ---- nx init runs the way setup runs it --------------------------------------------------------


def test_nx_init_is_told_not_to_install_a_beads_file(box: Box) -> None:
    """The plugin's setup passes --no-beads-prime; the installer user agreed to the CLI and the
    service, not to a user-level beads file."""
    box.with_uv()
    assert box.run("--local", "--autostart").returncode == 0
    assert box.run("--local", "--no-autostart").returncode == 0
    assert len(_nx_calls(box)) == 2
    assert all(c.endswith("--no-beads-prime") for c in _nx_calls(box)), _nx_calls(box)


# ---- uv's installer: its directory, its exit status, its temporary file ------------------------


def test_uv_installed_into_uv_install_dir_is_found(box: Box) -> None:
    custom = box.root / "uvdir"
    box.extra_env["UV_INSTALL_DIR"] = str(custom)
    result = box.run("--cli-only")
    assert result.returncode == 0, result.stderr
    assert (custom / "uv").exists()
    assert BOOTSTRAP in box.calls()


def test_a_failing_uv_installer_stops_the_script_and_leaves_no_file(box: Box) -> None:
    box.failing("uv-installer")
    result = box.run("--cli-only")
    assert result.returncode != 0
    assert "uv installer failed" in result.stderr
    assert BOOTSTRAP not in box.calls()
    assert list((box.root / "tmp").iterdir()) == []


def test_the_uv_installer_temporary_file_is_removed_and_private(box: Box) -> None:
    result = box.run("--cli-only")
    assert result.returncode == 0, result.stderr
    assert list((box.root / "tmp").iterdir()) == []
    (path,) = box.env_log("uv-installer-path.log")
    assert Path(path).parent == box.root / "tmp"
    assert Path(path).name.startswith("nx-install-uv.") and len(Path(path).name) > len("nx-install-uv.")
    assert box.env_log("uv-installer-mode.log") == ["-rw-------"]


def test_a_download_that_fails_leaves_no_file(box: Box) -> None:
    box.extra_env["NX_INSTALL_UV_URL"] = f"file://{box.root}/no-such-installer.sh"
    result = box.run("--cli-only")
    assert result.returncode != 0
    assert "could not download the uv installer" in result.stderr
    assert list((box.root / "tmp").iterdir()) == []


def test_the_download_fails_on_an_http_error(box: Box) -> None:
    """curl without -f saves a server's error page and the script would run it."""
    curl = shutil.which("curl")
    assert curl, "curl is needed to run this test"
    _executable(
        box.bin / "curl",
        f'#!/bin/sh\nprintf \'%s\\n\' "$*" >> "{box.root}/curl.log"\nexec {curl} "$@"\n',
    )
    assert box.run("--cli-only").returncode == 0
    (line,) = box.env_log("curl.log")
    flags = [word for word in line.split() if word.startswith("-") and not word.startswith("--")]
    assert any("f" in word for word in flags) or "--fail" in line, line


def test_an_interrupted_uv_download_leaves_no_temporary_file(box: Box) -> None:
    """Ctrl-C while the uv installer runs ends the script; its temporary file must go with it."""
    import signal  # noqa: PLC0415 -- POSIX-only test

    from tests._child_process import kill_group, popen_in_group, signal_group  # noqa: PLC0415

    (box.root / "hang-installer").write_text("")
    with SCRIPT.open("rb") as stdin:
        proc = popen_in_group(
            ["sh", "-s", "--", "--cli-only"], stdin=stdin, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=box.env(), cwd=box.root,
        )
    try:
        deadline = time.monotonic() + 30
        while not (box.root / "installer-started").exists():
            assert time.monotonic() < deadline, "the uv installer never started"
            time.sleep(0.05)
        assert [p.name for p in (box.root / "tmp").iterdir()], "no temporary file while it runs"
        signal_group(proc, signal.SIGTERM)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            kill_group(proc)
        proc.communicate()
    assert list((box.root / "tmp").iterdir()) == []


# ---- NX_INSTALL_UV_URL ------------------------------------------------------------------------


def test_the_uv_url_override_is_named_never_printed(box: Box) -> None:
    result = box.run("--cli-only")
    assert result.returncode == 0, result.stderr
    assert "NX_INSTALL_UV_URL" in result.stdout
    assert "uv-installer.sh" not in result.stdout + result.stderr


def test_a_plain_http_uv_url_is_refused_and_not_printed(box: Box) -> None:
    box.extra_env["NX_INSTALL_UV_URL"] = "http://internal.example.test/secret-path/uv.sh"
    result = box.run("--cli-only")
    assert result.returncode != 0
    assert "NX_INSTALL_UV_URL" in result.stderr
    assert "secret-path" not in result.stdout + result.stderr
    assert box.calls() == []


def test_the_uv_url_override_is_not_mentioned_when_uv_is_already_there(box: Box) -> None:
    box.with_uv()
    result = box.run("--cli-only")
    assert result.returncode == 0, result.stderr
    assert "NX_INSTALL_UV_URL" not in result.stdout + result.stderr


# ---- where the CLI is, and what the script says about it --------------------------------------


def test_a_build_that_leaves_no_nx_names_the_generation_layout(box: Box) -> None:
    box.with_uv()
    (box.root / "skip-nx").write_text("")
    result = box.run("--cli-only")
    assert result.returncode != 0
    assert f"{box.home}/.local/share/nexus/tools/current/bin" in result.stderr


def test_a_tilde_tools_directory_is_expanded(box: Box) -> None:
    box.with_uv()
    _executable(box.home / "custom" / "tools" / "current" / "bin" / "nx", (box.root / "nx-standin").read_text())
    box.extra_env["NX_TOOLS_DIR"] = "~/custom/tools"
    result = box.run("--cli-only")
    assert result.returncode == 0, result.stderr
    assert BOOTSTRAP not in box.calls()


def test_a_relative_tools_directory_is_refused(box: Box) -> None:
    """A relative setting means a different directory in every working directory; the default
    is used instead, so the nx that sits under the relative path is not found."""
    box.with_uv()
    _executable(box.root / "rel" / "tools" / "current" / "bin" / "nx", (box.root / "nx-standin").read_text())
    box.extra_env["NX_TOOLS_DIR"] = "rel/tools"
    assert box.run("--cli-only").returncode == 0
    assert BOOTSTRAP in box.calls()
