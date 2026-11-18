// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.log;

import ch.qos.logback.classic.pattern.ClassicConverter;
import ch.qos.logback.classic.spi.ILoggingEvent;
import dev.nexus.service.http.LogText;

/**
 * {@code %threadSafe}, in place of {@code %thread} (nexus-dngtz.1.11, RDR-167): the thread name written through
 * {@link LogText#mdcValue}, so a name that holds a line break, {@code ]}, a space or {@code =} cannot end the bracketed
 * block early or start a line. Every thread the engine names today has a literal name, so a name with none of those
 * characters ({@code main}, {@code t1-ttl-sweep}) prints as it did, and a thread with no name (a plain virtual thread)
 * still prints as an empty block. A name with a space prints the space as {@code \x20}: Hikari's
 * {@code HikariPool-1 housekeeper} prints as {@code HikariPool-1\x20housekeeper}.
 *
 * <p>logback makes this class by name from {@code logback.xml}; the native image lists it in
 * {@code META-INF/native-image/reflect-config.json}.
 */
public class SafeThreadConverter extends ClassicConverter {

    @Override
    public String convert(ILoggingEvent event) {
        String name = event.getThreadName();
        return name == null || name.isEmpty() ? "" : LogText.mdcValue(name);
    }
}
