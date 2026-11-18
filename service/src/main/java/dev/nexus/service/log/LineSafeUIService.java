// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.log;

import dev.nexus.service.http.LogText;
import liquibase.ui.ConsoleUIService;

/**
 * Liquibase's console UI with every message written through {@link LogText#lineSafe} (nexus-dngtz.1.11, RDR-167). When a
 * changeset fails, Liquibase sends {@code ERROR: Exception Primary Reason:  <the PostgreSQL error text>} through its UI, to
 * stdout, and PostgreSQL echoes stored values in that text. The "Running Changeset: ..." lines the supervisor reads are the
 * ids of the engine's own changelog files and have nothing to escape, so they print as they always did.
 *
 * <p>A stack trace is never printed: {@link #getOutputStackTraces} is pinned to {@code false}. Liquibase's own default is
 * false and nothing in the engine turns it on, but the trace {@code ConsoleUIService} prints would carry each exception's
 * message raw, so the pin keeps a later {@code setOutputStackTraces(true)} from opening that path.
 *
 * <p>{@code SchemaMigrator.update} runs the walk under a scope that binds it. It is made with {@code new}, so it needs no
 * reflection entry in the native image.
 */
public class LineSafeUIService extends ConsoleUIService {

    @Override
    public boolean getOutputStackTraces() {
        return false;
    }

    @Override
    public void sendMessage(String message) {
        super.sendMessage(LogText.lineSafe(message));
    }

    @Override
    public void sendErrorMessage(String message) {
        super.sendErrorMessage(LogText.lineSafe(message));
    }

    @Override
    public void sendErrorMessage(String message, Throwable exception) {
        super.sendErrorMessage(LogText.lineSafe(message), exception);
    }
}
