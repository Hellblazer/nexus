// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

/**
 * RDR-229 P3.3a (nexus-p2m1x.19): the one list of the seven id-twin foreign keys and their validation moves,
 * {@code db/rdr229/p33a-fk-twins.tsv}. The changesets' tests, the boot-time completion check and its read set are built from
 * it, so a twin added to it is added everywhere. The label of a twin's move is {@code rdr229-fkvalidate-<table>}, which cannot
 * contain {@code /} ({@code data_moves_label_check}); the worker's runs add {@code /<tenant>/<leaf>} to it.
 */
final class FkTwins {

    /** The classpath location of the list. */
    static final String RESOURCE = "db/rdr229/p33a-fk-twins.tsv";

    /** The label of table {@code T}'s validation move is this prefix and {@code T}. */
    static final String PREFIX = "rdr229-fkvalidate-";

    private FkTwins() { /* static */ }

    /** One twin: the table it is on, its columns and what it references (each comma separated), its name, the name key's ON DELETE action it copies, its move's label and the name key it sits beside. */
    record Row(String table, String fkColumns, String referencedColumns, String twin, String onDelete, String label,
               String nameKey) { }

    /** The list, parsed once: the completion check reads it on every sample. P31Checks re-reads its file each call; this one does not. */
    private static final class Holder {
        static final List<Row> ROWS = parse();
    }

    /** Every twin of the list, in file order (parsed once, then served from memory). */
    static List<Row> load() {
        return Holder.ROWS;
    }

    private static List<Row> parse() {
        List<Row> rows = new ArrayList<>();
        try (InputStream in = FkTwins.class.getClassLoader().getResourceAsStream(RESOURCE)) {
            if (in == null) {
                throw new IllegalStateException(RESOURCE + " is not on the classpath");
            }
            BufferedReader reader = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8));
            for (String line = reader.readLine(); line != null; line = reader.readLine()) {
                if (line.isBlank() || line.startsWith("#") || line.startsWith("table\t")) {
                    continue;
                }
                String[] f = line.split("\t", -1);
                if (f.length != 7) {
                    throw new IllegalStateException(RESOURCE + ": a row has " + f.length + " columns, not seven: " + line);
                }
                Row row = new Row(f[0], f[1], f[2], f[3], f[4], f[5], f[6]);
                if (!row.label().equals(PREFIX + row.table())) {
                    throw new IllegalStateException(RESOURCE + ": the label of " + row.table() + " is not " + PREFIX + row.table());
                }
                rows.add(row);
            }
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
        if (rows.isEmpty()) {
            throw new IllegalStateException(RESOURCE + " lists no row");
        }
        return List.copyOf(rows);
    }

    /** The twin whose validation move has {@code label}. */
    static Optional<Row> byLabel(String label) {
        return load().stream().filter(r -> r.label().equals(label)).findFirst();
    }

    /** Every validation label, in the list's order: one per twin. */
    static List<String> labels() {
        return load().stream().map(Row::label).toList();
    }
}
