# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Aura evaluation on the channel waiter path (RDR-249 Technical Design 4, 5 and 6; bead nexus-mdbds.11.1).

The waiter hands each board topic's kept rows to :meth:`AuraRuntime.offer`, which only matches and enqueues: it
never blocks the waiter's loop. Each aura has one queue holding its fires and clears together, in arrival order,
and one worker thread that runs one item at a time, so a clear that arrives during a fire lands after it. With more
than :data:`MAX_WAITING` items waiting the oldest fire is dropped and counted; a clear is never dropped.

A fire builds the procedure's arguments from the row, runs it through ``run_script`` on the worker thread, tests
``when.field`` and writes ``text`` as the aura's threads block. ``display.channel`` then sends
:func:`channel_notice`, a fixed template that carries nothing a tuple writer controls, whether or not the block
could be written. When ``display.channel`` is the name of a result field (RDR-252) the notice goes out only when that
field is ``True``; a missing or non-boolean field is a failure, like ``when.field``. A ``self.identity`` argument is the
identity the runtime was built with. A clear removes the block and runs nothing; a clear queued straight after another is dropped,
since it removes nothing more.

A failure (a ``ScriptRunError`` other than busy, a missing argument source, a missing or mistyped result field,
a display error, or any other exception in the fire) is logged with the aura name and tuple id and written into the block as one line; three in a
row disable the aura for the session, and the block says so. A success resets the count. ``ScriptBusyError`` is
not a failure: the run path's workers are shared with ``procedure_run`` and ``nx run``, so the item goes back to
the head of its queue and is retried after :data:`BUSY_RETRY_S`, up to :data:`BUSY_RETRIES` times, and is then
dropped and logged. A disabled aura does not clear: its block keeps the disabled notice.
"""

from __future__ import annotations

import collections
import dataclasses
import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

import structlog

from nexus.auras.aura import ArgSource, Aura
from nexus.lua_runtime.errors import ScriptBusyError, ScriptError, ScriptLimitError, ScriptRunError

_log = structlog.get_logger(__name__)

#: Most items an aura's queue holds waiting (the running one not counted); past it the oldest fire is dropped.
MAX_WAITING: int = 20
#: Consecutive failures that disable an aura for the session.
MAX_STRIKES: int = 3
#: Seconds between retries of an item the run path refused as busy, and how many retries before it is dropped.
BUSY_RETRY_S: float = 5.0
BUSY_RETRIES: int = 12

RunScript = Callable[..., Any]


class BlockWriter(Protocol):
    """What the runtime needs of :class:`nexus.auras.threads_block.ThreadsBlockWriter`."""

    def set_block(self, aura: str, threads_value: str, text: str) -> bool: ...

    def remove_block(self, aura: str, threads_value: str) -> bool: ...


def channel_notice(aura: Aura, subspace: str, tuple_id: str) -> str:
    """The one channel text an aura sends (RDR-211): the aura's name and threads value from its file, the subspace
    and tuple id from the engine row, and nothing else."""
    return (
        f'aura {aura.name} fired on {subspace} tuple {tuple_id}; '
        f'see the "aura: {aura.threads}" block in your threads entry'
    )


class _Failure(Exception):
    """A failed fire; ``kind`` names it in the block's error line."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind


#: The ``from`` value that names the engine. ``from`` is a dimension the writer typed, so any writer can post it;
#: a row counts as the engine's only when it also carries no ``sender_session`` (RDR-252 critique S3).
ENGINE_WRITER = "engine"
#: Refusal keys kept for the once-per-row log line; past this the set restarts, so a long session holds a bounded few.
MAX_REFUSED_KEYS = 1024


def _engine_wrote(row: Any) -> bool:
    """True when *row* is one the engine wrote itself: the engine stamps ``sender_session`` on a client's write and
    writes its own rows with none, so the stamp must be rendered (``sender_session_known``) and empty. An engine that
    renders no stamp cannot say, which reads as not the engine's. A blank stamp is a stamp: only a null one counts."""
    return bool(getattr(row, "sender_session_known", False)) and getattr(row, "sender_session", None) is None


