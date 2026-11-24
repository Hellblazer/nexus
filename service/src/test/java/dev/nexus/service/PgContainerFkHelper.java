// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.sql.Connection;
import java.util.List;
import java.util.stream.Stream;

/**
 * The foreign-key wrapper of the per-leaf key (nexus-p2m1x.21), held apart from {@link PgContainerHelper} so that file stays under its
 * size ceiling. Its identifiers go through {@link PgContainerHelper#requireTestIdentifier}, as every validated helper's do.
 */
public final class PgContainerFkHelper {
    private PgContainerFkHelper() { }

    /** The delete actions of a foreign key, for {@link #addFkNotValidColumns}; the constant's name with underscores as spaces is the keyword form. */
    public enum DeleteAction { NO_ACTION, RESTRICT, CASCADE, SET_NULL, SET_DEFAULT }

    /**
     * {@code ALTER TABLE table ADD CONSTRAINT name FOREIGN KEY (columns) REFERENCES refTable (refColumns) ON DELETE action [DEFERRABLE] NOT VALID}
     * (nexus-p2m1x.21) through {@code nexus_test.add_fk_not_valid_columns}: jOOQ renders NOT ENFORCED, which PostgreSQL 17 refuses, and the
     * other wrappers fix the column count or add {@code embedding_model} themselves. Every column and the constraint name goes through
     * {@link PgContainerHelper#requireTestIdentifier} and the lists must be the same length, in key order; the action is the enum. NOT VALID: the
     * caller runs {@link PgContainerHelper#validateConstraint} for a validated key. The ALTER runs in the connection's open transaction as that
     * connection's role.
     */
    public static void addFkNotValidColumns(Connection conn, Table<?> table, String constraintName, List<String> columns,
                                            Table<?> refTable, List<String> refColumns, DeleteAction onDelete, boolean deferrable) {
        if (columns.isEmpty() || columns.size() != refColumns.size()) {
            throw new IllegalArgumentException("a key needs as many referenced columns as columns: " + columns + " -> " + refColumns);
        }
        PgContainerHelper.requireTestIdentifier(constraintName);
        Stream.concat(columns.stream(), refColumns.stream()).forEach(PgContainerHelper::requireTestIdentifier);
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        ctx.select(DSL.function(DSL.name("nexus_test", "add_fk_not_valid_columns"), SQLDataType.BOOLEAN, DSL.val(ctx.render(table)),
                DSL.val(constraintName), DSL.val(String.join(",", columns)), DSL.val(ctx.render(refTable)), DSL.val(String.join(",", refColumns)),
                DSL.val(onDelete.name().replace('_', ' ')), DSL.val(deferrable)))
            .fetch();
    }
}
