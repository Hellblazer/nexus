// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.function.Function;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23, linda's Q1/Q2 rulings and the F2 finding): a write by id follows a one-row rename, writing under the
 * locked registry row's CURRENT label, and the lock order is advisory locks (the sweep gate) first, then the registry row. The gate is
 * keyed by LABEL, so the gate of the label the row has NOW can only be known after the row is read, which is after the gate of the
 * label the route named. Taking the second gate then inverts the order (a verb that holds the new label's gate waits for the row this
 * writer holds).
 *
 * <p>So the writer does not take it there. {@link #run} runs the whole transaction as an attempt that takes the gate of a label it is
 * GIVEN, first; after the registry read the attempt calls {@link #check}: if the row's current label is another, the attempt throws
 * {@link Moved} (the transaction rolls back and releases everything), and {@code run} runs it again from the top with that label's gate
 * first. The row read in the new attempt is the same id, so a second rename in the gap is followed the same way, a bounded number of
 * times; a collection renamed more often than that while the write waits is {@link RenameBusyException}, which the routes answer as
 * a retryable busy.
 *
 * <p>The label the attempt is given is a gate label only: the registry read still goes by the label the request named, which is how a
 * request by id is bound to its id ({@link ExpectedCollectionIds}).
 */
public final class RenameFollow {

    private static final int MAX_FOLLOWS = 5;

    private RenameFollow() {
    }

    /** The row's current label is not the label this attempt took its gate for: run again with this one. Carries no stack trace. */
    public static final class Moved extends RuntimeException {
        private final String label;

        Moved(String label) {
            super("the collection is now named " + label, null, false, false);
            this.label = label;
        }

        public String label() {
            return label;
        }
    }

    /** True for {@link Moved}: a writer that catches {@code RuntimeException} rethrows it, or the attempt would be reported as a failure. */
    public static boolean isMoved(Throwable e) {
        return e instanceof Moved;
    }

    /**
     * Runs {@code attempt} with {@code boundaryLabel} as the label to take the gate of, and again with the row's current label each time it
     * throws {@link Moved}.
     */
    public static <T> T run(String boundaryLabel, Function<String, T> attempt) {
        String gate = boundaryLabel;
        for (int follows = 0; ; follows++) {
            try {
                return attempt.apply(gate);
            } catch (Moved moved) {
                if (follows >= MAX_FOLLOWS) {
                    throw RenameBusyException.renamedRepeatedly(follows + 1);
                }
                gate = moved.label();
            }
        }
    }

    /** Called after the registry read: throws {@link Moved} when the locked row's current label is not the label whose gate this attempt holds. */
    public static void check(String gateLabel, CollectionRow locked) {
        if (!gateLabel.equals(locked.label())) {
            throw new Moved(locked.label());
        }
    }
}
