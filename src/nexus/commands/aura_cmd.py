# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx aura`` (RDR-249 Phase 1 Step 2, bead nexus-mdbds.10.2): check and list declarative auras.

``check`` runs :func:`nexus.auras.check.check_auras` over every aura file of both library roots
(or one), reads the engine's registry for the dictionary checks, and prints each skipped check by
name. ``list`` shows each aura with its trigger and whether it is on, and the level that decided.
Nothing here runs an aura.

RDR-252: ``check`` warns when an aura triggered on a ``board/ci/<base>--owner`` subspace is not gated on the group
that serves ``<base>`` (a warning does not refuse; an unreadable group is a skip). ``list`` shows, for each aura that is
on, whether the session's MCP server loaded it and why (which group), read from that server's state file
(:mod:`nexus.auras.loaded_state`), and says to reconnect when the server's identity is not this process's.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NamedTuple

import click

from nexus.auras import (
    Aura,
    AuraConfigError,
    AuraEntry,
    AuraError,
    enumerate_auras,
    load_aura,
    resolve,
    unknown_names,
)
from nexus.auras import loaded_state
from nexus.auras.check import check_auras
from nexus.lua_runtime.header import RepoProbe, RepoProbeError, probe_repo
from nexus.lua_runtime.library import REPO_ROOT_ENV, REPO_ROOT_PARTS, LibraryError, repo_root_enabled
from nexus.auras.aura import AURA_SUFFIX


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


def _group_reader() -> Any:
    """The group read for the serving-group check. It builds its client on the first read, so an engine that cannot be
    reached is a read that raises ``GroupUnreadable`` and the check is skipped by name. The one group seam of the
    command (RDR-252)."""
    from nexus.auras.load_gate import default_group_reader  # noqa: PLC0415 -- deferred: CLI startup cost

    return default_group_reader()


def _member_groups() -> Any:
    """The names of the groups this process's identity belongs to, for the serving check of a ``{serves}`` template. A
    process with no identity, or an engine that cannot be reached, raises ``GroupUnreadable`` and the check is skipped by
    name. The one membership seam of the command (RDR-252)."""
    from nexus.auras.load_gate import GroupUnreadable  # noqa: PLC0415 -- deferred: CLI startup cost
    from nexus.auras.session import read_memberships  # noqa: PLC0415

    def read() -> list[str]:
        from nexus.identity import token_header  # noqa: PLC0415

        try:
            record = token_header.current()
        except Exception as exc:  # noqa: BLE001 -- an unreadable token is no identity
            raise GroupUnreadable(f"this process's identity cannot be read: {exc}") from exc
        if record is None:
            raise GroupUnreadable("this process holds no identity (nx identity show says whether the session has one)")
        return read_memberships(record.identity)

    return read


def _loaded_inputs() -> tuple[Path, Any, int | None, str | None, Path | None]:
    """What the state-file view needs of this process: the directory of the files, the process table, the pid of the
    nearest Claude ancestor (``None`` with none), the identity this process resolves to (``None`` when it cannot
    say) and the token-file directory the holder check reads. The one process seam of ``list``."""
    from nexus import config as _config  # noqa: PLC0415 -- deferred: CLI startup cost
    from nexus.auras.loaded_state import state_dir  # noqa: PLC0415
    from nexus.identity import token_header  # noqa: PLC0415
    from nexus.identity.token_file import identity_dir  # noqa: PLC0415
    from nexus.identity.holder import Processes  # noqa: PLC0415
    from nexus.session import _command_name_of, find_immediate_claude_pid  # noqa: PLC0415

    pid: int | None = None
    identity: str | None = None
    try:
        found = find_immediate_claude_pid()
        if found > 0 and _command_name_of(found).lower().startswith("claude"):
            pid = found
        record = token_header.current()
        identity = record.identity if record is not None else None
    except Exception:  # noqa: BLE001 -- a process that cannot be read is no ancestor and no identity
        pass
    config_dir = _config.nexus_config_dir()
    return state_dir(config_dir), Processes(), pid, identity, identity_dir(config_dir)


def _fail(message: str) -> SystemExit:
    click.echo(f"Error: {message}", err=True)
    return SystemExit(1)


class _Scope(NamedTuple):
    """Where this run is: the working directory, its repo probe, and the repo root that both the
    aura files (``<root>/.nexus/procedures/``) and the per-repo config (``<root>/.nexus.yml``) are
    read from. Computed once, so the two cannot diverge."""

    cwd: Path | None
    probe: RepoProbe | None
    repo_root: Path | None


def _scope() -> _Scope:
    cwd = _cwd()
    try:
        probe = probe_repo(cwd)
    except RepoProbeError as exc:
        if repo_root_enabled():  # the library would refuse too: a shadow check must see the repo root
            raise _fail(f"cannot tell where the repository is: {exc}") from exc
        probe = None  # root off: only the config lookup needs a root, and falls back to its default
    return _Scope(cwd, probe, probe.root if probe is not None else None)


