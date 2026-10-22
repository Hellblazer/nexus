// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.reembed;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.CombinedWriteService;
import dev.nexus.service.db.ReembedJobRepository;
import dev.nexus.service.db.ReembedJobRepository.BatchResult;
import dev.nexus.service.db.ReembedJobRepository.Job;
import dev.nexus.service.db.ReembedJobRepository.Skip;
import dev.nexus.service.db.ReembedJobRepository.Transition;
import dev.nexus.service.db.ReembedSourceReader;
import dev.nexus.service.db.ReembedSourceReader.Candidate;
import dev.nexus.service.http.RequestContext;
import dev.nexus.service.reembed.ReembedStateMachine.Applied;
import dev.nexus.service.reembed.ReembedStateMachine.Event;
import dev.nexus.service.reembed.ReembedStateMachine.State;
import dev.nexus.service.vectors.VoyageEmbedder;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Duration;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.function.Consumer;
import java.util.function.Supplier;

/**
 * RDR-210 Phase 6 Step 2 (bead nexus-f98bg): the loop that carries a re-embed job from {@code planned} to a
 * finished state. One {@link #tick()} visits every tenant's jobs that are {@code planned}, {@code running} or in
 * {@code cutover} and drives each as far as it can go; a pause, an abort or a failure ends that job's work for the
 * tick, and the next tick picks up whatever state the job is in. Nothing is kept in memory between ticks, so a
 * restart resumes from the database: the first tick after boot is the boot pickup.
 *
 * <p><b>Fairness.</b> The tick is round-robin: each ready job gets a <i>turn</i> of at most
 * {@link Settings#maxBatchesPerTurn} batches, then the next job gets its turn, and the rounds repeat until every job
 * has stopped. A cutover picked up at boot, or a small job, therefore waits behind at most one turn of each job
 * ahead of it, never behind a large job's whole run. A job's turn ends early at any state that is not
 * {@code running}.
 *
 * <p><b>Every entry into {@code running} re-checks the target's model against the profile</b>, the boot pickup of a
 * job already {@code running} included. The entry ({@code PICKUP}, a resume) and the {@code PROFILE_CHANGED} that
 * follows a mismatch are two compare-and-set steps, so a crash between them leaves a {@code running} row nobody has
 * checked. The check therefore runs at the head of every drive of a {@code running} job, whichever way it got there.
 *
 * <p><b>A pass</b> walks the source's candidate documents ({@link ReembedSourceReader}) in tumbler order after the
 * job's cursor, in batches of whole documents up to {@link Settings#maxBatchChunks} manifest rows (a larger document
 * goes alone). A batch is read, checked against the token cap, then moved by
 * {@link CombinedWriteService#moveDocsCombined} under a {@link Settings#batchDeadline} request deadline, with the
 * job's id so each document's move and its record commit in one transaction. A document the engine refuses with
 * {@code move_precondition_failed} or as tombstoned is a <i>skip</i> (a client changed it; a later pass sees it
 * where the client put it); any other per-document failure, or an exception from the move, is a <i>failed batch</i>:
 * the job goes to {@code failed} and a resume continues. A pass ends when no candidate lies after the cursor. If
 * candidates remain (a skip, or a client writing back to the source after its document moved), a new pass starts
 * from the beginning, at most {@link Settings#maxPasses} passes in all; what is still there after the last pass is
 * recorded as skips with the reason {@value #REASON_PASS_LIMIT}.
 *
 * <p><b>Cutover</b> is {@link #cutover}: candidates left means {@code done_with_skips}; otherwise supersede the
 * source, then (unless {@code keep_source}) delete it, each irreversible step preceded by a monotone marker on the
 * job row so a restart repeats nothing it already did. Parity, the {@code kept_tombstoned} and residue
 * dispositions and {@code --wait} belong to nexus-09iho (Step 4) and are marked where they go.
 *
 * <p><b>Locks.</b> A document's move takes the sweep gate shared, the index-run lock, then the job row; the runner
 * itself holds none between calls, and control routes take only the job row, so there is no cycle.
 */
public final class ReembedRunner {

    private static final Logger log = LoggerFactory.getLogger(ReembedRunner.class);

