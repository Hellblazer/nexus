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

    // ── nexus-308ph: build_ref, the per-run artifact-identity discriminator ──
    //
    // Unlike release_version (which the /version body ALWAYS emits, using
    // explicit JSON null on a dev/unstamped build), build_ref is OMITTED
    // entirely when unset — a pinned release built before this field existed,
    // and every native-release build (which never stamps it), must produce a
    // byte-identical /version shape. These tests cover both the resolve/
    // normalize logic and the JSON-emission seam directly.

    @Test
    void buildRefBlankOrNullNormalizesToOmitted() {
        assertThat(VersionHandler.normalizeBuildRef(null)).isNull();
        assertThat(VersionHandler.normalizeBuildRef("")).isNull();
        assertThat(VersionHandler.normalizeBuildRef("   ")).isNull();
    }

    @Test
    void buildRefNonBlankReturnedVerbatimTrimmed() {
        // Opaque nonce, not a version string — no v-stripping, no SNAPSHOT/dev
        // filtering (unlike normalizeReleaseVersion): any non-blank value after
        // trimming is significant as-is.
        assertThat(VersionHandler.normalizeBuildRef("a1b2c3d+1690000000-4242"))
            .isEqualTo("a1b2c3d+1690000000-4242");
        assertThat(VersionHandler.normalizeBuildRef("  a1b2c3d+1690000000-4242  "))
            .isEqualTo("a1b2c3d+1690000000-4242");
        assertThat(VersionHandler.normalizeBuildRef("v1.2.3-SNAPSHOT-dev"))
            .isEqualTo("v1.2.3-SNAPSHOT-dev");
    }

    @Test
    void appendBuildRefFieldOmitsWhenNull() {
        var body = new StringBuilder();
        VersionHandler.appendBuildRefField(body, null);
        assertThat(body.toString())
            .as("no build_ref key at all — never \"build_ref\":null")
            .isEmpty();
    }

    @Test
    void appendBuildRefFieldEmitsWhenPresent() {
        var body = new StringBuilder();
        VersionHandler.appendBuildRefField(body, "a1b2c3d+1690000000-4242");
        assertThat(body.toString()).isEqualTo(",\"build_ref\":\"a1b2c3d+1690000000-4242\"");
    }

    @Test
    void unstampedSourceBuildRefResolvesToOmitted() {
        // Mirrors unstampedSourceResourceResolvesToNull: the checked-in
        // release.properties carries a BLANK build_ref, so a dev build (and
        // every native-release build, which never stamps this key) resolves to
        // null — appendBuildRefField then omits the field, keeping a pre-
        // nexus-308ph /version shape byte-identical.
        assertThat(VersionHandler.resolveBuildRef()).isNull();
    }

    // ── nexus-nyry9.9 (RDR-196 .p1c): nx_answer_steps capability advertisement ──

    @Test
    void appendNxAnswerStepsCapabilityFieldAlwaysEmitsTrue() {
        // Unlike build_ref (nullable, omitted when unset), this is a
        // compile-time constant on any engine build carrying this handler —
        // always present, always true. The .p1d client-side capability probe
        // reads "field present and true" as "this engine accepts steps[]".
        var body = new StringBuilder();
        VersionHandler.appendNxAnswerStepsCapabilityField(body);
        assertThat(body.toString()).isEqualTo(",\"nx_answer_steps_supported\":true");
    }
}
