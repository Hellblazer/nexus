// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.reembed;

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
 * a move target, so it reads as absent here) and it is not {@code legacy_grandfathered}. A candidate that is not superseded wins; a superseded
 * one is chosen only when no non-superseded candidate exists. Among the pool, the highest numeric
 * {@code model_version} wins, then the lexically lower name ({@code collectionForTuple}'s tie order).
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
                                String lifecycleState) {

        /** The row as {@code CatalogRepository#listCollections} returns it (the keys of its map shape). */
        public static CollectionRow fromMap(Map<String, Object> m) {
            return new CollectionRow(
                str(m.get("name")), str(m.get("content_type")), str(m.get("owner_id")),
                str(m.get("embedding_model")), str(m.get("model_version")),
                Boolean.TRUE.equals(m.get("legacy_grandfathered")),
                str(m.get("superseded_by")), str(m.get("lifecycle_state")));
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

    /** The target row for the tuple, per the class comment. */
    public static Result select(List<CollectionRow> rows, String contentType, String ownerId,
                                String embeddingModel) {
        Comparator<CollectionRow> order = Comparator
            .comparingInt((CollectionRow r) -> versionNumber(r.modelVersion())).reversed()
            .thenComparing(CollectionRow::name);
        List<CollectionRow> candidates = rows.stream()
            .filter(r -> contentType.equals(r.contentType()))
            .filter(r -> ownerId.equals(r.ownerId()))
            .filter(r -> embeddingModel.equals(r.embeddingModel()))
            .filter(r -> "live".equals(r.lifecycleState()))
            .filter(r -> !r.legacyGrandfathered())
            .toList();
        var live = candidates.stream().filter(r -> !r.superseded()).min(order);
        if (live.isPresent()) {
            return new Live(live.get().name());
        }
        return candidates.stream().min(order)
            .<Result>map(r -> new Superseded(r.name(), r.supersededBy()))
            .orElseGet(Absent::new);
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
