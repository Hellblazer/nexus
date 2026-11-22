// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.reembed;

import dev.nexus.service.db.CollectionRegistration;
import dev.nexus.service.db.ReembedJobRepository.Job;

import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.format.DateTimeParseException;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.regex.Pattern;

/**
 * RDR-210 Phase 6 Step 2 (bead nexus-f98bg): which registered collection a re-embed job moves into, and what a job
 * does about the answer. Pure: the caller lists the tenant's collection rows
 * ({@code CatalogRepository#listCollections}, list-route semantics, so superseded rows are present) and applies
 * the decision; nothing here reads or writes a store.
 *
 * <p><b>Selection</b> ({@link #select}) uses {@code CatalogRepository#collectionForTuple}'s order but a narrower
 * filter: a row is a candidate when its content type, owner and embedding model equal the wanted tuple, its
 * lifecycle state is {@code live} (collectionForTuple excludes only quarantine; a dormant or disputed row is not
 * a move target, so it reads as absent here). A {@code legacy_grandfathered} row is a candidate, as it is for
 * {@code collectionForTuple} (RDR-229 P2.8, engine 0.1.200: both dropped the filter together, so the two never
 * disagree on a tuple's winner). A candidate that is not superseded wins; a superseded
 * one is chosen only when no non-superseded candidate exists. Among the pool, the highest numeric
 * {@code model_version} wins, then the first registered ({@code created_at} ascending, compared as instants), then the
 * lexically lower name: {@code collectionForTuple}'s tie order (nexus-p2m1x.4), pinned against it by a parity test.
 *
 * <p><b>Decision</b> ({@link #decide}) turns the selection into one of four outcomes for the job's start:
 * register the target ({@link Register}); use a live row ({@link UseLive}); revive a superseded row
 * ({@link Revive}); or refuse ({@link Refuse}). A superseded row is revived only when its {@code superseded_by}
 * names this job's source (an earlier job the other way left it under {@code --keep-source}, as
 * {@code kept_tombstoned}, or aborted after the supersede) and no job in a lock state has that row as its
 * source. A job that holds the row as its source could still resume and supersede or delete it, so it blocks
 * the revive; a failed {@code parity_mismatch} job is such a holder. A superseded row naming any other successor
 * is refused, naming both.
 *
 * <p><b>Revive marks the earlier job</b> (ruling 2, 2026-10-22, narrowed with the RDR-210 coordinator the same
 * day): the most recent earlier job that went target to source is marked with disposition {@code revived} when
 * it is {@code done} or {@code aborted}, its state unchanged, so a later {@code --all} never deletes the revived
 * collection. A {@code failed} earlier job is never marked: it is in a lock state with the target as its source,
 * so {@link #decide} refuses the revive until it is aborted (reviving under it would let its {@code --resume}
 * undo the revive), matching {@code ReembedJobRepository.markRevived}.
 * {@link Revive#markEarlierJob} is that decision for a given earlier job state, for the repository to apply.
 */
public final class TargetSelection {

    private TargetSelection() {
    }

    /** A catalog collection row: the fields of {@code CatalogRepository}'s list-route row that selection reads. */
    public record CollectionRow(String name, String contentType, String ownerId, String embeddingModel,
                                String modelVersion, boolean legacyGrandfathered, String supersededBy,
                                String lifecycleState, String createdAt) {

        /** A row with no registration stamp: it ties on age with every other, so the name decides. */
        public CollectionRow(String name, String contentType, String ownerId, String embeddingModel,
                             String modelVersion, boolean legacyGrandfathered, String supersededBy,
                             String lifecycleState) {
            this(name, contentType, ownerId, embeddingModel, modelVersion, legacyGrandfathered, supersededBy,
                lifecycleState, "");
        }

        /** The row as {@code CatalogRepository#listCollections} returns it (the keys of its map shape). */
        public static CollectionRow fromMap(Map<String, Object> m) {
            return new CollectionRow(
                str(m.get("name")), str(m.get("content_type")), str(m.get("owner_id")),
                str(m.get("embedding_model")), str(m.get("model_version")),
                Boolean.TRUE.equals(m.get("legacy_grandfathered")),
                str(m.get("superseded_by")), str(m.get("lifecycle_state")), str(m.get("created_at")));
        }

        boolean superseded() {
            return supersededBy != null && !supersededBy.isEmpty();
        }

        private static String str(Object o) {
            return o == null ? "" : o.toString();
        }
    }

