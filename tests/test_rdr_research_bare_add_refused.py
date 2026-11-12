# SPDX-License-Identifier: AGPL-3.0-or-later
"""The bare-argument ``nx rdr preamble rdr-research -- add`` form is refused (RDR-240 TD7, nexus-qo7ie.33).

A finding passed as bare shell arguments was split by the shell (RDR-240 Problem Statement: a
finding with a semicolon ran ``git stash`` and truncated three records). The ``research_add``
procedure builds the record from a string and the caller writes it with ``memory_put``; the plugin
switched to it in conexus 8.0.0, and this package release retires the CLI form: every ``add`` prints
the replacement and exits nonzero, writing nothing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.commands import rdr as rdr_cmd
from nexus.commands.rdr import rdr

ROOT = Path(__file__).resolve().parent.parent

ADD_FORMS = [
    pytest.param(("add", "240", "a", "finding"), id="id-and-text"),
    pytest.param(("add", "240"), id="id-only"),
    pytest.param(("add", "RDR-240", "a", "finding"), id="rdr-prefixed-id"),
    pytest.param(
        ("add", "97", "--classification", "verified", "--method", "spike", "x"),
        id="flags",
    ),
    pytest.param(("add 240 a finding; with a semicolon",), id="one-element"),
    pytest.param(("ADD", "240", "x"), id="upper-case-verb"),
    pytest.param(("add",), id="verb-only"),
    pytest.param(("add 240",), id="one-element-id-only"),
]


@pytest.fixture
def no_t2(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> object:
        raise AssertionError("the refused add opened a T2 client")

    monkeypatch.setattr(rdr_cmd, "_t2_client_factory", _boom)
    monkeypatch.setattr(rdr_cmd, "_preamble_resolve_repo", lambda: (str(ROOT), "nexus"))


@pytest.mark.parametrize("argv", ADD_FORMS)
def test_every_add_form_exits_nonzero_and_names_the_replacement(
    argv: tuple[str, ...], no_t2: None
) -> None:
    res = CliRunner().invoke(rdr, ["preamble", "rdr-research", "--", *argv])
    assert res.exit_code != 0, res.output
    for words in ("research_add", "memory_put", "/conexus:rdr-research add"):
        assert words in res.output, res.output


def test_the_cli_writer_is_gone() -> None:
    assert not hasattr(rdr_cmd, "_rdr_research_add")


def test_no_shipped_source_sends_a_caller_to_the_bare_add() -> None:
    def grep(*pattern: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(ROOT), "grep", "-n", "-E", *pattern, "--", "src", "conexus"],
            capture_output=True,
            text=True,
        )

    # Non-vacuity (nexus-moht0): the scan must see the tree, so a pattern every tree holds matches.
    seen = grep("-e", "preamble_rdr_research")
    assert seen.returncode == 0 and seen.stdout, f"git grep saw nothing: {seen.stderr}"
    out = grep("-e", r"rdr-research (-- )?add")
    assert out.returncode in (0, 1), out.stderr
    hits = [
        ln for ln in out.stdout.splitlines() if "CHANGELOG" not in ln and "/conexus:rdr-research add" not in ln
    ]
    assert hits == [], hits
