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
from collections.abc import Iterable
from dataclasses import dataclass, fields, replace
from typing import Literal

from nexus.ci_base import branch_of
from nexus.lua_runtime.header import ARG_NAME_RE, RESERVED
from nexus.lua_runtime.library import READ_LIMIT, LibraryError, check_name
from nexus.lua_runtime.limits import Limits

#: The aura file suffix; the name is the file name less the whole of it.
AURA_SUFFIX: str = ".aura.toml"

#: Why a file is refused (``AuraError.kind``).
KINDS: tuple[str, ...] = (
    "bad_toml", "too_large", "unknown_key", "missing_key", "wrong_type", "invalid_name", "name_mismatch",
    "bad_subspace", "reserved_arg", "bad_source", "clear_equals_trigger", "bad_limit", "bad_threads",
    "bad_summary", "bad_field", "bad_group",
)

#: Separates a template's name from the base in an expansion's name (``ci_develop_red@nexus-develop``).
EXPANSION_SEP: str = "@"

#: The placeholder a template's trigger subspace carries once, inside its topic (RDR-252 "Aura per base"): the loader
#: expands it into one aura per base in the ``serves`` list of the ``[load]`` group. It is recognised on purpose, never
#: read as a literal topic.
SERVES_PLACEHOLDER: str = "{serves}"
#: The two placeholders of a template's ``display.threads``: the base (required, so no two expansions share a block) and
#: the branch, the part of the base after its last hyphen (:func:`nexus.ci_base.branch_of`).
BASE_PLACEHOLDER: str = "{base}"
BRANCH_PLACEHOLDER: str = "{branch}"
#: The ``self.<name>`` an argument may take from the base of a template's expansion.
SELF_BRANCH: str = "self.branch"
#: The longest ``display.threads`` value: it becomes the heading ``## aura: <value>`` in a threads entry.
MAX_THREADS_CHARS: int = 80
#: ``AuraError.key`` for a refusal of the file as a whole.
FILE_KEY: str = "(file)"

# One concrete board subspace: board/<topic> or board/ci/<topic>. A topic carries no slash and
# no wildcard; no other prefix (mailbox triggers are cut from version 1).
# A topic is any run of characters but "/", "*" and whitespace: the repo's own topics are looser
# than a slug (ci_wait builds board/ci/{repo}-{branch}--done unsanitized). "board/ci" alone is no
# topic: ci/ needs a topic after it.
_SUBSPACE_RE = re.compile(r"board/(?:ci/[^/*\s]+|(?!ci$)[^/*\s]+)")
_FIELD = r"[A-Za-z_][A-Za-z0-9_]*"
_DIMS_RE = re.compile(rf"dims\.({_FIELD})")
_BODY_RE = re.compile(r"body\.([^/*\s.]+)")
_FIELD_RE = re.compile(_FIELD)
_LITERAL_RE = re.compile(r'= "([^"\n]*)"')
#: A group name: the engine's address-segment pattern, at most 64 characters (``IdentityMemberships.GROUP``).
_GROUP_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
#: The one argument source the host fills itself: the session's identity id at load (RDR-252).
SELF_IDENTITY: str = "self.identity"

