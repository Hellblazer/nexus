# SPDX-License-Identifier: AGPL-3.0-or-later
"""The first-run text batch for 8.1.1 (nexus-f3yqd.6 triage F5, F10, F12, F13, F18, F19, F28).

Wording and help text only: each message a new user meets says what happened and what to do,
and each flag says what it does. No behaviour changes.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from click.testing import CliRunner

import nexus
from nexus.cli import main
from nexus.commands import doctor as doctor_cmd
from nexus.commands import init as init_cmd
from nexus.mcp import setup_tools


def _help(*args: str) -> str:
    return " ".join(CliRunner().invoke(main, [*args, "--help"]).output.split())


# F5: no jargon about which process started this server.
@pytest.mark.parametrize("mode", ["local", "cloud", "other"])
def test_the_setup_fallback_says_it_cannot_run_from_this_server(mode: str) -> None:
    text = setup_tools.terminal_commands(mode)
    assert text.startswith("Setup cannot run from this server, so nothing was ")
    assert "launcher" not in text


# F10: no placeholder tag a user cannot run, and no "see above".
def test_the_missing_engine_tag_message_says_why_and_where_the_tags_are() -> None:
    src = inspect.getsource(init_cmd)
    assert "engine-service-vX.Y.Z" not in src
    assert "(see above)" not in src
    assert "a released conexus always pins one" in src
    assert "engine-service-v* tags of github.com/Hellblazer/nexus" in src


# F12: --yes says what it registers.
def test_init_yes_says_it_starts_the_service_at_every_login() -> None:
    text = _help("init")
    assert "starts the storage service each time you log in" in text
    assert "starts each time you log in" in inspect.getsource(init_cmd._decide_autostart)


# F13: --force says what it overwrites.
def test_the_autostart_force_hint_says_what_it_overwrites() -> None:
    src = inspect.getsource(init_cmd)
    assert "replaces the autostart unit file already installed" in src
    assert "differs, re-run `nx daemon service install --autostart --force`" in src  # a symlinked unit is not fixed by --force


# F18: --skip-t3 names everything it skips.
def test_upgrade_skip_t3_names_everything_it_skips() -> None:
    text = _help("upgrade")
    for words in ("restart", "engine install", "PDF stack", "installer rebuild", "keeps running",
                  "The data steps still run"):
        assert words in text, words
    assert "T2-only" not in text  # the ladder's data steps run under --skip-t3


# F19: --yes no longer describes a prompt nothing raises.
def test_upgrade_yes_does_not_promise_a_prompt() -> None:
    text = _help("upgrade")
    assert "Nothing in `nx upgrade` asks for this today" in text
    assert "Nothing else prompts" not in text
    assert "the only prompt the ladder can raise" not in text  # developer docstring kept out of --help


def test_nothing_but_upgrade_reads_the_consent_env() -> None:
    # Pins the --yes help: once a step reads NX_ASSUME_YES, the help must stop saying it changes nothing.
    src = Path(nexus.__file__).parent
    readers = sorted(
        str(p.relative_to(src)) for p in src.rglob("*.py") if "NX_ASSUME_YES" in p.read_text(encoding="utf-8")
    )
    assert readers == ["commands/upgrade.py"], readers


# F28: the remedy names a command that does something.
def test_the_plan_parity_remedy_drops_the_no_op_flag() -> None:
    src = inspect.getsource(doctor_cmd)
    assert "nx plan reseed --force" not in src
    assert "Fix: run `nx plan reseed`." in src
