# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end shell-quoting verification for slash-command preamble lines.

The unit tests in ``test_command_context_command.py`` and
``test_rdr_preamble.py`` invoke the CLI with *pre-split* argv
(``["rdr-show", "--", "1"]``). That is NOT the path that broke: Claude Code
substitutes ``$ARGUMENTS`` **textually** into the ``!`…`` backtick line and
then a shell ``eval``s it. The original ``(eval):1: unmatched "`` failure lived
entirely in that substitution+eval step, which pre-split argv never exercises.

This module reproduces the real pipeline: take the exact backtick body from each
command ``.md``, textually substitute ``$ARGUMENTS`` with hostile inputs, replace
the ``nx`` program with a probe that prints the argv it receives, and ``eval`` the
result in a real shell. It asserts:

  1. Dropped-arg commands survive EVERY metacharacter (including a literal
     apostrophe) — the user input never reaches the shell line.
  2. Single-quoted commands neutralise injection: ``$(...)`` / backticks / shell
     operators arrive as INERT literal text in a single intact argv token, not
     executed. (Their one boundary — a literal apostrophe — is documented in the
     command bodies; token-only args cannot contain one.)

bead: follow-up to the RDR-130 command shell-quoting fix (#1007).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_CMD_DIR = Path(__file__).parent.parent / "conexus" / "commands"

# Inputs a user could plausibly type after a slash command. Each is a shell
# minefield; none should ever break the line or execute.
_INJECTION = [
    '003 --reason "fixed (the) broken thing"',  # double quotes + parens
    "`whoami`",                                   # backtick command substitution
    "$(whoami)",                                  # $() command substitution
    "$HOME && echo PWNED",                        # var expansion + shell operator
]
_APOSTROPHE = "it's done"  # the single-quote boundary

_BACKTICK_LINE = re.compile(r"^!`(nx (?:command-context|rdr preamble) [^`]+)`$")


def _probe(tmp_path: Path) -> Path:
    """A stand-in for `nx` that prints each argv token, one per line."""
    p = tmp_path / "argv_probe"
    p.write_text('#!/usr/bin/env bash\nfor a in "$@"; do printf "ARGV<%s>\\n" "$a"; done\n')
    p.chmod(0o755)
    return p


def _command_lines() -> dict[str, str]:
    """Map each command file to its backtick preamble body (sans the `!` and ticks)."""
    out: dict[str, str] = {}
    for md in sorted(_CMD_DIR.glob("*.md")):
        for line in md.read_text().splitlines():
            m = _BACKTICK_LINE.match(line)
            if m:
                out[md.name] = m.group(1)
    return out


def _render_and_eval(body: str, argument: str, probe: Path, shell: str) -> subprocess.CompletedProcess:
    """Mimic CC: textually substitute $ARGUMENTS, swap `nx`->probe, eval in `shell`."""
    rendered = body.replace("$ARGUMENTS", argument).replace("nx ", f"{probe} ", 1)
    return subprocess.run([shell, "-c", rendered], capture_output=True, text=True)


def _argv_after_terminator(stdout: str) -> list[str]:
    toks = re.findall(r"ARGV<(.*)>", stdout)
    return toks[toks.index("--") + 1:] if "--" in toks else []


def _assert_no_leaked_output(stdout: str) -> None:
    """Every non-empty stdout line must be a probe ``ARGV<…>`` line. A bare line
    (e.g. ``PWNED`` from an executed ``&& echo``, or a ``$(whoami)`` result) would
    prove the shell executed an injected payload rather than passing it inert."""
    leaked = [ln for ln in stdout.splitlines() if ln.strip() and not ln.startswith("ARGV<")]
    assert leaked == [], f"injected payload produced shell output: {leaked!r}"


_SHELLS = [s for s in ("bash", "zsh") if shutil.which(s)]

_LINES = _command_lines()
_DROPPED = {n: b for n, b in _LINES.items() if "$ARGUMENTS" not in b}
_SINGLE_QUOTED = {n: b for n, b in _LINES.items() if "'$ARGUMENTS'" in b}


def test_inventory_is_sane() -> None:
    """Guards against the regex silently matching nothing (which would make the
    parametrised tests vacuously pass)."""
    assert len(_LINES) == 25, sorted(_LINES)
    # No command may double-quote $ARGUMENTS (mirrors the static guard).
    assert not [n for n, b in _LINES.items() if '"$ARGUMENTS"' in b]
    assert len(_DROPPED) >= 18
    assert set(_SINGLE_QUOTED) == {
        "rdr-show.md", "rdr-gate.md", "rdr-accept.md", "rdr-research.md", "rdr-audit.md",
    }, sorted(_SINGLE_QUOTED)


@pytest.mark.skipif(not _SHELLS, reason="no bash/zsh available")
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("name", sorted(_DROPPED))
@pytest.mark.parametrize("arg", [*_INJECTION, _APOSTROPHE])
def test_dropped_arg_commands_survive_every_metacharacter(
    name: str, arg: str, shell: str, tmp_path: Path
) -> None:
    """Commands that drop the arg never let user text reach the shell line, so
    EVERY input — including a literal apostrophe — evals cleanly."""
    r = _render_and_eval(_DROPPED[name], arg, _probe(tmp_path), shell)
    assert r.returncode == 0, f"{name} [{shell}] arg={arg!r}: {r.stderr.strip()}"
    assert "unmatched" not in r.stderr and "(eval)" not in r.stderr
    _assert_no_leaked_output(r.stdout)  # no operator/subshell executed
    # The arg is dropped, so nothing appears after a `--` terminator.
    assert _argv_after_terminator(r.stdout) == []


@pytest.mark.skipif(not _SHELLS, reason="no bash/zsh available")
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("name", sorted(_SINGLE_QUOTED))
@pytest.mark.parametrize("arg", _INJECTION)
def test_single_quoted_commands_neutralize_injection(
    name: str, arg: str, shell: str, tmp_path: Path
) -> None:
    """Single-quoted commands pass injection payloads through INERT: the shell
    neither breaks nor executes them, and the program receives the payload as one
    intact argv token after `--`."""
    r = _render_and_eval(_SINGLE_QUOTED[name], arg, _probe(tmp_path), shell)
    assert r.returncode == 0, f"{name} [{shell}] arg={arg!r}: {r.stderr.strip()}"
    assert "unmatched" not in r.stderr and "(eval)" not in r.stderr
    # No operator/subshell executed: every stdout line is a probe ARGV line, so
    # no `echo PWNED` / `$(whoami)` output leaked alongside the argv.
    _assert_no_leaked_output(r.stdout)
    # And the payload arrives as ONE intact literal token verbatim ($HOME, `…`,
    # $(…) all unexpanded) — the definitive proof it was inert.
    assert _argv_after_terminator(r.stdout) == [arg], (
        f"{name} [{shell}]: expected intact literal token, got {_argv_after_terminator(r.stdout)!r}"
    )
