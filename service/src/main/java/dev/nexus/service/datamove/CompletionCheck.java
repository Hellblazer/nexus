// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.db.PgSession;
import dev.nexus.service.vectors.LeafIndexPass;
import dev.nexus.service.vectors.Rdr229SwapMove;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Table;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.Set;
import java.util.TreeSet;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17), the boot-time completion check (RDR item 6, "Readers during the stamp"): on each install,
 * have the stamp moves finished, and so may the engine's id readers, the one-row rename and the index builds switch?
 *
 * <p><b>READS, and writes one row.</b> Nothing here runs DDL, registers a move or migrates data: Sam's rule is no boot-time data
 * migrations, and a data move is a visible, resumable job. {@link #evaluate} reads the registry ({@code nexus.data_moves}, admin
 * connection only: {@code nexus_svc} holds no grant), the catalog and the CHECK constraints, and returns what it found; the engine
 * reports it in its own {@code /v1/status} field, {@code rdr229_completion} ({@link CompletionBootWork}). The one write is the gate 3
 * latch (P3.4, linda 2026-11-22): the first time gate 3 holds, a row in {@code nexus.rdr229_gate_latch} says so
 * ({@link LatchWriter}, {@link Gate3LatchWriter}). It never runs on the check's own session, which is read only at the server
 * ({@code default_transaction_read_only = on}, so a write there raises 25006): the engine's reader writes it on a session of its own. A
 * caller that passes no writer ({@link LatchWriter#NONE}) writes nothing, and a test that runs the check in a READ ONLY transaction
 * sees a write refused.
 *
 * <p><b>Complete</b> means the label's move has {@code completed_at}, or the move that supersedes it has. A label with no row
 * is incomplete, because later releases' rows are registered at run time. Two exceptions, both settled by residual R3 (the
 * queue table is absent on most installs): a failed move whose driving table no longer exists counts as complete, and the
 * one allowlisted label ({@link StampLabels#NOT_APPLICABLE_WHEN_ABSENT}) is <i>not applicable</i>, whether it has no row or
 * a failed one, while its table or its {@code collection_id} column is absent (vectors-068-18 is skipped, unrecorded, on an
 * install whose kept queue table another role owns, so a stamp there is never a failed move). Every other label with no row
 * stays incomplete.
 *
 * <p>A stamp label is complete only while its table's CHECKs are present and validated. A move whose {@code on_complete}
 * could not validate (a row names a collection that has no registry row in its tenant: an orphan) leaves the CHECK
 * {@code NOT VALID}, so its label stays incomplete and the check says how many rows violate it: the VALIDATE waits instead of
 * failing a boot, and {@code nx doctor} names the table and the count.
 *
 * <p><b>The gates, in order</b> (a gate holds only if the one before it does):
 * <ol>
 *   <li>every stamp move and every P3.3a id-twin validation move ({@link FkTwins}) is complete: P3.3b's pass starts. A P3.3a label is
 *       complete only while its twin foreign key is present and validated, as a stamp label is only while its CHECKs are;</li>
 *   <li>also both P3.3c swap moves and the three child validation moves ({@link ReadSet#p33c()}): P3.4's builds start. The swap
 *       of chunks is complete only while the RDR-225 composite on the id is present and validated, and a child validation label
 *       only while its id key into the chunks id key is present and validated (the twin rule of P3.3a's labels);</li>
 *   <li>also P3.4 complete on the install, per residual S-2 (nexus-p2m1x.40) and linda's rulings of 2026-11-22: the PCI clause
 *       ({@link Gate3Source}, {@code PciGate3Coverage}). It is read from the database, so every engine, a standby included,
 *       computes the same answer: every collection the planner indexes by name has a VALID id-predicate index, and an id index
 *       counts as covered whether or not its name index is gone (the name family retires after gate 3, and that must not flip it).
 *       With per-collection indexing off ({@code NX_SEARCH_PCI=0}) the clause is not applicable ({@link IdIndexCoverage#pciOff}).
 *       A store with no index of either kind but a collection at the build threshold does not pass.</li>
 * </ol>
 * Every gate also refuses while {@value #SUPERSEDED_BY_CHECK} is missing (linda's ruling 2026-11-21, item c): that CHECK
 * waits on D2's data (vectors-068-19) and lands on a later boot, and the switch must not run without it.
 */
public final class CompletionCheck {

    private static final Logger log = LoggerFactory.getLogger(CompletionCheck.class);

    /** The CHECK whose changeset waits on the data D2's repair UPDATE could violate. */
    public static final String SUPERSEDED_BY_CHECK = "catalog_collections_superseded_by_id_check";

    private static final Table<?> MOVES = DSL.table(DSL.name("nexus", "data_moves"));
    private static final long VIOLATOR_TIMEOUT_MS = 30_000L;

    private CompletionCheck() { /* static */ }

    /** How a label stands. */
    public enum Status { COMPLETE, NOT_APPLICABLE, INCOMPLETE }

    /** One label of the read set and why it stands as it does ({@code why} is empty for a complete one). */
    public record LabelState(String label, String table, Status status, String why) {
        public boolean satisfied() {
            return status != Status.INCOMPLETE;
        }
    }

    /** One listed CHECK on its table: present, and validated. */
    public record CheckState(String table, String constraint, boolean present, boolean validated) { }

    /**
     * Rows that still violate a table's CHECK once its stamp move is done. {@code orphans}: a non-empty name that names no
     * registry row in the row's tenant (the stamp cannot give it an id). {@code unstamped}: rows the stamp has not reached
     * (the registry row exists, or the CHECK is the NOT NULL guard). {@code counted} is false when the count timed out or
     * the read failed. The counts are what the engine's admin role can see.
     */
    public record Violators(String table, String constraint, long orphans, long unstamped, boolean counted) { }

    /** A constraint a label stands on: its table and its name. */
    private record ConstraintRef(String table, String constraint) { }

    /**
     * P3.3c's labels that are complete only while a constraint is present and validated: each child validation label and its id key, and the
     * chunks swap and the composite on the id. Built from {@link Rdr229SwapMove} and {@link LeafIndexPass}, never from literals.
     */
    private static final Map<String, ConstraintRef> P33C_CONSTRAINTS = p33cConstraints();

    private static Map<String, ConstraintRef> p33cConstraints() {
        Map<String, ConstraintRef> m = new HashMap<>();
        for (String child : Rdr229SwapMove.CHILD_TABLES) {
            m.put(Rdr229SwapMove.validationLabel(child), new ConstraintRef(child, Rdr229SwapMove.childKeyConstraint(child)));
        }
        m.put(LeafIndexPass.swapLabel("chunks"), new ConstraintRef("chunks", Rdr229SwapMove.COMPOSITE_CONSTRAINT));
        return Map.copyOf(m);
    }

    /** The labels each gate reads. A later release adds its labels here (and to the test that pins the set). */
    public record ReadSet(List<String> gate1Labels, List<String> gate2Labels) {
        public ReadSet {
            gate1Labels = List.copyOf(gate1Labels);
            gate2Labels = List.copyOf(gate2Labels);
        }

        /** P3.1's read set: one stamp label per table that carries a CHECK. Gate 2 adds nothing. */
        public static ReadSet p31() {
            return new ReadSet(StampLabels.all(), List.of());
        }

        /**
         * P3.3a's read set (nexus-p2m1x.19): P3.1's stamp labels and the seven id-twin validation labels ({@code db/rdr229/p33a-fk-twins.tsv}),
         * all in gate 1: P3.3b's pass starts only once the twins are validated. Gate 2 adds nothing.
         */
        public static ReadSet p33a() {
            List<String> gate1 = new ArrayList<>(StampLabels.all());
            gate1.addAll(FkTwins.labels());
            return new ReadSet(gate1, List.of());
        }

        /**
         * P3.3c's read set (nexus-p2m1x.21): P3.3a's, and in gate 2 the two swap moves ({@link LeafIndexPass#swapLabel}) and the three child
         * validation moves ({@link Rdr229SwapMove#validationLabel}), named from the classes that write them and never as literals here: P3.4's
         * per-collection builds start only once every one is complete. A validation label is complete only while its id key is present and
         * validated, and the chunks swap's only while the composite on the id is.
         */
        public static ReadSet p33c() {
            List<String> gate2 = new ArrayList<>();
            gate2.add(LeafIndexPass.swapLabel("chunks"));
            gate2.add(LeafIndexPass.swapLabel("taxonomy_centroids"));
            Rdr229SwapMove.CHILD_TABLES.forEach(t -> gate2.add(Rdr229SwapMove.validationLabel(t)));
            return new ReadSet(p33a().gate1Labels(), gate2);
        }

        /**
         * The read set a running engine reads: the one factory {@link CompletionBootWork#atBoot} and {@link #evaluate(DSLContext)} share, so the
         * boot wiring and the default cannot name different sets. It is the newest phase's set; a later step changes this one line and the test
         * that pins it ({@code CompletionBootReadSetTest}).
         */
        public static ReadSet boot() {
            return p33c();
        }
    }

    /**
     * What P3.4 supplies for gate 3: the collections that REQUIRE an id-predicate index (live, not superseded, and holding a VALID name-predicate
     * index or an id-predicate index of any validity; read from the database by {@code PciGate3Coverage}, so every engine agrees), the ones with a
     * VALID id-predicate index, and the ones whose id index is INVALID. Keys are whatever identifies a collection to the caller; the check only
     * compares them. A collection that qualifies for an index but has none of either kind is not required, except in the empty case
     * ({@link #nothingToIndex}).
     */
    public record IdIndexCoverage(Set<String> planned, Set<String> validIdIndexed, Set<String> invalidIdIndexed,
                                  boolean notApplicable, boolean nothingToIndex, boolean anyIdIndexInvalid) {
        public IdIndexCoverage {
            planned = Set.copyOf(planned);
            validIdIndexed = Set.copyOf(validIdIndexed);
            invalidIdIndexed = Set.copyOf(invalidIdIndexed);
        }

        /** Coverage over the keys the source read; neither not-applicable nor a verified empty store. */
        public IdIndexCoverage(Set<String> planned, Set<String> validIdIndexed, Set<String> invalidIdIndexed) {
            this(planned, validIdIndexed, invalidIdIndexed, false, false, !invalidIdIndexed.isEmpty());
        }

        /** As above with the not-applicable and verified-empty flags and no INVALID id index anywhere. */
        public IdIndexCoverage(Set<String> planned, Set<String> validIdIndexed, Set<String> invalidIdIndexed,
                               boolean notApplicable, boolean nothingToIndex) {
            this(planned, validIdIndexed, invalidIdIndexed, notApplicable, nothingToIndex, !invalidIdIndexed.isEmpty());
        }

        /**
         * The PCI clause does not apply: per-collection indexing is off ({@code NX_SEARCH_PCI=0}), so no per-collection index exists to
         * replace. Reported like {@link StampLabels#NOT_APPLICABLE_WHEN_ABSENT}: gate 3 then rests on its other conditions.
         */
        public static IdIndexCoverage pciOff() {
            return new IdIndexCoverage(Set.of(), Set.of(), Set.of(), true, false, false);
        }

        /**
         * The source read no required index and verified that no live collection reaches the build threshold: there is nothing to
         * index, so an empty plan is not the quiet planner the empty-plan veto guards against.
         */
        public static IdIndexCoverage verifiedEmpty() {
            return new IdIndexCoverage(Set.of(), Set.of(), Set.of(), false, true, false);
        }

        /** Planned collections with no VALID id index: missing, or INVALID. */
        public Set<String> uncovered() {
            Set<String> out = new TreeSet<>(planned);
            out.removeAll(validIdIndexed);
            return out;
        }
    }

    /** Where gate 3 reads P3.4's state. */
    @FunctionalInterface
    public interface Gate3Source {
        /** Empty when the source could not read its state (no source installed, the catalog unreadable): gate 3 then does not hold. */
        Optional<IdIndexCoverage> read(DSLContext ctx);

        /** No source installed (a test, or an engine built without the PCI reconciler): gate 3 cannot hold. */
        Gate3Source NOT_INSTALLED = ctx -> Optional.empty();
    }

    /**
     * RDR-229 P3.7: the two facts besides gate 3 that end the rename refusal. {@code dropMoveComplete}: the name-key drop move
     * ({@link NameKeyDropMove#LABEL}) has {@code completed_at}; a label with no row is incomplete, like every label. {@code listed}
     * and {@code missing}: the P3.5b changeset ids the list names ({@link P35bChangesets}) and those {@code databasechangelog} does
     * not record as applied. A missing or empty list is refused: {@link #p35bApplied} is false then.
     */
    public record RenameState(boolean dropMoveComplete, int listed, List<String> missing, boolean unreadable) {
        public RenameState {
            missing = List.copyOf(missing);
        }

        /** A state that was read. */
        public RenameState(boolean dropMoveComplete, int listed, List<String> missing) {
            this(dropMoveComplete, listed, missing, false);
        }

        /** The state could not be read (a failure or a timeout): the readiness is {@code readiness_unreadable}, never NONE. */
        public static final RenameState UNREADABLE = new RenameState(false, 0, List.of(), true);

        /** Not read yet, or built by hand: the drop move incomplete and no list. */
        public static final RenameState NOT_READ = new RenameState(false, 0, List.of());

        /** Every changeset of a non-empty list has applied. */
        public boolean p35bApplied() {
            return listed > 0 && missing.isEmpty();
        }
    }

    /**
     * R7's pre-flight (RDR-229 item 6, "dangling tombstones"; linda's ruling 2026-11-21): the registry's tombstones whose
     * {@code superseded_by} names no row of their tenant, by content type, and the tombstones the stamp resolved to its fallback
     * (their {@code superseded_by_id} is their own {@code collection_id}: retired, no successor). Counts only: {@code /v1/status}
     * is unauthenticated, so no name or tenant leaves the engine. {@code registryRows} is how many registry rows were read, so a
     * zero is told from a registry that was not read. What the admin role can see: where the migrating role is the serving role and
     * the registry is row-secured with no owner-read policy, it reads no row, and {@code registryRows} says 0.
     */
    public record Dangling(long registryRows, long total, Map<String, Long> byContentType, long selfIdTotal,
                           Map<String, Long> selfIdByContentType, boolean read) {
        public Dangling {
            byContentType = Map.copyOf(new java.util.TreeMap<>(byContentType));
            selfIdByContentType = Map.copyOf(new java.util.TreeMap<>(selfIdByContentType));
        }

        /** The registry was not read (the table is absent, or this result was built by hand). */
        public static final Dangling UNREAD = new Dangling(0L, 0L, Map.of(), 0L, Map.of(), false);
    }

    /** Counts the rows that still violate a table's CHECK. */
    @FunctionalInterface
    interface ViolatorCounter {
        Violators count(DSLContext ctx, P31Checks.Row row);
    }

    /** The counter a run uses: one bounded scan per table, only asked for in the state that needs it. */
    static final ViolatorCounter DIRECT = CompletionCheck::countViolators;

    /**
     * What one evaluation found.
     *
     * @param gate1                   every label of gate 1 is satisfied and {@value #SUPERSEDED_BY_CHECK} exists
     * @param gate2                   gate 1 and every label of gate 2
     * @param gate3                   gate 2 and P3.4 complete (S-2)
     * @param labels                  every label read, in read-set order
     * @param checks                  every listed CHECK of a table that exists
     * @param supersededByCheckPresent the CHECK of item (c)
     * @param supersededByWaiting     while it is missing: the registry rows its changeset waits on (a tombstone with a blank
     *                                model_version and no superseded_by_id), the count nx doctor shows; 0 otherwise
     * @param violators               tables whose stamp is done and whose CHECK is still NOT VALID, with the rows that violate it
     * @param gate3Uncovered          required collections with no VALID id index, when gate 3 was asked
     * @param gate3Why                why gate 3 does not hold, or empty
     * @param gate3PciNotApplicable   the PCI clause of gate 3 did not apply ({@link IdIndexCoverage#pciOff()}: per-collection indexing is off)
     * @param dangling                R7's pre-flight: the registry's tombstones that name no successor, by content type
     * @param gate3Latched            the install's gate 3 latch row exists ({@code nexus.rdr229_gate_latch}): gate 3 has held once, and the engine's
     *                                readers and the name-index retirement go by this, not by the live {@code gate3}
     * @param idIndexInvalid          some id-predicate PCI index is INVALID (building, killed or mid-rebuild); reported on its own, whether or not
     *                                gate 3 has latched
     * @param gate3LatchPciOff        the latch row says gate 3 latched while per-collection indexing was off ({@code pci_off}): no id index was required
     *                                then, so with PCI on the engine builds them first
     * @param rename                  RDR-229 P3.7: what the one-row rename waits for besides gate 3 ({@link RenameState})
     */
    public record Result(boolean gate1, boolean gate2, boolean gate3, List<LabelState> labels, List<CheckState> checks,
                         boolean supersededByCheckPresent, long supersededByWaiting, List<Violators> violators,
                         List<String> gate3Uncovered, String gate3Why, Dangling dangling, boolean gate3PciNotApplicable,
                         boolean gate3Latched, boolean idIndexInvalid, boolean gate3LatchPciOff,
                         RenameState rename) {

        /** As above with no rename read. */
        public Result(boolean gate1, boolean gate2, boolean gate3, List<LabelState> labels, List<CheckState> checks,
                      boolean supersededByCheckPresent, long supersededByWaiting, List<Violators> violators,
                      List<String> gate3Uncovered, String gate3Why, Dangling dangling, boolean gate3PciNotApplicable,
                      boolean gate3Latched, boolean idIndexInvalid, boolean gate3LatchPciOff) {
            this(gate1, gate2, gate3, labels, checks, supersededByCheckPresent, supersededByWaiting, violators, gate3Uncovered,
                gate3Why, dangling, gate3PciNotApplicable, gate3Latched, idIndexInvalid, gate3LatchPciOff, RenameState.NOT_READ);
        }

        /** As above with the latch row not saying PCI was off. */
        public Result(boolean gate1, boolean gate2, boolean gate3, List<LabelState> labels, List<CheckState> checks,
                      boolean supersededByCheckPresent, long supersededByWaiting, List<Violators> violators,
                      List<String> gate3Uncovered, String gate3Why, Dangling dangling, boolean gate3PciNotApplicable,
                      boolean gate3Latched, boolean idIndexInvalid) {
            this(gate1, gate2, gate3, labels, checks, supersededByCheckPresent, supersededByWaiting, violators, gate3Uncovered,
                gate3Why, dangling, gate3PciNotApplicable, gate3Latched, idIndexInvalid, false, RenameState.NOT_READ);
        }

        /** As above with neither a latch nor an INVALID id index. */
        public Result(boolean gate1, boolean gate2, boolean gate3, List<LabelState> labels, List<CheckState> checks,
                      boolean supersededByCheckPresent, long supersededByWaiting, List<Violators> violators,
                      List<String> gate3Uncovered, String gate3Why, Dangling dangling, boolean gate3PciNotApplicable) {
            this(gate1, gate2, gate3, labels, checks, supersededByCheckPresent, supersededByWaiting, violators, gate3Uncovered,
                gate3Why, dangling, gate3PciNotApplicable, false, false, false, RenameState.NOT_READ);
        }

        /** As above with a rename read and the latch, PCI and INVALID-index fields at their defaults. */
        public Result(boolean gate1, boolean gate2, boolean gate3, List<LabelState> labels, List<CheckState> checks,
                      boolean supersededByCheckPresent, long supersededByWaiting, List<Violators> violators,
                      List<String> gate3Uncovered, String gate3Why, Dangling dangling, RenameState rename) {
            this(gate1, gate2, gate3, labels, checks, supersededByCheckPresent, supersededByWaiting, violators, gate3Uncovered,
                gate3Why, dangling, false, false, false, false, rename);
        }

        /** As above with the PCI clause applicable. */
        public Result(boolean gate1, boolean gate2, boolean gate3, List<LabelState> labels, List<CheckState> checks,
                      boolean supersededByCheckPresent, long supersededByWaiting, List<Violators> violators,
                      List<String> gate3Uncovered, String gate3Why, Dangling dangling) {
            this(gate1, gate2, gate3, labels, checks, supersededByCheckPresent, supersededByWaiting, violators, gate3Uncovered,
                gate3Why, dangling, false, false, false, false, RenameState.NOT_READ);
        }

        /** As above with no dangling-tombstone read and no rename read (a caller that built the result by hand). */
        public Result(boolean gate1, boolean gate2, boolean gate3, List<LabelState> labels, List<CheckState> checks,
                      boolean supersededByCheckPresent, long supersededByWaiting, List<Violators> violators,
                      List<String> gate3Uncovered, String gate3Why) {
            this(gate1, gate2, gate3, labels, checks, supersededByCheckPresent, supersededByWaiting, violators, gate3Uncovered,
                gate3Why, Dangling.UNREAD, RenameState.NOT_READ);
        }

        /**
         * The first thing a rename still waits for, the one answer the refusal, {@code GET /v1/status} and the in-process readiness
         * share ({@link RenameReadiness}): {@link RenameReadiness.Missing#NONE} until gate 3 has been latched (the rename is the name path
         * and nothing is awaited), then the name-key drop move, then a missing or empty P3.5b list (a restart cannot fix it), then the
         * restart that applies P3.5b's changesets.
         *
         * <p><b>Keyed on the latched gate 3, not the live one.</b> The live gate 3 can drop back (an id index going INVALID) after it held,
         * and that is never a reason to return to the name path: the readers have switched to the id and, once the drop move has run, the
         * name keys are gone. From the latch a rename is one-row or refused.
         */
        public RenameReadiness.Missing renameMissing() {
            return renameMissing(gate3Latched);
        }

        /**
         * As {@link #renameMissing()} with the latch as the caller reads it now: the engine passes the rename window's regime OR the durable latch
         * ({@link CompletionBootWork#gate3Latched()}), which can be newer than this sample (a sample is read before the latch writer flips the regime).
         */
        public RenameReadiness.Missing renameMissing(boolean latched) {
            return latched ? readinessMissing() : RenameReadiness.Missing.NONE;
        }

        /**
         * What the one-row rename waits for, whether or not gate 3 has latched (linda 2026-11-23): total, and fail closed. The drop move is registered
         * only once the latch is written, so a complete move implies the row; readiness NONE implies it too, and is never answered from an error
         * ({@link RenameReadiness.Missing#READINESS_UNREADABLE}). The status field ({@link #renameMissing()}) keeps saying none before the latch.
         */
        public RenameReadiness.Missing readinessMissing() {
            if (rename.unreadable()) {
                return RenameReadiness.Missing.READINESS_UNREADABLE;
            }
            if (!rename.dropMoveComplete()) {
                return RenameReadiness.Missing.NAME_KEY_DROP_MOVE;
            }
            if (rename.listed() == 0) {
                return RenameReadiness.Missing.P35B_LIST_MISSING;
            }
            return rename.p35bApplied() ? RenameReadiness.Missing.NONE : RenameReadiness.Missing.P35B_RESTART;
        }

        /**
         * Whether a restart is what an install waits for: every stamp move is complete (gate 1) and the P3.5b list names changesets
         * that have not all applied. A missing or empty list is no restart's to fix, so it does not count.
         */
        public boolean restartNeeded() {
            return gate1 && rename.listed() > 0 && !rename.p35bApplied();
        }

        public List<LabelState> incomplete() {
            return labels.stream().filter(l -> !l.satisfied()).toList();
        }

        public long labelsSatisfied() {
            return labels.stream().filter(LabelState::satisfied).count();
        }
    }

    /** {@link #evaluate(DSLContext, ReadSet, Gate3Source, ViolatorCounter)} for the boot read set ({@link ReadSet#boot()}), gate 3 not installed. */
    public static Result evaluate(DSLContext ctx) {
        return evaluate(ctx, ReadSet.boot(), Gate3Source.NOT_INSTALLED, DIRECT);
    }

    /**
     * Reads the registry, the catalog and the CHECK constraints on {@code ctx} (the admin connection) and returns the state of
     * the three gates. Writes nothing.
     */
    static Result evaluate(DSLContext ctx, ReadSet readSet, Gate3Source gate3, ViolatorCounter counter) {
        return evaluate(ctx, readSet, gate3, counter, List.of());
    }

    /**
     * {@link #evaluate(DSLContext, ReadSet, Gate3Source, ViolatorCounter, List)} with the counter a running engine uses, for a caller outside this
     * package that evaluates the gates against a real {@link Gate3Source} (RDR-229 P3.4's end-to-end test).
     */
    public static Result evaluate(DSLContext ctx, ReadSet readSet, Gate3Source gate3,
                                  List<java.util.function.Predicate<DSLContext>> gate3Extras) {
        return evaluate(ctx, readSet, gate3, DIRECT, gate3Extras);
    }

    /**
     * As above with {@code gate3Extras}: further conditions gate 3 needs beside P3.4's id-predicate indexes (the runtime-built id UNIQUE
     * indexes, {@code IdUniqueIndexPass.allValid}, which {@code Main} wires in). Each is read only once gate 2 holds and the PCI coverage is
     * complete, on the check's read-only session; one that is false, or throws, keeps gate 3 closed. Gate 3 implies gate 2 structurally: nothing
     * past the gate-2 test below is read while gate 2 is false. Writes no latch.
     */
    static Result evaluate(DSLContext ctx, ReadSet readSet, Gate3Source gate3, ViolatorCounter counter,
                           List<java.util.function.Predicate<DSLContext>> gate3Extras) {
        return evaluate(ctx, readSet, gate3, counter, gate3Extras, LatchWriter.NONE);
    }

    /**
     * Writes the gate 3 latch row when gate 3 holds ({@code INSERT ... ON CONFLICT DO NOTHING} into {@code nexus.rdr229_gate_latch}). The check's
     * own session is read only at the server, so the engine's reader supplies a writer that uses a session of its own; a caller with a writable
     * {@code ctx} may pass {@link #latchOnCtx}. {@code pciOff} is the posture to record: true when the PCI clause did not apply
     * ({@code NX_SEARCH_PCI=0}), so no id index was required when the gate latched. A writer that throws leaves the latch unwritten and the check
     * unchanged: the next read tries again.
     */
    @FunctionalInterface
    public interface LatchWriter {
        void latch(DSLContext readCtx, String gate, boolean pciOff);

        /** Writes nothing: a caller that only reads (most tests). */
        LatchWriter NONE = (ctx, gate, pciOff) -> { };
    }

    /**
     * The seam the latch write goes through (P3.7, wren's critic): {@code writeRow} is the row write alone. The default runs it at once
     * ({@link #IMMEDIATE}); P3.7 passes one that takes its rename write lock, waits for in-flight renames, runs {@code writeRow}, then flips its
     * own regime, all under the lock. The in-process latched flag ({@code CompletionBootWork#gate3Latched}) is set only after this returns
     * without throwing. A writer that throws leaves the row (unless {@code writeRow} had already run) and the flag unlatched, and the next
     * evaluation tries again. It must be safe to call again after a failure: a row that exists is not written twice ({@code ON CONFLICT DO NOTHING}).
     */
    @FunctionalInterface
    public interface Gate3LatchWriter {
        void latch(Runnable writeRow);

        /** Runs the row write at once. */
        Gate3LatchWriter IMMEDIATE = Runnable::run;
    }

    /** The latch row of gate 3's name. */
    public static final String GATE3 = "gate3";

    private static final org.jooq.Table<?> LATCH = DSL.table(DSL.name("nexus", "rdr229_gate_latch"));
    private static final Field<String> LATCH_GATE = DSL.field(DSL.name("gate"), String.class);

    /** A writer that inserts on the {@code ctx} it is given (a connection that is not read only). */
    public static LatchWriter latchOnCtx(String engineVersion) {
        return (ctx, gate, pciOff) -> insertLatch(ctx, gate, engineVersion, pciOff);
    }

    private static final Field<Boolean> LATCH_PCI_OFF = DSL.field(DSL.name("pci_off"), Boolean.class);

    /**
     * {@code INSERT INTO nexus.rdr229_gate_latch ... ON CONFLICT DO NOTHING}, typed. The first writer's row stands, {@code pci_off} included:
     * it records the posture the gate latched in (no per-collection index was required because {@code NX_SEARCH_PCI=0}).
     *
     * <p>One transaction under {@link RenameLatchLock#exclusive} (RDR-229 P3.5a, ruling D2): the write waits for every collection rename in
     * flight, and a rename that starts after it reads the row, so no rename commits after the latch unseen. The wait is bounded
     * ({@link #LATCH_LOCK_TIMEOUT_MS}); a timeout throws, the row is not written, and the next evaluation tries again.
     */
    public static void insertLatch(DSLContext ctx, String gate, String engineVersion, boolean pciOff) {
        ctx.transaction(cfg -> {
            DSLContext tx = cfg.dsl();
            tx.select(DSL.function("set_config", String.class, DSL.val("lock_timeout"), DSL.val(LATCH_LOCK_TIMEOUT_MS), DSL.val(true))).fetch();
            dev.nexus.service.db.RenameLatchLock.exclusive(tx);
            tx.insertInto(LATCH, LATCH_GATE, DSL.field(DSL.name("latched_at"), OffsetDateTime.class),
                    DSL.field(DSL.name("engine_version"), String.class), LATCH_PCI_OFF)
                .values(gate, OffsetDateTime.now(java.time.ZoneOffset.UTC), engineVersion, pciOff)
                .onConflictDoNothing().execute();
        });
    }

    /** How long the latch write waits for in-flight renames (and for their commit), in milliseconds, as the GUC text. */
    static final String LATCH_LOCK_TIMEOUT_MS = "60000";

    /**
     * The latch row of {@code gate}: empty when there is none (or the table does not exist yet), else its {@code pci_off}. A failed read throws:
     * unknown is never "not latched".
     */
    public static Optional<Boolean> readLatch(DSLContext ctx, String gate) {
        return readLatch(ctx, tablesPresent(ctx), gate);
    }

    static Optional<Boolean> readLatch(DSLContext ctx, Set<String> tables, String gate) {
        if (!tables.contains("rdr229_gate_latch")) {
            return Optional.empty();
        }
        return Optional.ofNullable(ctx.select(LATCH_PCI_OFF).from(LATCH).where(LATCH_GATE.eq(gate)).fetchOne(LATCH_PCI_OFF));
    }

    /**
     * As above, and {@code latchWriter}: called once when gate 3 holds and the latch row does not exist yet (gate 3 holds, after the gate 2 test,
     * the PCI clause and every further condition). The result's {@code gate3Latched} is the row's existence after that call, and
     * {@code gate3LatchPciOff} its recorded posture.
     */
    static Result evaluate(DSLContext ctx, ReadSet readSet, Gate3Source gate3, ViolatorCounter counter,
                           List<java.util.function.Predicate<DSLContext>> gate3Extras, LatchWriter latchWriter) {
        return evaluate(ctx, readSet, gate3, counter, gate3Extras, latchWriter, P35bChangesets.resource());
    }

    /**
     * As above, with the P3.5b changeset ids the one-row rename waits for supplied by {@code p35b} (the resource {@link P35bChangesets} reads
     * in production; a test passes its own list).
     */
    static Result evaluate(DSLContext ctx, ReadSet readSet, Gate3Source gate3, ViolatorCounter counter,
                           java.util.function.Supplier<Set<String>> p35b) {
        return evaluate(ctx, readSet, gate3, counter, List.of(), LatchWriter.NONE, p35b);
    }

    /** The evaluation proper: the latch writer and the P3.5b list are both supplied. */
    static Result evaluate(DSLContext ctx, ReadSet readSet, Gate3Source gate3, ViolatorCounter counter,
                           List<java.util.function.Predicate<DSLContext>> gate3Extras, LatchWriter latchWriter,
                           java.util.function.Supplier<Set<String>> p35b) {
        Objects.requireNonNull(gate3Extras, "gate3Extras");
        Objects.requireNonNull(latchWriter, "latchWriter");
        Objects.requireNonNull(readSet, "readSet");
        Objects.requireNonNull(p35b, "p35b");
        Objects.requireNonNull(gate3, "gate3");
        Objects.requireNonNull(counter, "counter");
        Map<String, Move> moves = readMoves(ctx);
        Map<String, List<P31Checks.Row>> checksByTable = P31Checks.checksByTable();
        Set<String> tables = tablesPresent(ctx);
        Map<String, Boolean> checkValidated = readChecks(ctx, checksByTable, tables);
        Map<String, Boolean> twinValidated = readTwins(ctx, tables);

        List<CheckState> checks = new ArrayList<>();
        for (var e : checksByTable.entrySet()) {
            if (!tables.contains(e.getKey())) {
                continue;
            }
            for (P31Checks.Row row : e.getValue()) {
                Boolean validated = checkValidated.get(row.constraint());
                checks.add(new CheckState(row.table(), row.constraint(), validated != null, Boolean.TRUE.equals(validated)));
            }
        }
        boolean supersededByPresent = checkValidated.containsKey(SUPERSEDED_BY_CHECK);
        long waiting = supersededByPresent ? 0L : supersededByWaiting(ctx, tables, null);

        List<Violators> violators = new ArrayList<>();
        Set<String> labelsRead = new LinkedHashSet<>(readSet.gate1Labels());
        labelsRead.addAll(readSet.gate2Labels());
        Map<String, LabelState> states = new LinkedHashMap<>();
        for (String label : labelsRead) {
            states.put(label, labelState(ctx, label, moves, checksByTable, tables, checkValidated, twinValidated, counter, violators));
        }

        boolean gate1 = supersededByPresent
            && readSet.gate1Labels().stream().allMatch(l -> states.get(l).satisfied());
        boolean gate2 = gate1 && readSet.gate2Labels().stream().allMatch(l -> states.get(l).satisfied());

        boolean gate3Holds = false;
        boolean pciNotApplicable = false;
        boolean idInvalid = false;
        List<String> uncovered = List.of();
        String why = "";
        if (!gate2) {
            why = "gate 2 does not hold";
        } else {
            Optional<IdIndexCoverage> coverage = gate3.read(ctx);
            idInvalid = coverage.map(IdIndexCoverage::anyIdIndexInvalid).orElse(false);
            if (coverage.isEmpty()) {
                why = "the PCI coverage (P3.4) is not installed or could not be read (no catalog, or no registry rows visible), so nothing says the id indexes exist";
            } else if (coverage.get().notApplicable()) {
                pciNotApplicable = true;
                gate3Holds = extrasHold(ctx, gate3Extras);
                if (!gate3Holds) {
                    why = furtherConditionWhy(ctx);
                }
            } else if (coverage.get().planned().isEmpty() && !coverage.get().nothingToIndex()
                && liveCollections(ctx, tables, null) > 0) {
                // Review F5: an empty plan over a populated registry is a planner that found nothing, or failed quietly; it
                // is a pass only when there is nothing to plan.
                // no count: the text goes out on the unauthenticated /v1/status, and a count of collections describes tenants
                why = "no collection requires an id-predicate index while live collections are registered, so nothing says they are covered";
            } else {
                uncovered = new ArrayList<>(coverage.get().uncovered());
                gate3Holds = uncovered.isEmpty();
                if (!gate3Holds) {
                    why = "collections that hold a VALID name-predicate index or an id-predicate index have no VALID id-predicate index (missing or INVALID)";
                } else if (!extrasHold(ctx, gate3Extras)) {
                    gate3Holds = false;
                    why = furtherConditionWhy(ctx);
                }
            }
        }
        Optional<Boolean> latch = readLatch(ctx, tables, GATE3);
        if (gate3Holds && latch.isEmpty() && tables.contains("rdr229_gate_latch")) {
            boolean written = true;
            try {
                latchWriter.latch(ctx, GATE3, pciNotApplicable);
            } catch (RuntimeException e) {   // not latched yet: the next read tries again
                written = false;
                log.warn("event=rdr229_gate3_latch_not_written cause=\"{}\"", e.toString());
            }
            // A failed writer leaves this read unlatched, whatever the row says; the row it did write (if any) is read next time.
            latch = written ? readLatch(ctx, tables, GATE3) : Optional.empty();
        }
        return new Result(gate1, gate2, gate3Holds, List.copyOf(states.values()), List.copyOf(checks), supersededByPresent,
            waiting, List.copyOf(violators), List.copyOf(uncovered), why, danglingTombstones(ctx, tables, null), pciNotApplicable,
            latch.isPresent(), idInvalid, latch.orElse(false), readRenameState(ctx, moves, p35b));
    }

    /**
     * Why the further gate 3 conditions (the runtime-built id unique indexes) do not hold: names the changesets their twins still wait for
     * ({@link IdUniqueIndexPass#waitingChangesets}; ids only, no tenant or collection), when there are any. A failed read adds nothing.
     */
    private static String furtherConditionWhy(DSLContext ctx) {
        String base = "a further gate 3 condition does not hold (the runtime-built id unique indexes)";
        try {
            List<String> waiting = IdUniqueIndexPass.waitingChangesets(ctx);
            return waiting.isEmpty() ? base : base + "; an id twin waits for changeset" + (waiting.size() > 1 ? "s " : " ") + String.join(", ", waiting);
        } catch (RuntimeException e) {
            return base;
        }
    }

    private static boolean extrasHold(DSLContext ctx, List<java.util.function.Predicate<DSLContext>> extras) {
        for (var extra : extras) {
            boolean holds;
            try {
                holds = extra.test(ctx);
            } catch (RuntimeException e) {   // unknown is not "holds"
                holds = false;
            }
            if (!holds) {
                return false;
            }
        }
        return true;
    }

    /**
     * The bound on the readiness reads (the databasechangelog read), as {@code SET LOCAL statement_timeout}, per completion SAMPLE and not per rename: a
     * rename answers from the last sample and reads nothing of its own.
     */
    public static final long READINESS_READ_TIMEOUT_MS = 2000L;

    /**
     * {@link #renameState} under {@link #READINESS_READ_TIMEOUT_MS}, total: any failure or timeout, of the list or of the database, is
     * {@link RenameState#UNREADABLE} and never an exception or a NONE (a sample that threw would leave the last answer standing; this one says so).
     */
    private static RenameState readRenameState(DSLContext ctx, Map<String, Move> moves, java.util.function.Supplier<Set<String>> p35b) {
        try {
            Set<String> listed = p35b.get();
            return ctx.transactionResult(cfg -> {
                DSLContext tx = cfg.dsl();
                tx.select(DSL.function("set_config", String.class, DSL.val("statement_timeout"), DSL.val(Long.toString(READINESS_READ_TIMEOUT_MS)), DSL.val(true)))
                    .fetch();
                return renameState(tx, moves, listed);
            });
        } catch (RuntimeException e) {
            log.warn("event=rdr229_rename_readiness_unreadable cause=\"{}\"", e.toString());
            return RenameState.UNREADABLE;
        }
    }

    /** P3.7: the name-key drop move's completion (a label with no row is incomplete) and the P3.5b changesets not yet in databasechangelog. */
    private static RenameState renameState(DSLContext ctx, Map<String, Move> moves, Set<String> listed) {
        boolean dropped = complete(ctx, NameKeyDropMove.LABEL, moves, new HashSet<>());
        if (listed.isEmpty()) {
            return new RenameState(dropped, 0, List.of());
        }
        Set<String> missing = new TreeSet<>(listed);
        var log = DSL.table(DSL.name("public", "databasechangelog"));
        if (ctx.fetchExists(DSL.selectOne().where(DSL.function("to_regclass", Object.class, DSL.val("public.databasechangelog")).isNotNull()))) {
            Field<String> id = DSL.field(DSL.name("id"), String.class);
            Field<String> execType = DSL.field(DSL.name("exectype"), String.class);
            missing.removeAll(ctx.select(id).from(log).where(id.in(listed)).and(execType.in("EXECUTED", "RERAN", "MARK_RAN"))
                .fetch(id));
        }
        return new RenameState(dropped, listed.size(), List.copyOf(missing));
    }

    // ---- one label ---------------------------------------------------------------------------------------------------

    private static LabelState labelState(DSLContext ctx, String label, Map<String, Move> moves,
                                         Map<String, List<P31Checks.Row>> checksByTable, Set<String> tables,
                                         Map<String, Boolean> checkValidated, Map<String, Boolean> twinValidated,
                                         ViolatorCounter counter, List<Violators> violators) {
        Optional<ConstraintRef> twin = FkTwins.byLabel(label).map(r -> new ConstraintRef(r.table(), r.twin()))
            .or(() -> Optional.ofNullable(P33C_CONSTRAINTS.get(label)));
        String table = twin.map(ConstraintRef::table).orElseGet(() -> StampLabels.tableOf(label).orElse(null));
        Move row = moves.get(label);

        boolean done = complete(ctx, label, moves, new HashSet<>());
        StampLabels.Applicability only = StampLabels.NOT_APPLICABLE_WHEN_ABSENT.get(label);
        if (!done && only != null && !only.holds(ctx, tables) && (row == null || row.failed())) {
            // The queue's label with no row (a fresh install: aspects-005 dropped the table) or a failed one (the column
            // vectors-068-18 adds is missing on an install whose kept table another role owns): there is nothing to stamp.
            return new LabelState(label, table, Status.NOT_APPLICABLE, "n/a: " + only.table()
                + (tables.contains(only.table()) ? " has no " + only.column() + " column" : " is absent"));
        }
        if (!done) {
            return new LabelState(label, table, Status.INCOMPLETE, row == null ? "no row in nexus.data_moves"
                : (row.failed() ? "failed, and its driving table exists" : "open"));
        }
        if (twin.isPresent()) {
            // P3.3a: the move is done, and the label is complete only while its twin is on the table and validated (the stamp
            // label's CHECK rule). The twin is a foreign key, not a row count: a NOT VALID one waits on nothing the check can count.
            Boolean validated = twinValidated.get(twin.get().constraint());
            if (validated == null) {
                return tables.contains(table)
                    ? new LabelState(label, table, Status.INCOMPLETE, "foreign key " + twin.get().constraint() + " is missing")
                    : new LabelState(label, table, Status.COMPLETE, "");
            }
            return validated ? new LabelState(label, table, Status.COMPLETE, "")
                : new LabelState(label, table, Status.INCOMPLETE, "foreign key " + twin.get().constraint() + " is NOT VALID");
        }
        if (table == null || !checksByTable.containsKey(table)) {
            return new LabelState(label, table, Status.COMPLETE, "");
        }
        // The move is done: its table's CHECKs must be there and validated, or a VALIDATE is waiting on rows. Every NOT VALID CHECK
        // of the table is counted, not the first: on a table with two name columns the orphan may sit behind the second.
        List<Violators> waiting = new ArrayList<>();
        for (P31Checks.Row check : checksByTable.get(table)) {
            Boolean validated = checkValidated.get(check.constraint());
            if (validated == null) {
                if (!tables.contains(table)) {
                    return new LabelState(label, table, Status.COMPLETE, "");
                }
                return new LabelState(label, table, Status.INCOMPLETE, "CHECK " + check.constraint() + " is missing");
            }
            if (!validated) {
                waiting.add(counter.count(ctx, check));
            }
        }
        if (waiting.isEmpty()) {
            return new LabelState(label, table, Status.COMPLETE, "");
        }
        violators.addAll(waiting);
        // no count in the text: it goes out on the unauthenticated /v1/status (GET /v1/catalog/rdr229_completion_counts has the caller's own)
        return new LabelState(label, table, Status.INCOMPLETE, "CHECK "
            + waiting.stream().map(Violators::constraint).collect(java.util.stream.Collectors.joining(", ")) + " is NOT VALID");
    }

    /** A move row, as far as the check reads it. */
    private record Move(String label, boolean completed, boolean failed, String schema, String table, String supersedes) { }

    private static Map<String, Move> readMoves(DSLContext ctx) {
        Field<String> label = DSL.field(DSL.name("label"), String.class);
        Field<OffsetDateTime> completedAt = DSL.field(DSL.name("completed_at"), SQLDataType.TIMESTAMPWITHTIMEZONE);
        Field<OffsetDateTime> failedAt = DSL.field(DSL.name("failed_at"), SQLDataType.TIMESTAMPWITHTIMEZONE);
        Field<String> schema = DSL.field(DSL.name("driving_schema"), String.class);
        Field<String> table = DSL.field(DSL.name("driving_table"), String.class);
        Field<String> supersedes = DSL.field(DSL.name("supersedes"), String.class);
        Map<String, Move> out = new HashMap<>();
        for (var r : ctx.select(label, completedAt, failedAt, schema, table, supersedes).from(MOVES).fetch()) {
            out.put(r.value1(), new Move(r.value1(), r.value2() != null, r.value3() != null, r.value4(), r.value5(),
                r.value6()));
        }
        return out;
    }

    /** Complete: {@code completed_at}, or a completed superseding move, or failed with a driving table that no longer exists. */
    private static boolean complete(DSLContext ctx, String label, Map<String, Move> moves, Set<String> seen) {
        Move m = moves.get(label);
        if (m == null || !seen.add(label)) {
            return false;
        }
        if (m.completed()) {
            return true;
        }
        for (Move other : moves.values()) {
            if (label.equals(other.supersedes()) && complete(ctx, other.label(), moves, seen)) {
                return true;
            }
        }
        // to_regclass of the driving table is NULL (the table is gone): the move cannot ever run, and a table that is gone
        // has nothing to stamp (RDR-246: how aspect_extraction_queue ends, 42P01 is permanent).
        return m.failed() && !relationExists(ctx, m.schema(), m.table());
    }

    private static boolean relationExists(DSLContext ctx, String schema, String table) {
        var c = DSL.table(DSL.name("pg_catalog", "pg_class")).as("c");
        var n = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        return ctx.fetchExists(DSL.selectOne().from(c)
            .join(n).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq(table)));
    }

    // ---- catalog reads -----------------------------------------------------------------------------------------------

    static Set<String> tablesPresent(DSLContext ctx) {
        var c = DSL.table(DSL.name("pg_catalog", "pg_class")).as("c");
        var n = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        Field<String> relname = DSL.field(DSL.name("c", "relname"), String.class);
        return new HashSet<>(ctx.select(relname).from(c)
            .join(n).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("c", "relkind"), String.class).in("r", "p"))
            .fetch(relname));
    }

    /** constraint name to {@code convalidated}, for the listed CHECKs found on their own (parent) table. */
    static Map<String, Boolean> readChecks(DSLContext ctx, Map<String, List<P31Checks.Row>> byTable, Set<String> tables) {
        Set<String> names = new HashSet<>();
        Set<String> owners = new HashSet<>();
        for (var e : byTable.entrySet()) {
            if (tables.contains(e.getKey())) {
                owners.add(e.getKey());
                e.getValue().forEach(r -> names.add(r.constraint()));
            }
        }
        Map<String, Boolean> out = new HashMap<>();
        if (names.isEmpty()) {
            return out;
        }
        var con = DSL.table(DSL.name("pg_catalog", "pg_constraint")).as("con");
        var cls = DSL.table(DSL.name("pg_catalog", "pg_class")).as("cls");
        var ns = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("ns");
        Field<String> conname = DSL.field(DSL.name("con", "conname"), String.class);
        Field<String> relname = DSL.field(DSL.name("cls", "relname"), String.class);
        Field<Boolean> validated = DSL.field(DSL.name("con", "convalidated"), Boolean.class);
        for (var r : ctx.select(conname, relname, validated).from(con)
                .join(cls).on(DSL.field(DSL.name("cls", "oid")).eq(DSL.field(DSL.name("con", "conrelid"))))
                .join(ns).on(DSL.field(DSL.name("ns", "oid")).eq(DSL.field(DSL.name("cls", "relnamespace"))))
                .where(DSL.field(DSL.name("ns", "nspname"), String.class).eq("nexus"))
                .and(conname.in(names)).and(relname.in(owners)).fetch()) {
            // the constraint of its own table only: a leaf's inherited copy has another relname
            for (var e : byTable.entrySet()) {
                if (e.getKey().equals(r.value2()) && e.getValue().stream().anyMatch(x -> x.constraint().equals(r.value1()))) {
                    out.put(r.value1(), r.value3());
                }
            }
        }
        return out;
    }

    /** constraint name to {@code convalidated}, for the P3.3a id twins and P3.3c's id keys found on their own table. */
    static Map<String, Boolean> readTwins(DSLContext ctx, Set<String> tables) {
        Map<String, String> tableOfTwin = new HashMap<>();
        for (FkTwins.Row r : FkTwins.load()) {
            if (tables.contains(r.table())) {
                tableOfTwin.put(r.twin(), r.table());
            }
        }
        for (ConstraintRef r : P33C_CONSTRAINTS.values()) {
            if (tables.contains(r.table())) {
                tableOfTwin.put(r.constraint(), r.table());
            }
        }
        Map<String, Boolean> out = new HashMap<>();
        if (tableOfTwin.isEmpty()) {
            return out;
        }
        var con = DSL.table(DSL.name("pg_catalog", "pg_constraint")).as("con");
        var cls = DSL.table(DSL.name("pg_catalog", "pg_class")).as("cls");
        var ns = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("ns");
        Field<String> conname = DSL.field(DSL.name("con", "conname"), String.class);
        Field<String> relname = DSL.field(DSL.name("cls", "relname"), String.class);
        Field<Boolean> validated = DSL.field(DSL.name("con", "convalidated"), Boolean.class);
        for (var r : ctx.select(conname, relname, validated).from(con)
                .join(cls).on(DSL.field(DSL.name("cls", "oid")).eq(DSL.field(DSL.name("con", "conrelid"))))
                .join(ns).on(DSL.field(DSL.name("ns", "oid")).eq(DSL.field(DSL.name("cls", "relnamespace"))))
                .where(DSL.field(DSL.name("ns", "nspname"), String.class).eq("nexus"))
                .and(DSL.field(DSL.name("con", "contype"), String.class).eq("f"))
                // the key itself, not the copies PostgreSQL keeps per referenced partition of a key into a partitioned table (chunks)
                .and(DSL.field(DSL.name("con", "conparentid"), Long.class).eq(0L))
                .and(conname.in(tableOfTwin.keySet())).fetch()) {
            // the constraint of its own table only
            if (r.value2().equals(tableOfTwin.get(r.value1()))) {
                out.put(r.value1(), r.value3());
            }
        }
        return out;
    }

    static boolean columnExists(DSLContext ctx, String table, String column) {
        var a = DSL.table(DSL.name("pg_catalog", "pg_attribute")).as("a");
        var c = DSL.table(DSL.name("pg_catalog", "pg_class")).as("c");
        var n = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        return ctx.fetchExists(DSL.selectOne().from(a)
            .join(c).on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("a", "attrelid"))))
            .join(n).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq(table))
            .and(DSL.field(DSL.name("a", "attname"), String.class).eq(column))
            .and(DSL.field(DSL.name("a", "attisdropped"), Boolean.class).isFalse()));
    }

    /**
     * Registry rows vectors-068-19's precondition waits on, counted the way the precondition counts them; of one tenant when
     * {@code tenant} is given (the authenticated counts route), of every tenant otherwise (the in-memory result only).
     */
    static long supersededByWaiting(DSLContext ctx, Set<String> tables, String tenant) {
        if (!tables.contains("catalog_collections") || !columnExists(ctx, "catalog_collections", "superseded_by_id")) {
            return 0L;
        }
        Table<?> c = DSL.table(DSL.name("nexus", "catalog_collections"));
        Field<String> supersededBy = DSL.field(DSL.name("superseded_by"), String.class);
        Field<Long> supersededById = DSL.field(DSL.name("superseded_by_id"), Long.class);
        Field<String> modelVersion = DSL.field(DSL.name("model_version"), String.class);
        return ctx.fetchCount(c, supersededBy.ne("").and(supersededById.isNull()).and(modelVersion.eq("")).and(tenantScope(tenant, "tenant_id")));
    }

    /** The condition that scopes a read to {@code tenant} (none when null), on the column {@code column} of the alias-free or aliased table. */
    private static Condition tenantScope(String tenant, String... column) {
        return tenant == null ? DSL.noCondition() : DSL.field(DSL.name(column), String.class).eq(tenant);
    }

    /** Live registry rows (not a tombstone, not a quarantine): what the planner could index; one tenant's when {@code tenant} is given. */
    static long liveCollections(DSLContext ctx, Set<String> tables, String tenant) {
        if (!tables.contains("catalog_collections")) {
            return 0L;
        }
        Table<?> c = DSL.table(DSL.name("nexus", "catalog_collections"));
        Condition live = DSL.field(DSL.name("superseded_by"), String.class).eq("");
        if (columnExists(ctx, "catalog_collections", "lifecycle_state")) {
            live = live.and(DSL.field(DSL.name("lifecycle_state"), String.class).eq("live"));
        }
        return ctx.fetchCount(c, live.and(tenantScope(tenant, "tenant_id")));
    }

    /** R7's pre-flight: see {@link Dangling}. A read, bounded by the registry's size (hundreds of rows); one tenant's when {@code tenant} is given. */
    static Dangling danglingTombstones(DSLContext ctx, Set<String> tables, String tenant) {
        if (!tables.contains("catalog_collections")) {
            return Dangling.UNREAD;
        }
        Table<?> c = DSL.table(DSL.name("nexus", "catalog_collections")).as("c");
        Table<?> s = DSL.table(DSL.name("nexus", "catalog_collections")).as("s");
        Field<String> contentType = DSL.field(DSL.name("c", "content_type"), String.class);
        Field<String> supersededBy = DSL.field(DSL.name("c", "superseded_by"), String.class);
        Condition dangling = supersededBy.ne("").andNotExists(DSL.selectOne().from(s)
            .where(DSL.field(DSL.name("s", "tenant_id"), String.class).eq(DSL.field(DSL.name("c", "tenant_id"), String.class)))
            .and(DSL.field(DSL.name("s", "name"), String.class).eq(supersededBy)));
        Condition scope = tenantScope(tenant, "c", "tenant_id");
        dangling = dangling.and(scope);
        Map<String, Long> byType = countsByType(ctx, c, contentType, dangling);
        Map<String, Long> selfByType = Map.of();
        if (columnExists(ctx, "catalog_collections", "superseded_by_id")) {
            selfByType = countsByType(ctx, c, contentType, supersededBy.ne("").and(
                DSL.field(DSL.name("c", "superseded_by_id"), Long.class).eq(DSL.field(DSL.name("c", "collection_id"), Long.class))).and(scope));
        }
        return new Dangling(ctx.fetchCount(c, scope), byType.values().stream().mapToLong(Long::longValue).sum(), byType,
            selfByType.values().stream().mapToLong(Long::longValue).sum(), selfByType, true);
    }

    private static Map<String, Long> countsByType(DSLContext ctx, Table<?> c, Field<String> contentType, Condition where) {
        Map<String, Long> out = new LinkedHashMap<>();
        for (var r : ctx.select(contentType, DSL.count()).from(c).where(where).groupBy(contentType).fetch()) {
            out.put(r.value1(), r.value2().longValue());
        }
        return out;
    }

    // ---- the rows that keep a CHECK NOT VALID -------------------------------------------------------------------------

    private static Violators countViolators(DSLContext ctx, P31Checks.Row row) {
        try {
            return ctx.transactionResult(cfg -> {
                DSLContext tx = cfg.dsl();
                // PgSession.setLocal binds the matching network timeout with the statement bound (nexus-u9zkn)
                PgSession.setLocal(tx, "statement_timeout", Long.toString(VIOLATOR_TIMEOUT_MS));
                return count(tx, row, null);
            });
        } catch (DataAccessException e) {
            return new Violators(row.table(), row.constraint(), 0L, 0L, false);
        } finally {
            // the session outlives this scan (CompletionBootWork keeps it between samples): its short read bound does not
            try {
                ctx.connection(c -> PgSession.restoreNetworkTimeout(c, 0));
            } catch (DataAccessException e) {
                // a connection that cannot be reached fails its next read on its own
            }
        }
    }

    /** The violating rows of one CHECK; of one tenant when {@code tenant} is given (the authenticated counts route), of all otherwise. */
    static Violators count(DSLContext tx, P31Checks.Row row, String tenant) {
        Table<?> t = DSL.table(DSL.name("nexus", row.table())).as("t");
        Field<Long> id = DSL.field(DSL.name("t", row.idColumn()), Long.class);
        // every listed table has a tenant_id column; the explicit equality is on top of the row-level security a tenant-scoped session has
        Condition scope = tenantScope(tenant, "t", "tenant_id");
        if ("NOT_NULL".equals(row.kind())) {
            long n = tx.fetchCount(t, id.isNull().and(scope));
            return new Violators(row.table(), row.constraint(), 0L, n, true);
        }
        Field<String> name = DSL.field(DSL.name("t", row.nameColumn()), String.class);
        if ("SUPERSEDED".equals(row.kind())) {
            // a tombstone that names no registry row takes its own id (R7): never an orphan, always still to stamp
            return new Violators(row.table(), row.constraint(), 0L, tx.fetchCount(t, name.ne("").and(id.isNull()).and(scope)), true);
        }
        Condition violating = DSL.nullif(name, "").isNotNull().and(id.isNull()).and(scope);
        Table<?> r = DSL.table(DSL.name("nexus", "catalog_collections")).as("r");
        Field<String> registryName = DSL.field(DSL.name("r", "name"), String.class);
        Condition join = registryName.eq(name);
        if (columnExists(tx, row.table(), "tenant_id")) {
            join = join.and(DSL.field(DSL.name("r", "tenant_id"), String.class)
                .eq(DSL.field(DSL.name("t", "tenant_id"), String.class)));
        }
        var rec = tx.select(
                DSL.count().filterWhere(registryName.isNull()),
                DSL.count().filterWhere(registryName.isNotNull()))
            .from(t).leftJoin(r).on(join).where(violating).fetchOne();
        return new Violators(row.table(), row.constraint(), rec.value1(), rec.value2(), true);
    }
}
