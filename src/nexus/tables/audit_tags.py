# SPDX-License-Identifier: AGPL-3.0-or-later
"""The AUDIT_TAGS table, read from ``audit-tags.toml`` (RDR-238 Phase 1 Step 1, nexus-qo7ie.6).

The tags (and the title patterns, the K4 backstop for untagged older rows) that mark a T2 row as
an audit record. The engine refuses a ``replaces`` put whose target carries a tag or matches a
title pattern (the Java mirror ``AuditTags`` is generated from this table by
``scripts/gen_audit_tags_java.py``), and ``nx memory curate --t2`` reads the same set.

Normalisation, stated once in the TOML header and applied the same way in Java: split the tags
string on ``,``, trim each element (characters at or below U+0020, as Java's ``String.trim``),
drop empty ones, and compare each for exact, case-sensitive
equality with a table tag. A whole tag, never a substring.

Title patterns: each entry is one regex, searched anywhere in the title, case-insensitively and
ASCII-only (``re.I | re.A``, matching Java's ``CASE_INSENSITIVE``). No entry puts a quantifier
after a group (Java's engine recurses per character on that).
"""
from __future__ import annotations

import importlib.resources
import re
import tomllib
from functools import lru_cache
from typing import Final

from nexus.tables.load import Table, load_packaged_table

TABLE_RESOURCE: Final = "audit-tags.toml"


@lru_cache(maxsize=1)
def load_table() -> Table:
    return load_packaged_table(TABLE_RESOURCE)


def header_text() -> str:
    """The table file's leading comment block (where the normalisation rule is stated)."""
    text = importlib.resources.files("nexus.tables").joinpath(TABLE_RESOURCE).read_text(encoding="utf-8")
    lines: list[str] = []
    for line in text.splitlines():
        if not line.startswith("#"):
            break
        lines.append(line)
    return "\n".join(lines)


def _tags() -> frozenset[str]:
    out: list[str] = []
    for row in load_table().rows:
        out.append(str(row.match["tag"]))
    return frozenset(out)


#: The audit tags, one per table row.
AUDIT_TAGS: Final[frozenset[str]] = _tags()


#: What the trim removes: every character at or below U+0020, exactly Java's ``String.trim()``.
_TRIM: Final[str] = "".join(chr(c) for c in range(0x21))


def _raw() -> dict:
    data = importlib.resources.files("nexus.tables").joinpath(TABLE_RESOURCE).read_bytes()
    return tomllib.loads(data.decode("utf-8"))


#: The title patterns, in file order (the backstop for audit rows that carry no tag).
TITLE_PATTERNS: Final[tuple[str, ...]] = tuple(_raw()["audit"]["title_patterns"])

_COMPILED: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (p, re.compile(p, re.IGNORECASE | re.ASCII)) for p in TITLE_PATTERNS
)


def title_pattern_of(title: str | None) -> str | None:
    """The first title pattern (source text) that matches *title*, or None."""
    if not title:
        return None
    for source, compiled in _COMPILED:
        if compiled.search(title):
            return source
    return None


def is_audit_title(title: str | None) -> bool:
    return title_pattern_of(title) is not None


def split_tags(raw: str | None) -> tuple[str, ...]:
    """The whole tags of a tags string: split on ',', trim, drop empties. Order kept."""
    if not raw:
        return ()
    return tuple(t for t in (part.strip(_TRIM) for part in raw.split(",")) if t)


def audit_tags_of(raw: str | None) -> tuple[str, ...]:
    """The audit tags a tags string carries, in the order they appear."""
    return tuple(t for t in split_tags(raw) if t in AUDIT_TAGS)


def is_audit_tagged(raw: str | None) -> bool:
    return bool(audit_tags_of(raw))
