# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shape and behaviour pins for the unattended Phase 5 gate driver (nexus-f9bgu.53).

``tests/e2e/windows-guest/run-gate.sh`` and ``driver/*.ps1`` run the RDR-224
Phase 5 gate end to end in the Hyper-V guest. No PowerShell runs in CI or on the
laptop, so the PowerShell is held by text pins (the same method as
``tests/test_windows_phase5_gate_script.py``); the bash entry script is also run
against stand-ins for ``ssh``, ``scp``, ``security`` and ``launch.sh`` so its
exit code is checked as behaviour: 0 only on a PASSED verdict line.

Nothing here reaches a host, a keychain or a credential: the stand-ins are the
first thing on ``PATH`` in the child, and the harness asserts that before it
runs the script.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO: Path = Path(__file__).resolve().parent.parent
GUEST_DIR: Path = REPO / "tests" / "e2e" / "windows-guest"
DRIVER: Path = GUEST_DIR / "driver"
RUN_GATE: Path = GUEST_DIR / "run-gate.sh"
GATE: Path = REPO / "tests" / "e2e" / "windows-phase5-gate.ps1"

PW_SCRIPTS = ("restore.ps1", "guest-run.ps1")
NO_PW_SCRIPTS = ("hostcheck.ps1", "end.ps1")
INNER_SCRIPTS = ("inst.ps1", "setup-run.ps1", "gaterun.ps1", "waitv.ps1")
ALL_PS = PW_SCRIPTS + NO_PW_SCRIPTS + INNER_SCRIPTS


def _read(name: str) -> str:
    return (DRIVER / name).read_text(encoding="utf-8")


def _code_lines(text: str) -> list[str]:
    """Lines that are not whole-line comments."""
    return [ln for ln in text.splitlines() if not ln.strip().startswith("#")]


def _sh() -> str:
    return RUN_GATE.read_text(encoding="utf-8")


def test_the_driver_has_exactly_the_scripts_this_file_knows() -> None:
    assert {p.name for p in DRIVER.glob("*")} == set(ALL_PS)
    assert RUN_GATE.stat().st_mode & stat.S_IXUSR, "run-gate.sh must be executable"
    for name in ALL_PS:
        assert _read(name).startswith("# SPDX-License-Identifier: AGPL-3.0-or-later"), name
    assert _sh().startswith("#!/usr/bin/env bash\n# SPDX-License-Identifier: AGPL-3.0-or-later")


# ---- credentials ------------------------------------------------------------


def test_the_password_travels_only_in_the_keychain_to_ssh_pipe() -> None:
    sh = _sh()
    code = "\n".join(_code_lines(sh))
    # exactly one place invokes security, and it pipes straight into ssh -> powershell
    assert len(re.findall(r"\bsecurity\b", code)) == 1
    assert re.search(
        r'security find-generic-password -a "\$GUSER" -s "\$KEYCHAIN_SERVICE" -w \\\n'
        r'\s*\| ssh "\$\{SSH_OPTS\[@\]\}" "\$HOST" "powershell -NoProfile -ExecutionPolicy Bypass -File ',
        code,
    ), "the keychain read must be piped directly into ssh powershell -File"
    # never captured: no command substitution or assignment around security, no read -s
    assert not re.search(r"\$\(\s*security|`security|=\s*\$\(.*find-generic-password", code)
    assert not re.search(r"\bread\s+-s\b|\bPASSWORD\b|\bGPW\b|\bpw=", code)
    # no tracing or transcripts anywhere a script reads the password
    for text in [sh, *(_read(n) for n in ALL_PS)]:
        assert not re.search(r"set\s+-[a-z]*x|xtrace|-Verbose|Start-Transcript|VerbosePreference", "\n".join(_code_lines(text)))
    # the automation token stays inside launch.sh
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in sh and "claude_credentials" not in sh


_PW_LINE_SHAPES = (
    re.compile(r"^\$pw = \[Console\]::In\.ReadLine\(\)$"),
    re.compile(r"^if \(-not \$pw\) \{ throw 'nothing on stdin' \}$"),
    re.compile(
        r"^\$cred = New-Object PSCredential\(\$GuestUser, \(ConvertTo-SecureString \$pw -AsPlainText -Force\)\); \$pw = \$null$"
    ),
)


@pytest.mark.parametrize("name", PW_SCRIPTS)
def test_a_password_reading_script_reads_line_one_and_does_nothing_else_with_it(name: str) -> None:
    code = _code_lines(_read(name))
    pw_lines = [ln.strip() for ln in code if "$pw" in ln]
    assert len(pw_lines) == 3, pw_lines
    for ln in pw_lines:
        assert any(shape.match(ln) for shape in _PW_LINE_SHAPES), f"$pw used in an unexpected way: {ln!r}"
    assert sum("ReadLine" in ln for ln in code) == 1
    # the credential object is only handed to the guest connection
    for ln in (ln.strip() for ln in code if "$cred" in ln):
        assert (
            ln.startswith("$cred = New-Object PSCredential(")
            or re.search(r"-Credential \$cred\b", ln)
        ), f"$cred used in an unexpected way: {ln!r}"
    # nothing writes anything out of those variables
    text = _read(name)
    assert not re.search(r"(Out-File|Set-Content|Add-Content|Export-|Write-Host|Write-Output)[^\n]*\$(pw|cred)\b", text)
    assert not re.search(r"\$(pw|cred)\b[^\n]*(>>?|\| *(Out-File|Set-Content|Add-Content|Tee-Object))", text)


@pytest.mark.parametrize("name", NO_PW_SCRIPTS + INNER_SCRIPTS)
def test_scripts_that_need_no_password_never_read_stdin_or_build_a_credential(name: str) -> None:
    text = _read(name)
    assert not re.search(r"Console\]::In|ReadLine|PSCredential|ConvertTo-SecureString|-Credential\b|\$pw\b", text)


def test_the_guest_is_reached_only_by_the_two_password_scripts() -> None:
    for name in ALL_PS:
        text = _read(name)
        reaches = bool(re.search(r"New-PSSession|Invoke-Command -VMName", text))
        assert reaches == (name in PW_SCRIPTS), name


# ---- restore: the named checkpoint only --------------------------------------


def test_no_script_takes_deletes_or_renames_a_checkpoint() -> None:
    forbidden = re.compile(
        r"Checkpoint-VM|Remove-VMSnapshot|Remove-VMCheckpoint|Rename-VMSnapshot|Rename-VMCheckpoint|"
        r"Set-VMSnapshot|Export-VMSnapshot|Convert-VHD|Remove-VM\b|Set-VM\b",
        re.I,
    )
    for text in [_sh(), *(_read(n) for n in ALL_PS)]:
        assert not forbidden.search(text)
    restores = [(n, ln.strip()) for n in ALL_PS for ln in _code_lines(_read(n)) if "Restore-VM" in ln]
    assert restores == [
        ("restore.ps1", "Restore-VMSnapshot -VMName $vm -Name $cp -Confirm:$false -ErrorAction Stop")
    ]
    restore = _read("restore.ps1")
    assert "$cp = $Checkpoint" in restore
    # the checkpoint name is a parameter: no script spells one out, and bash only as the override default
    for name in ALL_PS:
        assert "clean-baseline" not in _read(name)
    assert len(re.findall(r"clean-baseline", _sh())) == 1  # only the override default
    assert 'CHECKPOINT="${NX_GUEST_CHECKPOINT:-clean-baseline-2026-10-06}"' in _sh()


def test_the_restore_refuses_a_guest_that_is_not_off_before_it_restores() -> None:
    restore = _read("restore.ps1")
    refuse = restore.index("if ($v.State -ne 'Off')")
    assert refuse < restore.index("Restore-VMSnapshot")
    assert "exit 3" in restore[refuse:restore.index("Restore-VMSnapshot")]


def test_the_network_adapter_is_sampled_after_the_restore_and_added_when_missing() -> None:
    restore = "\n".join(_code_lines(_read("restore.ps1")))  # a commented-out call does not count
    after = restore[restore.index("Restore-VMSnapshot"):]
    # sampled for 15 s: the restored configuration reads stale for a few seconds
    assert re.search(r"for \(\$i = 0; \$i -lt 5; \$i\+\+\) \{ \"nic count \$\{i\}x3s after the restore = \$\(Nics\)\"; Start-Sleep 3 \}", after)
    sample = after.index("Start-Sleep 3 }")
    ensure_off = after.index("EnsureNic 'after the restore, VM Off'")
    start = after.index("Start-VM -Name $vm")
    ensure_started = after.index("EnsureNic 'after Start-VM'")
    in_wait = after.index('EnsureNic "in the network wait, iteration $i"')
    assert sample < ensure_off < start < ensure_started < in_wait
    fn = restore[restore.index("function EnsureNic"):restore.index("$v = Get-VM")]
    assert "if ($n -eq 0) { Add-VMNetworkAdapter -VMName $vm -SwitchName 'Default Switch' -ErrorAction Stop;" in fn
    # the network is proved by a real request to a package index, bounded
    assert "https://pypi.org/simple/" in restore and "$i -lt 60" in restore
    assert "exit 4" in restore


# ---- the parse check comes before the installs -------------------------------


def test_the_gate_script_is_parsed_on_the_host_and_on_the_guest_before_any_install() -> None:
    for name in ("hostcheck.ps1", "restore.ps1"):
        text = _read(name)
        assert "[System.Management.Automation.Language.Parser]::ParseFile(" in text, name
    assert "exit 5" in _read("hostcheck.ps1"), "hostcheck must stop on a parse error"
    assert "$rc = 5" in _read("restore.ps1"), "restore must stop on a parse error"
    host = _read("hostcheck.ps1")
    assert host.index("ParseFile") < host.index("exit 5") < host.index("'NXG-RESULT: ok'")
    # the gate script is among what the host parses, and the restore parses it on the guest
    assert "Join-Path $RunDir 'stage'" in host and "-Filter '*.ps1'" in host
    restore = _read("restore.ps1")
    assert restore.index("ParseFile") < restore.index("exit $rc") < restore.index("'NXG-RESULT: ok'")
    assert "$pe.Count -ne 1 -or $pe[0] -ne 0" in restore
    sh = _sh()
    steps = [sh.index(s) for s in ("run_step 00 hostcheck", "run_step 01 restore", "run_step 02 install")]
    assert steps == sorted(steps), "hostcheck (host parse), restore (guest parse), then install"
    # and a step that did not report ok stops the run
    assert 'if [[ "$marker" -eq 1 ]] && ! grep -q \'^NXG-RESULT: ok\' "$log"; then' in sh
    # the staged gate script is the file the laptop hashed
    assert 'grep -qF "gate sha256=$gate_sha" "$out/01-restore.txt" || die 1' in sh


def test_powershell_5_1_only_syntax() -> None:
    for name in ALL_PS:
        for ln in _code_lines(_read(name)):
            assert not re.search(r"\?\?|\?\.|\s&&\s|\s\|\|\s|\bForEach-Object\s+-Parallel\b", ln), (name, ln)


# ---- installs, setup task, bounded waits ------------------------------------


def test_installs_follow_the_passing_run() -> None:
    inst = _read("inst.ps1")
    assert "uv tool install --python 3.12" in inst
    assert inst.count("--python 3.12") == 3  # wheel, PyPI, PyPI retry
    assert '"conexus==$Version"' in inst
    assert "https://astral.sh/uv/install.ps1" in inst and "https://claude.ai/install.ps1" in inst
    assert "winget install --id Git.Git -e --source winget" in inst
    assert '.local\\bin\\claude.exe" plugin marketplace add Hellblazer/nexus' in inst
    assert '.local\\bin\\claude.exe" plugin install conexus@nexus-plugins' in inst
    order = [inst.index(s) for s in ("install.ps1 | iex'", "uv tool install", "claude.ai/install.ps1", "winget install", "plugin install")]
    assert order == sorted(order)
    assert "-Wheel" not in inst.split("param(")[0].replace("-Wheel is given", "")  # only documented, not a switch
    assert re.search(r"\[Parameter\(Mandatory = \$true\)\]\[string\]\$Version", inst)
    # the wheel is installed from the guest's nx-gate folder, and its hash is printed
    assert '"$b\\$Wheel"' in inst and "wheel sha256=" in inst
    # an unattended run needs a machine result
    assert inst.rstrip().endswith("else { 'NXG-RESULT: ok' }")
    assert "does not report version $Version" in inst


def test_setup_runs_as_an_interactive_one_shot_task_with_a_bounded_wait() -> None:
    setup = _read("setup-run.ps1")
    assert "-LogonType Interactive" in setup
    assert "Register-ScheduledTask -TaskName 'nxgate-setup'" in setup and "Start-ScheduledTask -TaskName 'nxgate-setup'" in setup
    assert "Unregister-ScheduledTask -TaskName 'nxgate-setup'" in setup
    assert re.search(r"do \{ Start-Sleep 10;.*\} while \(\(-not \(\$l -match '\^setup exit='\)\) -and \(\(Get-Date\) - \$t0\)\.TotalMinutes -lt 25\)", setup)
    assert "-Phase setup -ExpectedVersion @VERSION@ -ExpectedEngine @ENGINE@" in setup
    assert setup.rstrip().endswith("'NXG-RESULT: ok' } else { 'NXG-RESULT: FAIL the setup phase did not finish with \"setup exit=0\" (see setup.log)' }")


def test_the_verdict_wait_is_a_bounded_poll_and_the_collection_is_read_only() -> None:
    wait = _read("waitv.ps1")
    assert re.search(r"do \{ Start-Sleep 10\b", wait)
    assert re.search(r"\} while \(\(\$running -gt 0 -or \$now -eq \$before\) -and \(\(Get-Date\) - \$t0\)\.TotalMinutes -lt 27\)", wait)
    # no blind sleep anywhere in the driver: every Start-Sleep is a short poll step or the one 90 s retry pause
    for name in ALL_PS:
        for secs in re.findall(r"Start-Sleep(?: -Seconds)? (\d+)", _read(name)):
            assert int(secs) <= 90, (name, secs)
    run = _read("guest-run.ps1")
    collect = run[run.index("if ($Collect -eq 'yes')"):]
    assert "Copy-Item -FromSession" in collect
    assert not re.search(r"Remove-Item|Set-Content|Add-Content|Out-File|Invoke-Command[^\n]*\{[^\n]*(Copy-Item|Move-Item|New-Item)", collect)
    # the guest-side listing reads only
    assert not re.search(r"Remove-|Stop-|Set-|Move-", collect[collect.index("$files = Invoke-Command"):collect.index("foreach ($f in $files)")])


def test_gaterun_names_the_source_and_takes_the_version_from_its_parameters() -> None:
    gaterun = _read("gaterun.ps1")
    assert "installed from a locally built candidate wheel (not a published build)" in gaterun
    assert "published conexus $Version" in gaterun
    for token in ("@WHAT@", "@VERSION@", "@ENGINE@", "@HOME@"):
        assert token in gaterun
    assert "-Phase verify -ExpectedVersion @VERSION@ -ExpectedEngine @ENGINE@" in gaterun
    assert "IN THE BACKGROUND" in gaterun


# ---- parameters, no release number -------------------------------------------


def test_version_and_engine_are_mandatory_and_no_release_is_spelled_out() -> None:
    sh = _sh()
    assert '[[ -n "$version" && -n "$engine" ]] || { usage; die 2 "--version and --engine are required"; }' in sh
    assert 'version=\'\'\nengine=\'\'' in sh
    for name in ("inst.ps1", "setup-run.ps1", "gaterun.ps1"):
        assert re.search(r"\[Parameter\(Mandatory = \$true\)\]\[string\]\$Version\b", _read(name)), name
    for name in ("setup-run.ps1", "gaterun.ps1"):
        assert re.search(r"\[Parameter\(Mandatory = \$true\)\]\[string\]\$Engine\b", _read(name)), name
    # nothing in code spells out a release or engine number (comments may cite records)
    triple = re.compile(r"(?<![\w.])\d+\.\d+\.\d+(?![\w.])")
    for label, text in [("run-gate.sh", sh), *((n, _read(n)) for n in ALL_PS)]:
        for ln in _code_lines(text):
            assert not triple.search(ln), f"{label} hardcodes a release-looking number: {ln.strip()!r}"
    assert "ExpectedVersion 7" not in "".join(_read(n) for n in ALL_PS)


def test_environment_overrides_use_the_names_launch_sh_reads() -> None:
    sh, launch = _sh(), (GUEST_DIR / "launch.sh").read_text(encoding="utf-8")
    for var in ("NX_GUEST_HOST", "NX_GUEST_VM", "NX_GUEST_USER", "NX_GUEST_KEYCHAIN_SERVICE"):
        assert f'"${{{var}:-' in sh and f'"${{{var}:-' in launch
    assert 'KEYCHAIN_SERVICE="${NX_GUEST_KEYCHAIN_SERVICE:-nexus-win-guest}"' in sh
    assert "NX_GUEST_CHECKPOINT" in sh
    # exported, so launch.sh sees the same guest this script drives
    assert re.search(r'^export NX_GUEST_HOST="\$HOST" NX_GUEST_VM="\$VM" NX_GUEST_USER="\$GUSER" NX_GUEST_KEYCHAIN_SERVICE="\$KEYCHAIN_SERVICE"$', sh, re.M)


def test_no_name_contains_the_reserved_host_words() -> None:
    word = re.compile(r"qwen|llama", re.I)
    for p in [RUN_GATE, *DRIVER.glob("*")]:
        assert not word.search(p.name)
    for name in ALL_PS:
        assert not word.search(_read(name)), name
    # the one place: the ssh alias of the real host, the same default launch.sh has
    hits = [ln for ln in _sh().splitlines() if word.search(ln)]
    assert hits == ['HOST="${NX_GUEST_HOST:-qwentescence}"']
    # the names the driver creates carry the nxg / nxgate prefix
    assert re.search(r'^RUN="nxg-\$\(date -u \+%Y%m%dT%H%M%SZ\)-\$\$"$', _sh(), re.M)


# ---- the staged files are removed by exact name ------------------------------


def test_cleanup_removes_by_exact_name_inside_the_run_directory() -> None:
    end = _read("end.ps1")
    removes = [ln.strip() for ln in _code_lines(end) if "Remove-Item" in ln]
    assert removes, "end.ps1 must remove what the run staged"
    for ln in removes:
        assert "-LiteralPath" in ln and "-Recurse" not in ln, ln
        assert not re.search(r"Remove-Item[^\n]*[*?]", ln), ln
    assert "-Recurse" not in end and "Get-ChildItem -Filter" not in end
    # the directory is validated before anything is touched
    assert end.index("RunDir -notmatch '^C:\\\\build\\\\guest\\\\nxg-[0-9]{8}T[0-9]{6}Z-[0-9]+$'") < end.index("Remove-Item")
    # every script the laptop stages is on the removal list, and nothing else is spelled out
    listed = set(re.findall(r"'([a-z-]+\.ps1)'", end[end.index("$mine = @("):end.index("$mine +=")]))
    assert listed == set(ALL_PS)
    # launch.sh's two staged files, by name; its helper scripts are left alone
    assert "'C:\\build\\guest\\nxgate\\stage\\claude-args.txt'" in end
    assert "'C:\\build\\guest\\nxgate\\stage\\windows-phase5-gate.ps1'" in end
    assert "host-recv.ps1" not in end and "token-send" not in end
    # the shutdown takes no checkpoint
    assert "Stop-VM -Name $vm -Force" in end
    sh = _sh()
    assert not re.search(r"Remove-Item|\brm\s+-rf?\s+[\"']?\$HOST|\bdel\b|\brmdir\b", "\n".join(_code_lines(sh)))
    # only the guest this run restored is shut down, and only unless asked to keep it
    assert "if [[ $keep_guest -eq 1 || $guest_touched -eq 0 ]]; then keep='-KeepGuest'; fi" in sh


# ---- the verdict decides the exit code ---------------------------------------


def test_the_exit_code_is_zero_only_for_the_passed_line() -> None:
    sh = _sh()
    assert "passed_re='^WINDOWS PHASE 5 GATE PASSED \\(executed=([0-9]+) declared=([0-9]+)\\)$'" in sh
    tail = sh[sh.index("rc=1\n"):]
    assert tail.count("rc=0") == 1 and 'exit "$rc"' in tail
    assert '[[ "${BASH_REMATCH[1]}" == "${BASH_REMATCH[2]}" ]]' in tail and '-gt 0 ]]' in tail
    # the verdict is the LAST non-blank line of the collected verdict.txt
    assert "grep -v '^[[:space:]]*$' \"$verdict_file\" | tail -n 1" in sh
    assert "no verdict.txt was collected" in sh


def test_the_gate_script_header_names_the_driver() -> None:
    header = GATE.read_text(encoding="utf-8")
    header = header[:header.index("param(")]
    unattended = header[header.index("Unattended"):header.index("THE ASSERTIONS")]
    assert "tests/e2e/windows-guest/run-gate.sh" in unattended
    assert "--version" in unattended and "--engine" in unattended and "--wheel" in unattended


# ---- run-gate.sh as behaviour, against stand-ins -----------------------------

PASSED = "WINDOWS PHASE 5 GATE PASSED (executed=5 declared=5)"

_FAKE_SSH = r"""#!/bin/bash
# stand-in for ssh: no network. Logs the command and whether stdin carried a line.
while [[ "${1:-}" == -o ]]; do shift 2; done
cmd="$2"
bytes=$(wc -c | tr -d ' ')
echo "bytes_on_stdin=$bytes :: $cmd" >> "$FAKE_ROOT/ssh.log"
winpath() { echo "$FAKE_ROOT/host/$(echo "$1" | sed -e 's#^C:\\build\\guest\\##' -e 's#\\#/#g')"; }
case "$cmd" in
  'cmd /c "mkdir '*)
    d="${cmd#cmd /c \"mkdir }"; d="${d%\"}"
    mkdir "$(winpath "$d")" 2>/dev/null || mkdir -p "$(winpath "$d")" || exit 1 ;;
  *-File*)
    script="${cmd#*-File }"; script="${script%% *}"
    base="${script##*\\}"
    rundir="$(winpath "${script%\\*}")"
    case "$base" in
      hostcheck.ps1)
        if [[ -n "${FAKE_FAIL_STEP:-}" && "$FAKE_FAIL_STEP" == hostcheck ]]; then echo "NXG-RESULT: FAIL REFUSED: the guest is not Off"; exit 3; fi
        echo "NXG-RESULT: ok" ;;
      restore.ps1)
        echo "gate sha256=$(shasum -a 256 "$rundir/stage/windows-phase5-gate.ps1" | cut -d' ' -f1)"
        if [[ "$cmd" == *" -Wheel "* ]]; then
          w="${cmd#* -Wheel }"; w="${w%% *}"
          echo "wheel sha256=$(shasum -a 256 "$rundir/stage/$w" | cut -d' ' -f1)"
        fi
        echo "NXG-RESULT: ok" ;;
      guest-run.ps1)
        if [[ "$cmd" == *" -Inner "* ]]; then
          i="${cmd#* -Inner }"; i="${i%% *}"
          echo "$i" >> "$FAKE_ROOT/inner.log"
          if [[ "${FAKE_FAIL_STEP:-}" == "$i" ]]; then echo "NXG-RESULT: FAIL $i broke"; else echo "NXG-RESULT: ok"; fi
        fi
        if [[ "$cmd" == *" -Collect yes"* ]]; then
          mkdir -p "$rundir/out"
          if [[ -n "${FAKE_VERDICT_FILE:-}" ]]; then cp "$FAKE_VERDICT_FILE" "$rundir/out/verdict.txt"; fi
          echo "collected verdict.txt"
        fi ;;
      end.ps1)
        echo "$cmd" >> "$FAKE_ROOT/end.log"
        rm -rf "$rundir"; echo "NXG-RESULT: ok" ;;
      *) echo "unexpected script $base" >&2; exit 9 ;;
    esac ;;
  *) echo "unexpected ssh command: $cmd" >&2; exit 9 ;;
esac
"""

_FAKE_SCP = r"""#!/bin/bash
# stand-in for scp: maps host:C:/build/guest/<x> onto $FAKE_ROOT/host/<x>.
args=()
for a in "$@"; do [[ "$a" == -* ]] || args+=("$a"); done
n=${#args[@]}
dest="${args[$((n-1))]}"
map() { echo "$FAKE_ROOT/host/${1#*:C:/build/guest/}"; }
echo "scp ${args[*]}" >> "$FAKE_ROOT/scp.log"
if [[ "${args[0]}" == *:C:/build/guest/* ]]; then
  src="$(map "${args[0]}")"; [[ -e "$src" ]] || exit 1
  cp -R "$src" "${dest%/}/"
else
  d="$(map "$dest")"; mkdir -p "$d"
  for ((i = 0; i < n - 1; i++)); do cp "${args[$i]}" "$d/"; done
fi
"""

_FAKE_SECURITY = """#!/bin/bash
# stand-in for security: prints a placeholder, never a credential.
echo "placeholder-not-a-secret"
"""

_FAKE_LAUNCH = """#!/bin/bash
echo "$*" >> "$FAKE_ROOT/launch.log"
[[ "${FAKE_FAIL_STEP:-}" == launch ]] && { echo "launch.sh: FAILED"; exit 1; }
echo "launch.sh: Claude Code is open in the guest's console session"
"""


def _bash() -> str:
    found = shutil.which("bash")
    assert found, "bash is required for the run-gate.sh behaviour pins"
    return found


class Harness:
    def __init__(self, tmp: Path) -> None:
        self.root = tmp / "fakeroot"
        self.tree = tmp / "tree"
        self.bin = tmp / "bin"
        self.out = tmp / "out"
        gdir = self.tree / "tests" / "e2e" / "windows-guest"
        gdir.mkdir(parents=True)
        shutil.copytree(DRIVER, gdir / "driver")
        shutil.copy2(RUN_GATE, gdir / "run-gate.sh")
        shutil.copy2(GATE, self.tree / "tests" / "e2e" / "windows-phase5-gate.ps1")
        self.script = gdir / "run-gate.sh"
        self.root.mkdir()
        (self.root / "host").mkdir()
        self.bin.mkdir()
        for name, body in (("ssh", _FAKE_SSH), ("scp", _FAKE_SCP), ("security", _FAKE_SECURITY)):
            self._exe(self.bin / name, body)
        self._exe(gdir / "launch.sh", _FAKE_LAUNCH)

    @staticmethod
    def _exe(path: Path, body: str) -> None:
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def run(self, *args: str, verdict: str | None = None, fail_step: str = "") -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HOME": str(self.root),
            "TMPDIR": str(self.root),
            "FAKE_ROOT": str(self.root),
            "FAKE_FAIL_STEP": fail_step,
        }
        if verdict is not None:
            vf = self.root / "verdict.in"
            vf.write_text(verdict, encoding="utf-8")
            env["FAKE_VERDICT_FILE"] = str(vf)
        # the stand-ins must be what the script finds, or this test would reach a real host
        probe = subprocess.run(
            [_bash(), "-c", "command -v ssh; command -v scp; command -v security"],
            env=env, capture_output=True, text=True, check=True,
        )
        assert probe.stdout.split() == [str(self.bin / n) for n in ("ssh", "scp", "security")]
        return subprocess.run(
            [_bash(), str(self.script), *args], env=env, capture_output=True, text=True, timeout=120, cwd=self.root,
        )


@pytest.fixture()
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


_BASE = ("--version", "9.8.7", "--engine", "6.5.4")


def test_a_passed_verdict_exits_zero_and_everything_staged_is_removed(harness: Harness) -> None:
    res = harness.run(*_BASE, "--out", str(harness.out), verdict=f"P0 PASS: ok\ngate script sha256 abc\n{PASSED}\n")
    assert res.returncode == 0, res.stderr + res.stdout
    assert res.stdout.rstrip().splitlines()[-2] == PASSED
    assert "conexus==9.8.7 from PyPI" in res.stdout
    ssh_log = (harness.root / "ssh.log").read_text()
    # the guest steps ran in order, with the password on stdin for exactly the password scripts
    assert (harness.root / "inner.log").read_text().split() == ["inst.ps1", "setup-run.ps1", "gaterun.ps1", "waitv.ps1"]
    for line in ssh_log.splitlines():
        reads_pw = "restore.ps1" in line or "guest-run.ps1" in line
        assert line.startswith("bytes_on_stdin=0 ::") != reads_pw, line
        assert "placeholder-not-a-secret" not in line
    assert "-Checkpoint clean-baseline-2026-10-06" in ssh_log and "-Arg1 9.8.7" in ssh_log and "-Arg2 6.5.4" in ssh_log
    # launch.sh: the gate script staged, the unattended prompt
    launch = (harness.root / "launch.log").read_text()
    assert "--permission-mode auto" in launch and "GATERUN.txt" in launch and "--stage" in launch
    # cleanup ran, shut the guest down, and left no host directory behind
    end = (harness.root / "end.log").read_text()
    assert "end.ps1" in end and "-KeepGuest" not in end
    assert list((harness.root / "host").iterdir()) == []
    assert (harness.out / "run" / "verdict.txt").read_text().rstrip().endswith(PASSED)
    assert (harness.out / "00-hostcheck.txt").exists() and (harness.out / "99-end.txt").exists()


@pytest.mark.parametrize(
    "verdict",
    [
        "WINDOWS PHASE 5 GATE FAILED (executed=5 declared=5; not passing: A4)\n",
        f"{PASSED}\nWINDOWS PHASE 5 GATE FAILED (executed=5 declared=5; not passing: A4)\n",
        "WINDOWS PHASE 5 GATE PASSED (executed=4 declared=5)\n",
        "WINDOWS PHASE 5 GATE PASSED (executed=0 declared=0)\n",
        "WINDOWS PHASE 5 GATE PASSED (executed=5 declared=5) and more\n",
        "P0 PASS: ok\n",
        "",
    ],
)
def test_anything_but_the_passed_line_exits_non_zero(harness: Harness, verdict: str) -> None:
    res = harness.run(*_BASE, "--out", str(harness.out), verdict=verdict)
    assert res.returncode != 0, res.stdout
    assert "GATE PASSED (executed=5 declared=5)\n" not in res.stdout.replace(verdict, "") or verdict.endswith("more\n")
    # the host is cleaned up whatever the verdict
    assert list((harness.root / "host").iterdir()) == []


def test_a_run_with_no_verdict_file_is_a_failed_run(harness: Harness) -> None:
    res = harness.run(*_BASE, "--out", str(harness.out))
    assert res.returncode == 1
    assert "no verdict.txt was collected" in res.stderr
    assert list((harness.root / "host").iterdir()) == []


@pytest.mark.parametrize("step", ["inst.ps1", "setup-run.ps1", "gaterun.ps1", "launch"])
def test_a_failed_step_stops_the_run_but_still_collects_and_cleans_up(harness: Harness, step: str) -> None:
    res = harness.run(*_BASE, "--out", str(harness.out), verdict=f"{PASSED}\n", fail_step=step)
    assert res.returncode != 0
    assert PASSED not in res.stdout, "a run that stopped early must not print a verdict"
    inner = (harness.root / "inner.log").read_text().split() if (harness.root / "inner.log").exists() else []
    assert "waitv.ps1" not in inner
    assert (harness.out / "run" / "verdict.txt").exists(), "the guest's run directory is collected on the way out"
    assert (harness.root / "end.log").exists()
    assert list((harness.root / "host").iterdir()) == []


def test_a_refusal_before_the_restore_never_shuts_the_guest_down(harness: Harness) -> None:
    res = harness.run(*_BASE, "--out", str(harness.out), fail_step="hostcheck")
    assert res.returncode != 0
    assert "restore.ps1" not in (harness.root / "ssh.log").read_text()
    assert "-KeepGuest" in (harness.root / "end.log").read_text(), "a guest this run did not restore is left as it is"
    assert list((harness.root / "host").iterdir()) == []


def test_keep_guest_is_passed_through(harness: Harness) -> None:
    res = harness.run(*_BASE, "--out", str(harness.out), "--keep-guest", verdict=f"{PASSED}\n")
    assert res.returncode == 0
    assert "-KeepGuest" in (harness.root / "end.log").read_text()


def test_a_candidate_wheel_is_installed_hashed_and_named_in_the_output(harness: Harness, tmp_path: Path) -> None:
    wheel = tmp_path / "conexus-9.8.7-py3-none-any.whl"
    wheel.write_bytes(b"not a real wheel")
    res = harness.run(*_BASE, "--wheel", str(wheel), "--out", str(harness.out), verdict=f"{PASSED}\n")
    assert res.returncode == 0, res.stderr + res.stdout
    assert "candidate wheel conexus-9.8.7-py3-none-any.whl" in res.stdout and "not a published build" in res.stdout
    ssh_log = (harness.root / "ssh.log").read_text()
    assert "-Wheel conexus-9.8.7-py3-none-any.whl" in ssh_log and "-Arg2 conexus-9.8.7-py3-none-any.whl" in ssh_log
    assert "-Arg3 candidate" in ssh_log
    assert "-Wheel conexus-9.8.7-py3-none-any.whl" in (harness.root / "end.log").read_text()
    assert list((harness.root / "host").iterdir()) == []


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("--version", "9.8.7"),
        ("--engine", "6.5.4"),
        ("--version", "9.8", "--engine", "6.5.4"),
        ("--version", "9.8.7", "--engine", "6.5.4;x"),
        ("--version", "9.8.7", "--engine", "6.5.4", "--wheel", "/nonexistent/conexus-9.8.7-py3-none-any.whl"),
        ("--version", "9.8.7", "--engine", "6.5.4", "--bogus"),
    ],
)
def test_bad_invocations_exit_2_before_any_remote_call(harness: Harness, args: tuple[str, ...]) -> None:
    res = harness.run(*args)
    assert res.returncode == 2
    assert not (harness.root / "ssh.log").exists() and not (harness.root / "scp.log").exists()


def test_a_wheel_for_another_version_is_refused(harness: Harness, tmp_path: Path) -> None:
    wheel = tmp_path / "conexus-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"x")
    res = harness.run(*_BASE, "--wheel", str(wheel))
    assert res.returncode == 2
    assert not (harness.root / "ssh.log").exists()
