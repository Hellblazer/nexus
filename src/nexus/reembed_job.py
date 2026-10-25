# SPDX-License-Identifier: AGPL-3.0-or-later
"""The client half of the cross-model re-embed job (RDR-210 Phase 6 Step 3, nexus-cnxjk).

The engine owns the job: a ``reembed_jobs`` row, a runner, the state table. This module holds what the
client decides before it asks the engine to start one, and renders back what the engine says:

* which collections are stale (``--all`` candidacy) and which a running job already holds;
* the conformant name of the target sibling, which the engine registers when none exists (the engine has
  no renderer; the client owns names, ``corpus.py``);
* whether the engine can serve the target model at all (``GET /version`` ``embedding_models``), because a
  degraded boot (Technical Design 2) leaves a family unserved and a job started into it moves nothing;
* the token cap (a default for Voyage targets, none for the on-device bge model);
* the one-line hint that names ``nx collection re-embed --all``, printed where a profile change leaves
  collections on the previous model;
* ``--wait``: the states a wait returns on, and the poll loop (Step 4, nexus-09iho).

Pure functions where possible, so the CLI tests do not need an engine.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import structlog

_log = structlog.get_logger(__name__)

#: The job states that hold a source's lock (the engine's partial unique index). A source in one of them is
#: named by ``--all``, never planned a second time.
LOCK_STATES: frozenset[str] = frozenset({"planned", "running", "paused", "cutover", "failed"})

#: RDR-210 "Cost": each run has a token cap with a default of 10 million; a plan above it is refused before
#: any write unless a flag raises it. A bge re-embed has no cap (Sam, Q5).
DEFAULT_TOKEN_CAP: int = 10_000_000

REEMBED_ALL_COMMAND: str = "nx collection re-embed --all"

#: The line printed where a profile change leaves collections on the previous model (RDR-210 "Re-embed to the
#: profile default"): after ``nx config set`` / ``unset`` flips the profile, and after ``nx daemon service
#: start`` when a live collection is off the profile.
REEMBED_HINT: str = (
    "Collections on the previous embedding model keep their vectors. "
    f"To move them to the new profile: {REEMBED_ALL_COMMAND}"
)

#: How long ``nx daemon service start`` waits for the catalog read behind the hint. The hint is a courtesy;
#: a slow engine must not hold the start command.
HINT_READ_TIMEOUT_S: float = 5.0


def model_family(model: str) -> str | None:
    """``voyage`` or ``bge`` from a model token's prefix (the doctor's rule); ``None`` for any other."""
    if not isinstance(model, str):
        return None
    if model.startswith("voyage"):
        return "voyage"
    if model.startswith("bge"):
        return "bge"
    return None


def effective_token_cap(target_model: str, flag: int | None) -> int | None:
    """The cap sent in the start body: ``flag`` or the default for a Voyage target, ``None`` for any other.

    A bge target is embedded on this machine and bills nothing, so it has no cap and a ``--token-cap`` given
    with it is not sent (the caller says so).
    """
    if model_family(target_model) != "voyage":
        return None
    return flag if flag is not None else DEFAULT_TOKEN_CAP


class TargetNameError(ValueError):
    """The source's row gives no usable owner to name its target sibling after; the message says why."""


#: Owner values that name no one: hygiene-002's backfill wrote the row's own tenant id where it could find no
#: owner, and ``default`` is the placeholder corpus (``collection_shape`` reports it as such).
_PLACEHOLDER_OWNERS: frozenset[str] = frozenset({"", "default"})


