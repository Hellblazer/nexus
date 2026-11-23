// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.DSLContext;

import java.util.List;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25): {@link ByIdRestoreSupport#dropRegistryNameKeys(DSLContext)} for the tests of the
 * {@code vectors} package (the class is package-private here). The tests of the Pci* readers rename a registry row in place,
 * which the name keys onto the registry refuse until they are dropped, as P3.7 drops them.
 */
public final class VecPciRegistryNames {

    private VecPciRegistryNames() { }

    /** Drops every foreign key onto the registry's name; returns {@code table.constraint} of each. */
    public static List<String> dropRegistryNameKeys(DSLContext su) {
        return ByIdRestoreSupport.dropRegistryNameKeys(su);
    }
}
