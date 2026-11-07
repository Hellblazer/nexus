# SPDX-License-Identifier: AGPL-3.0-or-later
"""The output posture for sensitive hits (RDR-243 Phase 3 Technical Design item 9, Q1; nexus-rfvyy.8.2).

What a surface prints to a reader that is, or feeds, the session model. A hit whose engine ``sensitive`` field is
true is, by default, replaced by one line and counted::

    [sensitive] <tumbler>: content withheld
    1 sensitive hit withheld

``<tumbler>`` is the hit's first sensitive owner in tumbler order (numeric, so ``1.9.4`` precedes ``1.10.1``), or
left out when none resolves. The line has no title, path or snippet. The config key ``sensitivity.mcp_output:
flag`` shows the content with a ``[sensitive]`` badge instead (and the reader clause,
:data:`nexus.context_annotations.SENSITIVE_READER_CLAUSE`, joins the reader instruction).

The key is read from the GLOBAL ``config.yml`` only, never the per-repo ``.nexus.yml``: a cloned repository must not
be able to switch withholding off. Every value but ``flag`` (absent, misspelled, a config that does not parse) is
``withhold``: the posture fails closed.

A catalog DOCUMENT row (nexus-rfvyy.1) is the same class of content: its title, path, URI, author and
bibliographic fields are what the document says about itself. :func:`present_document_rows` renders a page of them
the same way (the row keeps its tumbler and a few non-content columns and carries ``withheld``).

The pieces are small and pure so the MCP read tools, the CLI (nexus-rfvyy.8.3) and the catalog surfaces
(nexus-rfvyy.1) render a sensitive hit one way. Imports only the standard library at load; the config read is
deferred.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

__all__ = [
    "BADGE", "DocumentRows", "McpOutput", "McpOutputSetting", "chunk_row_facts", "cli_output_posture", "first_sensitive_owner", "hidden_endpoints", "hit_is_sensitive",
    "mcp_output_posture", "mcp_output_setting", "posture_of", "present_document_rows", "present_entries", "stub_line", "tumbler_order_key", "withheld_edges", "withheld_note",
]

McpOutput = Literal["withhold", "flag"]

#: The marker a shown-with-content sensitive hit carries under ``flag``, and the head of the stub line.
BADGE: Final = "[sensitive]"

#: The config section and key (``nx config set sensitivity.mcp_output flag``).
CONFIG_SECTION: Final = "sensitivity"
CONFIG_KEY: Final = "mcp_output"


def posture_of(value: object) -> McpOutput:
    """The posture one configured *value* means: ``flag`` only for the string ``flag`` (case and surrounding space
    ignored); anything else, an absent key and an unknown value included, is ``withhold``."""
    if isinstance(value, str) and value.strip().lower() == "flag":
        return "flag"
    return "withhold"


@dataclass(frozen=True)
class McpOutputSetting:
    """What the global config says about ``sensitivity.mcp_output`` and the posture that follows from it.

    *raw* is the configured value (``None`` when the key is absent); *error* is the reason the config could not
    be read (then *posture* is ``withhold`` and *raw* is ``None``)."""

    posture: McpOutput
    raw: object = None
    error: str = ""


def mcp_output_setting() -> McpOutputSetting:
    """Read ``sensitivity.mcp_output`` from the GLOBAL config only (a cloned repo's ``.nexus.yml`` must not open
    the gate) and return it with the posture it means. The one reader: the tools and ``nx doctor`` both go through
    it, so they cannot disagree."""
    try:
        from nexus.config import get_global_config_section  # noqa: PLC0415 - deferred: config pulls yaml and the tuning tables

        raw = get_global_config_section(CONFIG_SECTION).get(CONFIG_KEY)
    except Exception as exc:  # noqa: BLE001 - a broken config.yml must fail closed to withhold, never open
        return McpOutputSetting("withhold", None, f"{type(exc).__name__}: {exc}")
    return McpOutputSetting(posture_of(raw), raw)


def mcp_output_posture() -> McpOutput:
    """``flag`` only when the global config says exactly that (case and surrounding space ignored); else ``withhold``.

    Read per call: the global config parse is cached on the file's mtime and size, so an edit takes effect at once
    and a read costs a ``stat``. A config that cannot be read is ``withhold``."""
    return mcp_output_setting().posture


def tumbler_order_key(tumbler: str) -> tuple[tuple[int, int, str], ...]:
    """Sort key for dotted tumblers: numeric segments by value, so ``1.2.7 < 1.9.4 < 1.10.1``.

    A non-numeric segment sorts after every numeric one, by text, so a malformed value still orders."""
    return tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in str(tumbler).split(".")
    )


def first_sensitive_owner(owners: Iterable[str]) -> str:
    """The lowest of *owners* in tumbler order, or ``""`` when there is none."""
    clean = sorted({str(o) for o in owners if o}, key=tumbler_order_key)
    return clean[0] if clean else ""


def hit_is_sensitive(flag: object, owners: Sequence[str] | None = None) -> bool:
    """A hit is sensitive when the engine's ``sensitive`` field is exactly ``True`` OR a sensitive owner is named.

    The second leg is the fail-closed one: an engine that predates the field sends none, and the catalog's
    ``sensitive_owners`` still says so. ``False`` from the engine with no owner named is the clean case."""
    return flag is True or bool(owners)


def hit_facts(row: Mapping[str, object]) -> tuple[bool, tuple[str, ...]]:
    """``(sensitive, owners)`` from a hit's mapping (a result's metadata, or a raw engine row)."""
    raw = row.get("sensitive_owners")
    owners = tuple(str(o) for o in raw if o) if isinstance(raw, (list, tuple)) else ()
    # An engine that sends no ``sensitive`` flag, and an owner lookup that failed: nothing says the hit is clean, so
    # it is sensitive (fail closed; nexus.search_engine.OWNER_LOOKUP_FAILED_KEY).
    unknown = row.get("_owner_lookup_failed") is True and not isinstance(row.get("sensitive"), bool)
    return hit_is_sensitive(row.get("sensitive"), owners) or unknown, owners


def chunk_row_facts(row_flag: object, owners: Sequence[object] | None) -> tuple[bool, tuple[str, ...]]:
    """``(sensitive, sensitive owner tumblers)`` of one T3 chunk row a read tool is about to print (nexus-rfvyy.8).

    *row_flag* is the engine's ``sensitive`` field for the row; *owners* are the catalog's LIVE owner entries of the
    chunk. ``None`` for *owners* means the catalog could not be read: "could not look" is not "no owner", so the row
    is sensitive (the document view treats an unreadable catalog the same way)."""
    if owners is None:
        return True, ()
    flagged = tuple(str(getattr(o, "tumbler", "")) for o in owners if getattr(o, "sensitive", False) is True)
    return hit_is_sensitive(row_flag, flagged), flagged


def stub_line(tumbler: str = "") -> str:
    """The one line that stands in for a withheld hit: ``[sensitive] <tumbler>: content withheld``.

    With no tumbler it is ``[sensitive]: content withheld``."""
    return f"{BADGE} {tumbler}: content withheld" if tumbler else f"{BADGE}: content withheld"


def withheld_note(count: int) -> str:
    """The sentence that states how many hits a response withheld (``"1 sensitive hit withheld"``)."""
    return f"{count} sensitive hit{'' if count == 1 else 's'} withheld"


# ── catalog document rows (nexus-rfvyy.1) ────────────────────────────────────

#: The columns of a catalog document row that survive withholding: its address and the facts that say what kind of
#: row it is and how big, none of which is text the document wrote. ``title``, ``author``, ``year``, ``file_path``,
#: ``source_uri``, ``corpus``, ``meta``, ``bib_*``, ``head_hash`` and ``alias_of`` do not.
DOCUMENT_KEEP_KEYS: Final = ("tumbler", "content_type", "physical_collection", "chunk_count", "indexed_at")

#: The key a response carries for the withheld count, on a list page (one entry) or a dict result.
WITHHELD_KEY: Final = "_withheld"
#: The key that carries the reader clause when a sensitive row is shown with its content (``flag``).
NOTICE_KEY: Final = "_sensitive_notice"


@dataclass(frozen=True)
class DocumentRows:
    """A page of catalog document rows as the reader is to see them.

    ``rows`` keeps the page's length and order (a stub takes its row's slot, so ``next_offset`` is unchanged);
    ``withheld`` is how many rows became stubs and ``hidden`` their tumblers (what an edge touching one must not
    describe); ``shown_sensitive`` is true when a sensitive row is shown with its content (``flag``)."""

    rows: list[dict]
    withheld: int = 0
    hidden: frozenset[str] = frozenset()
    shown_sensitive: bool = False

    def trailer(self) -> list[dict]:
        """The entries a list result appends after its rows (before any ``_pagination``): the withheld count
        and, under ``flag``, the reader clause. Empty for a page with no sensitive row."""
        out: list[dict] = []
        if self.withheld:
            out.append({WITHHELD_KEY: {"count": self.withheld, "note": withheld_note(self.withheld)}})
        if self.shown_sensitive:
            out.append({NOTICE_KEY: _reader_clause()})
        return out

    def keys(self) -> dict[str, Any]:
        """The same facts as keys of a dict result (``show``, ``links``)."""
        out: dict[str, Any] = {}
        for entry in self.trailer():
            out.update(entry)
        return out


def _reader_clause() -> str:
    from nexus.context_annotations import SENSITIVE_READER_CLAUSE  # noqa: PLC0415 - deferred, keeps this module's load light

    return SENSITIVE_READER_CLAUSE


def stub_document_row(row: Mapping[str, object]) -> dict:
    """The stand-in for a withheld catalog document row: the kept columns, ``sensitive: true`` and ``withheld``
    (the stub line, which names the tumbler and no title)."""
    stub: dict = {k: row[k] for k in DOCUMENT_KEEP_KEYS if k in row}
    stub["sensitive"] = True
    stub["withheld"] = stub_line(str(row.get("tumbler", "")))
    return stub


def cli_output_posture() -> McpOutput:
    """The posture of CLI output (RDR-243 Q2, decided): ``withhold`` when the process was started by Claude Code
    (what its Bash tool reads reaches the session model), ``flag`` (content with a badge) in a terminal. The config
    key ``sensitivity.mcp_output`` is the MCP tools' and does not move this."""
    from nexus.session import started_by_claude_code  # noqa: PLC0415 - deferred, keeps this module's load light

    return "withhold" if started_by_claude_code() else "flag"


