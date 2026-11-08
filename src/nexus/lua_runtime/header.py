# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A procedure's header and its caller's arguments (RDR-240 Technical Design 1 and 2; nexus-mdbds.9).

A procedure file opens with four Lua comment lines, in this order::

    -- procedure: research_add
    -- summary: Build an RDR research record; the caller writes it with memory_put.
    -- args: rdr:integer, classification:string, repo:string
    -- grants: memory.list, tables.get

:func:`parse` is the only reader of those lines (RDR-240 Technical Design 1; RDR-242 residual
S4). :func:`prepare_args` checks the caller's arguments against them and adds the reserved
``now`` and ``repo`` when the header declares them (RDR-240 Technical Design 2). Both refuse
with :class:`~nexus.lua_runtime.errors.ScriptError`, of kind ``header`` or ``args``, before
any worker starts. This module runs in the calling process, never in the worker; the grants
it returns go to the static check, which cross-checks them against the bytecode.
Interface: T2 nexus/rdr242-interfaces, section C.
"""
from __future__ import annotations

import math
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from nexus.lua_runtime.errors import ScriptError
from nexus.lua_runtime.verb_types import JSONValue

#: The header's keys, in the order the file must give them.
KEYS: tuple[str, ...] = ("procedure", "summary", "args", "grants")
#: Argument types (RDR-240 Technical Design 1); a trailing ``?`` marks an optional argument.
TYPES: frozenset[str] = frozenset({"string", "integer", "number", "boolean"})
#: Argument names the host fills and a caller may never pass (Technical Design 2).
RESERVED: frozenset[str] = frozenset({"now", "repo"})
#: The run path's name rule (RDR-242 Technical Design 4); ``procedure`` must equal the file name.
NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
ARG_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
VERB_RE = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")
MAX_SUMMARY: int = 100
)
#: The largest integer a Lua 5.4 integer holds; a JSON integer outside it is refused.
LUA_INT_MAX: int = 2**63 - 1
LUA_INT_MIN: int = -(2**63)
#: How long the repo probe may take.
GIT_TIMEOUT: float = 10.0


@dataclass(frozen=True)
class ArgSpec:
    """One declared argument."""

    name: str
    type: str
    optional: bool = False


@dataclass(frozen=True)
class Header:
    """A parsed procedure header."""

    procedure: str
    summary: str
    args: tuple[ArgSpec, ...]
    grants: frozenset[str]


def _header_error(line: int, why: str) -> ScriptError:
    return ScriptError("header", f"line {line}: {why}")


def _split_lines(source: bytes) -> list[bytes]:
    """The first four lines, each without its terminator: ``\\n``, or ``\\r\\n`` (a checkout
    with Windows line endings). A ``\\r`` anywhere else stays in the line and is refused."""
    out: list[bytes] = []
    rest = source
    while len(out) < len(KEYS) and rest:
        line, nl, rest = rest.partition(b"\n")
        if nl and line.endswith(b"\r"):
            line = line[:-1]
        out.append(line)
    return out


def parse(source: bytes, *, name: str) -> Header:
    """Parse the four header lines of the procedure file *name* (its base name, no ``.lua``).

    Raises ``ScriptError("header", "line N: <why>")`` for a missing line, a key out of order,
    any byte outside printable ASCII or tab, a value that breaks its rule, or a
    ``procedure`` that is not *name*.
    """
    lines = _split_lines(source)
    values: list[str] = []
    for i, key in enumerate(KEYS):
        n = i + 1
        if i >= len(lines):
            raise _header_error(n, f"missing; this line must be '-- {key}: ...'")
        raw = lines[i]
        for b in raw:
            if not (32 <= b <= 126 or b == 9):
                raise _header_error(n, f"byte {b} is not printable ASCII or tab")
        text = raw.decode("ascii")
        prefix = f"-- {key}: "
        if not text.startswith(prefix):
            found = re.match(r"--\s*([A-Za-z_]+)\s*:", text)
            if found and found.group(1) in KEYS and found.group(1) != key:
                raise _header_error(n, f"key '{found.group(1)}' out of order; this line must be '-- {key}: ...'")
            raise _header_error(n, f"must be '-- {key}: <value>'")
        value = text[len(prefix):]
        if not value or value != value.strip(" \t"):
            raise _header_error(n, f"the {key} value is empty or has surrounding whitespace")
        values.append(value)
    procedure, summary, args_text, grants_text = values
    if not NAME_RE.fullmatch(procedure):
        raise _header_error(1, f"procedure '{procedure}' breaks the name rule [a-z][a-z0-9_]{{0,63}}")
    if procedure != name:
        raise _header_error(1, f"procedure '{procedure}' is not the file name '{name}'")
    if len(summary) > MAX_SUMMARY:
        raise _header_error(2, f"summary is {len(summary)} characters, over {MAX_SUMMARY}")
    return Header(procedure, summary, _parse_args(args_text), _parse_grants(grants_text))


def _items(text: str, line: int, what: str) -> list[str]:
    items = [s.strip(" \t") for s in text.split(",")]
    if any(not s for s in items):
        raise _header_error(line, f"an empty {what} item")
    if "none" in items and len(items) > 1:
        raise _header_error(line, f"'none' must stand alone in {what}")
    return [] if items == ["none"] else items


def _parse_args(text: str) -> tuple[ArgSpec, ...]:
    specs: list[ArgSpec] = []
    seen: set[str] = set()
    for item in _items(text, 3, "args"):
        name, colon, typ = item.partition(":")
        if not colon:
            raise _header_error(3, f"argument '{item}' has no ':type'")
        optional = typ.endswith("?")
        typ = typ.removesuffix("?")
        if not ARG_NAME_RE.fullmatch(name):
            raise _header_error(3, f"argument name '{name}' breaks [a-z][a-z0-9_]{{0,31}}")
        if typ not in TYPES:
            raise _header_error(3, f"argument '{name}' has unknown type '{typ}'")
        if name in seen:
            raise _header_error(3, f"argument '{name}' is named twice")
        if name in RESERVED and typ != "string":
            raise _header_error(3, f"reserved argument '{name}' must be a string")
        seen.add(name)
        specs.append(ArgSpec(name, typ, optional))
    return tuple(specs)


def _parse_grants(text: str) -> frozenset[str]:
    grants: list[str] = []
    for item in _items(text, 4, "grants"):
        if not VERB_RE.fullmatch(item):
            raise _header_error(4, f"grant '{item}' is not a verb name such as memory.get")
        if item in grants:
            raise _header_error(4, f"grant '{item}' is named twice")
        grants.append(item)
    return frozenset(grants)


# ---- arguments ----

def utc_now() -> datetime:
    """The host clock, the default for :func:`prepare_args`."""
    return datetime.now(UTC)


def _iso_utc(t: datetime) -> str:
    if t.tzinfo is None:
        raise ValueError("the clock must return an aware datetime")
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_env() -> dict[str, str]:
    # A caller's GIT_DIR or GIT_WORK_TREE would answer for some other repository.
    drop = {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_PREFIX"}
    env = {k: v for k, v in os.environ.items() if k not in drop}
    # git's messages are read below (_NO_WORK_TREE), so they must be the untranslated ones.
    env.update(LC_ALL="C", LANGUAGE="C")
    return env


#: git's own words when the directory has no work tree: outside any repository, or inside a
#: .git directory or a bare repository. Any other refusal is a probe failure.
_NO_WORK_TREE: tuple[str, ...] = ("not a git repository", "must be run in a work tree")


class RepoProbeError(Exception):
    """git could not say whether the directory is in a repo (git missing, a timeout, or a
    refusal other than "not a git repository", such as dubious ownership)."""


@dataclass(frozen=True)
class RepoProbe:
    """Where a run's repo is: the work-tree top (``VerbContext.repo_root``) and the
    worktree-stable name (the reserved argument ``repo``)."""

    root: Path
    name: str


def probe_repo(cwd: Path | None) -> RepoProbe | None:
    """The repo containing *cwd*, or ``None`` when *cwd* is in no work tree.

    This is the is-a-repo probe (RDR-240 gate r2 Significant 1): a directory that is not in a
    repo has no repo, never its own base name. One ``git rev-parse --show-toplevel
    --git-common-dir`` with the caller's ``GIT_DIR`` and the like removed gives both the top
    and the main checkout: the name is the parent of the common git directory, the rule
    ``nexus.repo_identity._resolve_main_repo`` applies for ``nx rdr``'s ``<repo>_rdr``, so a
    linked worktree gives the main checkout's name. Nothing is cached, so a transient failure
    never outlives its run. Raises :class:`RepoProbeError` when git cannot answer.
    """
    from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 - deferred: CLI startup cost

    # No directory, or one that no longer exists, is in no repo. Measured 2026-11-08: under
    # Claude Desktop chat the MCP server's cwd is Desktop's copy of the plugin, which Desktop
    # deletes when it replaces the plugin, so the process can outlive its own cwd.
    if cwd is None or not Path(cwd).is_dir():
        return None
    try:
        r = run_bounded(["git", "rev-parse", "--show-toplevel", "--git-common-dir"],
                        cwd=cwd, env=_git_env(), timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepoProbeError(f"git rev-parse did not run: {exc}") from exc
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        if any(phrase in err for phrase in _NO_WORK_TREE):
            return None
        raise RepoProbeError(f"git rev-parse exited {r.returncode}: {err[:200]}")
    # split("\n"), not splitlines(): a path may hold U+2028 or U+001C.
    lines = [ln for ln in r.stdout.split("\n") if ln.strip()]
    if len(lines) != 2:
        # Inside a .git directory or a bare repository there is no work tree.
        return None
    top, common = Path(lines[0]), Path(lines[1])
    if not common.is_absolute():
        common = Path(cwd) / common
    name = common.resolve().parent.name
    if not name:
        raise RepoProbeError(f"no repo name from git common dir {common}")
    return RepoProbe(top, name)


def repo_root(cwd: Path | None) -> Path | None:
    """The git work-tree top containing *cwd*, or ``None`` outside a repo."""
    probe = probe_repo(cwd)
    return probe.root if probe else None


def repo_name(cwd: Path | None) -> str | None:
    """The worktree-stable repo name for *cwd* (the main checkout's base name), or ``None``
    outside a repo."""
    probe = probe_repo(cwd)
    return probe.name if probe else None


def _args_error(why: str) -> ScriptError:
    # A caller's name can hold a lone surrogate; the error goes out as UTF-8 JSON.
    return ScriptError("args", why.encode("utf-8", "backslashreplace").decode("utf-8"))


def _check_type(spec: ArgSpec, value: object) -> JSONValue:
    t = spec.type
    if t == "string" and isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise _args_error(f"argument '{spec.name}' is not valid UTF-8 text") from exc
        return value
    if t == "boolean" and isinstance(value, bool):
        return value
    if t in ("integer", "number") and isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise _args_error(f"argument '{spec.name}' must be a finite {t}, got {value!r}")
        if t == "integer" and isinstance(value, float):
            if not value.is_integer():
                raise _args_error(f"argument '{spec.name}' must be an integer, got {value!r}")
            value = int(value)
        if isinstance(value, int) and not LUA_INT_MIN <= value <= LUA_INT_MAX:
            # Lua 5.4 holds an integer in 64 bits; lupa refuses a larger Python int.
            raise _args_error(f"argument '{spec.name}' is outside the 64-bit integer range")
        return value
    raise _args_error(f"argument '{spec.name}' must be {t}, got {type(value).__name__}")


def prepare_args(
    header: Header,
    args: Mapping[str, JSONValue],
    *,
    cwd: Path | None,
    clock: Callable[[], datetime] = utc_now,
    repo_probe: Callable[[Path | None], RepoProbe | None] = probe_repo,
) -> dict[str, JSONValue]:
    """Check *args* against *header*, fill the reserved values, and return the arguments the
    script receives, in header order; an absent optional argument is omitted (a JSON null
    counts as absent).

    The checks run as whole passes, in this order (RDR-240 gate r2 Significant 1):

    1. a caller-passed ``now`` or ``repo`` is refused;
    2. the reserved values the header declares are filled: ``now`` from *clock*, ``repo``
       from *repo_probe* over *cwd* (absent outside a repo; a probe git cannot answer is
       refused, never read as "no repo");
    3. a name that is not a string, or that the header does not declare, is refused;
    4. every missing required argument is refused, the first in header order named;
    5. each value's type is checked.

    *cwd* is the directory the run belongs to: the calling process's working directory, or
    ``None`` when it has none (``os.getcwd()`` raised); either way a directory in no repo gives
    no ``repo``. ``run_script`` runs in the MCP server or the ``nx`` process (the worker's own
    cwd is a temporary directory). Measured 2026-11-08 (nexus-mdbds.9): the plugin's launcher
    moves the MCP server to ``CLAUDE_PROJECT_DIR``, the directory the Claude Code session
    started in (the Desktop Code tab too); under Claude Desktop chat and Cowork the server stays
    in Desktop's copy of the plugin, which is no repo and can be deleted under it. *repo_probe* lets ``run_script`` probe once and reuse the root for
    ``VerbContext.repo_root``. Raises ``ScriptError("args", ...)`` naming the argument.
    """
    if not isinstance(args, Mapping):
        raise _args_error("arguments must be an object of name to value")
    if any(not isinstance(k, str) for k in args):
        raise _args_error("argument names must be strings")
    for name in sorted(RESERVED & set(args)):
        raise _args_error(f"argument '{name}' is reserved; the host fills it")
    declared = {s.name: s for s in header.args}
    given: dict[str, object] = dict(args)
    if "now" in declared:
        given["now"] = _iso_utc(clock())
    if "repo" in declared:
        try:
            probe = repo_probe(cwd)
        except RepoProbeError as exc:
            raise _args_error(f"argument 'repo' is reserved, and the repo could not be determined: {exc}") from exc
        if probe is not None:
            given["repo"] = probe.name
    for name in sorted(given):
        if name not in declared:
            raise _args_error(f"argument '{name}' is not declared in the header")
    for spec in header.args:
        if given.get(spec.name) is None and not spec.optional:
            if spec.name == "repo":
                raise _args_error("argument 'repo' is reserved, and there is no repo here to fill it")
            raise _args_error(f"argument '{spec.name}' is required")
    out: dict[str, JSONValue] = {}
    for spec in header.args:
        value = given.get(spec.name)
        if value is not None:
            out[spec.name] = _check_type(spec, value)
    return out
