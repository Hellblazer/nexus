// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.Table;

import java.sql.Connection;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17): the two statements the stamp-move tests need that jOOQ has no form for, run through test changelogs (no SQL text
 * in Java) and usable in any container, the data-move and empty-database ones included.
 */
public final class P31TestSql {

    /** The registry's owner-read policy, which D2's vectors-065 creates in the product changelog. */
    public static final String REGISTRY_OWNER_READ = "catalog_collections_owner_read";

    private P31TestSql() { /* static */ }

    /** {@code ALTER TABLE table VALIDATE CONSTRAINT constraint} on a superuser connection. */
    public static void validate(Connection su, Table<?> table, String constraint) throws Exception {
        PgContainerHelper.validateConstraintStandalone(su, table, constraint);
    }

    /**
     * Gives {@code role} a permissive SELECT policy {@value #REGISTRY_OWNER_READ} USING (true) on the registry: what D2's vectors-065 adds, and what
     * this tree does not carry until develop is merged in. Replaces one that is already there with the same definition, so the test still passes
     * after 065 arrives.
     */
    public static void ensureRegistryOwnerReadPolicy(Connection su, String role) throws Exception {
        PgContainerHelper.dropPolicyIfExists(su, CATALOG_COLLECTIONS, REGISTRY_OWNER_READ);
        PgContainerHelper.createPolicyUsingTrue(su, CATALOG_COLLECTIONS, REGISTRY_OWNER_READ,
            PgContainerHelper.PolicyKind.PERMISSIVE, "SELECT", role);
    }
}
