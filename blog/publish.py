#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "beautifulsoup4>=4.12",
# ]
# ///
"""Publish a markdown blog post to WordPress.com as native Gutenberg blocks.

Why this exists
---------------
Pasting plain HTML into the WP.com block editor lands every element as a
generic Paragraph block — code blocks become text, tables vanish, headings
need to be re-typed. Gutenberg only honours the right block types when the
HTML carries `<!-- wp:NAME -->` comment markers around each top-level
element. This script converts markdown → block-wrapped HTML → SSH+wp-cli on
the WP.com host in one shot.

Why SSH+wp-cli, not REST
------------------------
WP.com's /wp/v2/ REST proxy doesn't accept Application Passwords with Basic
Auth for write operations (returns "rest_cannot_create"). The only working
non-OAuth2 path on WP.com Atomic hosting is SSH+wp-cli, which is what this
script uses.

Setup (one-time)
----------------
1. Site dashboard → Settings → SFTP/SSH → toggle "Enable SSH access" ON.
2. Add an SSH key at wordpress.com/me/security/ssh-keys, then back on the
   site's SFTP/SSH panel select it from the dropdown and click
   "Attach SSH key to site".
3. Save config at ~/.config/nexus-blog/config.json:
     {
       "site": "tensegrity.blog",
       "ssh_user": "tensegritydotblog.wordpress.com",
       "ssh_host": "ssh.wp.com",
       "ssh_key": "~/.ssh/id_ed25519"
     }
4. pandoc must be on PATH.

Usage
-----
  publish.py POST.md --print-html              # dry-run: dump converted HTML
  publish.py POST.md --draft                   # create new draft, print id
  publish.py POST.md --post-id N               # update existing post (status unchanged)
  publish.py POST.md --post-id N --publish     # publish it
  publish.py POST.md --post-id N --draft       # demote published post back to draft
  publish.py POST.md --pull --post-id N        # fetch post N from WP, write to POST.pulled.md

Editing loop
------------
First run --draft, note the post-id, preview at
  https://wordpress.com/post/<site>/<post-id>
then iterate with --post-id N until happy, then --publish.

Round-trip from WP edits
------------------------
If you edit the post in the WP block editor, run --pull --post-id N to
fetch the current WP content and write it to POST.pulled.md (sidecar, never
clobbers the source). Diff with `diff -u POST.md POST.pulled.md` and merge
the changes you want back into POST.md by hand. Round-trip isn't perfect:
pandoc html→md normalises whitespace, smart quotes, and table wrapping, so
expect cosmetic diffs alongside the substantive ones.

Safety
------
Without --post-id this script ALWAYS creates a brand-new post. It cannot
overwrite an existing published post by accident — you have to pass the
target post-id explicitly. Post 1 ("Nexus by Example") on tensegrity.blog
is post-id 1284; never pass it as --post-id unless you really mean to edit
the live post.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

CONFIG_PATH = Path.home() / ".config" / "nexus-blog" / "config.json"


REQUIRED_KEYS = ("site", "ssh_user", "ssh_host")


def load_config() -> dict[str, str]:
    if not CONFIG_PATH.exists():
        sys.exit(
            f"Missing config at {CONFIG_PATH}.\n"
            "See the docstring at the top of this script for setup steps."
        )
    cfg = json.loads(CONFIG_PATH.read_text())
    missing = [k for k in REQUIRED_KEYS if k not in cfg or not cfg[k]]
    if missing:
        sys.exit(
            f"Config at {CONFIG_PATH} missing required key(s): {missing!r}\n"
            "See the docstring at the top of this script for setup steps."
        )
    cfg.setdefault("ssh_key", "~/.ssh/id_ed25519")
    return cfg


def md_to_html(md_path: Path) -> str:
    """Run pandoc, return inner-body HTML (no <html>/<head> wrapper)."""
    result = subprocess.run(
        [
            "pandoc",
            "--from", "markdown+pipe_tables+task_lists+smart",
            "--to", "html5",
            "--no-highlight",
            "--wrap=none",
            str(md_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def extract_title_and_strip(md_path: Path) -> tuple[str, Path]:
    """Pull the first `# Title` line off as the post title.

    Returns (title, path-to-temp-file-without-h1). If no H1 exists, the
    filename stem becomes the title and the original path is returned.
    """
    text = md_path.read_text()
    lines = text.splitlines()
    title = md_path.stem
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            stripped_text = "\n".join(lines[:i] + lines[i + 1 :])
            tmp = md_path.with_suffix(".__publish__.md")
            tmp.write_text(stripped_text)
            return title, tmp
    return title, md_path


def _wrap_list(el: Tag, ordered: bool) -> str:
    """Wrap a <ul>/<ol> + each <li> in Gutenberg list / list-item blocks."""
    new_lis: list[str] = []
    for li in el.find_all("li", recursive=False):
        inner = li.decode_contents().strip()
        new_lis.append(
            f"<!-- wp:list-item -->\n<li>{inner}</li>\n<!-- /wp:list-item -->"
        )
    items_html = "\n".join(new_lis)
    attrs = ' {"ordered":true}' if ordered else ""
    list_tag = "ol" if ordered else "ul"
    return (
        f"<!-- wp:list{attrs} -->\n"
        f"<{list_tag}>\n{items_html}\n</{list_tag}>\n"
        f"<!-- /wp:list -->"
    )


def wrap_blocks(html: str) -> str:
    """Wrap top-level pandoc-emitted HTML in Gutenberg block comments."""
    soup = BeautifulSoup(html, "html.parser")
    parts: list[str] = []

    for el in list(soup.children):
        if isinstance(el, NavigableString):
            text = str(el).strip()
            if text:
                parts.append(
                    f"<!-- wp:paragraph -->\n<p>{text}</p>\n<!-- /wp:paragraph -->"
                )
            continue
        if not isinstance(el, Tag):
            continue

        tag = el.name

        if tag == "p":
            parts.append(
                f"<!-- wp:paragraph -->\n<p>{el.decode_contents()}</p>\n"
                f"<!-- /wp:paragraph -->"
            )
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            attrs = "" if level == 2 else f' {{"level":{level}}}'
            inner = el.decode_contents()
            parts.append(
                f"<!-- wp:heading{attrs} -->\n"
                f"<{tag} class=\"wp-block-heading\">{inner}</{tag}>\n"
                f"<!-- /wp:heading -->"
            )
        elif tag == "pre":
            # Pandoc emits <pre><code class="lang">...</code></pre>; we keep
            # the inner code text and drop the language class (WP.com Code
            # block doesn't natively highlight; keeping classes confuses it).
            code_el = el.find("code")
            inner = code_el.decode_contents() if code_el else el.decode_contents()
            parts.append(
                f"<!-- wp:code -->\n"
                f'<pre class="wp-block-code"><code>{inner}</code></pre>\n'
                f"<!-- /wp:code -->"
            )
        elif tag == "ul":
            parts.append(_wrap_list(el, ordered=False))
        elif tag == "ol":
            parts.append(_wrap_list(el, ordered=True))
        elif tag == "blockquote":
            inner = el.decode_contents().strip()
            parts.append(
                f"<!-- wp:quote -->\n"
                f'<blockquote class="wp-block-quote">{inner}</blockquote>\n'
                f"<!-- /wp:quote -->"
            )
        elif tag == "hr":
            parts.append(
                "<!-- wp:separator -->\n"
                '<hr class="wp-block-separator has-alpha-channel-opacity"/>\n'
                "<!-- /wp:separator -->"
            )
        elif tag == "table":
            existing = el.get("class") or []
            el["class"] = list(existing) + ["wp-block-table"]
            parts.append(
                f"<!-- wp:table -->\n"
                f'<figure class="wp-block-table">{el}</figure>\n'
                f"<!-- /wp:table -->"
            )
        elif tag == "figure":
            # Image figure → wp:image. Lossy if pandoc emitted captions.
            img = el.find("img")
            if img is not None:
                src = img.get("src", "")
                alt = img.get("alt", "")
                parts.append(
                    f"<!-- wp:image -->\n"
                    f'<figure class="wp-block-image">'
                    f'<img src="{src}" alt="{alt}"/></figure>\n'
                    f"<!-- /wp:image -->"
                )
            else:
                parts.append(
                    f"<!-- wp:html -->\n{el}\n<!-- /wp:html -->"
                )
        elif tag == "div":
            # Pandoc sometimes wraps things in <div>. Pass through as raw HTML.
            parts.append(f"<!-- wp:html -->\n{el}\n<!-- /wp:html -->")
        else:
            # Unknown top-level element: wrap in raw HTML block as a fallback
            # so it at least round-trips cleanly through the editor.
            parts.append(f"<!-- wp:html -->\n{el}\n<!-- /wp:html -->")

    return "\n\n".join(parts)


def _shell_quote(s: str) -> str:
    """POSIX-safe single-quoting for embedding in a remote shell command."""
    return "'" + s.replace("'", "'\\''") + "'"


def _ssh_command(cfg: dict[str, str], remote_cmd: str) -> list[str]:
    key = str(Path(cfg["ssh_key"]).expanduser())
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-i", key,
        f"{cfg['ssh_user']}@{cfg['ssh_host']}",
        remote_cmd,
    ]


def fetch_post(cfg: dict[str, str], post_id: int) -> tuple[str, str]:
    """Pull (title, content) from WP via wp-cli."""
    remote = (
        f"wp post get {post_id} --field=post_title; "
        f"echo '<<<NEXUS_PUBLISH_BOUNDARY>>>'; "
        f"wp post get {post_id} --field=post_content"
    )
    cmd = _ssh_command(cfg, remote)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        sys.exit(
            f"wp-cli fetch failed (exit {proc.returncode}):\n"
            f"  stderr: {proc.stderr.strip()}"
        )
    parts = proc.stdout.split("<<<NEXUS_PUBLISH_BOUNDARY>>>\n", 1)
    if len(parts) != 2:
        sys.exit(f"Unexpected wp-cli output (no boundary):\n{proc.stdout[:400]}")
    title = parts[0].rstrip("\n")
    content = parts[1]
    return title, content


_SMART_TO_ASCII = {
    "\u2018": "'", "\u2019": "'",   # ' '
    "\u201c": '"', "\u201d": '"',   # " "
    "\u2013": "--",                   # –  (en dash → -- to match markdown source convention)
    # \u2014 (em dash) intentionally left intact — already used as-is in source
    "\u2026": "...",                  # …
    "\u00a0": " ",                    # non-breaking → space
}


_HR_RE = re.compile(r"^-{20,}$", re.MULTILINE)
_HEADING_ATTRS_RE = re.compile(r"^(#+\s+.+?)\s+\{[^}]+\}\s*$", re.MULTILINE)
_OL_INDENT_RE = re.compile(r"^(\s*\d+\.)  +", re.MULTILINE)
_ITALIC_RE = re.compile(r"(?<![\*\w])\*([^\s\*][^\*\n]*?[^\s\*]|\S)\*(?![\*\w])")
_FENCE_CLASS_RE = re.compile(r"^(\s*```)\s*[\w\.\-]+\s*$", re.MULTILINE)


def _normalize_for_diff(s: str) -> str:
    """Squeeze cosmetic round-trip noise so real edits stand out in diff.

    Pandoc html→md emits long underline rules for <hr>, attaches {#id .class}
    blocks to every heading, indents ordered list items with two spaces, and
    prefers *star* italic over _underscore_. The source files use the
    opposite convention for each. Normalising on the way in matches them.
    """
    for k, v in _SMART_TO_ASCII.items():
        s = s.replace(k, v)
    s = _HR_RE.sub("---", s)
    s = _HEADING_ATTRS_RE.sub(r"\1", s)
    s = _OL_INDENT_RE.sub(r"\1 ", s)
    s = _ITALIC_RE.sub(r"_\1_", s)
    s = _FENCE_CLASS_RE.sub(r"\1", s)
    return s


def block_html_to_markdown(html: str) -> str:
    """Strip Gutenberg block comments, run pandoc html→markdown.

    Output flavor is plain `markdown` (not `markdown_strict`) so we get
    fenced code blocks and underscore-italic — matches the convention the
    source files use, minimising round-trip diff noise. Smart Unicode
    punctuation is normalised back to ASCII for the same reason.
    """
    html = re.sub(r"<!--\s*/?wp:[^>]*?-->\n?", "", html)
    proc = subprocess.run(
        [
            "pandoc",
            "--from", "html",
            "--to", "markdown+pipe_tables-smart-fancy_lists-bracketed_spans-link_attributes",
            "--wrap=none",
            "--no-highlight",
            "--markdown-headings=atx",
        ],
        input=html,
        capture_output=True,
        text=True,
        check=True,
    )
    return _normalize_for_diff(proc.stdout)


def pull_to_sidecar(cfg: dict[str, str], md_path: Path, post_id: int) -> Path:
    """Pull post N from WP, write to <md_path>.pulled.md, return that path."""
    title, content_html = fetch_post(cfg, post_id)
    body_md = block_html_to_markdown(content_html)
    full_md = f"# {title}\n\n{body_md}"
    out_path = md_path.with_suffix(".pulled.md")
    out_path.write_text(full_md)
    return out_path


class WpcliError(RuntimeError):
    """Raised when an SSH+wp-cli call fails. Carries stderr+stdout for surfacing."""


def upload(
    cfg: dict[str, str],
    *,
    title: str,
    content: str,
    status: str | None,
    post_id: int | None,
    slug: str | None = None,
) -> dict[str, str]:
    """Run wp-cli over SSH. Returns dict with at least 'id' and 'status'.

    Raises WpcliError on failure (so batch callers can continue past one bad
    push). Single-post callers in main() turn this back into sys.exit().
    """
    if post_id is None:
        # Create. Title required, status defaults to draft.
        title_arg = f"--post_title={_shell_quote(title)}"
        status_arg = f"--post_status={_shell_quote(status or 'draft')}"
        slug_arg = f"--post_name={_shell_quote(slug)}" if slug else ""
        remote = (
            f"wp post create --post_type=post {title_arg} {status_arg} {slug_arg} "
            f"--porcelain -"
        )
    else:
        # Update. Trailing "-" tells wp-cli to read post content from stdin
        # (NOT --post_content=-, which stores the literal dash).
        parts = [f"wp post update {post_id}", f"--post_title={_shell_quote(title)}"]
        if status:
            parts.append(f"--post_status={_shell_quote(status)}")
        parts.append("-")
        remote = " ".join(parts)

    cmd = _ssh_command(cfg, remote)
    proc = subprocess.run(
        cmd, input=content, capture_output=True, text=True, timeout=120
    )
    if proc.returncode != 0:
        raise WpcliError(
            f"wp-cli over SSH failed (exit {proc.returncode}): "
            f"stderr={proc.stderr.strip()!r} stdout={proc.stdout.strip()!r}"
        )
    out = proc.stdout.strip()
    # On --porcelain create, stdout is just the new post-id. On update, stdout
    # is "Success: Updated post N." — fish out the id.
    if post_id is None:
        new_id = out.splitlines()[-1].strip()
        return {"id": new_id, "status": status or "draft"}
    return {"id": str(post_id), "status": status or "(unchanged)"}


def _build_block_html(md_path: Path) -> tuple[str, str]:
    """Run the title-extraction + pandoc + Gutenberg-block pipeline on a file.

    Returns (title, block_html). Raises FileNotFoundError if the file is gone.
    Used by both single-post and batch-mode flows so they stay in sync.
    """
    if not md_path.exists():
        raise FileNotFoundError(str(md_path))
    title, working_path = extract_title_and_strip(md_path)
    try:
        html = md_to_html(working_path)
    finally:
        if working_path != md_path and working_path.exists():
            working_path.unlink()
    return title, wrap_blocks(html)


def parse_batch_arg(arg: str) -> list[tuple[Path, int]]:
    """Parse `FILE:ID,FILE:ID,...` into a list of (path, post_id) pairs."""
    pairs: list[tuple[Path, int]] = []
    for raw in arg.split(","):
        entry = raw.strip()
        if not entry:
            continue
        if ":" not in entry:
            sys.exit(f"--batch entry missing ':id': {entry!r}")
        path_str, id_str = entry.rsplit(":", 1)
        try:
            post_id = int(id_str.strip())
        except ValueError:
            sys.exit(f"--batch entry id is not an integer: {entry!r}")
        pairs.append((Path(path_str.strip()), post_id))
    if not pairs:
        sys.exit("--batch is empty")
    return pairs


def run_batch(
    cfg: dict[str, str],
    pairs: list[tuple[Path, int]],
    *,
    status: str | None,
    delay_seconds: float = 1.0,
) -> int:
    """Push N posts over (typically) one TCP connection. Returns exit code.

    Continues past per-post failures, prints a summary table at the end.
    Inter-iteration `delay_seconds` is a polite back-pressure default.
    """
    site = cfg["site"]
    results: list[tuple[str, int, str, str]] = []  # (filename, id, state, detail)

    for i, (md_path, post_id) in enumerate(pairs):
        try:
            title, block_html = _build_block_html(md_path)
        except FileNotFoundError:
            results.append((md_path.name, post_id, "FAIL", "file not found"))
            print(f"FAIL  id={post_id}  ({md_path.name}): file not found", file=sys.stderr)
            continue
        except subprocess.CalledProcessError as e:
            results.append((md_path.name, post_id, "FAIL", f"pandoc: {e}"))
            print(f"FAIL  id={post_id}  ({md_path.name}): pandoc failure", file=sys.stderr)
            continue

        try:
            data = upload(
                cfg, title=title, content=block_html,
                status=status, post_id=post_id, slug=None,
            )
            results.append((md_path.name, post_id, "OK", data["status"]))
            print(
                f"OK    status={data['status']!r}  id={data['id']}  "
                f"({md_path.name})"
            )
        except WpcliError as e:
            results.append((md_path.name, post_id, "FAIL", str(e)))
            print(f"FAIL  id={post_id}  ({md_path.name}): {e}", file=sys.stderr)
        except subprocess.TimeoutExpired:
            results.append((md_path.name, post_id, "FAIL", "ssh timeout"))
            print(f"FAIL  id={post_id}  ({md_path.name}): ssh timeout", file=sys.stderr)

        if i < len(pairs) - 1 and delay_seconds > 0:
            time.sleep(delay_seconds)

    n_ok = sum(1 for r in results if r[2] == "OK")
    n_fail = len(results) - n_ok
    print()
    print(f"=== Batch summary ({len(pairs)} posts) ===")
    for name, pid, state, info in results:
        marker = "ok" if state == "OK" else "FAIL"
        print(f"  [{marker}] id={pid}  {name}")
        if state != "OK":
            print(f"         {info}")
    print(f"\n{n_ok} succeeded, {n_fail} failed")
    print(f"  edit: https://wordpress.com/post/{site}/<id>")
    return 0 if n_fail == 0 else 1


def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Publish a markdown post to WordPress.com as Gutenberg blocks."
        )
    )
    ap.add_argument(
        "md_path", type=Path, nargs="?", default=None,
        help="Path to .md file. Required unless --batch is given.",
    )
    ap.add_argument(
        "--post-id", type=int, default=None,
        help="Update existing post by ID (default: create new draft)",
    )
    ap.add_argument(
        "--publish", action="store_true",
        help="Set status=publish (default: draft on create, unchanged on update)",
    )
    ap.add_argument(
        "--draft", action="store_true",
        help="Force status=draft (default behavior on create; useful with --post-id to demote a published post back to draft)",
    )
    ap.add_argument(
        "--print-html", action="store_true",
        help="Print converted block-HTML to stdout; do not upload",
    )
    ap.add_argument(
        "--pull", action="store_true",
        help="Fetch post --post-id from WP and write to POST.pulled.md (sidecar; does not overwrite source)",
    )
    ap.add_argument(
        "--slug", default=None,
        help="Post slug (post_name) to set on create. Locks the eventual published URL.",
    )
    ap.add_argument(
        "--batch", default=None,
        help=(
            "Update multiple posts in one invocation: "
            "FILE1:ID1,FILE2:ID2,... . Pushes are sequential with a 1s pause "
            "between calls, but ControlMaster (if configured in ~/.ssh/config) "
            "lets them share one TCP connection. Per-post failures are collected; "
            "a summary table prints at the end. Mutually exclusive with md_path."
        ),
    )
    ap.add_argument(
        "--batch-delay", type=float, default=1.0,
        help="Inter-post pause in --batch mode, in seconds (default: 1.0).",
    )
    args = ap.parse_args()

    # md_path is optional iff --batch is given; required otherwise.
    if not args.batch and args.md_path is None:
        ap.error("md_path is required unless --batch is given")
    if args.batch and args.md_path is not None:
        ap.error("md_path is incompatible with --batch (the batch arg carries paths)")

    if args.batch:
        if args.print_html or args.pull:
            sys.exit("--batch is incompatible with --print-html / --pull.")
        if args.publish and args.draft:
            sys.exit("Pass either --publish or --draft, not both.")
        cfg = load_config()
        pairs = parse_batch_arg(args.batch)
        if args.publish:
            status = "publish"
        elif args.draft:
            status = "draft"
        else:
            status = None
        sys.exit(run_batch(cfg, pairs, status=status, delay_seconds=args.batch_delay))

    if args.pull:
        if args.post_id is None:
            sys.exit("--pull requires --post-id N")
        if args.publish or args.draft or args.print_html:
            sys.exit("--pull is read-only; cannot combine with --publish/--draft/--print-html")
        cfg = load_config()
        out = pull_to_sidecar(cfg, args.md_path, args.post_id)
        diff_cmd = f"diff -u {args.md_path} {out}" if args.md_path.exists() else f"cat {out}"
        print(f"Pulled post {args.post_id} → {out}")
        print(f"  diff: {diff_cmd}")
        return

    try:
        title, block_html = _build_block_html(args.md_path)
    except FileNotFoundError:
        sys.exit(f"Not found: {args.md_path}")

    if args.print_html:
        sys.stdout.write(block_html)
        return

    if args.publish and args.draft:
        sys.exit("Pass either --publish or --draft, not both.")

    cfg = load_config()
    if args.post_id is None:
        status = "publish" if args.publish else "draft"
    elif args.publish:
        status = "publish"
    elif args.draft:
        status = "draft"
    else:
        status = None  # leave existing post status alone on update

    try:
        data = upload(
            cfg,
            title=title,
            content=block_html,
            status=status,
            post_id=args.post_id,
            slug=args.slug,
        )
    except WpcliError as e:
        sys.exit(str(e))
    site = cfg["site"]
    print(
        f"OK  status={data['status']!r}  id={data['id']}\n"
        f"  edit: https://wordpress.com/post/{site}/{data['id']}"
    )


if __name__ == "__main__":
    main()
