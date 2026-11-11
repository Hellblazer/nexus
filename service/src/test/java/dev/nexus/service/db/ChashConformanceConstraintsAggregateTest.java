// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.db.ChashConformanceConstraints.Entry;
import dev.nexus.service.db.ChashConformanceConstraints.Report;
import dev.nexus.service.db.ChashConformanceConstraints.Row;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd: how {@code GET /v1/admin/chash-conformance} folds the {@code pg_constraint} rows of a partitioned table.
 * Pure, so the shapes PostgreSQL will not build on request (a parent valid over a copy that is not) are still pinned.
 * Each case names the mutation of {@link ChashConformanceConstraints#aggregate} it kills.
 */
class ChashConformanceConstraintsAggregateTest {

    private static final String CHUNKS_CHECK = "chunks_chash_octet_check";
    private static final String MANIFEST_CHECK = "catalog_document_chunks_chash_octet_check";
    private static final long CHUNKS = 100, MODEL_PARTITION = 101, LEAF_A = 102, LEAF_B = 103, MANIFEST = 200, RETIRED = 300;

    private static Row chunksRoot(boolean valid) {
        return new Row(CHUNKS, 0, "chunks", CHUNKS_CHECK, valid);
    }

    private static Row manifestRoot(boolean valid) {
        return new Row(MANIFEST, 0, "catalog_document_chunks", MANIFEST_CHECK, valid);
    }

    /** chunks over a model partition over two leaves: the depth a model-then-tenant partitioning has. */
    private static List<Row> chunksTree(boolean root, boolean model, boolean leafA, boolean leafB) {
        return new ArrayList<>(List.of(
            chunksRoot(root),
            new Row(MODEL_PARTITION, CHUNKS, "chunks_m1", CHUNKS_CHECK, model),
            new Row(LEAF_A, MODEL_PARTITION, "chunks_m1_t_a", CHUNKS_CHECK, leafA),
            new Row(LEAF_B, MODEL_PARTITION, "chunks_m1_t_b", CHUNKS_CHECK, leafB)));
    }

    private static Entry chunks(Report report) {
        return report.constraints().get(0);
    }

    /** Kills: counting only direct children (the depth-two leaves would be missed: partitions 1, not 3). */
    @Test
    void everyCopyAtAnyDepthIsCounted() {
        var rows = chunksTree(true, true, true, true);
        rows.add(manifestRoot(true));
        Report report = ChashConformanceConstraints.aggregate(rows);
        assertThat(chunks(report)).isEqualTo(new Entry("nexus.chunks", CHUNKS_CHECK, true, true, 3, 0));
        assertThat(report.constraints().get(1)).isEqualTo(
            new Entry("nexus.catalog_document_chunks", MANIFEST_CHECK, true, true, 0, 0));
        assertThat(report.allValidated()).isTrue();
    }

    /** Kills: reading the root's own convalidated alone (the mix would read valid). The mix is the parent-valid, copy-not-valid case. */
    @Test
    void aParentValidOverACopyNotValidReadsNotValidated() {
        var rows = chunksTree(true, true, true, false);
        rows.add(manifestRoot(true));
        Report report = ChashConformanceConstraints.aggregate(rows);
        assertThat(chunks(report).validated()).isFalse();
        assertThat(chunks(report).present()).isTrue();
        assertThat(chunks(report).partitions()).isEqualTo(3);
        assertThat(chunks(report).partitionsNotValidated()).isEqualTo(1);
        assertThat(report.allValidated()).isFalse();
    }

    /** Kills: reading the copies alone, ignoring the root. */
    @Test
    void aParentNotValidOverValidCopiesReadsNotValidated() {
        var rows = chunksTree(false, true, true, true);
        rows.add(manifestRoot(true));
        assertThat(chunks(ChashConformanceConstraints.aggregate(rows)).validated()).isFalse();
    }

    /** Kills: an intermediate (model) partition left out of the walk, so a not-valid leaf below it is not reached. */
    @Test
    void aNotValidCopyUnderAValidIntermediatePartitionIsFound() {
        var rows = chunksTree(true, true, false, true);
        rows.add(manifestRoot(true));
        var entry = chunks(ChashConformanceConstraints.aggregate(rows));
        assertThat(entry.validated()).isFalse();
        assertThat(entry.partitionsNotValidated()).isEqualTo(1);
    }

    /** Kills: taking a same-named constraint on a stray table as the root, or counting it as a copy. */
    @Test
    void aSameNamedConstraintOnAnUnrelatedTableIsNeitherRootNorCopy() {
        var rows = chunksTree(true, true, true, true);
        rows.add(manifestRoot(true));
        rows.add(new Row(RETIRED, 0, "chunks_retired_225", CHUNKS_CHECK, false));
        var entry = chunks(ChashConformanceConstraints.aggregate(rows));
        assertThat(entry.validated()).as("the retired table's NOT VALID copy is not ours").isTrue();
        assertThat(entry.partitions()).isEqualTo(3);
    }

    /** Kills: taking a root by constraint name alone: a stray same-named table must not stand in for a missing root. */
    @Test
    void aMissingRootIsAbsentAndNotValid_whateverElseCarriesTheName() {
        var rows = new ArrayList<>(List.of(new Row(RETIRED, 0, "chunks_retired_225", CHUNKS_CHECK, true), manifestRoot(true)));
        Report report = ChashConformanceConstraints.aggregate(rows);
        assertThat(chunks(report)).isEqualTo(new Entry("nexus.chunks", CHUNKS_CHECK, false, false, 0, 0));
        assertThat(report.allValidated()).isFalse();
    }

    /** Kills: all_validated true on an empty answer, or when one target is missing (a vacuous true). */
    @Test
    void allValidatedNeedsEveryTargetPresentAndValid() {
        assertThat(ChashConformanceConstraints.aggregate(List.of()).allValidated()).isFalse();
        assertThat(new Report(List.of()).allValidated()).as("an empty report proves nothing").isFalse();
        assertThat(ChashConformanceConstraints.aggregate(List.of(chunksRoot(true))).allValidated()).isFalse();
        assertThat(ChashConformanceConstraints.aggregate(List.of(chunksRoot(true), manifestRoot(false))).allValidated()).isFalse();
        assertThat(ChashConformanceConstraints.aggregate(List.of(chunksRoot(true), manifestRoot(true))).allValidated()).isTrue();
    }

    /** Kills: a copy whose parent chain is broken (its parent has no row) being counted for a root it does not reach. */
    @Test
    void aCopyWhoseChainDoesNotReachTheRootIsNotCounted() {
        var rows = new ArrayList<>(List.of(chunksRoot(true), manifestRoot(true),
            new Row(LEAF_A, 999, "orphan_leaf", CHUNKS_CHECK, false)));
        var entry = chunks(ChashConformanceConstraints.aggregate(rows));
        assertThat(entry.partitions()).isZero();
        assertThat(entry.validated()).isTrue();
    }
}
