# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx aura`` (RDR-249 Phase 1 Step 2, bead nexus-mdbds.10.2): check and list declarative auras.

``check`` runs :func:`nexus.auras.check.check_auras` over every aura file of both library roots
(or one), reads the engine's registry for the dictionary checks, and prints each skipped check by
name. ``list`` shows each aura with its trigger and whether it is on, and the level that decided.
Nothing here runs an aura.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from nexus.auras import (
    Aura,
    AuraConfigError,
    AuraEntry,
    AuraError,
    enumerate_auras,
    load_aura,
    resolve,
)
from nexus.auras.check import check_auras
from nexus.lua_runtime.header import RepoProbeError
from nexus.lua_runtime.library import LibraryError


def _cwd() -> Path | None:
    try:
        return Path.cwd()
    except OSError:  # a deleted working directory has no repo root
        return None


def _templates() -> tuple[list[dict[str, Any]] | None, str]:
    """The registry's templates with their docs, or ``(None, why)`` when the engine cannot say.

    The one engine seam of the command. Any failure to read the registry means the dictionary
    checks are skipped, by name, with the reason printed; it is never a pass.
    """
    from nexus.db.t2.http_tuple_store import HttpTupleStore  # noqa: PLC0415 — deferred: CLI startup cost

    store = None
    try:
        store = HttpTupleStore()
        return list(store.registry(doc=True).get("templates", [])), ""
    except Exception as exc:  # noqa: BLE001 — CLI boundary: report the skip, never a traceback
        return None, f"the engine registry could not be read ({type(exc).__name__}: {exc})"
    finally:
        if store is not None:
            store.close()


def _fail(message: str) -> SystemExit:
    click.echo(f"Error: {message}", err=True)
    return SystemExit(1)


def _entries() -> list[AuraEntry]:
    try:
        return enumerate_auras(cwd=_cwd())
    except RepoProbeError as exc:
        raise _fail(f"cannot tell where the repository is: {exc}") from exc


@click.group(name="aura")
def aura_group() -> None:
    """Declarative auras (RDR-249): check them and list them. Every aura is off until turned on."""


@aura_group.command(name="check")
@click.argument("name", required=False)
def check_cmd(name: str | None) -> None:
    """Check every aura file (or NAME): its grammar, its procedure and argument mapping, its
    [clear] table, a shared threads block, and its fields against the tuple dictionary.

    The dictionary checks read the engine's registry. When it cannot be read they are skipped,
    and each skipped check is printed by name with the reason: a skipped check is never a pass.
    Exits 1 when any aura is refused.
    """
    entries = _entries()
    selected = [e for e in entries if name is None or e.name == name]
    if name is not None and not selected:
        reason = "no aura file of that name in a root that is on"
        try:
            load_aura(name, cwd=_cwd())
        except LibraryError as exc:
            reason = f"{exc.reason}: {exc.detail}"
        except (AuraError, RepoProbeError) as exc:
            reason = str(exc)
        raise _fail(f"{name}: {reason}")
    if not selected:
        click.echo("No aura files found.")
        return
    templates: list[dict[str, Any]] | None = None
    why = ""
    if any(isinstance(e.result, Aura) for e in selected):
        templates, why = _templates()
    try:
        results = check_auras(entries, cwd=_cwd(), templates=templates, unavailable=why, only=name)
    except RepoProbeError as exc:
        raise _fail(f"cannot tell where the repository is: {exc}") from exc
    refused = 0
    for c in results:
        if c.ok:
            tail = "pass" if not c.skipped else "pass; skipped: " + ", ".join(s.check for s in c.skipped)
        else:
            refused += 1
            tail = "REFUSED" + ("; skipped: " + ", ".join(s.check for s in c.skipped) if c.skipped else "")
        click.echo(f"{c.name} [{c.root}]: {tail}")
        for f in c.findings:
            click.echo(f"  {f.check} {f.key}: {f.detail}")
        for s in c.skipped:
            click.echo(f"  skipped {s.check}: {s.reason}")
    skipped_total = sum(1 for c in results if c.skipped)
    click.echo(f"{len(results)} checked, {refused} refused, {skipped_total} with skipped checks.")
    if refused:
        raise SystemExit(1)


def _match_text(match: tuple[tuple[str, str], ...]) -> str:
    return " {" + ",".join(f"{k}={v}" for k, v in match) + "}" if match else ""


@aura_group.command(name="list")
@click.option("--json", "json_out", is_flag=True, default=False, help="Output as JSON.")
def list_cmd(json_out: bool) -> None:
    """List every aura file: name, summary, trigger, on or off, and the level that decided
    (default, global config, repo config or env). Auras are off by default; NX_AURAS_OFF only
    turns them off. A file that breaks the grammar is listed with its refusal."""
    entries = _entries()
    try:
        states = resolve([e.name for e in entries])
    except AuraConfigError as exc:
        raise _fail(str(exc)) from exc
    rows: list[dict[str, Any]] = []
    for e in entries:
        d = states[e.name]
        r = e.result
        if isinstance(r, Aura):
            rows.append({
                "name": e.name, "root": e.root, "summary": r.summary,
                "trigger": {"subspace": r.subspace, "match": dict(r.match)},
                "state": "on" if d.on else "off", "level": d.level, "error": None,
            })
        else:
            rows.append({
                "name": e.name, "root": e.root, "summary": f"refused: {r}", "trigger": None,
                "state": "on" if d.on else "off", "level": d.level, "error": str(r),
            })
    if json_out:
        click.echo(json.dumps(rows))
        return
    if not rows:
        click.echo("No aura files found.")
        return
    for row in rows:
        trig = row["trigger"]
        trigger = "-" if trig is None else trig["subspace"] + _match_text(tuple(trig["match"].items()))
        click.echo(f"{row['name']}  {row['summary']}  {trigger}  {row['state']}  {row['level']}")