def _entries(scope: _Scope) -> list[AuraEntry]:
    try:
        return enumerate_auras(cwd=scope.cwd, probe=scope.probe)
    except RepoProbeError as exc:
        raise _fail(f"cannot tell where the repository is: {exc}") from exc


def _unknown_name_warnings(entries: list[AuraEntry], scope: _Scope) -> None:
    """One warning line per configured or env name that matches no aura (it would fail open)."""
    try:
        unknown = unknown_names([e.name for e in entries], repo_root=scope.repo_root)
    except AuraConfigError as exc:
        raise _fail(str(exc)) from exc
    for name, source in unknown:
        where = f"{source} names no aura: {name}" if source == "NX_AURAS_OFF" else f"{source}: auras.{name} names no aura"
        click.echo(f"warning: {where}", err=True)


def _unscanned_repo_files(scope: _Scope) -> int:
    """How many ``*.aura.toml`` files sit in the repo root while its switch is off (0 when on)."""
    if repo_root_enabled() or scope.probe is None:
        return 0
    root = scope.probe.root.joinpath(*REPO_ROOT_PARTS)
    try:
        return sum(1 for p in root.glob(f"*{AURA_SUFFIX}") if p.is_file()) if root.is_dir() else 0
    except OSError:
        return 0


def _unscanned_note(count: int) -> str:
    return f"repo root not scanned ({REPO_ROOT_ENV} unset): {count} aura file(s) not checked"


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
    While NX_PROCEDURES_REPO_ROOT is off the repo's own aura files are not checked, and one line
    says how many (counted as a skip). A configured or NX_AURAS_OFF name that matches no aura is
    warned about on standard error. Exits 1 when any aura is refused.
    """
    scope = _scope()
    entries = _entries(scope)
    _unknown_name_warnings(entries, scope)
    unscanned = _unscanned_repo_files(scope)
    if unscanned:
        click.echo(_unscanned_note(unscanned))
    selected = [e for e in entries if name is None or e.name == name]
    if name is not None and not selected:
        reason = "no aura file of that name in a root that is on"
        try:
            load_aura(name, cwd=scope.cwd, probe=scope.probe)
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
        results = check_auras(
            entries, cwd=scope.cwd, templates=templates, unavailable=why, only=name, probe=scope.probe,
            read_group=_group_reader(), member_groups=_member_groups(),
        )
    except RepoProbeError as exc:
        raise _fail(f"cannot tell where the repository is: {exc}") from exc
    refused = 0
    for c in results:
        notes = ""
        if c.warnings:
            notes += "; warning: " + ", ".join(sorted({w.check for w in c.warnings}))
        if c.skipped:
            notes += "; skipped: " + ", ".join(s.check for s in c.skipped)
        if c.ok:
            tail = "pass" + notes
        else:
            refused += 1
            tail = "REFUSED" + notes
        click.echo(f"{c.name} [{c.root}]: {tail}")
        for f in c.findings:
            click.echo(f"  {f.check} {f.key}: {f.detail}")
        for w in c.warnings:
            click.echo(f"  warning {w.check} {w.key}: {w.detail}")
        for s in c.skipped:
            click.echo(f"  skipped {s.check}: {s.reason}")
    skipped_total = sum(1 for c in results if c.skipped) + (1 if unscanned else 0)
    warned_total = sum(1 for c in results if c.warnings)
    click.echo(
        f"{len(results)} checked, {refused} refused, {skipped_total} with skipped checks"
        + (f", {warned_total} with warnings" if warned_total else "")
        + "."
    )
    if refused:
        raise SystemExit(1)


#: Load reasons that come from a read the server made once at start and does not repeat: only a reconnect tries again.
_RETRY_ON_RECONNECT = frozenset({"memberships_unreadable", "group_unreadable"})
_RECONNECT_HINT = "; the engine could not be read at start: reconnect the MCP server (/mcp) once it is up"


def _loaded_text(load: dict[str, Any] | None) -> str:
    """``  loaded`` or ``  not loaded: <reason> (group <g>)`` for an aura the server's state file speaks of."""
    if load is None:
        return ""
    group = f" (group {load['group']})" if load["group"] else ""
    if load["loaded"]:
        if load.get("bases"):  # a {serves} template: each base with the group that serves it
            return "  loaded for " + ", ".join(f"{b['base']} ({b['group']})" if b["group"] else b["base"] for b in load["bases"])
        return f"  loaded{group}"
    hint = _RECONNECT_HINT if load["reason"] in _RETRY_ON_RECONNECT else ""
    return f"  not loaded: {load['reason']}{group}{hint}"


def _template_load(
    loaded: dict[str, loaded_state.AuraState], name: str,
) -> tuple[loaded_state.AuraState | None, list[tuple[str, str | None]]]:
    """What the state file says of a ``{serves}`` template (RDR-252 "Aura per base"): the server records its expansions as
    ``<name>@<base>``, so the template is loaded when any expansion is, for those bases and the group that serves each;
    otherwise the reason of the first refused one. ``(None, [])`` when the file says nothing of it."""
    prefix = f"{name}@"
    expansions = {k[len(prefix):]: v for k, v in sorted(loaded.items()) if k.startswith(prefix)}
    up = [(b, v.group) for b, v in expansions.items() if v.loaded]
    if up:
        return loaded_state.AuraState(True, "loaded", None), up
    if expansions:
        first = next(iter(expansions.values()))
        return loaded_state.AuraState(False, first.reason, first.group), []
    return None, []