def target_collection_name(
    content_type: str, owner_id: object, target_model: str, *, tenant_ids: Iterable[object] = (),
) -> str:
    """The conformant name (``<content_type>__<owner>__<model>__v1``) the engine registers for the target sibling.

    Built from the source's catalog ROW, never parsed from its name (RDR-204): ``owner_id`` with ``.`` read as
    ``-`` (a tumbler owner such as ``1.87`` is ``1-87`` in a name, as ``repos.py`` and ``nx index`` mint it).
    The engine registers this name only when no live sibling of the same ``(content_type, owner_id, model)``
    exists; otherwise it uses that sibling and ignores the name. It checks the registered row's tuple either way,
    so the name is a label and never the authority.

    Raises :class:`TargetNameError` rather than render a name that names no one: a null or blank owner, a
    placeholder owner (``default``, or one of ``tenant_ids``: the row's own tenant id, hygiene-002's stand-in), or an
    owner that does not make a conformant name.
    """
    from nexus.corpus import is_conformant_collection_name  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    if owner_id is None or not str(owner_id).strip():
        raise TargetNameError("its catalog row has no owner_id, so there is no owner to name the target after")
    owner = str(owner_id).strip()
    placeholders = _PLACEHOLDER_OWNERS | {str(t) for t in tenant_ids if t}
    if owner in placeholders:
        raise TargetNameError(
            f"its owner_id is the placeholder {owner!r} (no real owner was recorded), so a target named after it "
            "would belong to no one; give the collection an owner before re-embedding it"
        )
    name = f"{content_type}__{owner.replace('.', '-')}__{target_model}__v1"
    if not is_conformant_collection_name(name):
        raise TargetNameError(f"owner_id {owner!r} does not make a conformant collection name ({name!r})")
    return name


# ── the engine's side of "can it serve the target" ───────────────────────────


def missing_piece(target_model: str, served: Iterable[str]) -> str | None:
    """``None`` when ``target_model`` is among ``served``; else the sentence naming what the engine lacks.

    ``served`` is ``GET /version``'s ``embedding_models``. nexus-q6t89: a degraded boot serves one family and
    answers 422 for the other, so a plan that does not look would start a job into a family that cannot embed.
    """
    served = list(served)
    if target_model in served:
        return None
    from nexus.corpus import _SERVICE_RESTART_COMMAND  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    listed = ", ".join(served) or "no model"
    family = model_family(target_model)
    if family == "voyage":
        piece = (
            "it has no Voyage key. Persist one with `nx config set voyage_api_key --stdin` (or export "
            f"VOYAGE_API_KEY), then restart the service: `{_SERVICE_RESTART_COMMAND}`"
        )
    elif family == "bge":
        piece = (
            "it found no bge model files. Run `nx init --service` to provision them, then restart the "
            f"service: `{_SERVICE_RESTART_COMMAND}`"
        )
    else:
        piece = f"{target_model!r} is not a model it can load"
    return f"the engine does not serve {target_model} (it serves {listed}): {piece}"


# ── --all candidacy ──────────────────────────────────────────────────────────


def is_superseded(row: dict) -> bool:
    return bool(row.get("superseded_by"))


def stale_rows(rows: Iterable[dict], profile: dict[str, str]) -> list[dict]:
    """The live, unsuperseded rows whose model differs from the profile's model for their content type.

    Quarantine, dormant and disputed rows are never candidates (they drain through their own lifecycle), and
    a superseded row is not one either: ``--all`` lists a superseded source only through
    :func:`reentry_candidates`. A row whose content type has no profile model cannot be judged and is left out.
    """
    out = []
    for row in rows:
        if row.get("lifecycle_state") != "live" or is_superseded(row):
            continue
        want = profile.get(str(row.get("content_type")))
        if want is not None and row.get("embedding_model") != want:
            out.append(row)
    return sorted(out, key=lambda r: str(r.get("name", "")))


@dataclass(frozen=True)
class Reentry:
    """A source a ``kept_tombstoned`` job left superseded, listed again as that job's re-entry (not a new job).

    ``kind`` is ``cutover`` when purge-trash has left the source with no document row, ``running`` when a
    restore has made a document live in it. The engine performs the re-entry on a start whose source is the kept
    source (``HttpVectorClient.reembed_reenter``); the client lists the candidate, names the job and starts it.
    """

    job: dict
    source_row: dict
    kind: str


@dataclass
class Candidates:
    stale: list[dict] = field(default_factory=list)
    held: list[tuple[dict, dict]] = field(default_factory=list)
    reentries: list[Reentry] = field(default_factory=list)


