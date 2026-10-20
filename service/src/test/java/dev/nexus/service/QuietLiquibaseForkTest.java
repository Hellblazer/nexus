// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;

import java.util.logging.Level;
import java.util.logging.Logger;
import org.junit.jupiter.api.Test;

/**
 * nexus-km7ma: pins that the surefire fork really runs Liquibase quiet. A later argLine
 * override (jacoco, {@code @{argLine}}) would silently drop the JUL config flag; the pom
 * lint only reads text, this reads the live fork.
 */
class QuietLiquibaseForkTest {

    @Test
    void liquibaseLoggerEffectiveLevelIsWarning() {
        Level effective = null;
        for (Logger c = Logger.getLogger("liquibase"); c != null && effective == null; c = c.getParent()) {
            effective = c.getLevel();
        }
        assertNotNull(effective);
        assertEquals(Level.WARNING, effective);
    }

    @Test
    void liquibaseUpdateSummaryIsOff() {
        assertEquals("OFF", System.getProperty("liquibase.command.showSummary"));
    }
}
