// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/**
 * RDR-229 P3.5b (nexus-p2m1x.24): the changesets a fresh walk leaves unrecorded on purpose. The CONTINUE-gated changesets of
 * {@code vectors-078-rdr229-p35b-readers-continue.xml} wait for every {@code rdr229-stamp-*} move, which a fresh install has not
 * completed, so they skip on every fresh walk; {@code collections-001-2} (P3.8) waits for its own data move the same way. The
 * vectors-078 ids are read from the shipped list ({@value #LIST}, kept equal to the file by
 * {@code tests/test_rdr229_p35b_continue_list.py}), never typed here, so a test that counts the walk's skips follows the list.
 */
final class P35bContinueChangesets {

    static final String LIST = "db/rdr229/p35b-continue-changesets.txt";
    /** P3.8's self-retrying changeset: waits for its data move on a fresh install. */
    static final String COLLECTIONS_WAIT = "collections-001-2";

    private P35bContinueChangesets() {}

    /** The ids in {@value #LIST}, in file order; blank and {@code #} lines skipped. A missing or empty list is an error. */
    static List<String> ids() {
        try (InputStream in = P35bContinueChangesets.class.getClassLoader().getResourceAsStream(LIST)) {
            if (in == null) {
                throw new IllegalStateException("classpath resource missing: " + LIST);
            }
            List<String> ids = new String(in.readAllBytes(), StandardCharsets.UTF_8).lines()
                .map(String::strip)
                .filter(l -> !l.isEmpty() && !l.startsWith("#"))
                .toList();
            if (ids.isEmpty()) {
                throw new IllegalStateException(LIST + " lists no changeset");
            }
            return ids;
        } catch (IOException e) {
            throw new IllegalStateException("cannot read " + LIST, e);
        }
    }

    /** Every changeset a fresh walk skips with onFail="CONTINUE": {@value #COLLECTIONS_WAIT} and the P3.5b list. */
    static Set<String> freshWalkSkips() {
        Set<String> skips = new LinkedHashSet<>();
        skips.add(COLLECTIONS_WAIT);
        skips.addAll(ids());
        return skips;
    }
}
