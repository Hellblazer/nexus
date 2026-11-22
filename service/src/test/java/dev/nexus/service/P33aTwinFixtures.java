// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.DSLContext;
import org.jooq.impl.DSL;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;

/**
 * RDR-229 P3.3a (nexus-p2m1x.19): a fixture that simulates the name key's gap window (the collection foreign key absent, an orphan row seeded
 * with an id no registry row carries) must drop the id twin beside it, because the twin is NOT VALID but enforced on every new row. The twin names
 * are read from the one list the changesets are tested against, {@code db/rdr229/p33a-fk-twins.tsv}, so no name is retyped here.
 */
public final class P33aTwinFixtures {

    private static final String LIST = "db/rdr229/p33a-fk-twins.tsv";

    private P33aTwinFixtures() { }

    /** Drops the id twin of {@code table}'s registry foreign key, if the table has one and it is there. */
    public static void dropTwin(DSLContext ctx, String table) {
        String twin = twinOf(table);
        if (twin != null) {
            ctx.alterTable(DSL.table(DSL.name("nexus", table))).dropConstraintIfExists(DSL.name(twin)).execute();
        }
    }

    static String twinOf(String table) {
        try (InputStream in = P33aTwinFixtures.class.getClassLoader().getResourceAsStream(LIST)) {
            if (in == null) {
                throw new IllegalStateException(LIST + " is not on the classpath");
            }
            BufferedReader reader = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8));
            for (String line = reader.readLine(); line != null; line = reader.readLine()) {
                if (line.isBlank() || line.startsWith("#") || line.startsWith("table\t")) {
                    continue;
                }
                String[] f = line.split("\t", -1);
                if (f[0].equals(table)) {
                    return f[3];
                }
            }
            return null;
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
    }
}