_TOP_KEYS = frozenset({"aura", "summary", "procedure", "trigger", "args", "when", "clear", "display", "limits", "load"})
_TRIGGER_KEYS = frozenset({"subspace", "match"})
_WHEN_KEYS = frozenset({"field"})
_LOAD_KEYS = frozenset({"group"})
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
    field), the tuple ``id`` or ``subspace`` (``value`` empty), a ``literal`` (``value`` is the text), or
    ``self.identity`` (kind ``self``, ``value`` ``identity``): the host fills it at load, not the row. A template's
    ``self.branch`` (kind ``self``, ``value`` ``branch``) becomes a ``literal`` of the branch when the template expands."""

    kind: Literal["dims", "body", "id", "subspace", "literal", "self"]
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
    #: ``True`` sends the channel notice on every fire; a string is the name of a boolean field of the procedure's
    #: result, and the notice goes out only when that field is ``True`` (RDR-252).
    channel: bool | str
    limits: Limits
    #: ``[load] group``: the aura loads only in a session whose identity is a member of that group (RDR-252).
    load_group: str | None = None
    #: The base a template's expansion is for (``nexus-develop``); ``None`` for a file's own aura and for a template.
    base: str | None = None
    #: The template's name when this aura is one of its expansions (:func:`expand_template`).
    expanded_from: str | None = None

    @property
    def is_template(self) -> bool:
        """True for an aura whose trigger subspace carries :data:`SERVES_PLACEHOLDER`: it is expanded, never subscribed."""
        return SERVES_PLACEHOLDER in self.subspace


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
            return check_name(value)
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
        if text == SELF_IDENTITY:
            return ArgSource("self", "identity")
        if text == SELF_BRANCH:
            return ArgSource("self", "branch")
        if m := _DIMS_RE.fullmatch(text):
            return ArgSource("dims", m.group(1))
        if m := _BODY_RE.fullmatch(text):
            return ArgSource("body", m.group(1))
        if m := _LITERAL_RE.fullmatch(text):
            return ArgSource("literal", m.group(1))
        raise self.fail(
            "bad_source", key,
            f"{text!r} is not dims.<field>, body.<field>, id, subspace, {SELF_IDENTITY}, {SELF_BRANCH} (a template's) or a literal written = \"value\"",
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
    ``trigger.match``, a threads value that is blank, has a control character, is not ASCII or
    is over 80 characters, and a limit above its default (RDR-249 Technical Design 6).
    """
    r = _Reader(f"{name}{AURA_SUFFIX}")
    if len(data) >= READ_LIMIT:
        raise r.fail("too_large", FILE_KEY, f"is over {READ_LIMIT - 1} bytes")
    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise r.fail("bad_toml", FILE_KEY, f"is not TOML: {exc}") from exc
    except RecursionError as exc:  # a file of nested arrays under the size bound
        raise r.fail("bad_toml", FILE_KEY, "is nested too deeply to read") from exc

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
    template = "{" in subspace or "}" in subspace
    if template:
        rest = subspace.replace(SERVES_PLACEHOLDER, "", 1)
        if subspace.count(SERVES_PLACEHOLDER) != 1 or "{" in rest or "}" in rest or _SUBSPACE_RE.fullmatch(
            subspace.replace(SERVES_PLACEHOLDER, "x")
        ) is None:
            raise r.fail(
                "bad_subspace", "trigger.subspace",
                f"{subspace!r} uses a brace other than one {SERVES_PLACEHOLDER} inside the topic of a board/<topic> "
                "or board/ci/<topic>",
            )
    elif _SUBSPACE_RE.fullmatch(subspace) is None:
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
        if _FIELD_RE.fullmatch(when_field) is None:
            raise r.fail("bad_field", "when.field", f"{when_field!r} is not a field name {_FIELD}")

    clear_match: tuple[tuple[str, str], ...] | None = None
    if "clear" in top:
        clear = r.table(top["clear"], "clear", _CLEAR_KEYS)
        clear_match = r.match(r.required(clear, "match", "clear."), "clear.match")
        if dict(clear_match) == dict(match):
            raise r.fail("clear_equals_trigger", "clear.match", "equals trigger.match, so a post would both fire and clear")

    shown = threads
    if template:
        shown = _check_template_threads(r, threads)
    elif BASE_PLACEHOLDER in threads or BRANCH_PLACEHOLDER in threads:
        raise r.fail(
            "bad_threads", "display.threads",
            f"{BASE_PLACEHOLDER} and {BRANCH_PLACEHOLDER} belong to a template ({SERVES_PLACEHOLDER} in the trigger subspace)",
        )
    if (
        not shown.strip() or not shown.isprintable() or not shown.isascii()
        or shown != shown.strip() or len(shown) > MAX_THREADS_CHARS
    ):
        raise r.fail(
            "bad_threads", "display.threads",
            f"must be one line of printable ASCII, at most {MAX_THREADS_CHARS} characters, that is not blank "
            "and has no leading or trailing space",
        )
    if not summary.strip() or not summary.isprintable():
        raise r.fail("bad_summary", "summary", "must be one printable line that is not blank")
    channel = display.get("channel", False)
    if isinstance(channel, str):
        if _FIELD_RE.fullmatch(channel) is None:
            raise r.fail("bad_field", "display.channel", f"{channel!r} is not a result field name {_FIELD}")
    elif not isinstance(channel, bool):
        raise r.fail(
            "wrong_type", "display.channel",
            f"must be a boolean or the name of a result field, got {type(channel).__name__}",
        )

    load_group: str | None = None
    if "load" in top:
        load = r.table(top["load"], "load", _LOAD_KEYS)
        load_group = r.string(r.required(load, "group", "load."), "load.group")
        if _GROUP_RE.fullmatch(load_group) is None:
            raise r.fail(
                "bad_group", "load.group",
                f"{load_group!r} is not a group name: a letter or digit, then letters, digits, '.', '_' or '-', at most 64",
            )

    if template and load_group is not None:
        raise r.fail(
            "bad_group", "load.group",
            f"a {SERVES_PLACEHOLDER} template takes the serves of every group the session belongs to, so it names no group",
        )
    if not template:
        for arg, src in args:
            if src == ArgSource("self", "branch"):
                raise r.fail("bad_source", f"args.{arg}", f"{SELF_BRANCH} is the branch of a base, so only a {SERVES_PLACEHOLDER} template has one")

    limits = r.limits(r.table(top["limits"], "limits", _LIMIT_KEYS)) if "limits" in top else Limits()
    return Aura(
        name=aura, summary=summary, procedure=procedure, subspace=subspace, match=match, args=tuple(args),
        when_field=when_field, clear_match=clear_match, threads=threads, channel=channel, limits=limits,
        load_group=load_group,
    )


