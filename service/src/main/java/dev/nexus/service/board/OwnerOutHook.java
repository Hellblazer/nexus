// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import java.util.Map;

/**
 * Called by {@code TupleHandler} after a tuple write has committed and its response has gone out. An implementation
 * never throws: the write it follows has already happened.
 */
@FunctionalInterface
public interface OwnerOutHook {

    void afterOut(String tenant, String subspace, Map<String, String> dims, String body);

    OwnerOutHook NONE = (tenant, subspace, dims, body) -> { };
}