    /** The reason recorded for a document still in the source after the last pass. */
    public static final String REASON_PASS_LIMIT = "pass_limit";
    /** The engine's reason for a document that is no longer what the job read. */
    static final String REASON_PRECONDITION = CatalogRepository.MOVE_PRECONDITION_FAILED;
    static final String REASON_TOMBSTONED = "tombstoned";
    static final String REASON_CHUNK_MISSING = "source_chunk_missing";
    /** The prefix of the engine's tombstone refusal message ({@code TombstonedDocumentException}). */
    private static final String ENGINE_TOMBSTONED_PREFIX = "move refused: document is tombstoned";
    private static final int LEFTOVER_PAGE = 500;
    /** What a blocked cutover's {@code last_error} starts with; the finish clears a message that starts with it. */
    private static final String BLOCKED_PREFIX = "cutover is blocked: ";
    private static final int LAST_ERROR_MAX = 1000;

    /**
     * Bounds a runner works to. {@link #DEFAULT} is the RDR's: 100 chunks, three passes, 300 seconds, and a turn of
     * {@value #DEFAULT_BATCHES_PER_TURN} batches (at most about 1,000 chunks, so a turn is minutes, not hours).
     */
    public record Settings(int maxBatchChunks, int maxPasses, Duration batchDeadline, int maxBatchesPerTurn) {
        public static final int DEFAULT_BATCHES_PER_TURN = 10;
        public static final Settings DEFAULT = new Settings(100, 3, Duration.ofSeconds(300));

        public Settings {
            if (maxBatchChunks < 1 || maxPasses < 1 || batchDeadline.isNegative() || batchDeadline.isZero()
                    || maxBatchesPerTurn < 1) {
                throw new IllegalArgumentException("a runner needs positive bounds: " + this);
            }
        }

        public Settings(int maxBatchChunks, int maxPasses, Duration batchDeadline) {
            this(maxBatchChunks, maxPasses, batchDeadline, DEFAULT_BATCHES_PER_TURN);
        }
    }

    /** One job's place in a tick's rounds. */
    private record Turn(String tenant, UUID jobId) {
    }

    private enum Step { CONTINUE, STOP }

    private final ReembedJobRepository jobs;
    private final ReembedSourceReader reader;
    private final CatalogRepository catalog;
    private final CombinedWriteService mover;
    private final Supplier<List<String>> tenantSource;
    private final Settings settings;

    /**
     * Test-only seam, called with a phase name outside any transaction: {@code before_batch} (after the batch's candidates are chosen, before anything is read),
     * {@code before_move} (the batch is read, the move is about to run), {@code before_supersede},
     * {@code after_supersede}, {@code before_delete}, {@code delete} (immediately before each call to
     * {@code deleteCollection}), {@code after_delete}. An exception it throws stops that job's work for the tick,
     * as a crash at that point would. Null (a no-op) in production.
     */
    private volatile Consumer<String> phaseHookForTests;

    public ReembedRunner(ReembedJobRepository jobs, ReembedSourceReader reader, CatalogRepository catalog,
                         CombinedWriteService mover, Supplier<List<String>> tenantSource, Settings settings) {
        this.jobs = jobs;
        this.reader = reader;
        this.catalog = catalog;
        this.mover = mover;
        this.tenantSource = tenantSource;
        this.settings = settings;
    }

    /** Test-only: install (or clear with {@code null}) the phase hook. */
    public void setPhaseHookForTests(Consumer<String> hook) {
        this.phaseHookForTests = hook;
    }

    private void phase(String name) {
        Consumer<String> h = phaseHookForTests;
        if (h != null) {
            h.accept(name);
        }
    }

    // ── the entry point ──────────────────────────────────────────────────────

