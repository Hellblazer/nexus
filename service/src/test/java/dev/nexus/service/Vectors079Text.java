// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.regex.Pattern;

/** The text of vectors-079's row functions as the changeset defines them, for the tests that install a mutant over one and restore it. */
final class Vectors079Text {

    static final String CHANGESET = "db/changelog/vectors-079-child-row-name-from-chunk.xml";
    static final String MANIFEST = "catalog_document_chunks_name_from_chunk";
    static final String ORPHANED = "chunk_orphaned_at_name_from_chunk";
    static final String ASSIGNMENTS = "topic_assignments_name_from_chunk";
    private static final Pattern TOMBSTONE_GUARD = Pattern.compile(
        "    IF EXISTS \\(SELECT 1 FROM nexus\\.catalog_collections r\\n.*?\\n    END IF;\\n", Pattern.DOTALL);

    private Vectors079Text() { /* static */ }

    /** The CREATE OR REPLACE FUNCTION statement of {@code name}. */
    static String function(String name) throws Exception {
        try (InputStream in = Vectors079Text.class.getClassLoader().getResourceAsStream(CHANGESET)) {
            String xml = new String(in.readAllBytes(), StandardCharsets.UTF_8);
            int a = xml.indexOf("CREATE OR REPLACE FUNCTION nexus." + name + "()");
            if (a < 0) {
                throw new AssertionError(name + " is not in " + CHANGESET);
            }
            return xml.substring(a, xml.indexOf("$function$;", xml.indexOf("AS $function$", a)) + "$function$;".length());
        }
    }

    /** {@code sql} without the tombstone guard (the skip of step 2 for the cascade of a name-path rename). */
    static String withoutTombstoneGuard(String sql) {
        String out = TOMBSTONE_GUARD.matcher(sql).replaceFirst("");
        if (out.equals(sql)) {
            throw new AssertionError("non-vacuity: the guard was not found to remove");
        }
        return out;
    }
}