def reentry_candidates(
    jobs: Iterable[dict],
    rows_by_name: dict[str, dict],
    live_docs: Callable[[str], int],
    all_docs: Callable[[str], int],
) -> list[Reentry]:
    """The ``kept_tombstoned`` jobs whose source is ready to be re-entered.

    Never a ``kept_by_flag`` or ``kept_residue`` source: those keep a superseded source on purpose. A job is a
    candidate when it is ``done`` with disposition ``kept_tombstoned``, its source row is still superseded BY
    ITS TARGET (a source revived by a later job the other way names no successor, or another one, and must not
    be re-entered: nexus-f98bg critique, significant 2), no job holds the source's lock, and the source now
    holds either no document row (``cutover``) or a live one (``running``). Documents still tombstoned and
    none live: not yet.
    """
    jobs = list(jobs)
    locked = {j["source_collection"] for j in jobs if j.get("state") in LOCK_STATES}
    newest: dict[str, dict] = {}
    for job in jobs:  # the status route lists newest first; keep the first job per source
        newest.setdefault(str(job.get("source_collection")), job)
    out: list[Reentry] = []
    for source, job in newest.items():
        if job.get("state") != "done" or job.get("disposition") != "kept_tombstoned" or source in locked:
            continue
        row = rows_by_name.get(source)
        if row is None or row.get("superseded_by") != job.get("target_collection"):
            continue
        if live_docs(source) > 0:
            out.append(Reentry(job, row, "running"))
        elif all_docs(source) == 0:
            out.append(Reentry(job, row, "cutover"))
    return sorted(out, key=lambda r: str(r.job.get("source_collection")))


def _lock_holders(jobs: Iterable[dict]) -> dict[str, dict]:
    """Source name to the newest job holding its lock (:data:`LOCK_STATES`)."""
    holder: dict[str, dict] = {}
    for job in jobs:
        if job.get("state") in LOCK_STATES:
            holder.setdefault(str(job.get("source_collection")), job)
    return holder


def candidates(
    rows: Iterable[dict],
    profile: dict[str, str],
    jobs: Iterable[dict],
    live_docs: Callable[[str], int],
    all_docs: Callable[[str], int],
) -> Candidates:
    """``--all``: the stale collections to plan, the ones a job already holds, and the re-entries."""
    rows = list(rows)
    jobs = list(jobs)
    holder = _lock_holders(jobs)
    result = Candidates()
    for row in stale_rows(rows, profile):
        job = holder.get(str(row.get("name")))
        (result.held.append((row, job)) if job is not None else result.stale.append(row))
    result.reentries = reentry_candidates(
        jobs, {str(r.get("name")): r for r in rows}, live_docs, all_docs,
    )
    return result


# ── --wait ───────────────────────────────────────────────────────────────────

#: The states ``--wait`` returns on, naming the state (RDR-210 State table): the job finished (``done``,
#: ``done_with_skips``) or stopped and needs a person (``aborted``, ``failed``, ``paused``).
WAIT_STATES: frozenset[str] = frozenset({"done", "done_with_skips", "aborted", "failed", "paused"})

#: The states after which ``--wait`` runs taxonomy discover for the target: the target holds the moved documents.
DISCOVER_STATES: frozenset[str] = frozenset({"done", "done_with_skips"})

#: The states ``--wait`` exits 0 on. The other three stopped before the job finished.
FINISHED_STATES: frozenset[str] = DISCOVER_STATES

#: What the engine's ``last_error`` starts with while a job sits in ``cutover`` unable to proceed (the source is no
#: longer registered, or was superseded by another collection). The job is not failed and does not move on its own,
#: so a wait that polled for it would never return.
CUTOVER_BLOCKED_PREFIX: str = "cutover is blocked: "

#: Seconds between status polls. A re-embed runs minutes to hours; a poll is one cheap read.
WAIT_POLL_S: float = 2.0


def is_blocked(row: dict) -> bool:
    """Whether *row* is a job parked in ``cutover`` with the engine's blocked message in ``last_error``."""
    return row.get("state") == "cutover" and str(row.get("last_error") or "").startswith(CUTOVER_BLOCKED_PREFIX)


def discover_command(target: str) -> str:
    """The command that builds a target's topics, centroids and assignments in its own embedding space."""
    return f"nx taxonomy discover --collection {target}"


