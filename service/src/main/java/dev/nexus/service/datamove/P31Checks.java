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
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17): the one list of the Expand's id columns and CHECK constraints,
 * {@code db/rdr229/p31-checks.tsv}. The changesets, the R5 operator down-step and the boot-time completion check are all
 * built from it, so a table added to it is added everywhere.
 */
final class P31Checks {

    /** The classpath location of the list. */
    static final String RESOURCE = "db/rdr229/p31-checks.tsv";

    private P31Checks() { /* static */ }

    /**
     * One row of the list. {@code nameColumn} is {@code -} for the NOT NULL guard; {@code constraint} is {@code -} for a
     * history table (a bare nullable id, no CHECK, no stamp).
     */
    record Row(String table, String nameColumn, String idColumn, String constraint, String kind) {
        boolean hasCheck() {
            return !"-".equals(constraint);
        }
    }

    /** Every row of the list, in file order. */
    static List<Row> load() {
        List<Row> rows = new ArrayList<>();
        try (InputStream in = P31Checks.class.getClassLoader().getResourceAsStream(RESOURCE)) {
            if (in == null) {
                throw new IllegalStateException(RESOURCE + " is not on the classpath");
            }
            BufferedReader reader = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8));
            for (String line = reader.readLine(); line != null; line = reader.readLine()) {
                if (line.isBlank() || line.startsWith("#") || line.startsWith("table\t")) {
                    continue;
                }
                String[] f = line.split("\t", -1);
                if (f.length != 5) {
                    throw new IllegalStateException(RESOURCE + ": a row has " + f.length + " columns, not five: " + line);
                }
                rows.add(new Row(f[0], f[1], f[2], f[3], f[4]));
            }
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
        if (rows.isEmpty()) {
            throw new IllegalStateException(RESOURCE + " lists no row");
        }
        return List.copyOf(rows);
    }

    /** The rows that carry a CHECK, grouped by table in file order: what one table's stamp move covers. */
    static Map<String, List<Row>> checksByTable() {
        Map<String, List<Row>> out = new LinkedHashMap<>();
        for (Row r : load()) {
            if (r.hasCheck()) {
                out.computeIfAbsent(r.table(), t -> new ArrayList<>()).add(r);
            }
        }
        return out;
    }
}