    /** What {@link #select} found. */
    public sealed interface Result permits Absent, Live, Superseded {
    }

    /** No candidate row: the target must be registered. */
    public record Absent() implements Result {
    }

    /** A candidate that is not superseded. */
    public record Live(String name) implements Result {
    }

    /** Only superseded candidates exist; {@code name} is the best of them and {@code supersededBy} its successor. */
    public record Superseded(String name, String supersededBy) implements Result {
    }

    private static final Pattern VERSION = Pattern.compile("^v([0-9]+)$");

    /** {@code CatalogRepository.COL_VERSION_NUM}: the digits of {@code v<digits>}, else 0. */
    static int versionNumber(String modelVersion) {
        if (modelVersion == null) {
            return 0;
        }
        var m = VERSION.matcher(modelVersion);
        if (!m.matches()) {
            return 0;
        }
        try {
            return Integer.parseInt(m.group(1));
        } catch (NumberFormatException e) {
            return Integer.MAX_VALUE;
        }
    }

    /**
     * A {@code created_at} as the list route renders it (a Postgres timestamptz as text, {@code 2026-01-01
     * 00:00:00.5+00}, offset {@code +00}, {@code -08} or {@code +05:30}) as an instant, so rows whose
     * text differs in fraction or offset still order by time. A blank or unparseable stamp sorts last, so the
     * name decides among such rows.
     */
    static Instant registeredAt(String createdAt) {
        if (createdAt == null || createdAt.isBlank()) {
            return Instant.MAX;
        }
        String iso = createdAt.trim().replace(' ', 'T');
        if (OFFSET_HOURS_ONLY.matcher(iso).find()) {
            iso = iso + ":00";
        }
        try {
            return OffsetDateTime.parse(iso).toInstant();
        } catch (DateTimeParseException e) {
            return Instant.MAX;
        }
    }

    private static final Pattern OFFSET_HOURS_ONLY = Pattern.compile("[+-][0-9]{2}$");

    /** The target row for the tuple, per the class comment. */
    public static Result select(List<CollectionRow> rows, String contentType, String ownerId,
                                String embeddingModel) {
        Comparator<CollectionRow> order = Comparator
            .comparingInt((CollectionRow r) -> versionNumber(r.modelVersion())).reversed()
            .thenComparing((CollectionRow r) -> registeredAt(r.createdAt()))
            .thenComparing(CollectionRow::name);
        List<CollectionRow> candidates = rows.stream()
            .filter(r -> contentType.equals(r.contentType()))
            .filter(r -> ownerId.equals(r.ownerId()))
            .filter(r -> embeddingModel.equals(r.embeddingModel()))
            .filter(r -> "live".equals(r.lifecycleState()))
            .toList();
        var live = candidates.stream().filter(r -> !r.superseded()).min(order);
        if (live.isPresent()) {
            return new Live(live.get().name());
        }
        return candidates.stream().min(order)
            .<Result>map(r -> new Superseded(r.name(), r.supersededBy()))
            .orElseGet(Absent::new);
    }

    /**
     * The other collections that hold the source's lookup tuple (content type, owner, embedding model), by name: not
     * superseded and {@code lifecycle_state} not {@code quarantine}, the source's own row excluded. That is the one
     * "held" predicate RDR-229 uses everywhere (P3.8's partial UNIQUE, T2 nexus/sam-ruling-rdr229-p38-design-gate-v2-2026-11-21,
     * and catalog-051-2 and vectors-067), so a dormant or disputed holder counts. The tuple is not unique in the
     * registry (two sources that share a curator owner hold two rows on it), and the target is chosen by the tuple,
     * so a second source would be moved into the first source's target (nexus-p2m1x.28).
     */
    public static List<String> tupleMates(List<CollectionRow> rows, String source, String contentType,
                                          String ownerId, String embeddingModel) {
        return rows.stream()
            .filter(r -> !source.equals(r.name()))
            .filter(r -> contentType.equals(r.contentType()))
            .filter(r -> ownerId.equals(r.ownerId()))
            .filter(r -> embeddingModel.equals(r.embeddingModel()))
            .filter(r -> !"quarantine".equals(r.lifecycleState()) && !r.superseded())
            .map(CollectionRow::name)
            .sorted()
            .toList();
    }

