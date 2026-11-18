// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.log;

import ch.qos.logback.classic.pattern.ClassicConverter;
import ch.qos.logback.classic.spi.ILoggingEvent;
import dev.nexus.service.http.LogText;

/**
 * {@code %msgSafe}, in place of {@code %msg} (nexus-dngtz.1.11, RDR-167): the formatted message with every control character
 * written as an escape ({@link LogText#lineSafe}), so no logged value can start a new physical line. It sits at the sink, so
 * it covers every log call in the engine and every library that logs through logback. A message with nothing to escape is
 * written as it was, the same string.
 *
 * <p>logback makes this class by name from {@code logback.xml}; the native image lists it in
 * {@code META-INF/native-image/reflect-config.json}.
 */
public class SafeMessageConverter extends ClassicConverter {

    @Override
    public String convert(ILoggingEvent event) {
        return LogText.lineSafe(event.getFormattedMessage());
    }
}