def _unclaimed_files_text(listed: tuple[loaded_state.LoadedState, ...]) -> list[str]:
    """The lines for ``list`` run with no Claude ancestor: each state file's holder and identity, and each aura's load
    decision. It claims none of them, so nothing here reaches the table or ``--json``."""
    lines = ["no Claude ancestor: showing every MCP server's state file, none of them this process's"]
    for state in listed:
        name = loaded_state.state_path(Path(), state.pid, state.started_at).name
        identity = f"identity {state.identity}" if state.identity else "no identity"
        lines.append(f"  {name}: claude pid {state.pid}, {identity}, server pid {state.server_pid}")
        for aura, seen in sorted(state.auras.items()):
            group = f" (group {seen.group})" if seen.group else ""
            lines.append(f"    {aura}: " + (f"loaded{group}" if seen.loaded else f"not loaded: {seen.reason}{group}"))
    return lines


def _match_text(match: tuple[tuple[str, str], ...]) -> str:
    return " {" + ",".join(f"{k}={v}" for k, v in match) + "}" if match else ""


@aura_group.command(name="list")
@click.option("--json", "json_out", is_flag=True, default=False, help="Output as JSON.")
def list_cmd(json_out: bool) -> None:
    """List every aura file: name, summary, trigger, on or off, and the level that decided
    (default, global config, repo config or env). Auras are off by default; NX_AURAS_OFF only
    turns them off. A file that breaks the grammar is listed with its refusal. Warnings (a name
    in the config or NX_AURAS_OFF that matches no aura; the repo root not scanned) go to standard
    error, so --json stays parseable.

    For an aura that is on, the row also says whether this session's MCP server loaded it and why
    (for a group-gated aura, which group). That comes from the server's state file, found through
    the nearest Claude ancestor of this process; with no file, nothing is said. When the identity
    the auras were loaded for is not the one this process now resolves to (a cross-seat /resume),
    standard error says to reconnect the MCP server."""
    scope = _scope()
    entries = _entries(scope)
    try:
        states = resolve([e.name for e in entries], repo_root=scope.repo_root)
    except AuraConfigError as exc:
        raise _fail(str(exc)) from exc
    _unknown_name_warnings(entries, scope)
    unscanned = _unscanned_repo_files(scope)
    if unscanned:
        click.echo(_unscanned_note(unscanned), err=True)
    directory, procs, claude_pid, identity, token_dir = _loaded_inputs()
    view = loaded_state.view(directory, claude_pid=claude_pid, procs=procs, current_identity=identity, identity_dir=token_dir)
    if view.mismatch:
        click.echo(f"warning: {view.mismatch}", err=True)
    if not view.claimed and view.listed:
        for line in _unclaimed_files_text(view.listed):
            click.echo(line, err=True)
    loaded = view.state.auras if view.state is not None else {}
    #: An aura that is on and group-gated, for a caller with a Claude ancestor whose server wrote no file: the server
    #: never started, or it never bound an identity (a Desktop server whose bind did not come from the pending file).
    unwritten: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    for e in entries:
        d = states[e.name]
        r = e.result
        seen = loaded.get(e.name) if d.on else None
        bases: list[tuple[str, str | None]] = []
        if d.on and isinstance(r, Aura) and r.is_template and seen is None:
            seen, bases = _template_load(loaded, e.name)
        if d.on and view.claimed and view.state is None and isinstance(r, Aura) and (r.load_group or r.is_template):
            unwritten[e.name] = (
                f"  not loaded: no state file from this session's MCP server{f' (group {r.load_group})' if r.load_group else ''}; "
                "it has not started or has no identity: reconnect it (/mcp) once the session is bound"
            )
        load = None if seen is None else {
            "loaded": seen.loaded, "reason": seen.reason, "group": seen.group,
            **({"bases": [{"base": b, "group": g} for b, g in bases]} if bases else {}),
        }
        if isinstance(r, Aura):
            rows.append({
                "name": e.name, "root": e.root, "summary": r.summary,
                "trigger": {"subspace": r.subspace, "match": dict(r.match)},
                "state": "on" if d.on else "off", "level": d.level, "error": None, "loaded": load,
            })
        else:
            rows.append({
                "name": e.name, "root": e.root, "summary": f"refused: {r}", "trigger": None,
                "state": "on" if d.on else "off", "level": d.level, "error": str(r), "loaded": load,
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
        shown = _loaded_text(row["loaded"]) or unwritten.get(row["name"], "")
        click.echo(f"{row['name']}  {row['summary']}  {trigger}  {row['state']}  {row['level']}{shown}")
