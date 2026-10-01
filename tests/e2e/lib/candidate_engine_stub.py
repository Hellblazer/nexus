# SPDX-License-Identifier: AGPL-3.0-or-later
"""Loopback stub engine for candidate_engine_test.sh (nexus-0kmat). Stdlib only.

``python3 candidate_engine_stub.py <workdir>`` serves ``/version`` and ``/v1/status`` on an
ephemeral port, writes the port to ``<workdir>/stub.port``, and re-reads
``<workdir>/stub.status.json`` on every request when it exists, so a test can change the
counters between calls. A sibling file rather than a heredoc in the test: a heredoc body over
512 bytes deadlocks under bash 5.3 when the kernel shrinks pipes (tests/hooks/
test_heredoc_pipe_budget.py).
"""
import http.server
import json
import os
import sys

WORKDIR = sys.argv[1]
DEFAULT_STATUS = {
    "ownerless_write_mode": "enforce",
    "ownerless_writes_refused_total": 0,
    "ownerless_writes_would_refuse_total": 0,
}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/version":
            body = {"release_version": "0.1.142", "build_ref": "abc1234+99"}
        elif self.path == "/v1/status":
            override = os.path.join(WORKDIR, "stub.status.json")
            body = json.load(open(override)) if os.path.exists(override) else DEFAULT_STATUS
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
open(os.path.join(WORKDIR, "stub.port"), "w").write(str(server.server_address[1]))
server.serve_forever()
