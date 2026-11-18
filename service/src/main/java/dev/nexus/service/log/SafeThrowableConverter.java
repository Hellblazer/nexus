// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.log;

import ch.qos.logback.classic.pattern.ThrowableProxyConverter;
import ch.qos.logback.classic.spi.IThrowableProxy;
import ch.qos.logback.classic.spi.StackTraceElementProxy;
import dev.nexus.service.http.LogText;

/**
 * {@code %exSafe}, in place of {@code %ex} (nexus-dngtz.1.11, RDR-167): logback's own trace, with the message of every
 * exception in it (the top one, each cause, each suppressed one) written through {@link LogText#lineSafe}. A message with a
 * line break would otherwise start a new physical line, and PostgreSQL error text echoes stored values. The shape is
 * unchanged: the first line {@code <class>: <message>}, one {@code \tat ...} line per frame, {@code Caused by:},
 * {@code Suppressed:} and the frame counts, each on its own line, so a reader that follows continuation lines still can.
 *
 * <p>It reuses logback's renderer on a view of the throwable whose message is escaped, so the frame logic is logback's.
 * Because it is a {@code ThrowableHandlingConverter}, listing it in the pattern stops logback adding its plain
 * {@code %ex} as well (see {@code EnsureExceptionHandling}), so a trace prints once.
 */
public class SafeThrowableConverter extends ThrowableProxyConverter {

    @Override
    protected String throwableProxyToString(IThrowableProxy proxy) {
        return super.throwableProxyToString(new Safe(proxy));
    }

    /** The throwable with its class name and message escaped, and its causes and suppressed ones likewise. */
    private record Safe(IThrowableProxy delegate) implements IThrowableProxy {

        @Override
        public String getMessage() {
            return LogText.lineSafe(delegate.getMessage());
        }

        @Override
        public String getClassName() {
            return LogText.lineSafe(delegate.getClassName());
        }

        @Override
        public StackTraceElementProxy[] getStackTraceElementProxyArray() {
            return delegate.getStackTraceElementProxyArray();
        }

        @Override
        public int getCommonFrames() {
            return delegate.getCommonFrames();
        }

        @Override
        public IThrowableProxy getCause() {
            IThrowableProxy cause = delegate.getCause();
            return cause == null ? null : new Safe(cause);
        }

        @Override
        public IThrowableProxy[] getSuppressed() {
            IThrowableProxy[] suppressed = delegate.getSuppressed();
            if (suppressed == null) {
                return null;
            }
            IThrowableProxy[] wrapped = new IThrowableProxy[suppressed.length];
            for (int i = 0; i < suppressed.length; i++) {
                wrapped[i] = new Safe(suppressed[i]);
            }
            return wrapped;
        }

        @Override
        public boolean isCyclic() {
            return delegate.isCyclic();
        }
    }
}
