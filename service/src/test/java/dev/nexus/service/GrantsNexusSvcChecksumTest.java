// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import liquibase.ChecksumVersion;
import liquibase.change.CheckSum;
import liquibase.changelog.ChangeLogParameters;
import liquibase.changelog.ChangeSet;
import liquibase.changelog.DatabaseChangeLog;
import liquibase.parser.ChangeLogParser;
import liquibase.parser.ChangeLogParserFactory;
import liquibase.resource.ClassLoaderResourceAccessor;
import liquibase.resource.ResourceAccessor;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd.2.12 edited the body of {@code grants-nexus-svc-1} (its loop skips {@code nexus.data_moves}). The
 * changeset is {@code runAlways} and every migrated database holds the checksum of the text before the edit, so it
 * lists that checksum as a {@code <validCheckSum>}, the rule {@code grants-004-monitor-wal-visibility} sets and
 * {@link GrantsMonitorChecksumTest} pins: the REPLACED text's checksum, never the new one (listing the new one makes
 * validation a wildcard). This class pins the arithmetic without a database; the boot on an old row is
 * {@link GrantsNexusSvcUpgradeIntegrationTest}.
 *
 * <p>nexus-f3yqd.2.17 edited the body a second time (a REVOKE of every privilege nexus_svc holds on
 * {@code nexus.data_moves}, after the loop), so the text it replaced is listed as well, by the same rule: its checksum
 * ({@link #BEFORE_REVOKE_CHECKSUM}), never the new one.
 *
 * <p>The old text is {@code db/grants-nexus-svc-1-old-text/changelog.xml}, a byte copy of the changeset at a27ab2055,
 * the last commit before the edit. Both constants are Liquibase's own output ({@code ChangeSet.generateCheckSum}
 * with the latest checksum version, the "9:" form), not hand-derived.
 */
class GrantsNexusSvcChecksumTest {

    static final String CHANGESET_ID = "grants-nexus-svc-1";
    /** Liquibase's checksum of the changeset's text at a27ab2055, the one every migrated database holds. */
    static final String OLD_CHECKSUM = "9:15c9a38e1a6119c0702314e7d6922d73";
    /**
     * Liquibase's checksum of the text at c40f3b085: the loop skips nexus.data_moves, and nothing revokes. Every database
     * migrated by an engine that carried nexus-f3yqd.2.12 to .16 holds it (nexus-f3yqd.2.17 edited the body again).
     */
    static final String BEFORE_REVOKE_CHECKSUM = "9:6e8c092f58aba2818bb3f31943dd8f2f";
    /** Liquibase's checksum of the text the changelog carries now. */
    static final String NEW_CHECKSUM = "9:2d7a2244ff0b35bf3490f9c86d516ea8";

    private static final String HEAD_FILE = "db/changelog/grants-nexus-svc.xml";
    private static final String OLD_TEXT_FILE = "db/grants-nexus-svc-1-old-text/changelog.xml";
    private static final String BEFORE_REVOKE_TEXT_FILE = "db/grants-nexus-svc-1-before-revoke-text/changelog.xml";

    private static ChangeSet parse(String path) throws Exception {
        ResourceAccessor accessor = new ClassLoaderResourceAccessor();
        ChangeLogParser parser = ChangeLogParserFactory.getInstance().getParser(path, accessor);
        DatabaseChangeLog log = parser.parse(path, new ChangeLogParameters(), accessor);
        return log.getChangeSets().stream()
            .filter(cs -> CHANGESET_ID.equals(cs.getId()))
            .reduce((a, b) -> { throw new AssertionError("two changesets named " + CHANGESET_ID); })
            .orElseThrow(() -> new AssertionError(CHANGESET_ID + " not found in " + path));
    }

    private static String checksum(ChangeSet cs) {
        return cs.generateCheckSum(ChecksumVersion.latest()).toString();
    }

    @Test
    void theOldTextFixtureStillProducesTheShippedChecksum() throws Exception {
        assertThat(checksum(parse(OLD_TEXT_FILE)))
            .as("the byte copy of the a27ab2055 changeset must hash to the constant the migrated databases hold; "
                + "if this moves, Liquibase's checksum algorithm changed and every runAlways changeset's stored "
                + "checksum needs a validCheckSum")
            .isEqualTo(OLD_CHECKSUM);
    }

    @Test
    void theBeforeRevokeTextFixtureStillProducesTheChecksumMigratedDatabasesHold() throws Exception {
        assertThat(checksum(parse(BEFORE_REVOKE_TEXT_FILE)))
            .as("the byte copy of the c40f3b085 changeset must hash to the constant migrated databases hold")
            .isEqualTo(BEFORE_REVOKE_CHECKSUM);
    }

    @Test
    void theChangesetNowCarriesTheNewTextAndItsChecksum() throws Exception {
        assertThat(checksum(parse(HEAD_FILE)))
            .as("the text changed, so the checksum changed (else the edit changed nothing)")
            .isEqualTo(NEW_CHECKSUM)
            .isNotEqualTo(OLD_CHECKSUM)
            .isNotEqualTo(BEFORE_REVOKE_CHECKSUM);
    }

    @Test
    void aDatabaseMigratedWithTheOldChecksumStillValidates() throws Exception {
        ChangeSet head = parse(HEAD_FILE);
        assertThat(head.isCheckSumValid(CheckSum.parse(OLD_CHECKSUM)))
            .as("a databasechangelog row written before the registry edit must validate").isTrue();
        assertThat(head.isCheckSumValid(CheckSum.parse(BEFORE_REVOKE_CHECKSUM)))
            .as("a databasechangelog row written between the registry edit and the revoke must validate").isTrue();
        assertThat(head.isCheckSumValid(CheckSum.parse(NEW_CHECKSUM)))
            .as("a row written by this text must validate").isTrue();
    }

    @Test
    void theValidListHoldsOnlyTheOldChecksum_theCurrentOneWouldMakeItAWildcard() throws Exception {
        ChangeSet head = parse(HEAD_FILE);
        assertThat(head.getValidCheckSums().stream().map(CheckSum::toString))
            .as("the changeset's own current checksum must never be a validCheckSum (it makes validation accept every "
                + "stored value); an edit to this SQL adds the checksum of the text it REPLACES, never its own")
            .doesNotContain(checksum(head))
            .containsExactlyInAnyOrder(OLD_CHECKSUM, BEFORE_REVOKE_CHECKSUM);
        assertThat(head.isCheckSumValid(CheckSum.parse("9:00000000000000000000000000000000")))
            .as("a checksum nobody shipped must fail validation (non-vacuity: the list is not a wildcard)")
            .isFalse();
    }
}
