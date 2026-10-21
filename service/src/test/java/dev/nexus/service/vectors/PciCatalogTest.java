// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** RDR-227 Step 2 (nexus-43ulx.11): the pure half of {@link PciCatalog}. No database. */
class PciCatalogTest {

    @Test
    void indexName_is28Characters_andStableForFixedInputs() {
        String name = PciCatalog.indexName("voyage-code-3", "t1", "code__x__v1");

        assertThat(name).hasSize(28).startsWith("pci_").matches("pci_[0-9a-f]{24}");
        // sha256("voyage-code-3" NUL "t1" NUL "code__x__v1"), first 24 hex digits, computed outside this code.
        assertThat(name).isEqualTo("pci_7c53d438cd40ad5fa827b64e");
        assertThat(PciCatalog.indexName("voyage-code-3", "t1", "code__x__v1")).isEqualTo(name);
    }

    @Test
    void indexName_changesWithEachInput_andTheSeparatorKeepsFieldsApart() {
        String base = PciCatalog.indexName("m", "t", "c");

        assertThat(PciCatalog.indexName("m2", "t", "c")).isNotEqualTo(base);
        assertThat(PciCatalog.indexName("m", "t2", "c")).isNotEqualTo(base);
        assertThat(PciCatalog.indexName("m", "t", "c2")).isNotEqualTo(base);
        // Without a separator these two would hash the same bytes.
        assertThat(PciCatalog.indexName("ab", "c", "d")).isNotEqualTo(PciCatalog.indexName("a", "bc", "d"));
        assertThat(PciCatalog.indexName("a", "bc", "d")).isNotEqualTo(PciCatalog.indexName("a", "b", "cd"));
    }