    /**
     * One visit to every tenant's unfinished jobs, round-robin (see the class comment). A failure in one job or
     * tenant is logged and does not stop the others; the job keeps the state it had reached and the next tick
     * retries from there. Jobs that appear after the tick listed them wait for the next tick.
     */
    public void tick() {
        List<Turn> ready = new ArrayList<>();
        for (String tenant : tenantSource.get()) {
            try {
                for (Job job : jobs.listByStates(tenant, List.of(
                        ReembedJobRepository.PLANNED, ReembedJobRepository.RUNNING, ReembedJobRepository.CUTOVER))) {
                    ready.add(new Turn(tenant, job.jobId()));
                }
            } catch (RuntimeException e) {
                log.warn("event=reembed_list_failed tenant={} error={}", tenant, e.toString());
            }
        }
        while (!ready.isEmpty()) {
            for (Iterator<Turn> it = ready.iterator(); it.hasNext(); ) {
                Turn turn = it.next();
                boolean more;
                try {
                    more = drive(turn.tenant(), turn.jobId(), settings.maxBatchesPerTurn());
                } catch (RuntimeException e) {
                    log.warn("event=reembed_drive_failed tenant={} job={} error={}", turn.tenant(), turn.jobId(),
                        e.toString(), e);
                    more = false;
                }
                if (!more) {
                    it.remove();
                }
            }
        }
    }

    /**
     * Drive one job for one turn: until it has nothing more to do, or it has run {@code maxBatches} batches (a pass
     * end counts as one).
     *
     * @return true when the turn ended on the budget with the job still {@code running}, so it should get another
     */
    boolean drive(String tenant, UUID jobId, int maxBatches) {
        boolean profileChecked = false;
        int batches = 0;
        while (true) {
            Job job = jobs.get(tenant, jobId).orElse(null);
            if (job == null) {
                return false;
            }
            switch (State.fromWire(job.state())) {
                case PLANNED -> {
                    if (!transition(tenant, job, Event.PICKUP, Transition.NONE)) {
                        return false;
                    }
                }
                case RUNNING -> {
                    if (batches >= maxBatches) {
                        return true;
                    }
                    if (!profileChecked) {
                        if (!profileHolds(tenant, job)) {
                            return false;
                        }
                        profileChecked = true;
                    }
                    batches++;
                    if (batchStep(tenant, job) == Step.STOP) {
                        return false;
                    }
                }
                case CUTOVER -> {
                    cutover(tenant, job);
                    return false;
                }
                default -> {
                    return false;
                }
            }
        }
    }

    // ── profile ──────────────────────────────────────────────────────────────

    /**
     * Whether the target's embedding model is still the profile's model for the source's content type. A mismatch
     * moves the job to {@code failed/profile_changed}; an unregistered source or target to {@code failed/batch_failed}.
     *
     * @return true when the job may move documents
     */
    private boolean profileHolds(String tenant, Job job) {
        Map<String, Object> src = catalog.getCollection(tenant, job.sourceCollection());
        Map<String, Object> tgt = catalog.getCollection(tenant, job.targetCollection());
        if (src == null || tgt == null) {
            String missing = src == null ? job.sourceCollection() : job.targetCollection();
            transition(tenant, job, Event.BATCH_FAILED,
                Transition.failed(null, "collection " + missing + " is not registered"));
            return false;
        }
        String contentType = String.valueOf(src.get("content_type"));
        String targetModel = String.valueOf(tgt.get("embedding_model"));
        String profileModel = null;
        for (Map<String, Object> row : catalog.embeddingProfile(tenant)) {
            if (contentType.equals(row.get("content_type"))) {
                profileModel = String.valueOf(row.get("embedding_model"));
            }
        }
        if (targetModel.equals(profileModel)) {
            return true;
        }
        transition(tenant, job, Event.PROFILE_CHANGED, Transition.failed(null,
            "the profile's model for " + contentType + " is "
                + (profileModel == null ? "unset" : profileModel) + ", the target " + job.targetCollection()
                + " holds " + targetModel));
        return false;
    }

    // ── one batch, or the end of a pass ──────────────────────────────────────