def undiscovered(
    jobs: Iterable[dict], stamps: dict[str, dict], counts: dict[str, int], min_chunks: int,
) -> list[str]:
    """Targets of finished jobs (``done``, ``done_with_skips``) that have had no taxonomy discover since their job.

    *stamps* is ``{collection: {"last_discover_at": iso}}`` (the engine's taxonomy_meta), *counts* the targets'
    chunk counts. A target is listed when it holds at least *min_chunks* chunks (below that discover builds
    nothing, so asking for it would never clear) and its last discover is missing or older than the newest
    ``finished_at`` of a job into it. RDR-210 Technical Design 4: without ``--wait``, the doctor names the
    discover command until one has run.
    """
    from datetime import datetime  # noqa: PLC0415 — stdlib, only this helper needs it

    def when(iso: object) -> datetime | None:
        try:
            return datetime.fromisoformat(str(iso).replace("Z", "+00:00")) if iso else None
        except ValueError:
            return None

    finished: dict[str, datetime] = {}
    for j in jobs:
        target, at = j.get("target_collection"), when(j.get("finished_at"))
        if j.get("state") in DISCOVER_STATES and target and at is not None:
            finished[str(target)] = max(at, finished.get(str(target), at))
    out = []
    for target, at in finished.items():
        if counts.get(target, 0) < min_chunks:
            continue
        last = when((stamps.get(target) or {}).get("last_discover_at"))
        if last is None or last < at:
            out.append(target)
    return sorted(out)


def undiscovered_targets(jobs: Iterable[dict]) -> list[str]:
    """:func:`undiscovered` over the live engine: discover stamps from the taxonomy store, chunk counts from T3.

    Reads only the targets of finished jobs, one stamp request and one count per target. Raises on a read error;
    the doctor row catches it.
    """
    from nexus.db import make_t3  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore  # noqa: PLC0415 — deferred: CLI startup cost
    from nexus.db.t2.taxonomy_compute import MIN_DISCOVER_CHUNKS  # noqa: PLC0415 — deferred: CLI startup cost

    jobs = list(jobs)
    targets = sorted({
        str(j["target_collection"]) for j in jobs
        if j.get("state") in DISCOVER_STATES and j.get("target_collection")
    })
    if not targets:
        return []
    tax = HttpTaxonomyStore()
    try:
        stamps = tax.get_last_discover_stamps(targets)
    finally:
        tax.close()
    t3 = make_t3()
    counts = {t: int(t3.count(t)) for t in targets}
    return undiscovered(jobs, stamps, counts, MIN_DISCOVER_CHUNKS)


class WaitError(RuntimeError):
    """A job being waited on is no longer in the engine's status (it was never there, or the engine lost it)."""


def wait_for_jobs(
    fetch: Callable[[], list[dict]],
    job_ids: Iterable[str],
    *,
    sleep: Callable[[float], None],
    poll_s: float = WAIT_POLL_S,
    on_change: Callable[[dict], None] | None = None,
) -> dict[str, dict]:
    """Poll ``fetch`` (the engine's status rows) until every job in ``job_ids`` is in a :data:`WAIT_STATES` state
    or parked in a blocked ``cutover`` (:func:`is_blocked`: it waits for a person, not for time).

    Returns ``{job_id: final row}``. ``on_change`` is called with a row each time its state changes (the first
    sight of a job included), so a caller can print progress without printing every poll. A job missing from a
    poll is :class:`WaitError`: the verb never waits on a job the engine does not know.
    """
    pending = list(dict.fromkeys(job_ids))
    final: dict[str, dict] = {}
    last_state: dict[str, str] = {}
    while True:
        rows = {str(r.get("job_id")): r for r in fetch()}
        _log.info("reembed_wait_poll", jobs=len(pending), seen=len(rows))
        for job_id in list(pending):
            row = rows.get(job_id)
            if row is None:
                raise WaitError(f"re-embed job {job_id} is not in the engine's status")
            state = str(row.get("state"))
            if last_state.get(job_id) != state:
                last_state[job_id] = state
                if on_change is not None:
                    on_change(row)
            if state in WAIT_STATES or is_blocked(row):
                final[job_id] = row
                pending.remove(job_id)
        if not pending:
            return final
        sleep(poll_s)


