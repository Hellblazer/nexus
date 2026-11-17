// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import java.util.List;
import java.util.Locale;
import java.util.regex.Pattern;

/**
 * Whether a move is over the chunk family (RDR-246, Finishing a move): {@code nexus.chunks}, its leaves
 * ({@code chunks_m...}) and every table with a foreign key into it. A DROP or ALTER on one takes ACCESS EXCLUSIVE on every
 * chunk leaf, where it queues behind a {@code CREATE INDEX CONCURRENTLY}, so a move that finishes there ends the index
 * builders first. A move counts as chunk-family when its driving table is in the family (schema {@code nexus}) or
 * {@code on_complete} names a table that is.
 *
 * <p>The family is the one {@code tests/test_changelog_continue_precondition_chunks_lint.py} keeps ({@code _FK_TABLES} and
 * its derivation test). The names are matched in the {@code on_complete} text, normalised the way that lint does (quotes
 * dropped, whitespace around dots collapsed, lowercased), with or without a schema. A name in a comment or a literal counts
 * too: the cost of a false positive is one ended build, which the pass restarts; the cost of a miss is a queue behind it.
 */
final class ChunkFamily {

    private ChunkFamily() { /* static */ }

    /** The tables with a foreign key into {@code nexus.chunks}. */
    static final List<String> FOREIGN_KEY_TABLES =
        List.of("catalog_document_chunks", "topic_assignments", "chunk_orphaned_at");

    private static final Pattern NAMES = Pattern.compile(
        "\\bchunks\\b|\\bchunks_m|\\b(?:" + String.join("|", FOREIGN_KEY_TABLES) + ")\\b");

    /** True when the move's driving table is in the family, or its {@code on_complete} names a table that is. */
    static boolean touches(DataMove move) {
        if ("nexus".equals(move.drivingSchema()) && NAMES.matcher(move.drivingTable().toLowerCase(Locale.ROOT)).find()) {
            return true;
        }
        String onComplete = move.onComplete();
        return onComplete != null && NAMES.matcher(normalise(onComplete)).find();
    }

    static String normalise(String text) {
        return text.replace("\"", "").toLowerCase(Locale.ROOT).replaceAll("\\s*\\.\\s*", ".");
    }
}