def _matches(pairs: Sequence[tuple[str, str]] | None, row: Any) -> bool:
    """Every pair equals the row's dimension. A pair ``from = "engine"`` also needs the row to be one the engine
    wrote (:func:`_engine_wrote`): the typed ``from`` alone is a claim any writer can make."""
    if pairs is None:
        return False
    dims = row.dims or {}
    for k, v in pairs:
        if dims.get(k) != v:
            return False
        if k == "from" and v == ENGINE_WRITER and not _engine_wrote(row):
            return False
    return True


def _refused_for_stamp(pairs: Sequence[tuple[str, str]] | None, row: Any) -> bool:
    """True when *row* meets every pair of *pairs* except that its ``from = "engine"`` claim fails :func:`_engine_wrote`."""
    if pairs is None or _matches(pairs, row):
        return False
    dims = row.dims or {}
    return (any(k == "from" and v == ENGINE_WRITER for k, v in pairs) and all(dims.get(k) == v for k, v in pairs)
            and not _engine_wrote(row))


_ABSENT = object()


def _arg(spec: ArgSource, row: Any, name: str, identity: str | None = None) -> Any:
    if spec.kind == "self":
        if spec.value != "identity":  # self.branch is a template's: expansion made it a literal, so none is left to fill here
            raise _Failure("args", f"{name}: self.{spec.value} is not something a loaded aura fills")
        if identity is None:
            raise _Failure("args", f"{name}: this session has no identity to fill self.{spec.value} with")
        return identity
    if spec.kind == "id":
        return row.id
    if spec.kind == "subspace":
        return row.subspace
    if spec.kind == "literal":
        return spec.value
    if spec.kind == "dims":
        if spec.value not in row.dims:
            raise _Failure("args", f"{name}: the tuple has no dims.{spec.value}")
        return row.dims[spec.value]
    try:
        body = json.loads(row.body) if row.body else None
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise _Failure("args", f"{name}: the tuple body has no top-level {spec.value!r}")
    # A field the row lacks is left out, not a failure: an older engine's row, or an optional field the engine omits
    # (RDR-252 owner_alias for an identity with no alias). The procedure's header still refuses a missing required one.
    return body.get(spec.value, _ABSENT)


def _error_kind(exc: ScriptRunError) -> str:
    if isinstance(exc, ScriptError):
        return exc.kind
    if isinstance(exc, ScriptLimitError):
        return f"limit_{exc.limit}"
    return "worker"


@dataclasses.dataclass
class _Item:
    kind: str  # "fire" or "clear"
    row: Any
    busy: int = 0


class _Lane:
    """One aura's queue, worker and failure count."""

    def __init__(self, aura: Aura) -> None:
        self.aura = aura
        self.queue: collections.deque[_Item] = collections.deque()
        self.running = False
        self.strikes = 0
        self.disabled = False
        self.dropped = 0


