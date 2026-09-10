// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * RDR-205 typed error: {@code subspace} resolves to no registered template
 * (literal-before-template, {@code TemplateRegistry#resolve}).
 */
public final class UnknownSubspaceException extends TupleException {

    private final String subspace;

    public UnknownSubspaceException(String subspace) {
        super("UnknownSubspace", 404,
                "no template registered for subspace '" + subspace + "'");
        this.subspace = subspace;
    }

    public String subspace() {
        return subspace;
    }
}
