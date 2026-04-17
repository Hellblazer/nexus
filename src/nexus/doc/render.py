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
) -> str:
    """Return *text* with every in-scope token substituted."""
    tokens = parse_tokens(text)
    if not tokens:
        return text

    out_parts: list[str] = []
    last = 0
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
    return "".join(out_parts)


def render_file(
    path: Path,
    registry: ResolverRegistry,
    *,
    out_dir: Path | None = None,
    allow_unresolved: bool = False,
    emit: bool = True,
) -> RenderResult:
    """Render *path*, writing a ``<stem>.rendered.md`` sibling by default.

    Args:
        path: Source markdown.
        registry: Namespace → Resolver registry.
        out_dir: When set, writes the rendered sibling inside this
            directory instead of next to the source (mirrors the source
            directory structure). When ``None``, writes a sibling.
        allow_unresolved: If True, unresolved tokens are preserved
            verbatim in the output and collected in the result.
        emit: When False, the output is computed but NOT written to
            disk — the ``nx doc validate`` mode.
    """
    text = path.read_text()
    resolved_count = _count_resolvable_tokens(text, registry)
    out = render_text(text, registry, allow_unresolved=allow_unresolved)

    misses: list[tuple[Token, str]] = []
    if allow_unresolved:
        # Re-scan to collect misses (the render loop discards them when
        # the caller asked for permissive mode; callers that need a
        # miss report pay one extra pass here — cheap vs render cost).
        for tok in parse_tokens(text):
            resolver = registry.get(tok.namespace)
            if resolver is None:
                misses.append((tok, f"no resolver for namespace {tok.namespace!r}"))
                continue
            try:
                resolver.resolve(tok.key, tok.field, tok.filters)
            except ResolutionError as exc:
                misses.append((tok, str(exc)))

    if emit:
        dest = _rendered_sibling(path, out_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(out)

    return RenderResult(output=out, resolved=resolved_count, unresolved=misses)


def _count_resolvable_tokens(text: str, registry: ResolverRegistry) -> int:
    return sum(1 for tok in parse_tokens(text) if tok.namespace in registry)


def _rendered_sibling(path: Path, out_dir: Path | None) -> Path:
    if out_dir is None:
        return path.with_suffix(f".rendered{path.suffix}")
    # Mirror tree: preserve the source file's stem + ".rendered"
    return (out_dir / path.name).with_suffix(f".rendered{path.suffix}")
