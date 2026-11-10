# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read a memory directory's index and frontmatter (RDR-238 Technical Design 2 and 3).

Standard library only: the SessionStart curation verb (nexus-qo7ie.16) and
``nx memory curate --files`` (nexus-qo7ie.17) share this one parser, so the hook and the
report never disagree about what "indexed", "oversized" or "replaced" means.

Definitions, applied by :func:`scan`:

* The two index files are ``MEMORY.md`` and ``MEMORY-ondemand.md``. They are never reported
  as unindexed and are exempt from the size check.
* A memory file is a ``*.md`` file directly in the directory (subdirectories are not read).
* "Indexed" means its file name appears, as any ``*.md`` token, in either index file, or a
  memory file that is itself indexed that way links it with a markdown link
  ``[text](target.md)``. That is one hub level, no deeper (Sam, 2026-11-05, nexus-qo7ie.29).
* "Oversized" means more than :data:`OVERSIZE_BYTES` (8192) bytes.
* A "dangling" entry is a markdown link ``[text](target.md)`` in an index file whose target
  does not exist in the directory. Only links count: a bare file name in index prose (a
  mention of ``CLAUDE.md``) is not an entry.
* A file "declares" supersession with ``replaces`` in its YAML frontmatter, as a scalar
  (``replaces: old``), an inline list (``replaces: [a, b]``) or a block list. A declared
  target that is still present is reported. A target is present when a file named ``<slug>``
  or ``<slug>.md`` exists, or any file's frontmatter ``name`` equals the slug; a file never
  counts as its own target.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

INDEX_FILES: tuple[str, ...] = ("MEMORY.md", "MEMORY-ondemand.md")
OVERSIZE_BYTES = 8192

_MD_LINK = re.compile(r"\]\(([^)\s#]+\.md)(?:#[^)]*)?\)")


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def frontmatter_lines(text: str) -> list[str]:
    """The lines between a leading ``---`` and the next ``---``; empty when there is no block."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    block: list[str] = []
    for line in lines[1:]:
        if line.strip() == "---":
            return block
        block.append(line)
    return []  # an unclosed block is not frontmatter


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1].strip()
    return value


def frontmatter_value(text: str, key: str) -> str:
    """The scalar value of *key* in the frontmatter, or ''."""
    prefix = f"{key}:"
    for line in frontmatter_lines(text):
        if line.startswith(prefix):
            return _unquote(line[len(prefix):])
    return ""


def declared_replaces(text: str) -> list[str]:
    """The slugs a file names in its frontmatter ``replaces`` key, in order; [] when none."""
    block = frontmatter_lines(text)
    out: list[str] = []
    for i, line in enumerate(block):
        if not line.startswith("replaces:"):
            continue
        rest = line[len("replaces:"):].strip()
        if rest.startswith("["):
            inner = rest[1:rest.rfind("]")] if "]" in rest else rest[1:]
            out.extend(s for s in (_unquote(p) for p in inner.split(",")) if s)
        elif rest:
            out.append(_unquote(rest))
        else:
            for item in block[i + 1:]:
                stripped = item.strip()
                if not stripped.startswith("- "):
                    break
                value = _unquote(stripped[2:])
                if value:
                    out.append(value)
    return out


def index_mentions(text: str) -> set[str]:
    """Every ``*.md`` file name (basename) the index text mentions, link or bare."""
    names: set[str] = set()
    for token in re.findall(r"[A-Za-z0-9_.\-/]+\.md\b", text):
        names.add(token.rsplit("/", 1)[-1])
    return names


def index_link_targets(text: str) -> list[str]:
    """The relative ``*.md`` link targets of the index entries, in order (URLs excluded)."""
    return [t for t in _MD_LINK.findall(text) if "://" not in t]


@dataclass(frozen=True)
class MemoryScan:
    directory: Path
    index_files: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    unindexed: list[str] = field(default_factory=list)
    oversized: list[tuple[str, int]] = field(default_factory=list)
    dangling: list[tuple[str, str]] = field(default_factory=list)
    replaced_present: list[tuple[str, str, str]] = field(default_factory=list)


def scan(directory: Path, *, oversize: bool = True) -> MemoryScan:
    """Scan *directory* per the module docstring. Raises ``OSError`` only if it cannot be listed.

    *oversize* False skips the per-file size check and leaves ``oversized`` empty: the SessionStart
    hook never reports a size (nexus-qo7ie.27), so it does not pay one ``stat`` per file on every start.
    """
    present = sorted(p.name for p in directory.iterdir() if p.is_file() and p.suffix == ".md")
    index_files = sorted(n for n in present if n in INDEX_FILES)
    files = [n for n in present if n not in INDEX_FILES]

    index_text = {n: read_text(directory / n) for n in index_files}
    mentioned: set[str] = set()
    for text in index_text.values():
        mentioned |= index_mentions(text)

    texts = {n: read_text(directory / n) for n in files}
    hub_linked: set[str] = set()
    for n in files:
        if n in mentioned:
            # A hub link names a memory file only when it points into this directory:
            # "x.md" or "./x.md". "archive/x.md" and "../x.md" are other files.
            hub_linked |= {t.removeprefix("./") for t in index_link_targets(texts[n])
                           if "/" not in t.removeprefix("./")}
    unindexed = [n for n in files if n not in mentioned and n not in hub_linked]
    oversized = []
    for n in files if oversize else ():
        size = (directory / n).stat().st_size
        if size > OVERSIZE_BYTES:
            oversized.append((n, size))
    dangling = [
        (idx, target)
        for idx in index_files
        for target in index_link_targets(index_text[idx])
        if not (directory / target).is_file()
    ]

    names = {n: frontmatter_value(t, "name") for n, t in texts.items()}
    replaced: list[tuple[str, str, str]] = []
    for n in files:
        for slug in declared_replaces(texts[n]):
            target = _find_target(slug, n, files, names)
            if target is not None:
                replaced.append((n, slug, target))
    return MemoryScan(directory, index_files, files, unindexed, oversized, dangling, replaced)


def _find_target(slug: str, source: str, files: list[str], names: dict[str, str]) -> str | None:
    for candidate in (slug, f"{slug}.md"):
        if candidate in files and candidate != source:
            return candidate
    for n in files:
        if n != source and names.get(n) == slug:
            return n
    return None
