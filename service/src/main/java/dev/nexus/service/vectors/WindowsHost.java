// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.Locale;

/** The one place that decides "this is Windows" from {@code os.name} (nexus-f9bgu.27). */
final class WindowsHost {

    private WindowsHost() {}

    /** True when {@code osName} (an {@code os.name} value) names Windows; null is not Windows. */
    static boolean isWindows(String osName) {
        return osName != null && osName.toLowerCase(Locale.ROOT).startsWith("windows");
    }
}
