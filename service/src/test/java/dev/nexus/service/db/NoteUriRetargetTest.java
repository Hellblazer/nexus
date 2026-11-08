// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/** nexus-bivz3.24: the string helpers of the stale-note repair. */
class NoteUriRetargetTest {

    @Test
    void retarget_filesTheNoteUnderTheNewCollection_inTheStoredSpelling() {
        assertThat(NoteUri.retarget("nx-note://k__a/t/u", "k__a", "k__b")).isEqualTo("nx-note://k__b/t/u");
        assertThat(NoteUri.retarget("chroma://k__a/t", "k__a", "k__b")).isEqualTo("nx-note://k__b/t");
    }

    @Test
    void retarget_leavesEverythingElseAlone() {
        assertThat(NoteUri.retarget("nx-note://k__other/t", "k__a", "k__b")).isEqualTo("nx-note://k__other/t");
        assertThat(NoteUri.retarget("nx-note://k__ab/t", "k__a", "k__b")).as("a prefix of the name is not the name")
            .isEqualTo("nx-note://k__ab/t");
        assertThat(NoteUri.retarget("file:///k__a/t", "k__a", "k__b")).isEqualTo("file:///k__a/t");
        assertThat(NoteUri.retarget(null, "k__a", "k__b")).isNull();
    }

    @Test
    void collectionOf_readsEitherSpelling_andNothingElse() {
        assertThat(NoteUri.collectionOf("nx-note://k__a/t")).isEqualTo("k__a");
        assertThat(NoteUri.collectionOf("chroma://k__a/t")).isEqualTo("k__a");
        assertThat(NoteUri.collectionOf("nx-note://k__a")).as("no title part").isEqualTo("k__a");
        assertThat(NoteUri.collectionOf("nx-note:///t")).isEqualTo("");
        assertThat(NoteUri.collectionOf("nxexp://k__a/nx-note://k__b/t")).isNull();
        assertThat(NoteUri.collectionOf("https://example.org/a")).isNull();
        assertThat(NoteUri.collectionOf(null)).isNull();
    }

    @Test
    void isFileRoutedCollection_matchesTheSharedPrefixList() {
        for (String p : NoteUri.FILE_ROUTED_PREFIXES) {
            assertThat(NoteUri.isFileRoutedCollection(p + "x__m__v1")).as(p).isTrue();
        }
        assertThat(NoteUri.isFileRoutedCollection("knowledge__docs__m__v1")).isFalse();
        assertThat(NoteUri.isFileRoutedCollection(null)).isFalse();
    }
}