def present_document_rows(
    rows: Sequence[Mapping[str, object]], *, extra_sensitive: Iterable[str] = (), posture: McpOutput | None = None,
) -> DocumentRows:
    """Apply the output posture to a page of catalog document rows (``CatalogEntry.to_dict()`` shape).

    A row is sensitive when its ``sensitive`` field is exactly ``True`` or its tumbler is in *extra_sensitive*
    (the caller's fail-closed legs: an alias of a flagged canonical, a flag lookup that failed). The config is
    read only when the page holds a sensitive row, so a clean page is returned as it came. Under ``withhold`` each
    such row becomes :func:`stub_document_row`; under ``flag`` it is returned whole with ``sensitive: true`` and a
    ``badge``, and the result says the reader clause applies. *posture* names the posture; ``None`` reads the MCP
    tools' (``sensitivity.mcp_output``), and the CLI passes :func:`cli_output_posture`."""
    extra = {str(t) for t in extra_sensitive}
    flags = [r.get("sensitive") is True or str(r.get("tumbler", "")) in extra for r in rows]
    if not any(flags):
        return DocumentRows([dict(r) for r in rows])
    if (posture or mcp_output_posture()) == "flag":
        shown = [{**r, "sensitive": True, "badge": BADGE} if f else dict(r) for r, f in zip(rows, flags)]
        return DocumentRows(shown, shown_sensitive=True)
    shown = [stub_document_row(r) if f else dict(r) for r, f in zip(rows, flags)]
    hidden = frozenset(str(r.get("tumbler", "")) for r, f in zip(rows, flags) if f)
    return DocumentRows(shown, withheld=sum(flags), hidden=hidden)


