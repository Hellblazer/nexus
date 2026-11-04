# SPDX-License-Identifier: AGPL-3.0-or-later
"""Entity keys: the stable identifier of one record, such as ``CUST-0018``, that appears verbatim
in other artifacts (RDR-147 Step 1, nexus-fai28).

A collection opts in by config: ``entity.collections`` maps a collection-name glob to a regex, for
example ``{"knowledge__herb-*": "CUST-\\d{4}|eid_[0-9a-f]{8}"}``. The default is empty, so no
collection has a pattern and nothing changes. The glob is matched against the full collection name
(``knowledge__herb-core__<model>__v1``), first match in config order wins.

Two readers share the compiled patterns:

* :func:`classify_entity` names a document's key: the explicit ``meta["entity_key"]`` if it holds
  one, else the title when it matches the collection's pattern IN FULL.
* :func:`find_key_tokens` finds the keys named in evidence text, whole tokens only.

Every ``code__`` collection is excluded from both: a code title is ``rel_path:Lstart-Lend``, never a
key. The key is stamped into the catalog document's ``meta`` and nowhere else (never chunk
metadata, RDR-236); the stamp is :func:`resolve_put_key`'s, called by ``put_note``.

The module is light on purpose (``re`` and ``fnmatch`` only at module level): the Step 4 repair and
a Step 2 linker call it on hot paths.
"""
from __future__ import annotations

import fnmatch
import functools
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

#: The ``meta`` key a document's entity key lives under.
ENTITY_KEY_META = "entity_key"

#: The longest explicit entity key a put accepts, and the characters it refuses (any control
#: character, newline included): a key is an identifier read back out of text, never prose.
MAX_ENTITY_KEY_LEN = 256

#: A key is a whole token: no word character directly before or after it. ``CUST-00180`` holds no
#: ``CUST-0018`` key, and ``xCUST-0018`` holds none either.
_TOKEN_BEFORE = r"(?<!\w)"
_TOKEN_AFTER = r"(?!\w)"


class EntityConfigError(ValueError):
    """``entity.collections`` holds something that is not a glob-to-regex map, or a regex that does
    not compile. The message names the key (``entity.collections["<glob>"]``)."""


class EntityKeyError(ValueError):
    """An explicit ``entity_key`` the put refuses (:func:`check_explicit_key`), raised before any
    state is touched."""


def is_code_collection(collection: str) -> bool:
    """True for a ``code__`` collection, which has no entity keys. The prefix table is
    ``metadata_schema``'s, the one the chunk writer reads (a name-prefix dispatch, not a parse of
    the name's segments: the collection may be new and have no catalog row yet)."""
    from nexus.metadata_schema import chunk_content_type_for_collection  # noqa: PLC0415 — deferred: keep this module's import light

    return chunk_content_type_for_collection(collection or "") == "code"