    /**
     * The 409 reason when {@link #tupleMates} finds any: names the source and its mates, says why, and the exit. Tests pin
     * fragments of it with {@code contains}, not the whole text, so a sentence may be added; this one points a source the
     * re-own move skipped at the gc_audit query ({@link #REOWN_SKIPPED_QUERY}), since re-owning is not available for it.
     */
    public static String sharedSourceTupleReason(String source, List<String> mates, String contentType,
                                                 String ownerId, String embeddingModel) {
        return "source collection " + source + " shares its lookup tuple (content type " + contentType + ", owner "
            + ownerId + ", model " + embeddingModel + ") with " + String.join(", ", mates)
            + ": collections with the same owner, content type and model cannot be told apart when the re-embed "
            + "target is chosen by that tuple, so " + source + " would be moved into a target that "
            + String.join(", ", mates) + " also uses. " + ownerExit("one of the twins is re-owned or superseded")
            + " A collection the RDR-229 re-own move skipped, because the tuple it would move to was already held, "
            + "cannot be re-owned; the move records each skip and its holder: " + REOWN_SKIPPED_QUERY;
    }

    /**
     * Where an operator reads the rows the re-own move (vectors-067) skipped, and the collection that held each tuple (live,
     * dormant or disputed: the move's held predicate is unsuperseded and not in quarantine). The hint appears in the reason of
     * every twin refusal ({@link #sharedSourceTupleReason}), whether or not the source was skipped by the move.
     */
    static final String REOWN_SKIPPED_QUERY =
        "SELECT collection, details FROM nexus.gc_audit WHERE operation = 'rdr229_reown_skipped'";

    /** Why a client cannot fix a shared tuple in place, and what can. {@code then} finishes "can start once ...". */
    private static String ownerExit(String then) {
        return "A client cannot change a collection's owner in place (the registry keeps the owner a collection was "
            + "registered with); an owner changes through an engine data move, or a rename that re-homes the "
            + "collection under another owner (RDR-229 owner-is-source). "
            + "A re-embed of the source can start once " + then + ".";
    }

    /**
     * The job that makes a live target another source's: a job whose target is {@code target}, whose source is not
     * {@code source}, that <b>moved at least one document</b> ({@code documents_moved}, the job row's count of
     * documents copied; a job that was aborted or failed before copying one put nothing in the target), and whose
     * source has this source's embedding MODEL. Only the model is compared: the target itself fixed the content type
     * and owner the foreign source had when its job ran, and a later re-own of that source does not take its
     * documents out of the target, so its current owner is never read. The model is the row's {@code embedding_model}
     * while the row exists (superseded rows are listed), else the model token of the conformant name (a default
     * cutover deletes the row; a model never changes on a collection); a name that is not conformant reads as an
     * unknown model, which counts (a refusal, never a merge). The newest such job wins ({@code jobs} is newest
     * first). The name read is a
     * migration-source site, declared in {@code NameModelReadCensusTest.NAME_MODEL_READ_CALLS}: a re-embed source
     * row deleted by cutover; retires at RDR-229 P4. When it retires, a job whose source row is gone reads as an
     * unknown model and refuses, so the sibling narrowing reverts to conservative for a deleted source (a sibling on
     * another model is refused again once the first source's row has been deleted); one whose row is kept is read
     * by its row, as now.
     *
     * <p><b>Scope:</b> this closes same-model twins only. The target is keyed on content type, owner and the PROFILE
     * model, while both guards key on the SOURCE model, so two shared-curator sources on different source models
     * still re-embed into one target and merge. That is an accepted residual, not a pending one: Sam ruled "Accept,
     * documented" (T2 {@code nexus/sam-ruling-reembed-guard-residual-2026-11-21}).
     *
     * <p>A v2 of an already-moved source has the same model and a different name, so it is refused too: a deliberate
     * conservative refusal, since the job log is keyed by name and cannot tell a v2 from a shared-curator twin. The
     * log persists (a job row is never deleted), and re-owning or superseding the other source does not remove its
     * documents from the target, so the only exit is to re-own this source: a different owner gives it a different
     * target tuple.
     */
    public static Job foreignOrigin(List<Job> jobs, List<CollectionRow> rows, String target, String source,
                                    String embeddingModel) {
        for (Job j : jobs) {
            if (!target.equals(j.targetCollection()) || source.equals(j.sourceCollection())
                || j.documentsMoved() <= 0) {
                continue;
            }
            CollectionRow row = rows.stream().filter(r -> r.name().equals(j.sourceCollection())).findFirst()
                .orElse(null);
            String model = row != null ? row.embeddingModel()
                : CollectionRegistration.modelOfConformantName(j.sourceCollection());
            if (model == null || model.equals(embeddingModel)) {
                return j;
            }
        }
        return null;
    }

