// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import org.jooq.DSLContext;

import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17): the label of each table's stamp move, one scheme for the registering changesets and the
 * completion check. A label cannot contain {@code /} ({@code data_moves_label_check}); the worker's runs add
 * {@code /<tenant>/<leaf>} to it.
 */
final class StampLabels {

    /** The label of table {@code T}'s stamp move is this prefix and {@code T}. */
    static final String PREFIX = "rdr229-stamp-";

    private StampLabels() { /* static */ }

    static String labelFor(String table) {
        return PREFIX + table;
    }

    /** The table a stamp label stamps: one of the tables of {@code p31-checks.tsv} that carries a CHECK. */
    static Optional<String> tableOf(String label) {
        if (!label.startsWith(PREFIX)) {
            return Optional.empty();
        }
        String table = label.substring(PREFIX.length());
        return P31Checks.checksByTable().containsKey(table) ? Optional.of(table) : Optional.empty();
    }

    /** Every stamp label, in the list's order: one per table with a CHECK. */
    static List<String> all() {
        return P31Checks.checksByTable().keySet().stream().map(StampLabels::labelFor).toList();
    }

    /** A label that applies only while its table and column exist. */
    record Applicability(String table, String column) {
        boolean holds(DSLContext ctx, Set<String> tablesPresent) {
            return tablesPresent.contains(table) && CompletionCheck.columnExists(ctx, table, column);
        }
    }

    /**
     * The explicit allowlist of residual R3 (nexus-p2m1x.42): the labels that count as <i>not applicable</i>, rather than
     * incomplete, while they have no row or a failed one and their table or its {@code collection_id} column is absent.
     * {@code aspect_extraction_queue} is absent on most installs (aspects-005 drops it) and its column is missing on an
     * install whose kept table another role owns (vectors-068-18 is skipped, unrecorded). P3.3a adds its queue validation
     * label here when it ships; every other label with no row stays incomplete.
     */
    static final Map<String, Applicability> NOT_APPLICABLE_WHEN_ABSENT = Map.of(
        labelFor("aspect_extraction_queue"), new Applicability("aspect_extraction_queue", "collection_id"));
}
