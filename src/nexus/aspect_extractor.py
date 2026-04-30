# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Synchronous structured-aspect extractor (RDR-089 Phase 1.2,
RDR-096 P1.2 + P2.2).

Single public entrypoint: ``extract_aspects(content, source_path,
collection) -> AspectRecord | ExtractFail | None``. The function is
synchronous top to bottom — no ``async def``, no ``await``, no
event loop. The RDR-089 load-bearing contract requires this so the
document-grain hook chain (``fire_post_document_hooks``) can call it
from a sync dispatcher without dropping a coroutine.

**Going-forward writer contract** (RDR-096 P2.2 binds this; gate-
binding for the null-row DELETE migration):

* ``ExtractFail`` paths emit NO row at all. Read failures surface
  through the typed sentinel; the upsert-guard in
  ``commands/enrich.py`` skips on any ``ExtractFail``. This is the
  structural guarantee that pre-RDR-096 null-field rows are
  unreachable from new writes.
* Subprocess success paths (``_build_record``) populate at least
  one aspect field OR set ``confidence`` to a non-NULL value. The
  current Sonnet/Haiku extractors return real confidences; the
  ``rdr-frontmatter-v1`` parser sets ``confidence=1.0`` even when
  the document has no scholarly structure (the "structured-zero"
  case — empty fields, deterministic).
* Subprocess hard-failure paths (``_empty_record``) — schema
  validation failure, retry-exhausted — emit a null-fields row
  with ``confidence=None``. **NOTE**: this is the one path that
  produces ``(all-empty fields, extras='{}', confidence IS NULL)``
  going forward, distinguishable from RDR-096-pre read failures
  only by ``extracted_at`` timestamp. The P2.2 DELETE migration
  ran once at 4.16.0 ship time; subsequent retry-exhausted rows
  are the operator's signal that a real triage event happened
  (not a pre-RDR-096 ghost).

Phase 1 ships exactly one extractor config — ``scholarly-paper-v1``,
keyed on the ``knowledge__*`` collection prefix. Other prefixes
return ``None`` from ``extract_aspects``; the calling hook should
no-op on ``None``.

Invocation: ``subprocess.run(["claude", "-p", PROMPT,
"--output-format", "json"], timeout=180, capture_output=True,
text=True)``. The Claude CLI is expected to be on PATH; if it is
not, ``subprocess.run`` raises ``FileNotFoundError`` which propagates
(configuration error, not a runtime extraction failure).

The ``--output-format json`` flag returns a wrapper of the form
``{"result": "<model-response-text>", "session_id": ..., "usage":
...}``. The extractor pulls ``result`` and re-parses it as JSON.
Markdown code-fence wrapping (```json ... ```) is stripped
defensively before the inner parse.

Retry policy (audit F8):

* Retriable: ``TimeoutExpired``, non-zero exit with transient
  stderr ("rate limit", "overload", "network"), JSON parse
  failure on stdout.
* Non-retriable: schema-validation failure (response shape wrong
  — retry produces same shape), hard subprocess errors with
  non-transient stderr, source_path read failure (file truly
  missing).
* Max 3 attempts (1 initial + 2 retries). Exponential backoff
  with ±25% jitter: 2 s / 5 s / 12 s base intervals.
* Final failure produces an ``AspectRecord`` with all aspect
  fields null; ``extracted_at`` / ``extractor_name`` /
  ``model_version`` are populated. The row is queryable for
  triage — failure is visible (logged), not silent.
"""
from __future__ import annotations

import json
import random
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import structlog

from nexus.aspect_readers import ReadFail, ReadOk, read_source, uri_for
from nexus.db.t2.document_aspects import AspectRecord
from nexus.mcp_infra import get_t3

# Re-exported for ergonomic ``from nexus.aspect_extractor import
# AspectRecord``. Phase 2 hook authors land here first; the
# canonical definition stays with the store.
__all__ = [
    "AspectRecord",
    "ExtractFail",
    "ExtractorConfig",
    "extract_aspects",
    "select_config",
]

_log = structlog.get_logger(__name__)


# ── Read-failure sentinel (RDR-096 P1.2) ─────────────────────────────────────


@dataclass(frozen=True)
class ExtractFail:
    """Typed failure sentinel returned by ``extract_aspects`` when a
    source could not be read.

    The upsert-guard in ``commands/enrich.py`` (P1.3) skips on any
    ``ExtractFail`` — no row is written to ``document_aspects``. That
    is the structural guarantee replacing the prior null-field-row
    contract that polluted operator SQL fast paths.

    Reason space (string-typed for forward-compat with future readers):

    * ``unreachable`` / ``unauthorized`` / ``scheme_unknown`` /
      ``empty`` — passed through from
      :class:`nexus.aspect_readers.ReadFailReason`.
    * ``infra_unavailable`` — T3 client could not be obtained
      (singleton init failure). The URI was constructed but never
      dispatched. Distinct from ``unreachable`` so operators can
      separate "bad URI" from "no client".

    ``detail`` is operator-readable.
    """
    uri: str
    reason: str
    detail: str


# ── Extractor config ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExtractorConfig:
    """A collection-prefix-keyed extractor recipe.

    Two extractor shapes are supported:

    * **Claude-CLI extractor** (default): ``parser_fn`` is None and
      ``extract_aspects`` invokes the Claude subprocess with
      ``prompt_template.format(content=...)``. Used by
      ``scholarly-paper-v1`` for scholarly papers — fields
      require LLM-grade prose understanding.
    * **Pure-Python extractor** (``parser_fn`` set): the function
      receives ``(content, source_path, collection)`` and returns
      a dict matching the aspect schema. Used by ``rdr-frontmatter-v1``
      for RDR documents — the structure is deterministic
      (YAML frontmatter + labelled markdown sections), so a
      markdown parser handles it more reliably and at zero API
      cost.

    ``prompt_template`` is interpolated at call time with the
    document content (Claude-CLI path only). ``required_fields``
    lists keys that must be present in the parsed JSON for the
    response to validate. ``parser_fn`` if non-None is the
    deterministic alternative — when set, the Claude subprocess
    path is bypassed entirely.
    """

    extractor_name: str
    model_version: str
    prompt_template: str
    required_fields: tuple[str, ...]
    parser_fn: object = None  # Optional[Callable[[str, str, str], dict]]


_SCHOLARLY_PAPER_PROMPT = """\
You are extracting structured aspects from a scholarly paper. Read the
paper text below and return a JSON object with EXACTLY the following
fields:

  problem_formulation:    string — one or two sentences naming the
                          problem the paper addresses.
  proposed_method:        string — one to three sentences naming the
                          paper's proposed approach.
  experimental_datasets:  array of strings — datasets used (empty
                          array if none mentioned).
  experimental_baselines: array of strings — baselines compared
                          against (empty array if none mentioned).
  experimental_results:   string — one to two sentences summarizing
                          the headline experimental result.
  extras:                 JSON object — any other useful structured
                          fields (e.g. venue, ablations_present,
                          code_release). May be empty.
  confidence:             number in [0, 1] — your confidence that
                          the extraction is faithful to the paper.

