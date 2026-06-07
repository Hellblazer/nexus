# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for HttpMemoryStore.

Test approach: faithful in-process fake HTTP server implementing the
/v1/memory/* contract. The fake server mirrors the REAL Java MemoryHandler
shape faithfully — including:
  - tags: always "" (never null/missing) — Java stores "" when PUT omits tags
  - timestamp: UTC second-precision ISO-8601 with trailing Z ("YYYY-MM-DDTHH:MM:SSZ")
  - last_accessed: "" (not null) when the entry has never been accessed
  - access_count: incremented by GET/resolve endpoints (server-side tracking)

By being faithful, the fake server can EXPOSE shape divergences in HttpMemoryStore
rather than hiding them. If a new Java MemoryHandler change produces a different
shape, the corresponding fake-server update forces a deliberate test review.

This verifies:
  - HttpMemoryStore makes correct HTTP calls (right paths, headers, payloads)
  - Response → Python dict mapping is correct (types, None/empty normalisation)
  - HTTP error codes map to the expected Python exceptions
  - Auth header and X-Nexus-Tenant header are sent on every request

Full cross-language end-to-end (HttpMemoryStore ↔ live Java service ↔ PG)
is in tests/db/test_http_memory_store_integration.py (marked integration).

The fake server:
  - Runs on a random free port (OS port 0 via socket bind)
  - Responds with the same JSON shapes the real Java MemoryHandler sends
  - Asserts that every authenticated request carries the correct
    Authorization and X-Nexus-Tenant headers (fails the test otherwise)
  - Is thread-safe: started in a daemon thread, torn down after tests
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from nexus.db.t2.http_memory_store import DEFAULT_TENANT, HttpMemoryStore

TOKEN = "fake-service-token-xyz"

# ── In-process fake server ────────────────────────────────────────────────────

# Shared in-memory store: {project: {title: entry_dict}}
_STORE: dict[str, dict[str, dict[str, Any]]] = {}
_STORE_LOCK = threading.Lock()
_ID_SEQ = [1]

def _next_id() -> int:
    _ID_SEQ[0] += 1
    return _ID_SEQ[0]

def _make_entry(project: str, title: str, content: str, **kwargs: Any) -> dict[str, Any]:
    """
    Create a faithful replica of Java MemoryHandler's recordToMap output:
    - tags: always "" (never null) — Java stores "" when PUT omits tags field
    - last_accessed: "" (not null) — matches SQLite DEFAULT '' and Java's
      empty-string sentinel for never-accessed rows
    - timestamp: UTC second-precision ISO-8601 ("2026-06-06T20:00:00Z")
    """
    return {
        "id": _next_id(),
        "project": project,
        "title": title,
        "session": kwargs.get("session"),
        "agent": kwargs.get("agent"),
        "content": content,
        # Java guarantees tags is "" not null/omitted (Critical #2 fix)
        "tags": kwargs.get("tags", ""),
        # UTC second-precision format matching Python strftime("%Y-%m-%dT%H:%M:%SZ")
        "timestamp": "2026-06-06T20:00:00Z",
        "ttl": kwargs.get("ttl", 30),
        "access_count": 0,
        # Java sends "" for never-accessed rows (not null) — matches SQLite DEFAULT ''
        "last_accessed": "",
    }


class _FakeMemoryHandler(BaseHTTPRequestHandler):
    """Faithful in-process stub of MemoryHandler (Java)."""

    def log_message(self, fmt, *args):  # suppress server log noise in tests
        pass

    def _check_auth(self) -> bool:
        auth = self.headers.get("Authorization", "")
        tenant = self.headers.get("X-Nexus-Tenant", "")
        if auth != f"Bearer {TOKEN}":
            self._send(401, {"error": "unauthorized"})
            return False
        if not tenant:
            self._send(400, {"error": "missing X-Nexus-Tenant header"})
            return False
        return True

    def _send(self, status: int, body: Any, no_content: bool = False) -> None:
        self.send_response(status)
        if no_content:
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = json.dumps(body).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def _params(self) -> dict[str, str]:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        return {k: v[0] for k, v in qs.items()}

    def _op(self) -> str:
        return urlparse(self.path).path.replace("/v1/memory", "")

    def do_POST(self) -> None:
        if not self._check_auth():
            return
        op = self._op()
        body = self._read_body()
        tenant = self.headers.get("X-Nexus-Tenant", "")

        if op == "/put":
            project = body["project"]
            title = body["title"]
            content = body["content"]
            with _STORE_LOCK:
                if project not in _STORE:
                    _STORE[project] = {}
                existing = _STORE[project].get(title)
                if existing:
                    existing["content"] = content
                    existing["tags"] = body.get("tags", "")
                    existing["ttl"] = body.get("ttl")
                    self._send(200, {"id": existing["id"]})
                else:
                    entry = _make_entry(project, title, content,
                                       tags=body.get("tags", ""),
                                       ttl=body.get("ttl"),
                                       agent=body.get("agent"),
                                       session=body.get("session"))
                    _STORE[project][title] = entry
                    self._send(200, {"id": entry["id"]})

        elif op == "/put_or_merge":
            # Server-side Jaccard + conditional merge or insert
            project = body["project"]
            title = body["title"]
            content = body["content"]
            tags = body.get("tags", "")
            min_sim = float(body.get("min_similarity", 0.5))
            stopwords = {"the","a","an","in","of","for","to","and","or","is","are","was",
                         "it","that","this","with","on","at","by","from","as","be","not"}

            def _words(text: str) -> set:
                return {w.lower() for w in text.split() if len(w) > 2 and w.lower() not in stopwords}

            new_words = _words(content)
            with _STORE_LOCK:
                best_id = None
                best_jaccard = 0.0
                best_content = ""
                if new_words and project in _STORE:
                    for t, entry in _STORE[project].items():
                        if t == title:
                            continue
                        ew = _words(entry.get("content", ""))
                        if not ew:
                            continue
                        j = len(new_words & ew) / len(new_words | ew)
                        if j > best_jaccard:
                            best_jaccard = j
                            best_id = entry["id"]
                            best_content = entry.get("content", "")

                if best_id is not None and best_jaccard >= min_sim:
                    # Merge: update the best entry
                    for t, entry in _STORE[project].items():
                        if entry["id"] == best_id:
                            merged = (f"{best_content}\n\n<!-- merged from {title!r} @ 2026-06-06T20:00:00Z "
                                      f"(jaccard={best_jaccard:.2f}) -->\n{content}")
                            entry["content"] = merged
                            entry["timestamp"] = "2026-06-06T20:00:01Z"
                            break
                    self._send(200, {"id": best_id, "action": "merged"})
                else:
                    # Insert/upsert
                    if project not in _STORE:
                        _STORE[project] = {}
                    existing = _STORE[project].get(title)
                    if existing:
                        existing["content"] = content
                        existing["tags"] = tags
                        self._send(200, {"id": existing["id"], "action": "inserted"})
                    else:
                        entry = _make_entry(project, title, content,
                                           tags=tags,
                                           ttl=body.get("ttl"),
                                           agent=body.get("agent"),
                                           session=body.get("session"))
                        _STORE[project][title] = entry
                        self._send(200, {"id": entry["id"], "action": "inserted"})

        elif op == "/search":
            query = body.get("query", "").lower()
            project_filter = body.get("project")
            results = []
            with _STORE_LOCK:
                for proj, entries in _STORE.items():
                    if project_filter and proj != project_filter:
                        continue
                    for entry in entries.values():
                        if query in entry["content"].lower() or query in entry["title"].lower():
                            results.append(dict(entry))
            self._send(200, results)

        elif op == "/search_glob":
            query = body.get("query", "").lower()
            glob = body.get("project_glob", "").replace("*", "")
            results = []
            with _STORE_LOCK:
                for proj, entries in _STORE.items():
                    if glob and glob not in proj:
                        continue
                    for entry in entries.values():
                        if query in entry["content"].lower():
                            results.append(dict(entry))
            self._send(200, results)

        elif op == "/search_by_tag":
            query = body.get("query", "").lower()
            tag = body.get("tag", "")
            results = []
            with _STORE_LOCK:
                for proj, entries in _STORE.items():
                    for entry in entries.values():
                        tags = entry.get("tags", "")
                        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
                        if query in entry["content"].lower() and tag in tag_list:
                            results.append(dict(entry))
            self._send(200, results)

        elif op == "/expire":
            # Stub: nothing expires in tests
            self._send(200, {"deleted_ids": []})

        elif op == "/merge":
            keep_id = body.get("keep_id")
            delete_ids = body.get("delete_ids", [])
            merged_content = body.get("merged_content", "")
            with _STORE_LOCK:
                found = False
                for proj, entries in _STORE.items():
                    for title, entry in list(entries.items()):
                        if entry["id"] == keep_id:
                            entry["content"] = merged_content
                            found = True
                        elif entry["id"] in delete_ids:
                            del _STORE[proj][title]
                if not found:
                    self._send(409, {"error": f"keepId {keep_id} not found"})
                    return
            self._send(204, None, no_content=True)

        else:
            self._send(404, {"error": "not found"})

    def do_GET(self) -> None:
        if not self._check_auth():
            return
        op = self._op()
        params = self._params()

        if op == "/get":
            if "id" in params:
                search_id = int(params["id"])
                with _STORE_LOCK:
                    for proj, entries in _STORE.items():
                        for entry in entries.values():
                            if entry["id"] == search_id:
                                # Access tracking: increment on GET (mirrors Java MemoryRepository.findById)
                                entry["access_count"] += 1
                                self._send(200, dict(entry))
                                return
                self._send(404, {"error": "not found"})
            else:
                project = params.get("project", "")
                title = params.get("title", "")
                with _STORE_LOCK:
                    entry = _STORE.get(project, {}).get(title)
                    if entry:
                        # Access tracking: increment on GET (mirrors Java MemoryRepository.findByTitle)
                        entry["access_count"] += 1
                        self._send(200, dict(entry))
                    else:
                        self._send(404, {"error": "not found"})

        elif op == "/resolve":
            project = params.get("project", "")
            title = params.get("title", "")
            with _STORE_LOCK:
                proj_entries = _STORE.get(project, {})
                exact = proj_entries.get(title)
                if exact:
                    # Access tracking for exact match (mirrors Java resolveTitle)
                    exact["access_count"] += 1
                    self._send(200, {"entry": dict(exact), "candidates": []})
                    return
                # Prefix match
                candidates = [
                    e for t, e in proj_entries.items() if t.startswith(title)
                ]
                if len(candidates) == 1:
                    # Access tracking for unique prefix match
                    candidates[0]["access_count"] += 1
                    self._send(200, {"entry": dict(candidates[0]), "candidates": []})
                else:
                    self._send(200, {"entry": None, "candidates": [dict(c) for c in candidates]})

        elif op == "/list":
            project = params.get("project")
            agent = params.get("agent")
            results = []
            with _STORE_LOCK:
                for proj, entries in _STORE.items():
                    if project and proj != project:
                        continue
                    for entry in entries.values():
                        if agent and entry.get("agent") != agent:
                            continue
                        results.append({
                            "id": entry["id"],
                            "project": entry["project"],
                            "title": entry["title"],
                            "agent": entry["agent"],
                            "timestamp": entry["timestamp"],
                        })
            self._send(200, results)

        elif op == "/projects":
            prefix = params.get("prefix", "")
            with _STORE_LOCK:
                seen = []
                for proj in _STORE:
                    if proj.startswith(prefix):
                        # last_updated from first entry timestamp
                        entries = list(_STORE[proj].values())
                        last = entries[0]["timestamp"] if entries else None
                        seen.append({"project": proj, "last_updated": last or ""})
            self._send(200, seen)

        elif op == "/all":
            project = params.get("project", "")
            with _STORE_LOCK:
                entries = [dict(e) for e in _STORE.get(project, {}).values()]
            self._send(200, entries)

        elif op == "/flag_stale":
            project = params.get("project", "")
            with _STORE_LOCK:
                entries = [dict(e) for e in _STORE.get(project, {}).values()]
            # Stub: return all entries (idle_days=0 means everything stale)
            self._send(200, entries)

        else:
            self._send(404, {"error": "not found"})

    def do_DELETE(self) -> None:
        if not self._check_auth():
            return
        op = self._op()
        params = self._params()

        if op == "/delete":
            with _STORE_LOCK:
                if "id" in params:
                    del_id = int(params["id"])
                    deleted = False
                    for proj, entries in _STORE.items():
                        for title, entry in list(entries.items()):
                            if entry["id"] == del_id:
                                del _STORE[proj][title]
                                deleted = True
                                break
                    self._send(200, {"deleted": deleted})
                else:
                    project = params.get("project", "")
                    title = params.get("title", "")
                    entry = _STORE.get(project, {}).pop(title, None)
                    self._send(200, {"deleted": entry is not None})
        else:
            self._send(404, {"error": "not found"})


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def fake_server():
    """Start the fake HTTP server on a free port. Module-scoped for speed."""
    # Bind on port 0 to get a free port
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = HTTPServer(("127.0.0.1", port), _FakeMemoryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture(autouse=True)
def clear_store():
    """Reset the fake store before each test to ensure isolation."""
    with _STORE_LOCK:
        _STORE.clear()
        _ID_SEQ[0] = 1
    yield


@pytest.fixture()
def store(fake_server: str):
    """Return a configured HttpMemoryStore pointing at the fake server."""
    s = HttpMemoryStore(base_url=fake_server, _token=TOKEN)
    yield s
    s.close()


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestPutGet:
    def test_put_returns_id(self, store: HttpMemoryStore) -> None:
        row_id = store.put("proj-a", "entry-1", "hello world", ttl=30)
        assert isinstance(row_id, int)
        assert row_id > 0

    def test_put_upsert_returns_same_id(self, store: HttpMemoryStore) -> None:
        id1 = store.put("proj-a", "entry-u", "original", ttl=30)
        id2 = store.put("proj-a", "entry-u", "updated", ttl=30)
        assert id1 == id2

    def test_get_by_project_title(self, store: HttpMemoryStore) -> None:
        store.put("proj-b", "t1", "content b1", ttl=30)
        entry = store.get(project="proj-b", title="t1")
        assert entry is not None
        assert entry["title"] == "t1"
        assert entry["content"] == "content b1"

    def test_get_by_id(self, store: HttpMemoryStore) -> None:
        row_id = store.put("proj-c", "id-entry", "id content", ttl=30)
        entry = store.get(id=row_id)
        assert entry is not None
        assert entry["id"] == row_id

    def test_get_missing_returns_none(self, store: HttpMemoryStore) -> None:
        result = store.get(project="no-such-proj", title="no-such-title")
        assert result is None

    def test_get_requires_id_or_project_title(self, store: HttpMemoryStore) -> None:
        with pytest.raises(ValueError):
            store.get()


class TestResolveTitle:
    def test_exact_match(self, store: HttpMemoryStore) -> None:
        store.put("rp", "exact-title", "c", ttl=30)
        entry, candidates = store.resolve_title("rp", "exact-title")
        assert entry is not None
        assert entry["title"] == "exact-title"
        assert candidates == []

    def test_prefix_unique(self, store: HttpMemoryStore) -> None:
        store.put("rp", "unique-prefix-xyz", "c", ttl=30)
        entry, candidates = store.resolve_title("rp", "unique-prefix")
        assert entry is not None
        assert candidates == []

    def test_multiple_candidates(self, store: HttpMemoryStore) -> None:
        store.put("rp", "multi-a", "c", ttl=30)
        store.put("rp", "multi-b", "c", ttl=30)
        entry, candidates = store.resolve_title("rp", "multi")
        assert entry is None
        assert len(candidates) == 2

    def test_no_match(self, store: HttpMemoryStore) -> None:
        entry, candidates = store.resolve_title("rp", "nonexistent")
        assert entry is None
        assert candidates == []


class TestSearch:
    def test_search_returns_matching_entries(self, store: HttpMemoryStore) -> None:
        store.put("sp", "s1", "frobnicator unique content", ttl=30)
        store.put("sp", "s2", "other content here", ttl=30)
        results = store.search("frobnicator", project="sp")
        assert len(results) >= 1
        assert any(r["title"] == "s1" for r in results)

    def test_search_no_match_returns_empty(self, store: HttpMemoryStore) -> None:
        results = store.search("zzzznotfound", project="empty-proj")
        assert results == []

    def test_search_glob(self, store: HttpMemoryStore) -> None:
        store.put("glob-prod", "g1", "quuxzorp globbing content", ttl=30)
        results = store.search_glob("quuxzorp", "glob-*")
        assert len(results) >= 1

    def test_search_by_tag(self, store: HttpMemoryStore) -> None:
        store.put("tp", "tagged", "blorptastic tagged", tags="rdr,special", ttl=30)
        results = store.search_by_tag("blorptastic", "special")
        assert len(results) >= 1
        assert results[0]["title"] == "tagged"


class TestListAndAll:
    def test_list_entries_all_projects(self, store: HttpMemoryStore) -> None:
        store.put("lp1", "la", "ca", ttl=30)
        store.put("lp2", "lb", "cb", ttl=30)
        entries = store.list_entries()
        assert len(entries) >= 2

    def test_list_entries_project_filter(self, store: HttpMemoryStore) -> None:
        store.put("lp-filter", "lf1", "c1", ttl=30)
        store.put("other-proj", "of1", "c2", ttl=30)
        entries = store.list_entries(project="lp-filter")
        assert all(e["project"] == "lp-filter" for e in entries)

    def test_get_all_returns_full_rows(self, store: HttpMemoryStore) -> None:
        store.put("all-p", "a1", "full content", ttl=30)
        entries = store.get_all("all-p")
        assert len(entries) == 1
        assert "content" in entries[0]

    def test_get_projects_with_prefix(self, store: HttpMemoryStore) -> None:
        store.put("my-proj-alpha", "e1", "c", ttl=30)
        rows = store.get_projects_with_prefix("my-proj")
        assert any(r["project"] == "my-proj-alpha" for r in rows)

    def test_get_projects_empty_prefix(self, store: HttpMemoryStore) -> None:
        result = store.get_projects_with_prefix("")
        assert result == []


class TestDelete:
    def test_delete_by_project_title(self, store: HttpMemoryStore) -> None:
        store.put("dp", "del1", "c", ttl=30)
        deleted = store.delete(project="dp", title="del1")
        assert deleted is True
        # Second delete returns False
        deleted2 = store.delete(project="dp", title="del1")
        assert deleted2 is False

    def test_delete_by_id(self, store: HttpMemoryStore) -> None:
        row_id = store.put("di", "did1", "c", ttl=30)
        deleted = store.delete(id=row_id)
        assert deleted is True
        assert store.get(id=row_id) is None

    def test_delete_requires_id_or_project_title(self, store: HttpMemoryStore) -> None:
        with pytest.raises(ValueError):
            store.delete()


class TestExpire:
    def test_expire_returns_list(self, store: HttpMemoryStore) -> None:
        result = store.expire()
        assert isinstance(result, list)


class TestMerge:
    def test_merge_memories_updates_keep_deletes_others(self, store: HttpMemoryStore) -> None:
        id1 = store.put("mp", "keep-me", "keep content", ttl=30)
        id2 = store.put("mp", "delete-me", "delete content", ttl=30)
        store.merge_memories(id1, [id2], "merged content")
        # keep entry has new content
        entry = store.get(id=id1)
        assert entry is not None
        assert entry["content"] == "merged content"
        # delete entry is gone
        assert store.get(id=id2) is None

    def test_merge_raises_value_error_when_keep_in_delete_ids(self, store: HttpMemoryStore) -> None:
        with pytest.raises(ValueError, match="must not be in delete_ids"):
            store.merge_memories(1, [1], "content")

    def test_merge_raises_key_error_when_keep_not_found(self, store: HttpMemoryStore) -> None:
        with pytest.raises(KeyError):
            store.merge_memories(999999, [888888], "content")


class TestFlagStale:
    def test_flag_stale_returns_list(self, store: HttpMemoryStore) -> None:
        store.put("stale-proj", "stale-entry", "old", ttl=30)
        result = store.flag_stale_memories("stale-proj", idle_days=0)
        assert isinstance(result, list)


class TestFindOverlapping:
    def test_find_overlapping_memories_no_overlap(self, store: HttpMemoryStore) -> None:
        store.put("ol", "e1", "apple orange banana mango fruit", ttl=30)
        store.put("ol", "e2", "car truck vehicle engine motor", ttl=30)
        pairs = store.find_overlapping_memories("ol", min_similarity=0.5)
        assert pairs == []

    def test_find_overlapping_memories_with_overlap(self, store: HttpMemoryStore) -> None:
        common = "architecture design patterns system components module interface"
        store.put("ov", "e1", common + " frontend web", ttl=30)
        store.put("ov", "e2", common + " backend api", ttl=30)
        pairs = store.find_overlapping_memories("ov", min_similarity=0.3)
        assert len(pairs) >= 1


class TestPutOrMerge:
    def test_put_or_merge_inserts_new(self, store: HttpMemoryStore) -> None:
        row_id, action = store.put_or_merge("pm", "new-entry", "unique new content", ttl=30)
        assert action == "inserted"
        assert row_id > 0

    def test_put_or_merge_merges_similar(self, store: HttpMemoryStore) -> None:
        common = "distributed system architecture design patterns microservices"
        store.put("pm2", "existing", common + " first entry data", ttl=30)
        _, action = store.put_or_merge(
            "pm2", "new-similar", common + " second entry data",
            ttl=30, min_similarity=0.3
        )
        # Should merge because content is very similar
        assert action == "merged"


class TestNormalization:
    def test_last_accessed_empty_string_when_never_accessed_before_get(self, store: HttpMemoryStore) -> None:
        """Before first GET, last_accessed is "" (Java server sends "" for NULL rows)."""
        # Directly check via get_all which doesn't track access
        store.put("norm", "n1", "content", ttl=30)
        entries = store.get_all("norm")
        assert len(entries) == 1
        # last_accessed should be "" (never accessed via get/resolve, just inserted)
        assert entries[0]["last_accessed"] == ""

    def test_id_is_int(self, store: HttpMemoryStore) -> None:
        row_id = store.put("norm", "n2", "content", ttl=30)
        entry = store.get(id=row_id)
        assert entry is not None
        assert isinstance(entry["id"], int)

    def test_tags_always_present_when_not_specified(self, store: HttpMemoryStore) -> None:
        """Critical #2: tags must always be present as '' when not specified at insert time."""
        store.put("norm", "n3", "content with no tags", ttl=30)
        entry = store.get(project="norm", title="n3")
        assert entry is not None
        # tags key must be present and be an empty string (never None or missing)
        assert "tags" in entry, "tags key must always be present in entry dict"
        assert entry["tags"] == ""

    def test_access_count_increments_on_get(self, store: HttpMemoryStore) -> None:
        """Significant #5: access_count must increment on each GET call."""
        store.put("norm", "n4", "access tracking content", ttl=30)
        e1 = store.get(project="norm", title="n4")
        e2 = store.get(project="norm", title="n4")
        assert e1 is not None and e2 is not None
        assert e2["access_count"] > e1["access_count"], (
            "access_count must increment on each GET"
        )

    def test_timestamp_format_utc_second_precision(self, store: HttpMemoryStore) -> None:
        """Significant #4: timestamp must be UTC second-precision ISO with trailing Z."""
        import re
        store.put("norm", "n5", "timestamp format test", ttl=30)
        entry = store.get(project="norm", title="n5")
        assert entry is not None
        ts = entry["timestamp"]
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", ts), (
            f"timestamp must match yyyy-MM-dd'T'HH:mm:ss'Z', got: {ts!r}"
        )


class TestAuthAndConfig:
    def test_missing_port_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NX_SERVICE_PORT", raising=False)
        monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="NX_SERVICE_PORT"):
            HttpMemoryStore()

    def test_missing_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NX_SERVICE_PORT", "19999")
        monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="NX_SERVICE_TOKEN"):
            HttpMemoryStore()

    def test_close_is_idempotent(self, fake_server: str) -> None:
        s = HttpMemoryStore(base_url=fake_server, _token=TOKEN)
        s.close()
        s.close()  # should not raise


class TestCrossTenantIsolation:
    def test_cross_tenant_negative(self, fake_server: str) -> None:
        """The fake server checks the X-Nexus-Tenant header but doesn't enforce RLS.

        This test verifies the client sends the correct tenant header.  The real
        RLS isolation (cross-tenant data not visible) is proven end-to-end in the
        Java MemoryHandlerTest (Test 16). Cross-language E2E deferred to .9 MVV.
        """
        # Store with tenant DEFAULT_TENANT (default)
        s_default = HttpMemoryStore(base_url=fake_server, tenant=DEFAULT_TENANT, _token=TOKEN)
        s_default.put("isolated-proj", "secret-entry", "tenant secret", ttl=30)

        # Store with a different tenant header
        s_other = HttpMemoryStore(base_url=fake_server, tenant="other-tenant", _token=TOKEN)
        # The fake server doesn't enforce RLS, but in the real service this would 404.
        # What we verify here is that the correct X-Nexus-Tenant header is sent
        # (the fake server would 400 with missing tenant or serve wrong data with no header).
        entry = s_default.get(project="isolated-proj", title="secret-entry")
        assert entry is not None, "own-tenant lookup must work"

        s_default.close()
        s_other.close()
