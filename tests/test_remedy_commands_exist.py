# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``nx index`` and ``nx catalog purge-trash`` remedies the doctor and the reconcile verbs print name commands that exist.

``nx index`` is a bare group, so ``nx index <path> --force`` was "No such command"; ``nx catalog purge-trash`` has no
``--execute`` (the live run is ``--no-dry-run --confirm``). The docs audit for 8.5.6 found both in text a person copies.
This scans the source files that carry them and resolves every such command through the real click tree.
"""
from __future__ import annotations

import re
from pathlib import Path

import click
import pytest

from nexus.cli import main

SRC = Path(__file__).resolve().parent.parent / "src" / "nexus"
FILES = [
    "health.py",
    "indexer_utils.py",
    "commands/collection.py",
    "commands/catalog_cmds/reconcile_fences.py",
    "commands/catalog_cmds/reconcile_stale.py",
    "commands/catalog_cmds/doctor.py",
]
_INDEX = re.compile(r"\bnx index(?: [^\s`\"')(]+)*")
_PURGE = re.compile(r"\bnx catalog purge-trash((?: --?[\w-]+)*)")


def _group_command(*words: str) -> click.Command | None:
    cmd: click.Command = main
    for word in words:
        if not isinstance(cmd, click.Group):
            return cmd
        found = cmd.get_command(click.Context(cmd), word)
        if found is None:
            return None
        cmd = found
    return cmd


def _scan() -> tuple[list[str], list[str], int]:
    bad: list[str] = []
    seen_index = seen_purge = 0
    for rel in FILES:
        for lineno, line in enumerate((SRC / rel).read_text(encoding="utf-8").splitlines(), 1):
            for m in _INDEX.finditer(line):
                words = [w for w in m.group(0).split()[2:] if not w.startswith(("-", "<"))]
                if not words or not re.fullmatch(r"[a-z|]+", words[0]):
                    continue  # prose such as the log header "nx index ..."; a bare-group flag is judged below
                seen_index += 1
                for sub in words[0].split("|"):
                    cmd = _group_command("index", sub)
                    if cmd is None or isinstance(cmd, click.Group):
                        bad.append(f"{rel}:{lineno}: nx index {sub} is not a command")
            for m in re.finditer(r"\bnx index (--\w[\w-]*)", line):
                bad.append(f"{rel}:{lineno}: `nx index {m.group(1)}` names a flag of the bare group")
            for m in _PURGE.finditer(line):
                seen_purge += 1
                purge = _group_command("catalog", "purge-trash")
                real = {opt for p in purge.params for opt in (*p.opts, *p.secondary_opts)}
                for flag in m.group(1).split():
                    if flag not in real:
                        bad.append(f"{rel}:{lineno}: purge-trash has no {flag}")
    return bad, [], seen_index + seen_purge


def test_no_remedy_names_a_command_or_flag_the_cli_lacks() -> None:
    bad, _unused, examined = _scan()
    assert not bad, "\n".join(bad)
    assert examined >= 10, f"the scan examined {examined} remedies: it is looking at the wrong text"


def test_the_scan_fires_on_the_forms_it_was_written_for() -> None:
    """Non-vacuity: the old strings are what the regexes must catch."""
    assert _group_command("index", "repo") is not None and _group_command("index", "path") is None
    purge = _group_command("catalog", "purge-trash")
    real = {opt for p in purge.params for opt in (*p.opts, *p.secondary_opts)}
    assert "--execute" not in real and {"--no-dry-run", "--confirm"} <= real
    assert _INDEX.search("run nx index <path> --force now") and _PURGE.search("nx catalog purge-trash --execute")


@pytest.mark.parametrize("old", ["nx index <path> --force", "nx index --force"])
def test_the_old_forms_are_gone_from_the_scanned_files(old: str) -> None:
    for rel in FILES:
        assert old not in (SRC / rel).read_text(encoding="utf-8"), f"{rel} still says {old!r}"