Respond with ONLY the JSON object (no Markdown, no commentary).

Paper text follows:

---

{content}
"""


_SCHOLARLY_BATCH_PROMPT_HEADER = """\
You are extracting structured aspects from N scholarly papers in one
pass. Each paper carries a ``source_path`` identifier; you must echo
that identifier back in each output entry so the caller can match
extractions to inputs.

Return a JSON object with this exact shape:

  {{
    "papers": [
      {{
        "source_path": "<verbatim from input>",
        "problem_formulation": string — one or two sentences,
        "proposed_method": string — one to three sentences,
        "experimental_datasets": array of strings (empty if none),
        "experimental_baselines": array of strings (empty if none),
        "experimental_results": string — one to two sentences,
        "extras": JSON object (may be empty),
        "confidence": number in [0, 1]
      }},
      ...
    ]
  }}

The ``papers`` array must have EXACTLY one entry per input paper, in
the same order. Echo each paper's ``source_path`` verbatim. Respond
with ONLY the JSON object (no Markdown, no commentary).

The papers follow, separated by ``=====``. Each paper is preceded by
its ``source_path`` line.
"""


_SCHOLARLY_PAPER_CONFIG = ExtractorConfig(
    extractor_name="scholarly-paper-v1",
    # The model is pinned at config; the actual Claude CLI may
    # use a different model based on user config. This is the
    # *expected* model version for this extractor recipe and is
    # what gets stored in document_aspects.model_version so the
    # version-filter query (list_by_extractor_version) can
    # identify rows captured under an outdated recipe.
    model_version="claude-haiku-4-5-20251001",
    prompt_template=_SCHOLARLY_PAPER_PROMPT,
    required_fields=(
        "problem_formulation",
        "proposed_method",
        "experimental_datasets",
        "experimental_baselines",
        "experimental_results",
    ),
)


_REGISTRY: dict[str, ExtractorConfig] = {
    "knowledge__": _SCHOLARLY_PAPER_CONFIG,
    # docs__* (#377): markdown / ADR / design-doc collections produced
    # by `nx index repo` hold the same kind of substantive prose that
    # `knowledge__*` does — architecturally identical to scholarly
    # papers from the aspect-extraction perspective. Reuse the same
    # config so problem_formulation / proposed_method / etc. fields
    # apply uniformly. If a dedicated docs-prose schema ever
    # warrants splitting, the alias here can be replaced with a
    # purpose-built ExtractorConfig.
    "docs__": _SCHOLARLY_PAPER_CONFIG,
    # rdr__* — pure-Python extractor (RDR-089 Phase F). RDRs carry
    # YAML frontmatter + labelled markdown sections; a deterministic
    # parser is more reliable and zero-cost compared to forcing the
    # 5-field LLM extraction shape onto the document.
    "rdr__": ExtractorConfig(
        extractor_name="rdr-frontmatter-v1",
        model_version="rdr-frontmatter-v1",  # not a real model, pinned to recipe id
        prompt_template="",  # unused (parser_fn shortcut)
        required_fields=(
            "problem_formulation",
            "proposed_method",
            "experimental_datasets",
            "experimental_baselines",
            "experimental_results",
        ),
        parser_fn=lambda content, source_path, collection: _parse_rdr_aspects(content),
    ),
}


# ── RDR markdown + frontmatter parser (Phase F) ─────────────────────────────


# Section-name aliases. RDR template evolution has produced slight
# variations in the canonical heading text; tolerate them so the
# parser does not silently drop fields when authors stray from the
# template (e.g. "Problem" instead of "Problem Statement").
_RDR_SECTION_ALIASES = {
    "problem_formulation": (
        "Problem Statement", "Problem", "Context", "Motivation",
    ),
    "proposed_method": (
        "Proposed Solution", "Approach", "Design", "Solution",
    ),
    "experimental_results": (
        "Validation", "Results", "Outcomes", "Test Plan",
    ),
    "alternatives": (
        "Alternatives Considered", "Alternatives",
    ),
}

# Cap on per-section text persisted to document_aspects. RDR sections
# can be multi-page; the aspect column is meant for summaries, not
# full reproduction. 800 chars is enough for a few paragraphs of
# context.
_RDR_SECTION_TEXT_CAP = 800


def _parse_rdr_aspects(content: str) -> dict:
    """Deterministic RDR aspect parser. No LLM call.

    Maps RDR structure to the 5-field aspect schema:

      * problem_formulation ← "Problem Statement" (or alias) text
      * proposed_method     ← "Proposed Solution" (or alias) text
      * experimental_datasets ← always [] for RDRs
      * experimental_baselines ← list of "Alternative N: <title>"
                                 headers from "Alternatives Considered"
      * experimental_results ← "Validation" / "Results" text
      * extras              ← frontmatter dict (rdr_id, rdr_type,
                                 rdr_status, rdr_priority, title,
                                 related_issues), plus parsed
                                 ``related_rdrs`` extracted from
                                 the frontmatter ``related_issues``
                                 list

    Returns a dict matching the same schema ``extract_aspects``
    returns. ``confidence`` is 1.0 (deterministic parse).

    Failures (unparseable frontmatter, missing sections) yield
    empty / null fields rather than raising; the caller's
    ``_build_record`` schema validation catches required-field
    omissions and the standard null-fields fallback applies.
    """
    frontmatter, body = _split_rdr_frontmatter(content)
    sections = _parse_rdr_sections(body)

    extras = {}
    for key in (
        "id", "type", "status", "priority",
        "title", "author", "accepted_date", "closed_date", "created",
    ):
        if key in frontmatter and frontmatter[key] is not None:
            extras[f"rdr_{key}"] = frontmatter[key]
    if "related_issues" in frontmatter:
        related = frontmatter["related_issues"]
        if isinstance(related, list):
            extras["related_issues"] = [str(x) for x in related]
        elif isinstance(related, str) and related:
            extras["related_issues"] = [related]

    problem = _select_section(sections, _RDR_SECTION_ALIASES["problem_formulation"])
    method = _select_section(sections, _RDR_SECTION_ALIASES["proposed_method"])
    results = _select_section(sections, _RDR_SECTION_ALIASES["experimental_results"])
    alternatives_text = _select_section(sections, _RDR_SECTION_ALIASES["alternatives"])

    baselines = _parse_rdr_alternatives(alternatives_text) if alternatives_text else []

    return {
        "problem_formulation": _truncate(problem, _RDR_SECTION_TEXT_CAP),
        "proposed_method": _truncate(method, _RDR_SECTION_TEXT_CAP),
        "experimental_datasets": [],
        "experimental_baselines": baselines,
        "experimental_results": _truncate(results, _RDR_SECTION_TEXT_CAP),
        "extras": extras,
        "confidence": 1.0,
    }


def _split_rdr_frontmatter(content: str) -> tuple[dict, str]:
    """Split YAML frontmatter from the markdown body.

    RDR frontmatter is delimited by ``---`` lines at file start;
    parsed best-effort with a minimal YAML reader to avoid pulling
    pyyaml into the extractor's dependency footprint. Supports the
    keys actually used in the project's RDR template: scalar
    strings, integers, dates (treated as strings), and inline
    JSON-like lists ``[a, b, c]``.

    Returns ``({}, content)`` when frontmatter is absent or
    unparseable.
    """
    if not content.lstrip().startswith("---"):
        return {}, content
    # Find the opening fence.
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != "---":
        return {}, content
    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].rstrip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        return {}, content
    fm_text = "".join(lines[1:end_idx])
    body = "".join(lines[end_idx + 1:])
    return _parse_simple_yaml(fm_text), body


def _parse_simple_yaml(text: str) -> dict:
    """Minimal YAML reader for the keys an RDR frontmatter uses.

    Supported shapes:

    * ``key: value`` — scalar string. Surrounding double-quotes
      are stripped.
    * ``key: 42`` — integer scalar.
    * ``key: [a, b, c]`` — inline list, comma-split, quotes
      stripped per item.
    * ``key:\\n  - foo\\n  - bar`` — block list, accumulated as a
      Python list of stripped strings (with quote / bracket
      stripping per item).
    * ``key: |`` or ``key: >`` — block scalar indicator stored as
      ``None`` (the multi-line content is not preserved; the
      indicator alone is not useful and the previous version
      stored the literal ``|`` character which was misleading).

    Unsupported shapes (nested mappings, anchor references, the
    full set of YAML's quirks) yield string values verbatim or
    ``None`` rather than raising. Out-of-spec frontmatter degrades
    gracefully — callers should use the ``extras`` field for
    informational use only, not as a typed contract.

    Substantive critic finding: previous version corrupted block
    scalars to the indicator character and dropped block lists
    silently. Both are fixed here.
    """
    out: dict = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw_line = lines[i]
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            i += 1
            continue

        # Block scalar indicator: ``key: |`` or ``key: >``. Real YAML
        # would consume the indented continuation lines as the scalar
        # content; we store ``None`` and skip indented continuations
        # so they do not get misparsed as separate keys.
        if value in ("|", ">", "|-", ">-", "|+", ">+"):
            out[key] = None
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                # Continuation: blank or starts with whitespace.
                if nxt == "" or (nxt and nxt[0] in (" ", "\t")):
                    j += 1
                    continue
                break
            i = j
            continue

        # Empty value after colon: possibly the head of a block list.
        # Look ahead for ``  - <item>`` lines.
        if value == "":
            j = i + 1
            block_items: list[str] = []
            while j < len(lines):
                nxt = lines[j]
                stripped = nxt.lstrip()
                if not stripped:
                    j += 1
                    continue
                if not nxt.startswith((" ", "\t")):
                    break
                if stripped.startswith("- "):
                    item = stripped[2:].strip()
                    item = item.strip('"').strip("'")
                    if item:
                        block_items.append(item)
                    j += 1
                    continue
                # Indented but not a list item — treat as end of
                # block list (could be a nested mapping which we
                # do not support).
                break
            if block_items:
                out[key] = block_items
                i = j
                continue
            # Empty value with no block list → store empty string.
            out[key] = ""
            i += 1
            continue

        # Strip wrapping double-quotes
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        # Inline list: [a, b, c]
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            if not inner:
                out[key] = []
            else:
                items = [
                    s.strip().strip('"').strip("'")
                    for s in inner.split(",")
                ]
                out[key] = [s for s in items if s]
            i += 1
            continue
        # Numeric scalar
        if value.lstrip("-").isdigit():
            try:
                out[key] = int(value)
                i += 1
                continue
            except ValueError:
                pass
        out[key] = value
        i += 1
    return out


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.MULTILINE)
_FENCE_RE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)


def _mask_code_fences(body: str) -> str:
    """Replace fenced-code-block content with a same-length string of
    non-heading characters (newlines preserved verbatim, every other
    char becomes a space). Result has identical character offsets and
    line breaks as ``body`` so heading regex matches against the
    masked string can index back into ``body`` directly.

    Prevents shell ``## comment`` lines or quoted markdown snippets
    inside ``` fences from being mis-detected as section headings.
    """
    def _mask(m: re.Match) -> str:
        chunk = m.group(0)
        return "".join("\n" if c == "\n" else " " for c in chunk)
    return _FENCE_RE.sub(_mask, body)


def _parse_rdr_sections(body: str) -> dict[str, str]:
    """Slice the markdown body into a {section_name: section_text} dict.

    Recognises ``## Section Name`` headings as section delimiters.
    Subsections (``###`` and below) are kept inside the parent
    section's text. The text of each section is everything from
    the heading line up to the next ``##`` heading (exclusive),
    stripped of the heading line itself.

    Headings inside fenced code blocks (e.g. shell ``## comment``
    lines or quoted markdown samples) are not treated as section
    delimiters.
    """
    masked = _mask_code_fences(body)
    sections: dict[str, str] = {}
    matches = list(_HEADING_RE.finditer(masked))
    h2_matches = [m for m in matches if len(m.group(1)) == 2]
    for i, m in enumerate(h2_matches):
        name = m.group(2).strip()
        start = m.end()
        end = h2_matches[i + 1].start() if i + 1 < len(h2_matches) else len(body)
        # Mask preserves offsets, so body[start:end] yields the
        # original section text including the (now-quoted) fenced code.
        text = body[start:end].strip()
        sections[name] = text
    return sections


def _select_section(sections: dict[str, str], aliases: tuple[str, ...]) -> str:
    """Return the text of the first matching section name in ``aliases``,
    or '' when none of the aliases is present."""
    for alias in aliases:
        if alias in sections:
            return sections[alias]
    return ""


_ALTERNATIVE_HEADER_RE = re.compile(
    r"^###\s+(?:Alternative\s+\d+\s*:?\s*)?(.+?)\s*$",
    re.MULTILINE,
)


def _parse_rdr_alternatives(text: str) -> list[str]:
    """Extract alternative titles from the "Alternatives Considered"
    section. RDR templates use ``### Alternative N: <title>`` headers
    (the 'Alternative N:' prefix is optional / sometimes ``### <title>``
    plain). Returns the title list, or an empty list when no
    sub-headings are present.
    """
    titles: list[str] = []
    for m in _ALTERNATIVE_HEADER_RE.finditer(text):
        title = m.group(1).strip()
        if title:
            titles.append(title)
    return titles


def _truncate(text: str, cap: int) -> str:
    """Cap a long section to ``cap`` characters; append an ellipsis
    when truncation occurs. Empty strings pass through unchanged."""
    if not text:
        return text
    if len(text) <= cap:
        return text
    return text[:cap].rstrip() + "..."


def select_config(collection: str) -> ExtractorConfig | None:
    """Return the registered ``ExtractorConfig`` whose prefix matches
    ``collection``, or ``None`` if no prefix matches.

    Two prefixes ship:

    * ``knowledge__*`` → ``scholarly-paper-v1`` (Claude-CLI subprocess
      path, RDR-089 Phase 1).
    * ``rdr__*`` → ``rdr-frontmatter-v1`` (deterministic markdown +
      frontmatter parser, RDR-089 Phase F; zero API cost).

    Other prefixes (``docs__``, ``code__``, bare ``knowledge``
    without the double-underscore separator, etc.) return ``None``.
    """
    for prefix, config in _REGISTRY.items():
        if collection.startswith(prefix):
            return config
    return None


# ── Retry helpers ────────────────────────────────────────────────────────────


_RETRY_ATTEMPTS = 3
_RETRY_BASE_SECONDS = (2.0, 5.0, 12.0)
_RETRY_JITTER_FRACTION = 0.25

_TRANSIENT_STDERR_PATTERNS = (
    "rate limit",
    "rate_limit",
    "overload",
    "overloaded_error",
    "network",
    "timeout",
    "timed out",
)


def _sleep_with_jitter(attempt: int) -> None:
    """Sleep for the attempt's base interval with ±25% jitter.

    ``attempt`` is the zero-indexed attempt number — sleep between
    attempt N and N+1 uses ``_RETRY_BASE_SECONDS[attempt]``. No-op if
    ``attempt`` is past the schedule.
    """
    if attempt >= len(_RETRY_BASE_SECONDS):
        return
    base = _RETRY_BASE_SECONDS[attempt]
    delta = base * _RETRY_JITTER_FRACTION
    time.sleep(base + random.uniform(-delta, delta))


class _TransientFailure(Exception):
    """Raised on a retriable failure; the retry loop sleeps and tries
    again until the attempt cap is hit."""


class _HardFailure(Exception):
    """Raised on a non-retriable failure; the retry loop returns
    immediately without further attempts."""


# ── T3 content sourcing (RDR-089 follow-up) ──────────────────────────────────


# Section types worth sending to the scholarly-paper extractor. Anything
# else (references, acknowledgements, appendix) is dropped — they carry
# no signal for the 5-field aspect schema and only inflate the prompt.
_SCHOLARLY_SECTIONS = frozenset({
    "abstract", "introduction", "related_work",
    "methods", "results", "discussion", "conclusion",
    # Empty section_type comes from chunks before the first heading
    # (typically the title page + abstract for academic papers). Keep
    # it so we don't accidentally drop the abstract on PDFs whose
    # heading detection missed the "Abstract" label.
    "",
})

# Cap on the joined T3 content forwarded to the extractor. ~80 KB
# accommodates a 14-page paper's relevant sections while leaving
# generous headroom under the prompt budget. The extractor prompt
# itself is now stdin-fed (see _invoke_once below) so this cap exists
# for cost / latency / context-window reasons, not OS argv limits.
_T3_CONTENT_CAP_BYTES = 80_000


def _source_content_from_t3(collection: str, source_path: str) -> str:
    """Reassemble the source document's text from T3 chunks, preferring
    section-scoped chunks for scholarly papers.

    .. deprecated:: RDR-096 P1.2 (2026-04-27)

       ``extract_aspects`` no longer calls this function — it routes
       through :func:`nexus.aspect_readers.read_source` with a
       ``chroma://<collection>/<source_path>`` URI instead. The
       function is retained as a deprecation shim for the worker's
       batch-path pre-fetch (``aspect_worker._process_batch``) until
       the worker migrates to ``read_source`` in a follow-up bead;
       slated for removal in Phase 5 of RDR-096.

    Returns an empty string on any failure (catalog lookup miss, T3
    error, no chunks). Callers must treat the empty return as a signal
    to fall back to disk read.

    Routing notes:

    * For ``knowledge__*`` collections the result is filtered to
      :data:`_SCHOLARLY_SECTIONS` and capped at
      :data:`_T3_CONTENT_CAP_BYTES`. This is the path that drove the
      whole change: we previously slurped 14 pages of dense text into
      argv.
    * For other collections the full document is reassembled (no
      filter). The cap still applies as a defensive bound.

    The function is best-effort by design — every failure path returns
    ``""`` and the caller falls back to the disk read. We don't want
    the aspect extractor to crash because T3 is briefly unavailable.
    """
    import warnings  # noqa: PLC0415
    warnings.warn(
        "nexus.aspect_extractor._source_content_from_t3 is deprecated; "
        "callers should migrate to nexus.aspect_readers.read_source with "
        "a chroma:// URI. Slated for removal in RDR-096 Phase 5.",
        DeprecationWarning,
        stacklevel=2,
    )
    try:
        from nexus.db.t3 import T3Database  # noqa: PLC0415

        # Bare constructor — credential fallback (chroma_tenant/database/api_key)
        # comes from nexus.config via get_credential(); no positional args.
        with T3Database() as t3:
            try:
                coll = t3.get_or_create_collection(collection)
            except Exception:
                # CloudClient raises on missing collection; treat as
                # "no chunks" so the disk fallback can run.
                return ""
            from nexus.db.chroma_quotas import QUOTAS  # noqa: PLC0415

            # Page through all chunks for this source. QUOTAS.MAX_QUERY_RESULTS
            # caps the per-call limit; documents with more chunks than
            # the cap are paginated. 300 (current cap) covers ~450 KB
            # of chunked text per page — a single page suffices for
            # all but the longest scholarly papers.
            collected: list[tuple[int, str, str]] = []  # (chunk_index, section_type, text)
            offset = 0
            page_limit = QUOTAS.MAX_QUERY_RESULTS
            while True:
                got = coll.get(
                    where={"source_path": source_path},
                    include=["documents", "metadatas"],
                    limit=page_limit,
                    offset=offset,
                )
                docs = got.get("documents") or []
                mds = got.get("metadatas") or []
                if not docs:
                    break
                for md, doc in zip(mds, docs):
                    if not doc:
                        continue
                    ci = md.get("chunk_index", 0) if isinstance(md, dict) else 0
                    st = md.get("section_type", "") if isinstance(md, dict) else ""
                    collected.append((int(ci), str(st), doc))
                if len(docs) < page_limit:
                    break
                offset += page_limit

            if not collected:
                return ""

            collected.sort(key=lambda x: x[0])
            if collection.startswith("knowledge__"):
                kept = [
                    (ci, st, txt) for ci, st, txt in collected
                    if st in _SCHOLARLY_SECTIONS
                ]
                # If section filtering nuked everything (heading detection
                # failed entirely on this document), fall back to all
                # chunks rather than returning "" — better degraded
                # extraction than no extraction.
                if not kept:
                    kept = collected
            else:
                kept = collected

            joined = "\n\n".join(txt for _, _, txt in kept)
            if len(joined.encode("utf-8", errors="replace")) > _T3_CONTENT_CAP_BYTES:
                # Truncate at byte boundary (decoded back with errors=ignore)
                # so the cap isn't broken by a multi-byte UTF-8 split.
                joined = joined.encode("utf-8", errors="replace")[:_T3_CONTENT_CAP_BYTES]\
                    .decode("utf-8", errors="ignore")
            return joined
    except Exception:
        _log.debug(
            "aspect_extractor_t3_source_failed",
            collection=collection,
            source_path=source_path,
            exc_info=True,
        )
        return ""


# ── Extraction core ──────────────────────────────────────────────────────────


def extract_aspects(
    content: str,
    source_path: str,
    collection: str,
    *,
    lookup_path: str = "",
) -> AspectRecord | ExtractFail | None:
    """Synchronously extract aspects from ``content`` and return either
    a populated ``AspectRecord``, an :class:`ExtractFail` sentinel
    when the source could not be read, or ``None`` when ``collection``
    has no registered extractor.

    Content-sourcing contract (RDR-096 P1.2 — replaces the prior
    T3-then-disk fallback):

    * ``content`` non-empty → used directly.
    * ``content == ""`` → construct
      ``chroma://<collection>/<lookup_path or source_path>`` and
      dispatch through :func:`nexus.aspect_readers.read_source`. The
      chroma reader reassembles the document from T3 chunks via the
      ``CHROMA_IDENTITY_FIELD`` dispatch table (``title`` for
      ``knowledge__*`` collections, ``source_path`` for everything
      else).
    * Read failure → return ``ExtractFail(uri, reason, detail)`` with
      no row written. Replaces the prior null-field-row contract that
      let drift cases (catalog-stale paths, slug-shaped sources)
      pollute downstream SQL.

    nexus-v9az: ``lookup_path`` decouples the chroma identity match
    from the storage key. ``source_path`` is preserved on the
    ``AspectRecord`` for downstream joins; ``lookup_path`` (when
    provided) is used to build the chroma URI. Use case: catalog
    rows recovered via ``--from-t3`` carry relative ``file_path`` but
    chunks were ingested with absolute ``source_path``; the caller
    passes the absolute path as ``lookup_path`` so the chroma reader
    actually finds the chunks.

    On unsupported collection (no config match) returns ``None``; the
    caller (post-store hook) should no-op.

    On extractor failure past content sourcing (subprocess timeout,
    schema validation, retry-exhausted), returns an ``AspectRecord``
    with aspect fields null and ``confidence=None``; the row remains
    queryable for triage. That subprocess-failure path is intentionally
    out of scope for the P1.2 read-side fix.
    """
    config = select_config(collection)
    if config is None:
        return None

    if not content:
        # P1.2: URI-based source dispatch. The URI is constructed from
        # ``(collection, lookup_path or source_path)`` at extract-time.
        # nexus-v9az: ``lookup_path`` overrides the URI's identity
        # segment when it differs from the storage ``source_path``
        # (relative-vs-absolute reconciliation after ``--from-t3``
        # recovery). ``source_path`` percent-encoding keeps directory
        # separators intact (``safe='/'``) and avoids ``#``/``?``
        # being parsed as URI fragments / queries by the reader.
        uri_identity = lookup_path or source_path
        uri = f"chroma://{collection}/{quote(uri_identity, safe='/')}"
        try:
            t3 = get_t3()
        except Exception as exc:
            _log.warning(
                "aspect_extractor_t3_unavailable",
                uri=uri,
                error=str(exc),
            )
            return ExtractFail(
                uri=uri,
                reason="infra_unavailable",
                detail=f"get_t3 failed: {type(exc).__name__}: {exc}",
            )
        result = read_source(uri, t3=t3)
        if isinstance(result, ReadFail):
            _log.warning(
                "aspect_extractor_source_unreadable",
                uri=uri,
                reason=result.reason,
                detail=result.detail,
            )
            return ExtractFail(uri=uri, reason=result.reason, detail=result.detail)
        assert isinstance(result, ReadOk)
        content = result.text

    # Defense against embedded null bytes (P1.3 spike finding 2026-04-25):
    # some PDF extractors emit \x00 in their text output. Strip them
    # before prompting (they carry no semantic content) and before any
    # subprocess hand-off (POSIX argv rejects them outright).
    content = content.replace("\x00", "")

    if config.parser_fn is not None:
        # Pure-Python parser path (RDR-089 Phase F: rdr-frontmatter-v1).
        # No subprocess, no retry budget, no Claude API cost. Errors
        # surface as null-fields records.
        try:
            parsed = config.parser_fn(content, source_path, collection)
        except Exception as exc:
            _log.warning(
                "aspect_extractor_parser_fn_raised",
                extractor=config.extractor_name,
                source_path=source_path,
                error=str(exc),
                exc_info=True,
            )
            return _empty_record(source_path, collection, config)
        if not isinstance(parsed, dict):
            _log.warning(
                "aspect_extractor_parser_fn_wrong_shape",
                extractor=config.extractor_name,
                source_path=source_path,
                got=type(parsed).__name__,
            )
            return _empty_record(source_path, collection, config)
        return _build_record(parsed, source_path, collection, config)

    prompt = config.prompt_template.format(content=content)
    parsed = _retry_subprocess(prompt, config)
    if parsed is None:
        return _empty_record(source_path, collection, config)

    return _build_record(parsed, source_path, collection, config)


# ── Batch extraction (RDR-089 Phase 4 — one Claude call per N papers) ────────


# Per-paper timeout add for batched calls. Single-paper timeout is 180 s
# (see _invoke_once); each additional paper adds this many seconds.
# Conservatively chosen: real-world claude-paper extraction at 25 s per
# document means a batch of 5 takes ~75-100 s in practice; the timeout
# headroom (180 + 4 * 60 = 420 s for batch=5) absorbs Claude's variance.
_BATCH_TIMEOUT_PER_EXTRA_PAPER_S = 60


def extract_aspects_batch(
    items: list[tuple[str, str, str]],
) -> list[AspectRecord | None]:
    """Synchronously extract aspects for N papers in ONE Claude call.

    Each input tuple is ``(collection, source_path, content)``. Returns
    a list aligned with input order: ``AspectRecord`` per paper on
    success, ``None`` for papers whose collection has no registered
    extractor (the batch only covers a single registered config — see
    grouping note below), or a null-fields ``AspectRecord`` for
    papers whose individual extraction failed even though the batch
    as a whole succeeded.

    Cost amortisation: one ``claude -p`` call extracts N papers,
    sharing the model's per-call overhead (system prompt, response
    framing, network round-trip) across all of them. At ``N=5`` the
    measured speedup is typically 2-3x over five sequential single-
    paper calls, with each individual paper getting slightly less
    attention from the model. Use single-paper extraction when the
    extractor must be conservative; use batch when corpus drain time
    dominates.

    Single-config batches: every input must map to the same
    ``ExtractorConfig`` (same prompt template + schema). The batch
    function rejects mixed configs and returns ``None`` for any
    paper whose collection has no config (the caller should not
    have included such papers).

    Empty-content fallback: papers with ``content=""`` are read
    from disk by the worker before this function is called; this
    function does NOT do its own source-path reading. If a paper's
    ``content`` is empty when this runs, it lands in the null-
    fields output without invoking the subprocess for that paper.
    The single-paper ``extract_aspects`` (above) keeps the disk-
    read fallback for the simpler call path.

    Subprocess + retry semantics mirror the single-paper path:
    transient failures retry up to 3 times with exponential
    backoff; hard failures (schema validation, non-transient
    stderr) yield null-fields records for the affected papers
    without retrying.
    """
    if not items:
        return []

    # Group by ExtractorConfig (allows future multi-config batches; for
    # Phase D ships single-config support — multi-config raises).
    config = None
    out: list[AspectRecord | None] = [None] * len(items)
    for idx, (collection, _source_path, _content) in enumerate(items):
        item_config = select_config(collection)
        if item_config is None:
            out[idx] = None
            continue
        if config is None:
            config = item_config
        elif config is not item_config:
            raise ValueError(
                f"extract_aspects_batch requires all items to share "
                f"a single ExtractorConfig; got {config.extractor_name} "
                f"and {item_config.extractor_name} in one batch"
            )
    if config is None:
        # Every item was unsupported.
        return out

    # Filter to only items with a valid config; remember their original
    # index so we can splice results back into the right slots.
    indexed_inputs: list[tuple[int, str, str, str]] = [
        (i, items[i][0], items[i][1], items[i][2])
        for i in range(len(items))
        if out[i] is not None or select_config(items[i][0]) is not None
    ]
    # The above simplifies to "all items where config is not None":
    indexed_inputs = [
        (i, items[i][0], items[i][1], items[i][2])
        for i in range(len(items))
        if select_config(items[i][0]) is not None
    ]

    # Per-paper empty-content guard.
    callable_inputs: list[tuple[int, str, str, str]] = []
    for idx, collection, source_path, content in indexed_inputs:
        if not content:
            _log.warning(
                "aspect_extractor_batch_empty_content",
                source_path=source_path,
                collection=collection,
                detail=(
                    "batch path received empty content; per-paper file "
                    "read is the worker's responsibility before invoking "
                    "this function. Recording null-fields and skipping."
                ),
            )
            out[idx] = _empty_record(source_path, collection, config)
            continue
        # Defensive null-byte strip (matches single-paper path).
        content = content.replace("\x00", "")
        callable_inputs.append((idx, collection, source_path, content))

    if not callable_inputs:
        return out

    prompt = _build_batch_prompt(callable_inputs, config)
    timeout = 180 + _BATCH_TIMEOUT_PER_EXTRA_PAPER_S * (len(callable_inputs) - 1)
    parsed = _retry_subprocess_batch(prompt, config, timeout=timeout)

    if parsed is None:
        # Whole batch failed: every paper gets a null-fields record.
        for idx, collection, source_path, _content in callable_inputs:
            out[idx] = _empty_record(source_path, collection, config)
        return out

    # Demux parsed['papers'] back into per-input slots by source_path.
    by_source = {}
    for entry in parsed.get("papers", []):
        if not isinstance(entry, dict):
            continue
        sp = entry.get("source_path")
        if isinstance(sp, str):
            by_source[sp] = entry

    for idx, collection, source_path, _content in callable_inputs:
        entry = by_source.get(source_path)
        if entry is None:
            _log.warning(
                "aspect_extractor_batch_missing_entry",
                source_path=source_path,
                detail="batch response did not include this source_path",
            )
            out[idx] = _empty_record(source_path, collection, config)
            continue
        out[idx] = _build_record_from_entry(entry, source_path, collection, config)

    return out


def _build_batch_prompt(
    callable_inputs: list[tuple[int, str, str, str]],
    config: ExtractorConfig,
) -> str:
    """Build the multi-paper prompt for a batch call.

    Header is the shared instruction; each paper is presented with a
    ``source_path`` line followed by its content, separated from
    other papers by a ``=====`` divider.
    """
    parts = [_SCHOLARLY_BATCH_PROMPT_HEADER]
    for _idx, _collection, source_path, content in callable_inputs:
        parts.append("\n=====\n")
        parts.append(f"source_path: {source_path}\n\n")
        parts.append(content)
    parts.append("\n=====\n")
    return "".join(parts)


def _retry_subprocess_batch(
    prompt: str,
    config: ExtractorConfig,
    *,
    timeout: int,
) -> dict | None:
    """Retry-wrapped batch subprocess call. Same classification as
    single-paper ``_retry_subprocess`` (transient → retry; hard →
    null-out). Returns the outer dict ``{"papers": [...]}`` on
    success, ``None`` on final failure.
    """
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return _invoke_once_batch(prompt, timeout=timeout)
        except _TransientFailure as exc:
            _log.debug(
                "aspect_extractor_batch_transient_failure",
                attempt=attempt + 1,
                attempts_remaining=_RETRY_ATTEMPTS - attempt - 1,
                error=str(exc),
                extractor=config.extractor_name,
            )
            if attempt < _RETRY_ATTEMPTS - 1:
                _sleep_with_jitter(attempt)
            continue
        except _HardFailure as exc:
            _log.warning(
                "aspect_extractor_batch_hard_failure",
                attempt=attempt + 1,
                error=str(exc),
                extractor=config.extractor_name,
            )
            return None
    _log.warning(
        "aspect_extractor_batch_retries_exhausted",
        attempts=_RETRY_ATTEMPTS,
        extractor=config.extractor_name,
    )
    return None


def _invoke_once_batch(prompt: str, *, timeout: int) -> dict:
    """Single batch subprocess invocation. Same shape as
    ``_invoke_once`` but with a configurable timeout (single-paper
    is hardcoded at 180 s; batch scales up). Prompt is stdin-fed
    for the same reason as the single-paper path.
    """
    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "json"],
            input=prompt,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise _TransientFailure(f"timeout after {timeout}s: {exc}") from exc
    except OSError as exc:
        # ENOMEM and similar boundary errors — non-retriable at this
        # prompt size. Caller may fall back to per-paper. (E2BIG is
        # no longer reachable now that prompts go via stdin.)
        raise _HardFailure(f"subprocess exec failed: {exc}") from exc

    if result.returncode != 0:
        stderr_lc = (result.stderr or "").lower()
        if any(p in stderr_lc for p in _TRANSIENT_STDERR_PATTERNS):
            raise _TransientFailure(
                f"transient stderr (rc={result.returncode}): "
                f"{(result.stderr or '')[:200]}",
            )
        raise _HardFailure(
            f"non-zero exit (rc={result.returncode}): "
            f"{(result.stderr or '')[:200]}",
        )

    try:
        outer = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise _TransientFailure(f"outer json parse failure: {exc}") from exc
    if not isinstance(outer, dict) or "result" not in outer:
        raise _HardFailure(
            "claude --output-format json wrapper missing 'result' key"
        )

    inner_text = outer["result"]
    if not isinstance(inner_text, str):
        raise _HardFailure(
            f"wrapper 'result' is not a string (got {type(inner_text).__name__})",
        )
    cleaned = _strip_code_fence(inner_text.strip())
    try:
        parsed = json.loads(cleaned)
    except (ValueError, TypeError) as exc:
        raise _TransientFailure(f"inner json parse failure: {exc}") from exc

    if not isinstance(parsed, dict):
        raise _HardFailure(
            f"inner json top-level not a dict (got {type(parsed).__name__})"
        )
    papers = parsed.get("papers")
    if not isinstance(papers, list):
        raise _HardFailure(
            "batch response missing 'papers' array"
        )

    return parsed


def _build_record_from_entry(
    entry: dict,
    source_path: str,
    collection: str,
    config: ExtractorConfig,
) -> AspectRecord:
    """Build an ``AspectRecord`` from a single batch-response entry.

    Validates required fields against the config schema. Missing
    fields trigger the same null-fields fallback as single-paper
    schema-validation failure (the per-paper failure does not
    cascade to other papers in the batch).
    """
    missing = [f for f in config.required_fields if f not in entry]
    if missing:
        _log.warning(
            "aspect_extractor_batch_entry_schema_invalid",
            missing_fields=missing,
            source_path=source_path,
        )
        return _empty_record(source_path, collection, config)

    datasets = entry.get("experimental_datasets", [])
    if not isinstance(datasets, list):
        datasets = []
    baselines = entry.get("experimental_baselines", [])
    if not isinstance(baselines, list):
        baselines = []
    extras = entry.get("extras", {})
    if not isinstance(extras, dict):
        extras = {}
    confidence = entry.get("confidence")
    if confidence is not None and not isinstance(confidence, (int, float)):
        confidence = None

    return AspectRecord(
        collection=collection,
        source_path=source_path,
        problem_formulation=_str_or_none(entry.get("problem_formulation")),
        proposed_method=_str_or_none(entry.get("proposed_method")),
        experimental_datasets=[str(x) for x in datasets],
        experimental_baselines=[str(x) for x in baselines],
        experimental_results=_str_or_none(entry.get("experimental_results")),
        extras=extras,
        confidence=float(confidence) if confidence is not None else None,
        extracted_at=datetime.now(UTC).isoformat(),
        model_version=config.model_version,
        extractor_name=config.extractor_name,
    )


# ── Subprocess invocation + retry loop ───────────────────────────────────────


def _retry_subprocess(
    prompt: str,
    config: ExtractorConfig,
) -> dict | None:
    """Invoke the Claude CLI with retry. Return the parsed JSON dict on
    success, or ``None`` after final failure.
    """
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return _invoke_once(prompt)
        except _TransientFailure as exc:
            _log.debug(
                "aspect_extractor_transient_failure",
                attempt=attempt + 1,
                attempts_remaining=_RETRY_ATTEMPTS - attempt - 1,
                error=str(exc),
                extractor=config.extractor_name,
            )
            if attempt < _RETRY_ATTEMPTS - 1:
                _sleep_with_jitter(attempt)
            continue
        except _HardFailure as exc:
            _log.warning(
                "aspect_extractor_hard_failure",
                attempt=attempt + 1,
                error=str(exc),
                extractor=config.extractor_name,
            )
            return None
    _log.warning(
        "aspect_extractor_retries_exhausted",
        attempts=_RETRY_ATTEMPTS,
        extractor=config.extractor_name,
    )
    return None


def _invoke_once(prompt: str) -> dict:
    """Single subprocess invocation. Raises ``_TransientFailure`` on
    retriable failure, ``_HardFailure`` on non-retriable. Returns the
    parsed JSON dict on success.

    Prompt body is fed via stdin rather than argv. The Claude CLI
    treats a missing positional ``[prompt]`` as a stdin read; passing
    multi-page papers as argv tripped macOS ARG_MAX (errno 7) for
    14-page documents in production. Stdin has no such limit.
    """
    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "json"],
            input=prompt,
            timeout=180,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise _TransientFailure(f"timeout after 180s: {exc}") from exc
    except OSError as exc:
        raise _HardFailure(f"subprocess exec failed: {exc}") from exc

    if result.returncode != 0:
        stderr_lc = (result.stderr or "").lower()
        if any(p in stderr_lc for p in _TRANSIENT_STDERR_PATTERNS):
            raise _TransientFailure(
                f"transient stderr (rc={result.returncode}): "
                f"{(result.stderr or '')[:200]}",
            )
        raise _HardFailure(
            f"non-zero exit (rc={result.returncode}): "
            f"{(result.stderr or '')[:200]}",
        )

    # Outer parse: --output-format json returns a session-metadata
    # wrapper of shape {"result": "<text>", "session_id": ..., ...}.
    try:
        outer = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise _TransientFailure(f"outer json parse failure: {exc}") from exc
    if not isinstance(outer, dict) or "result" not in outer:
        raise _HardFailure(
            "claude --output-format json wrapper missing 'result' key "
            f"(got keys: {list(outer.keys()) if isinstance(outer, dict) else type(outer).__name__})",
        )

    inner_text = outer["result"]
    if not isinstance(inner_text, str):
        raise _HardFailure(
            f"wrapper 'result' is not a string (got {type(inner_text).__name__})",
        )

    # Inner parse: the model's response text. Strip markdown
    # code fences defensively — the prompt asks for raw JSON but
    # Claude can emit ```json ... ``` wrapping anyway.
    cleaned = _strip_code_fence(inner_text.strip())
    try:
        parsed = json.loads(cleaned)
    except (ValueError, TypeError) as exc:
        raise _TransientFailure(f"inner json parse failure: {exc}") from exc

    if not isinstance(parsed, dict):
        # Top-level non-dict response is a schema violation — retrying
        # the same prompt cannot reshape the response.
        raise _HardFailure(
            f"inner json top-level not a dict (got {type(parsed).__name__})",
        )

    return parsed


def _strip_code_fence(s: str) -> str:
    """Strip a leading ```json (or bare ```) fence and trailing ``` if
    the model wrapped the response in a markdown code block.
    """
    if s.startswith("```"):
        # Drop the first line (the opening fence).
        lines = s.split("\n")
        if len(lines) >= 2:
            inner = "\n".join(lines[1:])
            if inner.endswith("```"):
                inner = inner[: -len("```")].rstrip()
            return inner
    return s


# ── Schema validation + record building ──────────────────────────────────────


def _build_record(
    parsed: dict,
    source_path: str,
    collection: str,
    config: ExtractorConfig,
) -> AspectRecord:
    """Validate ``parsed`` against the extractor's schema. On schema
    failure return a null-fields record (caller already invoked the
    subprocess; retrying produces the same shape so the failure is
    surfaced via null fields rather than a re-attempt).
    """
    missing = [f for f in config.required_fields if f not in parsed]
    if missing:
        _log.warning(
            "aspect_extractor_schema_validation_failed",
            missing_fields=missing,
            extractor=config.extractor_name,
            source_path=source_path,
        )
        return _empty_record(source_path, collection, config)

    # Coerce JSON-shaped fields (lists / dicts) defensively. Wrong
    # types yield empty defaults; the row is still useful for
    # downstream operators on the populated fields.
    datasets = parsed.get("experimental_datasets", [])
    if not isinstance(datasets, list):
        datasets = []
    baselines = parsed.get("experimental_baselines", [])
    if not isinstance(baselines, list):
        baselines = []
    extras = parsed.get("extras", {})
    if not isinstance(extras, dict):
        extras = {}
    confidence = parsed.get("confidence")
    if confidence is not None and not isinstance(confidence, (int, float)):
        confidence = None

    return AspectRecord(
        collection=collection,
        source_path=source_path,
        problem_formulation=_str_or_none(parsed.get("problem_formulation")),
        proposed_method=_str_or_none(parsed.get("proposed_method")),
        experimental_datasets=[str(x) for x in datasets],
        experimental_baselines=[str(x) for x in baselines],
        experimental_results=_str_or_none(parsed.get("experimental_results")),
        extras=extras,
        confidence=float(confidence) if confidence is not None else None,
        extracted_at=datetime.now(UTC).isoformat(),
        model_version=config.model_version,
        extractor_name=config.extractor_name,
        source_uri=uri_for(collection, source_path),
    )


def _empty_record(
    source_path: str,
    collection: str,
    config: ExtractorConfig,
) -> AspectRecord:
    """Build an ``AspectRecord`` with all aspect fields null/empty,
    preserving identity (collection, source_path) + extractor
    metadata (extracted_at, extractor_name, model_version) for triage.

    **RDR-096 P2.2 limitation (intentional)**: this row's shape is
    structurally identical to a pre-RDR-096 read-failure ghost row
    (all-empty aspect fields, ``extras='{}'``, ``confidence IS NULL``).
    The P2.2 DELETE migration ran once at 4.16.0 ship time; subsequent
    rows in this shape are real subprocess-side failures (timeout,
    schema validation, retry-exhausted) emitted by THIS function and
    are distinguishable from pre-RDR-096 ghosts only by the
    ``extracted_at`` timestamp (post-4.16.0 shipdate). Operators
    triaging null-field rows after 4.16.0 should treat them as
    triage-worthy events, not as legacy noise. See module docstring
    for the full going-forward writer contract.
    """
    return AspectRecord(
        collection=collection,
        source_path=source_path,
        problem_formulation=None,
        proposed_method=None,
        experimental_datasets=[],
        experimental_baselines=[],
        experimental_results=None,
        extras={},
        confidence=None,
        extracted_at=datetime.now(UTC).isoformat(),
        model_version=config.model_version,
        extractor_name=config.extractor_name,
        source_uri=uri_for(collection, source_path),
    )




def _str_or_none(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)
