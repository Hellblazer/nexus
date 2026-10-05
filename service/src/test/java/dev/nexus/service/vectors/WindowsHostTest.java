// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/** nexus-f9bgu.27 (review m10): the one Windows test shared by OrtInitGate and OrtTempSweep. */
class WindowsHostTest {

    @Test
    void windowsIsRecognisedFromTheOsName() {
        assertThat(WindowsHost.isWindows("Windows 11")).isTrue();
        assertThat(WindowsHost.isWindows("Windows Server 2022")).isTrue();
        assertThat(WindowsHost.isWindows("WINDOWS 10")).isTrue();
        assertThat(WindowsHost.isWindows("Mac OS X")).isFalse();
        assertThat(WindowsHost.isWindows("Linux")).isFalse();
        assertThat(WindowsHost.isWindows(null)).isFalse();
    }
}
