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

import hashlib
import os
import re
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

from tests._child_process import kill_group, popen_in_group, signal_group

REPO: Path = Path(__file__).resolve().parent.parent
GUEST_DIR: Path = REPO / "tests" / "e2e" / "windows-guest"
DRIVER: Path = GUEST_DIR / "driver"
RUN_GATE: Path = GUEST_DIR / "run-gate.sh"
GATE: Path = REPO / "tests" / "e2e" / "windows-phase5-gate.ps1"

PW_SCRIPTS = ("restore.ps1", "guest-run.ps1", "stduser.ps1", "cache.ps1")
NO_PW_SCRIPTS = ("hostcheck.ps1", "end.ps1")
INNER_SCRIPTS = ("inst.ps1", "setup-run.ps1", "gaterun.ps1", "waitv.ps1", "as-user.ps1")
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
        r'\s*\| ssh "\$\{SSH_OPTS\[@\]\}" "\$HOST" "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ',
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
    # the only other use of the value: its characters, one by one, into a SecureString
    # (ConvertTo-SecureString -AsPlainText would put the plain text in the cmdlet's parameter binding)
    re.compile(
        r"^\$sec = New-Object System\.Security\.SecureString; "
        r"foreach \(\$c in \$pw\.ToCharArray\(\)\) \{ \$sec\.AppendChar\(\$c\) \}; \$sec\.MakeReadOnly\(\); \$pw = \$null; \$c = \$null$"
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
    # the SecureString is only wrapped in the credential, which is only handed to the guest connection
    for ln in (ln.strip() for ln in code if re.search(r"\$sec\b", ln) and not ln.strip().startswith("$sec = New-Object")):
        assert ln == "$cred = New-Object PSCredential($GuestUser, $sec)", f"$sec used in an unexpected way: {ln!r}"
    for ln in (ln.strip() for ln in code if "$cred" in ln):
        assert (
            ln == "$cred = New-Object PSCredential($GuestUser, $sec)"
            or re.search(r"-Credential \$cred\b", ln)
        ), f"$cred used in an unexpected way: {ln!r}"
    assert "ConvertTo-SecureString" not in "\n".join(code) and "-AsPlainText" not in "\n".join(code)
    # nothing writes anything out of those variables
    text = _read(name)
    assert not re.search(r"(Out-File|Set-Content|Add-Content|Export-|Write-Host|Write-Output)[^\n]*\$(pw|cred)\b", text)
    assert not re.search(r"\$(pw|cred)\b[^\n]*(>>?|\| *(Out-File|Set-Content|Add-Content|Tee-Object))", text)


@pytest.mark.parametrize("name", NO_PW_SCRIPTS + INNER_SCRIPTS)
def test_scripts_that_need_no_password_never_read_stdin_or_build_a_credential(name: str) -> None:
    text = _read(name)
    assert not re.search(r"Console\]::In|ReadLine|PSCredential|SecureString|-Credential\b|\$pw\b|\$sec\b", text)


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


def test_the_restore_prints_one_fixed_marker_right_after_the_restore_succeeds() -> None:
    lines = [ln.strip() for ln in _code_lines(_read("restore.ps1"))]
    at = next(i for i, ln in enumerate(lines) if ln.startswith("Restore-VMSnapshot"))
    # run-gate.sh treats the guest as this run's only when this line is in the restore step's output
    assert lines[at + 1] == "'NXG-RESTORED: ok'"
    assert sum("NXG-RESTORED" in ln for ln in lines) == 1
    assert "NXG-RESTORED" not in "".join(_read(n) for n in ALL_PS if n != "restore.ps1")


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
    # the hook records are read in the guest and written on the host; a failure there cannot fail the collection
    records = collect[collect.index("$records = Invoke-Command"):collect.index("[System.IO.File]::WriteAllLines(")]
    assert "ReadAllLines($t.FullName)" in records and not re.search(r"Remove-|Stop-|Set-|Move-|Copy-|New-Item|WriteAll", records)
    # a context record is a hook's normal output, not a failure (the first real run counted one as not-success)
    assert "-notin 'hook_success', 'hook_additional_context'" in records
    assert "(Join-Path $out 'hook-records.txt')" in collect and "} catch { \"hook records not collected:" in collect


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
    assert "if [[ $keep_guest -eq 1 || $touched -eq 0 ]]; then keep='-KeepGuest'; fi" in sh
    # "restored" is evidence (the restore step's marker line), never a flag set before the step runs
    assert "grep -q '^NXG-RESTORED: ok' \"$out/01-restore.txt\"" in sh
    assert "guest_touched=1" not in sh


# ---- the verdict decides the exit code ---------------------------------------


def test_the_exit_code_is_zero_only_for_the_passed_line() -> None:
    sh = _sh()
    assert "passed_re='^WINDOWS PHASE 5 GATE PASSED \\(executed=([0-9]+) declared=([0-9]+)\\)$'" in sh
    tail = sh[sh.index("rc=1\n"):]
    assert tail.count("rc=0") == 1 and 'exit "$rc"' in tail
    assert '[[ "${BASH_REMATCH[1]}" == "${BASH_REMATCH[2]}" ]]' in tail and '-gt 0 ]]' in tail
    # the verdict is the LAST non-blank line of the collected verdict.txt
    assert "grep -v '^[[:space:]]*$' | tail -n 1" in sh
    assert "no verdict.txt was collected" in sh
    # and only when the gate's own sha line names the script this run staged
    assert "gate script sha256 ${gate_sha} " in sh and "the setup phase ran the same file" in sh


def test_the_gate_script_header_names_the_driver() -> None:
    header = GATE.read_text(encoding="utf-8")
    header = header[:header.index("param(")]
    unattended = header[header.index("Unattended"):header.index("THE ASSERTIONS")]
    assert "tests/e2e/windows-guest/run-gate.sh" in unattended
    assert "--version" in unattended and "--engine" in unattended and "--wheel" in unattended


# ---- run-gate.sh as behaviour, against stand-ins -----------------------------

PASSED = "WINDOWS PHASE 5 GATE PASSED (executed=5 declared=5)"
GATE_SHA = hashlib.sha256(GATE.read_bytes()).hexdigest()
OTHER_SHA = "0" * 64


def _sha_line(sha: str = GATE_SHA, same: bool = True) -> str:
    """The gate's own provenance line, as the gate writes it (its Windows path included)."""
    ran = "the setup phase ran the same file" if same else f"the setup phase ran sha256 {OTHER_SHA}, a DIFFERENT file"
    return f"gate script sha256 {sha} (C:\\Users\\nxguest\\nx-gate\\windows-phase5-gate.ps1; {ran})"


def _verdict(last: str, *, sha: str | None = GATE_SHA, same: bool = True, bom: bool = False, crlf: bool = False, bom_on_sha_line: bool = False) -> bytes:
    """A collected verdict.txt. The real file (gate3 run, 2026-10-12) is UTF-8 with a BOM on line 1 and CRLF
    throughout: its first line is the JSON of the results, then the sha line, then the verdict line."""
    nl = "\r\n" if crlf else "\n"
    lines = [] if bom_on_sha_line else ['{"P0":{"detail":"nx, version 9.8.7","status":"PASS"}}']
    if sha is not None:
        lines.append(_sha_line(sha, same))
    lines.append(last)
    return (b"\xef\xbb\xbf" if bom else b"") + (nl.join(lines) + nl).encode("utf-8")


_FAKE_SSH = r"""#!/bin/bash
# stand-in for ssh: no network. Logs the command and how many bytes arrived on stdin.
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
        if [[ "${FAKE_FAIL_STEP:-}" == hostcheck ]]; then echo "NXG-RESULT: FAIL REFUSED: the guest is not Off"; exit 3; fi
        echo "NXG-RESULT: ok" ;;
      restore.ps1)
        # the real script refuses before it restores anything when the guest is not Off
        if [[ "${FAKE_FAIL_STEP:-}" == restore ]]; then echo "NXG-RESULT: FAIL REFUSED: guest is not Off"; exit 3; fi
        echo "NXG-RESTORED: ok"
        if [[ "${FAKE_FAIL_STEP:-}" == restore-late ]]; then echo "NXG-RESULT: FAIL the guest has no network"; exit 4; fi
        echo "gate sha256=$(shasum -a 256 "$rundir/stage/windows-phase5-gate.ps1" | cut -d' ' -f1)"
        if [[ "$cmd" == *" -Wheel "* ]]; then
          w="${cmd#* -Wheel }"; w="${w%% *}"
          echo "wheel sha256=$(shasum -a 256 "$rundir/stage/$w" | cut -d' ' -f1)"
        fi
        echo "NXG-RESULT: ok" ;;
      stduser.ps1)
        if [[ "${FAKE_FAIL_STEP:-}" == stduser ]]; then echo "NXG-RESULT: FAIL nxstd did not sign in at the console"; exit 7; fi
        echo "std gate sha256=$(shasum -a 256 "$rundir/stage/windows-phase5-gate.ps1" | cut -d' ' -f1)"
        if [[ "$cmd" == *" -Wheel "* ]]; then
          w="${cmd#* -Wheel }"; w="${w%% *}"
          echo "std wheel sha256=$(shasum -a 256 "$rundir/stage/$w" | cut -d' ' -f1)"
        fi
        echo "NXG-RESULT: ok" ;;
      guest-run.ps1)
        if [[ "$cmd" == *" -Inner "* ]]; then
          i="${cmd#* -Inner }"; i="${i%% *}"
          echo "$i" >> "$FAKE_ROOT/inner.log"
          if [[ "${FAKE_SLOW_INNER:-}" == "$i" ]]; then sh -c 'touch "$1"; exec sleep "$2"' _ "$FAKE_ROOT/slow-started" "${FAKE_SLEEP:-60}"; fi
          if [[ "${FAKE_FAIL_STEP:-}" == "$i" ]]; then echo "NXG-RESULT: FAIL $i broke"; else echo "NXG-RESULT: ok"; fi
        fi
        if [[ "$cmd" == *" -Collect yes"* ]]; then
          if [[ "$cmd" != *" -Inner "* && -n "${FAKE_COLLECT_SLEEP:-}" ]]; then sh -c 'touch "$1"; exec sleep "$2"' _ "$FAKE_ROOT/collecting" "$FAKE_COLLECT_SLEEP"; fi
          mkdir -p "$rundir/out"
          if [[ -n "${FAKE_VERDICT_FILE:-}" ]]; then cp "$FAKE_VERDICT_FILE" "$rundir/out/verdict.txt"; fi
          echo "collected verdict.txt"
        fi ;;
      cache.ps1)
        m="${cmd#* -Mode }"; m="${m%% *}"
        echo "$m :: $cmd" >> "$FAKE_ROOT/cache.log"
        if [[ "${FAKE_FAIL_STEP:-}" == "$m" ]]; then echo "NXG-RESULT: FAIL cache $m broke"; exit 1; fi
        echo "NXG-RESULT: ok" ;;
      end.ps1)
        echo "$cmd" >> "$FAKE_ROOT/end.log"
        rm -rf "$rundir"
        if [[ -z "${FAKE_END_BAD:-}" ]]; then echo "NXG-RESULT: ok"; else echo "NXG-RESULT: FAIL the run directory was not emptied"; fi ;;
      *) echo "unexpected script $base" >&2; exit 9 ;;
    esac ;;
  *) echo "unexpected ssh command: $cmd" >&2; exit 9 ;;
esac
"""

_FAKE_SCP = r"""#!/bin/bash
# stand-in for scp: maps host:C:/build/guest/<x> onto $FAKE_ROOT/host/<x>.
args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o) shift 2 ;;
    -*) shift ;;
    *) args+=("$1"); shift ;;
  esac
done
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
echo "call" >> "$FAKE_ROOT/security.log"
[[ -n "${FAKE_SECURITY_FAIL:-}" ]] && exit 44
echo "placeholder-not-a-secret"
"""

_FAKE_LAUNCH = """#!/bin/bash
echo "bytes_on_stdin=$(wc -c | tr -d ' ') :: run_as=${NX_GUEST_RUN_AS:-} :: $*" >> "$FAKE_ROOT/launch.log"
[[ "${FAKE_FAIL_STEP:-}" == launch ]] && { echo "launch.sh: FAILED"; exit 1; }
echo "launch.sh: Claude Code is open in the guest's console session"
"""

_FAKE_BAD_SHASUM = """#!/bin/bash
# stand-in for a shasum that fails and prints nothing
exit 1
"""

_STDIN_CANARY = "STDIN-CANARY-must-not-be-read\n"


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

    def _env(self, verdict: bytes | str | None, fail_step: str, extra: dict[str, str] | None) -> dict[str, str]:
        env = {
            "PATH": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HOME": str(self.root),
            "TMPDIR": str(self.root),
            "FAKE_ROOT": str(self.root),
            "FAKE_FAIL_STEP": fail_step,
            **(extra or {}),
        }
        if verdict is not None:
            vf = self.root / "verdict.in"
            vf.write_bytes(verdict if isinstance(verdict, bytes) else verdict.encode("utf-8"))
            env["FAKE_VERDICT_FILE"] = str(vf)
        # the stand-ins must be what the script finds, or this test would reach a real host
        probe = subprocess.run(
            [_bash(), "-c", "command -v ssh; command -v scp; command -v security"],
            env=env, capture_output=True, text=True, check=True,
        )
        assert probe.stdout.split() == [str(self.bin / n) for n in ("ssh", "scp", "security")]
        return env

    def run(
        self, *args: str, verdict: bytes | str | None = None, fail_step: str = "", env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        # the canary is what a non-password ssh would swallow if it lacked </dev/null
        return subprocess.run(
            [_bash(), str(self.script), *args], env=self._env(verdict, fail_step, env),
            capture_output=True, text=True, timeout=120, cwd=self.root, input=_STDIN_CANARY,
        )

    def start(
        self, *args: str, verdict: bytes | str | None = None, fail_step: str = "", env: dict[str, str] | None = None
    ) -> subprocess.Popen[str]:
        """The script in a session of its own, so a signal to its process group is a terminal Ctrl-C or hangup."""
        return popen_in_group(
            [_bash(), str(self.script), *args], env=self._env(verdict, fail_step, env),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=self.root,
        )

    def end_calls(self) -> list[str]:
        path = self.root / "end.log"
        return path.read_text().splitlines() if path.exists() else []

    def ssh_lines(self) -> list[str]:
        return (self.root / "ssh.log").read_text().splitlines()


@pytest.fixture()
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


_BASE = ("--version", "9.8.7", "--engine", "6.5.4")


def _assert_cleaned_up_once(h: Harness) -> None:
    """end.ps1 ran exactly once. (The stand-in end.ps1 removes the host directory, so an empty host proves
    only that it was called; what the real one removes is pinned by the text tests above.)"""
    assert len(h.end_calls()) == 1, h.end_calls()
    assert list((h.root / "host").iterdir()) == []


def test_a_passed_verdict_exits_zero_and_everything_staged_is_removed(harness: Harness) -> None:
    res = harness.run(*_BASE, "--out", str(harness.out), verdict=_verdict(PASSED))
    assert res.returncode == 0, res.stderr + res.stdout
    assert res.stdout.rstrip().splitlines()[-2] == PASSED
    assert "conexus==9.8.7 from PyPI" in res.stdout
    ssh_log = harness.ssh_lines()
    # the guest steps ran in order, with the password on stdin for exactly the password scripts
    # and the caller's stdin (the canary) read by none of the others
    assert (harness.root / "inner.log").read_text().split() == ["inst.ps1", "setup-run.ps1", "gaterun.ps1", "waitv.ps1"]
    for line in ssh_log:
        reads_pw = "restore.ps1" in line or "guest-run.ps1" in line or "cache.ps1" in line
        assert line.startswith("bytes_on_stdin=0 ::") != reads_pw, line
        assert "placeholder-not-a-secret" not in line
    assert "-Checkpoint clean-baseline-2026-10-06" in "\n".join(ssh_log) and "-Arg1 9.8.7" in "\n".join(ssh_log)
    assert "-Arg2 6.5.4" in "\n".join(ssh_log)
    # launch.sh: the gate script staged, the unattended prompt, and it did not take the caller's stdin
    launch = (harness.root / "launch.log").read_text()
    assert launch.startswith("bytes_on_stdin=0 ::")
    assert "--permission-mode auto" in launch and "GATERUN.txt" in launch and "--stage" in launch
    # cleanup ran once, shut the guest down (no -KeepGuest), and left no host directory behind
    _assert_cleaned_up_once(harness)
    assert "-KeepGuest" not in harness.end_calls()[0]
    assert (harness.out / "run" / "verdict.txt").read_bytes().rstrip().endswith(PASSED.encode())
    assert (harness.out / "00-hostcheck.txt").exists() and (harness.out / "99-end.txt").exists()


@pytest.mark.parametrize(
    "verdict",
    [
        # the real shape: BOM on line 1, CRLF throughout
        _verdict(PASSED, bom=True, crlf=True),
        # BOM on the line the sha is on, so the BOM strip is what lets the provenance line match
        _verdict(PASSED, bom=True, crlf=True, bom_on_sha_line=True),
        _verdict(PASSED, crlf=True),
        _verdict(PASSED, bom=True),
    ],
    ids=["real-shape", "bom-on-sha-line", "crlf-only", "bom-only"],
)
def test_the_real_verdict_file_shape_exits_zero(harness: Harness, verdict: bytes) -> None:
    res = harness.run(*_BASE, "--out", str(harness.out), verdict=verdict)
    assert res.returncode == 0, res.stderr + res.stdout
    assert "\r" not in res.stdout and "\ufeff" not in res.stdout
    assert res.stdout.rstrip().splitlines()[-2] == PASSED


@pytest.mark.parametrize(
    "verdict",
    [
        _verdict("WINDOWS PHASE 5 GATE FAILED (executed=5 declared=5; not passing: A4)"),
        _verdict("WINDOWS PHASE 5 GATE FAILED (executed=5 declared=5; not passing: A4)", bom=True, crlf=True),
        _verdict(PASSED) + b"WINDOWS PHASE 5 GATE FAILED (executed=5 declared=5; not passing: A4)\n",
        _verdict("WINDOWS PHASE 5 GATE PASSED (executed=4 declared=5)"),
        _verdict("WINDOWS PHASE 5 GATE PASSED (executed=0 declared=0)"),
        _verdict("WINDOWS PHASE 5 GATE PASSED (executed=5 declared=5) and more"),
        _verdict("P0 PASS: ok"),
        b"",
    ],
    ids=["failed", "failed-real-shape", "failed-after-passed", "executed-lt-declared", "zero-zero", "trailing-text", "no-verdict-line", "empty"],
)
def test_anything_but_the_passed_line_exits_non_zero(harness: Harness, verdict: bytes) -> None:
    # every one of these carries a valid sha line, so it is the verdict line itself that fails them
    res = harness.run(*_BASE, "--out", str(harness.out), verdict=verdict)
    assert res.returncode != 0, res.stdout
    assert PASSED not in res.stdout.splitlines()
    _assert_cleaned_up_once(harness)


@pytest.mark.parametrize(
    ("verdict", "why"),
    [
        (_verdict(PASSED, sha=OTHER_SHA), "a different gate script sha256"),
        (_verdict(PASSED, sha=None), "no sha line"),
        (_verdict(PASSED, same=False), "setup ran a different file"),
        (_verdict(PASSED, sha=GATE_SHA[:-1]), "a truncated sha"),
        (_verdict(PASSED, sha=None, bom=True, crlf=True), "no sha line, real shape"),
    ],
    ids=["other-sha", "no-sha-line", "setup-differs", "truncated-sha", "no-sha-line-real-shape"],
)
def test_a_passed_verdict_from_another_gate_script_is_refused(harness: Harness, verdict: bytes, why: str) -> None:
    res = harness.run(*_BASE, "--out", str(harness.out), verdict=verdict)
    assert res.returncode != 0, why
    assert "does not carry the gate script line for this run's gate script" in res.stderr, res.stderr
    assert GATE_SHA in res.stderr
    _assert_cleaned_up_once(harness)


def test_a_run_with_no_verdict_file_is_a_failed_run(harness: Harness) -> None:
    res = harness.run(*_BASE, "--out", str(harness.out))
    assert res.returncode == 1
    assert "no verdict.txt was collected" in res.stderr
    _assert_cleaned_up_once(harness)


@pytest.mark.parametrize("step", ["inst.ps1", "setup-run.ps1", "gaterun.ps1", "launch"])
def test_a_failed_step_stops_the_run_but_still_collects_and_cleans_up(harness: Harness, step: str) -> None:
    res = harness.run(*_BASE, "--out", str(harness.out), verdict=_verdict(PASSED), fail_step=step)
    assert res.returncode != 0
    assert PASSED not in res.stdout, "a run that stopped early must not print a verdict"
    inner = (harness.root / "inner.log").read_text().split() if (harness.root / "inner.log").exists() else []
    assert "waitv.ps1" not in inner
    assert (harness.out / "run" / "verdict.txt").exists(), "the guest's run directory is collected on the way out"
    _assert_cleaned_up_once(harness)
    assert "-KeepGuest" not in harness.end_calls()[0], "a guest this run restored is shut down"


def test_a_refusal_before_the_restore_never_shuts_the_guest_down(harness: Harness) -> None:
    res = harness.run(*_BASE, "--out", str(harness.out), fail_step="hostcheck")
    assert res.returncode != 0
    assert "restore.ps1" not in "\n".join(harness.ssh_lines())
    _assert_cleaned_up_once(harness)
    assert "-KeepGuest" in harness.end_calls()[0], "a guest this run did not restore is left as it is"


def test_a_refusal_by_the_restore_step_leaves_the_guest_alone_and_touches_it_no_more(harness: Harness) -> None:
    """restore.ps1 refuses (the guest is not Off, e.g. another run's): this run restored nothing, so it must
    neither collect from that guest nor power it off."""
    res = harness.run(*_BASE, "--out", str(harness.out), fail_step="restore", verdict=_verdict(PASSED))
    assert res.returncode != 0
    lines = harness.ssh_lines()
    password_calls = [ln for ln in lines if "restore.ps1" in ln or "guest-run.ps1" in ln]
    assert len(password_calls) == 1 and "restore.ps1" in password_calls[0], password_calls
    assert len((harness.root / "security.log").read_text().splitlines()) == 1, "the keychain was read after the refusal"
    assert not any("-Collect" in ln for ln in lines)
    assert not (harness.out / "run").exists() and not (harness.out / "90-collect-on-exit.txt").exists()
    _assert_cleaned_up_once(harness)
    assert "-KeepGuest" in harness.end_calls()[0]


def test_a_failure_after_the_restore_marker_collects_and_powers_the_guest_off(harness: Harness) -> None:
    res = harness.run(*_BASE, "--out", str(harness.out), fail_step="restore-late", verdict=_verdict(PASSED))
    assert res.returncode != 0
    assert any("-Collect yes" in ln for ln in harness.ssh_lines()), "the restored guest's run directory is collected"
    assert (harness.out / "run" / "verdict.txt").exists()
    _assert_cleaned_up_once(harness)
    assert "-KeepGuest" not in harness.end_calls()[0], "a guest this run restored is shut down"


def test_keep_guest_is_passed_through(harness: Harness) -> None:
    res = harness.run(*_BASE, "--out", str(harness.out), "--keep-guest", verdict=_verdict(PASSED))
    assert res.returncode == 0
    assert "-KeepGuest" in harness.end_calls()[0]


def test_a_candidate_wheel_is_installed_hashed_and_named_in_the_output(harness: Harness, tmp_path: Path) -> None:
    wheel = tmp_path / "conexus-9.8.7-py3-none-any.whl"
    wheel.write_bytes(b"not a real wheel")
    res = harness.run(*_BASE, "--wheel", str(wheel), "--out", str(harness.out), verdict=_verdict(PASSED))
    assert res.returncode == 0, res.stderr + res.stdout
    assert "candidate wheel conexus-9.8.7-py3-none-any.whl" in res.stdout and "not a published build" in res.stdout
    ssh_log = "\n".join(harness.ssh_lines())
    assert "-Wheel conexus-9.8.7-py3-none-any.whl" in ssh_log and "-Arg2 conexus-9.8.7-py3-none-any.whl" in ssh_log
    assert "-Arg3 candidate" in ssh_log
    assert "-Wheel conexus-9.8.7-py3-none-any.whl" in harness.end_calls()[0]
    _assert_cleaned_up_once(harness)


def test_a_failed_cleanup_is_warned_about_last_and_does_not_change_a_passed_exit(harness: Harness) -> None:
    res = harness.run(*_BASE, "--out", str(harness.out), verdict=_verdict(PASSED), env={"FAKE_END_BAD": "1"})
    assert res.returncode == 0, "a failed cleanup must not turn a PASSED run into a failure"
    err = res.stderr.rstrip().splitlines()
    assert err and "did not report NXG-RESULT: ok" in err[-1], res.stderr
    assert "99-end.txt" in err[-1]


def test_a_clean_run_prints_no_cleanup_warning(harness: Harness) -> None:
    res = harness.run(*_BASE, "--out", str(harness.out), verdict=_verdict(PASSED))
    assert res.returncode == 0
    assert "did not report NXG-RESULT: ok" not in res.stderr


def test_a_failing_keychain_read_fails_the_step_not_just_the_pipe(harness: Harness) -> None:
    """pipefail by behaviour: security | ssh must report security's failure even though ssh succeeds."""
    res = harness.run(*_BASE, "--out", str(harness.out), verdict=_verdict(PASSED), env={"FAKE_SECURITY_FAIL": "1"})
    assert res.returncode != 0
    assert not (harness.root / "inner.log").exists(), "the run went on to the guest steps after a failed keychain read"
    assert "step restore failed" in res.stderr


def test_an_unusable_sha256_stops_the_run_before_any_remote_call(harness: Harness) -> None:
    Harness._exe(harness.bin / "shasum", _FAKE_BAD_SHASUM)
    Harness._exe(harness.bin / "sha256sum", _FAKE_BAD_SHASUM)
    res = harness.run(*_BASE, "--out", str(harness.out), verdict=_verdict(PASSED))
    assert res.returncode == 2
    assert "sha256" in res.stderr
    assert not (harness.root / "ssh.log").exists() and not (harness.root / "scp.log").exists()


def test_a_step_that_outlives_its_bound_is_killed_and_fails_the_run(harness: Harness) -> None:
    t0 = time.monotonic()
    res = harness.run(
        *_BASE, "--out", str(harness.out), verdict=_verdict(PASSED),
        env={"NX_GUEST_TIMEOUT_INSTALL": "2", "FAKE_SLOW_INNER": "inst.ps1", "FAKE_SLEEP": "40"},
    )
    assert time.monotonic() - t0 < 25, "the install step was not cut off at its bound"
    assert res.returncode != 0
    assert "timed out after 2 s" in res.stderr + res.stdout
    assert "setup-run.ps1" not in (harness.root / "inner.log").read_text()
    _assert_cleaned_up_once(harness)


def _wait_for(path: Path, seconds: float = 30.0) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        time.sleep(0.05)


_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="process-group signals do not exist on Windows")


@_POSIX_ONLY
@pytest.mark.parametrize(
    ("sig", "code"),
    [(signal.SIGINT, 130), (signal.SIGHUP, 129), (signal.SIGTERM, 143)],
    ids=["INT", "HUP", "TERM"],
)
def test_a_terminal_signal_during_a_step_collects_cleans_up_once_and_exits_with_the_signal_code(
    harness: Harness, sig: signal.Signals, code: int
) -> None:
    proc = harness.start(
        *_BASE, "--out", str(harness.out), verdict=_verdict(PASSED),
        env={"FAKE_SLOW_INNER": "setup-run.ps1", "FAKE_SLEEP": "60"},
    )
    try:
        _wait_for(harness.root / "slow-started")
        signal_group(proc, sig)
        stdout, stderr = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            kill_group(proc)
    assert proc.returncode == code, stderr
    _assert_cleaned_up_once(harness)
    assert (harness.out / "run" / "verdict.txt").exists(), "the guest's run directory is collected on the way out"
    assert "-KeepGuest" not in harness.end_calls()[0]


@_POSIX_ONLY
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGHUP], ids=["INT", "HUP"])
def test_a_signal_during_the_collection_on_exit_still_runs_the_end_step_exactly_once(
    harness: Harness, sig: signal.Signals
) -> None:
    proc = harness.start(
        *_BASE, "--out", str(harness.out), verdict=_verdict(PASSED), fail_step="inst.ps1",
        env={"FAKE_COLLECT_SLEEP": "60"},
    )
    t0 = time.monotonic()
    try:
        _wait_for(harness.root / "collecting")
        signal_group(proc, sig)
        stdout, stderr = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            kill_group(proc)
    assert time.monotonic() - t0 < 45, "the interrupted collection was waited out"
    assert proc.returncode == 1, stderr  # the failed step's own exit, not the signal's
    _assert_cleaned_up_once(harness)
    assert "-KeepGuest" not in harness.end_calls()[0]


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


