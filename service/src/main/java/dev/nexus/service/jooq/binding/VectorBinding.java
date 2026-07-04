/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.jooq.binding;

import org.jooq.Binding;
import org.jooq.BindingGetResultSetContext;
import org.jooq.BindingGetSQLInputContext;
import org.jooq.BindingGetStatementContext;
import org.jooq.BindingRegisterContext;
import org.jooq.BindingSQLContext;
import org.jooq.BindingSetSQLOutputContext;
import org.jooq.BindingSetStatementContext;
import org.jooq.Converter;
import org.jooq.conf.ParamType;
import org.jooq.impl.DSL;

import java.sql.SQLException;
import java.sql.SQLFeatureNotSupportedException;
import java.sql.Types;

/**
 * jOOQ {@link Binding} for the pgvector {@code vector} type as {@link Vector}
 * (nexus-xtmtf).
 *
 * <p>Registered via the codegen {@code forcedType}, so every generated
 * {@code chunks_<dim>.embedding} / {@code taxonomy_centroids_<dim>.embedding}
 * field is a {@code TableField<R, Vector>} and DSL statements bind vectors
 * directly — this class owns the {@code ::vector} cast and the
 * {@code [f1,f2,...]} text literal that previously lived as raw-SQL
 * {@code ?::vector} + {@code vectorLiteral()} string building at every call
 * site.
 *
 * <p>In-repo rather than jOOQ's own {@code FloatVector} because pgvector
 * support in {@code jooq-postgres-extensions} is commercial-edition-only;
 * the OSS jar carries no vector classes.
 */
public class VectorBinding implements Binding<Object, Vector> {

    private static final Converter<Object, Vector> CONVERTER = new Converter<>() {
        @Override
        public Vector from(Object db) {
            return db == null ? null : Vector.parse(db.toString());
        }

        @Override
        public Object to(Vector user) {
            return user == null ? null : user.toString();
        }

        @Override
        public Class<Object> fromType() {
            return Object.class;
        }

        @Override
        public Class<Vector> toType() {
            return Vector.class;
        }
    };

    @Override
    public Converter<Object, Vector> converter() {
        return CONVERTER;
    }

    @Override
    public void sql(BindingSQLContext<Vector> ctx) {
        if (ctx.render().paramType() == ParamType.INLINED) {
            Vector v = ctx.value();
            ctx.render().visit(DSL.inline(v == null ? null : v.toString()))
               .sql("::vector");
        } else {
            ctx.render().sql(ctx.variable()).sql("::vector");
        }
    }

    @Override
    public void register(BindingRegisterContext<Vector> ctx) throws SQLException {
        ctx.statement().registerOutParameter(ctx.index(), Types.VARCHAR);
    }

    @Override
    public void set(BindingSetStatementContext<Vector> ctx) throws SQLException {
        Vector v = ctx.value();
        if (v == null) {
            ctx.statement().setNull(ctx.index(), Types.OTHER);
        } else {
            ctx.statement().setString(ctx.index(), v.toString());
        }
    }

    @Override
    public void get(BindingGetResultSetContext<Vector> ctx) throws SQLException {
        String s = ctx.resultSet().getString(ctx.index());
        ctx.value(s == null ? null : Vector.parse(s));
    }

    @Override
    public void get(BindingGetStatementContext<Vector> ctx) throws SQLException {
        String s = ctx.statement().getString(ctx.index());
        ctx.value(s == null ? null : Vector.parse(s));
    }

    @Override
    public void set(BindingSetSQLOutputContext<Vector> ctx) throws SQLException {
        throw new SQLFeatureNotSupportedException("vector via SQLOutput");
    }

    @Override
    public void get(BindingGetSQLInputContext<Vector> ctx) throws SQLException {
        throw new SQLFeatureNotSupportedException("vector via SQLInput");
    }
}
