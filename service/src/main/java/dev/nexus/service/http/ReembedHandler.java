// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.ReembedJobRepository;
import dev.nexus.service.db.ReembedJobRepository.Job;
import dev.nexus.service.db.ReembedJobRepository.JobLockHeld;
import dev.nexus.service.db.ReembedJobRepository.LockContention;
import dev.nexus.service.db.ReembedJobRepository.Transition;
import dev.nexus.service.db.ReembedSourceReader;
import dev.nexus.service.reembed.ReembedStateMachine;
import dev.nexus.service.reembed.ReembedStateMachine.Applied;
import dev.nexus.service.reembed.ReembedStateMachine.Event;
import dev.nexus.service.reembed.ReembedStateMachine.Refused;
import dev.nexus.service.reembed.ReembedStateMachine.State;
import dev.nexus.service.reembed.ReembedStateMachine.Unchanged;
import dev.nexus.service.reembed.TargetSelection;
import dev.nexus.service.reembed.TargetSelection.Absent;
import dev.nexus.service.reembed.TargetSelection.CollectionRow;
import dev.nexus.service.reembed.TargetSelection.Live;
import dev.nexus.service.reembed.TargetSelection.LockHolder;
import dev.nexus.service.reembed.TargetSelection.Refuse;
import dev.nexus.service.reembed.TargetSelection.Result;
import dev.nexus.service.reembed.TargetSelection.Revive;
import dev.nexus.service.reembed.TargetSelection.Superseded;
import dev.nexus.service.reembed.TargetSelection.UseLive;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.time.OffsetDateTime;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.function.Predicate;

/**
 * RDR-210 Phase 6 Step 2 (bead nexus-f98bg): the control routes of the cross-model re-embed job, on
 * {@code /v1/vectors/reembed} (the longest-prefix context, so it wins over {@code /v1/vectors}).
 *
 * <ul>
 *   <li>{@code POST /plan} {@code {source_collection, target_collection?}}: what a job on the source would do, with
 *       every refusal a start would make, writing nothing (200). The plan states the chosen target and what would
 *       happen to it ({@code existing}, {@code revive} or {@code register}), the documents and chunks it moves, the
 *       token estimate, and the counts the source's delete would destroy (RDR-210 Cutover).</li>
 *   <li>{@code GET /status}: every job row of the tenant, newest first, as {@code {"jobs": [...]}}.</li>
 *   <li>{@code POST /jobs} {@code {source_collection, target_collection?, token_cap?, keep_source?, force_delete?}}:
 *       plan the target, then start the job (201 with the job row).</li>
 *   <li>{@code POST /jobs/{id}/pause}, {@code /resume} ({@code {token_cap?}}), {@code /abort}: 200 with the job
 *       row; a repeat of what the job already is answers 200 with {@code "unchanged": true} and touches nothing; a
 *       transition the state machine refuses is 409 naming the state.</li>
 * </ul>
 *
 * <p>The job row is flat snake_case JSON. A body carries collection names ({@code source_collection},
 * {@code target_collection}) and no tenant: the tenant comes only from {@link RequestContext#tenant()}, the
 * authenticated one.
 *
 * <p><b>Local engine only.</b> The routes are registered always and answer {@code 503} naming local mode when the
 * engine has no runner ({@code NX_ENGINE_LOCAL} unset, or no embedder), never a 404: a missing capability is a
 * refusal. {@code unavailable} is that message, null when the runner exists.
 *
 * <p><b>Choosing the target</b> ({@link TargetSelection} over the tenant's collection rows, superseded ones
 * included), for the source's content type and owner and the tenant's profile model for that content type:
 * <ul>
 *   <li>none exists: register {@code target_collection} through the profile-gated upsert (the gate fixes the
 *       model), then check that the registered row is a live collection of exactly that tuple, else 409. A name
 *       that is already registered, and so cannot be a fresh tuple-conformant row, is refused before anything is
 *       written, so a registration is never repointed;</li>
 *   <li>a live row exists: use it ({@code target_collection} is ignored);</li>
 *   <li>a superseded row naming this source as its successor: revive it (the upsert clears the supersession), then
 *       mark the earlier {@code done} or {@code aborted} job that went the other way {@code revived}. The mark is
 *       made before the start and also when the target is found live, and it is idempotent, so a revive whose start
 *       or mark failed is healed by the retry;</li>
 *   <li>otherwise 409 naming the row, its successor, or the job that holds it.</li>
 * </ul>
 * The refusals that need no write all run before the first write. The source lock is taken by the start itself; a
 * start that loses it to a concurrent start after a registration or a revive leaves that in place and answers 409
 * naming the winner.
 *
 * <p><b>Resume</b> applies the transition and wakes the runner. The profile re-check "on every entry into running"
 * is the runner's, at the head of every drive of a running job, so a resume into a changed profile answers 200
 * {@code running} and the job is failed ({@code profile_changed}) by the drive the wake-up starts. A
 * {@code token_cap} on a resume may only raise the job's cap, and only takes effect when the resume applies.
 */