    private Step batchStep(String tenant, Job job) {
        String source = job.sourceCollection();
        List<Candidate> candidates = reader.candidates(tenant, source, job.docCursor(), settings.maxBatchChunks());
        if (candidates.isEmpty()) {
            return passEnd(tenant, job);
        }
        List<Candidate> batch = new ArrayList<>();
        int chunks = 0;
        for (Candidate c : candidates) {
            if (!batch.isEmpty() && chunks + c.manifestRows() > settings.maxBatchChunks()) {
                break;
            }
            batch.add(c);
            chunks += c.manifestRows();
        }
        phase("before_batch");
        long started = System.nanoTime();

        List<String> docIds = batch.stream().map(Candidate::docId).toList();
        Map<String, List<Map<String, Object>>> manifests = catalog.getManifestMany(tenant, docIds);
        Set<String> wanted = new LinkedHashSet<>();
        for (List<Map<String, Object>> rows : manifests.values()) {
            for (Map<String, Object> row : rows) {
                if (source.equals(row.get("collection"))) {
                    wanted.add(String.valueOf(row.get("chash")).toLowerCase(Locale.ROOT));
                }
            }
        }
        Map<String, Map<String, Object>> sourceChunks = new HashMap<>();
        for (Map<String, Object> chunk : reader.chunks(tenant, source, wanted)) {
            sourceChunks.put((String) chunk.get("chash"), chunk);
        }

        // Documents the job can move now, and the ones it already knows it cannot.
        List<Skip> skips = new ArrayList<>();
        List<Map<String, Object>> docs = new ArrayList<>();
        Map<String, List<String>> expected = new LinkedHashMap<>();
        Map<String, Map<String, Object>> chunksToMove = new LinkedHashMap<>();
        for (Candidate c : batch) {
            List<Map<String, Object>> rows = manifests.getOrDefault(c.docId(), List.of());
            List<String> chashes = new ArrayList<>(rows.size());
            boolean allInSource = true;
            boolean allReadable = true;
            for (Map<String, Object> row : rows) {
                String chash = String.valueOf(row.get("chash")).toLowerCase(Locale.ROOT);
                chashes.add(chash);
                allInSource &= source.equals(row.get("collection"));
                Map<String, Object> chunk = sourceChunks.get(chash);
                allReadable &= chunk != null && chunk.get("text") != null;
            }
            if (!allInSource) {
                // Some row already names another collection: the engine would refuse the document for the same reason.
                skips.add(new Skip(c.docId(), REASON_PRECONDITION));
                continue;
            }
            if (!allReadable) {
                skips.add(new Skip(c.docId(), REASON_CHUNK_MISSING));
                continue;
            }
            docs.add(Map.of("doc_id", c.docId(), "rows", rows));
            expected.put(c.docId(), chashes);
            for (String chash : chashes) {
                chunksToMove.putIfAbsent(chash, sourceChunks.get(chash));
            }
        }

        // The token cap, against the estimate for the chunks this batch would embed (a chunk the target already
        // holds is not embedded, so the estimate only ever overstates).
        long estimate = 0;
        for (Map<String, Object> chunk : chunksToMove.values()) {
            estimate += VoyageEmbedder.estimateTokens((String) chunk.get("text"));
        }
        if (job.tokenCap() != null && job.tokensBilled() + estimate > job.tokenCap()) {
            transition(tenant, job, Event.TOKEN_CAP, Transition.failed(null, "the next batch needs about " + estimate
                + " tokens and the job has billed " + job.tokensBilled() + " of a cap of " + job.tokenCap()));
            return Step.STOP;
        }

        long embedded = 0;
        long reused = 0;
        long tokens = 0;
        List<String> fatal = new ArrayList<>();
        Set<String> failedDocs = new LinkedHashSet<>();
        if (!docs.isEmpty()) {
            phase("before_move");
            CombinedWriteService.CombinedWriteResult result;
            RequestContext.setDeadlineNanos(System.nanoTime() + settings.batchDeadline().toNanos());
            try {
                phase("move_call");
                result = mover.moveDocsCombined(tenant, source, job.targetCollection(),
                    new ArrayList<>(chunksToMove.values()), docs, expected, job.jobId());
            } catch (RuntimeException e) {
                // What an exception from the move leaves behind. The move embeds the batch's new chunks first (the
                // billed tokens), then commits one transaction per document, each of which records its own move,
                // documents_moved and cursor (so those are already counted and the cursor already stands at the
                // last committed document). The batch's totals (chunks embedded and reused, tokens billed) died
                // with the exception and are not attributable to documents here, so they are NOT folded: the job's
                // chunks_embedded and chunks_reused undercount by that batch, and tokens_billed (the number the cap
                // reads) by at most the estimate computed above, which the failure records in last_error. The skips
                // found above are not recorded either; the next attempt finds them again.
                log.warn("event=reembed_batch_failed tenant={} job={} source={} cursor={} tokens_unaccounted_upto={} "
                        + "error={}", tenant, job.jobId(), source, job.docCursor(), estimate, e.toString(), e);
                String note = " [up to " + estimate + " tokens may have been billed for this batch and are not "
                    + "counted in tokens_billed]";
                transition(tenant, job, Event.BATCH_FAILED,
                    Transition.failed(null, truncate(e.toString(), LAST_ERROR_MAX - note.length()) + note));
                return Step.STOP;
            } finally {
                RequestContext.clearDeadline();
            }
            Map<String, Object> response = result.response();
            embedded = number(response.get("embed_embedded"));
            reused = number(response.get("embed_skipped"));
            tokens = result.tokens();
            if (response.get("failed") instanceof List<?> failed) {
                for (Object o : failed) {
                    if (!(o instanceof Map<?, ?> f)) {
                        continue;
                    }
                    String docId = String.valueOf(f.get("doc_id"));
                    String reason = String.valueOf(f.get("reason"));
                    if (REASON_PRECONDITION.equals(reason)) {
                        skips.add(new Skip(docId, REASON_PRECONDITION));
                    } else if (reason.startsWith(ENGINE_TOMBSTONED_PREFIX)) {
                        skips.add(new Skip(docId, REASON_TOMBSTONED));
                    } else {
                        fatal.add(docId + ": " + reason);
                        failedDocs.add(docId);
                    }
                }
            }
        }

        // Past the whole batch unless a document failed. The documents after a failed one commit regardless (each
        // is its own transaction), and each commit advanced the monotone cursor past the failed document; left
        // there, a resume would step over it and only the next pass would find it, to record it as a pass_limit
        // skip with the real failure lost. So a failed batch sets the cursor back to just before the first failed
        // document (never below where the batch started), and the resume reads that document first.
        String cursor = fatal.isEmpty() ? batch.get(batch.size() - 1).docId() : null;
        String retryAfter = fatal.isEmpty() ? null : cursorBeforeFirstFailure(batch, failedDocs, job.docCursor());
        Optional<Job> folded = jobs.advanceAfterBatch(tenant, job.jobId(),
            new BatchResult(skips, embedded, reused, tokens, cursor), retryAfter);
        Job after = folded.orElse(job);
        log.info("event=reembed_batch tenant={} job={} source={} target={} cursor={} documents_moved={} "
                + "documents_skipped={} chunks_embedded={} chunks_reused={} elapsed_ms={} tokens={} failed_documents={}",
            tenant, job.jobId(), source, job.targetCollection(), after.docCursor(), after.documentsMoved(),
            after.documentsSkipped(), embedded, reused, (System.nanoTime() - started) / 1_000_000, tokens,
            fatal.size());
        if (folded.isEmpty()) {
            return Step.STOP;
        }
        if (!fatal.isEmpty()) {
            transition(tenant, after, Event.BATCH_FAILED, Transition.failed(null, truncate(
                fatal.size() + " document(s) failed: " + String.join("; ", fatal.subList(0, Math.min(3, fatal.size()))))));
            return Step.STOP;
        }
        return Step.CONTINUE;
    }

