// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/** RDR-205 typed error: {@code in}/{@code inp} against a template whose {@code take.enabled} is false. */
public final class TakeDisabledException extends TupleException {

    private final String subspace;
    private final String template;

    public TakeDisabledException(String subspace, String template) {
        super("TakeDisabled", 422,
                "template '" + template + "' disables take (subspace '" + subspace + "')");
        this.subspace = subspace;
        this.template = template;
    }

    public String subspace() {
        return subspace;
    }

    public String template() {
        return template;
    }
}