public final class ReembedHandler implements HttpHandler {

    public static final String PATH = "/v1/vectors/reembed";

    private static final Logger log = LoggerFactory.getLogger(ReembedHandler.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() { };

    private static final Set<String> START_FIELDS = Set.of(
        "source_collection", "target_collection", "token_cap", "keep_source", "force_delete");
    private static final Set<String> PLAN_FIELDS = Set.of("source_collection", "target_collection");
    private static final Set<String> RESUME_FIELDS = Set.of("token_cap");
    private static final Set<String> NO_FIELDS = Set.of();

    /** How many times a control request re-reads a job whose state changed between its read and its update. */
    private static final int CONTROL_ATTEMPTS = 3;

    private final ReembedJobRepository jobs;
    private final CatalogRepository catalog;
    private final ReembedSourceReader reader;
    private final String unavailable;
    private final Runnable wakeRunner;

    /**
     * @param reader      counts the plan states ({@link ReembedSourceReader#planCounts})
     * @param unavailable null when a runner exists; else the message of the 503 every route answers
     * @param wakeRunner  asks the runner to run now ({@code NexusService#kickReembed}); called after a start or a
     *                    resume has committed
     */
    public ReembedHandler(ReembedJobRepository jobs, CatalogRepository catalog, ReembedSourceReader reader,
                          String unavailable, Runnable wakeRunner) {
        this.jobs = jobs;
        this.catalog = catalog;
        this.reader = reader;
        this.unavailable = unavailable;
        this.wakeRunner = wakeRunner;
    }

    /** A complete response, thrown to stop a request where a refusal is decided. No stack: it is control flow. */
    private static final class Reply extends RuntimeException {
        private final int status;
        private final String json;

        Reply(int status, String json) {
            super(json, null, false, false);
            this.status = status;
            this.json = json;
        }
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        String tenant = RequestContext.tenant();
        if (tenant == null) {
            HttpUtil.send(exchange, 500, "{\"error\":\"internal: tenant not set\"}");
            return;
        }
        if (unavailable != null) {
            HttpUtil.send(exchange, 503, MAPPER.writeValueAsString(Map.of("error", unavailable)));
            return;
        }
        String op = exchange.getRequestURI().getPath().substring(PATH.length());
        String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);
        try {
            if (op.equals("/jobs")) {
                requirePost(method);
                start(exchange, tenant);
            } else if (op.equals("/plan")) {
                requirePost(method);
                plan(exchange, tenant);
            } else if (op.equals("/status")) {
                if (!"GET".equals(method)) {
                    throw new Reply(405, "{\"error\":\"method not allowed\"}");
                }
                status(exchange, tenant);
            } else if (op.startsWith("/jobs/")) {
                String[] parts = op.substring("/jobs/".length()).split("/", -1);
                Event event = parts.length == 2 ? controlEvent(parts[1]) : null;
                if (event == null) {
                    throw new Reply(404, error("not found: " + op));
                }
                requirePost(method);
                control(exchange, tenant, parseJobId(parts[0]), event);
            } else {
                throw new Reply(404, error("not found: " + op));
            }
        } catch (Reply r) {
            HttpUtil.send(exchange, r.status, r.json);
        } catch (JsonProcessingException e) {
            HttpUtil.send(exchange, 400, error("malformed JSON body: " + e.getOriginalMessage()));
        } catch (IllegalArgumentException e) {
            HttpUtil.send(exchange, 400, error(e.getMessage()));
        } catch (JobLockHeld e) {
            HttpUtil.send(exchange, 409, held(e.getMessage(), e.existingJobId(), e.existingState()));
        } catch (LockContention e) {
            exchange.getResponseHeaders().set("Retry-After", "1");
            HttpUtil.send(exchange, 409, MAPPER.writeValueAsString(orderedMap(
                "error", e.getMessage(), "retryable", true)));
        } catch (CatalogRepository.EmbeddingProfileConflictException e) {
            HttpUtil.send(exchange, 422, error(e.getMessage()));
        } catch (Exception e) {
            if (!HttpUtil.sendTypedDbError(exchange, e, log, "reembed_handler", "op=" + op + " tenant=" + tenant)) {
                log.error("event=reembed_handler_error op={} tenant={} error={}", op, tenant, e.getMessage(), e);
                HttpUtil.send(exchange, 500, "{\"error\":\"internal server error\"}");
            }
        }
    }

