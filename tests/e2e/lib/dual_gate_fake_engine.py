#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A canned engine for tests/e2e/dual_embedder_gate_guard_test.sh (nexus-clvut).

Serves ``<METHOD>_<path with / as _>.json`` from SCENARIO_DIR for each request, with
the status in the matching ``.code`` file (200 when only the body exists, 404 when
neither does) and a ``Retry-After`` header from the matching ``.retry_after`` file.
The directory is read on every request, so the test changes the scenario between
cases by rewriting files. Every request is appended to
REQUEST_LOG as ``METHOD PATH``. Binds 127.0.0.1 port 0 and writes the port to
PORT_FILE once it is listening.

Usage: dual_gate_fake_engine.py SCENARIO_DIR PORT_FILE REQUEST_LOG
"""
from __future__ import annotations

import pathlib
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCENARIO = pathlib.Path(sys.argv[1])
PORT_FILE = pathlib.Path(sys.argv[2])
REQUEST_LOG = pathlib.Path(sys.argv[3])


class _Canned(BaseHTTPRequestHandler):
    def _serve(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        with REQUEST_LOG.open("a", encoding="utf-8") as log:
            log.write(f"{self.command} {self.path}\n")
        key = f"{self.command}_{self.path.strip('/').replace('/', '_')}"
        body_file = SCENARIO / f"{key}.json"
        code_file = SCENARIO / f"{key}.code"
        if code_file.exists():
            code = int(code_file.read_text().strip())
        else:
            code = 200 if body_file.exists() else 404
        body = body_file.read_bytes() if body_file.exists() else b'{"error": "no canned body"}'
        retry_after_file = SCENARIO / f"{key}.retry_after"
        self.send_response(code)
        if retry_after_file.exists():
            self.send_header("Retry-After", retry_after_file.read_text().strip())
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _serve
    do_POST = _serve

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 -- BaseHTTPRequestHandler's signature
        return


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Canned)
    tmp = PORT_FILE.with_suffix(".tmp")
    tmp.write_text(str(server.server_address[1]), encoding="utf-8")
    tmp.replace(PORT_FILE)
    server.serve_forever()


if __name__ == "__main__":
    main()