# ---- text pins for what the stand-ins cannot show ---------------------------


def test_every_ssh_and_scp_call_is_batch_mode_and_the_non_password_ones_read_no_stdin() -> None:
    sh = _sh()
    assert "SSH_OPTS=(-o BatchMode=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=6)" in sh
    code = _code_lines(sh)
    for ln in code:
        if re.search(r"^\s*(\|\s*)?(ssh|scp)\b", ln) or re.search(r"\bssh \"", ln):
            assert "SSH_OPTS" in ln, f"ssh/scp without the shared options: {ln.strip()!r}"
    assert not re.search(r"\bscp -q\b", "\n".join(code)), "scp must take the shared options, not a bare -q"
    # powershell is never interactive, whichever way it is started from the Mac
    assert len(re.findall(r"powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File", sh)) == 2
    assert "powershell -NoProfile -ExecutionPolicy" not in sh
    assert "set -uo pipefail" in sh


# ---- --standard-user ---------------------------------------------------------


def test_a_plain_run_never_names_a_run_as_account(harness: Harness) -> None:
    res = harness.run(*_BASE, "--out", str(harness.out), verdict=_verdict(PASSED))
    assert res.returncode == 0, res.stderr + res.stdout
    assert not any("stduser.ps1" in ln or "-RunAs" in ln or "-AsUser" in ln for ln in harness.ssh_lines())
    assert ":: run_as= ::" in (harness.root / "launch.log").read_text()
    assert "nxguest\\nx-gate\\GATERUN.txt" in (harness.root / "launch.log").read_text()
    assert "standard user" not in res.stdout


