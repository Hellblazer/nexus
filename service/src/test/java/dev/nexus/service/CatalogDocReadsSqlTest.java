// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogDocReads;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.math.BigInteger;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-uwggq.2.3 and .4 (engine review r1): the SQL the two paged reads render, pinned without a database. The meta_min
 * bound must keep its cast behind the regex guard in a CASE (PostgreSQL does not promise the order of an AND, and the cast
 * raises on text that is not an integer, which would fail the whole read on one malformed value); the author statement must
 * fold with the pg_c_utf8 collation on BOTH sides of the LIKE, because a bare lower() folds ASCII only on a C-ctype cluster.
 */
class CatalogDocReadsSqlTest {

    private static String sql(org.jooq.Condition c) {
        return DSL.using(SQLDialect.POSTGRES).renderInlined(c).toLowerCase();
    }

    @Test
    void theIntegerBoundKeepsItsCastBehindTheRegexInsideACase() {
        var filter = new CatalogDocReads.MetaFilter(null, List.of(), List.of(new CatalogDocReads.MetaMin("miss_count", BigInteger.TWO)), false);
        String sql = sql(CatalogDocReads.metaWhere("t", filter));
        int caseAt = sql.indexOf("case when");
        int thenAt = sql.indexOf(" then ", caseAt);
        int castAt = sql.indexOf("cast(", thenAt);
        assertThat(caseAt).as(sql).isGreaterThanOrEqualTo(0);
        assertThat(sql.substring(caseAt, thenAt)).as("the regex test is the WHEN").contains("~").doesNotContain("cast(");
        assertThat(castAt).as("the numeric cast comes after THEN").isGreaterThan(thenAt);
        assertThat(sql.substring(castAt)).contains("as numeric").contains("else false end");
        assertThat(sql.substring(0, caseAt)).as("no cast before the CASE: not a regex AND cast").doesNotContain("as numeric");
    }

    @Test
    void theAuthorStatementFoldsWithPgCUtf8OnBothSidesOfTheLike() {
        String sql = sql(CatalogDocReads.authorWhere("t", "müller"));
        assertThat(sql).contains("lower(").contains("like").contains("escape");
        assertThat(sql.split("collate \"pg_c_utf8\"", -1).length - 1).as("one collation per side: " + sql).isEqualTo(2);
    }

    @Test
    void theSchemeStatementStripsThenRemovesThenReadsThePrefixInThatOrder() {
        String sql = sql(CatalogDocReads.schemeWhere("t", "https"));
        int ltrim = sql.indexOf("ltrim(");
        int translate = sql.indexOf("translate(");
        int substring = sql.indexOf("substring(");
        assertThat(substring).as(sql).isGreaterThanOrEqualTo(0);
        assertThat(translate).as("translate (tab, CR, LF out) wraps the ltrim (C0 and space off the left), as urlsplit orders them").isGreaterThan(substring);
        assertThat(ltrim).isGreaterThan(translate);
        assertThat(sql).contains("'https'");
    }

    @Test
    void sensitiveOnlyAddsTheFlagToThePredicate() {
        var eq = List.of(new CatalogDocReads.MetaEq("k", "v", null));
        assertThat(sql(CatalogDocReads.metaWhere("t", new CatalogDocReads.MetaFilter(null, eq, List.of(), true)))).contains("sensitive");
        assertThat(sql(CatalogDocReads.metaWhere("t", new CatalogDocReads.MetaFilter(null, eq, List.of(), false)))).doesNotContain("sensitive");
    }
}