    /**
     * The cursor a resume should start from so that the batch's first failed document is read again: the document
     * just before it in the batch, or {@code batchStart} when it is the batch's first.
     */
    private static String cursorBeforeFirstFailure(List<Candidate> batch, Set<String> failed, String batchStart) {
        String before = batchStart;
        for (Candidate c : batch) {
            if (failed.contains(c.docId())) {
                return before;
            }
            before = c.docId();
        }
        return batchStart;
    }

    /** No candidate lies after the cursor: finish the job's pass, start another, or move to cutover. */
    private Step passEnd(String tenant, Job job) {
        String source = job.sourceCollection();
        if (!reader.anyCandidate(tenant, source)) {
            return transition(tenant, job, Event.PASS_COMPLETE, Transition.NONE) ? Step.CONTINUE : Step.STOP;
        }
        if (job.passNumber() >= settings.maxPasses()) {
            recordLeftoversAsSkips(tenant, job);
            return transition(tenant, job, Event.PASS_COMPLETE, Transition.NONE) ? Step.CONTINUE : Step.STOP;
        }
        Optional<Job> next = jobs.startNextPass(tenant, job.jobId(), job.passNumber());
        log.info("event=reembed_pass_started tenant={} job={} pass={} started={}", tenant, job.jobId(),
            job.passNumber() + 1, next.isPresent());
        return next.isPresent() ? Step.CONTINUE : Step.STOP;
    }