def test_a_standard_user_run_makes_the_account_after_the_restore_and_runs_every_later_step_as_it(harness: Harness) -> None:
    res = harness.run(*_BASE, "--standard-user", "--out", str(harness.out), verdict=_verdict(PASSED))
    assert res.returncode == 0, res.stderr + res.stdout
    scripts = [ln for ln in harness.ssh_lines() if "-File" in ln]
    order = [re.search(r"\\([a-z-]+\.ps1)", ln).group(1) for ln in scripts]  # type: ignore[union-attr]
    assert order[:3] == ["hostcheck.ps1", "restore.ps1", "stduser.ps1"]
    std = scripts[2]
    assert "-StdUser nxstd" in std and "-GuestUser nxguest" in std and not std.startswith("bytes_on_stdin=0 ")
    # the restore still runs as the administrator; everything after the account exists names it
    assert "-RunAs" not in scripts[1]
    later = [ln for ln in scripts[3:] if "guest-run.ps1" in ln]
    assert len(later) == 4 and all(ln.rstrip().endswith("-RunAs nxstd") or "-RunAs nxstd " in ln for ln in later)
    install = next(ln for ln in later if "-Inner inst.ps1" in ln)
    assert "-RunAs nxstd -AsUser yes" in install
    assert sum("-AsUser" in ln for ln in scripts) == 1, "only the install is run as the account by as-user.ps1"
    launch = (harness.root / "launch.log").read_text()
    assert ":: run_as=nxstd ::" in launch and "nxstd\\nx-gate\\GATERUN.txt" in launch
    assert "run as standard user nxstd (not an administrator)" in res.stdout
    _assert_cleaned_up_once(harness)


