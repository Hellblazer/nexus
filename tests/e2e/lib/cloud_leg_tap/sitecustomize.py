# SPDX-License-Identifier: AGPL-3.0-or-later
# ruff: noqa: PLC0415
"""The cloud leg's HTTP tap: one line per request the real client sends, at the client boundary.

Put this directory on PYTHONPATH of an ``nx`` (or candidate-python) process and name a file in NX_CLOUDLEG_TAP:
Python imports ``sitecustomize`` at start and this patches the two transports the client uses (``httpx.Client.send``
for the T2, scratch, tuple and identity stores; ``http.client.HTTPConnection`` for the T3 vector client), appending
``<status>\\t<METHOD>\\t<path>\\t<host>`` per exchange. Only requests to the host in NX_CLOUDLEG_HOST are recorded. Never
records a header, a query string or a body, so a bearer or identity token cannot reach the file. A request that
raises records status 0 and the exception's class name as a fifth field.
"""
import os
import threading

_TAP = os.environ.get("NX_CLOUDLEG_TAP", "")
_HOST = os.environ.get("NX_CLOUDLEG_HOST", "")
_LOCK = threading.Lock()


def _rec(status, method, path, host, err=""):
    line = f"{status}\t{method}\t{path.split('?', 1)[0]}\t{host}" + (f"\t{err}" if err else "") + "\n"
    with _LOCK, open(_TAP, "a", encoding="utf-8") as fh:
        fh.write(line)


def _install():
    with open(_TAP, "a", encoding="utf-8") as fh:
        fh.write("#tap-ready\n")
    import http.client as hc

    import httpx

    orig_send = httpx.Client.send

    def send(self, request, *args, **kwargs):
        mine = not _HOST or request.url.host == _HOST
        try:
            resp = orig_send(self, request, *args, **kwargs)
        except BaseException as exc:
            if mine:
                _rec(0, request.method, request.url.path, request.url.host, type(exc).__name__)
            raise
        if mine:
            _rec(resp.status_code, request.method, request.url.path, request.url.host)
        return resp

    httpx.Client.send = send

    orig_put = hc.HTTPConnection.putrequest
    orig_get = hc.HTTPConnection.getresponse

    def putrequest(self, method, url, *args, **kwargs):
        self._cl_tap = (method, url)
        return orig_put(self, method, url, *args, **kwargs)

    def getresponse(self, *args, **kwargs):
        tag = getattr(self, "_cl_tap", None)
        mine = tag is not None and (not _HOST or self.host == _HOST)
        try:
            resp = orig_get(self, *args, **kwargs)
        except BaseException as exc:
            if mine:
                _rec(0, tag[0], tag[1], self.host, type(exc).__name__)
            raise
        if mine:
            _rec(resp.status, tag[0], tag[1], self.host)
        return resp

    hc.HTTPConnection.putrequest = putrequest
    hc.HTTPConnection.getresponse = getresponse


if _TAP:
    try:
        _install()
    except Exception:  # noqa: BLE001 - a tap that cannot install shows as no #tap-ready line, which the leg fails on
        pass
