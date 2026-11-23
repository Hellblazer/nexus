// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23): the registry refusals a write answers as typed 404, 409 or 500, as one list. A writer that catches
 * {@code RuntimeException} (a fail-safe, a per-document failure list) rethrows these first: swallowing one turns a write that cannot
 * land under its collection into a 200.
 */
public final class CollectionIdRefusals {

    private CollectionIdRefusals() {
    }

    /** True for the registry's typed refusals of a write: an id no row has, a retired id or name, an id and a client label that no longer agree, a row of another id. */
    public static boolean isRefusal(Throwable e) {
        return e instanceof UnknownCollectionIdException || e instanceof CollectionRetiredIdException
            || e instanceof CollectionIdMismatchException || e instanceof CollectionSupersededException;
    }

    /** Rethrows {@code e} when it is one of the registry's typed refusals; returns for anything else. */
    public static void rethrowIfRefusal(RuntimeException e) {
        if (isRefusal(e)) {
            throw e;
        }
    }
}
