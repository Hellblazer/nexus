// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.Table;

import java.sql.Connection;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17): the statement the stamp-move tests need that jOOQ has no form for, run through a test changelog (no SQL text
 * in Java) and usable in any container, the data-move and empty-database ones included.
 */
public final class P31TestSql {

    private P31TestSql() { /* static */ }

    /** {@code ALTER TABLE table VALIDATE CONSTRAINT constraint} on a superuser connection. */
    public static void validate(Connection su, Table<?> table, String constraint) throws Exception {
        PgContainerHelper.validateConstraintStandalone(su, table, constraint);
    }
}
