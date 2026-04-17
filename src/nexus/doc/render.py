# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-082: Render engine for ``nx doc render`` / ``nx doc validate``.

Pipeline: ``parse_tokens → lookup resolver → resolve → substitute``.
A token whose namespace has no registered resolver, or whose resolver
raises :class:`~nexus.doc.resolvers.ResolutionError`, causes render to
fail loud by default. Set ``allow_unresolved=True`` to preserve the
literal token text instead (``--allow-unresolved`` CLI flag).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from nexus.doc.resolvers import ResolutionError, ResolverRegistry
from nexus.doc.tokens import Token, parse_tokens

__all__ = ["RenderError", "RenderResult", "render_text", "render_file"]


class RenderError(Exception):
    """Raised when one or more tokens cannot be resolved and the
    caller did not opt in to ``allow_unresolved``."""


@dataclass(slots=True)
class RenderResult:
    """Result of a render_file call."""

    output: str
    resolved: int
    unresolved: list[tuple[Token, str]]   # (token, reason) for each miss


def render_text(
    text: str,
    registry: ResolverRegistry,
    *,
    allow_unresolved: bool = False,
) -> tuple[str, int, list[tuple[Token, str]]]:
    """Return ``(output, resolved_count, misses)``.

    ``resolved_count`` only counts tokens that a registered resolver
    handled without raising. Tokens with no registered namespace or
    ones that raise ``ResolutionError`` are misses; when
    ``allow_unresolved`` is False the first miss raises ``RenderError``,
    otherwise they accumulate in the ``misses`` list and the literal
    token text is preserved in the output.
    """
    tokens = parse_tokens(text)
    if not tokens:
        return text, 0, []

    out_parts: list[str] = []
    last = 0
    resolved = 0
    misses: list[tuple[Token, str]] = []

    for tok in tokens:
        start, end = tok.span
        out_parts.append(text[last:start])
        resolver = registry.get(tok.namespace)
        if resolver is None:
            if allow_unresolved:
                out_parts.append(tok.raw)
                misses.append((tok, f"no resolver for namespace {tok.namespace!r}"))
            else:
                raise RenderError(
                    f"line {tok.lineno} col {tok.col}: no resolver registered "
                    f"for namespace {tok.namespace!r} in {tok.raw!r}"
                )
        else:
            try:
                value = resolver.resolve(tok.key, tok.field, tok.filters)
                out_parts.append(value)
                resolved += 1
            except ResolutionError as exc:
                if allow_unresolved:
                    out_parts.append(tok.raw)
                    misses.append((tok, str(exc)))
                else:
                    raise RenderError(
                        f"line {tok.lineno} col {tok.col}: {exc} "
                        f"(token={tok.raw!r})"
                    ) from exc
        last = end

    out_parts.append(text[last:])
    return "".join(out_parts), resolved, misses


def render_file(
    path: Path,
    registry: ResolverRegistry,
    *,
    out_dir: Path | None = None,
    allow_unresolved: bool = False,
    emit: bool = True,
    source_root: Path | None = None,
) -> RenderResult:
    """Render *path*, writing a ``<stem>.rendered.md`` sibling by default.

    Args:
        path: Source markdown.
        registry: Namespace → Resolver registry.
        out_dir: When set, writes the rendered sibling inside this
            directory. The source's *relative* path (against
            *source_root*) is preserved — a mirror tree.
        source_root: Anchor for mirror-tree relative-path computation.
            Defaults to ``Path.cwd()``. Ignored when ``out_dir`` is None.
        allow_unresolved: If True, unresolved tokens are preserved
            verbatim in the output and collected in the result.
        emit: When False, the output is computed but NOT written to
            disk — the ``nx doc validate`` mode.
    """
    text = path.read_text()
    out, resolved_count, misses = render_text(
        text, registry, allow_unresolved=allow_unresolved,
    )
    if emit:
        dest = _rendered_sibling(path, out_dir, source_root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(out)
    return RenderResult(output=out, resolved=resolved_count, unresolved=misses)


def _rendered_sibling(
    path: Path, out_dir: Path | None, source_root: Path | None = None,
) -> Path:
    """Compute the destination path for a rendered sibling.

    When *out_dir* is None, the sibling lives next to the source with
    a ``.rendered`` stem suffix. When *out_dir* is set, the source's
    path relative to *source_root* (default: cwd) is preserved under
    *out_dir* — a mirror tree. Sources outside *source_root* fall back
    to basename-only placement under *out_dir*.
    """
    if out_dir is None:
        return path.with_suffix(f".rendered{path.suffix}")
    anchor = (source_root or Path.cwd()).resolve()
    try:
        rel = path.resolve().relative_to(anchor)
    except ValueError:
        rel = Path(path.name)
    return (out_dir / rel).with_suffix(f".rendered{path.suffix}")
