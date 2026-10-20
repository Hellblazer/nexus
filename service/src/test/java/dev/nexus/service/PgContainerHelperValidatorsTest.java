package dev.nexus.service;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Container-free pins on the validators in front of {@link PgContainerHelper}'s fixed-statement test changelogs
 * (nexus-cbo4a batch 11 review fixes). A changelog substitutes its parameters into SQL text, so what the validator
 * admits is the whole injection surface: these tests state it, in both directions, with no database.
 */
class PgContainerHelperValidatorsTest {

    private static final DSLContext CTX = DSL.using(SQLDialect.POSTGRES);

    // Plain-SQL table text, held in constants: the point of these tests is that such a Table is REFUSED, and a literal
    // argument to DSL.table would (rightly) be flagged by RawSqlGateTest's DSL-template scan.
    private static final String PLAIN_SQL_DROP = "x; drop";
    private static final String PLAIN_UNQUOTED = "nexus.chunks";
    private static final String PLAIN_QUOTED_THEN_DROP = "\"nexus\".\"chunks\"; drop table x";

    // ── requireTestLiteral ───────────────────────────────────────────────────────────────────

    @Test
    void literal_admitsABackslash_asALiteralCharacter() {
        // PciCatalogIntegrationTest#aCollectionNameWithABackslash_roundTripsWithStandardConformingStringsOn feeds this
        // name through createLeafHnswIndex. Under standard_conforming_strings = on (that test asserts it) a backslash
        // inside '...' is an ordinary character, so it is passed through unchanged.
        String name = "knowledge__back\\slash__minilm-l6-v2-384__v1";
        assertThat(PgContainerHelper.requireTestLiteral(name)).isEqualTo(name);
    }

    @Test
    void literal_doublesEachApostrophe_andNothingElse() {
        assertThat(PgContainerHelper.requireTestLiteral("it's")).isEqualTo("it''s");
        assertThat(PgContainerHelper.requireTestLiteral("a'b'c")).isEqualTo("a''b''c");
        assertThat(PgContainerHelper.requireTestLiteral("code__x-y.z__v1")).isEqualTo("code__x-y.z__v1");
    }

    @Test
    void literal_backslashBeforeAQuote_staysInsideTheLiteral() {
        // 'a\'' under scs=on is: a, backslash, an escaped apostrophe, then the closing quote is the NEXT character
        // in the changelog. The doubled apostrophe keeps the value inside the literal; a backslash cannot escape it.
        assertThat(PgContainerHelper.requireTestLiteral("a\\'")).isEqualTo("a\\''");
    }

    @Test
    void literal_refusesWhatCouldReachTheChangelogOrEndTheStatement() {
        for (String bad : new String[] {
            "a$b", "${x}", "a{b", "a}b", "a;b", "a b", "a\tb", "a\nb", "a\rb", "a\"b", "a'; drop table x; --",
            "a'||(select 1)||'", "$$", "a`b", "a(b", "a,b", "a/b", "a*b", "a=b", "é", "", " "}) {
            assertThatThrownBy(() -> PgContainerHelper.requireTestLiteral(bad))
                .as("must stay rejected: [" + bad.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + "]")
                .isInstanceOf(IllegalArgumentException.class);
        }
        assertThatThrownBy(() -> PgContainerHelper.requireTestLiteral(null))
            .isInstanceOf(IllegalArgumentException.class);
    }

    // ── requireTestIdentifier (the one validator PartitionScratch now shares) ─────────────────

    @Test
    void identifier_isAPlainLowercaseName_otherThanPublic() {
        PgContainerHelper.requireTestIdentifier("chunks_m1234abcd_t_0123456789abcdef");
        PgContainerHelper.requireTestIdentifier("_x9");
        for (String bad : new String[] {"public", "", "1a", "A", "a-b", "a b", "a;b", "a.b", "a\"b", "a$b", "a'b"}) {
            assertThatThrownBy(() -> PgContainerHelper.requireTestIdentifier(bad))
                .as("must stay rejected: [" + bad + "]").isInstanceOf(IllegalArgumentException.class);
        }
        assertThatThrownBy(() -> PgContainerHelper.requireTestIdentifier(null))
            .isInstanceOf(IllegalArgumentException.class);
    }

    // ── renderTableParam: a Table<?> that reaches a changelog parameter ──────────────────────

    @Test
    void renderedTable_admitsAQuotedNameWithOrWithoutASchema() {
        assertThat(PgContainerHelper.renderTableParam(CTX, DSL.table(DSL.name("nexus", "chunks"))))
            .isEqualTo("\"nexus\".\"chunks\"");
        assertThat(PgContainerHelper.renderTableParam(CTX, DSL.table(DSL.name("databasechangelog"))))
            .isEqualTo("\"databasechangelog\"");
    }

    @Test
    void renderedTable_refusesPlainSqlAndAnythingElseThatIsNotAQuotedPlainName() {
        // DSL.table(String) is plain SQL: it renders as the text it was given, so the Table type alone is no guard.
        assertThatThrownBy(() -> PgContainerHelper.renderTableParam(CTX, DSL.table(PLAIN_SQL_DROP)))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("quoted plain name");
        for (org.jooq.Table<?> bad : new org.jooq.Table<?>[] {
                DSL.table(PLAIN_UNQUOTED),
                DSL.table(PLAIN_QUOTED_THEN_DROP),
                DSL.table(DSL.name("Nexus", "chunks")),
                DSL.table(DSL.name("nexus", "Chunks")),
                DSL.table(DSL.name("a", "b", "c")),
                DSL.table(DSL.name("a b")),
                DSL.table(DSL.name("a\"b")),
                DSL.table(DSL.name("1a"))}) {
            assertThatThrownBy(() -> PgContainerHelper.renderTableParam(CTX, bad))
                .as("must stay rejected: " + CTX.render(bad)).isInstanceOf(IllegalArgumentException.class);
        }
    }

    // ── createBtreeIndexWhereEquals is typed jOOQ, not a changelog ───────────────────────────

    @Test
    void btreeIndexWhereEquals_rendersAsATypedCreateIndex_withTheValueEscapedByJooq() {
        assertThat(CTX.renderInlined(PgContainerHelper.btreeIndexWhereEqualsQuery(
                CTX, "nexus", "chunks_t", "pci_x", "collection", "zzz")))
            .isEqualTo("create index \"pci_x\" on \"nexus\".\"chunks_t\"(\"collection\") where \"collection\" = 'zzz'");
        assertThat(CTX.renderInlined(PgContainerHelper.btreeIndexWhereEqualsQuery(
                CTX, "nexus", "chunks_t", "pci_x", "collection", "it's")))
            .endsWith("where \"collection\" = 'it''s'");
        assertThatThrownBy(() -> PgContainerHelper.btreeIndexWhereEqualsQuery(
                CTX, "nexus", "chunks_t", "pci_x", "collection; drop", "zzz"))
            .isInstanceOf(IllegalArgumentException.class);
    }
}