def test_a_failed_account_step_stops_before_any_install_and_collects_from_the_administrator(harness: Harness) -> None:
    res = harness.run(*_BASE, "--standard-user", "--out", str(harness.out), verdict=_verdict(PASSED), fail_step="stduser")
    assert res.returncode == 1
    assert not (harness.root / "inner.log").exists()
    collect = [ln for ln in harness.ssh_lines() if "guest-run.ps1" in ln]
    assert len(collect) == 1 and "-Collect yes" in collect[0] and "-RunAs" not in collect[0]
    _assert_cleaned_up_once(harness)


def test_the_standard_user_must_not_be_the_administrator_account(harness: Harness) -> None:
    res = harness.run(*_BASE, "--standard-user", env={"NX_GUEST_STD_USER": "nxguest"})
    assert res.returncode == 2 and not (harness.root / "ssh.log").exists()


def test_the_account_step_and_the_run_as_wrapper_keep_their_guarantees() -> None:
    std = _read("stduser.ps1")
    # the account is a member of Users only, and the step proves it is no administrator before it reports ok
    assert "Add-LocalGroupMember -SID 'S-1-5-32-545' -Member $u" in std
    assert std.count("Add-LocalGroupMember") == 1
    assert std.index("in Administrators=False") < std.rindex("'NXG-RESULT: ok'")
    # its secret is random, set without a command line, and removed from Winlogon once the account is signed in
    assert "RNGCryptoServiceProvider" in std and "net user" not in std and "net.exe" not in std
    assert std.index("-Name DefaultPassword -Value $tp; $tp = $null") < std.index("Remove-ItemProperty -Path $wl -Name DefaultPassword")
    assert not re.search(r'"[^"\n]*\$tp\b[^"\n]*"|Write-|Out-File[^\n]*\$tp|Set-Content[^\n]*\$tp', std)
    asu = _read("as-user.ps1")
    assert "-UserId $u -LogonType Interactive -RunLevel Limited" in asu
    assert "administrators-member=False$" in asu
    assert re.search(r"do \{ Start-Sleep 10;.*\} while \(\(-not \(\$l -match '\^asuser exit='\)\) -and \(\(Get-Date\) - \$t0\)\.TotalMinutes -lt 25\)", asu)
    # an inner ok is never printed bare when the wrapper itself failed
    assert 'if ($bad) { $l | ForEach-Object { "inner: $_" }; "NXG-RESULT: FAIL $bad" } else { $l }' in asu
    # the setup task and the Claude Code window run as the named account
    assert "$who = if ($env:NXG_RUN_AS) { $env:NXG_RUN_AS } else { $env:USERNAME }" in _read("setup-run.ps1")
    recv = (GUEST_DIR / "host-recv.ps1").read_text(encoding="utf-8")
    assert "New-ScheduledTaskPrincipal -UserId $who -LogonType Interactive" in recv
    assert "NTAccount($env:COMPUTERNAME, $runAs)), 'Read', 'Allow'" in recv


