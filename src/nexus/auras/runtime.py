# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Aura evaluation on the channel waiter path (RDR-249 Technical Design 4, 5 and 6; bead nexus-mdbds.11.1).

The waiter hands each board topic's kept rows to :meth:`AuraRuntime.offer`, which only matches and enqueues: it
never blocks the waiter's loop. Each aura has one queue holding its fires and clears together, in arrival order,
and one worker thread that runs one item at a time, so a clear that arrives during a fire lands after it. With more
than :data:`MAX_WAITING` items waiting the oldest fire is dropped and counted; a clear is never dropped.

A fire builds the procedure's arguments from the row, runs it through ``run_script`` on the worker thread, tests
``when.field`` and writes ``text`` as the aura's threads block. ``display.channel`` then sends
:func:`channel_notice`, a fixed template that carries nothing a tuple writer controls. A clear removes the block
and runs nothing.

A failure (a ``ScriptRunError`` other than busy, a missing argument source, a missing or mistyped result field,
or a display error) is logged with the aura name and tuple id and written into the block as one line; three in a
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


def _matches(pairs: Sequence[tuple[str, str]] | None, dims: Mapping[str, str]) -> bool:
    return pairs is not None and all(dims.get(k) == v for k, v in pairs)


def _arg(source: ArgSource, row: Any, name: str) -> Any:
    if source.kind == "id":
        return row.id
    if source.kind == "subspace":
        return row.subspace
    if source.kind == "literal":
        return source.value
    if source.kind == "dims":
        if source.value not in row.dims:
            raise _Failure("args", f"{name}: the tuple has no dims.{source.value}")
        return row.dims[source.value]
    try:
        body = json.loads(row.body) if row.body else None
    except ValueError:
        body = None
    if not isinstance(body, dict) or source.value not in body:
        raise _Failure("args", f"{name}: the tuple body has no top-level {source.value!r}")
    return body[source.value]


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
    ``None`` when there is no channel; *sleep* waits between busy retries.
    """

    def __init__(
        self,
        auras: Sequence[Aura],
        *,
        writer: BlockWriter,
        run: RunScript | None = None,
        notify: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if run is None:
            from nexus.lua_runtime import run_script as run  # noqa: PLC0415 -- the run path loads on first use
        self._run = run
        self._writer = writer
        self._notify = notify
        self._sleep = sleep
        self._lanes = {a.name: _Lane(a) for a in auras}
        self._cond = threading.Condition()

    # -- the waiter side: match and enqueue, never block ---------------------------------------------------------

    def topics(self) -> dict[str, str]:
        """Each loaded aura's name and its trigger subspace."""
        return {name: lane.aura.subspace for name, lane in self._lanes.items()}

    def offer(self, subspace: str, rows: Sequence[Any]) -> None:
        for lane in self._lanes.values():
            if lane.aura.subspace != subspace:
                continue
            for row in rows:
                dims = row.dims or {}
                if not lane.aura.match or _matches(lane.aura.match, dims):
                    self._enqueue(lane, _Item("fire", row))
                elif _matches(lane.aura.clear_match, dims):
                    self._enqueue(lane, _Item("clear", row))

    def _enqueue(self, lane: _Lane, item: _Item) -> None:
        with self._cond:
            if lane.disabled:
                return
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
                _log.warning("aura_clear_failed", aura=lane.aura.name, tuple_id=item.row.id, error=str(exc))

    def _fire(self, lane: _Lane, item: _Item) -> None:
        aura, row = lane.aura, item.row
        try:
            args = {name: _arg(source, row, name) for name, source in aura.args}
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
            try:
                wrote = self._writer.set_block(aura.name, aura.threads, text)
            except Exception as exc:  # noqa: BLE001 -- a T2 failure is a display error
                raise _Failure("display", str(exc)) from exc
        except _Failure as failure:
            self._strike(lane, row, failure)
            return
        lane.strikes = 0
        if wrote and aura.channel and self._notify is not None:
            try:
                self._notify(channel_notice(aura, row.subspace, row.id))
            except Exception as exc:  # noqa: BLE001 -- the block is written; a lost notice is only logged
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
            return self._cond.wait_for(lambda: not any(l.running for l in self._lanes.values()), timeout)

    def is_disabled(self, aura: str) -> bool:
        return self._lanes[aura].disabled

    def strikes(self, aura: str) -> int:
        return self._lanes[aura].strikes

    def dropped(self, aura: str) -> int:
        return self._lanes[aura].dropped

    def waiting(self, aura: str) -> list[tuple[str, str]]:
        with self._cond:
            return [(i.kind, i.row.id) for i in self._lanes[aura].queue]