class AuraRuntime:
    """Evaluates the session's loaded auras against board rows (see the module docstring).

    *writer* writes and removes threads blocks; *run* is ``run_script``; *notify* sends a channel text, or is
    ``None`` when there is no channel (its meta carries the same three values as the text); *sleep* waits between busy retries;
    *identity* is the session's identity id, which fills a ``self.identity`` argument (RDR-252) and is ``None`` for a
    server that started unbound.
    """

    def __init__(
        self,
        auras: Sequence[Aura],
        *,
        writer: BlockWriter,
        run: RunScript | None = None,
        notify: Callable[[str, dict[str, str]], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        identity: str | None = None,
    ) -> None:
        if run is None:
            from nexus.lua_runtime import run_script as run  # noqa: PLC0415 -- the run path loads on first use
        self._run = run
        self._writer = writer
        self._notify = notify
        self._sleep = sleep
        self._identity = identity
        self._lanes = {a.name: _Lane(a) for a in auras}
        self._cond = threading.Condition()
        self._retiring = 0  # retirements of pre-template blocks still running (:meth:`retire_blocks`)
        self._refused: set[tuple[str, str]] = set()  # (aura, row id) already logged for its stamp refusal

    def extend(self, auras: Sequence[Aura], identity: str | None) -> None:
        """Take more auras after the first load, and the identity a ``self.identity`` argument now fills: a server that
        loaded with no identity gates its group-gated auras again once it binds (RDR-252). Call it on the loop that
        calls :meth:`offer`; a lane already present is kept."""
        with self._cond:
            self._identity = identity
            for aura in auras:
                self._lanes.setdefault(aura.name, _Lane(aura))

    def retire_blocks(self, pairs: Sequence[tuple[str, str]]) -> None:
        """Remove each ``(aura name, threads value)`` block from the session's threads entry, on a worker thread (a T2
        read and write), under the same lock as every other block write. The loader asks it once a template that replaced
        a pre-template aura has expanded, so the old heading does not stay in the entry. A failure is logged, never raised."""
        pairs = list(pairs)
        if not pairs:
            return
        with self._cond:
            self._retiring += 1
        threading.Thread(target=self._retire, args=(pairs,), name="aura-retire", daemon=True).start()

    def _retire(self, pairs: list[tuple[str, str]]) -> None:
        try:
            for aura, threads_value in pairs:
                try:
                    self._writer.remove_block(aura, threads_value)
                except Exception as exc:  # noqa: BLE001 -- an old block left in place is cosmetic
                    _log.warning("aura_legacy_block_not_removed", aura=aura, error=str(exc))
        finally:
            with self._cond:
                self._retiring -= 1
                self._cond.notify_all()

    # -- the waiter side: match and enqueue, never block ---------------------------------------------------------

    def topics(self) -> dict[str, str]:
        """Each loaded aura's name and its trigger subspace."""
        return {name: lane.aura.subspace for name, lane in self._lanes.items()}

    def offer(self, subspace: str, rows: Sequence[Any]) -> None:
        for lane in self._lanes.values():
            if lane.aura.subspace != subspace:
                continue
            for row in rows:
                if not lane.aura.match or _matches(lane.aura.match, row):
                    self._enqueue(lane, _Item("fire", row))
                elif _matches(lane.aura.clear_match, row):
                    self._enqueue(lane, _Item("clear", row))
                elif _refused_for_stamp(lane.aura.match, row) or _refused_for_stamp(lane.aura.clear_match, row):
                    self._note_stamp_refusal(lane, row)

    def _note_stamp_refusal(self, lane: _Lane, row: Any) -> None:
        """One debug line per (aura, row): a row claiming ``from = "engine"`` that the stamp rules out. Without it an
        engine whose own rows began to carry a stamp would silence the aura with no trace (RDR-252 critique S3b).
        The line says the row was stamped, never which session stamped it."""
        key = (lane.aura.name, str(row.id))
        with self._cond:
            if key in self._refused:
                return
            if len(self._refused) >= MAX_REFUSED_KEYS:
                self._refused.clear()
            self._refused.add(key)
        _log.debug("aura_from_engine_ignored", aura=lane.aura.name, tuple_id=row.id,
                   stamp="unrendered" if not getattr(row, "sender_session_known", False) else "present")

    def _enqueue(self, lane: _Lane, item: _Item) -> None:
        with self._cond:
            if lane.disabled:
                return
            if item.kind == "clear" and lane.queue and lane.queue[-1].kind == "clear":
                return  # a second clear in a row removes nothing the first does not
            lane.queue.append(item)
            dropped = 0
            while len(lane.queue) > MAX_WAITING:
                oldest_fire = next((i for i in lane.queue if i.kind == "fire"), None)
                if oldest_fire is None:
                    break
                lane.queue.remove(oldest_fire)
                dropped += 1
            if dropped:
                lane.dropped += dropped
                _log.warning("aura_queue_dropped", aura=lane.aura.name, dropped=dropped, total=lane.dropped)
            if not lane.running:
                lane.running = True
                threading.Thread(target=self._work, args=(lane,), name=f"aura-{lane.aura.name}", daemon=True).start()

    # -- the worker side --------------------------------------------------------------------------------------------

    def _work(self, lane: _Lane) -> None:
        while True:
            with self._cond:
                if lane.disabled:
                    lane.queue.clear()
                if not lane.queue:
                    lane.running = False
                    self._cond.notify_all()
                    return
                item = lane.queue.popleft()
            try:
                if item.kind == "clear":
                    self._writer.remove_block(lane.aura.name, lane.aura.threads)
                else:
                    self._fire(lane, item)
            except ScriptBusyError:
                if item.busy >= BUSY_RETRIES:
                    _log.warning("aura_busy_dropped", aura=lane.aura.name, tuple_id=item.row.id, retries=item.busy)
                    continue
                item.busy += 1
                with self._cond:
                    lane.queue.appendleft(item)
                self._sleep(BUSY_RETRY_S)
            except Exception as exc:  # noqa: BLE001 -- an aura never stops the waiter or the announcement
                # Only a clear reaches here (a fire turns every other exception into a strike); its block stays
                # until the next fire or clear.
                _log.warning("aura_clear_failed", aura=lane.aura.name, tuple_id=item.row.id, error=str(exc))

    def _fire(self, lane: _Lane, item: _Item) -> None:
        aura, row = lane.aura, item.row
        try:
            args = {name: value for name, spec in aura.args
                    if (value := _arg(spec, row, name, self._identity)) is not _ABSENT}
            try:
                result = self._run(aura.procedure, args, dataclasses.asdict(aura.limits))
            except ScriptBusyError:
                raise
            except ScriptRunError as exc:
                raise _Failure(_error_kind(exc), str(exc)) from exc
            if not isinstance(result, dict):
                raise _Failure("result", "the procedure's result is not a table")
            if aura.when_field is not None:
                shown = result.get(aura.when_field)
                if not isinstance(shown, bool):
                    raise _Failure("result", f"when.field {aura.when_field!r} is not a boolean in the result")
                if not shown:
                    lane.strikes = 0
                    return
            text = result.get("text")
            if not isinstance(text, str):
                raise _Failure("result", "the result has no string field 'text'")
            wake = aura.channel is True
            if isinstance(aura.channel, str):
                wake = result.get(aura.channel)
                if not isinstance(wake, bool):
                    raise _Failure("result", f"display.channel {aura.channel!r} is not a boolean in the result")
            try:
                wrote = self._writer.set_block(aura.name, aura.threads, text)
            except Exception as exc:  # noqa: BLE001 -- a T2 failure is a display error
                raise _Failure("display", str(exc)) from exc
        except _Failure as failure:
            self._strike(lane, row, failure)
            return
        except ScriptBusyError:
            raise
        except Exception as exc:  # noqa: BLE001 -- a bug or an OS error in a fire is a failure like any other
            self._strike(lane, row, _Failure("internal", f"{type(exc).__name__}: {exc}"))
            return
        lane.strikes = 0
        if not wrote:
            _log.warning("aura_block_not_written", aura=aura.name, tuple_id=row.id)
        if wake and self._notify is not None:
            try:
                meta = {"aura": aura.name, "subspace": row.subspace, "tuple_id": row.id}
                self._notify(channel_notice(aura, row.subspace, row.id), meta)
            except Exception as exc:  # noqa: BLE001 -- a lost notice is logged, never a strike
                _log.warning("aura_notice_failed", aura=aura.name, tuple_id=row.id, error=str(exc))

    def _strike(self, lane: _Lane, row: Any, failure: _Failure) -> None:
        lane.strikes += 1
        _log.warning(
            "aura_fire_failed", aura=lane.aura.name, tuple_id=row.id, kind=failure.kind,
            error=str(failure), strikes=lane.strikes,
        )
        if lane.strikes >= MAX_STRIKES:
            with self._cond:
                lane.disabled = True
            text = f"aura disabled after {MAX_STRIKES} failures: {failure.kind}"
            _log.warning("aura_disabled", aura=lane.aura.name, kind=failure.kind)
        else:
            text = f"aura error: {failure.kind} on tuple {row.id}"
        try:
            self._writer.set_block(lane.aura.name, lane.aura.threads, text)
        except Exception as exc:  # noqa: BLE001 -- already a failure; the log line carries it
            _log.warning("aura_error_line_failed", aura=lane.aura.name, tuple_id=row.id, error=str(exc))

    # -- reading state (doctor rows and tests) -----------------------------------------------------------------------

    def wait_idle(self, timeout: float) -> bool:
        """Wait until every queue is empty and no worker runs. False on timeout."""
        with self._cond:
            return self._cond.wait_for(
                lambda: not self._retiring and not any(l.running for l in self._lanes.values()), timeout,
            )

    def is_disabled(self, aura: str) -> bool:
        return self._lanes[aura].disabled

    def strikes(self, aura: str) -> int:
        return self._lanes[aura].strikes

    def dropped(self, aura: str) -> int:
        return self._lanes[aura].dropped

    def waiting(self, aura: str) -> list[tuple[str, str]]:
        with self._cond:
            return [(i.kind, i.row.id) for i in self._lanes[aura].queue]
