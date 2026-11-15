# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The aura file and its parser (RDR-249 Technical Design 1 and 6; bead nexus-mdbds.10.1).

An aura is ``<name>.aura.toml``: a trigger on one board subspace, a map from tuple fields to a
procedure's arguments, an optional condition on the procedure's result, an optional clearing
match, a threads block to show it in, and optional lower limits. :func:`parse_aura` reads the
bytes with :mod:`tomllib` against a closed key set; any other key, a missing required key, or a
wrong type refuses the whole file. Every refusal is an :class:`AuraError` that names the file
and the key.

What the parser does not check belongs to ``nx aura check`` (RDR-249 Phase 1 Step 2): that the
procedure exists, that every required argument is mapped against its header, and that the
trigger's fields are in the dictionary.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, fields
from typing import Literal

from nexus.lua_runtime.header import ARG_NAME_RE, RESERVED
from nexus.lua_runtime.library import READ_LIMIT, LibraryError, _check_name
from nexus.lua_runtime.limits import Limits

#: The aura file suffix; the name is the file name less the whole of it.
AURA_SUFFIX: str = ".aura.toml"

#: Why a file is refused (``AuraError.kind``).
KINDS: tuple[str, ...] = (
    "bad_toml", "too_large", "unknown_key", "missing_key", "wrong_type", "invalid_name", "name_mismatch",
    "bad_subspace", "reserved_arg", "bad_source", "clear_equals_trigger", "bad_limit", "bad_threads",
)

#: ``AuraError.key`` for a refusal of the file as a whole.
FILE_KEY: str = "(file)"

# One concrete board subspace: board/<topic> or board/ci/<topic>. A topic carries no slash and
# no wildcard; no other prefix (mailbox triggers are cut from version 1).
_SUBSPACE_RE = re.compile(r"board/(?:ci/)?[A-Za-z0-9][A-Za-z0-9._-]*")
_FIELD = r"[A-Za-z_][A-Za-z0-9_]*"
_DIMS_RE = re.compile(rf"dims\.({_FIELD})")
_BODY_RE = re.compile(rf"body\.({_FIELD})")
_LITERAL_RE = re.compile(r'= "([^"\n]*)"')

_TOP_KEYS = frozenset({"aura", "summary", "procedure", "trigger", "args", "when", "clear", "display", "limits"})
_TRIGGER_KEYS = frozenset({"subspace", "match"})
_WHEN_KEYS = frozenset({"field"})
_CLEAR_KEYS = frozenset({"match"})
_DISPLAY_KEYS = frozenset({"threads", "channel"})
_LIMIT_KEYS = frozenset(f.name for f in fields(Limits))


class AuraError(Exception):
    """An aura file the parser refuses; ``kind`` is one of :data:`KINDS`.

    ``file`` is the file name (``<name>.aura.toml``) and ``key`` the dotted key at fault
    (``display.channel``; :data:`FILE_KEY` for the file as a whole). The message carries both.
    """

    def __init__(self, kind: str, file: str, key: str, detail: str) -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown AuraError kind {kind!r}")
        super().__init__(f"{file}: {key}: {detail}")
        self.kind = kind
        self.file = file
        self.key = key
        self.detail = detail


@dataclass(frozen=True)
class ArgSource:
    """Where an argument's value comes from: ``dims.<field>`` or ``body.<field>`` (``value`` is the
    field), the tuple ``id`` or ``subspace`` (``value`` empty), or a ``literal`` (``value`` is the text)."""

    kind: Literal["dims", "body", "id", "subspace", "literal"]
    value: str = ""


@dataclass(frozen=True)
class Aura:
    """A parsed aura file. ``match``, ``args`` and ``clear_match`` are pairs in file order."""

    name: str
    summary: str
    procedure: str
    subspace: str
    match: tuple[tuple[str, str], ...]
    args: tuple[tuple[str, ArgSource], ...]
    when_field: str | None
    clear_match: tuple[tuple[str, str], ...] | None
    threads: str
    channel: bool
    limits: Limits