def _compile_all(items: tuple[tuple[str, str], ...]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    out: list[tuple[str, re.Pattern[str]]] = []
    for glob, pattern in items:
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise EntityConfigError(
                f'entity.collections["{glob}"] is not a valid regex: {exc} (pattern {pattern!r})'
            ) from exc
        # The token form wraps the pattern in a group, which a pattern-wide inline flag such as
        # ``(?i)`` cannot sit inside: refuse it here, at load, not at the first search.
        try:
            re.compile(f"{_TOKEN_BEFORE}(?:{pattern}){_TOKEN_AFTER}", compiled.flags)
        except re.error as exc:
            raise EntityConfigError(
                f'entity.collections["{glob}"] cannot be used as a whole-token pattern: {exc} '
                f"(pattern {pattern!r}). A pattern-wide inline flag must be scoped to the group: "
                "write (?i:...) rather than (?i)..."
            ) from exc
        out.append((glob, compiled))
    return tuple(out)


@functools.lru_cache(maxsize=32)
def _compiled(items: tuple[tuple[str, str], ...]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    return _compile_all(items)


def _items(collections: object) -> tuple[tuple[str, str], ...]:
    if collections is None:
        return ()
    if not isinstance(collections, Mapping):
        raise EntityConfigError(
            f"entity.collections must be a map of collection glob to regex, got {type(collections).__name__}")
    items: list[tuple[str, str]] = []
    for glob, pattern in collections.items():
        if not isinstance(glob, str) or not isinstance(pattern, str) or not pattern:
            raise EntityConfigError(
                f'entity.collections["{glob}"] must be a non-empty regex string, got {pattern!r}')
        items.append((glob, pattern))
    return tuple(items)


def validate_entity_config(config: Mapping[str, Any], source: str = "") -> None:
    """Raise :class:`EntityConfigError` when *config*'s ``entity.collections`` is malformed.
    ``load_config`` calls it on each config file before the merge (*source* is the file's path, so
    the message names the file the bad entry came from) and once on the merged result. Compiles once
    per distinct map (the cache is keyed on the map's items, so every later read is a lookup). Only a
    missing or ``None`` ``collections`` is "nothing configured"; ``[]``, ``""``, ``0`` and ``false``
    are not maps and raise."""
    try:
        entity = config.get("entity")
        if entity is None:
            entity = {}
        if not isinstance(entity, Mapping):
            raise EntityConfigError(f"entity must be a map, got {type(entity).__name__}")
        _compiled(_items(entity.get("collections")))
    except EntityConfigError as exc:
        if source:
            raise EntityConfigError(f"{source}: {exc}") from exc
        raise


def pattern_for_collection(collection: str, config: Mapping[str, Any] | None = None) -> re.Pattern[str] | None:
    """The entity-key regex configured for *collection* (the first glob that matches its full
    name), or ``None``: no glob matches, nothing is configured, or *collection* is a ``code__``
    collection even when a glob matches it. *config* defaults to ``load_config()``."""
    if not collection or is_code_collection(collection):
        return None
    if config is None:
        from nexus.config import load_config  # noqa: PLC0415 — config imports this module lazily; keep the cycle out of module load

        config = load_config()
    entity = config.get("entity") or {}
    items = _items(entity.get("collections") if isinstance(entity, Mapping) else None)
    if not items:
        return None
    for glob, pattern in _compiled(items):
        if fnmatch.fnmatchcase(collection, glob):
            return pattern
    return None


def _field(doc: Any, name: str) -> Any:
    return doc.get(name) if isinstance(doc, Mapping) else getattr(doc, name, None)


def classify_entity(doc: Any, config: Mapping[str, Any] | None = None) -> str | None:
    """The entity key of *doc* (a catalog entry or a mapping with ``title``, ``meta`` and
    ``physical_collection``), or ``None``.

    An explicit ``meta["entity_key"]`` wins. Otherwise, when the document's collection has a pattern
    and the WHOLE title matches it, the title is the key. ``Customer CUST-0018`` is not a key;
    ``CUST-0018`` is. A ``code__`` collection has none, whatever its meta says."""
    collection = _field(doc, "physical_collection") or ""
    if is_code_collection(collection):
        return None
    meta = _field(doc, "meta") or {}
    explicit = meta.get(ENTITY_KEY_META) if isinstance(meta, Mapping) else None
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    title = _field(doc, "title") or ""
    if not title or not collection:
        return None
    pattern = pattern_for_collection(collection, config)
    if pattern is not None and pattern.fullmatch(title):
        return title
    return None


def find_key_tokens(text: str, collection: str, config: Mapping[str, Any] | None = None) -> list[str]:
    """The distinct entity keys named in *text*, in first-seen order. Whole tokens only:
    ``CUST-00180`` yields nothing for ``CUST-\\d{4}``. ``[]`` when *collection* has no pattern or is
    a ``code__`` collection."""
    pattern = pattern_for_collection(collection, config)
    if pattern is None or not text:
        return []
    token = _token_pattern(pattern)
    seen: dict[str, None] = {}
    for match in token.finditer(text):
        seen.setdefault(match.group(0), None)
    return list(seen)


@functools.lru_cache(maxsize=32)
def _token_pattern(pattern: re.Pattern[str]) -> re.Pattern[str]:
    return re.compile(f"{_TOKEN_BEFORE}(?:{pattern.pattern}){_TOKEN_AFTER}", pattern.flags)


def check_explicit_key(collection: str, explicit: str) -> None:
    """Refuse an explicit entity key a put cannot honour, before it touches any state: a control
    character (newline included), a key longer than :data:`MAX_ENTITY_KEY_LEN`, or any key on a
    ``code__`` collection (it has no keys, so the key would be dropped without a word). An unset or
    all-whitespace key is "none given". Raises :class:`EntityKeyError` (a ``ValueError``)."""
    if not explicit or not explicit.strip():
        return
    if any(unicodedata.category(ch) == "Cc" for ch in explicit):
        raise EntityKeyError("entity_key must not contain control characters (newline included)")
    if len(explicit.strip()) > MAX_ENTITY_KEY_LEN:
        raise EntityKeyError(f"entity_key is longer than {MAX_ENTITY_KEY_LEN} characters")
    if is_code_collection(collection):
        raise EntityKeyError(
            f"entity_key is not allowed on a code collection ({collection}): code__ collections have "
            "no entity keys, so the key would be dropped. Store the document in a knowledge, docs or "
            "rdr collection, or leave the key off.")


def resolve_put_key(*, collection: str, title: str, explicit: str = "") -> str:
    """The key a ``store_put`` stamps into the document's catalog ``meta``: the caller's
    *explicit* key, else the title when the collection's pattern matches it in full, else ``""``
    (nothing to stamp). The one rule of :func:`classify_entity`, so a put and a later read agree.

    An explicit key that :func:`check_explicit_key` refuses raises ``ValueError`` here, before the
    caller has registered or written anything. A title-derived key on a ``code__`` collection stays
    silent: nothing was asked for."""
    check_explicit_key(collection, explicit)
    key = classify_entity({
        "physical_collection": collection, "title": title, "meta": {ENTITY_KEY_META: explicit},
    })
    return key or ""