def present_entries(cat: Any, entries: Sequence[Any], *, posture: McpOutput | None = None) -> DocumentRows:
    """*entries* (``CatalogEntry`` objects) as the reader is to see them under *posture* (``None``: the MCP tools').

    A sensitive document's title, path, URI, author and bibliographic fields are content, so a withheld row is the
    kept columns plus ``withheld``, in the same slot. The flag is the row's own ``sensitive`` column. A merged
    duplicate (``alias_of``) carries its own, unset, flag while the canonical holds the content, so an alias is
    asked about once, following the alias; when that read fails the alias is withheld (fail closed)."""
    aliases = [
        str(e.tumbler) for e in entries
        if getattr(e, "alias_of", "") and getattr(e, "sensitive", False) is not True
    ]
    extra: set[str] = set()
    if aliases:
        from nexus import doc_sensitivity  # noqa: PLC0415 - deferred: the module reaches the catalog factory

        try:
            extra = doc_sensitivity.flagged(aliases, cat=cat)
        except doc_sensitivity.SensitivityUnknown:
            extra = set(aliases)
    return present_document_rows([_entry_row(e) for e in entries], extra_sensitive=extra, posture=posture)


def _entry_row(entry: Any) -> dict[str, Any]:
    """A catalog entry as a row: its ``to_dict()`` when it has one, else its attributes (the presenter takes any
    entry-shaped object, as the CLI verbs did before they routed through it)."""
    to_dict = getattr(entry, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    return dict(vars(entry))


def hidden_endpoints(
    cat: Any, hidden: Iterable[str], tumblers: Iterable[str], *, posture: McpOutput | None = None,
) -> frozenset[str]:
    """The documents an edge must not describe: *hidden* (the withheld documents the caller already presented) and,
    under a withhold, every other endpoint in *tumblers* whose catalog row is sensitive. One rule for every
    surface that returns link edges (nexus-rfvyy.8 fix r1): an edge touching a withheld document loses its ``meta``
    (:func:`withheld_edges`), whichever side the withheld document is on. Only ``meta`` is withheld: the edge's
    spans stay (they are positions and chunk hashes, not text).

    A flag read that fails hides every endpoint (fail closed). Nothing is read under ``flag``. *posture* ``None``
    reads the MCP tools' (``sensitivity.mcp_output``)."""
    out = set(hidden)
    ends = {str(t) for t in tumblers if t}
    if ends and (posture or mcp_output_posture()) == "withhold":
        from nexus import doc_sensitivity  # noqa: PLC0415 - deferred: the module reaches the catalog factory

        try:
            out |= doc_sensitivity.flagged(ends, cat=cat)
        except doc_sensitivity.SensitivityUnknown:
            out |= ends
    return frozenset(out)


def withheld_edges(edges: Iterable[Mapping[str, object]], hidden: frozenset[str]) -> list[dict]:
    """*edges* (link dicts) with ``meta`` emptied on every edge that touches a withheld document.

    The edge, both tumblers, its type and its spans stay: that is graph structure. ``meta`` is free text a linker
    may have written about the document, so it goes with the title."""
    return [
        {**e, "meta": {}} if hidden and (str(e.get("from")) in hidden or str(e.get("to")) in hidden) else dict(e)
        for e in edges
    ]
