# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-082: Resolver protocol + built-in bead/RDR resolvers + registry.

The registry is the extension point RDR-083 plugs its
``AnchorResolver`` and ``ChashResolver`` into without touching parser,
engine, or CLI.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Protocol

__all__ = [
    "Resolver",
    "ResolutionError",
    "ResolverRegistry",
    "BeadResolver",
    "RdrResolver",
]


class ResolutionError(Exception):
    """Raised by a resolver when a key/field cannot be resolved."""


class Resolver(Protocol):
    """Minimal contract. Implementers read their own namespace's data
    source (bead DB, RDR frontmatter, ChromaDB, ...) and return a
    markdown-safe string. Unknown key → ``ResolutionError``."""

    def resolve(
        self, key: str, field: str | None, filters: dict[str, str],
    ) -> str:  # pragma: no cover - Protocol
        ...


class ResolverRegistry:
    """Maps namespace → Resolver. Add third-party resolvers via
    :meth:`register` — the render engine looks up ``namespace`` here
    at dispatch time so nothing in the parser or CLI needs to know
    about them.
    """

    def __init__(self, initial: dict[str, Resolver] | None = None) -> None:
        self._by_ns: dict[str, Resolver] = dict(initial or {})

    def register(self, namespace: str, resolver: Resolver) -> None:
        self._by_ns[namespace] = resolver

    def get(self, namespace: str) -> Resolver | None:
        return self._by_ns.get(namespace)

    def __contains__(self, namespace: str) -> bool:
        return namespace in self._by_ns


# ── BeadResolver ─────────────────────────────────────────────────────────────


_BEAD_DEFAULT_FIELD = "title"
_BEAD_ALLOWED_FIELDS = frozenset({
    "title", "status", "assignee", "closed_at", "epic_id", "progress", "id",
})


class BeadResolver:
    """``{{bd:<id>[.field]}}`` — ``bd show <id> --json`` lookup.

    A per-instance cache keyed on ``key`` coalesces multiple field
    reads of the same bead into one subprocess call — ``nx doc render``
    is expected to instantiate a fresh resolver per render, so the
    cache is bounded by the document's unique-bead count.
    """

    def __init__(self) -> None:
        self._cache: dict[str, dict[str, Any]] = {}

    def resolve(
        self, key: str, field: str | None, filters: dict[str, str],
    ) -> str:
        data = self._cache.get(key)
        if data is None:
            data = self._fetch(key)
            self._cache[key] = data
        sel = field or _BEAD_DEFAULT_FIELD
        if sel not in _BEAD_ALLOWED_FIELDS:
            raise ResolutionError(
                f"bead field {sel!r} not in allow-list {sorted(_BEAD_ALLOWED_FIELDS)}"
            )
        value = data.get(sel)
        if value is None:
            raise ResolutionError(
                f"bead {key!r} has no field {sel!r}"
            )
        return str(value)

    def _fetch(self, key: str) -> dict[str, Any]:
        try:
            proc = subprocess.run(
                ["bd", "show", key, "--json"],
                capture_output=True, text=True, timeout=10,
            )
        except FileNotFoundError as exc:
            raise ResolutionError(
                "`bd` CLI not on PATH — install beads to resolve {{bd:…}} tokens"
            ) from exc
        if proc.returncode != 0:
            raise ResolutionError(
                f"bd show {key!r} exited {proc.returncode}: "
                f"{proc.stderr.strip()[:200]}"
            )
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise ResolutionError(
                f"bd show {key!r} returned non-JSON: {proc.stdout[:200]!r}"
            ) from exc


# ── RdrResolver ──────────────────────────────────────────────────────────────


_RDR_DEFAULT_FIELD = "title"
_RDR_ALLOWED_FIELDS = frozenset({
    "title", "status", "type", "priority", "author", "gated", "closed",
    "close_reason", "epic_bead",
})

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


