# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-h33x8.1: per-capability tool-use census over transcript JSONL.

Answers one question and refuses to answer a second: *which capabilities
did this project's sessions actually invoke, and how often* — never
*was that compliant*. Non-use of a capability can be a discipline failure
or a correct rejection, and a census that conflates the two manufactures
compliance debt out of rational choices (see nexus-h33x8.6, where
``nx_answer``'s near-zero use is most likely a latency trade).

Substrate: no hook, no daemon, no new log. Every tool call is already a
``tool_use`` block in the session transcript JSONL that Claude Code
writes under ``~/.claude/projects/<slug>/``. That makes this census
*retroactive* over transcripts already on disk, which a hook-based
counter could never have been.

Layout this walks::

    <project-dir>/<session-id>.jsonl                          orchestrator
    <project-dir>/<session-id>/subagents/agent-*.jsonl        subagents
    <project-dir>/<session-id>/subagents/workflows/*/agent-*.jsonl
    <project-dir>/<session-id>/tool-results/                  NOT transcripts

Subagent transcripts are matched by FILENAME (``agent-*.jsonl``), not by
"lives in a subdirectory". The looser discriminator happens to be safe
today — there are zero stray ``.jsonl`` files under ``tool-results/``
corpus-wide — but it encodes the wrong rule, and the sibling directory it
would sweep up is the one holding persisted tool output.

THE ROLL-UP RULE, stated explicitly because leaving it unstated is what
made the epic's baseline table irreproducible: **a subagent's calls
attribute to its PARENT session.** ``<sid>/subagents/**`` rolls up to
``<sid>``; orchestrator-vs-subagent is a DIMENSION of a session, not a
session boundary. Counting sidechain files as their own sessions changes
Serena from 13/100 to 31/100 while leaving its lifetime total at 464 —
identical call counts under a different denominator — which is exactly
the discrepancy that made the epic's "Serena is in nx_answer's category"
framing unsafe. Lifetime totals are scope-independent; session counts are
not, so every session-count in this module names its scope.

TWO DENOMINATORS, always. ``ALL`` is every measurable session; the
``SUBSTANTIAL`` subset is sessions with at least
``SUBSTANTIAL_THRESHOLD`` calls. nexus-h33x8.5 pre-registers its
prediction against the substantial subset, so a census that emitted only
one denominator would leave that prediction uncheckable against this
module's own output.

The orchestrator/subagent split is load-bearing, not cosmetic: the
epic's sharpest signal is that the SubagentStart preflight gets 15-23x
the compliance of the SessionStart delivery of the same instruction
(``plan_search`` 12 orchestrator calls against 281 subagent calls). A
census that summed those would have hidden the finding that motivated it.

NON-VACUITY. ``UNMEASURABLE`` exists because nexus-nu7fo shipped a guard
whose ``undeclared=0`` was structurally unfalsifiable — it could not
distinguish "nothing was wrong" from "nothing was checked". Here, a zero
that means *measured zero* and a zero that means *measured nothing* are
textually distinct in every output mode, and a run that measured nothing
exits non-zero. SCOPING, stated so a caller does not over-read ``$?``:
the exit code answers "did this run measure anything at all", NOT "is
this corpus healthy". Sessions that carry no tool call are normal (they
are the majority — most transcripts are aborted or conversational), so
they are counted, listed by reason, and reported as a share, but they do
not fail the run. A caller that needs a health threshold must read
``unmeasurable_share``, not the exit status.

Shape borrowed from :mod:`nexus.routing_stats` (``nx hook routing-stats``),
which aggregates a different JSONL log for the same purpose — spotting
inert matchers that never fired.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

__all__ = [
    "CAPABILITIES",
    "CorpusCensus",
    "CorpusDispatchCensus",
    "DispatchRecord",
    "LEDGER_NAME_CHARSET_RE",
    "MISSING_SUBAGENT_TYPE",
    "SessionCensus",
    "SessionDispatchCensus",
    "UNMEASURABLE_EMPTY",
    "UNMEASURABLE_MISSING",
    "UNMEASURABLE_NO_TOOL_USE",
    "UNMEASURABLE_UNPARSEABLE",
    "UNMEASURABLE_UNREADABLE",
    "SUBSTANTIAL_THRESHOLD",
    "census_corpus",
    "census_corpus_dispatches",
    "census_session",
    "census_session_dispatches",
    "classify_tool",
    "count_tool_uses",
    "default_project_dir",
    "dispatches_to_json",
    "find_suspect_dispatch_shaped_blocks",
    "iter_dispatches",
    "iter_tool_use_blocks",
    "render_dispatches_text",
    "render_text",
    "sanitize_dispatch_name",
    "session_transcript_files",
    "to_json",
]

#: MCP tool-name prefix for the nexus server.
NEXUS_MCP_PREFIX = "mcp__plugin_conexus_nexus__"
#: MCP tool-name prefix for the Serena code-intelligence server.
SERENA_MCP_PREFIX = "mcp__plugin_sn_serena"
#: MCP tool-name prefix shared by every conexus-plugin server (nexus,
#: nexus-catalog, sequential-thinking).
CONEXUS_MCP_PREFIX = "mcp__plugin_conexus_"

#: Retrieval tools on the nexus MCP server. The bucket means "reached for
#: retrieval", so the scoped-search variants belong in it alongside the
#: two the epic's baseline counted.
#:
#: HISTORICAL SLICES, all derivable from ``to_json``'s per-tool counts
#: because no two derivations of this bucket have agreed:
#:   epic baseline    138 = search + query
#:   plan audit       184 = search + query + catalog search
#:   this bucket      197 = the above + the three scoped variants
#: The composition is stated rather than the number pinned; a taxonomy
#: bent to reproduce a prior figure would just move the disagreement.
SEARCH_QUERY_TOOLS = frozenset({
    "search",
    "query",
    "search_graph_hop",
    "search_metadata_scoped",
    "search_topic_scoped",
    # RDR-156 Decision 5 (nexus-ubnwk): the fourth combined-query shape.
    "search_aspect_scoped",
})

#: Retrieval tools on the nexus-catalog MCP server. ``search`` is
#: metadata-first document discovery — a retrieval use. ``link_query``
#: is graph traversal and stays in other_nx_mcp.
CATALOG_SEARCH_TOOLS = frozenset({"mcp__plugin_conexus_nexus-catalog__search"})

#: Host tools counted as the baseline denominator — the work every
#: session does regardless of which capabilities it reaches for.
BASELINE_TOOLS = frozenset({"Bash", "Read", "Edit", "Write"})

#: Capability buckets, in report order.
CAPABILITIES: tuple[str, ...] = (
    "skill",
    "agent",
    "serena",
    "nx_answer",
    "search_query",
    "other_nx_mcp",
    "baseline",
    "other",
)

UNMEASURABLE_EMPTY = "empty-transcript"
UNMEASURABLE_UNPARSEABLE = "unparseable-transcript"
UNMEASURABLE_NO_TOOL_USE = "no-tool-use-blocks"
UNMEASURABLE_MISSING = "no-transcript-found"
UNMEASURABLE_UNREADABLE = "unreadable-transcript"

#: A session is "substantial" at or above this many tool calls. The epic
#: derives its stratified figures from this cut (82 of 100 sessions), and
#: nexus-h33x8.5 pre-registers its prediction against that subset.
SUBSTANTIAL_THRESHOLD = 50

#: Filename glob for subagent transcripts.
SUBAGENT_GLOB = "agent-*.jsonl"


def classify_tool(name: str) -> str:
    """Map a tool name to its capability bucket.

    Ordering matters. Serena is tested before the retrieval-tool rule
    because ``mcp__plugin_sn_serena__search_for_pattern`` is a Serena
    use, not a retrieval use; a substring rule would inflate one epic
    signal at the other's expense.
    """
    if name == "Skill":
        return "skill"
    if name == "Agent":
        return "agent"
    if name.startswith(SERENA_MCP_PREFIX):
        return "serena"
    if name in CATALOG_SEARCH_TOOLS:
        return "search_query"
    if name.startswith(NEXUS_MCP_PREFIX):
        tool = name[len(NEXUS_MCP_PREFIX):]
        if tool == "nx_answer":
            return "nx_answer"
        if tool in SEARCH_QUERY_TOOLS:
            return "search_query"
        return "other_nx_mcp"
    if name.startswith(CONEXUS_MCP_PREFIX):
        return "other_nx_mcp"
    if name in BASELINE_TOOLS:
        return "baseline"
    return "other"


def iter_tool_use_blocks(
    records: Iterable[dict[str, Any]],
) -> Iterator[tuple[int, dict[str, Any]]]:
    """Yield ``(record_index, tool_use_block)`` for every tool call.

    The whole block is yielded, ``input`` included, so callers that need
    a call's arguments do not have to re-walk the transcript.
    nexus-h33x8.2 keys on the dispatching ``Agent`` block's
    ``input.subagent_type``; extracting that from a name-only counter
    would have meant duplicating this walk.
    """
    for index, record in enumerate(records):
        if record.get("type") != "assistant":
            continue
        message = record.get("message") or {}
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield index, block


def count_tool_uses(records: Iterable[dict[str, Any]]) -> tuple[dict[str, int], int]:
    """Count ``tool_use`` blocks per tool name across parsed records.

    Pure over already-parsed records so it is unit-testable with
    fixtures and needs no substrate. Returns ``(counts, records_seen)``.
    """
    materialized = list(records)
    counts: Counter[str] = Counter()
    for _index, block in iter_tool_use_blocks(materialized):
        counts[block.get("name") or "<unnamed>"] += 1
    return dict(counts), len(materialized)


def _parse_jsonl(path: pathlib.Path) -> tuple[list[dict[str, Any]], int, bool]:
    """Parse one JSONL file, tolerating truncation.

    Returns ``(records, parse_errors, unreadable)``. ``unreadable`` is
    kept distinct from a parse error so an operator chasing a permission
    problem is not sent to look for malformed JSON.
    """
    records: list[dict[str, Any]] = []
    errors = 0
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return [], 0, True
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            errors += 1
            continue
        if isinstance(record, dict):
            records.append(record)
        else:
            errors += 1
    return records, errors, False


def _last_timestamp(records: Iterable[dict[str, Any]]) -> str | None:
    stamps = [r.get("timestamp") for r in records if isinstance(r.get("timestamp"), str)]
    return max(stamps) if stamps else None


@dataclass
class SessionCensus:
    """One session's counts, split by who made the call."""

    session_id: str
    orchestrator: dict[str, int] = field(default_factory=dict)
    subagent: dict[str, int] = field(default_factory=dict)
    parse_errors: int = 0
    subagent_files: int = 0
    records_seen: int = 0
    last_timestamp: str | None = None
    unmeasurable_reason: str | None = None

    @property
    def measurable(self) -> bool:
        return self.unmeasurable_reason is None

    @property
    def partial(self) -> bool:
        """Measurable, but some lines did not parse. Never silent."""
        return self.measurable and self.parse_errors > 0

    @property
    def total_calls(self) -> int:
        return sum(self.orchestrator.values()) + sum(self.subagent.values())

    @property
    def substantial(self) -> bool:
        """At or above the substantial-session cut. See SUBSTANTIAL_THRESHOLD."""
        return self.total_calls >= SUBSTANTIAL_THRESHOLD


def session_transcript_files(
    project_dir: pathlib.Path, session_id: str
) -> tuple[pathlib.Path | None, list[pathlib.Path], bool]:
    """Locate one session's transcripts.

    Returns ``(orchestrator_or_None, subagent_paths, traversal_error)``.
    Shared so nexus-h33x8.2 can reuse the layout rules — the filename
    scoping and the parent-attribution roll-up — instead of restating
    them, which is the dual-maintenance class this repo's daemon
    lifecycle rule already names as recurring.

    A directory that cannot be listed sets ``traversal_error`` rather
    than raising: a census whose contract is to degrade a session to
    UNMEASURABLE must not die walking a corpus it exists to survive.
    """
    main = project_dir / f"{session_id}.jsonl"
    orchestrator = main if main.is_file() else None

    sub_dir = project_dir / session_id / "subagents"
    subagents: list[pathlib.Path] = []
    traversal_error = False
    try:
        if sub_dir.is_dir():
            subagents = sorted(sub_dir.rglob(SUBAGENT_GLOB))
    except OSError:
        traversal_error = True
    return orchestrator, subagents, traversal_error


def census_session(project_dir: pathlib.Path, session_id: str) -> SessionCensus:
    """Census one session: its orchestrator transcript plus every subagent."""
    main, sub_paths, traversal_error = session_transcript_files(project_dir, session_id)

    orchestrator: Counter[str] = Counter()
    subagent: Counter[str] = Counter()
    errors = 0
    records_seen = 0
    stamps: list[str] = []
    sub_files = 0
    found_any_file = False
    unreadable = traversal_error

    # rglob picks up workflows/<wf-id>/agent-*.jsonl too; the parent
    # session owns all of it (see the roll-up rule in the module docstring).
    for path, bucket in [(main, orchestrator)] + [(p, subagent) for p in sub_paths]:
        if path is None:
            continue
        found_any_file = True
        if bucket is subagent:
            sub_files += 1
        records, errs, could_not_read = _parse_jsonl(path)
        counts, seen = count_tool_uses(records)
        bucket.update(counts)
        errors += errs
        unreadable = unreadable or could_not_read
        records_seen += seen
        if (ts := _last_timestamp(records)) is not None:
            stamps.append(ts)

    census = SessionCensus(
        session_id=session_id,
        orchestrator=dict(orchestrator),
        subagent=dict(subagent),
        parse_errors=errors,
        subagent_files=sub_files,
        records_seen=records_seen,
        last_timestamp=max(stamps) if stamps else None,
    )

    # Reason precedence: no file at all, then nothing parsed, then parsed
    # cleanly but carried no tool call. Each is a different fact about the
    # measurement and collapsing them would hide which one occurred.
    if not found_any_file and not traversal_error:
        census.unmeasurable_reason = UNMEASURABLE_MISSING
    elif census.total_calls > 0:
        census.unmeasurable_reason = None
    elif unreadable:
        census.unmeasurable_reason = UNMEASURABLE_UNREADABLE
    elif records_seen == 0 and errors == 0:
        census.unmeasurable_reason = UNMEASURABLE_EMPTY
    elif records_seen == 0:
        census.unmeasurable_reason = UNMEASURABLE_UNPARSEABLE
    else:
        census.unmeasurable_reason = UNMEASURABLE_NO_TOOL_USE
    return census


@dataclass
class CorpusCensus:
    """Aggregate over the sessions in scope."""

    project_dir: pathlib.Path
    sessions: list[SessionCensus] = field(default_factory=list)
    unmeasurable: list[SessionCensus] = field(default_factory=list)
    scope_error: str | None = None
    filtered_by_since: int = 0

    @property
    def measurable_sessions(self) -> int:
        return len(self.sessions)

    @property
    def unmeasurable_sessions(self) -> int:
        return len(self.unmeasurable)

    @property
    def unmeasurable_by_reason(self) -> dict[str, int]:
        return dict(Counter(s.unmeasurable_reason or "?" for s in self.unmeasurable))

    @property
    def substantial_sessions(self) -> int:
        return sum(1 for s in self.sessions if s.substantial)

    @property
    def unmeasurable_share(self) -> float:
        """Share of in-scope sessions that yielded no measurement.

        The health signal the exit code deliberately does NOT carry.
        """
        total = self.measurable_sessions + self.unmeasurable_sessions
        return (self.unmeasurable_sessions / total) if total else 0.0

    def _in_scope(self, substantial_only: bool) -> list[SessionCensus]:
        return [s for s in self.sessions if s.substantial] if substantial_only else self.sessions

    @property
    def exit_code(self) -> int:
        """Non-zero when the run measured *nothing*.

        Individual unmeasurable sessions are normal — 18 of 100 sessions
        in the epic's baseline carried no tool call at all. What must
        never render as success is a whole run that measured nothing.
        """
        return 0 if self.measurable_sessions else 1

    # -- roll-ups ---------------------------------------------------------

    def _calls(self, scope: str, capability: str, substantial_only: bool = False) -> int:
        total = 0
        for sess in self._in_scope(substantial_only):
            counts = sess.orchestrator if scope == "orchestrator" else sess.subagent
            for name, n in counts.items():
                if classify_tool(name) == capability:
                    total += n
        return total

    def orchestrator_calls(self, capability: str, substantial_only: bool = False) -> int:
        return self._calls("orchestrator", capability, substantial_only)

    def subagent_calls(self, capability: str, substantial_only: bool = False) -> int:
        return self._calls("subagent", capability, substantial_only)

    def total_calls(self, capability: str, substantial_only: bool = False) -> int:
        return (
            self.orchestrator_calls(capability, substantial_only)
            + self.subagent_calls(capability, substantial_only)
        )

    def _sessions_using(self, scope: str, capability: str, substantial_only: bool = False) -> int:
        hits = 0
        for sess in self._in_scope(substantial_only):
            counts = sess.orchestrator if scope == "orchestrator" else sess.subagent
            if any(classify_tool(n) == capability for n in counts):
                hits += 1
        return hits

    def orchestrator_sessions_using(self, capability: str, substantial_only: bool = False) -> int:
        return self._sessions_using("orchestrator", capability, substantial_only)

    def subagent_sessions_using(self, capability: str, substantial_only: bool = False) -> int:
        return self._sessions_using("subagent", capability, substantial_only)

    def any_sessions_using(self, capability: str, substantial_only: bool = False) -> int:
        """Sessions using a capability at EITHER scope — the epic's roll-up rule.

        A session that touched Serena only through a subagent still used
        Serena; subagent calls attribute to the parent session.
        """
        hits = 0
        for sess in self._in_scope(substantial_only):
            names = list(sess.orchestrator) + list(sess.subagent)
            if any(classify_tool(n) == capability for n in names):
                hits += 1
        return hits

    def tools_by_scope(self, scope: str) -> dict[str, int]:
        merged: Counter[str] = Counter()
        for sess in self.sessions:
            merged.update(sess.orchestrator if scope == "orchestrator" else sess.subagent)
        return dict(merged)


def default_project_dir() -> pathlib.Path:
    """Transcript dir for the current working directory.

    Claude Code slugifies the absolute cwd by replacing every character
    that is not alphanumeric with ``-`` — not just ``/``. A dotted username
    (``/Users/hal.hildebrand/git/nexus``) is empirical proof: the on-disk
    directory is ``-Users-hal-hildebrand-git-nexus``, i.e. the ``.`` in
    ``hal.hildebrand`` becomes a ``-`` exactly like every ``/`` does
    (verified via ``ls ~/.claude/projects/``, nexus-bieuw). A prior
    implementation that only replaced ``/`` silently produced the wrong,
    nonexistent path for every dotted-username box, and the pytest-tmp-path
    fixture used to assert this behavior happened to route through
    ``pytest-of-<user>`` on the same dotted username, so it never caught it.
    ``NX_CENSUS_PROJECT_DIR`` overrides, which is what makes the CLI
    testable without touching the real ``~/.claude``.
    """
    override = os.environ.get("NX_CENSUS_PROJECT_DIR")
    if override:
        return pathlib.Path(override)
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(pathlib.Path.cwd().resolve()))
    # KNOWN GAP (nexus-6we44): Claude Code additionally truncates slugs over
    # 200 chars to slug[:200] + "-" + base36-hash(original path). Paths that
    # long will still compute a nonexistent dir here.
    return pathlib.Path.home() / ".claude" / "projects" / slug


def _session_ids(project_dir: pathlib.Path) -> tuple[list[str], bool]:
    """Session ids under ``project_dir``. Second element flags a listing failure."""
    try:
        return sorted(p.stem for p in project_dir.glob("*.jsonl")), False
    except OSError:
        return [], True


def census_corpus(
    project_dir: pathlib.Path,
    *,
    session: str | None = None,
    since: str | None = None,
) -> CorpusCensus:
    """Census every session under ``project_dir`` (or just one).

    ``since`` is an ISO date/timestamp prefix compared against each
    session's latest record timestamp — a session still running counts
    as recent, which is what "since" means for an append-only log.
    """
    result = CorpusCensus(project_dir=project_dir)

    if not project_dir.is_dir():
        result.scope_error = f"project dir not found: {project_dir}"
        return result

    if session:
        ids, listing_failed = [session], False
    else:
        ids, listing_failed = _session_ids(project_dir)
    if listing_failed:
        result.scope_error = f"project dir could not be listed: {project_dir}"
        return result
    if not ids:
        result.scope_error = f"no session transcripts under {project_dir}"
        return result

    filtered_by_since = 0
    for session_id in ids:
        sess = census_session(project_dir, session_id)
        # A session with no timestamp at all is KEPT: --since must never
        # silently drop what it cannot date.
        if since and sess.last_timestamp is not None and sess.last_timestamp < since:
            filtered_by_since += 1
            continue
        (result.sessions if sess.measurable else result.unmeasurable).append(sess)

    result.filtered_by_since = filtered_by_since
    if session and not result.sessions:
        # Distinguish "no such session" from "--since excluded it" — the
        # same empty result, two very different facts for the caller.
        if filtered_by_since:
            result.scope_error = (
                f"session {session!r} exists but its latest record predates --since {since}"
            )
        elif all(
            sess.unmeasurable_reason == UNMEASURABLE_MISSING for sess in result.unmeasurable
        ):
            result.scope_error = f"session not found: {session}"
    return result


# ---------------------------------------------------------------------------
# dispatch recognition (nexus-h33x8.2)
#
# THE DEBT THIS PAYS: nexus-nu7fo recorded four consecutive sessions in
# which the RDR-184 Gap-1 ledger recognised ZERO of N dispatched agents
# (0/6, 0/10, 0/7, 0/2), because the guard keyed on a NAME-MORPHOLOGY
# (``a<name>-<hash>``) the Agent tool has no ``name`` parameter to ever
# produce. This section is the transcript-based recognizer nu7fo's own
# fix option (a) called for: every ``Agent`` tool_use block already
# carries ``input.subagent_type`` and ``input.description`` — verified,
# not assumed, against session 75695009 and pinned by the golden fixture
# below.
#
# SCOPE FENCE (binding, not a suggestion): this module SUPPLIES the
# recognizer. It does not modify ``tests/e2e/lib/expectations.sh``, does
# not change the ledger's verdict logic, and does not decide nu7fo's
# resolution — that bead (closed 2026-08-07, superseded by the
# dispatch-expect hook + the AGENTS.md verbatim-subagent-type convention)
# owns that call.
# ---------------------------------------------------------------------------

#: The ledger's own name charset, mirrored READ-ONLY from
#: ``expectations_expect`` in ``tests/e2e/lib/expectations.sh`` (its "Name
#: charset" comment, current at time of writing). A plugin-namespaced
#: ``subagent_type`` like ``conexus:substantive-critic`` ALREADY satisfies
#: this — that is the point of AGENTS.md's hot-rule convention, "keyed on
#: the subagent type verbatim, colon included — never an invented name"
#: (nexus-nu7fo). This constant exists so the rare value that would NOT
#: satisfy the ledger's charset is caught and reported instead of emitted
#: silently mismatched.
#:
#: DISCREPANCY WITH THE BEAD'S OWN TEXT, stated rather than hidden:
#: nexus-h33x8.2's BUILD section quotes an OLDER, colon-EXCLUDING charset
#: (``^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$``) and says a colon "MUST be
#: sanitized before emission". That was true of the PINNED plugin release
#: at the time the bead was written. nexus-nu7fo's own comment history
#: (2026-08-01, "PREMISE CORRECTION" / retraction / final confirmation)
#: records the colon being ADDED to the live charset (nexus-qc4p1) and the
#: bead closing on 2026-08-07 with the verbatim-with-colon convention
#: adopted. Sanitizing the colon AWAY today would recreate the exact
#: pairing failure the whole nu7fo saga is about — so this module follows
#: the CURRENT charset, verbatim-preserving whenever the raw value already
#: qualifies, and transforms only what the ledger would otherwise reject.
LEDGER_NAME_CHARSET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_:-]{0,63}$")

#: Substituted when an ``Agent`` block carries no ``input.subagent_type``
#: at all. This is NOT an invented placeholder — it is the harness's own
#: documented default. ``conexus/hooks/scripts/agent-dispatch-expect.sh``
#: (nexus-a795d) computes the EXPECT-row name identically:
#:
#:     # subagent_type ABSENT/EMPTY => general-purpose (nexus-a795d).
#:     ...
#:     str(ti.get("subagent_type") or "general-purpose"),
#:
#: because the harness itself starts a ``general-purpose`` agent for an
#: omitted ``subagent_type`` — the dispatch is real, only the field is
#: absent. A recognizer that emitted anything else here (the original
#: ``"missing-subagent-type"`` sentinel this constant used to hold, review
#: finding S1, nexus-h33x8.2 fix round 2 2026-08-08) would key its row
#: DIFFERENTLY from the row the ledger's own hook already writes for the
#: exact same dispatch, breaking "pass ``subagent_type`` straight to
#: ``expectations_expect``" for every one of these rows (28/1304 in the
#: live corpus at last count). Already ledger-charset valid, so it never
#: itself needs sanitizing; ``DispatchRecord`` still flags
#: ``subagent_type_missing`` (and keeps ``subagent_type_raw is None``) so
#: this is never confused with a REAL ``general-purpose`` dispatch that
#: had a properly-populated field.
MISSING_SUBAGENT_TYPE = "general-purpose"


def sanitize_dispatch_name(raw: str) -> tuple[str, bool]:
    """Map a raw ``subagent_type`` to a form the ledger's charset accepts.

    Returns ``(sanitized, changed)``. The common case — a plugin-namespaced
    type such as ``conexus:substantive-critic`` — already matches
    :data:`LEDGER_NAME_CHARSET_RE` and comes back UNCHANGED, verbatim
    colon included; ``changed`` is only ``True`` for a value the ledger's
    ``expectations_expect`` would otherwise reject outright (an empty
    string, a leading non-alphanumeric character, an interior character
    outside the charset, or a string over 64 characters). This function
    never invents a name (nexus-nu7fo's central complaint) — it only
    repairs what would fail validation.
    """
    if LEDGER_NAME_CHARSET_RE.match(raw):
        return raw, False
    scrubbed = re.sub(r"[^A-Za-z0-9_:-]", "-", raw)
    if not scrubbed or not scrubbed[0].isalnum():
        scrubbed = "x" + scrubbed
    return scrubbed[:64], True


@dataclass
class DispatchRecord:
    """One ``Agent`` tool_use block, decoded for the RDR-184 ledger.

    ``session_ordinal`` numbers every Agent dispatch found for the session
    in the same file-then-record order :func:`census_session` walks
    (orchestrator file first, then subagent files). Within the subagent
    files, that order is ``sorted(sub_dir.rglob(...))`` — LEXICOGRAPHIC BY
    FILENAME, not temporal — so a cross-file ``session_ordinal`` sequence
    is a stable display order, not proof of dispatch wall-clock order
    (already the case pre-fix; noted explicitly per review, since the
    ordinal is display-only doctrine anyway — see below). ``type_ordinal``
    numbers only the occurrences of THIS record's sanitized
    ``subagent_type`` — "the Nth dispatch of this type" — which is what
    BUILD's "ordinal is required because the same type is dispatched
    repeatedly" is actually disambiguating.

    NEITHER ordinal is a ledger PAIRING key. ``tests/e2e/lib/
    expectations.sh``'s own "WHY N-OF-TYPE AND NOT AN ORDINAL" comment
    (read-only, not restated here beyond this pointer) establishes that no
    per-instance identifier survives from the PreToolUse payload to the
    SubagentStart payload, so an ordinal invented on this side could never
    be paired against anything the hook writes. The ledger already
    matches N EXPECT rows of a type against N STARTs of that type
    (first-appearance order, credit-consuming); this module's
    contribution is exposing that same N — see
    ``SessionDispatchCensus.type_counts`` — with the ordinals kept as a
    human display aid ("which one was #2") rather than a join key.
    """

    session_id: str
    scope: str  # "orchestrator" | "subagent"
    record_index: int
    session_ordinal: int
    type_ordinal: int
    subagent_type_raw: str | None
    subagent_type_missing: bool
    subagent_type_sanitized: str
    subagent_type_changed: bool
    description: str | None

    def to_dict(self) -> dict[str, Any]:
        """``subagent_type`` here is the SANITIZED, ledger-consumable form —
        what a caller should hand to ``expectations_expect``. The raw
        transcript value is kept alongside for provenance."""
        return {
            "session_id": self.session_id,
            "scope": self.scope,
            "record_index": self.record_index,
            "session_ordinal": self.session_ordinal,
            "type_ordinal": self.type_ordinal,
            "subagent_type": self.subagent_type_sanitized,
            "subagent_type_raw": self.subagent_type_raw,
            "subagent_type_missing": self.subagent_type_missing,
            "subagent_type_sanitized_changed": self.subagent_type_changed,
            "description": self.description,
        }


def iter_dispatches(
    records: list[dict[str, Any]],
    *,
    session_id: str,
    scope: str,
    ordinal_start: int = 1,
    type_counts: Counter[str] | None = None,
) -> list[DispatchRecord]:
    """Decode every ``Agent`` tool_use block in ``records`` into ``DispatchRecord``s.

    Built on :func:`iter_tool_use_blocks` — its ``input`` is what carries
    ``subagent_type`` / ``description``; a name-only counter (like
    :func:`count_tool_uses`) would have discarded exactly what this needs,
    which is why nexus-h33x8.1 exported the richer primitive for this
    module to extend rather than duplicate.

    ``type_counts`` is the running per-type ordinal counter, mutated in
    place. It defaults to a FRESH counter (one call == one file's worth of
    ordinals), which is correct for a single-file caller but WRONG for a
    multi-file session: a caller walking several transcript files for one
    session (:func:`census_session_dispatches`, orchestrator + subagent
    files) must pass the SAME ``Counter`` across every call — exactly as it
    already threads ``ordinal_start`` — or two dispatches of the same type
    in different files both land on ``type_ordinal=1`` instead of ``[1, 2]``
    (review finding I1, nexus-h33x8.2 fix round 2026-08-08).
    """
    out: list[DispatchRecord] = []
    type_seen: Counter[str] = type_counts if type_counts is not None else Counter()
    ordinal = ordinal_start
    for index, block in iter_tool_use_blocks(records):
        if block.get("name") != "Agent":
            continue
        block_input = block.get("input") or {}
        raw = block_input.get("subagent_type")
        missing = raw is None
        sanitized, changed = sanitize_dispatch_name(raw if raw is not None else MISSING_SUBAGENT_TYPE)
        type_seen[sanitized] += 1
        out.append(
            DispatchRecord(
                session_id=session_id,
                scope=scope,
                record_index=index,
                session_ordinal=ordinal,
                type_ordinal=type_seen[sanitized],
                subagent_type_raw=raw,
                subagent_type_missing=missing,
                subagent_type_sanitized=sanitized,
                subagent_type_changed=changed,
                description=block_input.get("description"),
            )
        )
        ordinal += 1
    return out


def find_suspect_dispatch_shaped_blocks(
    records: list[dict[str, Any]], *, scope: str
) -> list[dict[str, Any]]:
    """Flag ``tool_use`` blocks that LOOK like a dispatch but are not named ``Agent``.

    Rider 2 (fix round 2, nexus-h33x8.2, 2026-08-08): the one structurally
    silent drift shape this recognizer cannot detect from its own count
    alone is an Agent-block-specific rename or restructure — every OTHER
    tool_use still parses and counts normally, dispatches quietly stop
    being recognized, and nothing here goes UNMEASURABLE because the
    transcript is perfectly well-formed. This repo has already lived
    through exactly this shape once: ``conexus/hooks/scripts/
    agent-dispatch-expect.sh`` still special-cases ``"Task"`` as "the
    pre-rename spelling of the same tool" alongside ``"Agent"``. A block
    named anything other than ``Agent`` that nonetheless carries
    ``input.subagent_type`` is the same signature — flagged here, NOT
    silently dropped and NOT failing the census (this is a WARNING, not an
    UNMEASURABLE reason; the census still trusts its own Agent-only count).
    """
    suspects: list[dict[str, Any]] = []
    for index, block in iter_tool_use_blocks(records):
        name = block.get("name")
        if name == "Agent":
            continue
        subagent_type = (block.get("input") or {}).get("subagent_type")
        if subagent_type:
            suspects.append(
                {
                    "scope": scope,
                    "record_index": index,
                    "tool_name": name,
                    "subagent_type": subagent_type,
                }
            )
    return suspects


@dataclass
class SessionDispatchCensus:
    """One session's recognized Agent dispatches."""

    session_id: str
    dispatches: list[DispatchRecord] = field(default_factory=list)
    suspect_blocks: list[dict[str, Any]] = field(default_factory=list)
    parse_errors: int = 0
    subagent_files: int = 0
    records_seen: int = 0
    last_timestamp: str | None = None
    unmeasurable_reason: str | None = None

    @property
    def measurable(self) -> bool:
        return self.unmeasurable_reason is None

    @property
    def partial(self) -> bool:
        return self.measurable and self.parse_errors > 0

    @property
    def total_dispatches(self) -> int:
        return len(self.dispatches)

    @property
    def type_counts(self) -> dict[str, int]:
        """N-of-type per SANITIZED subagent_type.

        This is what the ledger's N-of-type credit matching
        (``tests/e2e/lib/expectations.sh``) actually consumes — NOT the
        ordinal, see :class:`DispatchRecord`'s docstring for why an
        ordinal cannot be a pairing key in this harness.
        """
        return dict(Counter(d.subagent_type_sanitized for d in self.dispatches))

    @property
    def sanitize_collisions(self) -> list[dict[str, Any]]:
        """Sanitized names produced by more than one DISTINCT raw value.

        Collision behavior: rows are never silently merged. Two different
        raw ``subagent_type`` strings folding to the same ledger key would
        otherwise let a downstream consumer double-credit two different
        agent types as if they were one; this surfaces the fact instead so
        it can be inspected. Two dispatches sharing the SAME raw value
        (the common, expected case — a type dispatched more than once) is
        not a collision, it is exactly what ``type_ordinal`` disambiguates.
        """
        by_sanitized: dict[str, set[str]] = {}
        for d in self.dispatches:
            raw_key = d.subagent_type_raw if d.subagent_type_raw is not None else MISSING_SUBAGENT_TYPE
            by_sanitized.setdefault(d.subagent_type_sanitized, set()).add(raw_key)
        return [
            {"sanitized": sanitized, "raw_values": sorted(raws)}
            for sanitized, raws in sorted(by_sanitized.items())
            if len(raws) > 1
        ]


def census_session_dispatches(project_dir: pathlib.Path, session_id: str) -> SessionDispatchCensus:
    """Census one session's Agent dispatches.

    Reuses :func:`session_transcript_files` rather than restating the
    file-walk or the parent-attribution roll-up — nexus-h33x8.1 exported
    it precisely so this bead would not duplicate it (the dual-maintenance
    class this repo's daemon-lifecycle hot rule already names as
    recurring). The UNMEASURABLE-reason precedence mirrors
    :func:`census_session` exactly, substituting "found ANY tool_use
    block" for "found ANY dispatch" as the measurability signal — a
    session that used other tools but dispatched nothing is a MEASURED
    zero dispatch count, not a fourth unmeasurable reason.
    """
    main, sub_paths, traversal_error = session_transcript_files(project_dir, session_id)

    dispatches: list[DispatchRecord] = []
    errors = 0
    records_seen = 0
    total_tool_use_blocks = 0
    stamps: list[str] = []
    sub_files = 0
    found_any_file = False
    unreadable = traversal_error
    ordinal = 1
    # SAME counter across every file in the session, mirroring ordinal_start
    # above — a type dispatched once in the orchestrator file and again in a
    # subagent file must land on type_ordinal [1, 2], not [1, 1] (review
    # finding I1, nexus-h33x8.2 fix round 2026-08-08). A fresh Counter per
    # file would silently produce duplicate (subagent_type, type_ordinal)
    # keys within one session.
    type_counts: Counter[str] = Counter()
    suspect_blocks: list[dict[str, Any]] = []

    for path, scope in [(main, "orchestrator")] + [(p, "subagent") for p in sub_paths]:
        if path is None:
            continue
        found_any_file = True
        if scope == "subagent":
            sub_files += 1
        records, errs, could_not_read = _parse_jsonl(path)
        total_tool_use_blocks += sum(1 for _ in iter_tool_use_blocks(records))
        decoded = iter_dispatches(
            records,
            session_id=session_id,
            scope=scope,
            ordinal_start=ordinal,
            type_counts=type_counts,
        )
        dispatches.extend(decoded)
        ordinal += len(decoded)
        # Rider 2: warn-only drift signal, never gates measurability.
        suspect_blocks.extend(find_suspect_dispatch_shaped_blocks(records, scope=scope))
        errors += errs
        unreadable = unreadable or could_not_read
        records_seen += len(records)
        if (ts := _last_timestamp(records)) is not None:
            stamps.append(ts)

    census = SessionDispatchCensus(
        session_id=session_id,
        dispatches=dispatches,
        suspect_blocks=suspect_blocks,
        parse_errors=errors,
        subagent_files=sub_files,
        records_seen=records_seen,
        last_timestamp=max(stamps) if stamps else None,
    )

    # Same precedence as census_session: no file at all, then "found some
    # signal", then unreadable, then empty/unparseable/no-tool-use. Each is
    # a different fact about the measurement.
    if not found_any_file and not traversal_error:
        census.unmeasurable_reason = UNMEASURABLE_MISSING
    elif total_tool_use_blocks > 0:
        census.unmeasurable_reason = None
    elif unreadable:
        census.unmeasurable_reason = UNMEASURABLE_UNREADABLE
    elif records_seen == 0 and errors == 0:
        census.unmeasurable_reason = UNMEASURABLE_EMPTY
    elif records_seen == 0:
        census.unmeasurable_reason = UNMEASURABLE_UNPARSEABLE
    else:
        census.unmeasurable_reason = UNMEASURABLE_NO_TOOL_USE
    return census


@dataclass
class CorpusDispatchCensus:
    """Aggregate dispatch recognition over the sessions in scope."""

    project_dir: pathlib.Path
    sessions: list[SessionDispatchCensus] = field(default_factory=list)
    unmeasurable: list[SessionDispatchCensus] = field(default_factory=list)
    scope_error: str | None = None

    @property
    def measurable_sessions(self) -> int:
        return len(self.sessions)

    @property
    def unmeasurable_sessions(self) -> int:
        return len(self.unmeasurable)

    @property
    def unmeasurable_by_reason(self) -> dict[str, int]:
        return dict(Counter(s.unmeasurable_reason or "?" for s in self.unmeasurable))

    @property
    def exit_code(self) -> int:
        """Non-zero only when the run measured *nothing* — see census_corpus."""
        return 0 if self.measurable_sessions else 1

    @property
    def total_dispatches(self) -> int:
        return sum(s.total_dispatches for s in self.sessions)

    @property
    def type_counts(self) -> dict[str, int]:
        totals: Counter[str] = Counter()
        for sess in self.sessions:
            totals.update(sess.type_counts)
        return dict(totals)

    @property
    def sanitize_collisions(self) -> list[dict[str, Any]]:
        """Corpus-wide collisions, RECOMPUTED over every dispatch's raw
        value — not merged from each session's own (already-filtered)
        collision list.

        Merging per-session collisions would miss a cross-session
        collision: session A contributing only ``"weird!type"`` and
        session B only ``"weird?type"`` each shows ZERO collisions on its
        own (one raw value per sanitized key, within that session), yet
        both fold to the same corpus-level ``type_counts`` key. Review
        finding M2, nexus-h33x8.2 fix round 2026-08-08.
        """
        by_sanitized: dict[str, set[str]] = {}
        for sess in self.sessions:
            for d in sess.dispatches:
                raw_key = (
                    d.subagent_type_raw if d.subagent_type_raw is not None else MISSING_SUBAGENT_TYPE
                )
                by_sanitized.setdefault(d.subagent_type_sanitized, set()).add(raw_key)
        return [
            {"sanitized": k, "raw_values": sorted(v)}
            for k, v in sorted(by_sanitized.items())
            if len(v) > 1
        ]

    @property
    def suspect_blocks_total(self) -> int:
        """Corpus-wide count of Rider-2 drift-signal blocks — see
        :func:`find_suspect_dispatch_shaped_blocks`. Warn-only; never
        affects ``exit_code``."""
        return sum(len(s.suspect_blocks) for s in self.sessions)


def census_corpus_dispatches(
    project_dir: pathlib.Path, *, session: str | None = None
) -> CorpusDispatchCensus:
    """Census every session's Agent dispatches under ``project_dir`` (or just one).

    Scoping and ``scope_error`` precedence mirror :func:`census_corpus`.
    """
    result = CorpusDispatchCensus(project_dir=project_dir)

    if not project_dir.is_dir():
        result.scope_error = f"project dir not found: {project_dir}"
        return result

    if session:
        ids, listing_failed = [session], False
    else:
        ids, listing_failed = _session_ids(project_dir)
    if listing_failed:
        result.scope_error = f"project dir could not be listed: {project_dir}"
        return result
    if not ids:
        result.scope_error = f"no session transcripts under {project_dir}"
        return result

    for session_id in ids:
        sess = census_session_dispatches(project_dir, session_id)
        (result.sessions if sess.measurable else result.unmeasurable).append(sess)

    if session and not result.sessions:
        if all(s.unmeasurable_reason == UNMEASURABLE_MISSING for s in result.unmeasurable):
            result.scope_error = f"session not found: {session}"
    return result


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

_VERDICT_REFUSAL = (
    "Counts only — this census emits no compliance verdict. Non-use of a "
    "capability may be a forgotten affordance or a correct rejection, and "
    "nothing here distinguishes them (nexus-h33x8.1 HARD REQUIREMENT)."
)


def render_text(result: CorpusCensus) -> str:
    """Human-readable report. Measured zero and measured nothing differ."""
    lines: list[str] = []

    if result.scope_error:
        lines.append(f"UNMEASURABLE: {result.scope_error}")
        lines.append("")
        lines.append(_VERDICT_REFUSAL)
        return "\n".join(lines) + "\n"

    n = result.measurable_sessions
    m = result.substantial_sessions
    lines.append(f"Capability census — {result.project_dir}")
    lines.append(
        f"{n} measurable session(s); zeros below are MEASURED zeros. "
        f"Subagent calls roll up to their PARENT session."
    )
    lines.append("")

    for label, substantial in (
        (f"ALL MEASURABLE SESSIONS (n={n})", False),
        (f"SUBSTANTIAL SESSIONS — >={SUBSTANTIAL_THRESHOLD} calls (n={m})", True),
    ):
        lines.append(label)
        header = (
            f"{'capability':<14} {'sess':>5} {'orch sess':>9} {'orch calls':>10} "
            f"{'sub sess':>8} {'sub calls':>9} {'total':>8}"
        )
        lines.append(header)
        lines.append("-" * len(header))
        for cap in CAPABILITIES:
            lines.append(
                f"{cap:<14} {result.any_sessions_using(cap, substantial):>5d} "
                f"{result.orchestrator_sessions_using(cap, substantial):>9d} "
                f"{result.orchestrator_calls(cap, substantial):>10d} "
                f"{result.subagent_sessions_using(cap, substantial):>8d} "
                f"{result.subagent_calls(cap, substantial):>9d} "
                f"{result.total_calls(cap, substantial):>8d}"
            )
        lines.append("")

    lines.append(
        "The 'sess' column counts sessions using the capability at EITHER scope; "
        "'orch sess' and 'sub sess' split it. Session counts are scope-dependent, "
        "call counts are not."
    )
    lines.append("")

    partial = [s for s in result.sessions if s.partial]
    if partial:
        lines.append(
            f"PARTIAL: {len(partial)} measured session(s) had unparseable lines "
            f"({sum(s.parse_errors for s in partial)} line(s) skipped)."
        )

    if result.filtered_by_since:
        lines.append(f"FILTERED: {result.filtered_by_since} session(s) excluded by --since.")

    if result.unmeasurable:
        lines.append(
            f"UNMEASURABLE: {result.unmeasurable_sessions} session(s) yielded no "
            f"measurement — NOT a zero ({result.unmeasurable_share:.1%} of sessions "
            "in scope; this share is a health signal the exit code does not carry):"
        )
        for reason, count in sorted(result.unmeasurable_by_reason.items()):
            lines.append(f"    {reason:<24} {count:>4d}")
    lines.append("")
    lines.append(_VERDICT_REFUSAL)
    return "\n".join(lines) + "\n"


def to_json(result: CorpusCensus) -> str:
    """Machine-readable report, carrying per-tool counts.

    Per-tool detail is what keeps a narrower slice (say, the epic's
    ``search`` + ``query`` = 138) derivable from a run whose bucket is
    deliberately wider.
    """
    payload: dict[str, Any] = {
        "project_dir": str(result.project_dir),
        "scope_error": result.scope_error,
        "measurable_sessions": result.measurable_sessions,
        "unmeasurable_sessions": result.unmeasurable_sessions,
        "unmeasurable_by_reason": result.unmeasurable_by_reason,
        "exit_code": result.exit_code,
        "verdict": None,
        "verdict_refusal": _VERDICT_REFUSAL,
        "substantial_threshold": SUBSTANTIAL_THRESHOLD,
        "substantial_sessions": result.substantial_sessions,
        "unmeasurable_share": result.unmeasurable_share,
        "filtered_by_since": result.filtered_by_since,
        "rollup_rule": (
            "subagent calls attribute to the PARENT session; "
            "orchestrator-vs-subagent is a dimension, not a session boundary"
        ),
        "capabilities": {
            denom: {
                "orchestrator": {
                    cap: {
                        "sessions": result.orchestrator_sessions_using(cap, sub),
                        "calls": result.orchestrator_calls(cap, sub),
                    }
                    for cap in CAPABILITIES
                },
                "subagent": {
                    cap: {
                        "sessions": result.subagent_sessions_using(cap, sub),
                        "calls": result.subagent_calls(cap, sub),
                    }
                    for cap in CAPABILITIES
                },
                "any_scope": {
                    cap: result.any_sessions_using(cap, sub) for cap in CAPABILITIES
                },
            }
            for denom, sub in (("all", False), ("substantial", True))
        },
        "tools": {
            "orchestrator": result.tools_by_scope("orchestrator"),
            "subagent": result.tools_by_scope("subagent"),
        },
        "sessions": [
            {
                "session_id": s.session_id,
                "orchestrator_calls": sum(s.orchestrator.values()),
                "subagent_calls": sum(s.subagent.values()),
                "subagent_files": s.subagent_files,
                "parse_errors": s.parse_errors,
                "partial": s.partial,
                "substantial": s.substantial,
                "last_timestamp": s.last_timestamp,
            }
            for s in result.sessions
        ],
        "unmeasurable": [
            {
                "session_id": s.session_id,
                "reason": s.unmeasurable_reason,
                "parse_errors": s.parse_errors,
                "subagent_files": s.subagent_files,
            }
            for s in result.unmeasurable
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# dispatch recognition — rendering (nexus-h33x8.2)
# ---------------------------------------------------------------------------

_DISPATCH_VERDICT_REFUSAL = (
    "Recognized dispatches only. This does NOT verify EXPECT rows were "
    "written, does NOT compute undeclared/BLINDSPOT, and does not modify "
    "or replace tests/e2e/lib/expectations.sh (nexus-h33x8.2 scope fence "
    "— that ledger's own resolution is nexus-nu7fo's call). 'subagent_type' "
    "below is the SANITIZED, ledger-consumable form; a '*' marks a row "
    "where sanitization changed the raw transcript value."
)


def render_dispatches_text(result: CorpusDispatchCensus) -> str:
    """Human-readable dispatch report. Measured zero and measured nothing differ."""
    lines: list[str] = []

    if result.scope_error:
        lines.append(f"UNMEASURABLE: {result.scope_error}")
        lines.append("")
        lines.append(_DISPATCH_VERDICT_REFUSAL)
        return "\n".join(lines) + "\n"

    n = result.measurable_sessions
    lines.append(f"Dispatch census — {result.project_dir}")
    lines.append(
        f"{n} measurable session(s); {result.total_dispatches} Agent dispatch(es) "
        "recognised in total. Subagent dispatches roll up to their PARENT session."
    )
    if result.suspect_blocks_total:
        lines.append(
            f"WARNING: {result.suspect_blocks_total} non-Agent tool_use block(s) carrying "
            "input.subagent_type across the corpus — see per-session WARNING lines below."
        )
    lines.append("")

    for sess in result.sessions:
        lines.append(f"session {sess.session_id}  ({sess.total_dispatches} dispatch(es))")
        if sess.dispatches:
            header = f"{'ord':>4} {'type-ord':>8} {'scope':<11} subagent_type"
            lines.append(header)
            lines.append("-" * len(header))
            for d in sess.dispatches:
                marker = "*" if d.subagent_type_changed else " "
                desc = f"  # {d.description}" if d.description else ""
                lines.append(
                    f"{d.session_ordinal:>4} {d.type_ordinal:>8} {d.scope:<11} "
                    f"{marker}{d.subagent_type_sanitized}{desc}"
                )
            lines.append("")
            lines.append("  N-of-type (what the ledger's credit matching consumes):")
            for t, c in sorted(sess.type_counts.items()):
                lines.append(f"    {t:<48} {c:>3}")
            if sess.sanitize_collisions:
                lines.append("  SANITIZE COLLISIONS (distinct raw values folded to one key):")
                for collision in sess.sanitize_collisions:
                    lines.append(
                        f"    {collision['sanitized']}: {', '.join(collision['raw_values'])}"
                    )
        else:
            lines.append("  (no Agent dispatch — measured zero, not unmeasurable)")
        if sess.suspect_blocks:
            lines.append(
                f"  WARNING: {len(sess.suspect_blocks)} non-Agent tool_use block(s) carried "
                "input.subagent_type — possible Agent-block rename/drift, NOT counted above:"
            )
            for suspect in sess.suspect_blocks:
                lines.append(
                    f"    scope={suspect['scope']} record={suspect['record_index']} "
                    f"name={suspect['tool_name']!r} subagent_type={suspect['subagent_type']!r}"
                )
        lines.append("")

    # Rider 1 (fix round 2, nexus-h33x8.2, 2026-08-08): without this block a
    # corrupted-tail transcript silently read as a clean, lower dispatch
    # count in text mode — the same fact JSON already carries via
    # ``partial``/``parse_errors`` on each session, just never rendered here.
    partial = [s for s in result.sessions if s.partial]
    if partial:
        lines.append(
            f"PARTIAL: {len(partial)} measured session(s) had unparseable lines "
            f"({sum(s.parse_errors for s in partial)} line(s) skipped) — the "
            "dispatch count above may be an undercount, not a clean total."
        )
        lines.append("")

    if result.unmeasurable:
        lines.append(
            f"UNMEASURABLE: {result.unmeasurable_sessions} session(s) yielded no "
            "measurement — NOT a zero:"
        )
        for reason, count in sorted(result.unmeasurable_by_reason.items()):
            lines.append(f"    {reason:<24} {count:>4d}")
        lines.append("")

    lines.append(_DISPATCH_VERDICT_REFUSAL)
    return "\n".join(lines) + "\n"


def dispatches_to_json(result: CorpusDispatchCensus) -> str:
    """Machine-readable dispatch report.

    Each row's ``subagent_type`` is the SANITIZED, ledger-consumable
    form — pass it straight to ``expectations_expect``. ``type_counts``
    (per session and aggregate) is the N-of-type figure the ledger's own
    credit matching actually consumes; ordinals are exposed for display
    only, see :class:`DispatchRecord`.
    """
    payload: dict[str, Any] = {
        "project_dir": str(result.project_dir),
        "scope_error": result.scope_error,
        "measurable_sessions": result.measurable_sessions,
        "unmeasurable_sessions": result.unmeasurable_sessions,
        "unmeasurable_by_reason": result.unmeasurable_by_reason,
        "exit_code": result.exit_code,
        "total_dispatches": result.total_dispatches,
        "type_counts": result.type_counts,
        "sanitize_collisions": result.sanitize_collisions,
        "suspect_blocks_total": result.suspect_blocks_total,
        "ledger_name_charset": LEDGER_NAME_CHARSET_RE.pattern,
        "verdict_refusal": _DISPATCH_VERDICT_REFUSAL,
        "sessions": [
            {
                "session_id": s.session_id,
                "total_dispatches": s.total_dispatches,
                "type_counts": s.type_counts,
                "sanitize_collisions": s.sanitize_collisions,
                "suspect_blocks": s.suspect_blocks,
                "subagent_files": s.subagent_files,
                "parse_errors": s.parse_errors,
                "partial": s.partial,
                "last_timestamp": s.last_timestamp,
                "dispatches": [d.to_dict() for d in s.dispatches],
            }
            for s in result.sessions
        ],
        "unmeasurable": [
            {
                "session_id": s.session_id,
                "reason": s.unmeasurable_reason,
                "parse_errors": s.parse_errors,
                "subagent_files": s.subagent_files,
            }
            for s in result.unmeasurable
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)
