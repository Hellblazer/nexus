// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import org.jooq.Parameter;
import org.jooq.impl.AbstractRoutine;
import org.jooq.impl.DSL;
import org.jooq.impl.Internal;
import org.jooq.impl.SQLDataType;

/**
 * dml_utils' {@code run_migration_chunks_committing}, as a typed jOOQ routine, the way generated code models a stored
 * routine: each parameter is a {@link Parameter} with its data type and every value is a bind. All twelve parameters are
 * passed, in the procedure's own order, with the defaults pinned (alias {@code t}, chunking {@code primary_key}, poll
 * timeout 1 ms, no boundary key index, not partition-wise); the worker supplies the SQL text, the driving relation, the
 * run label, the chunk size, one thread and the throttle.
 *
 * <p>jOOQ renders a PostgreSQL routine as {@code select * from f(...)}, which PostgreSQL refuses for a procedure. Executed
 * under the {@link org.jooq.SQLDialect#DEFAULT} dialect the same routine is a JDBC callable statement, {@code { call
 * dml_utils.run_migration_chunks_committing (?, ...) }}, and the driver, set to {@code escapeSyntaxCallMode=callIfNoReturn}
 * on the call connection, sends it as {@code CALL}. That connection also sets {@code stringtype=unspecified}, so a text bind
 * reaches the procedure's domain and enum parameters ({@code non_null_text}, {@code chunking_strategy}) untyped and the
 * server resolves it. No SQL text is written here or in {@link CallConnection}.
 */
final class CommittingProcedure extends AbstractRoutine<Void> {

    private static final long serialVersionUID = 1L;

    private static final Parameter<String> SQL_TEXT =
        Internal.createParameter("i_sql_text", SQLDataType.VARCHAR, false, false);
    private static final Parameter<String> SCHEMA =
        Internal.createParameter("i_driving_table_schema_name", SQLDataType.VARCHAR, false, false);
    private static final Parameter<String> TABLE =
        Internal.createParameter("i_driving_table_name", SQLDataType.VARCHAR, false, false);
    private static final Parameter<String> LABEL =
        Internal.createParameter("i_label", SQLDataType.VARCHAR, false, false);
    private static final Parameter<Integer> CHUNK_SIZE =
        Internal.createParameter("i_chunk_size", SQLDataType.INTEGER, false, false);
    private static final Parameter<Integer> THREADS =
        Internal.createParameter("i_threads", SQLDataType.INTEGER, false, false);
    private static final Parameter<String> ALIAS =
        Internal.createParameter("i_driving_table_alias", SQLDataType.VARCHAR, false, false);
    private static final Parameter<String> CHUNK_BY =
        Internal.createParameter("i_chunk_by", SQLDataType.VARCHAR, false, false);
    private static final Parameter<Integer> POLL_TIMEOUT_MS =
        Internal.createParameter("i_poll_timeout_ms", SQLDataType.INTEGER, false, false);
    private static final Parameter<Integer> SLEEP_MS =
        Internal.createParameter("i_sleep_ms", SQLDataType.INTEGER, false, false);
    private static final Parameter<String> BOUNDARY_KEY_INDEX =
        Internal.createParameter("i_boundary_key_index", SQLDataType.VARCHAR, false, false);
    private static final Parameter<Boolean> PARTITION_WISE =
        Internal.createParameter("i_partition_wise", SQLDataType.BOOLEAN, false, false);

    CommittingProcedure(String sqlText, String drivingSchema, String drivingTable, String runLabel, int chunkSize,
                        int sleepMs) {
        super("run_migration_chunks_committing", DSL.schema(DSL.name("dml_utils")));
        addInParameter(SQL_TEXT);
        addInParameter(SCHEMA);
        addInParameter(TABLE);
        addInParameter(LABEL);
        addInParameter(CHUNK_SIZE);
        addInParameter(THREADS);
        addInParameter(ALIAS);
        addInParameter(CHUNK_BY);
        addInParameter(POLL_TIMEOUT_MS);
        addInParameter(SLEEP_MS);
        addInParameter(BOUNDARY_KEY_INDEX);
        addInParameter(PARTITION_WISE);
        setValue(SQL_TEXT, sqlText);
        setValue(SCHEMA, drivingSchema);
        setValue(TABLE, drivingTable);
        setValue(LABEL, runLabel);
        setValue(CHUNK_SIZE, chunkSize);
        setValue(THREADS, 1);
        setValue(ALIAS, "t");
        setValue(CHUNK_BY, "primary_key");
        setValue(POLL_TIMEOUT_MS, 1);
        setValue(SLEEP_MS, sleepMs);
        setValue(BOUNDARY_KEY_INDEX, null);
        setValue(PARTITION_WISE, false);
    }
}