# ---- --no-admin-git ----------------------------------------------------------


def test_no_admin_git_stages_the_install_guides_own_portablegit_block(harness: Harness) -> None:
    shutil.copytree(REPO / "docs", harness.tree / "docs", ignore=lambda d, names: [n for n in names if n != "windows-install.md"])
    res = harness.run(*_BASE, "--standard-user", "--no-admin-git", "--out", str(harness.out), verdict=_verdict(PASSED))
    assert res.returncode == 0, res.stderr + res.stdout
    block = (harness.out / "portable-git.ps1").read_text(encoding="utf-8")
    guide = (REPO / "docs" / "windows-install.md").read_text(encoding="utf-8")
    assert block.strip() and block in guide, "the staged block must be the guide's text, unchanged"
    assert "PortableGit" in block and "SetEnvironmentVariable('Path'" in block and "Get-FileHash" in block
    assert any("portable-git.ps1" in ln for ln in (harness.root / "scp.log").read_text().splitlines())
    std = next(ln for ln in harness.ssh_lines() if "stduser.ps1" in ln)
    assert "-AdminGit no" in std
    assert "no Git from nxguest: the account installs PortableGit" in res.stdout


def test_a_standard_user_run_without_the_flag_has_the_administrator_install_git(harness: Harness) -> None:
    res = harness.run(*_BASE, "--standard-user", "--out", str(harness.out), verdict=_verdict(PASSED))
    assert res.returncode == 0, res.stderr + res.stdout
    assert "-AdminGit yes" in next(ln for ln in harness.ssh_lines() if "stduser.ps1" in ln)
    assert not (harness.out / "portable-git.ps1").exists()