    @Test
    void indexName_refusesNulAndNull() {
        assertThatThrownBy(() -> PciCatalog.indexName("m\0x", "t", "c")).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> PciCatalog.indexName("m", "t\0x", "c")).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> PciCatalog.indexName("m", "t", "c\0x")).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> PciCatalog.indexName(null, "t", "c")).isInstanceOf(NullPointerException.class);
    }

    @Test
    void parseCollection_readsThePredicateAndUndoublesQuotes() {
        assertThat(PciCatalog.parseCollection("(collection = 'code__1-1__v1'::text)")).contains("code__1-1__v1");
        assertThat(PciCatalog.parseCollection("(collection = 'it''s'::text)")).contains("it's");
        assertThat(PciCatalog.parseCollection("(collection = '''a'''''::text)")).contains("'a''");
        assertThat(PciCatalog.parseCollection("(collection = ''::text)")).contains("");
        assertThat(PciCatalog.parseCollection("(collection = 'a\\b'::text)")).contains("a\\b");
    }

    @Test
    void parseCollection_everythingElseIsUnparsed() {
        assertThat(PciCatalog.parseCollection(null)).isEmpty();
        assertThat(PciCatalog.parseCollection("")).isEmpty();
        assertThat(PciCatalog.parseCollection("((collection = 'a'::text) AND (tenant_id = 't'::text))")).isEmpty();
        assertThat(PciCatalog.parseCollection("((collection = 'a'::text) OR (collection = 'b'::text))")).isEmpty();
        assertThat(PciCatalog.parseCollection("(collection <> 'a'::text)")).isEmpty();
        assertThat(PciCatalog.parseCollection("(tenant_id = 'a'::text)")).isEmpty();
        assertThat(PciCatalog.parseCollection("(collection = ANY ('{a,b}'::text[]))")).isEmpty();
        assertThat(PciCatalog.parseCollection("(collection = 'a'::text) ")).isEmpty();
        assertThat(PciCatalog.parseCollection("collection = 'a'::text")).isEmpty();
        // An unbalanced quote: the run 'a'b' is not a quoted literal.
        assertThat(PciCatalog.parseCollection("(collection = 'a'b'::text)")).isEmpty();
        assertThat(PciCatalog.parseCollection("(collection = 'a'::varchar)")).isEmpty();
    }

    @Test
    void parseBoundValue_readsASingleValueListBound() {
        assertThat(PciCatalog.parseBoundValue("FOR VALUES IN ('minilm-l6-v2-384')")).contains("minilm-l6-v2-384");
        assertThat(PciCatalog.parseBoundValue("FOR VALUES IN ('o''brien')")).contains("o'brien");
        assertThat(PciCatalog.parseBoundValue("DEFAULT")).isEmpty();
        assertThat(PciCatalog.parseBoundValue("FOR VALUES IN ('a', 'b')")).isEmpty();
        assertThat(PciCatalog.parseBoundValue("FOR VALUES FROM (1) TO (2)")).isEmpty();
        assertThat(PciCatalog.parseBoundValue(null)).isEmpty();
    }

    private static final String GOOD_NAME = "pci_0123456789abcdef01234567";
    private static final String GOOD_PREDICATE = "(collection = 'c'::text)";

    @Test
    void attributedCollection_parsesOnlyWhenEveryConditionHolds() {
        assertThat(PciCatalog.attributedCollection(GOOD_NAME, "hnsw", GOOD_PREDICATE, true, "on")).contains("c");
        // Each condition alone breaks it.
        assertThat(PciCatalog.attributedCollection("pci_foo", "hnsw", GOOD_PREDICATE, true, "on")).isEmpty();
        assertThat(PciCatalog.attributedCollection(GOOD_NAME + "_ccnew", "hnsw", GOOD_PREDICATE, true, "on")).isEmpty();
        assertThat(PciCatalog.attributedCollection("pci_0123456789ABCDEF01234567", "hnsw", GOOD_PREDICATE, true, "on"))
            .as("upper case hex is not the builder's name").isEmpty();
        assertThat(PciCatalog.attributedCollection("pci_0123456789abcdef0123456", "hnsw", GOOD_PREDICATE, true, "on"))
            .as("23 digits").isEmpty();
        assertThat(PciCatalog.attributedCollection(null, "hnsw", GOOD_PREDICATE, true, "on")).isEmpty();
        assertThat(PciCatalog.attributedCollection(GOOD_NAME, "btree", GOOD_PREDICATE, true, "on")).isEmpty();
        assertThat(PciCatalog.attributedCollection(GOOD_NAME, "ivfflat", GOOD_PREDICATE, true, "on")).isEmpty();
        assertThat(PciCatalog.attributedCollection(GOOD_NAME, null, GOOD_PREDICATE, true, "on")).isEmpty();
        assertThat(PciCatalog.attributedCollection(GOOD_NAME, "hnsw", "(tenant_id = 'c'::text)", true, "on")).isEmpty();
        assertThat(PciCatalog.attributedCollection(GOOD_NAME, "hnsw", null, true, "on")).isEmpty();
        assertThat(PciCatalog.attributedCollection(GOOD_NAME, "hnsw", GOOD_PREDICATE, false, "on")).isEmpty();
    }

    @Test
    void attributedCollection_standardConformingStringsNotOn_isUnparsed() {
        assertThat(PciCatalog.attributedCollection(GOOD_NAME, "hnsw", GOOD_PREDICATE, true, "off")).isEmpty();
        assertThat(PciCatalog.attributedCollection(GOOD_NAME, "hnsw", GOOD_PREDICATE, true, "")).isEmpty();
        assertThat(PciCatalog.attributedCollection(GOOD_NAME, "hnsw", GOOD_PREDICATE, true, null)).isEmpty();
    }

    // ---- REINDEX CONCURRENTLY leftovers (nexus-rn1yd, T2 nexus_rdr/227-decision-4-amendment-reindex-leftovers) ----

    @Test
    void leftoverCollection_isAttributedOnlyToAnInvalidBuilderNamePlusCcnewOrCcold() {
        for (String suffix : new String[] {"_ccnew", "_ccold", "_ccnew1", "_ccold12", "_ccnew0"}) {
            assertThat(PciCatalog.leftoverCollection(GOOD_NAME + suffix, "hnsw", GOOD_PREDICATE, true, "on", false))
                .as(suffix).contains("c");
        }
        // A VALID _ccnew/_ccold is never a leftover: it is unparsed and nothing touches it.
        assertThat(PciCatalog.leftoverCollection(GOOD_NAME + "_ccnew", "hnsw", GOOD_PREDICATE, true, "on", true))
            .isEmpty();
        assertThat(PciCatalog.leftoverCollection(GOOD_NAME + "_ccold", "hnsw", GOOD_PREDICATE, true, "on", true))
            .isEmpty();
        // The builder's own name is not a leftover whatever its validity (a failed build is FAILED_BUILD).
        assertThat(PciCatalog.leftoverCollection(GOOD_NAME, "hnsw", GOOD_PREDICATE, true, "on", false)).isEmpty();
    }

    @Test
    void leftoverCollection_everyOtherConditionStillBreaksIt() {
        String ok = GOOD_NAME + "_ccnew";
        assertThat(PciCatalog.leftoverCollection(ok, "hnsw", GOOD_PREDICATE, true, "on", false)).contains("c");
        assertThat(PciCatalog.leftoverCollection(ok, "btree", GOOD_PREDICATE, true, "on", false)).isEmpty();
        assertThat(PciCatalog.leftoverCollection(ok, null, GOOD_PREDICATE, true, "on", false)).isEmpty();
        assertThat(PciCatalog.leftoverCollection(ok, "hnsw", "(tenant_id = 'c'::text)", true, "on", false)).isEmpty();
        assertThat(PciCatalog.leftoverCollection(ok, "hnsw", null, true, "on", false)).isEmpty();
        assertThat(PciCatalog.leftoverCollection(ok, "hnsw", GOOD_PREDICATE, false, "on", false))
            .as("a leaf whose model or tenant did not parse").isEmpty();
        assertThat(PciCatalog.leftoverCollection(ok, "hnsw", GOOD_PREDICATE, true, "off", false))
            .as("scs off").isEmpty();
        assertThat(PciCatalog.leftoverCollection(ok, "hnsw", GOOD_PREDICATE, true, null, false)).isEmpty();
    }

    @Test
    void leftoverCollection_refusesEveryNameThatIsNotABuilderNameWithTheLeftoverSuffix() {
        for (String name : new String[] {
            "pci_foo", "pci_foo_ccnew", null, "",
            "pci_0123456789ABCDEF01234567_ccnew",     // upper case hex
            "pci_0123456789abcdef0123456_ccnew",      // 23 digits
            "pci_0123456789abcdef012345678_ccnew",    // 25 digits
            GOOD_NAME + "_ccnewx",                    // not digits
            GOOD_NAME + "_ccnew_1",
            GOOD_NAME + "_ccnew1x",
            GOOD_NAME + "_cc",
            GOOD_NAME + "_ccnewold",
            GOOD_NAME + "ccnew",
            "x" + GOOD_NAME + "_ccnew"}) {
            assertThat(PciCatalog.leftoverCollection(name, "hnsw", GOOD_PREDICATE, true, "on", false))
                .as(String.valueOf(name)).isEmpty();
        }
    }

    @Test
    void anIndexIsEitherParsedOrALeftoverOrNeither_neverTwo() {
        var parsed = new PciCatalog.Index(GOOD_NAME, true, "c");
        var leftover = new PciCatalog.Index(GOOD_NAME + "_ccnew", false, null, "c");
        var unparsed = new PciCatalog.Index("pci_foo", true, null);

        assertThat(parsed.parsed()).isTrue();
        assertThat(parsed.leftover()).isFalse();
        assertThat(leftover.parsed()).as("a leftover is not parsed in the routing sense").isFalse();
        assertThat(leftover.leftover()).isTrue();
        assertThat(leftover.leftoverOf()).isEqualTo("c");
        assertThat(unparsed.parsed()).isFalse();
        assertThat(unparsed.leftover()).isFalse();
        org.assertj.core.api.Assertions.assertThatThrownBy(() -> new PciCatalog.Index(GOOD_NAME, false, "a", "b"))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void aValidIndexCannotRepresentALeftover() {
        org.assertj.core.api.Assertions
            .assertThatThrownBy(() -> new PciCatalog.Index(GOOD_NAME + "_ccnew", true, null, "c"))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("valid index is never");
        // The invalid pair still builds, and so does a valid unparsed index with no claim.
        assertThat(new PciCatalog.Index(GOOD_NAME + "_ccnew", false, null, "c").leftover()).isTrue();
        assertThat(new PciCatalog.Index(GOOD_NAME + "_ccnew", true, null).leftover()).isFalse();
    }

    @Test
    void snapshot_countsALeftoverOnItsOwn_notAsUnparsed_notAsInvalid_andNeverRoutesToIt() {
        var leaf = new PciCatalog.Leaf("nexus", "l", "m", "t", java.util.List.of(
            new PciCatalog.Index(GOOD_NAME, true, "c"),
            new PciCatalog.Index(GOOD_NAME + "_ccnew", false, null, "c"),
            new PciCatalog.Index(GOOD_NAME + "_ccold1", false, null, "c"),
            new PciCatalog.Index("pci_foo", true, null),
            new PciCatalog.Index("pci_" + "a".repeat(24) + "_ccnew", true, null)));

        PciCatalog.Snapshot snap = new PciCatalog.Snapshot(java.util.List.of(leaf));

        assertThat(snap.validCount()).isEqualTo(1);
        assertThat(snap.invalidCount()).as("leftovers are not parsed invalid builds").isZero();
        assertThat(snap.unparsedCount()).as("leftovers are counted once, apart from unparsed").isEqualTo(2);
        assertThat(snap.reindexLeftoverCount()).isEqualTo(2);
        assertThat(snap.hasValidIndex("m", "t", "c")).isTrue();
        assertThat(snap.validEntries()).containsExactly("l:c:" + GOOD_NAME);
    }

    @Test
    void aLeafWithOnlyALeftover_hasNoValidIndex() {
        var leaf = new PciCatalog.Leaf("nexus", "l", "m", "t", java.util.List.of(
            new PciCatalog.Index(GOOD_NAME + "_ccnew", false, null, "c")));

        PciCatalog.Snapshot snap = new PciCatalog.Snapshot(java.util.List.of(leaf));

        assertThat(snap.hasValidIndex("m", "t", "c")).isFalse();
        assertThat(snap.validIndex("m", "t", "c")).isEmpty();
        assertThat(snap.reindexLeftoverCount()).isEqualTo(1);
        assertThat(snap.validCount() + snap.invalidCount() + snap.unparsedCount()).isZero();
    }

    @Test
    void snapshot_ofNoLeaves_isEmptyAndCountsNothing() {
        PciCatalog.Snapshot snap = new PciCatalog.Snapshot(java.util.List.of());

        assertThat(snap.leaves()).isEmpty();
        assertThat(snap.validCount()).isZero();
        assertThat(snap.unparsedCount()).isZero();
        assertThat(snap.hasValidIndex("m", "t", "c")).isFalse();
    }
}