class RdrResolver:
    """``{{rdr:<id>[.field]}}`` — reads ``docs/rdr/rdr-<id>-*.md``
    frontmatter. The file itself is authoritative (mirrored in T2 but
    the file is the source of truth per CLAUDE.md's RDR convention).
    """

    def __init__(self, rdr_dir: Path) -> None:
        self._rdr_dir = Path(rdr_dir)
        self._cache: dict[str, dict[str, Any]] = {}

    def resolve(
        self, key: str, field: str | None, filters: dict[str, str],
    ) -> str:
        data = self._cache.get(key)
        if data is None:
            data = self._fetch(key)
            self._cache[key] = data
        sel = field or _RDR_DEFAULT_FIELD
        if sel not in _RDR_ALLOWED_FIELDS:
            raise ResolutionError(
                f"rdr field {sel!r} not in allow-list {sorted(_RDR_ALLOWED_FIELDS)}"
            )
        value = data.get(sel)
        if value is None:
            raise ResolutionError(
                f"rdr-{key} frontmatter has no field {sel!r}"
            )
        return str(value)

    def _fetch(self, key: str) -> dict[str, Any]:
        # nexus-51j: case-insensitive match so projects that use the
        # uppercase ``RDR-NNN-*.md`` convention (common, visually
        # distinguishes RDRs from other docs/) work alongside the
        # nexus-default lowercase ``rdr-NNN-*.md``. A numeric key
        # matches any zero-padding of the same integer; a non-numeric
        # key matches the literal text.
        if key.isdigit():
            pattern = re.compile(
                rf"^rdr-0*{int(key)}-.+\.md$", re.IGNORECASE,
            )
        else:
            pattern = re.compile(
                rf"^rdr-{re.escape(key)}-.+\.md$", re.IGNORECASE,
            )
        candidates = [
            p for p in self._rdr_dir.glob("*.md") if pattern.match(p.name)
        ]
        if not candidates:
            raise ResolutionError(
                f"no RDR file matching rdr-{key}-*.md (case-insensitive) "
                f"under {self._rdr_dir}"
            )
        # nexus-gcwq: disambiguate when the RDR directory carries sibling
        # child-artifact files (IMPL reports, post-mortems, supplements,
        # calibration artifacts) sharing the same ``RDR-NNN-`` prefix.
        # The raw glob returns all matches; ``candidates[0]`` is
        # filesystem-order-dependent and typically picks the child file
        # because "IMPL" sorts before most lowercase titles on macOS.
        # Partition into primary (main RDR) vs child (marker-bearing)
        # files and prefer primary when any exist. Sort each partition
        # alphabetically so the return is deterministic across file-
        # systems — the prior contract leaked glob order into the result.
        selected = _select_primary_rdr(candidates)
        text = selected.read_text(errors="replace")
        m = _FRONTMATTER_RE.search(text)
        if not m:
            raise ResolutionError(
                f"rdr-{key} has no YAML frontmatter"
            )
        return _parse_frontmatter(m.group(1))


# Tokens that denote a child-artifact RDR file rather than the main RDR.
# Matched as a lowercase kebab-case segment: ``-impl-``, ``-post-mortem-``,
# ``-supplement-``, ``-calibration-``. The separators constrain the match
# so genuine title words happening to contain these letters (unlikely,
# but possible for e.g. "implementation-details") don't get mis-classified.
#
# Review remediation (Reviewer C/I-5): each marker is a tuple of acceptable
# *ends*. ``-calibration-`` matches ``rdr-079-calibration-phase-1.md`` (a
# child) but NOT ``rdr-200-calibration-free-inference.md`` (a primary RDR
# whose title starts with "calibration-free"). The dual entry "<marker>."
# covers the extensionless child case (``rdr-079-calibration.md``).
_CHILD_MARKER_SUFFIXES: tuple[str, ...] = (
    "-impl-", "-impl.",
    "-post-mortem-", "-post-mortem.",
    "-supplement-", "-supplement.",
    "-calibration-", "-calibration.",
)

# Back-compat alias for external callers that imported the old tuple.
_CHILD_MARKERS: tuple[str, ...] = _CHILD_MARKER_SUFFIXES


def _select_primary_rdr(candidates: list["Path"]) -> "Path":
    """Return the primary (main) RDR file from a list of candidates.

    A "primary" file's stem has no child-artifact marker; a "child" has
    at least one. When both exist, primary wins. Within each class the
    alphabetically-first stem wins so the return value is filesystem-
    order-independent (globs on macOS HFS+ differ from Linux ext4).

    Invariant: ``candidates`` is non-empty. The caller enforces this.
    """
    def _is_child(path: "Path") -> bool:
        # Match against the *name* (stem + ext) so the trailing "."
        # variants like ``-calibration.`` anchor against the extension
        # boundary rather than the end of the stem.
        name_lower = path.name.lower()
        return any(marker in name_lower for marker in _CHILD_MARKER_SUFFIXES)

    primaries = sorted([c for c in candidates if not _is_child(c)])
    if primaries:
        return primaries[0]
    return sorted(candidates)[0]


def _parse_frontmatter(block: str) -> dict[str, Any]:
    """Minimal YAML subset — single-line ``key: value`` pairs only.
    Sufficient for the RDR frontmatter schema; falls back to PyYAML
    when available for anything more complex (lists, nested maps)."""
    try:
        import yaml
        data = yaml.safe_load(block) or {}
        return data if isinstance(data, dict) else {}
    except ImportError:
        pass

    out: dict[str, Any] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        key = k.strip()
        val = v.strip().strip('"').strip("'")
        if key:
            out[key] = val
    return out