def _check_template_threads(r: _Reader, threads: str) -> str:
    """A template's ``display.threads`` as the file may write it: ``{base}`` once or more (so no two expansions share a
    block) and ``{branch}``, no other brace. Returns the text with each placeholder replaced by a stand-in, for the
    value rules the expansions must meet."""
    rest = threads.replace(BASE_PLACEHOLDER, "").replace(BRANCH_PLACEHOLDER, "")
    if "{" in rest or "}" in rest:
        raise r.fail("bad_threads", "display.threads", f"a template's threads value takes only {BASE_PLACEHOLDER} and {BRANCH_PLACEHOLDER} in braces")
    if BASE_PLACEHOLDER not in threads:
        raise r.fail(
            "bad_threads", "display.threads",
            f"a template's threads value must contain {BASE_PLACEHOLDER}, so the expansions for two bases never write one block",
        )
    return threads.replace(BASE_PLACEHOLDER, "x").replace(BRANCH_PLACEHOLDER, "x")


#: What the procedure's own ``branch`` argument accepts (``ci_red_notice.lua``, ``ci_red_nudge.lua``): a letter or digit,
#: then letters, digits, dot, dash and underscore, at most :data:`MAX_BRANCH_CHARS`. A base whose branch fails it is
#: refused at expansion, so the procedure never strikes out on it at the first red.
_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
MAX_BRANCH_CHARS: int = 32


def expand_template(
    aura: Aura, serves: Iterable[str], *, group: str | None = None,
) -> tuple[list[Aura], list[tuple[str, str]]]:
    """The expansions of the template *aura* for the bases in *serves*, in order, and the bases left out with why.
    *group* is the group that serves them; each expansion carries it as its ``load_group``.

    Each expansion is named ``<aura name>@<base>``, triggers on the subspace with its base in place of
    :data:`SERVES_PLACEHOLDER`, shows in the threads block the template's value gives with ``{base}`` and ``{branch}``
    filled, and passes the base's branch (:func:`nexus.ci_base.branch_of`) as a literal wherever the template mapped
    ``self.branch``. A base twice is expanded once. A base whose subspace is no board topic, or whose threads value breaks the
    threads rules (80 printable ASCII characters), is left out and named in the second list. An aura that is not a
    template expands to itself.
    """
    if not aura.is_template:
        return [aura], []
    out: list[Aura] = []
    rejected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for base in serves:
        if base in seen:
            continue
        seen.add(base)
        subspace = aura.subspace.replace(SERVES_PLACEHOLDER, base)
        if _SUBSPACE_RE.fullmatch(subspace) is None:
            rejected.append((base, f"{subspace!r} is not one concrete board/<topic> or board/ci/<topic>"))
            continue
        branch = branch_of(base)
        if any(src == ArgSource("self", "branch") for _, src in aura.args) and (
            _BRANCH_RE.fullmatch(branch) is None or len(branch) > MAX_BRANCH_CHARS
        ):
            rejected.append((base, f"its branch {branch!r} is not a branch of at most {MAX_BRANCH_CHARS} letters, digits, dot, dash and underscore"))
            continue
        threads = aura.threads.replace(BASE_PLACEHOLDER, base).replace(BRANCH_PLACEHOLDER, branch)
        if not threads.isprintable() or not threads.isascii() or len(threads) > MAX_THREADS_CHARS:
            rejected.append((base, f"the threads value {threads!r} is over {MAX_THREADS_CHARS} characters or not printable ASCII"))
            continue
        args = tuple(
            (name, ArgSource("literal", branch) if src == ArgSource("self", "branch") else src) for name, src in aura.args
        )
        out.append(replace(
            aura, name=f"{aura.name}{EXPANSION_SEP}{base}", subspace=subspace, threads=threads, args=args,
            base=base, expanded_from=aura.name, load_group=group,
        ))
    return out, rejected
