# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nexus.hooks.t2_prefix_scan`` — ``_snippet()`` and the render caps
in ``_build_output()``. The HTTP-level behaviour of ``scan()`` is pinned in
``tests/hooks/test_t2_prefix_scan.py``."""
from typing import Any

from nexus.hooks.t2_prefix_scan import (
    _HARD_CAP,
    _SNIPPET_LIMIT,
    _TITLE_LIMIT,
    _build_output,
    _snippet,
)

# ── _snippet ─────────────────────────────────────────────────────────────────

def test_snippet_returns_first_meaningful_line() -> None:
    assert _snippet("Hello world") == "Hello world"


def test_snippet_skips_blank_lines() -> None:
    assert _snippet("\n\nHello") == "Hello"


def test_snippet_skips_headings() -> None:
    assert _snippet("# Title\nBody text") == "Body text"


def test_snippet_skips_separator_lines() -> None:
    assert _snippet("---\nContent here") == "Content here"
    assert _snippet("===\nMore content") == "More content"


def test_snippet_truncates_at_max_chars() -> None:
    """Explicit ``max_chars`` rather than the function's own tuned default
    (nexus-h33x8.5 fix-pass: the default was retuned 120->70; hardcoding
    the pre-tune value here made this test a silent pin on a render-density
    constant it has no business caring about)."""
    long_line = "x" * 200
    result = _snippet(long_line, max_chars=50)
    assert result == "x" * 50 + "…"


def test_snippet_no_ellipsis_when_short() -> None:
    result = _snippet("short")
    assert "…" not in result
    assert result == "short"


def test_snippet_returns_empty_for_all_headings() -> None:
    assert _snippet("# H1\n## H2\n### H3") == ""


def test_snippet_returns_empty_for_empty_content() -> None:
    assert _snippet("") == ""


# ── cap algorithm constants ───────────────────────────────────────────────────

def test_cap_constants_are_consistent() -> None:
    """_HARD_CAP must exceed _TITLE_LIMIT which must exceed _SNIPPET_LIMIT."""
    assert _SNIPPET_LIMIT < _TITLE_LIMIT
    assert _TITLE_LIMIT < _HARD_CAP


# ── cap algorithm, through the real _build_output ─────────────────────────────
# _build_output takes its store as a parameter; this one serves fixed rows in
# insertion order, which is the order the engine's DESC listing would give.


class _RowStore:
    """Fake ``HttpMemoryStore`` surface ``_build_output`` calls against
    (nexus-fow78: ``list_entries`` + targeted ``get``, not ``get_all`` —
    proves the render-cap algorithm against the scoped fetch path)."""

    def __init__(self) -> None:
        self._rows: dict[str, list[dict[str, Any]]] = {}
        self.limits_requested: list[int | None] = []
        self.get_calls: list[tuple[str, str]] = []

    def put(self, project: str, title: str, content: str) -> None:
        self._rows.setdefault(project, []).append({"title": title, "content": content})

    def list_entries(
        self,
        project: str | None = None,
        agent: str | None = None,
        limit: int | None = None,
        *,
        timeout: float | None = None,
        retry_read_timeout: bool = True,
    ) -> list[dict[str, Any]]:
        self.limits_requested.append(limit)
        rows = [
            {"title": r["title"], "project": project}
            for r in self._rows.get(project or "", [])
        ]
        return rows[:limit] if limit is not None else rows

    def get(
        self,
        project: str | None = None,
        title: str | None = None,
        id: int | None = None,
        *,
        timeout: float | None = None,
        retry_read_timeout: bool = True,
    ) -> dict[str, Any] | None:
        self.get_calls.append((project or "", title or ""))
        for r in self._rows.get(project or "", []):
            if r["title"] == title:
                return dict(r)
        return None

    def namespaces(self) -> list[dict[str, Any]]:
        return [{"project": p} for p in self._rows]


def _run_scan(store: _RowStore, project_name: str) -> str:
    return "\n".join(_build_output(store, project_name, store.namespaces()))  # type: ignore[arg-type]


def test_entries_up_to_snippet_limit_include_snippet() -> None:
    """The first ``_SNIPPET_LIMIT`` entries per namespace include ' — snippet'."""
    store = _RowStore()
    for i in range(1, _SNIPPET_LIMIT + 1):
        store.put("repo", f"entry-{i}.md", f"Content of entry {i}")
    output = _run_scan(store, "repo")
    assert output.count(" — Content of entry") == _SNIPPET_LIMIT


def test_entries_snippet_limit_to_title_limit_are_title_only() -> None:
    """``_TITLE_LIMIT - _SNIPPET_LIMIT`` entries per namespace appear
    without a snippet (title-only), out of ``_TITLE_LIMIT`` total.

    Derived from the constants themselves (nexus-h33x8.5 fix-pass: the
    caps were retuned 5/8->3/5; a version hardcoding "8"/"5"/"3" would
    have silently pinned the pre-tune values rather than the behavior).
    """
    store = _RowStore()
    for i in range(1, _TITLE_LIMIT + 1):
        store.put("repo", f"entry-{i}.md", f"Content of entry {i}")
    output = _run_scan(store, "repo")
    entry_lines = [ln for ln in output.splitlines() if "entry-" in ln]
    with_snippet = [ln for ln in entry_lines if " — " in ln]
    without_snippet = [ln for ln in entry_lines if " — " not in ln]
    assert len(with_snippet) == _SNIPPET_LIMIT
    assert len(without_snippet) == _TITLE_LIMIT - _SNIPPET_LIMIT


def test_entries_beyond_title_limit_are_marked_as_more() -> None:
    """Entries beyond ``_TITLE_LIMIT`` per namespace are marked '… (more)'.

    nexus-xn9ut: the scan now lists only ``_TITLE_LIMIT + 1`` rows per
    namespace (the whole-project list cost 2.4s of the hook's 9s bound), so
    the exact overflow count ('… (N more)', nexus-h33x8.5) is no longer
    known; the extra row says more exist. Pinned here: the bound reaches
    the store, and the marker still appears."""
    overflow = 3
    store = _RowStore()
    for i in range(1, _TITLE_LIMIT + overflow + 1):
        store.put("repo", f"entry-{i}.md", f"Content {i}")
    output = _run_scan(store, "repo")
    assert "… (more)" in output
    assert store.limits_requested and all(
        lim == _TITLE_LIMIT + 1 for lim in store.limits_requested
    ), store.limits_requested


def test_exactly_title_limit_entries_have_no_more_marker() -> None:
    store = _RowStore()
    for i in range(1, _TITLE_LIMIT + 1):
        store.put("repo", f"entry-{i}.md", f"Content {i}")
    assert "… (more)" not in _run_scan(store, "repo")


def test_hard_cap_across_namespaces() -> None:
    """Total rendered entries across namespaces must not exceed _HARD_CAP."""
    store = _RowStore()
    # Three namespaces each with 10 entries — would be 30 without cap
    for ns in ["repo", "repo_rdr", "repo_knowledge"]:
        for i in range(1, 11):
            store.put(ns, f"{ns}-entry-{i}.md", f"Content {i}")
    output = _run_scan(store, "repo")

    # Count rendered entries (lines with "  " prefix that are not "… (N more)")
    rendered = [
        ln for ln in output.splitlines()
        if ln.startswith("  ") and not ln.startswith("  …")
    ]
    assert rendered, "the cap proves nothing over an empty render"
    assert len(rendered) <= _HARD_CAP


def test_content_fetch_is_scoped_to_snippet_ranks() -> None:
    """nexus-fow78: ``get()`` (the per-entry content fetch) must only be
    issued for the ranks that actually render a snippet (``_SNIPPET_LIMIT``
    per namespace) — never once per entry in the namespace, however large.
    """
    store = _RowStore()
    for i in range(1, 51):
        store.put("repo", f"entry-{i}.md", f"Content {i}")
    _run_scan(store, "repo")
    assert len(store.get_calls) == _SNIPPET_LIMIT
    assert store.get_calls == [("repo", f"entry-{i}.md") for i in range(1, _SNIPPET_LIMIT + 1)]


def test_namespace_header_appears_per_namespace() -> None:
    """Each non-empty namespace gets its own '### T2 Memory ...' header."""
    store = _RowStore()
    store.put("repo", "main.md", "main content")
    store.put("repo_rdr", "rdr.md", "rdr content")
    output = _run_scan(store, "repo")
    assert "### T2 Memory" in output
    assert "### T2 Memory (rdr)" in output
