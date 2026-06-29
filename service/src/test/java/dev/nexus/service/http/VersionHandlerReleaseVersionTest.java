// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-002 release_version contract — unit coverage for the fail-closed
 * normalization (no DB / live service required).
 *
 * <p>The release identity is stamped from the git tag at native-build time; a
 * dev / unstamped build must report {@code release_version=null} so an RDR-002
 * version-pin consumer (nx guided-upgrade ez5.4) fail-closes.
 */
class VersionHandlerReleaseVersionTest {

    @Test
    void stampedReleaseIsReturnedVerbatim() {
        assertThat(VersionHandler.normalizeReleaseVersion("0.1.6")).isEqualTo("0.1.6");
        assertThat(VersionHandler.normalizeReleaseVersion("  1.2.3  ")).isEqualTo("1.2.3");
    }

    @Test
    void leadingVPrefixIsStripped() {
        // Symmetry with the Python consumer's parser (code-review L3).
        assertThat(VersionHandler.normalizeReleaseVersion("v0.1.6")).isEqualTo("0.1.6");
        assertThat(VersionHandler.normalizeReleaseVersion("V1.2.3")).isEqualTo("1.2.3");
        assertThat(VersionHandler.normalizeReleaseVersion("v")).isNull();
    }

    @Test
    void blankOrNullFailsClosed() {
        assertThat(VersionHandler.normalizeReleaseVersion(null)).isNull();
        assertThat(VersionHandler.normalizeReleaseVersion("")).isNull();
        assertThat(VersionHandler.normalizeReleaseVersion("   ")).isNull();
    }

    @Test
    void snapshotOrDevQualifierFailsClosed() {
        assertThat(VersionHandler.normalizeReleaseVersion("1.0-SNAPSHOT")).isNull();
        assertThat(VersionHandler.normalizeReleaseVersion("1.0-snapshot")).isNull();
        assertThat(VersionHandler.normalizeReleaseVersion("0.1.6-dev")).isNull();
    }

    @Test
    void unstampedSourceResourceResolvesToNull() {
        // The checked-in release.properties carries a BLANK release_version; under
        // surefire that resource is on the classpath, so a dev build resolves to
        // null — the fail-closed default. (A release build overwrites the line via
        // the engine-service-release workflow stamp step.)
        assertThat(VersionHandler.resolveReleaseVersion()).isNull();
    }
}