    private static void requirePost(String method) {
        if (!"POST".equals(method)) {
            throw new Reply(405, "{\"error\":\"method not allowed\"}");
        }
    }

    private static Event controlEvent(String verb) {
        return switch (verb) {
            case "pause" -> Event.PAUSE;
            case "resume" -> Event.RESUME;
            case "abort" -> Event.ABORT;
            default -> null;
        };
    }

    private static UUID parseJobId(String raw) {
        try {
            return UUID.fromString(raw);
        } catch (IllegalArgumentException e) {
            throw new IllegalArgumentException("malformed job id: " + raw);
        }
    }

    // ── GET /status ──────────────────────────────────────────────────────────

    private void status(HttpExchange exchange, String tenant) throws IOException {
        List<Map<String, Object>> rows = new java.util.ArrayList<>();
        for (Job j : jobs.listAll(tenant)) {
            rows.add(row(j));
        }
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(orderedMap("jobs", rows)));
    }

    // ── POST /plan, POST /jobs ───────────────────────────────────────────────

    /**
     * What a start decides before it writes: the source's row, the profile model, the target the job would use and
     * what would happen to it. Every refusal a start makes without writing is made here ({@link Reply}), so the plan
     * and the start refuse identically.
     *
     * @param decision {@link UseLive}, {@link Revive} or {@link Absent}'s outcome (neither of those: a register)
     */
    private record Resolved(String source, String sourceModel, String contentType, String owner,
                            String profileModel, TargetSelection.Decision decision) {
    }

    private Resolved resolve(String tenant, String source, String requestedTarget) throws JsonProcessingException {
        if (source.equals(requestedTarget)) {
            throw new IllegalArgumentException("target_collection must differ from source_collection");
        }

        // The source's lock, first, so a resent start or a start on a failed job's source answers with the job it
        // met and nothing is read or written for it. The repository's start re-checks inside its transaction.
        List<Job> locked = jobs.listByStates(tenant, ReembedJobRepository.LOCK_STATES);
        Job onSource = firstMatch(locked, j -> j.sourceCollection().equals(source) || j.targetCollection().equals(source));
        if (onSource != null) {
            throw heldBy(onSource, onSource.sourceCollection().equals(source)
                ? "source " + source + " is held by"
                : "source " + source + " is the target of");
        }

        Map<String, Object> srcRow = catalog.getCollection(tenant, source);
        if (srcRow == null) {
            throw new Reply(404, error("source collection " + source + " is not registered"));
        }
        String contentType = String.valueOf(srcRow.get("content_type"));
        String owner = String.valueOf(srcRow.get("owner_id"));
        String successor = String.valueOf(srcRow.get("superseded_by"));
        if (!successor.isEmpty()) {
            throw new Reply(409, MAPPER.writeValueAsString(orderedMap(
                "error", "source collection " + source + " is already superseded by " + successor
                    + "; a superseded collection is not re-embedded",
                "collection", source, "superseded_by", successor)));
        }
        String profileModel = profileModel(tenant, contentType);
        if (profileModel == null) {
            throw new Reply(409, error("the tenant has no embedding profile for content type " + contentType
                + "; there is no model to re-embed " + source + " into"));
        }
        String sourceModel = String.valueOf(srcRow.get("embedding_model"));
        if (profileModel.equals(sourceModel)) {
            throw new Reply(409, error("source collection " + source + " already holds the profile model "
                + profileModel + " for content type " + contentType + "; nothing to re-embed"));
        }

        Result found = TargetSelection.select(rows(tenant, contentType), contentType, owner, profileModel);
        String candidate = switch (found) {
            case Live l -> l.name();
            case Superseded s -> s.name();
            case Absent a -> null;
        };
        LockHolder holder = candidate == null ? null
            : Optional.ofNullable(firstMatch(locked, j -> j.sourceCollection().equals(candidate)))
                .map(j -> new LockHolder(j.jobId(), j.state())).orElse(null);

        TargetSelection.Decision decision = TargetSelection.decide(found, source, holder);
        if (decision instanceof Refuse r) {
            Map<String, Object> refusal = orderedMap("error", r.reason(), "collection", candidate);
            if (holder != null) {
                refusal.put("job_id", holder.jobId().toString());
                refusal.put("state", holder.state());
            }
            throw new Reply(409, MAPPER.writeValueAsString(refusal));
        }
        if (!(decision instanceof UseLive) && !(decision instanceof Revive)) {
            requireRegistrable(tenant, requestedTarget, contentType, owner, profileModel, locked);
        }
        return new Resolved(source, sourceModel, contentType, owner, profileModel, decision);
    }

    /** {@code POST /plan}: the start's decision and refusals, the counts the cutover will destroy, and no write. */
    private void plan(HttpExchange exchange, String tenant) throws IOException {
        Map<String, Object> body = readBody(exchange, PLAN_FIELDS);
        String source = requireString(body, "source_collection");
        String requestedTarget = optionalString(body, "target_collection");
        Resolved r = resolve(tenant, source, requestedTarget);

        final String target;
        final String action;
        if (r.decision() instanceof UseLive u) {
            target = u.name();
            action = "existing";
        } else if (r.decision() instanceof Revive v) {
            target = v.name();
            action = "revive";
        } else {
            target = requestedTarget;
            action = "register";
        }
        ReembedSourceReader.PlanCounts n = reader.planCounts(tenant, source);
        Map<String, Object> out = orderedMap(
            "source_collection", source,
            "content_type", r.contentType(),
            "owner_id", r.owner(),
            "source_model", r.sourceModel(),
            "target_model", r.profileModel(),
            "target_collection", target,
            "target_action", action,
            "documents", n.documents(),
            "tombstoned_documents", n.tombstonedDocuments(),
            "chunks", n.chunks(),
            "ownerless_chunks", n.ownerlessChunks(),
            "token_estimate", n.tokenEstimate(),
            "quarantine_rows", n.quarantineRows(),
            "foreign_topic_assignments", n.foreignTopicAssignments(),
            "topic_links", n.topicLinks(),
            "unmatched_satellite_rows", n.unmatchedSatelliteRows());
        log.info("event=reembed_plan tenant={} source={} target={} target_action={} documents={} chunks={} "
                + "token_estimate={} quarantine_rows={} foreign_topic_assignments={} topic_links={} "
                + "unmatched_satellite_rows={}", tenant, source, target, action, n.documents(), n.chunks(),
            n.tokenEstimate(), n.quarantineRows(), n.foreignTopicAssignments(), n.topicLinks(),
            n.unmatchedSatelliteRows());
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(out));
    }

    private void start(HttpExchange exchange, String tenant) throws IOException {
        Map<String, Object> body = readBody(exchange, START_FIELDS);
        String source = requireString(body, "source_collection");
        String requestedTarget = optionalString(body, "target_collection");
        Long tokenCap = optionalPositiveLong(body, "token_cap");
        boolean keepSource = optionalBoolean(body, "keep_source");
        boolean forceDelete = optionalBoolean(body, "force_delete");

        Resolved resolved = resolve(tenant, source, requestedTarget);
        TargetSelection.Decision decision = resolved.decision();
        String contentType = resolved.contentType();
        String owner = resolved.owner();
        final String target;
        final String action;
        if (decision instanceof UseLive u) {
            target = u.name();
            action = "existing";
        } else if (decision instanceof Revive v) {
            // The collection is the source of no lock-state job (decide refused otherwise). The upsert is the one
            // verb that clears a supersession; it changes nothing else of an existing row.
            catalog.upsertCollection(tenant, orderedMap(
                "name", v.name(), "content_type", contentType, "owner_id", owner));
            target = v.name();
            action = "revived";
        } else {
            target = register(tenant, requestedTarget, contentType, owner, resolved.profileModel(),
                jobs.listByStates(tenant, ReembedJobRepository.LOCK_STATES));
            action = "registered";
        }

        // The earlier job is marked before the start, and also on the live branch: the revive is the upsert that
        // cleared the supersession, so the mark follows it, not the start. A start that then loses (a lock, a crash)
        // leaves the target live and the earlier job marked; a failure of the mark itself leaves it unmarked, and
        // the retry arrives here as a live target. markRevived is idempotent. A registered target is new, so no
        // earlier job went target to source.
        Optional<Job> earlier = "registered".equals(action)
            ? Optional.empty() : jobs.markRevived(tenant, target, source);
        Job job = jobs.start(tenant, source, target, tokenCap, keepSource, forceDelete);
        log.info("event=reembed_job_started tenant={} job={} source={} target={} target_action={} token_cap={} "
                + "keep_source={} force_delete={} earlier_job_revived={}", tenant, job.jobId(), source, target,
            action, tokenCap, keepSource, forceDelete, earlier.map(j -> j.jobId().toString()).orElse("none"));
        wakeRunner.run();
        Map<String, Object> out = row(job);
        out.put("target_action", action);
        earlier.ifPresent(j -> out.put("revived_job_id", j.jobId().toString()));
        HttpUtil.send(exchange, 201, MAPPER.writeValueAsString(out));
    }

    private static String needOf(String contentType, String owner, String profileModel) {
        return "content type " + contentType + ", owner " + owner + ", model " + profileModel;
    }

    /**
     * The refusals that stand before a target is registered, none of which writes: no name sent, the name held as a
     * lock-state job's source, the name already registered. The plan runs them too, so it refuses what the start would.
     */
    private void requireRegistrable(String tenant, String requested, String contentType, String owner,
                                    String profileModel, List<Job> locked) {
        String need = needOf(contentType, owner, profileModel);
        if (requested == null) {
            throw new IllegalArgumentException("no registered collection has " + need
                + "; send target_collection to register one");
        }
        Job asSource = firstMatch(locked, j -> j.sourceCollection().equals(requested));
        if (asSource != null) {
            throw heldBy(asSource, "target_collection " + requested + " is held as the source of");
        }
        Map<String, Object> existing = catalog.getCollection(tenant, requested);
        if (existing != null) {
            throw new Reply(409, jsonOf(orderedMap(
                "error", "target_collection " + requested + " is already registered as content type "
                    + existing.get("content_type") + ", owner " + existing.get("owner_id") + ", model "
                    + existing.get("embedding_model") + ", which is not a live collection of " + need,
                "collection", requested)));
        }
    }

    /**
     * Register {@code requested} as the job's target and check what was registered. Returns the name.
     * Nothing is written when the name is missing, held as a lock-state job's source, or already registered.
     */
    private String register(String tenant, String requested, String contentType, String owner, String profileModel,
                            List<Job> locked) {
        requireRegistrable(tenant, requested, contentType, owner, profileModel, locked);
        String need = needOf(contentType, owner, profileModel);
        catalog.upsertCollection(tenant, orderedMap("name", requested, "content_type", contentType, "owner_id", owner));
        // The profile gate fixed the model and a trigger may have settled the owner: check what is there now.
        Result registered = TargetSelection.select(rows(tenant, contentType), contentType, owner, profileModel);
        if (registered instanceof Live l && l.name().equals(requested)) {
            return requested;
        }
        Map<String, Object> now = catalog.getCollection(tenant, requested);
        throw new Reply(409, jsonOf(orderedMap(
            "error", "target_collection " + requested + " was registered but is not a live collection of " + need
                + (now == null ? "" : "; the row holds content type " + now.get("content_type") + ", owner "
                    + now.get("owner_id") + ", model " + now.get("embedding_model"))
                + ". The registration was left in place",
            "collection", requested)));
    }

    private List<CollectionRow> rows(String tenant, String contentType) {
        return catalog.listCollections(tenant, contentType, null).stream().map(CollectionRow::fromMap).toList();
    }

    private String profileModel(String tenant, String contentType) {
        String model = null;
        for (Map<String, Object> row : catalog.embeddingProfile(tenant)) {
            if (contentType.equals(row.get("content_type"))) {
                model = String.valueOf(row.get("embedding_model"));
            }
        }
        return model;
    }

    // ── POST /jobs/{id}/pause|resume|abort ───────────────────────────────────

    private void control(HttpExchange exchange, String tenant, UUID jobId, Event event) throws IOException {
        Map<String, Object> body = readBody(exchange, event == Event.RESUME ? RESUME_FIELDS : NO_FIELDS);
        Long tokenCap = event == Event.RESUME ? optionalPositiveLong(body, "token_cap") : null;
        for (int attempt = 0; attempt < CONTROL_ATTEMPTS; attempt++) {
            Job job = jobs.get(tenant, jobId).orElse(null);
            if (job == null) {
                throw new Reply(404, error("no re-embed job " + jobId));
            }
            State from = State.fromWire(job.state());
            switch (ReembedStateMachine.decide(from, event, job.cutoverStep(), job.disposition())) {
                case Unchanged u -> {
                    // Answered from the row as read, with no write: updated_at is the stored one.
                    Map<String, Object> out = row(job);
                    out.put("unchanged", true);
                    HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(out));
                    return;
                }
                case Refused r -> throw new Reply(409, MAPPER.writeValueAsString(orderedMap(
                    "error", r.reason(), "job_id", job.jobId().toString(), "state", job.state())));
                case Applied a -> {
                    if (tokenCap != null) {
                        if (job.tokenCap() == null) {
                            throw new Reply(409, MAPPER.writeValueAsString(orderedMap(
                                "error", "job " + jobId + " has no token cap to raise",
                                "job_id", job.jobId().toString(), "state", job.state())));
                        }
                        if (tokenCap < job.tokenCap()) {
                            throw new Reply(409, MAPPER.writeValueAsString(orderedMap(
                                "error", "token_cap can only be raised: the job's cap is " + job.tokenCap(),
                                "job_id", job.jobId().toString(), "state", job.state())));
                        }
                    }
                    Optional<Job> updated = jobs.applyTransition(tenant, jobId, from.wire(), job.cutoverStep(),
                        a.next().wire(), new Transition(null, null, null, tokenCap));
                    if (updated.isPresent()) {
                        log.info("event=reembed_job_{} tenant={} job={} from={} to={}",
                            event.name().toLowerCase(Locale.ROOT), tenant, jobId, from.wire(), a.next().wire());
                        if (event == Event.RESUME) {
                            wakeRunner.run();
                        }
                        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(row(updated.get())));
                        return;
                    }
                    // The job moved (the runner, or another request) between the read and the update: decide again.
                }
            }
        }
        throw new Reply(409, error("job " + jobId + " kept changing state while the request was applied; retry"));
    }

    // ── responses ────────────────────────────────────────────────────────────

    /** The job as the wire shows it: flat, snake_case, timestamps in UTC. */
    static Map<String, Object> row(Job j) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("job_id", j.jobId().toString());
        m.put("source_collection", j.sourceCollection());
        m.put("target_collection", j.targetCollection());
        m.put("state", j.state());
        m.put("disposition", j.disposition());
        m.put("failure_reason", j.failureReason());
        m.put("last_error", j.lastError());
        m.put("doc_cursor", j.docCursor());
        m.put("pass_number", j.passNumber());
        m.put("cutover_step", j.cutoverStep());
        m.put("documents_moved", j.documentsMoved());
        m.put("documents_skipped", j.documentsSkipped());
        m.put("chunks_embedded", j.chunksEmbedded());
        m.put("chunks_reused", j.chunksReused());
        m.put("tokens_billed", j.tokensBilled());
        m.put("token_cap", j.tokenCap());
        m.put("keep_source", j.keepSource());
        m.put("force_delete", j.forceDelete());
        m.put("started_at", utc(j.startedAt()));
        m.put("updated_at", utc(j.updatedAt()));
        m.put("finished_at", utc(j.finishedAt()));
        return m;
    }

    private static String utc(OffsetDateTime t) {
        return t == null ? null : t.toInstant().toString();
    }

    private static String error(String message) {
        try {
            return MAPPER.writeValueAsString(Map.of("error", String.valueOf(message)));
        } catch (JsonProcessingException e) {
            throw new IllegalStateException(e);
        }
    }

    private static String jsonOf(Map<String, Object> m) {
        try {
            return MAPPER.writeValueAsString(m);
        } catch (JsonProcessingException e) {
            throw new IllegalStateException(e);
        }
    }

    private static String held(String message, UUID jobId, String state) {
        return jsonOf(orderedMap("error", message, "job_id", jobId.toString(), "state", state));
    }

    private static Reply heldBy(Job job, String relation) {
        return new Reply(409, held("re-embed job " + job.jobId() + " (" + job.state() + "): " + relation
            + " " + job.sourceCollection() + " to " + job.targetCollection(), job.jobId(), job.state()));
    }

    private static Job firstMatch(List<Job> jobs, Predicate<Job> test) {
        for (Job j : jobs) {
            if (test.test(j)) {
                return j;
            }
        }
        return null;
    }

    /** Alternating key, value pairs into an insertion-ordered map; a null value stays (JSON null). */
    private static Map<String, Object> orderedMap(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            m.put((String) kv[i], kv[i + 1]);
        }
        return m;
    }

    // ── request bodies ───────────────────────────────────────────────────────

    /** The body as a JSON object (an empty body is {}); a field outside {@code allowed} is a 400, not ignored. */
    private static Map<String, Object> readBody(HttpExchange exchange, Set<String> allowed) throws IOException {
        byte[] bytes = exchange.getRequestBody().readAllBytes();
        Map<String, Object> body = bytes.length == 0 ? Map.of() : MAPPER.readValue(bytes, MAP_TYPE);
        if (body == null) {
            body = Map.of();
        }
        for (String key : body.keySet()) {
            if (!allowed.contains(key)) {
                throw new IllegalArgumentException("unknown field " + key
                    + (allowed.isEmpty() ? "; this request takes no body" : "; allowed: " + new java.util.TreeSet<>(allowed)));
            }
        }
        return body;
    }

    private static String requireString(Map<String, Object> body, String key) {
        String s = optionalString(body, key);
        if (s == null) {
            throw new IllegalArgumentException("missing required string field: " + key);
        }
        return s;
    }

    private static String optionalString(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (v == null) {
            return null;
        }
        if (!(v instanceof String s) || s.isBlank()) {
            throw new IllegalArgumentException(key + " must be a non-empty string");
        }
        return s;
    }

    private static Long optionalPositiveLong(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (v == null) {
            return null;
        }
        if (!(v instanceof Integer || v instanceof Long) || ((Number) v).longValue() < 1) {
            throw new IllegalArgumentException(key + " must be a positive integer");
        }
        return ((Number) v).longValue();
    }

    private static boolean optionalBoolean(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (v == null) {
            return false;
        }
        if (!(v instanceof Boolean b)) {
            throw new IllegalArgumentException(key + " must be true or false");
        }
        return b;
    }
}