def test_no_admin_git_needs_standard_user_and_a_guide_that_still_has_the_block(harness: Harness) -> None:
    res = harness.run(*_BASE, "--no-admin-git")
    assert res.returncode == 2 and not (harness.root / "ssh.log").exists()
    (harness.tree / "docs").mkdir()
    (harness.tree / "docs" / "windows-install.md").write_text("```powershell\nuv tool install conexus\n```\n", encoding="utf-8")
    res = harness.run(*_BASE, "--standard-user", "--no-admin-git", "--out", str(harness.out))
    assert res.returncode == 2 and "no powershell block naming PortableGit" in res.stderr
    assert not (harness.root / "ssh.log").exists()


def test_the_no_git_run_keeps_its_guarantees_in_the_scripts() -> None:
    std = _read("stduser.ps1")
    # with -AdminGit no the administrator runs no winget, and a guest that already has Git is refused
    assert "if ($adminGit -eq 'no') { 'the administrator installs no Git (-AdminGit no)' }" in std
    assert "this is not a no-Git run" in std
    inst = _read("inst.ps1")
    # the account runs the staged block itself, and Git is found in either place afterwards
    assert 'powershell -NoProfile -ExecutionPolicy Bypass -File "$b\\portable-git.ps1"' in inst
    assert "$env:LOCALAPPDATA\\Programs\\Git\\cmd" in inst and "if (-not (GitDir)) { $bad += 'git.exe is missing' }" in inst
    assert "$mine += Join-Path $RunDir 'portable-git.ps1'" in _read("end.ps1")


# ---- --applocker -------------------------------------------------------------


def test_applocker_is_passed_to_the_stduser_step_and_needs_standard_user(harness: Harness) -> None:
    res = harness.run(*_BASE, "--applocker")
    assert res.returncode == 2 and "the --applocker modes need --standard-user" in res.stderr
    assert not (harness.root / "ssh.log").exists()
    res = harness.run(*_BASE, "--standard-user", "--applocker", "--out", str(harness.out), verdict=_verdict(PASSED))
    assert res.returncode == 0, res.stderr + res.stdout
    assert "-AppLocker yes" in next(ln for ln in harness.ssh_lines() if "stduser.ps1" in ln)
    assert "AppLocker default rules enforced" in res.stdout


def test_a_run_without_the_flag_sets_no_applocker_policy(harness: Harness) -> None:
    res = harness.run(*_BASE, "--standard-user", "--out", str(harness.out), verdict=_verdict(PASSED))
    assert res.returncode == 0, res.stderr + res.stdout
    assert "-AppLocker no" in next(ln for ln in harness.ssh_lines() if "stduser.ps1" in ln)
    assert "AppLocker" not in res.stdout
    std = _read("stduser.ps1")
    assert "[string]$AppLocker = 'no'" in std
    # every policy statement sits behind the flag
    assert std.count("if ($appLocker -ne 'no') {") == 2
    assert std.index("if ($appLocker -ne 'no') {") < std.index("Set-AppLockerPolicy")


def test_the_applocker_run_cannot_pass_under_a_policy_that_is_not_in_effect() -> None:
    std = _read("stduser.ps1")
    # all four collections enforced, with the default rules' three audiences
    for kind in ("Exe", "Msi", "Script", "Appx"):
        assert f"<RuleCollection Type=\"{kind}\" EnforcementMode=\"Enabled\">' +" in std, "the base policy enforces every collection"
    assert "%PROGRAMFILES%\\*" in std and "%WINDIR%\\*" in std and 'UserOrGroupSid="S-1-5-32-544"' in std
    assert "sc.exe config AppIDSvc start= auto" in std
    # the control file is started AS the account until the AppLocker log records the block (event 8004);
    # the policy reading alone said Denied 75 s before anything was blocked (2026-10-13)
    assert "New-ScheduledTaskPrincipal -UserId $u -LogonType Interactive -RunLevel Limited" in std
    assert "$want = if ($appLocker -eq 'audit') { 8003 } else { 8004 }" in std
    assert "$_.Id -eq $want -and $_.Message -like '*APPLOCKER-CONTROL.EXE*'" in std
    fail = next(ln for ln in std.splitlines() if "NXG-RESULT: FAIL AppLocker is not in effect" in ln)
    for needed in ("applocker AppIDSvc=Running", "applocker control for the account: Denied", "applocker System32 for the account: Allowed"):
        assert f"$text -notmatch '{needed}'" in fail
    # an enforced run needs the refused start (8004); only the audit run accepts the audit record (8003)
    assert "'applocker control start as the account: event ' + $(if ($AppLocker -eq 'audit') { '8003' } else { '8004' }) + ' recorded=True'" in fail
    assert "$xml.Replace('EnforcementMode=\"Enabled\"', 'EnforcementMode=\"AuditOnly\"')" in std


def test_applocker_audit_is_its_own_mode(harness: Harness) -> None:
    res = harness.run(*_BASE, "--applocker-audit")
    assert res.returncode == 2 and not (harness.root / "ssh.log").exists()
    res = harness.run(*_BASE, "--standard-user", "--applocker-audit", "--out", str(harness.out), verdict=_verdict(PASSED))
    assert res.returncode == 0, res.stderr + res.stdout
    assert "-AppLocker audit" in next(ln for ln in harness.ssh_lines() if "stduser.ps1" in ln)
    assert "audit only (nothing is blocked)" in res.stdout and "default rules enforced" not in res.stdout
    inst = _read("inst.ps1")
    assert 'if (Test-Path "$b\\applocker-control.exe") {' in inst and "applocker language mode:" in inst


