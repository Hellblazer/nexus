# SPDX-License-Identifier: AGPL-3.0-or-later
"""Answer synthesis: Haiku-powered cited answer generation."""
from __future__ import annotations

import json as _json
import threading

from nexus.config import HAIKU_MODEL
from nexus.types import SearchResult

_HAIKU_MODEL = HAIKU_MODEL

_anthropic_instance: "object | None" = None
_anthropic_lock = threading.Lock()


def _anthropic_client():
    """Return a cached anthropic.Anthropic instance."""
    global _anthropic_instance
    if _anthropic_instance is not None:
        return _anthropic_instance
    with _anthropic_lock:
        if _anthropic_instance is None:
            import anthropic
            from nexus.config import get_credential
            _anthropic_instance = anthropic.Anthropic(api_key=get_credential("anthropic_api_key"))
    return _anthropic_instance


def _haiku_answer(query: str, results: list[SearchResult]) -> str:
    """Synthesize an answer using Haiku with <cite i="N"> references."""
    import anthropic

    snippets = "\n".join(
        f"[{i}] {r.metadata.get('source_path', 'unknown')}:"
        f"{r.metadata.get('line_start', '?')}\n{r.content[:400]}"
        for i, r in enumerate(results)
    )
    prompt = (
        f"Answer the question: {query}\n\n"
        f"Use these sources (cite with <cite i=\"N\"> inline):\n{snippets}\n\n"
        "Cite each source by index number. Use <cite i=\"N\"> for single source, "
        "<cite i=\"N-M\"> for a range of consecutive sources."
    )
    client = _anthropic_client()
    msg = client.messages.create(
        model=_HAIKU_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    if not msg.content:
        return ""
    return msg.content[0].text


def _haiku_refine(query: str, results: list[SearchResult]) -> dict:
    """Ask Haiku whether to refine the query. Returns {"done": True} or {"query": "..."}."""
    import anthropic

    snippets = "\n".join(
        f"{i}: {r.content[:200]}" for i, r in enumerate(results[:10])
    )
    prompt = (
        f"Query: {query}\n\nTop results:\n{snippets}\n\n"
        "Are these results sufficient? Respond ONLY with valid JSON:\n"
        '{"done": true}  — if results are sufficient\n'
        '{"query": "<refined query>"}  — if results need improvement'
    )
    client = _anthropic_client()
    msg = client.messages.create(
        model=_HAIKU_MODEL,
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    if not msg.content:
        return {"done": True}
    text = msg.content[0].text.strip()
    try:
        return _json.loads(text)
    except _json.JSONDecodeError:
        return {"done": True}


def answer_mode(query: str, results: list[SearchResult]) -> str:
    """Synthesize a cited answer for *query* using Haiku.

    Returns: synthesis text with <cite i="N"> inline + numbered citation footer.
    """
    synthesis = _haiku_answer(query, results)

    # Build citation footer
    footer_lines: list[str] = []
    for i, r in enumerate(results):
        source_path = r.metadata.get("source_path", "?")
        line_start = r.metadata.get("line_start", "?")
        line_end = r.metadata.get("line_end", "?")
        match_pct = max(0.0, 1.0 - r.distance) * 100
        line_ref = f"{line_start}-{line_end}" if line_end != "?" else str(line_start)
        footer_lines.append(f"{i}: {source_path}:{line_ref} ({match_pct:.1f}% match)")

    return synthesis + "\n\n" + "\n".join(footer_lines)
