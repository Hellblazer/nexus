# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""Dry-run preview of the link generators (nexus-glivh).

Split out of ``link_generator`` (nexus-u5m5g.8, fix round 1) to keep that module under its size
ceiling; ``link_generator`` re-exports every public name here.
"""

from __future__ import annotations

from dataclasses import dataclass

from nexus.catalog.catalog_protocol import CatalogReader

# ── Dry-run preview (nexus-glivh) ────────────────────────────────────────────
#
# `nx catalog generate-links --dry-run` previously printed "dry-run mode not
# yet supported for link preview" and previewed nothing, so the only way to
# learn what a pass would do was to let it write to a live catalog. That is
# the wrong order for a generator whose failure mode is VOLUME: the
# implements-heuristic flood (nexus-ybj1b) had to be disabled at engine
# v0.1.57 after exactly that kind of unmeasured pass.

@dataclass(frozen=True)
class ProposedLink:
    """One link a dry run would have created."""
    from_tumbler: str
    to_tumbler: str
    link_type: str
    created_by: str


class DryRunLinkWriter:
    """A ``CatalogWriter``-shaped recorder that writes nothing.

    Composes with the generators UNCHANGED: they already take a ``writer=``,
    so a dry run is the real code path with the writes swapped out. A preview
    computed by a separate path is a preview of the wrong thing.

    ``link_if_absent`` returns True only when a link is genuinely new, so a
    count means "would create" and never "would attempt". Pre-existing links
    are supplied up front via :func:`load_existing_link_keys` rather than
    probed one at a time, which would be thousands of round trips.
    """

    def __init__(
        self, existing: set[tuple[str, str, str]] | None = None, *, resolver: object | None = None,
    ) -> None:
        # *resolver* is a catalog reader: with it, a path edge is resolved the way the engine will
        # (see ``links_upsert_many``); without it the edge is counted as it stands.
        self._resolver = resolver
        self._existing: set[tuple[str, str, str]] = existing or set()
        self._seen: set[tuple[str, str, str]] = set()
        self._proposed: list[ProposedLink] = []
        self._retracted: list[ProposedLink] = []
        self._path_edges = 0
        self._unresolved_paths = 0
        self._same_content_sent = 0

    def link_if_absent(
        self, from_t: object, to_t: object, link_type: str,
        created_by: str = "", **_meta: object,
    ) -> bool:
        key = (str(from_t), str(to_t), str(link_type))
        # Dedupe within the run as well as against the catalog: a generator
        # that proposes the same edge twice must not be counted twice, or the
        # projected volume is inflated by its own repetition.
        if key in self._existing or key in self._seen:
            return False
        self._seen.add(key)
        self._proposed.append(
            ProposedLink(str(from_t), str(to_t), str(link_type), str(created_by))
        )
        return True

    def links_upsert_many(self, edges: list[dict]) -> dict:
        """Record the edges a generator would send through ``links_upsert_many`` (RDR-193 Step 7).

        A tumbler edge is counted against the existing links like ``link_if_absent``'s.

        A path edge (``{owner, file_path}``) is the engine's to resolve. With a *resolver*, the
        preview does the same lookup the engine does for its file-path arm, in ONE batch read
        (``find_all_by_file_paths``): the edge targets the document at that path under the named
        owner, a path with no such document is counted in ``unresolved_paths`` and proposed
        nowhere, and the resolved edge is checked against the existing links like any other. The
        engine also resolves by the file's identity (its ``source_uri``), which can reach a
        document registered under another owner or under a different ``file_path`` string, so the
        preview can still fall a little short of the engine; it never counts a path that names
        nothing. Without a *resolver*, the edge is recorded with the path as its endpoint
        (``path:<owner>:<file_path>``), counted before the engine skips the paths it cannot
        resolve and the links that exist, an UPPER BOUND, and ``path_edges`` says how many of the
        proposals are such.
        """
        resolved = self._resolve_paths(edges)
        targets = resolved or {}

        def label(end: dict) -> str:
            if end.get("tumbler"):
                return str(end["tumbler"])
            hit = targets.get((str(end["owner"]), str(end["file_path"])))
            return hit if hit else f"path:{end['owner']}:{end['file_path']}"

        created = skipped = 0
        for edge in edges:
            if resolved is not None and not edge["to"].get("tumbler") and (
                str(edge["to"]["owner"]), str(edge["to"]["file_path"]),
            ) not in targets:
                self._unresolved_paths += 1
                continue
            from_label, to_label = label(edge["from"]), label(edge["to"])
            key = (from_label, to_label, str(edge["link_type"]))
            if key in self._existing or key in self._seen:
                skipped += 1
                continue
            self._seen.add(key)
            self._proposed.append(ProposedLink(from_label, to_label, key[2], str(edge.get("created_by", ""))))
            if to_label.startswith("path:") and not edge["to"].get("tumbler"):
                self._path_edges += 1
            created += 1
        return {"created": created, "skipped": skipped, "unresolved": []}

    def _resolve_paths(self, edges: list[dict]) -> dict[tuple[str, str], str] | None:
        """``(owner, file_path) -> tumbler`` for the path edges that name a document of the owner,
        or None when there is no resolver (every path edge then stays an unresolved upper bound)."""
        if self._resolver is None:
            return None
        wanted = {
            (str(e["to"]["owner"]), str(e["to"]["file_path"])) for e in edges if not e["to"].get("tumbler")
        }
        if not wanted:
            return {}
        found = self._resolver.find_all_by_file_paths(sorted({fp for _, fp in wanted}))  # type: ignore[attr-defined]
        out: dict[tuple[str, str], str] = {}
        for owner, file_path in wanted:
            own = sorted(str(e.tumbler) for e in found.get(file_path, []) if str(e.tumbler).startswith(owner + "."))
            if own:
                out[(owner, file_path)] = own[-1]
        return out

    def links_same_content(self, tumblers: list[str], *, prune: bool = False) -> dict:
        """Count the documents a generator would send to the engine's same-content linker. The
        grouping runs in the engine, so the links it would create are not previewed
        (``same_content_sent`` is what was submitted)."""
        self._same_content_sent += len(tumblers)
        return {"created": 0, "retracted": 0}

    @property
    def path_edges(self) -> int:
        """How many proposals are path edges the engine would still have to resolve."""
        return self._path_edges

    @property
    def unresolved_paths(self) -> int:
        """Path edges the preview dropped because the owner has no document at that path (only with a
        resolver)."""
        return self._unresolved_paths

    @property
    def same_content_sent(self) -> int:
        """Documents submitted to the same-content linker, whose links this preview cannot count."""
        return self._same_content_sent

    # The generators only ever call link_if_absent, but a writer that silently
    # accepted a direct link() would write nothing while reporting nothing —
    # fail loud instead of previewing a lie.
    def link(self, *_args: object, **_kwargs: object) -> None:
        raise NotImplementedError(
            "DryRunLinkWriter received a direct link() call. Dry-run preview "
            "only models link_if_absent; a generator using link() would write "
            "unconditionally and its volume would not appear in this preview."
        )

    def unlink(self, from_t: object, to_t: object, link_type: str = "") -> int:
        """Record a retraction (nexus-h0uy7's same-as prune); delete nothing."""
        self._retracted.append(
            ProposedLink(str(from_t), str(to_t), str(link_type), created_by="")
        )
        return 1

    def close(self) -> None:
        return None

    @property
    def proposed(self) -> list[ProposedLink]:
        return list(self._proposed)

    @property
    def retracted(self) -> list[ProposedLink]:
        return list(self._retracted)

    def count_by_link_type(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in self._proposed:
            out[p.link_type] = out.get(p.link_type, 0) + 1
        return out

    def count_by_creator(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in self._proposed:
            out[p.created_by] = out.get(p.created_by, 0) + 1
        return out

    def fan_out(self) -> dict[str, int]:
        """Proposed out-degree per source document.

        The number that decides whether a pass is a fix or a flood: total
        count hides a single document proposing thousands of edges.
        """
        out: dict[str, int] = {}
        for p in self._proposed:
            out[p.from_tumbler] = out.get(p.from_tumbler, 0) + 1
        return out


def load_existing_link_keys(cat: CatalogReader, *, page: int = 200) -> set[tuple[str, str, str]]:
    """Every ``(from, to, link_type)`` already in the catalog.

    Paged; the whole link set is small enough to hold in memory (order 1e3 on
    a real corpus). Raises rather than returning a partial set, because a
    short read would silently inflate the "would create" count with links that
    already exist.
    """
    keys: set[tuple[str, str, str]] = set()
    offset = 0
    while True:
        batch = cat.link_query(limit=page, offset=offset)
        if not batch:
            break
        for link in batch:
            keys.add((str(link.from_tumbler), str(link.to_tumbler), str(link.link_type)))
        if len(batch) < page:
            break
        offset += page
    return keys
