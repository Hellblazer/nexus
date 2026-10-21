// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import liquibase.Scope;
import liquibase.ui.LoggerUIService;
import org.junit.platform.launcher.LauncherSession;
import org.junit.platform.launcher.LauncherSessionListener;

import java.util.Map;

/**
 * nexus-64kwn: in a surefire fork that sets {@link SchemaMigrator#UI_PROPERTY}, enters a Liquibase scope
 * whose UI is a {@link LoggerUIService} on the thread that runs the tests, for the whole session.
 *
 * <p>About 30 test classes and the shared cluster's template build call {@code Liquibase.update} themselves,
 * not through {@link SchemaMigrator#migrate}, and each printed one "Running Changeset" line per changeset.
 * Liquibase's root scope is per thread and always binds a ConsoleUIService, so the scope has to be
 * entered here; a walk on another thread is covered by SchemaMigrator itself when it goes through migrate().
 * The scope is never exited: it lives as long as the fork. Registered in
 * META-INF/services/org.junit.platform.launcher.LauncherSessionListener (test resources only).
 */
public final class LiquibaseLoggerUiSession implements LauncherSessionListener {

    @Override
    public void launcherSessionOpened(LauncherSession session) {
        if (!SchemaMigrator.UI_LOGGER.equals(System.getProperty(SchemaMigrator.UI_PROPERTY))) {
            return;
        }
        try {
            Scope.enter(Map.of(Scope.Attr.ui.name(), new LoggerUIService()));
        } catch (Exception e) {
            throw new IllegalStateException("nexus-64kwn: could not enter the Liquibase logger-UI scope", e);
        }
    }
}
