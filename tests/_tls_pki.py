# SPDX-License-Identifier: AGPL-3.0-or-later
"""A throwaway PKI and TLS servers for the nexus-ec37o proxy and CA tests.

``Pki`` makes certificate authorities and a leaf for ``127.0.0.1`` / ``localhost`` signed by one
(``cryptography`` is a runtime dependency of nexus). ``TlsOrigin`` is a port-0 https server that
records every ``Authorization`` header it is sent. ``TerminatingProxy`` is the attack the bead is
about: a plain-TCP proxy that answers ``CONNECT`` with 200 and then terminates TLS with a leaf of
its own CA, reading the request. ``TunnelProxy`` is a legitimate CONNECT proxy that pipes bytes to
the origin without reading them.
"""
from __future__ import annotations

import datetime
import http.server
import ipaddress
import json
import socket
import ssl
import threading
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from nexus.engine_version import REQUIRED_ENGINE_VERSION

ENGINE_VERSION = ".".join(str(part) for part in REQUIRED_ENGINE_VERSION)


def _key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _name(common: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])


def _pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


@dataclass(frozen=True)
class Leaf:
    cert_file: Path
    key_file: Path


@dataclass(frozen=True)
class Ca:
    """A certificate authority; ``pem`` is the bundle to trust it with."""

    name: str
    pem: Path
    _cert: x509.Certificate
    _key: ec.EllipticCurvePrivateKey

    def issue_leaf(self, directory: Path, stem: str) -> Leaf:
        key = _key()
        now = datetime.datetime.now(datetime.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(_name("127.0.0.1"))
            .issuer_name(self._cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                critical=False,
            )
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self._cert.public_key()), critical=False)
            .sign(self._key, hashes.SHA256())
        )
        cert_file, key_file = directory / f"{stem}.pem", directory / f"{stem}.key"
        cert_file.write_bytes(_pem(cert))
        key_file.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        ))
        return Leaf(cert_file, key_file)


def make_ca(directory: Path, name: str) -> Ca:
    key = _key()
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(name))
        .issuer_name(_name(name))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    pem = directory / f"{name}.pem"
    pem.write_bytes(_pem(cert))
    return Ca(name, pem, cert, key)


def server_context(leaf: Leaf) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(leaf.cert_file, leaf.key_file)
    return ctx


def _answer(path: str) -> bytes:
    if "/data-tokens/mint" in path:
        body: object = {"data_token": "minted-data-token", "expires_in_seconds": 600}
    elif path == "/version":
        body = {"app_version": "1.0", "release_version": ENGINE_VERSION}
    elif "/memory/list" in path or "/memory/projects" in path:
        body = []
    else:
        body = {"ok": True}
    return json.dumps(body).encode()


class TlsOrigin:
    """A port-0 https engine stand-in recording ``(path, Authorization)`` of every request."""

    def __init__(self, leaf: Leaf) -> None:
        self.requests: list[tuple[str, str | None]] = []
        outer = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length:
                    self.rfile.read(length)
                outer.requests.append((self.path, self.headers.get("Authorization")))
                payload = _answer(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _serve  # noqa: N815 -- stdlib callback names

            def log_message(self, *_a: object) -> None:
                pass

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.socket = server_context(leaf).wrap_socket(self._httpd.socket, server_side=True)
        self.port = self._httpd.server_address[1]
        self.url = f"https://127.0.0.1:{self.port}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    @property
    def auth(self) -> list[str]:
        return [a for _p, a in self.requests if a is not None]

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _read_head(sock: socket.socket | ssl.SSLSocket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    return data


class _ConnectProxy:
    """A port-0 TCP proxy that takes ``CONNECT host:port``; subclasses decide what happens next."""

    def __init__(self) -> None:
        #: ``CONNECT host:port`` lines, then ``(path, Authorization)`` of requests read inside a tunnel
        self.connects: list[str] = []
        self.requests: list[tuple[str, str | None]] = []
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self.port = self._listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def auth(self) -> list[str]:
        return [a for _p, a in self.requests if a is not None]

    @property
    def touched(self) -> bool:
        """Did anything reach this proxy at all?"""
        return bool(self.connects or self.requests)

    def _accept(self) -> None:
        while not self._closed:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            head = _read_head(conn)
            first = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            self.connects.append(first)
            if not first.startswith("CONNECT "):
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                return
            self._tunnel(conn, first.split()[1])
        except (OSError, ssl.SSLError):
            pass
        finally:
            conn.close()

    def _tunnel(self, conn: socket.socket, target: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        self._closed = True
        self._listener.close()


class TerminatingProxy(_ConnectProxy):
    """Says 200 to a CONNECT, terminates TLS with *leaf*, and reads the request inside: the attack
    (the CA that signed *leaf* is the one a repository points ``SSL_CERT_FILE`` at). It answers the
    request itself, so a caller that trusts it gets a reply and has sent it the bearer."""

    def __init__(self, leaf: Leaf) -> None:
        self._ctx = server_context(leaf)
        super().__init__()

    def _tunnel(self, conn: socket.socket, target: str) -> None:
        conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        with self._ctx.wrap_socket(conn, server_side=True) as tls:
            head = _read_head(tls).decode("latin-1", "replace")
            lines = head.split("\r\n")
            auth = next((line.split(":", 1)[1].strip() for line in lines if line.lower().startswith("authorization:")), None)
            path = lines[0].split()[1] if lines and len(lines[0].split()) > 1 else ""
            self.requests.append((path, auth))
            payload = _answer(path)
            tls.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                + str(len(payload)).encode() + b"\r\n\r\n" + payload,
            )


class TunnelProxy(_ConnectProxy):
    """A legitimate CONNECT proxy: pipes bytes both ways and reads none of them."""

    def _tunnel(self, conn: socket.socket, target: str) -> None:
        host, _, port = target.rpartition(":")
        upstream = socket.create_connection((host, int(port)), timeout=10)
        conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")

        def pipe(src: socket.socket, dst: socket.socket) -> None:
            try:
                while chunk := src.recv(65536):
                    dst.sendall(chunk)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        back = threading.Thread(target=pipe, args=(upstream, conn), daemon=True)
        back.start()
        pipe(conn, upstream)
        back.join(timeout=10)
        upstream.close()