def test_applocker_allow_enforces_programs_and_names_the_installs_folders(harness: Harness) -> None:
    res = harness.run(*_BASE, "--applocker-allow")
    assert res.returncode == 2 and not (harness.root / "ssh.log").exists()
    res = harness.run(*_BASE, "--standard-user", "--applocker-allow", "--out", str(harness.out), verdict=_verdict(PASSED))
    assert res.returncode == 0, res.stderr + res.stdout
    assert "-AppLocker allow" in next(ln for ln in harness.ssh_lines() if "stduser.ps1" in ln)
    assert "script rules audit only" in res.stdout
    std = _read("stduser.ps1")
    # the allow rules exist only in the allow modes; any other mode replaces the marker with nothing
    assert std.index("$allow = ''") < std.index("if ($appLocker -in 'allow', 'allow-dll-audit', 'allow-dll') {") < std.index("$xml = $xml.Replace('@ALLOW@', $allow)")
    for folder in (".local\\bin\\*", "AppData\\Roaming\\uv\\*", "AppData\\Local\\uv\\cache\\*", "AppData\\Local\\Programs\\Git\\*", ".config\\nexus\\*"):
        assert f"'{folder}'" in std
    # the control file's folder is not among them, so the enforcement wait still sees a refused start
    assert "'nx-gate" not in std


# ---- --wdac-audit / --wdac-allow ---------------------------------------------


@pytest.mark.parametrize(
    ("flag", "mode", "note"),
    [
        ("--wdac-audit", "audit", "WDAC DefaultWindows policy in audit mode (nothing is blocked)"),
        ("--wdac-allow", "allow", "WDAC DefaultWindows policy enforced, with path rules for this install's folders; script enforcement off"),
    ],
)
def test_wdac_modes_need_a_standard_user_with_no_admin_git(harness: Harness, flag: str, mode: str, note: str) -> None:
    # The policy trusts only code Windows signs, for administrators too, so an all-users Git is refused.
    res = harness.run(*_BASE, flag)
    assert res.returncode == 2 and "the --wdac modes need --standard-user --no-admin-git" in res.stderr
    res = harness.run(*_BASE, "--standard-user", flag)
    assert res.returncode == 2 and not (harness.root / "ssh.log").exists()
    res = harness.run(*_BASE, "--standard-user", "--no-admin-git", flag, "--applocker-allow")
    assert res.returncode == 2 and "separate runs" in res.stderr
    shutil.copytree(REPO / "docs", harness.tree / "docs", ignore=lambda d, names: [n for n in names if n != "windows-install.md"])
    res = harness.run(*_BASE, "--standard-user", "--no-admin-git", flag, "--out", str(harness.out), verdict=_verdict(PASSED))
    assert res.returncode == 0, res.stderr + res.stdout
    line = next(ln for ln in harness.ssh_lines() if "stduser.ps1" in ln)
    assert f"-Wdac {mode}" in line and "-AppLocker no" in line
    assert note in res.stdout


def test_a_run_without_a_wdac_flag_deploys_no_policy(harness: Harness) -> None:
    res = harness.run(*_BASE, "--standard-user", "--out", str(harness.out), verdict=_verdict(PASSED))
    assert res.returncode == 0, res.stderr + res.stdout
    assert "-Wdac no" in next(ln for ln in harness.ssh_lines() if "stduser.ps1" in ln)
    assert "WDAC" not in res.stdout
    std = _read("stduser.ps1")
    assert "[ValidateSet('no', 'audit', 'allow')][string]$Wdac = 'no'" in std
    # every WDAC statement sits behind the flag
    assert std.count("if ($wdac -ne 'no') {") == 2
    assert std.index("if ($wdac -ne 'no') {") < std.index("CiTool.exe\" --update-policy")


def test_the_wdac_run_cannot_pass_under_a_policy_that_is_not_in_effect() -> None:
    std = _read("stduser.ps1")
    # the base is Windows' own enforced example; audit is option 3; allow turns off runtime file-path
    # protection (18) and script enforcement (11) and adds path rules naming the account
    assert "ExamplePolicies\\DefaultWindows_Enforced.xml" in std
    assert "if ($wdac -eq 'audit') { Set-RuleOption -FilePath $x -Option 3 }" in std
    assert "Set-RuleOption -FilePath $x -Option 18" in std and "Set-RuleOption -FilePath $x -Option 11" in std
    assert '$rules += New-CIPolicyRule -FilePathRule "C:\\Users\\$u\\$p"' in std
    # the deploy stops unless every path rule is in the policy file (a refused merge once deployed none)
    assert 'if ($have -ne $folders.Count) { "NXG-RESULT: FAIL the WDAC policy has $have of $($folders.Count) path rules"; return }' in std
    # the control is an UNSIGNED program built in the guest: a Windows-signed copy is trusted wherever it is
    assert "csc.exe\" /nologo /out:\"$b\\wdac-control.exe\"" in std
    assert "$want = if ($wdac -eq 'audit') { 3076 } else { 3077 }" in std
    assert "LogName = 'Microsoft-Windows-CodeIntegrity/Operational'; Id = $want; StartTime = $t1" in std
    fail = next(ln for ln in std.splitlines() if "NXG-RESULT: FAIL WDAC is not in effect" in ln)
    for needed in ("wdac policy active: count=1 ", "wdac control built: True signed=NotSigned"):
        assert f"$text -notmatch '{needed}'" in fail
    assert "'wdac control start as the account: event ' + $(if ($Wdac -eq 'audit') { '3076' } else { '3077' }) + ' recorded=True'" in fail
    # a failed deploy stops the step before the restart wait
    assert "if (($pre | Out-String) -match 'NXG-RESULT: FAIL') { exit 6 }" in std
    inst = _read("inst.ps1")
    assert 'if (Test-Path "$b\\wdac-control.exe") {' in inst and "wdac language mode:" in inst


@pytest.mark.parametrize(
    ("flag", "mode", "note"),
    [
        ("--applocker-allow-dll-audit", "allow-dll-audit", "DLL rules and script rules audit only"),
        ("--applocker-allow-dll", "allow-dll", "enforced for programs and DLLs"),
    ],
)
def test_applocker_dll_modes_add_the_dll_collection(harness: Harness, flag: str, mode: str, note: str) -> None:
    res = harness.run(*_BASE, flag)
    assert res.returncode == 2 and not (harness.root / "ssh.log").exists()
    res = harness.run(*_BASE, "--standard-user", flag, "--out", str(harness.out), verdict=_verdict(PASSED))
    assert res.returncode == 0, res.stderr + res.stdout
    assert f"-AppLocker {mode}" in next(ln for ln in harness.ssh_lines() if "stduser.ps1" in ln)
    assert note in res.stdout
    std = _read("stduser.ps1")
    # the DLL collection exists only in these two modes, enforced only in allow-dll, and carries the
    # install's folders; the executable control wait still expects a refused start (8004)
    block = std[std.index("if ($appLocker -in 'allow-dll-audit', 'allow-dll') {"):std.index("$xml = $xml.Replace('@ALLOW@', $allow)")]
    assert '<RuleCollection Type="Dll" EnforcementMode="' in block
    assert "if ($appLocker -eq 'allow-dll') { 'Enabled' } else { 'AuditOnly' }" in block
    assert "foreach ($p in @($folders) + $(if ($appLocker -eq 'allow-dll') { $dllExtra } else { @() }))" in block


