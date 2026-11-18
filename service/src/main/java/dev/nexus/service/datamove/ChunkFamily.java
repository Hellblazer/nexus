// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import java.util.List;
import java.util.Locale;
import java.util.regex.Pattern;

/**
 * Whether a move is over the chunk family (RDR-246, Finishing a move): {@code nexus.chunks}, its leaves
 * ({@code chunks_m...}), the partition helpers ({@code partition_...}), the index builder ({@code pci_...}) and every table
 * with a foreign key into {@code nexus.chunks}. A DROP or ALTER on one takes ACCESS EXCLUSIVE on every chunk leaf, where it
 * queues behind a {@code CREATE INDEX CONCURRENTLY}, so a move that finishes there ends the index builders first. A move
 * counts as chunk-family when its driving table is in the family (schema {@code nexus}) or {@code on_complete} names a
 * table or routine that is.
 *
 * <p>The family is the one {@code tests/test_changelog_continue_precondition_chunks_lint.py} keeps (its {@code _FK_TABLES}
 * and {@code _FORBIDDEN} patterns); {@code ChunkFamilyLintPinTest} reads that file and fails when this list or these patterns
 * differ from it. The names are matched in the {@code on_complete} text, normalised the way that lint does (quotes dropped,
 * whitespace around dots collapsed, lowercased), with or without a schema. A name in a comment or a literal counts too: the
 * cost of a false positive is one ended build, which the pass restarts; the cost of a miss is a queue behind it, which
 * {@code lock_timeout} turns into a retry.
 */
final class ChunkFamily {

    private ChunkFamily() { /* static */ }

    /** The tables with a foreign key into {@code nexus.chunks}. */
    static final List<String> FOREIGN_KEY_TABLES =
        List.of("catalog_document_chunks", "topic_assignments", "chunk_orphaned_at");

    /**
     * The lint's name patterns, as regular-expression source and in its order: {@code nexus.chunks}, a chunk leaf, a partition
     * helper, the index builder.
     */
    static final List<String> NAME_PATTERNS = List.of("\\bchunks\\b", "\\bchunks_m", "\\bpartition_", "\\bpci_");

    /** The lint's foreign-key entry: {@code \b(?:} the tables joined with {@code |} {@code )\b}. */
    static final String FOREIGN_KEY_PATTERN = "\\b(?:" + String.join("|", FOREIGN_KEY_TABLES) + ")\\b";

    private static final Pattern NAMES = Pattern.compile(String.join("|", NAME_PATTERNS) + "|" + FOREIGN_KEY_PATTERN);

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
