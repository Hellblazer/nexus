# SPDX-License-Identifier: AGPL-3.0-or-later
"""Known-shape names exercising the three name-resolution surfaces.

RDR-087 Phase 3.1 (nexus-yi4b.3.1). Foundation for Probe 3a of
``nx doctor --check=search``: a curated list of name shapes whose
resolution was either a historical bug (nexus-51j, nexus-7ay,
nexus-rc45) or an edge case worth pinning (long names, dot-bearing
bead-id shapes). Probe 3a walks this list, dispatches each entry to
its ``expected_surface``, and fails loud when a routing result
regresses.

The fixture itself is pure data — no live calls — so it's safe to
import in any test context without Voyage / ChromaDB / filesystem.
Surface bindings live in the probe, not here.

Surface literals:

- ``resolve_corpus`` — ``src/nexus/corpus.py::resolve_corpus``. Maps
  a corpus prefix or exact collection name to the matching T3
  collections.
- ``rdr_resolve``   — ``src/nexus/doc/resolvers.py::RdrResolver.resolve``.
  Reads RDR frontmatter; case-insensitive per the nexus-51j fix
  (PR #173).
- ``resolve_span`` — ``src/nexus/catalog/catalog.py::Catalog.resolve_span``.
  Resolves ``chash:<sha>[:<start>-<end>]`` spans to chunk content.

Adding an entry: pick the surfaces the name *should* reach and list
the bug class in ``shape_note`` so a future reader can trace the
canary back to its incident or edge-case rationale.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NameCanary:
    name: str
    expected_surface: frozenset[str]
    shape_note: str


def _canary(
    name: str, surfaces: tuple[str, ...], shape_note: str,
) -> NameCanary:
    return NameCanary(
        name=name,
        expected_surface=frozenset(surfaces),
        shape_note=shape_note,
    )


NAME_CANARIES: list[NameCanary] = [
    # ── Multi-hyphen corpus (nexus-rc45) ──────────────────────────────────
    _canary(
        "art-grossberg-papers",
        ("resolve_corpus",),
        "nexus-rc45: multi-hyphen corpus name; silent threshold-drop on "
        "scholarly PDF collection when resolver split on first hyphen.",
    ),
    _canary(
        "mit-cbcl-working-papers",
        ("resolve_corpus",),
        "Three-hyphen variant — pins resolve_corpus behaviour beyond the "
        "two-hyphen nexus-rc45 case.",
    ),

    # ── Mixed-case RDR identifier (nexus-51j) ─────────────────────────────
    _canary(
        "73",
        ("rdr_resolve",),
        "nexus-51j: bare numeric id; RdrResolver must zero-pad and match "
        "both 'rdr-073-*.md' and 'RDR-073-*.md'.",
    ),
    _canary(
        "RDR-073",
        ("rdr_resolve",),
        "nexus-51j: fully-qualified uppercase form; case-sensitive glob "
        "(pre-PR #173) missed this on Linux/macOS fs.",
    ),
    _canary(
        "rdr-73",
        ("rdr_resolve",),
        "nexus-51j: unpadded lowercase form; canary crosses zero-pad + "
        "case-fold together.",
    ),

    # ── Hash-suffixed (projection / catalog shapes) ───────────────────────
    _canary(
        "ART-8c2e74c0",
        ("resolve_corpus",),
        "Hash-suffixed corpus alias; verifies prefix normalisation "
        "tolerates a short-hash tail (8-hex).",
    ),

    # ── Dot-bearing (bead-id-like shape) ──────────────────────────────────
    _canary(
        "nexus-qo0.1",
        ("resolve_corpus",),
        "Bead-id-like shape with a dotted child; '.' is a token "
        "boundary in several resolvers and has broken split rules before.",
    ),

    # ── Long-name edge case ───────────────────────────────────────────────
    _canary(
        "a-very-long-collection-name-exceeding-sixty-four-characters-for-truncation-edge",
        ("resolve_corpus",),
        ">64-char canary: catches resolver truncation bugs and any "
        "ChromaDB name-length ceiling regressions.",
    ),

    # ── Prefix broadcast (nexus-7ay class) ────────────────────────────────
    _canary(
        "docs",
        ("resolve_corpus",),
        "nexus-7ay class: bare meta-name broadcasts to every docs__* "
        "collection; validate-refs proximity false-positives fire here.",
    ),
    _canary(
        "code",
        ("resolve_corpus",),
        "Sibling of the 'docs' broadcast shape; code__* prefix family.",
    ),

    # ── Span resolution shape ─────────────────────────────────────────────
    _canary(
        "chash:0000000000000000000000000000000000000000000000000000000000000000",
        ("resolve_span",),
        "Syntactically valid chash span with a zeroed digest — asserts the "
        "resolver accepts full-64-hex shape without raising, even though "
        "the digest has no live chunk (RDR-086).",
    ),
]
