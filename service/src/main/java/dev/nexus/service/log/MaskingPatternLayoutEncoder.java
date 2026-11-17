// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.log;

import ch.qos.logback.classic.encoder.PatternLayoutEncoder;
import ch.qos.logback.classic.spi.ILoggingEvent;

import java.nio.charset.Charset;

/**
 * A {@link PatternLayoutEncoder} that runs {@link SecretMask} over the whole formatted event, message, MDC and stack
 * trace with its causes, before the bytes are written. One pass over a string the layout built anyway; a line that
 * holds no secret costs the scan and nothing else.
 */
public final class MaskingPatternLayoutEncoder extends PatternLayoutEncoder {

    @Override
    public byte[] encode(ILoggingEvent event) {
        String text = SecretMask.mask(layout.doLayout(event));
        Charset charset = getCharset();
        return charset == null ? text.getBytes() : text.getBytes(charset);
    }
}
