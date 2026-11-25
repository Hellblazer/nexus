// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * RDR-229 P3.1 stage 3 (nexus-p2m1x.17): what each per-table stamp move is registered with, written down once for the tests.
 *
 * <p>The values are P3.2's (T2 nexus_rdr/229-p32-fork-results-2026-11-20 (2/2) item 5, ratified by Sam's ask C ruling,
 * nexus/sam-ruling-rdr229-ask-c-2026-11-20; bead nexus-p2m1x.17, "Registering-INSERT values"). The constraints each move
 * validates come from the one list {@code db/rdr229/p31-checks.tsv}, the same list R5's down-step is generated from, so a
 * move and its CHECK cannot drift apart.
 *
 * <p>{@link #warmVerifyMs} is the measured cost of one table-wide verification on the P3.2 fork (production's row counts,
 * T2 nexus_rdr/229-p32-fork-results-2026-11-20 (1/2) section 1, "stamp verifications"): chunks 2,780 + 580 ms for the two
 * tenants, the manifest 590 ms for nexus (710 ms whole, apportioned by row share), catalog_documents 390 ms, every other
 * table under 120 ms. {@link #recheckBudgetMs} is what the completion transaction's re-check needs of the one shared
 * {@code statement_timeout_ms}: the table-wide run and the sum of the tenants' warm runs (which read the same rows split
 * by tenant, so about one more table-wide run) and a fixed cost per run.
 */
final class StampMoveSpecs {

    private StampMoveSpecs() {}

    static final String LABEL_PREFIX = "rdr229-stamp-";
    static final String CHANGESET_PREFIX = "vectors-070-";
    static final String CHANGELOG = "db/changelog/vectors-070-collection-id-stamp-moves.xml";
    static final Path CHANGELOG_FILE = Path.of("src", "main", "resources", CHANGELOG);
    static final Path CHECKS = Path.of("src", "main", "resources", "db", "rdr229", "p31-checks.tsv");

    /** Tenants the fork's registry listed: 7 (56 leaves = 7 tenants x 4 models x 2 trees). The budget assumes this many runs. */
    static final int TENANTS = 7;
    /** What one more run costs beyond its scan: the SET of the tenant, the plan, the connection round trip. A stated assumption, not a measurement. */
    static final int RUN_OVERHEAD_MS = 50;
    /** Sam's ask C ruling: 60 s on every SUE-only move (the stamps and every VALIDATE), ten times the longest measured statement. */
    static final int STATEMENT_TIMEOUT_MS = 60_000;

    /** One move: its table, the P3.2 throttle, the cost of a warm verification and the order it is registered in. */
    record Spec(String table, int chunkSize, int sleepMs, int warmVerifyMs) {
        String label() {
            return LABEL_PREFIX + table;
        }

        /** {@code 2 x warm verification + TENANTS x run overhead}: the table-wide run, the tenants' runs (one more table's worth) and each run's fixed cost. */
        int recheckBudgetMs() {
            return 2 * warmVerifyMs + TENANTS * RUN_OVERHEAD_MS;
        }

        /** The ids this move fills and the constraints it validates, from the list. */
        List<Check> checks() {
            return StampMoveSpecs.checks().stream().filter(c -> c.table().equals(table)).toList();
        }
    }

    /** The order the moves are registered in, which is the order the worker takes them (oldest first): the small registry first, the 38-hour chunks stamp last. */
    static final List<Spec> SPECS = List.of(
        new Spec("catalog_collections", 1000, 4000, 120),
        new Spec("catalog_documents", 1000, 4000, 390),
        new Spec("chash_remap", 1000, 4000, 120),
        new Spec("pdf_pipeline", 1000, 4000, 120),
        new Spec("reembed_jobs", 1000, 4000, 120),
        new Spec("canary_query_ring", 1000, 4000, 120),
        new Spec("document_aspects", 1000, 4000, 120),
        new Spec("document_highlights", 1000, 4000, 120),
        new Spec("taxonomy_meta", 1000, 4000, 120),
        new Spec("topics", 1000, 4000, 120),
        new Spec("chunk_orphaned_at", 1000, 4000, 120),
        new Spec("taxonomy_centroids", 100, 2000, 120),
        new Spec("catalog_document_chunks", 1000, 4000, 710),
        new Spec("topic_assignments", 1000, 4000, 120),
        new Spec("chunks", 100, 20_000, 3360));

    /**
     * The retired queue's move (residual R3): registered by vectors-070-30 only where the table is kept with its id column, so a fresh walk
     * registers none and {@link #SPECS} (what the walk registers) leaves it out. It sits before chunks in the file, so the small moves finish first.
     */
    static final Spec QUEUE = new Spec("aspect_extraction_queue", 1000, 4000, 120);

    /** {@link #SPECS} and the conditional queue move, in the order the changeset file registers them. */
    static final List<Spec> WITH_QUEUE = withQueueBeforeChunks();

    private static List<Spec> withQueueBeforeChunks() {
        List<Spec> out = new ArrayList<>(SPECS.subList(0, SPECS.size() - 1));
        out.add(QUEUE);
        out.add(SPECS.get(SPECS.size() - 1));
        return List.copyOf(out);
    }

    /** A row of p31-checks.tsv: {@code constraint} is {@code -} for a history table. */
    record Check(String table, String nameColumn, String idColumn, String constraint, String kind) {
        boolean validated() {
            return !constraint.equals("-");
        }
    }

    static List<Check> checks() {
        try {
            List<Check> rows = new ArrayList<>();
            for (String line : Files.readAllLines(CHECKS)) {
                if (line.isBlank() || line.startsWith("#") || line.startsWith("table\t")) continue;
                String[] f = line.split("\t");
                assertThat(f).as("a row of the list has five columns: %s", line).hasSize(5);
                rows.add(new Check(f[0], f[1], f[2], f[3], f[4]));
            }
            return rows;
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
    }

    /** The constraints of {@code table} the move validates, by table (the queue's included: its move is conditional, not absent), the history tables have none. */
    static Map<String, List<String>> constraintsByTable() {
        Map<String, List<String>> out = new LinkedHashMap<>();
        for (Check c : checks()) {
            if (c.validated()) {
                out.computeIfAbsent(c.table(), k -> new ArrayList<>()).add(c.constraint());
            }
        }
        return out;
    }

    /** The id columns the move over {@code table} sets: every NAME_ID and SUPERSEDED row's id column. */
    static List<String> idColumns(String table) {
        return checks().stream().filter(c -> c.table().equals(table) && c.validated() && !c.kind().equals("NOT_NULL"))
            .map(Check::idColumn).toList();
    }

    /** The name columns the move over {@code table} reads, in the same order as {@link #idColumns}. */
    static List<String> nameColumns(String table) {
        return checks().stream().filter(c -> c.table().equals(table) && c.validated() && !c.kind().equals("NOT_NULL"))
            .map(Check::nameColumn).toList();
    }
}
