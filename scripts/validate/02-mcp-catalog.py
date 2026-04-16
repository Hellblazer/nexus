# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exercise every tool in the nexus-catalog MCP server.

10 tools: catalog_search, catalog_show, catalog_list, catalog_register,
catalog_update, catalog_link, catalog_links, catalog_link_query,
catalog_resolve, catalog_stats.

Populates the sandbox catalog with a minimal fixture (4 entries, 2 links)
before exercising the read/write/graph paths.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from contextlib import contextmanager


_pass: int = 0
_fail: int = 0
_failures: list[tuple[str, str]] = []


def ts() -> str:
    return time.strftime("%H:%M:%S")


def info(msg: str) -> None:
    print(f"[{ts()}]    {msg}", flush=True)


def step(msg: str) -> None:
    print(f"\n[{ts()}] ─── {msg} ───", flush=True)


@contextmanager
def case(name: str):
    global _pass, _fail
    start = time.monotonic()
    try:
        yield
        dur = int((time.monotonic() - start) * 1000)
        print(f"[{ts()}]  ✓ {name}  ({dur} ms)", flush=True)
        _pass += 1
    except AssertionError as exc:
        dur = int((time.monotonic() - start) * 1000)
        print(f"[{ts()}]  ✗ {name}  ({dur} ms) — assertion: {exc}", flush=True)
        _fail += 1
        _failures.append((name, f"assertion: {exc}"))
    except Exception as exc:
        dur = int((time.monotonic() - start) * 1000)
        short = f"{type(exc).__name__}: {exc}"
        print(f"[{ts()}]  ✗ {name}  ({dur} ms) — {short}", flush=True)
        if os.environ.get("NX_VALIDATE_VERBOSE"):
            traceback.print_exc()
        _fail += 1
        _failures.append((name, short))


# ── Fixture setup ────────────────────────────────────────────────────────────

def _setup_catalog_fixture() -> dict[str, str]:
    """Seed the sandbox catalog with a fixture; return a label→tumbler map.

    catalog_register takes an owner (a root tumbler like "1", "2", …) and
    auto-allocates the next available child tumbler under that owner.
    """
    step("Setup — register 4 entries + 2 links")
    from nexus.mcp.catalog import catalog_register, catalog_link

    # First, seed two owner roots so register() has something to allocate under.
    # Owners are registered as top-level tumblers. The catalog auto-creates
    # them on first child registration when the owner isn't known.
    tumblers: dict[str, str] = {}

    with case("register entry 1 (code module under owner 1)"):
        r = catalog_register(
            title="Plan Matcher",
            owner="1",
            content_type="code",
            file_path="src/nexus/plans/matcher.py",
        )
        assert isinstance(r, dict) and "tumbler" in r, r
        tumblers["matcher"] = r["tumbler"]
        info(f"  → {r['tumbler']}")

    with case("register entry 2 (RDR under owner 2)"):
        r = catalog_register(
            title="RDR-078 Plan-Centric Retrieval",
            owner="2",
            content_type="rdr",
            file_path="docs/rdr/rdr-078-plan-centric.md",
        )
        assert "tumbler" in r, r
        tumblers["rdr"] = r["tumbler"]
        info(f"  → {r['tumbler']}")

    with case("register entry 3 (doc under owner 3)"):
        r = catalog_register(
            title="Architecture Guide",
            owner="3",
            content_type="docs",
            file_path="docs/architecture.md",
        )
        assert "tumbler" in r, r
        tumblers["arch"] = r["tumbler"]
        info(f"  → {r['tumbler']}")

    with case("register entry 4 (code under owner 1, 2nd child)"):
        r = catalog_register(
            title="Plan Runner",
            owner="1",
            content_type="code",
            file_path="src/nexus/plans/runner.py",
        )
        assert "tumbler" in r, r
        tumblers["runner"] = r["tumbler"]
        info(f"  → {r['tumbler']}")

    with case("link RDR implements matcher"):
        r = catalog_link(
            from_tumbler=tumblers["rdr"],
            to_tumbler=tumblers["matcher"],
            link_type="implements",
        )
        assert isinstance(r, dict) and "error" not in r, r

    with case("link architecture cites RDR"):
        r = catalog_link(
            from_tumbler=tumblers["arch"],
            to_tumbler=tumblers["rdr"],
            link_type="cites",
        )
        assert isinstance(r, dict) and "error" not in r, r

    return tumblers


# ── Suite ────────────────────────────────────────────────────────────────────

def run_suite() -> None:
    tumblers = _setup_catalog_fixture()

    step("Read paths: list / show / resolve / stats / search")
    from nexus.mcp.catalog import (
        catalog_list, catalog_show, catalog_resolve, catalog_stats, catalog_search,
    )

    with case("catalog_list"):
        r = catalog_list(limit=10)
        assert isinstance(r, list), type(r)
        assert len(r) >= 4, f"expected ≥4 entries, got {len(r)}"
        info(f"  {len(r)} entries listed")

    with case("catalog_show by tumbler"):
        r = catalog_show(tumbler=tumblers["matcher"])
        assert isinstance(r, dict) and "error" not in r, r
        assert r.get("title") == "Plan Matcher", r

    with case("catalog_resolve by owner"):
        r = catalog_resolve(owner="1")
        assert isinstance(r, list), type(r)
        info(f"  resolved owner 1 → {len(r)} physical collections")

    with case("catalog_stats"):
        r = catalog_stats()
        assert isinstance(r, dict)
        info(f"  stats: {r}")

    with case("catalog_search by title"):
        r = catalog_search(query="Plan Matcher", limit=5)
        assert isinstance(r, list)
        info(f"  search → {len(r)} hits")

    step("Link graph paths: links / link_query")
    from nexus.mcp.catalog import catalog_links, catalog_link_query

    with case("catalog_links from RDR seed"):
        r = catalog_links(tumbler=tumblers["rdr"], depth=1)
        assert isinstance(r, dict)
        assert "nodes" in r and "edges" in r, r
        info(f"  {len(r.get('nodes', []))} nodes, {len(r.get('edges', []))} edges")

    with case("catalog_link_query by from_tumbler"):
        r = catalog_link_query(from_tumbler=tumblers["rdr"])
        assert isinstance(r, list)
        info(f"  link rows: {len(r)}")

    step("Mutation: update")
    from nexus.mcp.catalog import catalog_update

    with case("catalog_update rename"):
        r = catalog_update(tumbler=tumblers["matcher"], title="Plan Matcher (renamed)")
        assert isinstance(r, dict) and "error" not in r, r

    with case("catalog_show reflects rename"):
        r = catalog_show(tumbler=tumblers["matcher"])
        assert isinstance(r, dict)
        assert "renamed" in r.get("title", "").lower(), r


def main() -> int:
    print(f"[{ts()}] MCP catalog tools live exercise — catalog={os.environ.get('NEXUS_CATALOG_PATH')}")
    try:
        # Ensure catalog is initialised
        from pathlib import Path
        from nexus.catalog.catalog import Catalog
        cat_path = Path(os.environ["NEXUS_CATALOG_PATH"])
        if not Catalog.is_initialized(cat_path):
            Catalog.init(cat_path)
        run_suite()
    finally:
        print(f"\n[{ts()}] ── mcp-catalog: {_pass} pass, {_fail} fail ──")
        for name, err in _failures:
            print(f"       - {name}: {err}")
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
