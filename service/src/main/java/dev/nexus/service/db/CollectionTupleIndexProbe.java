// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import javax.sql.DataSource;
import java.util.function.Supplier;

/** STUB (test-first): replaced by the implementation commit. */
public final class CollectionTupleIndexProbe {
    private CollectionTupleIndexProbe() { }

    public static Boolean present(DataSource ds) {
        return null;
    }

    public static Supplier<Boolean> statusSupplier(DataSource ds) {
        return () -> null;
    }
}
