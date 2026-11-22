// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.Set;
import java.util.TreeSet;
import java.util.function.Supplier;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27): the P3.5b CONTINUE changesets whose application the one-row rename waits for (RDR item 6, "Readers
 * during the stamp"). The list is the resource {@value #RESOURCE}, one changeset id per line, {@code #} comments and blank lines
 * skipped; P3.5b's code writes it. {@link CompletionCheck} reads {@code databasechangelog} for every id in it.
 *
 * <p><b>A missing or empty list is refused</b>: it reads as the empty set, and the check counts an empty list as "not applied", so a
 * build that ships without its list never lets a rename through. Tests supply their own list through the
 * {@link Supplier} seam of {@link CompletionBootWork}.
 */
public final class P35bChangesets {

    /** The classpath resource P3.5b's code commit writes. */
    public static final String RESOURCE = "db/rdr229/p35b-continue-changesets.txt";

    private P35bChangesets() { /* static */ }

    /** The ids in {@link #RESOURCE}, or the empty set when the resource is absent or lists nothing. */
    public static Set<String> fromResource() {
        try (InputStream in = P35bChangesets.class.getClassLoader().getResourceAsStream(RESOURCE)) {
            return in == null ? Set.of() : parse(in);
        } catch (IOException e) {
            return Set.of();   // an unreadable list is a missing one: refused
        }
    }

    /** One id per line; {@code #} comments and blank lines skipped. */
    static Set<String> parse(InputStream in) throws IOException {
        Set<String> ids = new TreeSet<>();
        try (var r = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8))) {
            String line;
            while ((line = r.readLine()) != null) {
                String t = line.strip();
                if (!t.isEmpty() && !t.startsWith("#")) {
                    ids.add(t);
                }
            }
        }
        return Set.copyOf(ids);
    }

    /** {@link #fromResource} as the seam type {@link CompletionBootWork} takes. */
    public static Supplier<Set<String>> resource() {
        return P35bChangesets::fromResource;
    }
}
