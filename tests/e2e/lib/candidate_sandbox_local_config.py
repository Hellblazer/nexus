# SPDX-License-Identifier: AGPL-3.0-or-later
"""Write a loopback engine's URL and one tenant's token as sandbox secret files (candidate_sandbox.sh, local mode).

Usage: candidate_sandbox_local_config.py <engine.json> <tenant-index> <secrets-dir>. The token goes engine.json -> a 0600
file; it is never an argument or stdout.
"""
import json
import os
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    doc = json.load(fh)
i = int(sys.argv[2])
out = sys.argv[3]
url = doc["base_url"]
if not url.startswith("http://127.0.0.1:"):
    sys.exit("refusing a non-loopback local engine: " + url.split("//", 1)[-1].split("/")[0].rsplit(":", 1)[0])
tenants = doc.get("tenants") or [{"tenant": doc["tenant"], "token": doc["token"]}]
for name, value in (("NX_SERVICE_URL", url), ("NX_SERVICE_TOKEN", tenants[i]["token"]), ("NX_LOCAL", "1")):
    fd = os.open(os.path.join(out, name), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value + "\n")