# ── the hint ─────────────────────────────────────────────────────────────────


def profile_models(reader: Any) -> dict[str, str]:
    """``{content_type: embedding_model}`` from the engine's own profile (never the client's intent)."""
    return {str(r["content_type"]): str(r["embedding_model"]) for r in reader.embedding_profile()}


@dataclass(frozen=True)
class OffProfile:
    """The off-profile predicate's answer: the rows owed a re-embed, and the superseded rows it could not judge.

    ``unjudged`` is empty unless the job table or the document counts could not be read, in which case it holds
    every superseded row (each is a kept source whose live-document check needs both) and ``unread`` names the read
    that failed (``"job table"`` or ``"document counts"``). The hint ignores both; the doctor says them, so
    "nothing is off-profile" is never printed over rows nobody looked at. ``held`` maps the name of each stale
    row a job holds (its lock states) to that job: ``--all`` names the job instead of planning the source.
    """

    rows: list[dict] = field(default_factory=list)
    unjudged: list[dict] = field(default_factory=list)
    unread: str = ""
    held: dict[str, dict] = field(default_factory=dict)


def off_profile_rows(
    rows: Iterable[dict],
    profile: dict[str, str],
    jobs: Callable[[], Iterable[dict]],
    live_docs: Callable[[str], int],
    all_docs: Callable[[str], int],
) -> list[dict]:
    """:func:`judge_off_profile_rows`' rows, for a caller that does not report what it could not judge."""
    return judge_off_profile_rows(rows, profile, jobs, live_docs, all_docs).rows


def judge_off_profile_rows(
    rows: Iterable[dict],
    profile: dict[str, str],
    jobs: Callable[[], Iterable[dict]],
    live_docs: Callable[[str], int],
    all_docs: Callable[[str], int],
    *,
    read_jobs: bool = False,
) -> OffProfile:
    """THE off-profile predicate (nexus-n6mh0, RDR-210 Phase 6 Step 5, S-R2-5): the rows still owed a re-embed.

    The doctor's off-profile row, the daemon-start and config hints, and ``--all`` all answer "is anything off
    the profile". They answer it here, so they cannot disagree. A row is off-profile when it is

    * live, not superseded, and under a model other than the profile's for its content type
      (:func:`stale_rows`; a source a running job holds is still off-profile), or
    * a source a ``kept_tombstoned`` job left superseded that holds a live document again (a restore): the
      routing listing leaves a superseded source out, so nothing serves that document until ``--all`` moves it.
      This is :func:`candidates`' ``running`` re-entry.

    Everything else superseded is not: a ``--keep-source`` source (``kept_by_flag``), a ``kept_residue`` source,
    a ``kept_tombstoned`` source with no live document (its re-entry is only the delete, so it is listed by
    ``--all`` but strands nothing), and a rename's tombstone. After ``re-embed --keep-source`` the source is
    therefore not drift: its successor carries the documents.

    ``jobs`` is a thunk and is read only when some row is superseded, the one case that needs it. An unreadable
    job table (or unreadable document counts) leaves the superseded rows out (the hint and the doctor are best
    effort), keeps the rest, and returns those superseded rows as ``unjudged``. ``read_jobs`` (the doctor) also
    reads the job table when only stale rows exist, so a row a running or failed job holds can be named with its job.
    """
    rows = list(rows)
    if not any(is_superseded(r) for r in rows):
        stale = stale_rows(rows, profile)
        if not (read_jobs and stale):
            return OffProfile(stale)
        try:
            holders = _lock_holders(list(jobs()))
        except Exception as exc:  # noqa: BLE001 — boundary: holders only annotate rows that are reported anyway
            _log.debug("off_profile_holders_unread", error=str(exc))
            return OffProfile(stale)
        return OffProfile(stale, held={str(r.get("name")): holders[str(r.get("name"))] for r in stale if str(r.get("name")) in holders})
    unread = "job table"
    try:
        job_rows = list(jobs())
        unread = "document counts"
        found = candidates(rows, profile, job_rows, live_docs, all_docs)
    except Exception as exc:  # noqa: BLE001 — boundary: the exception needs the job table and counts; without them only the rows speak
        _log.debug("off_profile_exception_unread", error=str(exc), unread=unread)
        unjudged = sorted((r for r in rows if is_superseded(r)), key=lambda r: str(r.get("name", "")))
        return OffProfile(stale_rows(rows, profile), unjudged, unread)
    out = list(found.stale) + [row for row, _job in found.held]
    out += [r.source_row for r in found.reentries if r.kind == "running"]
    held = {str(row.get("name")): job for row, job in found.held}
    return OffProfile(sorted(out, key=lambda r: str(r.get("name", ""))), held=held)


