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

    // -- RDR-230 P1.1: the anchor ------------------------------------------------------------------------

    private static final String ANCHOR_TEXT =
        "{\"nexus_pci\":1,\"built_at\":\"2026-10-20T08:00:00Z\",\"rows\":58588,\"bytes\":480116736,"
            + "\"origin\":\"built\"}";

    @Test
    void anchor_writesTheRdrsExactText_andParsesItBack() {
        var anchor = new PciCatalog.Anchor(java.time.Instant.parse("2026-10-20T08:00:00Z"), 58588, 480116736,
            PciCatalog.ORIGIN_BUILT);

        assertThat(anchor.toJson()).isEqualTo(ANCHOR_TEXT);
        assertThat(PciCatalog.parseAnchor(ANCHOR_TEXT)).contains(anchor);
        // A time with a fraction round-trips at the microsecond PostgreSQL keeps, and no finer.
        var fine = new PciCatalog.Anchor(java.time.Instant.parse("2026-10-20T08:00:00.123456789Z"), 0, 0, "adopted");
        assertThat(fine.builtAt()).isEqualTo(java.time.Instant.parse("2026-10-20T08:00:00.123456Z"));
        assertThat(PciCatalog.parseAnchor(fine.toJson())).contains(fine);
    }

    private static final String ANCHOR_WITH_RELFILENODE =
        "{\"nexus_pci\":1,\"built_at\":\"2026-10-20T08:00:00Z\",\"rows\":58588,\"bytes\":480116736,"
            + "\"origin\":\"built\",\"relfilenode\":16421}";

    /**
     * The anchor rule of the engine before the relfilenode field (af47a7334), as the deployed engine runs it: the
     * version must be 1, the four fields must be there and well typed, and any other field is ignored. The text this
     * engine writes must pass it, so a rollback or a mixed-engine fleet does not re-adopt.
     */
    private static boolean oldRuleReadsIt(String comment) {
        try {
            var n = new com.fasterxml.jackson.databind.ObjectMapper().readTree(comment);
            if (n == null || !n.isObject()) {
                return false;
            }
            var version = n.get("nexus_pci");
            var builtAt = n.get("built_at");
            var rows = n.get("rows");
            var bytes = n.get("bytes");
            var origin = n.get("origin");
            if (version == null || !version.isIntegralNumber() || version.asInt() != 1
                || builtAt == null || !builtAt.isTextual()
                || rows == null || !rows.isIntegralNumber() || !rows.canConvertToLong() || rows.asLong() < 0
                || bytes == null || !bytes.isIntegralNumber() || !bytes.canConvertToLong() || bytes.asLong() < 0
                || origin == null || !origin.isTextual() || origin.asText().isBlank()) {
                return false;
            }
            java.time.Instant.parse(builtAt.asText());
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    @Test
    void anchor_recordsTheRelfilenodeItWasWrittenAgainst_asAnAdditiveField_andAnAnchorWithoutOneStaysValid() {
        var at = java.time.Instant.parse("2026-10-20T08:00:00Z");
        var stamped = new PciCatalog.Anchor(at, 58588, 480116736, PciCatalog.ORIGIN_BUILT, 16421L);

        assertThat(PciCatalog.ANCHOR_VERSION).as("additive: the version stays 1").isEqualTo(1);
        assertThat(stamped.toJson()).isEqualTo(ANCHOR_WITH_RELFILENODE);
        assertThat(PciCatalog.parseAnchor(ANCHOR_WITH_RELFILENODE)).contains(stamped);
        // The text this engine writes is read by the engine before the field existed.
        assertThat(oldRuleReadsIt(ANCHOR_WITH_RELFILENODE)).isTrue();
        assertThat(oldRuleReadsIt(ANCHOR_TEXT)).as("the check reads the old text too").isTrue();
        assertThat(oldRuleReadsIt(ANCHOR_TEXT.replace("\"nexus_pci\":1", "\"nexus_pci\":2")))
            .as("and the old rule refuses a version it does not know").isFalse();
        // An anchor written before the field existed is a valid anchor: its relfilenode is unknown, not an error.
        var old = PciCatalog.parseAnchor(ANCHOR_TEXT).orElseThrow();
        assertThat(old.relfilenode()).isNull();
        assertThat(old.builtAt()).isEqualTo(at);
        assertThat(old.rows()).isEqualTo(58588);
        assertThat(old.rebuiltSince(99L)).as("no relfilenode, nothing to differ from").isFalse();
        assertThat(stamped.rebuiltSince(16421L)).isFalse();
        assertThat(stamped.rebuiltSince(16500L)).isTrue();
        assertThat(stamped.rebuiltSince(null)).as("an unreadable current relfilenode is no evidence").isFalse();
        assertThat(stamped.withRelfilenode(7L).relfilenode()).isEqualTo(7L);
        assertThat(old.sameFigures(stamped)).as("the figures, not the relfilenode").isTrue();
        assertThat(old.withRelfilenode(16421L).toJson()).as("stamping adds the field and nothing else")
            .isEqualTo(ANCHOR_TEXT.replace("}", ",\"relfilenode\":16421}"));
        // An unknown extra field is ignored, and so is an optional field of the wrong type.
        assertThat(PciCatalog.parseAnchor(ANCHOR_WITH_RELFILENODE.replace("}", ",\"future\":1}"))).contains(stamped);
        for (String bad : new String[] {"\"16421\"", "-1", "1.5", "null"}) {
            assertThat(PciCatalog.parseAnchor(ANCHOR_WITH_RELFILENODE.replace("16421", bad))).as(bad)
                .hasValueSatisfying(a -> assertThat(a.relfilenode()).isNull());
        }
    }

    @Test
    void anchor_redUnfixed_isAnAdditiveOptionalField_thatRoundTrips_andTheOldRuleStillReadsIt() {
        var at = java.time.Instant.parse("2026-10-20T08:00:00Z");
        var red = new PciCatalog.RedUnfixed(java.time.Instant.parse("2026-10-21T09:30:00.123456Z"), 16421L, 58588L,
            java.util.List.of("89abcdef01234567", "0123456789abcdef"));
        var anchor = new PciCatalog.Anchor(at, 58588, 480116736, PciCatalog.ORIGIN_BUILT, 16421L, red);
        String text = ANCHOR_WITH_RELFILENODE.replace("}", ",\"red_unfixed\":{\"at\":\"2026-10-21T09:30:00.123456Z\","
            + "\"relfilenode\":16421,\"rows\":58588,\"queries\":[\"0123456789abcdef\",\"89abcdef01234567\"]}}");

        assertThat(anchor.toJson()).isEqualTo(text);
        assertThat(PciCatalog.parseAnchor(text)).contains(anchor);
        assertThat(oldRuleReadsIt(text)).as("an engine that knows only the old fields reads it").isTrue();
        assertThat(anchor.withoutRedUnfixed().toJson()).isEqualTo(ANCHOR_WITH_RELFILENODE);
        assertThat(anchor.withRelfilenode(9L).redUnfixed()).as("carried forward by a rewrite").isEqualTo(red);
        assertThat(anchor.sameFigures(anchor.withoutRedUnfixed())).isTrue();
        for (String bad : new String[] {"1", "\"x\"", "{}",
            "{\"at\":\"yesterday\",\"relfilenode\":1,\"rows\":1,\"queries\":[\"0123456789abcdef\"]}",
            "{\"at\":\"2026-10-21T09:30:00Z\",\"relfilenode\":-1,\"rows\":1,\"queries\":[\"0123456789abcdef\"]}",
            "{\"at\":\"2026-10-21T09:30:00Z\",\"relfilenode\":1,\"queries\":[\"0123456789abcdef\"]}",
            "{\"at\":\"2026-10-21T09:30:00Z\",\"relfilenode\":1,\"rows\":1,\"queries\":[]}",
            "{\"at\":\"2026-10-21T09:30:00Z\",\"relfilenode\":1,\"rows\":1,\"queries\":\"0123456789abcdef\"}",
            "{\"at\":\"2026-10-21T09:30:00Z\",\"relfilenode\":1,\"rows\":1,\"queries\":[\"short\"]}",
            "{\"at\":\"2026-10-21T09:30:00Z\",\"relfilenode\":1,\"rows\":1,\"queries\":[1]}"}) {
            assertThat(PciCatalog.parseAnchor(ANCHOR_WITH_RELFILENODE.replace("}", ",\"red_unfixed\":" + bad + "}")))
                .as(bad).hasValueSatisfying(a -> assertThat(a.redUnfixed()).isNull());
        }
    }

    @Test
    void queryIds_areSortedAndDistinct_andAQueryKeepsItsIdWhateverElseIsInTheSet() {
        var a = new float[] {1f, 2f, 3f};
        var b = new float[] {4f, 5f, 6f};
        var c = new float[] {7f, 8f, 9f};
        var idA = PciQueryRing.queryIds(java.util.List.of(a)).get(0);

        assertThat(idA).hasSize(PciQueryRing.QUERY_ID_HEX).matches("[0-9a-f]+");
        assertThat(PciQueryRing.queryIds(java.util.List.of(a, b))).isEqualTo(PciQueryRing.queryIds(java.util.List.of(b, a)))
            .isSorted().hasSize(2).contains(idA);
        assertThat(PciQueryRing.queryIds(java.util.List.of(a, c, a))).as("distinct").hasSize(2).contains(idA);
        assertThat(PciQueryRing.queryIds(java.util.List.of(a, b))).doesNotContainAnyElementsOf(
            PciQueryRing.queryIds(java.util.List.of(c)));
    }

    @Test
    void anchor_aTextWeDoNotWrite_isRefusedByTheWriter_andReadableByTheParser() {
        // Phase 2 adds an origin of its own; a reader of today still reads it, and this engine writes only two.
        String future = ANCHOR_TEXT.replace("\"built\"", "\"reindexed\"");
        assertThat(PciCatalog.parseAnchor(future)).hasValueSatisfying(a -> assertThat(a.origin()).isEqualTo("reindexed"));
        assertThatThrownBy(() -> new PciCatalog.Anchor(java.time.Instant.EPOCH, 0, 0, "reindexed").toJson())
            .isInstanceOf(IllegalStateException.class);
        assertThatThrownBy(() -> new PciCatalog.Anchor(java.time.Instant.EPOCH, -1, 0, "built"))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void parseAnchor_everythingThatIsNotOneAnchorIsEmpty_andNothingThrows() {
        for (String text : new String[] {
            null, "", "   ", "an operator's note", "[]", "[1]", "\"x\"", "42", "null", "{", "{}",
            ANCHOR_TEXT.replace("\"nexus_pci\":1", "\"nexus_pci\":2"),
            ANCHOR_TEXT.replace("\"nexus_pci\":1", "\"nexus_pci\":\"1\""),
            ANCHOR_TEXT.replace("\"nexus_pci\":1,", ""),
            ANCHOR_TEXT.replace("\"built_at\":\"2026-10-20T08:00:00Z\",", ""),
            ANCHOR_TEXT.replace("2026-10-20T08:00:00Z", "yesterday"),
            ANCHOR_TEXT.replace("\"rows\":58588", "\"rows\":-1"),
            ANCHOR_TEXT.replace("\"rows\":58588", "\"rows\":1.5"),
            ANCHOR_TEXT.replace("\"rows\":58588", "\"rows\":\"58588\""),
            ANCHOR_TEXT.replace("\"bytes\":480116736", "\"bytes\":99999999999999999999999"),
            ANCHOR_TEXT.replace(",\"origin\":\"built\"", ""),
            ANCHOR_TEXT.replace("\"origin\":\"built\"", "\"origin\":\"\""),
            ANCHOR_TEXT.replace("\"origin\":\"built\"", "\"origin\":7")}) {
            assertThat(PciCatalog.parseAnchor(text)).as(String.valueOf(text)).isEmpty();
        }
        // A well-formed anchor with an unknown field is still an anchor.
        assertThat(PciCatalog.parseAnchor(ANCHOR_TEXT.replace("}", ",\"future\":{\"a\":1}}"))).isPresent();
    }

    @Test
    void anIndexCarriesItsAnchor_andTheSnapshotAnswersWithTheValidIndexsOnly() {
        var anchor = new PciCatalog.Anchor(java.time.Instant.parse("2026-10-20T08:00:00Z"), 58588, 480116736, "built");
        var leaf = new PciCatalog.Leaf("nexus", "l", "m", "t", java.util.List.of(
            new PciCatalog.Index(GOOD_NAME, true, "c", null, ANCHOR_TEXT),
            new PciCatalog.Index("pci_" + "b".repeat(24), true, "unparsable", null, "not json"),
            new PciCatalog.Index("pci_" + "c".repeat(24), true, "uncommented"),
            new PciCatalog.Index("pci_" + "d".repeat(24), false, "building", null, ANCHOR_TEXT)));

        PciCatalog.Snapshot snap = new PciCatalog.Snapshot(java.util.List.of(leaf));

        assertThat(leaf.indexes().get(0).anchor()).contains(anchor);
        assertThat(leaf.indexes().get(1).anchor()).isEmpty();
        assertThat(leaf.indexes().get(2).comment()).isNull();
        assertThat(snap.anchor("m", "t", "c")).contains(anchor);
        assertThat(snap.anchor("m", "t", "unparsable")).as("a comment that does not parse").isEmpty();
        assertThat(snap.anchor("m", "t", "uncommented")).isEmpty();
        assertThat(snap.anchor("m", "t", "building")).as("an index that is not valid is not routed").isEmpty();
        assertThat(snap.anchor("m", "other", "c")).isEmpty();
        assertThat(((PciIndexSet) snap).anchor("m", "t", "c")).contains(anchor);
        assertThat(PciIndexSet.NONE.anchor("m", "t", "c")).isEmpty();
    }

    // ---- RDR-229 P3.4 (nexus-p2m1x.22): the id predicate ----

    /** The text PostgreSQL deparses for {@code WHERE collection_id = '42'} on a bigint column (PciIdIndexIntegrationTest pins it on a real server). */
    private static final String ID_PREDICATE = "(collection_id = '42'::bigint)";

    @Test
    void parseCollectionId_readsTheDeparsedIdPredicate() {
        assertThat(PciCatalog.parseCollectionId(ID_PREDICATE)).contains(42L);
        assertThat(PciCatalog.parseCollectionId("(collection_id = '9223372036854775807'::bigint)")).contains(Long.MAX_VALUE);
        assertThat(PciCatalog.parseCollectionId("(collection_id = '1'::bigint)")).contains(1L);
    }

    @Test
    void parseCollectionId_everythingElseIsUnparsed_andTheTwoShapesNeverCross() {
        assertThat(PciCatalog.parseCollectionId(null)).isEmpty();
        assertThat(PciCatalog.parseCollectionId("")).isEmpty();
        assertThat(PciCatalog.parseCollectionId("(collection_id = '42'::text)")).isEmpty();
        assertThat(PciCatalog.parseCollectionId("(collection_id = '4x2'::bigint)")).isEmpty();
        assertThat(PciCatalog.parseCollectionId("(collection_id = '9223372036854775808'::bigint)")).as("overflows a long").isEmpty();
        assertThat(PciCatalog.parseCollectionId("(collection_id = '-1'::bigint)")).as("an id is positive").isEmpty();
        assertThat(PciCatalog.parseCollectionId("((collection_id = '42'::bigint) AND (tenant_id = 't'::text))")).isEmpty();
        assertThat(PciCatalog.parseCollectionId("(collection = 'a'::text)")).as("a name predicate is not an id one").isEmpty();
        assertThat(PciCatalog.parseCollection(ID_PREDICATE)).as("an id predicate is not a name one").isEmpty();
        // A collection NAMED like an id predicate stays a name.
        assertThat(PciCatalog.parseCollection("(collection = '(collection_id = ''42''::bigint)'::text)"))
            .contains("(collection_id = '42'::bigint)");
    }

    @Test
    void indexNameById_hasTheBuilderShape_andNeverEqualsANameHash() {
        String byId = PciCatalog.indexNameById("m", "t", 42L);

        assertThat(byId).hasSize(28).matches("pci_[0-9a-f]{24}");
        assertThat(PciCatalog.isBuilderName(byId)).isTrue();
        assertThat(PciCatalog.indexNameById("m", "t", 42L)).isEqualTo(byId);
        assertThat(PciCatalog.indexNameById("m", "t", 43L)).isNotEqualTo(byId);
        assertThat(PciCatalog.indexNameById("m2", "t", 42L)).isNotEqualTo(byId);
        assertThat(PciCatalog.indexNameById("m", "t2", 42L)).isNotEqualTo(byId);
        // The id's decimal text as a NAME of the same collection: the domain tag keeps the two hashes apart.
        assertThat(PciCatalog.indexName("m", "t", "42")).isNotEqualTo(byId);
        // A name that spells the tag and the id cannot collide either: names hold no NUL, and the id input carries a third one.
        assertThat(PciCatalog.indexName("m", "t", "id")).isNotEqualTo(PciCatalog.indexNameById("m", "t", 1L));
        assertThat(PciCatalog.indexName("m", "t", "collection_id")).isNotEqualTo(byId);
        assertThat(PciCatalog.indexName("m", "t", "collection_id 42")).isNotEqualTo(byId);
    }

    @Test
    void indexNameById_refusesNulNullAndANonPositiveId() {
        assertThatThrownBy(() -> PciCatalog.indexNameById("m\0x", "t", 1L)).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> PciCatalog.indexNameById("m", "t\0x", 1L)).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> PciCatalog.indexNameById(null, "t", 1L)).isInstanceOf(NullPointerException.class);
        assertThatThrownBy(() -> PciCatalog.indexNameById("m", "t", 0L)).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> PciCatalog.indexNameById("m", "t", -3L)).isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void attributedCollectionId_parsesOnlyWhenEveryConditionHolds() {
        assertThat(PciCatalog.attributedCollectionId(GOOD_NAME, "hnsw", ID_PREDICATE, true, "on")).contains(42L);
        assertThat(PciCatalog.attributedCollectionId("pci_foo", "hnsw", ID_PREDICATE, true, "on")).isEmpty();
        assertThat(PciCatalog.attributedCollectionId(GOOD_NAME + "_ccnew", "hnsw", ID_PREDICATE, true, "on")).isEmpty();
        assertThat(PciCatalog.attributedCollectionId(GOOD_NAME, "btree", ID_PREDICATE, true, "on")).isEmpty();
        assertThat(PciCatalog.attributedCollectionId(GOOD_NAME, "hnsw", GOOD_PREDICATE, true, "on")).isEmpty();
        assertThat(PciCatalog.attributedCollectionId(GOOD_NAME, "hnsw", ID_PREDICATE, false, "on")).isEmpty();
        assertThat(PciCatalog.attributedCollectionId(GOOD_NAME, "hnsw", ID_PREDICATE, true, "off")).isEmpty();
        // The name rule does not read an id predicate, so one index is never both.
        assertThat(PciCatalog.attributedCollection(GOOD_NAME, "hnsw", ID_PREDICATE, true, "on")).isEmpty();
    }

    @Test
    void anIdIndexCarriesItsKindAndTheIdFromItsPredicate_andIsNotANameIndex() {
        var byId = new PciCatalog.Index(PciCatalog.indexNameById("m", "t", 42L), true, null, null, null, null, 42L);
        var byName = new PciCatalog.Index(GOOD_NAME, true, "c");
        var other = new PciCatalog.Index("pci_operator", true, null);

        assertThat(byId.kind()).isEqualTo(PciCatalog.PredicateKind.ID);
        assertThat(byId.idParsed()).isTrue();
        assertThat(byId.parsed()).as("parsed() stays the name rule: no name consumer reads an id index").isFalse();
        assertThat(byId.collection()).isNull();
        assertThat(byId.collectionId()).isEqualTo(42L);
        assertThat(byName.kind()).isEqualTo(PciCatalog.PredicateKind.NAME);
        assertThat(byName.collectionId()).isNull();
        assertThat(other.kind()).isEqualTo(PciCatalog.PredicateKind.OTHER);
        assertThatThrownBy(() -> new PciCatalog.Index("pci_x", true, "c", null, null, null, 42L))
            .as("a name and an id together").isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void theSnapshotKeepsTheNameAndTheIdIndexOfOneCollectionApart() {
        String idName = PciCatalog.indexNameById("m", "t", 42L);
        var leaf = new PciCatalog.Leaf("nexus", "l", "m", "t", java.util.List.of(
            new PciCatalog.Index(GOOD_NAME, true, "c"),
            new PciCatalog.Index(idName, true, null, null, null, null, 42L),
            new PciCatalog.Index(PciCatalog.indexNameById("m", "t", 43L), false, null, null, null, null, 43L),
            new PciCatalog.Index("pci_operator", true, null)));

        PciCatalog.Snapshot snap = new PciCatalog.Snapshot(java.util.List.of(leaf));

        assertThat(snap.hasValidIndex("m", "t", "c")).as("the name index").isTrue();
        assertThat(snap.hasValidIdIndex("m", "t", 42L)).as("the id index").isTrue();
        assertThat(snap.hasValidIdIndex("m", "t", 43L)).as("an INVALID id index").isFalse();
        assertThat(snap.hasValidIdIndex("m", "t2", 42L)).isFalse();
        assertThat(snap.validIdKeys()).containsExactly(PciCatalog.idKey("m", "t", 42L));
        assertThat(snap.invalidIdKeys()).containsExactly(PciCatalog.idKey("m", "t", 43L));
        assertThat(snap.validCount()).as("name indexes only: the router set never lists an id index").isEqualTo(1);
        assertThat(snap.validEntries()).containsExactly("l:c:" + GOOD_NAME);
        assertThat(snap.validIdCount()).isEqualTo(1);
        assertThat(snap.invalidIdCount()).isEqualTo(1);
        assertThat(snap.invalidCount()).isZero();
        assertThat(snap.unparsedCount()).as("an id index is not an unparsed one").isEqualTo(1);
        assertThat(PciCatalog.idKey("m", "t", 42L)).isNotEqualTo(PciCatalog.idKey("m", "t", 43L));
    }

    // ---- M4: the id attribution checks the name hash, as the builder's ownership does ----

    @Test
    void idOfBuilderIndex_acceptsOnlyTheNameTheBuilderGivesThatId() {
        String name = PciCatalog.indexNameById("m", "t", 42L);

        assertThat(PciCatalog.idOfBuilderIndex("m", "t", name, "hnsw", ID_PREDICATE, "on", false, true)).contains(42L);
        assertThat(PciCatalog.idOfBuilderIndex("m", "t", GOOD_NAME, "hnsw", ID_PREDICATE, "on", false, true))
            .as("an operator's pci_ index that reads an id predicate is not the builder's").isEmpty();
        assertThat(PciCatalog.idOfBuilderIndex("m", "t", PciCatalog.indexNameById("m", "t", 43L), "hnsw", ID_PREDICATE, "on", false, true))
            .as("another id's name").isEmpty();
        assertThat(PciCatalog.idOfBuilderIndex("m", "other", name, "hnsw", ID_PREDICATE, "on", false, true)).as("another tenant's").isEmpty();
        assertThat(PciCatalog.idOfBuilderIndex(null, "t", name, "hnsw", ID_PREDICATE, "on", false, true)).isEmpty();
        // The leftover of the builder's id index: its base name is checked.
        assertThat(PciCatalog.idOfBuilderIndex("m", "t", name + "_ccnew", "hnsw", ID_PREDICATE, "on", true, false)).contains(42L);
        assertThat(PciCatalog.idOfBuilderIndex("m", "t", GOOD_NAME + "_ccnew", "hnsw", ID_PREDICATE, "on", true, false)).isEmpty();
        assertThat(PciCatalog.idOfBuilderIndex("m", "t", name + "_ccnew", "hnsw", ID_PREDICATE, "on", true, true)).as("valid: not a leftover").isEmpty();
    }

    // ---- N2: the leftover of an id index's REINDEX CONCURRENTLY ----

    @Test
    void leftoverCollectionId_isAttributedOnlyToAnInvalidBuilderNamePlusCcnewOrCcold_withAnIdPredicate() {
        for (String suffix : new String[] {"_ccnew", "_ccold", "_ccnew1", "_ccold12"}) {
            assertThat(PciCatalog.leftoverCollectionId(GOOD_NAME + suffix, "hnsw", ID_PREDICATE, true, "on", false)).as(suffix).contains(42L);
        }
        assertThat(PciCatalog.leftoverCollectionId(GOOD_NAME + "_ccnew", "hnsw", ID_PREDICATE, true, "on", true)).as("valid: never a leftover").isEmpty();
        assertThat(PciCatalog.leftoverCollectionId(GOOD_NAME, "hnsw", ID_PREDICATE, true, "on", false)).as("the builder's own name").isEmpty();
        assertThat(PciCatalog.leftoverCollectionId(GOOD_NAME + "_ccnew", "hnsw", GOOD_PREDICATE, true, "on", false)).as("a name predicate").isEmpty();
        assertThat(PciCatalog.leftoverCollectionId(GOOD_NAME + "_ccnew", "btree", ID_PREDICATE, true, "on", false)).isEmpty();
        assertThat(PciCatalog.leftoverCollectionId(GOOD_NAME + "_ccnew", "hnsw", ID_PREDICATE, false, "on", false)).isEmpty();
        assertThat(PciCatalog.leftoverCollectionId(GOOD_NAME + "_ccnew", "hnsw", ID_PREDICATE, true, "off", false)).isEmpty();
        assertThat(PciCatalog.leftoverCollection(GOOD_NAME + "_ccnew", "hnsw", ID_PREDICATE, true, "on", false))
            .as("the name rule still reads no id predicate").isEmpty();
    }

    @Test
    void anIdLeftoverIsALeftover_notAnIdIndex_andTheSnapshotCountsItAsAnInvalidIdIndex() {
        var leftover = new PciCatalog.Index(GOOD_NAME + "_ccnew", false, null, null, null, null, null, 42L);
        assertThat(leftover.leftover()).isTrue();
        assertThat(leftover.leftoverOfId()).isEqualTo(42L);
        assertThat(leftover.leftoverOf()).isNull();
        assertThat(leftover.idParsed()).as("not an id index: nothing serves from it").isFalse();
        assertThat(leftover.parsed()).isFalse();
        assertThatThrownBy(() -> new PciCatalog.Index(GOOD_NAME + "_ccnew", true, null, null, null, null, null, 42L))
            .as("a valid index is never a leftover").isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new PciCatalog.Index("pci_x", false, "c", null, null, null, null, 42L)).isInstanceOf(IllegalArgumentException.class);

        var leaf = new PciCatalog.Leaf("nexus", "l", "m", "t", java.util.List.of(
            new PciCatalog.Index(PciCatalog.indexNameById("m", "t", 42L), true, null, null, null, null, 42L), leftover));
        var snap = new PciCatalog.Snapshot(java.util.List.of(leaf));

        assertThat(snap.reindexLeftoverCount()).isEqualTo(1);
        assertThat(snap.validIdCount()).as("the original").isEqualTo(1);
        assertThat(snap.invalidIdCount()).as("the leftover is an INVALID id-predicate index: id_index_invalid sees it").isEqualTo(1);
        assertThat(snap.invalidIdKeys()).containsExactly(PciCatalog.idKey("m", "t", 42L));
        assertThat(snap.unparsedCount()).isZero();
    }
}
