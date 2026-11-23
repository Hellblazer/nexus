// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

import org.jooq.DSLContext;
import org.jooq.impl.DSL;

/**
 * RDR-229 P3.5c tests: a one-row rename of the registry, what P3.7's rename does. The foreign keys onto the registry's name (what
 * P3.7 drops) go first, so {@code UPDATE ... SET name} is possible; every other table keeps the old name and the collection id
 * stays, so the name and the id now disagree about the collection.
 */
public final class RegistryRenameSupport {

    private RegistryRenameSupport() {
    }

    /** Drops every foreign key onto {@code nexus.catalog_collections} (typed, from pg_constraint). Idempotent. */
    public static void dropForeignKeysOntoTheRegistry(DSLContext dsl) {
        var con = DSL.table(DSL.name("pg_catalog", "pg_constraint"));
        var cls = DSL.table(DSL.name("pg_catalog", "pg_class"));
        var nsp = DSL.table(DSL.name("pg_catalog", "pg_namespace"));
        var target = DSL.function("to_regclass", Object.class, DSL.val("nexus.catalog_collections"));
        for (var fk : dsl
                .select(DSL.field(DSL.name("pg_namespace", "nspname"), String.class),
                        DSL.field(DSL.name("pg_class", "relname"), String.class),
                        DSL.field(DSL.name("pg_constraint", "conname"), String.class))
                .from(con)
                .join(cls).on(DSL.field(DSL.name("pg_class", "oid")).eq(DSL.field(DSL.name("pg_constraint", "conrelid"))))
                .join(nsp).on(DSL.field(DSL.name("pg_namespace", "oid")).eq(DSL.field(DSL.name("pg_class", "relnamespace"))))
                .where(DSL.field(DSL.name("pg_constraint", "contype"), String.class).eq("f"))
                .and(DSL.field(DSL.name("pg_constraint", "confrelid")).eq(target))
                .and(DSL.field(DSL.name("pg_constraint", "conparentid")).eq(0L))
                .fetch()) {
            dsl.alterTable(DSL.table(DSL.name(fk.value1(), fk.value2()))).dropConstraint(DSL.name(fk.value3())).execute();
        }
    }

    /** Renames the registry row {@code from} to {@code to} in place (the id stays); the foreign keys must already be dropped. */
    public static void rename(DSLContext dsl, String tenant, String from, String to) {
        int n = dsl.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, to)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(from)).execute();
        assertThat(n).as("the registry row of " + from + " is renamed").isEqualTo(1);
    }
}