    /** The 409 reason when {@link #foreignOrigin} finds a job: names the source, the target and the other source. */
    public static String foreignOriginReason(String source, String target, Job origin) {
        return "the job log shows another source collection, " + origin.sourceCollection() + ", was re-embedded into "
            + "target " + target + " (job " + origin.jobId() + ", " + origin.state() + ", "
            + origin.documentsMoved() + " document(s) moved) from a source with this source's embedding model, so "
            + source + " would be moved into a target that " + origin.sourceCollection() + " already filled. "
            + ownerExit(source + " is re-owned, which gives it a different target tuple (re-owning "
                + origin.sourceCollection() + " does not take its documents out of the target, and "
                + origin.sourceCollection() + " may be an earlier version of " + source + ")");
    }

    /** A job in a lock state whose source is the candidate target: its id and state, to name in a refusal. */
    public record LockHolder(UUID jobId, String state) {
    }

    /** What the job's start does about the target. */
    public sealed interface Decision permits Register, UseLive, Revive, Refuse {
    }

    /** Register the target through the profile-gated upsert. */
    public record Register() implements Decision {
    }

    /** The target is registered and live; use it. */
    public record UseLive(String name) implements Decision {
    }

    /**
     * Revive {@code name} through the upsert that clears {@code superseded_by}, then mark the earlier job
     * disposition {@code revived}.
     */
    public record Revive(String name) implements Decision {

        /** The states in which an earlier job is marked {@code revived}: done or aborted. A failed one holds the
         *  lock, so {@link TargetSelection#decide} refuses before any marking. */
        private static final Set<String> MARKED_STATES = Set.of("done", "aborted");

        /** Whether the earlier job, in {@code earlierJobState}, is marked {@code revived} (its state unchanged). */
        public static boolean markEarlierJob(String earlierJobState) {
            return MARKED_STATES.contains(earlierJobState);
        }
    }

    /** Refuse the start (a 409); {@code reason} names the row, its successor or the holding job. */
    public record Refuse(String reason) implements Decision {
    }

    /**
     * @param result      {@link #select}'s answer
     * @param source      this job's source collection
     * @param lockHolder  a job in a lock state whose source is the selected target, or null when none is
     */
    public static Decision decide(Result result, String source, LockHolder lockHolder) {
        return switch (result) {
            case Absent a -> new Register();
            case Live l -> lockHolder == null ? new UseLive(l.name()) : heldBy(l.name(), lockHolder);
            case Superseded s -> {
                if (!source.equals(s.supersededBy())) {
                    yield new Refuse("target " + s.name() + " is superseded by " + s.supersededBy()
                        + ", not by this job's source " + source);
                }
                yield lockHolder == null ? new Revive(s.name()) : heldBy(s.name(), lockHolder);
            }
        };
    }

    private static Refuse heldBy(String target, LockHolder holder) {
        return new Refuse("re-embed job " + holder.jobId() + " (" + holder.state() + ") holds target " + target
            + " as its source");
    }
}
