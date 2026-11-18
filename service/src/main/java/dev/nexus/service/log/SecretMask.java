// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.log;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.LinkedHashSet;
import java.util.Set;

/**
 * The engine's one set of credentials to keep out of its logs, its registry columns and its reports. Set once at startup
 * from the credentials the engine resolved ({@code Main} adds the admin and the application password; the data-move
 * worker's factory adds the admin password it uses, which changes nothing once {@code Main} has), and read by
 * {@link dev.nexus.service.datamove.FailureHandler#oneLine} (every text a failure carries), the data-move events, the
 * state a move is reported with, and the log encoder ({@link LogMasking}), which covers a line and its stack trace.
 *
 * <p>This is process-wide state, because the log encoder and a static text helper have no instance to inject it into. It
 * only grows (a secret added stays), so a text never loses its protection. {@link #reset} is for tests.
 *
 * <p>A secret shorter than {@value #SHORT} characters is still redacted: it also masks any unrelated text that contains
 * it, and one warning says so when it is added.
 */
public final class SecretMask {

    private static final Logger log = LoggerFactory.getLogger(SecretMask.class);

    /** A secret shorter than this makes the warning that redacting it will also mask unrelated text. */
    static final int SHORT = 4;

    private static final Set<String> SECRETS = new LinkedHashSet<>();
    private static volatile SecretRedactor current = SecretRedactor.NONE;

    private SecretMask() { /* static */ }

    /** Adds a secret; a blank one and one already added change nothing. */
    public static synchronized void add(String secret) {
        if (secret == null || secret.isBlank() || SECRETS.contains(secret)) {
            return;
        }
        if (secret.length() < SHORT) {
            log.warn("event=secret_mask_short_secret length={} reason=\"redaction also masks unrelated text that contains it\"",
                secret.length());
        }
        SECRETS.add(secret);
        current = new SecretRedactor(SECRETS);
    }

    /** {@code text} with every added secret replaced by {@value SecretRedactor#REDACTED}. */
    public static String mask(String text) {
        return current.redact(text);
    }

    /** Whether any secret has been added. */
    public static boolean active() {
        return current.active();
    }

    /** Forgets every secret. For tests, which share one JVM. */
    public static synchronized void reset() {
        SECRETS.clear();
        current = SecretRedactor.NONE;
    }
}
