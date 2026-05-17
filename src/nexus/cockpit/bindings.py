# SPDX-License-Identifier: AGPL-3.0-or-later
"""_BindingWatcher -- reaction loop for the cockpit (RDR-111 §Phase 2, nexus-0xaq).

The hook-bridge layer (``nexus.cockpit.hook_bridge``) drains Claude Code
hook events into the tuplespace, but tuples sitting in ``tuples.db`` do
nothing on their own -- the cockpit needs a *reaction loop* that watches
committed events and dispatches user-declared bindings. This module is
that loop.

Design summary
--------------

- **Event source**: the ``events`` table in ``tuples.db`` (see RDR-110
  store.py -- the table is fed by ``trg_tuples_out`` and ``trg_claim_log_event``
  on every committed tuple operation). Each row has a monotonic ``rowid``
  used as the watcher cursor.

- **Cursor persistence**: ``watcher_state`` table, keyed by
  ``(subspace, profile)``. The watcher persists ``last_rowid`` after every
  batch so a restart resumes at-least-once without replaying the whole
  events table.

- **Idempotency / dedup**: rowid is strictly monotonic. The watcher only
  fetches ``rowid > last_rowid`` and advances ``last_rowid`` to the max
  rowid in the batch. Reprocessing the same event is impossible within a
  single watcher process. Across processes the cursor table guarantees
  the same property after a restart.

- **Concurrency**: single asyncio task. SQL work is dispatched via
  ``asyncio.to_thread`` to keep the loop non-blocking. Actions are
  invoked sequentially per event in cursor order; one slow action
  briefly holds up its successors but does not affect other watchers
  (each subspace gets its own task).

- **Error containment**: each binding action is wrapped in
  ``try/except Exception``. A raising action is logged with structured
  context and skipped; the watcher continues with the next binding and
  the next event. One bad binding does not crash the loop or starve
  other bindings.

- **EventStream RPC fallback**: this implementation polls the ``events``
  table directly. The daemon-mode ``event_stream.subscribe`` RPC
  (nexus-m4gm) is the future upgrade path -- when it ships, this module
  should grow a ``transport`` parameter and prefer the RPC. Until then,
  polling the same table the RPC reads from gives bit-identical
  semantics.

Limits
------

- ``match`` is a flat dict of field equality predicates against the
  event record. No regex, no dotted access, no boolean composition.
  Listed as a PR follow-up; v1 keeps surface area small.

- ``action`` is one of: ``python:<dotted.module:func>`` (called with
  ``(event, binding, context)``) or ``log:<marker>`` (emits a single
  structlog event with that marker). No shell-out by design.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import importlib
import os
import re as _re
import sqlite3
import time
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Callable, Optional  # noqa: F401  -- Callable used in type hints

import structlog
import yaml

# nexus-uf3w (S360-uni S1): SR-1 profile-name allowlist promoted from
# bindings_crud._VALID_PROFILE_NAME_RE so load_profile can enforce it
# too. The CRUD copy is kept as a thin re-export so callers that
# imported the symbol via the old module still resolve it.
_VALID_PROFILE_NAME_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BindingProfileError(ValueError):
    """Raised when a binding profile YAML fails validation."""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Action:
    """Validated action descriptor (kind + payload).

    ``kind`` is currently one of ``"python"`` or ``"log"``. ``target`` is
    the dotted callable for ``python`` or the marker string for ``log``.
    """

    kind: str
    target: str


@dataclasses.dataclass(frozen=True)
class Binding:
    """One binding row: when ``match`` is satisfied, fire ``action``.

    nexus-7lb9: ``enabled`` defaults to True; disabled bindings are
    loaded into the watcher's profile list but skipped at dispatch
    time. CRUD via :func:`nexus.cockpit.bindings_crud.toggle_binding`
    flips this flag on the YAML file; the watcher's mtime-poll reload
    picks the change up on the next tick.
    """

    name: str
    match: dict[str, Any]
    action: Action
    enabled: bool = True


#: nexus-26b7 (notable, dim-13 N-3): bump-on-incompatible-change so a
#: newer wheel can detect a profile authored against a future format
#: and refuse cleanly. Currently 1 (additive-only since arc landing).
BINDING_PROFILE_SCHEMA_VERSION: int = 1


@dataclasses.dataclass(frozen=True)
class BindingProfile:
    """A named bundle of bindings loaded from one YAML file."""

    name: str
    bindings: tuple[Binding, ...]
    schema_version: int = BINDING_PROFILE_SCHEMA_VERSION


@dataclasses.dataclass(frozen=True)
class EventRecord:
    """One row out of the ``events`` table -- what bindings match against."""

    cursor: int
    subspace: str
    op: str
    tuple_id: str
    payload_summary: Optional[str]
    category: Optional[str]
    ts: float

    def as_match_target(self) -> dict[str, Any]:
        """Return a plain dict for predicate evaluation."""
        return {
            "subspace": self.subspace,
            "op": self.op,
            "tuple_id": self.tuple_id,
            "category": self.category,
        }


# ---------------------------------------------------------------------------
# Profile loading
# ---------------------------------------------------------------------------


def _validate_match(match: Any, *, source: str) -> dict[str, Any]:
    if not isinstance(match, dict):
        raise BindingProfileError(
            f"{source}: 'match' must be a mapping, got {type(match).__name__}"
        )
    bad = [k for k in match if not isinstance(k, str)]
    if bad:
        raise BindingProfileError(f"{source}: non-string match keys: {bad!r}")
    return dict(match)


def _validate_action(action: Any, *, source: str) -> Action:
    if not isinstance(action, dict):
        raise BindingProfileError(
            f"{source}: 'action' must be a mapping, got {type(action).__name__}"
        )
    kind = action.get("kind")
    if kind == "python":
        callable_ref = action.get("callable")
        if not isinstance(callable_ref, str) or ":" not in callable_ref:
            raise BindingProfileError(
                f"{source}: python action requires 'callable: module.path:func'"
            )
        return Action(kind="python", target=callable_ref)
    if kind == "log":
        marker = action.get("marker")
        if not isinstance(marker, str) or not marker:
            raise BindingProfileError(
                f"{source}: log action requires non-empty 'marker' string"
            )
        return Action(kind="log", target=marker)
    raise BindingProfileError(
        f"{source}: unknown action kind {kind!r} (expected 'python' or 'log')"
    )


def load_profile(path: Path) -> BindingProfile:
    """Parse a binding-profile YAML file, raising on malformed input."""
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise BindingProfileError(f"{path.name}: malformed YAML -- {exc}") from exc

    if not isinstance(raw, dict):
        raise BindingProfileError(
            f"{path.name}: top-level YAML must be a mapping, "
            f"got {type(raw).__name__}"
        )

    name = raw.get("profile")
    if not isinstance(name, str) or not name:
        raise BindingProfileError(f"{path.name}: missing/empty 'profile' field")

    # nexus-uf3w (S360-uni S1): enforce the SR-1 allowlist on the
    # LOAD path too, not just the CRUD-create entry point. A
    # same-UID agent that drops a YAML directly into the profiles
    # dir would otherwise smuggle an arbitrary `profile:` value
    # into the derived subspace (line 463-465).
    if not _VALID_PROFILE_NAME_RE.fullmatch(name):
        raise BindingProfileError(
            f"{path.name}: invalid 'profile' value {name!r}; must "
            f"match ^[A-Za-z0-9][A-Za-z0-9_-]*$ (SR-1 allowlist)."
        )

    # nexus-26b7 (notable, dim-13 N-3): refuse YAMLs whose
    # schema_version is newer than the wheel supports. Older YAMLs
    # without the field are accepted at the current default to
    # preserve backward-compat.
    schema_version_raw = raw.get("schema_version", BINDING_PROFILE_SCHEMA_VERSION)
    if not isinstance(schema_version_raw, int):
        raise BindingProfileError(
            f"{path.name}: 'schema_version' must be int, "
            f"got {type(schema_version_raw).__name__}"
        )
    if schema_version_raw > BINDING_PROFILE_SCHEMA_VERSION:
        raise BindingProfileError(
            f"{path.name}: schema_version={schema_version_raw} is newer "
            f"than this wheel supports (max {BINDING_PROFILE_SCHEMA_VERSION}); "
            "upgrade conexus."
        )

    raw_bindings = raw.get("bindings")
    if not isinstance(raw_bindings, list):
        raise BindingProfileError(
            f"{path.name}: 'bindings' must be a list, got "
            f"{type(raw_bindings).__name__}"
        )

    bindings: list[Binding] = []
    seen_names: set[str] = set()
    for i, b in enumerate(raw_bindings):
        if not isinstance(b, dict):
            raise BindingProfileError(
                f"{path.name}: binding[{i}] must be a mapping"
            )
        bname = b.get("name")
        if not isinstance(bname, str) or not bname:
            raise BindingProfileError(
                f"{path.name}: binding[{i}] missing/empty 'name'"
            )
        # nexus-uf3w (S360-uni S2): NFC normalise so a binding named
        # 'café' (NFC) and one named 'café' (NFD) collide as expected.
        # macOS HFS+ round-trips filenames through NFD; without this
        # the dedup check sees two distinct strings.
        bname_norm = unicodedata.normalize("NFC", bname)
        if bname_norm in seen_names:
            raise BindingProfileError(
                f"{path.name}: duplicate binding name {bname_norm!r}"
            )
        seen_names.add(bname_norm)
        source = f"{path.name}:{bname_norm}"
        match = _validate_match(b.get("match"), source=source)
        action = _validate_action(b.get("action"), source=source)
        enabled_raw = b.get("enabled", True)
        if not isinstance(enabled_raw, bool):
            raise BindingProfileError(
                f"{source}: 'enabled' must be a bool, "
                f"got {type(enabled_raw).__name__}"
            )
        bindings.append(
            Binding(
                name=bname_norm,
                match=match,
                action=action,
                enabled=enabled_raw,
            )
        )

    return BindingProfile(
        name=name,
        bindings=tuple(bindings),
        schema_version=schema_version_raw,
    )


def load_profiles_dir(profiles_dir: Path) -> list[BindingProfile]:
    """Load all ``*.yml`` profiles in *profiles_dir* (non-recursive)."""
    if not profiles_dir.is_dir():
        return []
    profiles: list[BindingProfile] = []
    for yml in sorted(profiles_dir.glob("*.yml")):
        profiles.append(load_profile(yml))
    return profiles


# ---------------------------------------------------------------------------
# Match-predicate evaluation
# ---------------------------------------------------------------------------


def matches(event: EventRecord, predicate: dict[str, Any]) -> bool:
    """Return True iff every key in *predicate* equals the same field on *event*.

    Empty predicate matches everything (deliberate; lets a profile express
    a "fire on every event" binding). Unknown keys never match.
    """
    target = event.as_match_target()
    for k, v in predicate.items():
        if target.get(k) != v:
            return False
    return True


# ---------------------------------------------------------------------------
# Action dispatch
# ---------------------------------------------------------------------------


# nexus-6m9i (third 360° SEC-1): module-path allowlist for python
# action callables. The binding YAML's ``action.callable`` field
# resolves to ``importlib.import_module(mod_name)`` + getattr — a
# same-UID agent that drops a YAML into ~/.config/nexus/bindings/
# profiles/ could otherwise specify ``os:system`` or any installed
# package callable, and the daemon's 50ms hot-reload would import it
# (and its top-level side effects) within a tick.
#
# The allowlist accepts two namespace prefixes by default plus any
# extras the operator opts in via ``NX_BINDING_CALLABLE_NAMESPACES``
# (comma-separated, e.g. ``my_pkg.callbacks,vendor_lib.cb``).
_DEFAULT_CALLABLE_NAMESPACES: tuple[str, ...] = (
    "nexus.",
    "nexus_plugins.",
    # Synthetic test stubs (tests/cockpit/test_binding_watcher.py,
    # tests/cockpit/test_bindings_crud.py): modules named
    # ``nexus_test_*`` or ``_test_*`` registered in sys.modules by
    # the test harness OR resolved through the ``tests.`` package
    # path. Production users are unlikely to ship a binding YAML
    # pointing at such a module, but the test fixtures need them
    # resolvable without an env override.
    "nexus_test_",
    "_test_",
    "tests.",
)


class CallableNotAllowed(BindingProfileError):
    """Raised when an action callable's module is outside the allowlist.

    nexus-6m9i (third 360° SEC-1).
    """


def _allowed_callable_namespaces() -> tuple[str, ...]:
    extra = os.environ.get("NX_BINDING_CALLABLE_NAMESPACES", "").strip()
    if not extra:
        return _DEFAULT_CALLABLE_NAMESPACES
    extras = tuple(
        p.strip() + ("" if p.strip().endswith(".") else ".")
        for p in extra.split(",")
        if p.strip()
    )
    return _DEFAULT_CALLABLE_NAMESPACES + extras


def _resolve_python_callable(target: str) -> Callable[..., Any]:
    mod_name, _, attr = target.partition(":")
    if not mod_name or not attr:
        raise BindingProfileError(
            f"python action target {target!r} must be of the form "
            "'module.path:func'"
        )
    namespaces = _allowed_callable_namespaces()
    if not any(
        mod_name == ns.rstrip(".") or mod_name.startswith(ns)
        for ns in namespaces
    ):
        raise CallableNotAllowed(
            f"python action target {target!r} resolves to module "
            f"{mod_name!r} which is outside the allowlist "
            f"({', '.join(namespaces)}). Set NX_BINDING_CALLABLE_"
            "NAMESPACES to extend the allowlist."
        )
    mod = importlib.import_module(mod_name)
    fn = getattr(mod, attr, None)
    if not callable(fn):
        raise BindingProfileError(
            f"python action target {target!r} is not callable"
        )
    return fn


#: Default TTL on the action_idempotency rows. RDR-111:933.
_IDEMPOTENCY_TTL_SECONDS = 300.0


# nexus-9pkn (S360-time S1): wall-clock floor that never moves
# backwards. A backward NTP correction or sleep/wake jump would
# otherwise shorten the dedup window and admit replay of an already-
# dispatched side-effecting action. Tracked in-process; cross-process
# safety still relies on the daemon's periodic
# ``sweep_action_idempotency`` and reasonably-bounded NTP slews.
import threading as _threading_idempotency  # noqa: PLC0415

_idempotency_clock_lock = _threading_idempotency.Lock()
_idempotency_clock_floor: float = 0.0


def _idempotency_now() -> float:
    """Wall-clock time, clamped against backward movement.

    Returns the larger of ``time.time()`` and the largest value
    previously observed by this helper. Subsequent callers see at
    least the clamped value.
    """
    global _idempotency_clock_floor
    with _idempotency_clock_lock:
        now = max(time.time(), _idempotency_clock_floor)
        _idempotency_clock_floor = now
        return now


def _idempotency_key(binding_name: str, tuple_id: str) -> str:
    """Deterministic key for the ``action_idempotency`` table.

    RDR-111 lines 909-942: ``sha256(binding_name + tuple_id)``. The key
    is per-(binding, event) so two different bindings on the same event
    each get their own dedup row, but a watcher restart that replays
    the same event for the same binding hits the dedup hit and skips.
    """
    raw = f"{binding_name}:{tuple_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _idempotency_check(
    memory_conn: sqlite3.Connection, *, key: str, now: float
) -> bool:
    """Return True if a non-expired key already exists for this action."""
    row = memory_conn.execute(
        "SELECT 1 FROM action_idempotency "
        "WHERE idempotency_key = ? AND expires_at > ? LIMIT 1",
        (key, now),
    ).fetchone()
    return row is not None


def _idempotency_record(
    memory_conn: sqlite3.Connection, *, key: str, expires_at: float
) -> None:
    """Insert (or refresh) the dedup row for a successfully dispatched action."""
    memory_conn.execute(
        "INSERT INTO action_idempotency (idempotency_key, expires_at) "
        "VALUES (?, ?) "
        "ON CONFLICT(idempotency_key) DO UPDATE SET expires_at = excluded.expires_at",
        (key, expires_at),
    )
    memory_conn.commit()


async def _dispatch_action(
    action: Action,
    *,
    event: EventRecord,
    binding: Binding,
    context: "BindingContext",
) -> None:
    if action.kind == "log":
        # Log actions are inherently idempotent, a second log line on
        # crash-replay is harmless, and the dedup gate would only waste a
        # SELECT round-trip per event.
        _log.info(
            "binding_action_log",
            marker=action.target,
            binding=binding.name,
            subspace=event.subspace,
            op=event.op,
            tuple_id=event.tuple_id,
        )
        return
    if action.kind == "python":
        # RDR-111:909-942 dedup gate. Only applied when memory_conn is
        # wired (production daemon path). Tests that pass a stub context
        # without memory_conn keep the legacy at-least-once semantics.
        if context.memory_conn is not None:
            key = _idempotency_key(binding.name, event.tuple_id)
            # nexus-9pkn (S360-time S1): clamped wall clock.
            now = _idempotency_now()
            try:
                if _idempotency_check(context.memory_conn, key=key, now=now):
                    _log.info(
                        "binding_action_dedup_skip",
                        binding=binding.name,
                        subspace=event.subspace,
                        tuple_id=event.tuple_id,
                    )
                    return
            except sqlite3.Error:
                # Table missing or other persistent error, fall through to
                # dispatch (degrade-open). Without the table, replays cannot
                # be filtered, but skipping the action is the worse failure
                # mode (silent loss of side effect).
                _log.exception(
                    "binding_action_idempotency_check_failed",
                    binding=binding.name,
                    tuple_id=event.tuple_id,
                )

        fn = _resolve_python_callable(action.target)
        result = fn(event, binding, context)
        if asyncio.iscoroutine(result):
            await result

        if context.memory_conn is not None:
            try:
                _idempotency_record(
                    context.memory_conn,
                    key=_idempotency_key(binding.name, event.tuple_id),
                    # nexus-9pkn (S360-time S1): clamped wall clock.
                    expires_at=_idempotency_now() + _IDEMPOTENCY_TTL_SECONDS,
                )
            except sqlite3.Error:
                _log.exception(
                    "binding_action_idempotency_record_failed",
                    binding=binding.name,
                    tuple_id=event.tuple_id,
                )
        return
    raise BindingProfileError(f"unknown action kind {action.kind!r}")


# ---------------------------------------------------------------------------
# Context passed to python actions
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class BindingContext:
    """Mutable context object passed to python action callables.

    Holds the tuplespace handles an action might need to write derived
    tuples back into the system. Tests inject a stub object; production
    wiring constructs one in :meth:`_BindingWatcher.start`.

    ``memory_conn`` is the optional ``memory.db`` connection used for
    the ``action_idempotency`` dedup gate (RDR-111 §lines 909-942,
    nexus-8wvs). When ``None`` the watcher runs without the dedup gate
   , tests that inject a stub context exercise this path. Production
    wiring (the daemon) populates it so a crash between dispatch and
    cursor save cannot re-fire a python action on restart.
    """

    conn: Optional[sqlite3.Connection] = None
    index: Any = None  # nexus.tuplespace.index.TupleIndex
    registry: Any = None  # nexus.tuplespace.registry.Registry
    profile_name: str = ""
    memory_conn: Optional[sqlite3.Connection] = None
    # Free-form bag so reference actions and tests can stash arbitrary
    # state without growing the dataclass for every consumer.
    extras: dict[str, Any] = dataclasses.field(default_factory=dict)


# ---------------------------------------------------------------------------
# Reference action callables (the "is this primitive useful?" demonstration)
# ---------------------------------------------------------------------------


def action_emit_derived(
    event: EventRecord,
    binding: Binding,
    context: BindingContext,
) -> None:
    """Reference binding action: in-tuplespace reaction.

    On a PostToolUse 'out' event, writes a derived tuple onto
    ``derived/<profile>`` recording the source event. This is the
    canonical "one event in, a different event out" demonstration.
    """
    if context.conn is None or context.index is None or context.registry is None:
        _log.warning(
            "binding_action_emit_derived_missing_context",
            binding=binding.name,
        )
        return
    from nexus.tuplespace.api import out

    profile = context.profile_name or "default"
    subspace = f"derived/{profile}"
    dimensions = {
        "profile": profile,
        "source_op": event.op,
        "source_sub": event.subspace,
        "tuple_id": event.tuple_id,
    }
    # nexus-26b7 (notable, dim-10 U-3): NFC-normalise the derived
    # match_text so an NFD-form subspace (Mac HFS+) does not produce
    # a tuple_id that differs from its NFC twin elsewhere.
    match_text = unicodedata.normalize(
        "NFC", f"{event.subspace} {event.op} {event.tuple_id}"
    )
    out(
        conn=context.conn,
        index=context.index,
        registry=context.registry,
        subspace=subspace,
        content=match_text,
        dimensions=dimensions,
        match_text=match_text,
    )


def action_log_marker(
    event: EventRecord,
    binding: Binding,
    context: BindingContext,
) -> None:
    """Reference binding action: side effect via structlog.

    Emits a structlog event with a stable marker key so a downstream
    log consumer (or a test) can detect the reaction. No subprocess, no
    network.
    """
    _log.info(
        "binding_marker",
        marker="cockpit.binding.notification",
        binding=binding.name,
        subspace=event.subspace,
        op=event.op,
        tuple_id=event.tuple_id,
    )


# ---------------------------------------------------------------------------
# Cursor + event-table SQL helpers (run via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _load_cursor(
    conn: sqlite3.Connection, *, subspace_prefix: str, profile: str
) -> int:
    row = conn.execute(
        "SELECT last_rowid FROM watcher_state WHERE subspace = ? AND profile = ?",
        (subspace_prefix, profile),
    ).fetchone()
    if row is None:
        return 0
    return int(row[0])


def _save_cursor(
    conn: sqlite3.Connection,
    *,
    subspace_prefix: str,
    profile: str,
    last_rowid: int,
) -> None:
    conn.execute(
        "INSERT INTO watcher_state (subspace, profile, last_rowid, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(subspace, profile) DO UPDATE SET "
        "last_rowid = excluded.last_rowid, updated_at = excluded.updated_at",
        (subspace_prefix, profile, last_rowid, time.time()),
    )
    conn.commit()


_GLOB_SPECIAL_CHARS = frozenset("*?[")


def _glob_to_prefix_range(subspace_glob: str) -> tuple[str, str] | None:
    """Return ``(lo, hi)`` so ``lo <= subspace < hi`` matches ``prefix*``.

    Returns ``None`` when ``subspace_glob`` is not a pure-prefix shape
    (e.g. ``*``, ``[abc]*``, ``foo?bar``); callers fall back to GLOB.

    For ``"tasks/*"`` returns ``("tasks/", "tasks0")``: ``"0"`` is the
    next ASCII byte after ``"/"`` (``0x30`` > ``0x2F``), and SQLite's
    default ``BINARY`` collation compares bytewise so the half-open range
    covers exactly the rows starting with ``"tasks/"``. We append a
    high-codepoint byte (``"￿"``) for prefixes that don't end in a
    safely-incrementable ASCII byte, giving SQLite something concrete to
    range-scan against rather than rebuilding the alphabet boundary.
    """
    if not subspace_glob.endswith("*"):
        return None
    prefix = subspace_glob[:-1]
    # Reject anything with glob metacharacters inside the prefix.
    if any(c in _GLOB_SPECIAL_CHARS for c in prefix):
        return None
    if not prefix:
        return None
    # Compute the smallest string strictly greater than every ``prefix*``
    # match. nexus-2kld.4 (HR-4, 2026-05-17): use ``"\U0010FFFF"``
    # (last valid Unicode codepoint, UTF-8: 0xF4 0x8F 0xBF 0xBF) rather
    # than the prior ``"￿"`` (U+FFFF, UTF-8: 0xEF 0xBF 0xBF) which
    # sits BELOW any supplementary-plane byte sequence (those start
    # at 0xF0+). A glob like ``"tasks/*"`` against a subspace such as
    # ``"tasks/🔥"`` silently missed the row under the old sentinel
    # since SQLite TEXT uses bytewise BINARY collation.
    hi = prefix + "\U0010FFFF"
    return prefix, hi


def _build_fetch_event_batch_sql(
    *,
    subspace_glob: str,
    after_rowid: int,
    limit: int,
) -> tuple[str, tuple]:
    """Pick the right SQL + params for ``_fetch_event_batch``.

    Returns ``(sql, params)`` so callers (and tests) can run EXPLAIN
    QUERY PLAN against the rewrite. Three branches:

    - ``"*"`` -> drop the subspace predicate entirely. The query becomes
      a rowid range walk (integer primary key) and never touches
      ``idx_events_subspace_rowid``.
    - ``"prefix*"`` -> rewrite to ``subspace >= 'prefix' AND
      subspace < 'prefix￿'``, which SQLite resolves via
      ``idx_events_subspace_rowid`` as a range scan.
    - Anything else (e.g. ``"[abc]*"``, ``"foo?bar"``) keeps the
      original ``GLOB`` predicate. SQLite uses whatever index it can.
    """
    if subspace_glob == "*":
        return (
            "SELECT rowid, subspace, op, tuple_id, payload_summary, category, ts "
            "FROM events WHERE rowid > ? ORDER BY rowid LIMIT ?",
            (after_rowid, limit),
        )
    prefix_range = _glob_to_prefix_range(subspace_glob)
    if prefix_range is not None:
        lo, hi = prefix_range
        return (
            "SELECT rowid, subspace, op, tuple_id, payload_summary, category, ts "
            "FROM events WHERE subspace >= ? AND subspace < ? AND rowid > ? "
            "ORDER BY rowid LIMIT ?",
            (lo, hi, after_rowid, limit),
        )
    return (
        "SELECT rowid, subspace, op, tuple_id, payload_summary, category, ts "
        "FROM events WHERE subspace GLOB ? AND rowid > ? "
        "ORDER BY rowid LIMIT ?",
        (subspace_glob, after_rowid, limit),
    )


def _fetch_event_batch(
    conn: sqlite3.Connection,
    *,
    subspace_glob: str,
    after_rowid: int,
    limit: int,
) -> list[EventRecord]:
    """Pull the next ``events`` batch since ``after_rowid``.

    nexus-anjo: dispatches on the glob shape so SQLite can use the
    right index. See :func:`_build_fetch_event_batch_sql` for the
    rewrite rules.
    """
    sql, params = _build_fetch_event_batch_sql(
        subspace_glob=subspace_glob, after_rowid=after_rowid, limit=limit
    )
    rows = conn.execute(sql, params).fetchall()
    return [
        EventRecord(
            cursor=int(r[0]),
            subspace=str(r[1]),
            op=str(r[2]),
            tuple_id=str(r[3]),
            # nexus-26b7 (notable, dim-5 N4): mirror the explicit
            # casts above so a BLOB or numeric in events.payload_summary
            # / .category surfaces as a typed string rather than
            # smuggling a non-Optional[str] into a frozen dataclass.
            payload_summary=str(r[4]) if r[4] is not None else None,
            category=str(r[5]) if r[5] is not None else None,
            ts=float(r[6]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# The watcher
# ---------------------------------------------------------------------------


class BindingWatcher:
    """Async polling reaction loop over the tuplespace ``events`` table.

    One watcher subscribes to a single *subspace_glob* (defaults to ``*``
    so all events are visible) and dispatches every loaded *profile*'s
    bindings on each event in cursor order.

    Wire from cockpit startup:

        >>> watcher = BindingWatcher(
        ...     conn=conn,
        ...     profiles=load_profiles_dir(profiles_dir),
        ...     context=BindingContext(conn=conn, index=index, registry=registry),
        ... )
        >>> task = asyncio.create_task(watcher.run())
        >>> # ... later ...
        >>> watcher.request_stop()
        >>> await task

    The class is intentionally direct-mode polling. Daemon-mode users
    will swap the SQL polling for ``event_stream.subscribe`` once the
    RPC ships (nexus-m4gm, RDR-112). Until then, this is the same query
    the daemon-side handler runs -- identical semantics.
    """

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        profiles: Iterable[BindingProfile],
        context: BindingContext,
        subspace_glob: str = "*",
        poll_interval: float = 0.05,
        batch_limit: int = 100,
        stop_timeout: float = 5.0,
        profiles_dirs: Optional[Iterable[Path]] = None,
    ) -> None:
        self._conn = conn
        self._profiles: tuple[BindingProfile, ...] = tuple(profiles)
        self._context = context
        self._subspace_glob = subspace_glob
        self._poll_interval = poll_interval
        self._batch_limit = batch_limit
        self._stop_timeout = stop_timeout
        self._stop = asyncio.Event()
        # nexus-7lb9: profile-reload support. When ``profiles_dirs`` is
        # provided, ``_reload_if_changed`` re-scans those directories and
        # rebuilds ``self._profiles`` when any source file's mtime changes
        # (or the directory contents change). Cheap: one stat per file
        # per check, polled at the existing tick cadence.
        self._profiles_dirs: tuple[Path, ...] = (
            tuple(profiles_dirs) if profiles_dirs else ()
        )
        # nexus-9pkn (S360-time S2): fingerprint pairs (mtime, size) so
        # two writes within the same coarse-mtime second still produce
        # distinct fingerprints. Older APFS-fallback, FAT, and some NFS
        # filesystems round mtime to whole seconds.
        self._profile_fingerprints: dict[Path, tuple[float, int]] = {}
        self._fingerprint_profiles_dirs()
        # One cursor per (subspace_glob, profile_name). Loaded lazily in run().
        self._cursors: dict[str, int] = {}
        # Task handle from start(); stays None until start() is called.
        self._task: asyncio.Task | None = None  # type: ignore[type-arg]

    # -- lifecycle --------------------------------------------------------

    def request_stop(self) -> None:
        """Signal the watcher to exit at the next loop boundary."""
        self._stop.set()

    def start(self) -> "asyncio.Task[None]":
        """Schedule :meth:`run` on the current event loop. Idempotent.

        Returns the asyncio task driving the loop. Subsequent calls
        return the existing task without spawning a second one. The
        daemon stores the watcher instance and calls :meth:`stop`
        during shutdown; tests can ``await`` the returned task
        directly for fine-grained orchestration.

        nexus-26b7 (notable, dim-6 N6): MUST be called from within a
        running event loop. ``asyncio.create_task`` raises a generic
        ``RuntimeError`` otherwise; surface a clearer message.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError(
                "BindingWatcher.start() must be called from a running "
                "event loop (asyncio.create_task requires one)."
            ) from exc
        if self._task is not None and not self._task.done():
            return self._task
        self._task = asyncio.create_task(self.run())
        return self._task

    async def stop(self) -> None:
        """Signal the loop to exit and wait for the task to finish.

        Bounded by ``stop_timeout`` (default 5s, matching the daemon's
        socket-close timeout). On timeout the task is cancelled and a
        warning is logged. Safe to call before :meth:`start` (no-op)
        or twice (second call is a no-op).
        """
        self.request_stop()
        if self._task is None or self._task.done():
            return
        try:
            await asyncio.wait_for(self._task, timeout=self._stop_timeout)
        except asyncio.TimeoutError:
            _log.warning(
                "binding_watcher_stop_timeout",
                timeout=self._stop_timeout,
            )
            self._task.cancel()
            try:
                await self._task
            # nexus-26b7 (notable, dim-5 N5): keep the swallow on
            # cancellation but log everything else so a surprise
            # exception on shutdown does not vanish entirely.
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                _log.debug(
                    "binding_watcher_stop_swallowed",
                    exc_type=type(exc).__qualname__,
                    exc=str(exc),
                )

    async def run(self) -> None:
        """Run the polling loop until :meth:`request_stop` is invoked."""
        # Load per-profile cursors once at startup.
        #
        # SQLite connection objects are pinned to the thread that opened
        # them (default ``check_same_thread=True``). Production callers
        # construct the watcher on the asyncio loop thread, so running
        # the SQL inline here is correct. We deliberately do NOT use
        # ``asyncio.to_thread`` for the SQLite calls -- that would land in
        # a worker thread and trip SQLite's same-thread guard. The
        # queries themselves are sub-millisecond and never block the
        # loop noticeably.
        for profile in self._profiles:
            self._cursors[profile.name] = _load_cursor(
                self._conn,
                subspace_prefix=self._subspace_glob,
                profile=profile.name,
            )

        _log.info(
            "binding_watcher_started",
            profiles=[p.name for p in self._profiles],
            subspace_glob=self._subspace_glob,
            cursors=dict(self._cursors),
        )

        while not self._stop.is_set():
            advanced = await self._tick()
            if advanced == 0:
                # Quiet -- wait for either poll-tick or stop.
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._poll_interval
                    )
                except asyncio.TimeoutError:
                    pass

        _log.info(
            "binding_watcher_stopped",
            cursors=dict(self._cursors),
        )

    # -- single iteration --------------------------------------------------

    async def _tick(self) -> int:
        """Run one polling iteration. Returns the number of events processed.

        nexus-7lb9: each tick first checks whether the profile source
        files have changed on disk and reloads if so. The mtime stat is
        cheap (one stat per *.yml in the configured dirs) and skipped
        entirely when no ``profiles_dirs`` were wired (legacy path).
        """
        self._reload_if_changed()
        total = 0
        for profile in self._profiles:
            cursor = self._cursors.get(profile.name, 0)
            try:
                events = _fetch_event_batch(
                    self._conn,
                    subspace_glob=self._subspace_glob,
                    after_rowid=cursor,
                    limit=self._batch_limit,
                )
            except sqlite3.Error:
                _log.exception(
                    "binding_watcher_fetch_failed",
                    profile=profile.name,
                    cursor=cursor,
                )
                continue

            if not events:
                continue

            self._context.profile_name = profile.name
            for event in events:
                await self._dispatch_event(profile, event)
            # Cursor advances even if individual bindings raised -- error
            # containment means we never get stuck on a poison event.
            new_cursor = max(e.cursor for e in events)
            self._cursors[profile.name] = new_cursor
            try:
                _save_cursor(
                    self._conn,
                    subspace_prefix=self._subspace_glob,
                    profile=profile.name,
                    last_rowid=new_cursor,
                )
            except sqlite3.Error:
                _log.exception(
                    "binding_watcher_cursor_save_failed",
                    profile=profile.name,
                    cursor=new_cursor,
                )
            total += len(events)
        return total

    # -- mtime-based hot reload (nexus-7lb9) ----------------------------

    def _fingerprint_profiles_dirs(self) -> None:
        """Snapshot per-file (mtime, size) across the watcher's profile dirs.

        nexus-9pkn (S360-time S2): tuple includes size so back-to-back
        writes within the same coarse-mtime second still produce
        different fingerprints. mtime alone misses such edits on
        filesystems with 1-second granularity.
        """
        fps: dict[Path, tuple[float, int]] = {}
        for d in self._profiles_dirs:
            if not d.is_dir():
                continue
            for yml in d.glob("*.yml"):
                try:
                    st = yml.stat()
                    fps[yml] = (st.st_mtime, st.st_size)
                except OSError:
                    continue
        self._profile_fingerprints = fps

    def _reload_if_changed(self) -> None:
        """Re-load profiles when any source YAML's mtime changed.

        No-op when ``profiles_dirs`` is empty (the legacy fixed-profile
        construction path). If ANY YAML fails to parse, the previous
        profile list is retained in full — a broken YAML in the user
        dir cannot brick the watcher OR silently drop a previously-
        loaded profile (nexus-0cf1.2, TR-2).
        """
        if not self._profiles_dirs:
            return
        previous = dict(self._profile_fingerprints)
        self._fingerprint_profiles_dirs()
        if previous == self._profile_fingerprints:
            return

        # Something changed: reload everything. nexus-0cf1.2 (TR-2):
        # ANY parse error retains the previous profile list. Partial
        # success would silently drop profiles whose YAML went bad
        # mid-reload, which violates the docstring contract.
        new_profiles: list[BindingProfile] = []
        had_failure = False
        for d in self._profiles_dirs:
            if not d.is_dir():
                continue
            for yml in sorted(d.glob("*.yml")):
                try:
                    new_profiles.append(load_profile(yml))
                except Exception as exc:
                    had_failure = True
                    _log.warning(
                        "binding_watcher_profile_reload_failed",
                        path=str(yml),
                        error=str(exc),
                    )

        if had_failure:
            _log.warning(
                "binding_watcher_reload_aborted_retaining_previous",
                profile_count=len(self._profiles),
            )
            # Restore the previous fingerprints so the next tick re-tries.
            self._profile_fingerprints = previous
            return

        # nexus-26b7 (notable, dim-1 N1): mass-delete gap. If the
        # reload returned no profiles but we had some, retain the
        # previous list and log a warning. The TR-2 docstring promises
        # that a broken YAML cannot silently drop previously-loaded
        # profiles; an empty directory after a mass `rm *.yml` was
        # not covered.
        if not new_profiles and self._profiles:
            _log.warning(
                "binding_watcher_reload_aborted_no_profiles_found",
                previous_count=len(self._profiles),
                profiles_dirs=[str(d) for d in self._profiles_dirs],
            )
            self._profile_fingerprints = previous
            return

        self._profiles = tuple(new_profiles)
        _log.info(
            "binding_watcher_profiles_reloaded",
            count=len(new_profiles),
            profiles=[p.name for p in new_profiles],
        )

    async def _dispatch_event(
        self, profile: BindingProfile, event: EventRecord
    ) -> None:
        for binding in profile.bindings:
            # nexus-7lb9: disabled bindings stay in the loaded profile
            # so the CRUD MCP can list / toggle them, but they do not
            # fire actions.
            if not binding.enabled:
                continue
            if not matches(event, binding.match):
                continue
            try:
                await _dispatch_action(
                    binding.action,
                    event=event,
                    binding=binding,
                    context=self._context,
                )
            except Exception:
                # Containment per acceptance criterion 4: one bad binding
                # must not crash the watcher or starve siblings.
                _log.exception(
                    "binding_action_failed",
                    profile=profile.name,
                    binding=binding.name,
                    action_kind=binding.action.kind,
                    action_target=binding.action.target,
                    subspace=event.subspace,
                    op=event.op,
                    tuple_id=event.tuple_id,
                )