    private void recordLeftoversAsSkips(String tenant, Job job) {
        String after = "";
        while (true) {
            List<Candidate> page = reader.candidates(tenant, job.sourceCollection(), after, LEFTOVER_PAGE);
            if (page.isEmpty()) {
                return;
            }
            List<Skip> skips = page.stream().map(c -> new Skip(c.docId(), REASON_PASS_LIMIT)).toList();
            jobs.advanceAfterBatch(tenant, job.jobId(), new BatchResult(skips, 0, 0, 0, null));
            after = page.get(page.size() - 1).docId();
        }
    }

    // ── cutover ──────────────────────────────────────────────────────────────

    /**
     * Finish a job whose pass completed. The order, with the marker committed before each irreversible step:
     * <ol>
     *   <li>a candidate still in the source: {@code done_with_skips} (nothing is superseded or deleted);</li>
     *   <li>supersede the source by the target (idempotent), then marker {@code superseded};</li>
     *   <li>(Step 4, nexus-09iho) the {@code kept_tombstoned} disposition goes here;</li>
     *   <li>{@code keep_source}: {@code done} with disposition {@code kept_by_flag};</li>
     *   <li>(Step 4, nexus-09iho) the parity check goes here, before the delete;</li>
     *   <li>the <b>delete guard</b>: if the source still holds a live candidate, a tombstoned document, a live
     *       document homed there whose chunks are elsewhere, or an aspect, queue or highlight row, the delete would
     *       destroy it, so the cutover stays in {@code cutover} with {@code last_error} saying what is left (see
     *       {@link #blocked}) and the next tick looks again. This is a refusal, not Step 4's {@code kept_tombstoned}
     *       and {@code kept_residue} dispositions, which replace it. The check runs once, just before the marker
     *       is written, and not again once the marker says the delete began (abort is refused from there, so the
     *       decision stands). A write that lands in the instants between this check and the delete itself is not
     *       caught here; Step 4's parity-in-the-delete-transaction closes it;</li>
     *   <li>marker {@code delete_started} (abort is refused from here), delete the source only if its registry row
     *       still exists, then {@code done} with disposition {@code deleted}.</li>
     * </ol>
     * A restart repeats from the top: a delete that committed leaves no row, so it is not issued again. An
     * exception from a step leaves the job in {@code cutover} for the next tick.
     */
    private void cutover(String tenant, Job job) {
        String source = job.sourceCollection();
        boolean deleteBegun = ReembedStateMachine.CUTOVER_STEP_DELETE_STARTED.equals(job.cutoverStep());
        if (!deleteBegun) {
            if (reader.anyCandidate(tenant, source)) {
                transition(tenant, job, Event.CUTOVER_SKIPS, Transition.NONE);
                return;
            }
            phase("before_supersede");
            Map<String, Object> row = catalog.getCollection(tenant, source);
            if (row == null) {
                blocked(tenant, job, "the source is no longer registered; abort the job");
                return;
            }
            int superseded = catalog.supersedeCollection(tenant, source, job.targetCollection(), null);
            if (superseded == 0) {
                blocked(tenant, job, "the source is already superseded by " + row.get("superseded_by")
                    + ", not by " + job.targetCollection());
                return;
            }
            if (!jobs.setCutoverStep(tenant, job.jobId(), ReembedStateMachine.CUTOVER_STEP_SUPERSEDED)) {
                return;
            }
            phase("after_supersede");
            // Step 4 (nexus-09iho): the kept_tombstoned disposition, for a source that still holds tombstoned
            // documents, goes here.
            if (job.keepSource()) {
                finishCutover(tenant, job.jobId(), Transition.disposition("kept_by_flag"));
                return;
            }
            // Step 4 (nexus-09iho): the parity check (moved rows against the target's manifest) goes here, before
            // the delete.
        }
        if (!deleteBegun) {
            // The delete guard (see the method comment): the delete removes every document row homed at the source
            // and every aspect, queue and highlight row naming it, none of which the pass moved if it is still here.
            ReembedSourceReader.Residue residue = reader.residue(tenant, source);
            if (!residue.isEmpty()) {
                blocked(tenant, job, "the source still holds " + residue.describe()
                    + "; deleting it would destroy them. Remove or move them, or abort the job");
                return;
            }
        }
        if (!jobs.setCutoverStep(tenant, job.jobId(), ReembedStateMachine.CUTOVER_STEP_DELETE_STARTED)) {
            return;
        }
        phase("before_delete");
        if (catalog.getCollection(tenant, source) != null) {
            phase("delete");
            catalog.deleteCollection(tenant, source);
        }
        phase("after_delete");
        finishCutover(tenant, job.jobId(), Transition.disposition("deleted"));
    }

