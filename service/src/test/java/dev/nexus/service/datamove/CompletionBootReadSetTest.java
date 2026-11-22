// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.db.AdminConnection;
import org.junit.jupiter.api.Test;

import dev.nexus.service.vectors.LeafIndexPass;
import dev.nexus.service.vectors.Rdr229SwapMove;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * RDR-229 P3.3a and P3.3c (nexus-p2m1x.19, nexus-p2m1x.21): what a running engine's completion check reads. {@code Main} builds the reporter only through
 * {@link CompletionBootWork#atBoot}, which reads {@link CompletionCheck.ReadSet#boot()}, which is P3.3c's set (P3.3a's, and in gate 2 the swap and validation labels); so reverting {@code Main} to P3.1's
 * set, or the factory to {@code p31()}, fails here without a database.
 */
class CompletionBootReadSetTest {

    private static final Path MAIN = Path.of("src", "main", "java", "dev", "nexus", "service", "Main.java");

    @Test
    void theBootReadSetIsP33csSet_whichKeepsP33asGate1AndAddsTheSwapAndValidationLabelsToGate2() {
        assertThat(CompletionCheck.ReadSet.boot()).isEqualTo(CompletionCheck.ReadSet.p33c());
        assertThat(CompletionCheck.ReadSet.boot().gate1Labels()).containsAll(FkTwins.labels()).containsAll(StampLabels.all()).hasSize(23)
            .isEqualTo(CompletionCheck.ReadSet.p33a().gate1Labels());
        assertThat(CompletionCheck.ReadSet.boot().gate2Labels()).as("gate 2: both swaps and the three child validations, from their owners")
            .containsExactly(LeafIndexPass.swapLabel("chunks"), LeafIndexPass.swapLabel("taxonomy_centroids"),
                Rdr229SwapMove.validationLabel("catalog_document_chunks"), Rdr229SwapMove.validationLabel("chunk_orphaned_at"),
                Rdr229SwapMove.validationLabel("topic_assignments"));
        assertThat(CompletionCheck.ReadSet.boot()).as("it is not P3.3a's set").isNotEqualTo(CompletionCheck.ReadSet.p33a());
        assertThat(CompletionCheck.ReadSet.boot()).as("it is not P3.1's set").isNotEqualTo(CompletionCheck.ReadSet.p31());
    }

    @Test
    void theReporterAnEngineStartsReadsTheBootReadSet() {
        var work = CompletionBootWork.atBoot(new BootWorkRegistry(java.time.Clock.systemUTC()), new AdminConnection("jdbc:postgresql://localhost:1/none", "u", "p"));
        assertThat(work.readSet()).as("the database-backed reporter's read set").isEqualTo(CompletionCheck.ReadSet.boot());
    }

    @Test
    void mainStartsTheReporterThroughTheFactory_andNamesNoReadSetOfItsOwn() throws IOException {
        String main = Files.readString(MAIN);
        assertThat(main).as("Main starts the completion reporter through the one factory").contains("CompletionBootWork.atBoot(");
        assertThat(main).as("and names no read set itself").doesNotContain("ReadSet.p31(").doesNotContain("ReadSet.p33a(").doesNotContain("ReadSet.p33c(")
            .doesNotContain("new dev.nexus.service.datamove.CompletionBootWork(");
    }
}