# ---------------------------------------------------------------------------
# Default profile location
# ---------------------------------------------------------------------------


def default_profiles_dir() -> Path:
    """Resolve the canonical builtin profiles dir relative to the package.

    Returns the shipped builtin path under ``nx/tuplespace/builtin/bindings/
    profiles/``. Operator-created bindings live in :func:`user_profiles_dir`
    so CRUD writes do not modify checked-in defaults.
    """
    here = Path(__file__).resolve()
    # src/nexus/cockpit/bindings.py -> repo root -> nx/tuplespace/builtin/bindings/profiles
    repo_root = here.parent.parent.parent.parent
    return repo_root / "nx" / "tuplespace" / "builtin" / "bindings" / "profiles"


def user_profiles_dir() -> Path:
    """Return the user-owned binding-profile dir.

    nexus-7lb9: operator-created bindings (via the binding_create MCP
    tool or hand-edited YAML) live here, separate from the shipped
    builtin defaults under :func:`default_profiles_dir`. The watcher
    loads both dirs (builtin first, then user) and the user profiles
    can override builtin profiles by name.
    """
    # nexus-bkvg (FS-3): route through nexus_config_dir() so
    # NEXUS_CONFIG_DIR overrides apply here too. The previous direct
    # expanduser("~") bypassed the TR-5 wiring, leaving sandboxed
    # runs and multi-profile installs unable to redirect binding YAMLs.
    from nexus.config import nexus_config_dir  # noqa: PLC0415

    return nexus_config_dir() / "bindings" / "profiles"
