# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx run`` - run a library procedure (RDR-240 Technical Design 3; nexus-qo7ie.31).

``nx run <name> [--args-file F | -]`` calls
:func:`nexus.lua_runtime.runner.run_procedure` and nothing else, prints the JSON result on
standard output, and shows an error as ``<kind>: <detail>`` on standard error with exit 1.
``nx run --list`` lists the library's procedures with their header summary. The body of
:func:`_run` is pinned by ``tests/test_procedure_front_doors_lint.py``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click

from nexus.lua_runtime import library
from nexus.lua_runtime.header import RepoProbeError
from nexus.lua_runtime.runner import PROCEDURE_ERRORS, describe_error, run_procedure


def _refuse_constant(constant: str) -> object:
    """``json.loads`` accepts ``NaN`` and ``Infinity``, which are not JSON; the sandbox's
    argument check would see a float it cannot send on, so the front door says so first."""
    raise ValueError(f"{constant} is not a JSON number")


def _load_args(args_file: str | None, source: str | None) -> dict[str, Any]:
    """The procedure's arguments: a JSON object from ``--args-file F`` (``-`` is standard
    input), from a bare ``-``, or ``{}`` when neither is given."""
    if source is not None and source != "-":
        raise click.UsageError(f"unexpected argument {source!r}; the only second argument is '-' (standard input)")
    if args_file is not None and source is not None:
        raise click.UsageError("give the arguments once: --args-file F, or '-', not both")
    where = args_file if args_file is not None else source
    if where is None:
        return {}
    try:
        text = sys.stdin.read() if where == "-" else Path(where).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise click.UsageError(f"cannot read the arguments from {where}: {exc}") from exc
    try:
        args = json.loads(text, parse_constant=_refuse_constant)
    except (ValueError, RecursionError) as exc:  # RecursionError: nesting past the parser's depth
        raise click.UsageError(f"the arguments in {where} are not valid JSON: {exc}") from exc
    if not isinstance(args, dict):
        raise click.UsageError(f"the arguments in {where} must be a JSON object, not {type(args).__name__}")
    return args


def _run(name: str, args: dict[str, Any]) -> None:
    try:
        result = run_procedure(name, args)
    except PROCEDURE_ERRORS as exc:
        click.echo(describe_error(exc), err=True)
        sys.exit(1)
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


def _list() -> None:
    try:
        entries = library.procedures(cwd=Path.cwd())
    except RepoProbeError as exc:
        click.echo(f"repo procedures not listed: {exc}", err=True)
        entries = library.procedures(cwd=None)
    for p in entries:
        click.echo(f"{p.name}\t{p.summary}" + ("\t(repo)" if p.root == "repo" else ""))
    if not library.repo_root_enabled():
        click.echo(
            f"repo procedures (.nexus/procedures/) are off; set {library.REPO_ROOT_ENV}=1 to run them",
            err=True,
        )


@click.command("run")
@click.argument("name", required=False)
@click.argument("source", required=False)
@click.option("--args-file", "args_file", default=None, metavar="F",
              help="JSON object of the procedure's arguments, read from the file F ('-' is standard input).")
@click.option("--list", "list_", is_flag=True, help="List the library's procedures with their summaries and exit.")
def run_cmd(name: str | None, source: str | None, args_file: str | None, list_: bool) -> None:
    """Run the library procedure NAME and print its JSON result.

    The arguments are a JSON object given with --args-file F, or on standard input
    with a bare '-'; none means {}. An error prints as '<kind>: <detail>' and exits 1.
    """
    if list_:
        if name is not None or source is not None or args_file is not None:
            raise click.UsageError("--list takes no name and no arguments")
        _list()
        return
    if name is None:
        raise click.UsageError("missing argument NAME (or use --list)")
    _run(name, _load_args(args_file, source))
