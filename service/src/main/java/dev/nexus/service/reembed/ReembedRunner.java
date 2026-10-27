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
 * where the client put it); so is a note whose rewritten {@code source_uri} a live document in the target holds
 * ({@value #REASON_URI_TAKEN}, RDR-231 step 4: found before any embedding, or by the engine's own check if the holder
 * appeared since, and recorded with the holder's id); any other per-document failure, or an exception from the move, is a <i>failed batch</i>:
 * the job goes to {@code failed} and a resume continues. A pass ends when no candidate lies after the cursor. If
 * candidates remain (a skip, or a client writing back to the source after its document moved), a new pass starts
 * from the beginning, at most {@link Settings#maxPasses} passes in all; what is still there after the last pass is
 * recorded as skips with the reason {@value #REASON_PASS_LIMIT}.
 *
 * <p><b>Cutover</b> is {@link #cutover}, the RDR's ordered steps (Step 4, nexus-09iho): candidates left means
 * {@code done_with_skips}; otherwise supersede the source, then end {@code kept_tombstoned}, {@code kept_by_flag},
 * {@code failed/parity_mismatch} or {@code kept_residue} (unless {@code force_delete}) as each check finds, and
 * only past all of them delete the source, each irreversible step preceded by a monotone marker on the job row so
 * a restart repeats nothing it already did.
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
    /**
     * RDR-231: the document's rewritten source_uri is held by a live document. The skip keeps the holder's id; the
     * document is left in the source, counted once, and reported with its holder.
     */
    static final String REASON_URI_TAKEN = ReembedJobRepository.REASON_SOURCE_URI_TAKEN;
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
        // RDR-231 step 4: a document whose rewritten source_uri a live document holds is skipped here, before any of
        // its chunks reach the token estimate or the embed, so a collision spends no tokens. The engine repeats the
        // check inside the document's transaction for a holder that appears after this read.
        Map<String, String> uriHolders = catalog.moveUriHolders(tenant, source, job.targetCollection(), docIds);
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
            String uriHolder = uriHolders.get(c.docId());
            if (uriHolder != null) {
                skips.add(new Skip(c.docId(), REASON_URI_TAKEN, uriHolder));
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
                    } else if (reason.startsWith(CatalogRepository.MOVE_SOURCE_URI_TAKEN)) {
                        // The engine's backstop (completeMove) found a holder the pre-check did not: the same skip.
                        skips.add(new Skip(docId, REASON_URI_TAKEN,
                            reason.substring(CatalogRepository.MOVE_SOURCE_URI_TAKEN.length())));
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
     * Finish a job whose pass completed. RDR-210 Cutover, "Order inside cutover", with the marker committed before
     * each irreversible step. Every step is safe to repeat, so a restart re-runs from the first:
     * <ol>
     *   <li>a live candidate still in the source (a skip, or a client's write since the pass): {@code
     *       done_with_skips}, nothing superseded or deleted. A candidate is a live document with a manifest row
     *       naming the source, so a write-back of a moved document is found here, before the supersede (nexus-6grho);</li>
     *   <li>supersede the source by the target (idempotent), then marker {@code superseded};</li>
     *   <li>a document row left in the source that is soft-deleted (trash): {@code done} with disposition {@code
     *       kept_tombstoned}, no parity check, no delete. The delete would take the trash's rows with it; a later
     *       re-entry ({@link ReembedStateMachine.Event#REENTER_RUNNING}, {@code REENTER_CUTOVER}) finishes the job
     *       once they are purged or restored;</li>
     *   <li>{@code keep_source}: {@code done} with disposition {@code kept_by_flag};</li>
     *   <li>the <b>parity check</b> ({@link ReembedSourceReader#parity}): no manifest row names the source, no live
     *       document is homed there, and every document this job moved is in the target by both columns. A failure
     *       deletes nothing, keeps the source superseded, and takes the job to {@code failed/parity_mismatch} with the
     *       counts and the documents to inspect in {@code last_error}. {@code --resume} re-runs the pass and this
     *       check; {@code --abort} leaves the source superseded and undeleted. {@code force_delete} does not reach
     *       it: no flag deletes a live document's only copy;</li>
     *   <li>the <b>residue check</b>: aspect, queue and highlight rows still naming the source belong to no document
     *       the job moved, and the delete would take them. Unless {@code force_delete}, the job ends {@code done}
     *       with disposition {@code kept_residue} and the counts in {@code last_error}; with it the delete proceeds;</li>
     *   <li>marker {@code delete_started} (abort is refused from here), delete the source only if its registry row
     *       still exists, then {@code done} with disposition {@code deleted}.</li>
     * </ol>
     * Step 1 differs once the source is superseded (a marker, or the row names this job's target): a live document
     * this job has not already tried sends the job back to {@code running} to move it ({@code CUTOVER_RESCAN});
     * only documents it tried and could not move end {@code done_with_skips}. Steps 3 to 6 run once, before the
     * marker is written. A restart past {@code delete_started} does not repeat them, but the parity check and the
     * trash count run again immediately before {@code deleteCollection}, marker or not, and a source that fails
     * them goes to {@code failed/parity_mismatch} (abort is allowed from {@code failed}) instead of being deleted.
     * A manifest write that lands after the supersede is refused by the engine (nexus-mp8ys); a document registered
     * against the source in the instants between that last look and the delete itself is the one window left. An
     * exception from a step leaves the job in {@code cutover} for the next tick.
     */
    private void cutover(String tenant, Job job) {
        String source = job.sourceCollection();
        boolean deleteBegun = ReembedStateMachine.CUTOVER_STEP_DELETE_STARTED.equals(job.cutoverStep());
        String forceNote = null;
        if (!deleteBegun) {
            Map<String, Object> row = catalog.getCollection(tenant, source);
            if (reader.anyCandidate(tenant, source)) {
                boolean superseded = job.cutoverStep() != null
                    || (row != null && job.targetCollection().equals(String.valueOf(row.get("superseded_by"))));
                if (!superseded) {
                    transition(tenant, job, Event.CUTOVER_SKIPS, Transition.NONE);
                } else if (reader.anyUntriedCandidate(tenant, source, job.jobId())) {
                    rescan(tenant, job);
                } else {
                    // Every live document left is one this job already tried and could not move. The source is
                    // already superseded, so say where they are rather than leave a bare done_with_skips.
                    transition(tenant, job, Event.CUTOVER_SKIPS, new Transition(null,
                        "live document(s) this job could not move are still in the superseded source " + source
                            + "; nothing routes to them there. Fix what blocks them and move them by hand, or "
                            + "nx collection delete " + source + " discards them", null, null));
                }
                return;
            }
            phase("before_supersede");
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
            long tombstoned = reader.tombstonedDocuments(tenant, source);
            if (tombstoned > 0) {
                finishCutover(tenant, job.jobId(), Event.CUTOVER_DONE, new Transition(null,
                    tombstoned + " tombstoned document(s) still hold the source, so it was kept (superseded, not "
                        + "deleted). Purge or restore them, then nx collection re-embed --all re-enters this job",
                    ReembedStateMachine.DISPOSITION_KEPT_TOMBSTONED, null));
                return;
            }
            if (job.keepSource()) {
                finishCutover(tenant, job.jobId(), Event.CUTOVER_DONE, Transition.disposition("kept_by_flag"));
                return;
            }
            // The counter the parity check compares is the row as it stands now, not the snapshot the tick began from.
            Job now = jobs.get(tenant, job.jobId()).orElse(null);
            if (now == null) {
                return;
            }
            ReembedSourceReader.Parity parity = reader.parity(tenant, source, job.targetCollection(), job.jobId(),
                now.documentsMoved());
            if (!parity.holds()) {
                String why = parity.describe(source);
                log.warn("event=reembed_parity_mismatch tenant={} job={} source={} target={} manifest_rows_naming_source={} "
                        + "live_homed_at_source={} moved_recorded={} moved_agreeing={} documents_moved={}", tenant,
                    job.jobId(), source, job.targetCollection(), parity.manifestRowsNamingSource(),
                    parity.liveHomedAtSource(), parity.movedRecorded(), parity.movedAgreeing(),
                    parity.documentsMoved());
                finishCutover(tenant, job.jobId(), Event.PARITY_FAILED, Transition.failed(null, truncate(why)));
                return;
            }
            ReembedSourceReader.Residue residue = reader.residue(tenant, source);
            if (residue.satelliteRows() > 0) {
                String what = residue.describeSatellites();
                if (!job.forceDelete()) {
                    finishCutover(tenant, job.jobId(), Event.CUTOVER_DONE, new Transition(null,
                        "the source was kept (superseded, not deleted): " + what + " still name it and belong to no "
                            + "document this job moved. nx collection delete " + source + " deletes them with it; a "
                            + "job started with --force-delete would have",
                        "kept_residue", null));
                    return;
                }
                forceNote = forceNoteOf(what);
                log.warn("event=reembed_force_delete tenant={} job={} source={} rows=\"{}\"", tenant, job.jobId(),
                    source, what);
            }
        }
        // The marker and the note a restart would lose go in one statement: an empty note clears what an earlier
        // failure left in last_error, a force note replaces it.
        if (!jobs.setCutoverStep(tenant, job.jobId(), ReembedStateMachine.CUTOVER_STEP_DELETE_STARTED,
                forceNote == null ? "" : forceNote)) {
            return;
        }
        phase("before_delete");
        Map<String, Integer> removed = null;
        if (catalog.getCollection(tenant, source) != null) {
            // Checked again right before the delete, whatever the marker says: the marker stops the checks above
            // from repeating on a restart, and a document registered at the source since (a registration the engine
            // does not refuse, unlike a manifest write) would die with it. A source already deleted needs none.
            String refusal = refusalBeforeDelete(tenant, job);
            if (refusal != null) {
                log.warn("event=reembed_delete_refused tenant={} job={} source={} reason=\"{}\"", tenant, job.jobId(),
                    source, refusal);
                finishCutover(tenant, job.jobId(), Event.PARITY_FAILED, Transition.failed(null, truncate(refusal)));
                return;
            }
            if (deleteBegun) {
                // A resume or restart past the marker did not run the residue check, and last_error may still hold
                // the refusal that failed the job before: record what this delete takes (nothing, when no
                // force_delete), replacing it. A fresh run wrote its note with the marker.
                jobs.setLastError(tenant, job.jobId(), ReembedJobRepository.CUTOVER,
                    forceNote != null ? forceNote : forceNoteNow(tenant, job));
            }
            phase("delete");
            removed = catalog.deleteCollection(tenant, source);
            log.info("event=reembed_source_deleted tenant={} job={} source={} removed={}", tenant, job.jobId(), source,
                removed);
        }
        phase("after_delete");
        finishDeleted(tenant, job.jobId(), removed);
    }

    /**
     * The last look before {@code deleteCollection}: the parity check again, and the trash. Null when the delete may
     * run; else the {@code last_error} of the {@code parity_mismatch} that refuses it.
     */
    private String refusalBeforeDelete(String tenant, Job job) {
        Job now = jobs.get(tenant, job.jobId()).orElse(null);
        if (now == null) {
            return "the job row is gone";
        }
        ReembedSourceReader.Parity parity = reader.parity(tenant, job.sourceCollection(), job.targetCollection(),
            job.jobId(), now.documentsMoved());
        long tombstoned = reader.tombstonedDocuments(tenant, job.sourceCollection());
        if (parity.holds() && tombstoned == 0) {
            return null;
        }
        String why = parity.holds() ? "the delete was refused at the last check" : parity.describe(job.sourceCollection());
        if (tombstoned > 0) {
            why += " " + tombstoned + " tombstoned document(s) also hold the source.";
        }
        return why;
    }

    /** The note for a {@code force_delete} job whose delete will take satellite rows with it, or null. */
    private String forceNoteNow(String tenant, Job job) {
        if (!job.forceDelete()) {
            return null;
        }
        ReembedSourceReader.Residue residue = reader.residue(tenant, job.sourceCollection());
        return residue.satelliteRows() == 0 ? null : forceNoteOf(residue.describeSatellites());
    }

    private static String forceNoteOf(String what) {
        return "--force-delete deleted " + what + " that named the source and belonged to no moved document";
    }

    /** Finish a delete: {@code deleted}, with what the delete removed after whatever note the marker step recorded. */
    private void finishDeleted(String tenant, UUID jobId, Map<String, Integer> removed) {
        String removedNote = null;
        if (removed != null) {
            StringBuilder sb = new StringBuilder();
            removed.forEach((table, n) -> {
                if (n != null && n > 0) {
                    sb.append(sb.length() == 0 ? "" : ", ").append(table).append('=').append(n);
                }
            });
            removedNote = sb.length() == 0 ? null : "the delete removed " + sb;
        }
        String note = null;
        if (removedNote != null) {
            Job now = jobs.get(tenant, jobId).orElse(null);
            String prior = now == null || now.lastError() == null || now.lastError().startsWith(BLOCKED_PREFIX)
                ? null : now.lastError();
            note = truncate(prior == null ? removedNote : prior + "; " + removedNote);
        }
        finishCutover(tenant, jobId, Event.CUTOVER_DONE, new Transition(null, note, "deleted", null));
    }

    /** A live document arrived in a superseded source: walk it again (the state machine's {@code CUTOVER_RESCAN}). */
    private void rescan(String tenant, Job job) {
        if (!(ReembedStateMachine.decide(State.CUTOVER, Event.CUTOVER_RESCAN, job.cutoverStep()) instanceof Applied)) {
            return;
        }
        Optional<Job> back = jobs.rescan(tenant, job.jobId(), job.cutoverStep());
        log.info("event=reembed_state_changed tenant={} job={} from=cutover to=running cause=cutover_rescan applied={}",
            tenant, job.jobId(), back.isPresent());
    }

    /**
     * End the cutover with {@code event}, from the job row and marker as they stand now (the tick's snapshot
     * predates the marker this cutover wrote). A block this cutover recorded earlier is cleared first, so a job that
     * finishes does not keep saying it is blocked.
     */
    private void finishCutover(String tenant, UUID jobId, Event event, Transition change) {
        Job now = jobs.get(tenant, jobId).orElse(null);
        if (now != null) {
            if (now.lastError() != null && now.lastError().startsWith(BLOCKED_PREFIX)) {
                jobs.setLastError(tenant, jobId, ReembedJobRepository.CUTOVER, null);
            }
            transition(tenant, now, event, change);
        }
    }

    /**
     * The cutover cannot proceed and no state change fits (the state machine has no cutover-to-failed edge for a
     * missing or foreign-superseded source, and it heals when the cause clears): log it and record the reason in
     * {@code last_error}, where the status the routes show reads it. Repeating on every tick, it writes once (the
     * repository skips an equal message).
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
