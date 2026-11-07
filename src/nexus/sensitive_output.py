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

The pieces are small and pure so the MCP read tools, the CLI (nexus-rfvyy.8.3) and the catalog surfaces
(nexus-rfvyy.1) render a sensitive hit one way. Imports only the standard library at load; the config read is
deferred.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Final, Literal

__all__ = [
    "BADGE", "McpOutput", "first_sensitive_owner", "hit_is_sensitive", "mcp_output_posture", "stub_line",
    "tumbler_order_key", "withheld_note",
]

McpOutput = Literal["withhold", "flag"]

#: The marker a shown-with-content sensitive hit carries under ``flag``, and the head of the stub line.
BADGE: Final = "[sensitive]"

#: The config section and key (``nx config set sensitivity.mcp_output flag``).
CONFIG_SECTION: Final = "sensitivity"
CONFIG_KEY: Final = "mcp_output"


def mcp_output_posture() -> McpOutput:
    """``flag`` only when the global config says exactly that (case and surrounding space ignored); else ``withhold``.

    Read per call: the global config parse is cached on the file's mtime and size, so an edit takes effect at once
    and a read costs a ``stat``. A config that cannot be read is ``withhold``."""
    try:
        from nexus.config import get_global_config_section  # noqa: PLC0415 - deferred: config pulls yaml and the tuning tables

        value = get_global_config_section(CONFIG_SECTION).get(CONFIG_KEY)
    except Exception:  # noqa: BLE001 - a broken config.yml must fail closed to withhold, never open
        return "withhold"
    if isinstance(value, str) and value.strip().lower() == "flag":
        return "flag"
    return "withhold"


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
    return hit_is_sensitive(row.get("sensitive"), owners), owners


def stub_line(tumbler: str = "") -> str:
    """The one line that stands in for a withheld hit: ``[sensitive] <tumbler>: content withheld``.

    With no tumbler it is ``[sensitive]: content withheld``."""
    return f"{BADGE} {tumbler}: content withheld" if tumbler else f"{BADGE}: content withheld"


def withheld_note(count: int) -> str:
    """The sentence that states how many hits a response withheld (``"1 sensitive hit withheld"``)."""
    return f"{count} sensitive hit{'' if count == 1 else 's'} withheld"