class _Reader:
    """The checks of one file, each raising an :class:`AuraError` that names it."""

    def __init__(self, file: str) -> None:
        self.file = file

    def fail(self, kind: str, key: str, detail: str) -> AuraError:
        return AuraError(kind, self.file, key, detail)

    def table(self, value: object, key: str, allowed: frozenset[str] | None) -> dict[str, object]:
        """*value* as a table whose keys are all in *allowed* (``None``: any key)."""
        if not isinstance(value, dict):
            raise self.fail("wrong_type", key, f"must be a table, got {type(value).__name__}")
        for k in value:
            if allowed is not None and k not in allowed:
                prefix = f"{key}." if key else ""
                raise self.fail("unknown_key", f"{prefix}{k}", f"not a key of {key or 'an aura file'}")
        return value

    def string(self, value: object, key: str) -> str:
        if not isinstance(value, str):
            raise self.fail("wrong_type", key, f"must be a string, got {type(value).__name__}")
        return value

    def required(self, table: dict[str, object], name: str, prefix: str) -> object:
        if name not in table:
            raise self.fail("missing_key", f"{prefix}{name}", "required key is missing")
        return table[name]

    def name_of(self, value: str, key: str) -> str:
        try:
            return _check_name(value)
        except LibraryError as exc:
            raise self.fail("invalid_name", key, exc.detail) from exc

    def match(self, value: object, key: str) -> tuple[tuple[str, str], ...]:
        if not isinstance(value, dict):
            raise self.fail("wrong_type", key, f"must be an inline table of strings, got {type(value).__name__}")
        return tuple((k, self.string(v, f"{key}.{k}")) for k, v in value.items())

    def source(self, value: object, key: str) -> ArgSource:
        text = self.string(value, key)
        if text == "id":
            return ArgSource("id")
        if text == "subspace":
            return ArgSource("subspace")
        if m := _DIMS_RE.fullmatch(text):
            return ArgSource("dims", m.group(1))
        if m := _BODY_RE.fullmatch(text):
            return ArgSource("body", m.group(1))
        if m := _LITERAL_RE.fullmatch(text):
            return ArgSource("literal", m.group(1))
        raise self.fail(
            "bad_source", key,
            f"{text!r} is not dims.<field>, body.<field>, id, subspace or a literal written = \"value\"",
        )

    def limits(self, raw: dict[str, object]) -> Limits:
        defaults = Limits()
        for k, v in raw.items():
            key = f"limits.{k}"
            want = type(getattr(defaults, k))
            if isinstance(v, bool) or not isinstance(v, (int, float)) or (want is int and not isinstance(v, int)):
                raise self.fail("wrong_type", key, f"must be {'an integer' if want is int else 'a number'}, got {v!r}")
            try:
                defaults.lowered(**{k: v})
            except ValueError as exc:
                raise self.fail("bad_limit", key, str(exc)) from exc
        return defaults.lowered(**raw)  # type: ignore[arg-type]  # each value checked above


def parse_aura(data: bytes, *, name: str) -> Aura:
    """The :class:`Aura` in *data*, the bytes of ``<name>.aura.toml``.

    *name* is the file's name less the suffix; the file's ``aura`` key must equal it. Raises
    :class:`AuraError` for text that is not TOML or is over the library's read bound, a key
    outside the closed set, a missing required key, a wrong type, a name that breaks RDR-242's
    name rule, a subspace that is not one concrete ``board/<topic>`` or ``board/ci/<topic>``, a
    reserved argument mapped, an argument source of an unknown form, ``clear.match`` equal to
    ``trigger.match``, a threads value that is blank or has a control character, and a limit
    above its default (RDR-249 Technical Design 6).
    """
    r = _Reader(f"{name}{AURA_SUFFIX}")
    if len(data) >= READ_LIMIT:
        raise r.fail("too_large", FILE_KEY, f"is over {READ_LIMIT - 1} bytes")
    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise r.fail("bad_toml", FILE_KEY, f"is not TOML: {exc}") from exc

    top = r.table(doc, "", _TOP_KEYS)
    aura = r.string(r.required(top, "aura", ""), "aura")
    summary = r.string(r.required(top, "summary", ""), "summary")
    procedure = r.string(r.required(top, "procedure", ""), "procedure")
    trigger = r.table(r.required(top, "trigger", ""), "trigger", _TRIGGER_KEYS)
    subspace = r.string(r.required(trigger, "subspace", "trigger."), "trigger.subspace")
    display = r.table(r.required(top, "display", ""), "display", _DISPLAY_KEYS)
    threads = r.string(r.required(display, "threads", "display."), "display.threads")

    r.name_of(aura, "aura")
    if aura != name:
        raise r.fail("name_mismatch", "aura", f"is {aura!r}, and the file is named {name!r}")
    r.name_of(procedure, "procedure")
    if _SUBSPACE_RE.fullmatch(subspace) is None:
        raise r.fail(
            "bad_subspace", "trigger.subspace",
            f"{subspace!r} is not one concrete board/<topic> or board/ci/<topic>",
        )
    match = r.match(trigger["match"], "trigger.match") if "match" in trigger else ()

    args: list[tuple[str, ArgSource]] = []
    if "args" in top:
        for arg, value in r.table(top["args"], "args", None).items():
            key = f"args.{arg}"
            if arg in RESERVED:
                raise r.fail("reserved_arg", key, f"{arg!r} is reserved: the host fills it")
            if ARG_NAME_RE.fullmatch(arg) is None:
                raise r.fail("invalid_name", key, f"{arg!r} breaks the argument name rule [a-z][a-z0-9_]{{0,31}}")
            args.append((arg, r.source(value, key)))

    when_field: str | None = None
    if "when" in top:
        when = r.table(top["when"], "when", _WHEN_KEYS)
        when_field = r.string(r.required(when, "field", "when."), "when.field")

    clear_match: tuple[tuple[str, str], ...] | None = None
    if "clear" in top:
        clear = r.table(top["clear"], "clear", _CLEAR_KEYS)
        clear_match = r.match(r.required(clear, "match", "clear."), "clear.match")
        if dict(clear_match) == dict(match):
            raise r.fail("clear_equals_trigger", "clear.match", "equals trigger.match, so a post would both fire and clear")

    if not threads.strip() or not threads.isprintable():
        raise r.fail("bad_threads", "display.threads", "must be one printable line that is not blank")
    channel = display.get("channel", False)
    if not isinstance(channel, bool):
        raise r.fail("wrong_type", "display.channel", f"must be a boolean, got {type(channel).__name__}")

    limits = r.limits(r.table(top["limits"], "limits", _LIMIT_KEYS)) if "limits" in top else Limits()
    return Aura(
        name=aura, summary=summary, procedure=procedure, subspace=subspace, match=match, args=tuple(args),
        when_field=when_field, clear_match=clear_match, threads=threads, channel=channel, limits=limits,
    )
