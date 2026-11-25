// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

/**
 * vectors-079's shipped changeset as the tests that install a mutant over its functions see it. The mutant is the shipped text with one
 * piece substituted ({@code PgContainerHelper#replayChangesetSubstituting}: it throws when the piece is no longer in the text, which is
 * the non-vacuity check), and the shipped text is replayed back afterwards.
 */
final class Vectors079Text {

    static final String CHANGESET = "db/changelog/vectors-079-child-row-name-from-chunk.xml";
    static final String CHANGESET_ID = "vectors-079-1";

    private Vectors079Text() { /* static */ }

    /** Replays the changeset with {@code from} replaced by {@code to}, runs {@code work}, and replays the shipped changeset back. */
    static void withMutant(Rdr229RenameEngine engine, String from, String to, Runnable work) {
        withSubstitutions(engine, new java.util.LinkedHashMap<>(java.util.Map.of(from, to)), work);
    }

    /** {@link #withMutant} with several substitutions, applied in map order (an instrumentation and a mutant together). */
    static void withSubstitutions(Rdr229RenameEngine engine, java.util.LinkedHashMap<String, String> substitutions, Runnable work) {
        engine.su(ctx -> ctx.connectionResult(c -> {
            try {
                PgContainerHelper.replayChangesetSubstituting(c, CHANGESET, CHANGESET_ID, substitutions);
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
            return null;
        }));
        try {
            work.run();
        } finally {
            engine.su(ctx -> ctx.connectionResult(c -> {
                try {
                    PgContainerHelper.replayChangeset(c, CHANGESET, CHANGESET_ID);
                } catch (Exception e) {
                    throw new IllegalStateException(e);
                }
                return null;
            }));
        }
    }
}
