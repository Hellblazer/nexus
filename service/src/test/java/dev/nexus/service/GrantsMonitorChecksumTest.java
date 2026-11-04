// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import liquibase.changelog.ChangeLogParameters;
import liquibase.changelog.ChangeSet;
import liquibase.changelog.DatabaseChangeLog;
import liquibase.parser.ChangeLogParser;
import liquibase.parser.ChangeLogParserFactory;
import liquibase.resource.ClassLoaderResourceAccessor;
import liquibase.resource.ResourceAccessor;
import liquibase.ChecksumVersion;
import liquibase.change.CheckSum;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * grants-004-monitor-wal-visibility's RAISE message used to name two repo-only files
 * ({@code src/nexus/db/pg_provision.py}, {@code docs/configuration.md}) to operators who have no
 * checkout. The text is part of the changeset's checksum, and the changeset is {@code runAlways}
 * and already recorded in every migrated database's {@code databasechangelog}, so the edit carries
 * a {@code <validCheckSum>} for the text engines up to and including engine-service-v0.1.178
 * shipped (the old checksum only; the wildcard test below says why). This class pins the checksum arithmetic without a database; the boot-on-an-old-row proof
 * is {@code SchemaMigratorIntegrationTest#grants004_migratedWithTheOldChecksum_stillBoots_...}.
 *
 * <p>The old text lives in {@code db/grants-004-old-text/changelog.xml}, a byte copy of the
 * changeset from the v0.1.178 tag (the body never changed in any shipped tag since v0.1.75: the
 * body hash is identical across all of them). The two constants are Liquibase's own output for the
 * old and new text, not hand-derived.
 */
class GrantsMonitorChecksumTest {

    static final String CHANGESET_ID = "grants-004-monitor-wal-visibility";
    /** Liquibase's checksum of the text engine-service-v0.1.75 through v0.1.178 shipped. */
    static final String OLD_CHECKSUM = "9:290051361d2bf95a1a84d7592c6b93b2";
    /** Liquibase's checksum of the text the changelog carries now. */
    static final String NEW_CHECKSUM = "9:7ca5e06c2689ec433055061da82eb147";

    private static final String HEAD_FILE = "db/changelog/grants-nexus-svc.xml";
    private static final String OLD_TEXT_FILE = "db/grants-004-old-text/changelog.xml";

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
            .as("the byte copy of the v0.1.178 changeset must hash to the constant the migrated "
                + "databases hold; if this moves, Liquibase's checksum algorithm changed and every "
                + "runAlways changeset's stored checksum needs a validCheckSum")
            .isEqualTo(OLD_CHECKSUM);
    }

    @Test
    void theChangesetNowCarriesTheNewTextAndItsChecksum() throws Exception {
        ChangeSet head = parse(HEAD_FILE);
        assertThat(checksum(head))
            .as("the text changed, so the checksum changed (else this edit changed nothing)")
            .isEqualTo(NEW_CHECKSUM)
            .isNotEqualTo(OLD_CHECKSUM);
    }

    @Test
    void aDatabaseMigratedWithTheOldChecksumStillValidates() throws Exception {
        ChangeSet head = parse(HEAD_FILE);
        assertThat(head.isCheckSumValid(CheckSum.parse(OLD_CHECKSUM)))
            .as("a databasechangelog row written by any engine through v0.1.178 must validate")
            .isTrue();
        assertThat(head.isCheckSumValid(CheckSum.parse(NEW_CHECKSUM)))
            .as("a row written by this text must validate").isTrue();
    }

    /**
     * Liquibase 4.29 accepts a stored checksum when the changeset's CURRENT checksum equals any
     * validCheckSum entry, so listing the current checksum makes every stored value valid. The
     * documented advice ("either the old checksum or the new checksum from the error message")
     * is therefore wrong for the new one. The list holds the old checksum and nothing else.
     */
    @Test
    void theValidListHoldsOnlyTheOldChecksum_theCurrentOneWouldMakeItAWildcard() throws Exception {
        ChangeSet head = parse(HEAD_FILE);
        assertThat(head.getValidCheckSums().stream().map(CheckSum::toString))
            .containsExactly(OLD_CHECKSUM);
        assertThat(head.isCheckSumValid(CheckSum.parse("9:00000000000000000000000000000000")))
            .as("a checksum nobody shipped must fail validation (non-vacuity: the list is not a wildcard)")
            .isFalse();
    }

    @Test
    void listingTheCurrentChecksumWouldAcceptAnything_soThisPinDetectsTheMistake() throws Exception {
        // Falsification of the rule above, on the old-text fixture: give it ITS OWN checksum as a
        // validCheckSum and a bogus stored value validates. If Liquibase ever fixes this, this test
        // fails and the comment in grants-nexus-svc.xml can be retired.
        ChangeSet fixture = parse(OLD_TEXT_FILE);
        fixture.addValidCheckSum(checksum(fixture));
        assertThat(fixture.isCheckSumValid(CheckSum.parse("9:00000000000000000000000000000000"))).isTrue();
    }
}