# ---- --cache (nexus-bpzt7) ----------------------------------------------------


def _scripts_in_order(h: Harness) -> list[str]:
    """The host script each ssh call ran, in order (guest-run.ps1 shown with its inner script)."""
    names = []
    for line in h.ssh_lines():
        m = re.search(r"-File \S*\\([a-z-]+\.ps1)", line)
        if not m:
            continue
        name = m.group(1)
        inner = re.search(r" -Inner ([a-z-]+\.ps1)", line)
        mode = re.search(r" -Mode ([a-z]+)", line)
        names.append(f"{name}:{inner.group(1)}" if inner else f"{name}:{mode.group(1)}" if mode else name)
    return names


def test_cache_on_is_the_default_seeds_before_the_install_and_harvests_after_the_verdict(harness: Harness) -> None:
    res = harness.run(*_BASE, "--standard-user", "--out", str(harness.out), verdict=_verdict(PASSED))
    assert res.returncode == 0, res.stderr + res.stdout
    order = _scripts_in_order(harness)
    assert order.index("stduser.ps1") < order.index("cache.ps1:seed") < order.index("guest-run.ps1:inst.ps1")
    assert order.index("guest-run.ps1:waitv.ps1") < order.index("cache.ps1:harvest") < order.index("end.ps1")
    ssh = "\n".join(harness.ssh_lines())
    assert "restore.ps1" in ssh and "-Cache yes" in ssh
    # the cache is filled for the account that installs, keyed on the published version
    cache_log = (harness.root / "cache.log").read_text()
    assert "-Account nxstd -Key 9.8.7" in cache_log
    assert "--cache on" in res.stdout and (harness.out / "07-harvest.txt").exists()


def test_cache_off_touches_no_cache_and_leaves_windows_update_alone(harness: Harness) -> None:
    res = harness.run(*_BASE, "--cache", "off", "--out", str(harness.out), verdict=_verdict(PASSED))
    assert res.returncode == 0, res.stderr + res.stdout
    assert not (harness.root / "cache.log").exists()
    assert "-Cache no" in "\n".join(harness.ssh_lines())
    assert "--cache off: everything downloaded fresh" in res.stdout


def test_a_candidate_wheel_keys_the_cache_on_its_sha256(harness: Harness, tmp_path: Path) -> None:
    wheel = tmp_path / "conexus-9.8.7-py3-none-any.whl"
    wheel.write_bytes(b"not really a wheel")
    sha = hashlib.sha256(wheel.read_bytes()).hexdigest()
    res = harness.run(*_BASE, "--wheel", str(wheel), "--out", str(harness.out), verdict=_verdict(PASSED))
    assert res.returncode == 0, res.stderr + res.stdout
    assert f"-Key w-{sha[:16]}" in (harness.root / "cache.log").read_text()


def test_a_bad_cache_value_exits_2_before_any_remote_call(harness: Harness) -> None:
    res = harness.run(*_BASE, "--cache", "maybe")
    assert res.returncode == 2
    assert not (harness.root / "ssh.log").exists()


def test_a_failed_seed_stops_before_the_install(harness: Harness) -> None:
    res = harness.run(*_BASE, "--out", str(harness.out), verdict=_verdict(PASSED), fail_step="seed")
    assert res.returncode != 0
    assert not (harness.root / "inner.log").exists() or "inst.ps1" not in (harness.root / "inner.log").read_text()
    _assert_cleaned_up_once(harness)


def test_a_failed_harvest_is_reported_and_does_not_change_a_passed_verdict(harness: Harness) -> None:
    res = harness.run(*_BASE, "--out", str(harness.out), verdict=_verdict(PASSED), fail_step="harvest")
    assert res.returncode == 0, res.stderr + res.stdout
    assert "the cache harvest did not finish (not part of the verdict)" in res.stdout
    _assert_cleaned_up_once(harness)


def test_the_cache_script_writes_only_under_its_folder_and_removes_by_exact_name() -> None:
    text = _read("cache.ps1")
    code = "\n".join(_code_lines(text))
    assert "$root = 'C:\\build\\guest\\cache'" in code
    for ln in (ln.strip() for ln in _code_lines(text) if "Remove-Item" in ln):
        assert "-LiteralPath" in ln and "-Recurse" not in ln and not re.search(r"Remove-Item[^\n]*[*?]", ln), ln
    # host-side writes name a path built from $root (or the guest session, -ToSession/-FromSession)
    for ln in (ln.strip() for ln in _code_lines(text) if re.search(r"Set-Content|Move-Item|New-Item -ItemType Directory -Force \$root", ln)):
        assert "$root" in ln or "$keys" in ln or "$uvTar" in ln or "$tmp" in ln, ln
    assert "Checkpoint-VM" not in code and "Restore-VM" not in code


def test_the_cached_claude_binary_is_used_only_when_it_is_the_latest_releases_by_sha256() -> None:
    inst = _read("inst.ps1")
    fn = inst[inst.index("function CachedClaude"):inst.index("Step 'claude code'")]
    assert "claude-code-releases" in fn and "/latest" in fn and "manifest.json" in fn
    assert fn.index("Get-FileHash") < fn.index("install")
    assert "if ($got -ne" in fn
    step = inst[inst.index("Step 'claude code'"):]
    step = step[:step.index("\n}\n")]
    # anything but a used cached binary runs the official installer
    assert "if (-not $used) { powershell -NoProfile -ExecutionPolicy Bypass -Command 'irm https://claude.ai/install.ps1 | iex' }" in step


def test_updates_go_off_inside_the_guest_at_first_contact_and_nothing_changes_on_the_host() -> None:
    restore = _read("restore.ps1")
    block = restore[restore.index("$quietBlock = {"):restore.index("$quieted = $Cache -ne 'yes'")]
    for name in ("'wuauserv', 'UsoSvc', 'DoSvc'", "NoAutoUpdate", "SignatureScheduleDay 8", "SignatureUpdateInterval 0"):
        assert name in block, name
    # applied inside the network wait, before the first reachability probe, and only with -Cache yes
    loop = restore[restore.index("for ($i = 0; $i -lt 60; $i++)"):]
    assert loop.index("$quietBlock") < loop.index("Invoke-WebRequest https://pypi.org/simple/")
    # Sam, 2026-10-15: no host-side VM settings from the driver
    for name in ALL_PS:
        code = "\n".join(_code_lines(_read(name)))
        assert not re.search(r"Enable-VMIntegrationService|Disable-VMIntegrationService|Copy-VMFile|Set-VMNetworkAdapter|Set-VMProcessor|Set-VMMemory", code), name


def test_the_network_probe_asks_for_headers_only() -> None:
    # https://pypi.org/simple/ is the whole package index (47 MB on 2026-10-15). A GET that timed out was
    # fetched again every 5 s, and one slow boot spent about 0.9 GB on it (nexus-bpzt7).
    restore = _read("restore.ps1")
    probes = [ln for ln in _code_lines(restore) if "Invoke-WebRequest" in ln]
    assert probes and all("-Method Head" in ln for ln in probes), probes
