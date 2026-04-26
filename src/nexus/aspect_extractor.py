# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Synchronous structured-aspect extractor (RDR-089 Phase 1.2).

Single public entrypoint: ``extract_aspects(content, source_path,
collection) -> AspectRecord | None``. The function is synchronous top
to bottom — no ``async def``, no ``await``, no event loop. The
RDR-089 load-bearing contract requires this so the document-grain
hook chain (``fire_post_document_hooks``) can call it from a sync
dispatcher without dropping a coroutine.

Phase 1 ships exactly one extractor config — ``scholarly-paper-v1``,
keyed on the ``knowledge__*`` collection prefix. Other prefixes
return ``None`` from ``extract_aspects``; the calling hook should
no-op on ``None``.

Invocation: ``subprocess.run(["claude", "-p", PROMPT, "--json"],
timeout=180, capture_output=True, text=True)``. The Claude CLI is
expected to be on PATH; if it is not, ``subprocess.run`` raises
``FileNotFoundError`` which propagates (configuration error, not a
runtime extraction failure).

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
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import structlog

from nexus.db.t2.document_aspects import AspectRecord

# Re-exported for ergonomic ``from nexus.aspect_extractor import
# AspectRecord``. Phase 2 hook authors land here first; the
# canonical definition stays with the store.
__all__ = ["AspectRecord", "ExtractorConfig", "extract_aspects", "select_config"]

_log = structlog.get_logger(__name__)


# ── Extractor config ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExtractorConfig:
    """A collection-prefix-keyed extractor recipe.

    ``prompt_template`` is interpolated at call time with the
    document content. ``required_fields`` is the list of keys that
    must be present in the parsed JSON for the response to validate;
    missing or wrong-typed fields trigger the non-retriable
    schema-validation path (the Claude CLI cannot make the response
    shape correct by retrying with the same prompt).
    """

    extractor_name: str
    model_version: str
    prompt_template: str
    required_fields: tuple[str, ...]


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


_REGISTRY: dict[str, ExtractorConfig] = {
    "knowledge__": ExtractorConfig(
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
    ),
}


def select_config(collection: str) -> ExtractorConfig | None:
    """Return the registered ``ExtractorConfig`` whose prefix matches
    ``collection``, or ``None`` if no prefix matches.

    Phase 1 ships only the ``knowledge__`` prefix; any other
    collection (including ``docs__``, ``code__``, ``rdr__``, and
    bare ``knowledge`` without the double-underscore separator)
    returns ``None``.
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


# ── Extraction core ──────────────────────────────────────────────────────────


def extract_aspects(
    content: str,
    source_path: str,
    collection: str,
) -> AspectRecord | None:
    """Synchronously extract aspects from ``content`` and return a
    populated ``AspectRecord``, or ``None`` when ``collection`` has no
    registered extractor.

    Content-sourcing contract (RDR-089 P0.1 / audit F4):

    * ``content`` non-empty → used directly.
    * ``content == ""`` → read ``source_path`` from disk.
    * Read failure (missing or unreadable file) → null-fields
      record returned without invoking the extractor subprocess.

    On unsupported collection (no config match) returns ``None``;
    the caller (post-store hook) should no-op.

    On extractor failure (any failure path past content sourcing),
    returns an ``AspectRecord`` with aspect fields null and
    ``confidence=None``; the row remains queryable for triage.
    """
    config = select_config(collection)
    if config is None:
        return None

    if not content:
        try:
            content = Path(source_path).read_text(
                encoding="utf-8", errors="replace",
            )
        except (OSError, UnicodeDecodeError) as exc:
            _log.warning(
                "aspect_extractor_source_path_unreadable",
                source_path=source_path,
                error=str(exc),
            )
            return _empty_record(source_path, collection, config)

    prompt = config.prompt_template.format(content=content)
    parsed = _retry_subprocess(prompt, config)
    if parsed is None:
        return _empty_record(source_path, collection, config)

    return _build_record(parsed, source_path, collection, config)


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
    """
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--json"],
            timeout=180,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise _TransientFailure(f"timeout after 180s: {exc}") from exc

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
        parsed = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise _TransientFailure(f"json parse failure: {exc}") from exc

    if not isinstance(parsed, dict):
        # Top-level non-dict response is a schema violation — retrying
        # the same prompt cannot reshape the response.
        raise _HardFailure(
            f"json top-level not a dict (got {type(parsed).__name__})",
        )

    return parsed


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
    )


def _empty_record(
    source_path: str,
    collection: str,
    config: ExtractorConfig,
) -> AspectRecord:
    """Build an ``AspectRecord`` with all aspect fields null/empty,
    preserving identity (collection, source_path) + extractor
    metadata (extracted_at, extractor_name, model_version) for triage.
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
    )


def _str_or_none(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)