    /** Move the job from {@code cutover} to {@code done}, from the marker as it stands now. */
    private void finishCutover(String tenant, UUID jobId, Transition change) {
        Job now = jobs.get(tenant, jobId).orElse(null);
        if (now != null) {
            if (now.lastError() != null && now.lastError().startsWith(BLOCKED_PREFIX)) {
                // the block this cutover recorded has cleared; a finished job does not keep saying it is blocked
                jobs.setLastError(tenant, jobId, ReembedJobRepository.CUTOVER, null);
            }
            transition(tenant, now, Event.CUTOVER_DONE, change);
        }
    }

    /**
     * The cutover cannot proceed and no state change fits (the state machine has no cutover-to-failed edge but the
     * parity one, and it heals when the cause clears): log it and record the reason in {@code last_error}, where
     * the status the routes show reads it. Repeating on every tick, it writes once (the repository skips an equal
     * message).
     */
    private void blocked(String tenant, Job job, String why) {
        log.warn("event=reembed_cutover_blocked tenant={} job={} source={} target={} reason=\"{}\"", tenant,
            job.jobId(), job.sourceCollection(), job.targetCollection(), why);
        jobs.setLastError(tenant, job.jobId(), ReembedJobRepository.CUTOVER, truncate(BLOCKED_PREFIX + why));
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    /**
     * Apply {@code event} to {@code job} as the state machine decides it, by compare-and-set on the state and
     * marker the decision was made from. The {@code failure_reason} is the state machine's, never a mapping here.
     *
     * @return true when the transition applied
     */
    private boolean transition(String tenant, Job job, Event event, Transition change) {
        State from = State.fromWire(job.state());
        if (!(ReembedStateMachine.decide(from, event, job.cutoverStep(), job.disposition()) instanceof Applied applied)) {
            return false;
        }
        String reason = ReembedStateMachine.failureReason(event);
        Transition t = reason == null ? change
            : new Transition(reason, change.lastError(), change.disposition(), change.tokenCap());
        Optional<Job> updated = jobs.applyTransition(tenant, job.jobId(), from.wire(), job.cutoverStep(),
            applied.next().wire(), t);
        log.info("event=reembed_state_changed tenant={} job={} from={} to={} cause={} applied={}", tenant,
            job.jobId(), from.wire(), applied.next().wire(), event.name().toLowerCase(Locale.ROOT),
            updated.isPresent());
        return updated.isPresent();
    }

    private static long number(Object o) {
        return o instanceof Number n ? n.longValue() : 0L;
    }

    private static String truncate(String s) {
        return truncate(s, LAST_ERROR_MAX);
    }

    private static String truncate(String s, int max) {
        return s.length() <= max ? s : s.substring(0, Math.max(0, max));
    }
}