def local_jobs() -> list[dict]:
    """The tenant's re-embed jobs, newest first, for the hint and the doctor; ``[]`` outside local mode.

    The job runs on the local engine only, so no other mode has one to read. Raises when the engine cannot
    answer: :func:`off_profile_rows` treats that as no job table.
    """
    from nexus.config import is_local_mode  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    if not is_local_mode():
        return []
    from nexus.db import make_t3  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    return list(make_t3().reembed_status())  # type: ignore[attr-defined]


def off_profile_collections(
    reader: Any,
    *,
    rows: Iterable[dict] | None = None,
    profile: dict[str, str] | None = None,
    jobs: Callable[[], Iterable[dict]] | None = None,
) -> list[dict]:
    """:func:`judge_off_profile_collections`' rows: what the hint calls."""
    return judge_off_profile_collections(reader, rows=rows, profile=profile, jobs=jobs).rows


def judge_off_profile_collections(
    reader: Any,
    *,
    rows: Iterable[dict] | None = None,
    profile: dict[str, str] | None = None,
    jobs: Callable[[], Iterable[dict]] | None = None,
    read_jobs: bool = False,
) -> OffProfile:
    """:func:`judge_off_profile_rows` over a catalog reader: what the doctor row calls (with ``read_jobs``).

    ``rows`` and ``profile`` are taken when the caller has already read them (the doctor has). The document
    counts the re-entry check needs are read once each, and only if a job makes a superseded row a candidate.
    """
    if profile is None:
        profile = profile_models(reader)
    if not profile:
        return OffProfile()
    rows = list(reader.list_collections()) if rows is None else list(rows)
    counts: dict[bool, dict[str, int]] = {}

    def doc_count(deleted: bool) -> Callable[[str], int]:
        def count(name: str) -> int:
            if deleted not in counts:
                counts[deleted] = reader.collection_doc_counts(include_deleted=deleted)
            return counts[deleted].get(name, 0)
        return count

    return judge_off_profile_rows(
        rows, profile, jobs or local_jobs, doc_count(False), doc_count(True), read_jobs=read_jobs,
    )


def has_off_profile_collection(reader: Any, jobs: Callable[[], Iterable[dict]] | None = None) -> bool:
    """Whether any collection is off the engine's profile, by :func:`off_profile_rows`."""
    return bool(off_profile_collections(reader, jobs=jobs))


def off_profile_hint(
    reader_factory: Callable[[], Any] | None = None,
    jobs_reader: Callable[[], Iterable[dict]] | None = None,
) -> str | None:
    """:data:`REEMBED_HINT` when a collection is off the profile (:func:`off_profile_rows`), else ``None``.

    Best effort: any failure (the engine unreachable, an engine below the profile route) is no hint. The read
    runs on a thread that is abandoned after :data:`HINT_READ_TIMEOUT_S`, so a hung engine cannot hold the
    caller (``nx daemon service start``). ``jobs_reader`` replaces the engine's job read (tests).
    """
    box: list[bool] = []

    def read() -> None:
        try:
            factory = reader_factory
            if factory is None:
                from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

                factory = make_catalog_reader
            reader = factory()
            if reader is not None:
                box.append(has_off_profile_collection(reader, jobs_reader))
        except Exception as exc:  # noqa: BLE001 — a courtesy line: no failure may reach the start command
            _log.debug("reembed_hint_read_failed", error=str(exc))

    worker = threading.Thread(target=read, name="reembed-hint", daemon=True)
    worker.start()
    worker.join(HINT_READ_TIMEOUT_S)
    return REEMBED_HINT if box and box[0] else None
