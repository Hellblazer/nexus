# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The quoted form of a collection label in a corpus or scope list (nexus-o5vbo).

A reader of a corpus or scope list gives four label shapes a meaning of their own, so a label of that shape passes
the RDR-229 label rule, registers, and cannot then be named there:

* ``all`` is the fan-out wildcard (the MCP ``corpus`` argument; a scope tag drops it);
* a trailing ``*``, ``-*`` or ``-<8 hex>`` is stripped from a scope tag (a glob or a hash suffix);
* a leading ``$`` is a plan binding.

A part wrapped in one pair of double quotes names the label inside exactly: ``"all"`` is the label ``all``, never the
wildcard; ``"foo*"`` is the label ``foo*``, never a glob. The quotes are removed once, so a label that itself starts and
ends with ``"`` is named by wrapping it again (``""x""`` names ``"x"``). A part with no surrounding quote pair reads as
it always did. ``"`` is legal in a label, and the label rule is untouched (a rule clause needs Sam's ruling); this is the
readers' side only.

Pure string functions with no imports from the rest of the package, so ``nexus.plans.scope`` and ``nexus.mcp.core`` can
both use it without a cycle.
"""
from __future__ import annotations

import re

__all__ = ["HASH_SUFFIX_RE", "WILDCARD_WORDS", "is_quoted", "label_needs_quote", "quote_corpus_label", "unquote_label"]

_QUOTE = '"'
HASH_SUFFIX_RE: re.Pattern[str] = re.compile(r"-[0-9a-fA-F]{8}$")
#: The words a reader treats as a wildcard (compared lower-case: the plan matcher folds case).
WILDCARD_WORDS: frozenset[str] = frozenset({"all"})


def is_quoted(part: str) -> bool:
    """True when *part* is wrapped in one pair of double quotes (so a lone ``"`` is not)."""
    return len(part) >= 2 and part[0] == _QUOTE and part[-1] == _QUOTE


def unquote_label(part: str) -> tuple[str, bool]:
    """``(label, True)`` for a quoted part, with the one outer pair removed; ``(part, False)`` for any other."""
    if is_quoted(part):
        return part[1:-1], True
    return part, False


def label_needs_quote(label: str) -> bool:
    """True when a reader would misread *label* unquoted: a wildcard word, a trailing ``*`` or 8-hex suffix, a leading
    ``$``, or a label that starts and ends with a quote (read as a quoted part)."""
    return (
        label.lower() in WILDCARD_WORDS
        or label.endswith("*")
        or bool(HASH_SUFFIX_RE.search(label))
        or label.startswith("$")
        or is_quoted(label)
    )


def quote_corpus_label(label: str) -> str:
    """*label* as one part of an MCP corpus list: the wildcard word and a quote-wrapped label are wrapped in quotes,
    because the corpus reader would otherwise fan out or strip them; every other label is returned bare, since that
    reader reads a glob, a hash suffix or a leading ``$`` as part of the name (the scope-tag reader is the one that
    strips them, and it takes :func:`label_needs_quote`)."""
    return f"{_QUOTE}{label}{_QUOTE}" if label in WILDCARD_WORDS or is_quoted(label) else label
