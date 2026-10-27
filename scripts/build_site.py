#!/usr/bin/env python3
"""Build a static embed page from the Colofornia Dreaming Substack.

Posts are downloaded directly from Substack (no rss2json or other
third-party proxy). The RSS feed is preferred. If Cloudflare challenges
that URL, the script reads Substack's own public archive JSON instead.
Output is a self-contained HTML page meant to be iframed: no scripts, no
frame-busting, styling in the page itself.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

FEED_URL = "https://coloforniadreaming.substack.com/feed"
ARCHIVE_URL = "https://coloforniadreaming.substack.com/api/v1/archive?sort=new&offset=0&limit=12"
SUBSCRIBE_EMBED_URL = "https://coloforniadreaming.substack.com/embed"
PUBLICATION_URL = "https://coloforniadreaming.substack.com"
DEFAULT_TITLE = "Colofornia Dreaming"
MAX_POSTS = 10
# Substack sits behind Cloudflare. A browser User-Agent is still a direct fetch.
# GitHub-hosted runners often also need a browser TLS fingerprint (curl_cffi).
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
ROOT = Path(__file__).resolve().parents[1]
SITE_DIR = ROOT / "site"
TAG_RE = re.compile(r"<[^>]+>")
FRAME_BUST_RE = re.compile(r"top\.location|parent\.location|frameElement|framebust", re.I)


@dataclass(frozen=True)
class Post:
    title: str
    url: str
    published: datetime | None
    summary: str


@dataclass(frozen=True)
class Feed:
    title: str
    posts: list[Post]


def _looks_like_feed(body: bytes) -> bool:
    head = body.lstrip()[:400].lower()
    return head.startswith(b"<?xml") or b"<rss" in head or b"<feed" in head


def _looks_like_json(body: bytes) -> bool:
    return body.lstrip()[:1] in (b"{", b"[")


def _acceptable(body: bytes) -> bool:
    return _looks_like_feed(body) or _looks_like_json(body)


def _snippet(body: bytes) -> str:
    return body[:160].decode("utf-8", "replace").replace("\n", " ")


def fetch_with_urllib(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": BROWSER_UA,
            "Accept": "application/rss+xml, application/xml, text/xml, application/json;q=0.9, */*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = _snippet(exc.read())
        raise RuntimeError(f"python HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"python request failed: {exc}") from exc
    if not _acceptable(body):
        raise RuntimeError(f"python response was not a feed: {_snippet(body)}")
    return body


def fetch_with_curl(url: str) -> bytes:
    """Direct download with curl. Used when Python's HTTP client is rejected."""
    if shutil.which("curl") is None:
        raise RuntimeError("curl is not installed")
    result = subprocess.run(
        [
            "curl",
            "-sS",
            "-L",
            "--compressed",
            "--max-time",
            "40",
            "-A",
            BROWSER_UA,
            "-H",
            "Accept: application/rss+xml, application/xml, text/xml, application/json;q=0.9, */*;q=0.8",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
            "-w",
            "\n%{http_code}",
            url,
        ],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", "replace")[:200]
        raise RuntimeError(f"curl failed ({result.returncode}): {err}")
    raw = result.stdout
    if b"\n" not in raw:
        raise RuntimeError("curl returned no status code")
    body, _, status = raw.rpartition(b"\n")
    code = status.decode("ascii", "replace").strip()
    if code != "200" or not _acceptable(body):
        raise RuntimeError(f"curl HTTP {code}: {_snippet(body)}")
    return body


def fetch_with_curl_cffi(url: str) -> bytes:
    """Direct download with a browser TLS fingerprint. Optional dependency."""
    try:
        from curl_cffi import requests as curl_requests
    except ImportError as exc:
        raise RuntimeError("curl_cffi is not installed") from exc
    try:
        response = curl_requests.get(
            url,
            impersonate="chrome",
            timeout=40,
            headers={
                "Accept": "application/rss+xml, application/xml, text/xml, application/json;q=0.9, */*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
    except Exception as exc:
        raise RuntimeError(f"curl_cffi request failed: {exc}") from exc
    body = response.content or b""
    if response.status_code != 200 or not _acceptable(body):
        raise RuntimeError(f"curl_cffi HTTP {response.status_code}: {_snippet(body)}")
    return body


def fetch_bytes(url: str, fetchers: tuple) -> bytes:
    errors: list[str] = []
    for fetcher in fetchers:
        try:
            return fetcher(url)
        except RuntimeError as exc:
            errors.append(str(exc)[:180])
    raise RuntimeError("; ".join(errors))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str:
    for child in element:
        if _local(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def _safe_http_url(url: str) -> str | None:
    cleaned = url.strip()
    if cleaned.startswith("https://") or cleaned.startswith("http://"):
        return cleaned
    return None


def _plain_text(value: str) -> str:
    text = TAG_RE.sub(" ", value)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _truncate(text: str, limit: int = 160) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(".,;:")
    if not cut:
        cut = text[:limit].rstrip()
    return cut + "…"


def parse_pub_date(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_feed(xml_bytes: bytes) -> Feed:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise RuntimeError(f"Substack feed was not valid RSS: {exc}") from exc

    channel = next((child for child in root if _local(child.tag) == "channel"), None)
    if channel is None:
        raise RuntimeError("Substack feed has no channel")

    title = _plain_text(_child_text(channel, "title")) or DEFAULT_TITLE
    posts: list[Post] = []
    for item in channel:
        if _local(item.tag) != "item":
            continue
        item_title = _plain_text(_child_text(item, "title"))
        url = _safe_http_url(_child_text(item, "link"))
        if not item_title or url is None:
            continue
        posts.append(
            Post(
                title=item_title,
                url=url,
                published=parse_pub_date(_child_text(item, "pubDate")),
                summary=_truncate(_plain_text(_child_text(item, "description"))),
            )
        )
    return Feed(title=title, posts=select_posts(posts))


def select_posts(posts: list[Post], limit: int = MAX_POSTS) -> list[Post]:
    """Newest first. Undated items stay at the end. Cap at the latest `limit`."""

    def sort_key(post: Post) -> tuple[int, float]:
        if post.published is None:
            return (1, 0.0)
        return (0, -post.published.timestamp())

    return sorted(posts, key=sort_key)[:limit]


def parse_iso(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_archive(payload: bytes) -> Feed:
    """Substack's public archive. Same posts as the RSS feed, as JSON."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Substack archive was not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise RuntimeError("Substack archive was not a list of posts")

    posts: list[Post] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        item_title = _plain_text(str(item.get("title") or ""))
        url = _safe_http_url(str(item.get("canonical_url") or ""))
        if url is None:
            slug = str(item.get("slug") or "").strip()
            if slug and all(part not in slug for part in ("/", " ", "..")):
                url = f"{PUBLICATION_URL}/p/{slug}"
        if not item_title or url is None:
            continue
        summary = _truncate(_plain_text(str(item.get("subtitle") or item.get("description") or "")))
        posts.append(
            Post(
                title=item_title,
                url=url,
                published=parse_iso(str(item.get("post_date") or "")),
                summary=summary,
            )
        )
    return Feed(title=DEFAULT_TITLE, posts=select_posts(posts))


def load_posts() -> tuple[Feed, str]:
    """Prefer the RSS feed. Fall back to Substack's own archive if it is challenged."""
    sources = (
        ("rss", FEED_URL, parse_feed),
        ("archive", ARCHIVE_URL, parse_archive),
    )
    cheap = (fetch_with_urllib, fetch_with_curl)
    errors: list[str] = []
    for fetchers in (cheap, (fetch_with_curl_cffi,)):
        for label, url, parser in sources:
            try:
                body = fetch_bytes(url, fetchers)
                feed = parser(body)
            except RuntimeError as exc:
                errors.append(f"{label}: {exc}")
                continue
            if not feed.posts:
                errors.append(f"{label}: no posts")
                continue
            print(f"Source: {label} ({url})")
            return feed, url
    raise RuntimeError("Could not read posts from Substack. " + " | ".join(errors))


def format_date(when: datetime) -> str:
    return f"{when.strftime('%B')} {when.day}, {when.year}"


def render_posts(posts: list[Post]) -> str:
    if not posts:
        return '<p class="empty">No posts were found in the feed.</p>'

    items: list[str] = []
    for index, post in enumerate(posts, start=1):
        summary = f'<span class="dek">{html.escape(post.summary)}</span>' if post.summary else ""
        if post.published is not None:
            meta = (
                f'<time datetime="{post.published.strftime("%Y-%m-%d")}">'
                f"{html.escape(format_date(post.published))}</time>"
            )
        else:
            meta = "<span>Date unavailable</span>"
        items.append(
            "\n".join(
                [
                    '<li class="post">',
                    f'  <a href="{html.escape(post.url, quote=True)}" target="_blank" rel="noopener noreferrer">',
                    f'    <span class="num">{index:02d}</span>',
                    '    <span class="body">',
                    f'      <span class="title">{html.escape(post.title)}</span>',
                    f"      {summary}" if summary else "",
                    f'      <span class="meta">{meta}<span class="visually-hidden">Opens in a new tab</span></span>',
                    "    </span>",
                    "  </a>",
                    "</li>",
                ]
            )
        )
    return '<ol class="posts">\n' + "\n".join(items) + "\n</ol>"


def render_page(feed: Feed, generated_at: datetime, source_url: str = FEED_URL) -> str:
    generated = generated_at.astimezone(timezone.utc)
    stamp = f"{format_date(generated)} at {generated.strftime('%H:%M')} UTC"
    posts_html = render_posts(feed.posts)
    title = feed.title or DEFAULT_TITLE
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} — Latest posts</title>
  <meta name="description" content="Latest posts from {html.escape(title)}, plus the Substack subscribe form.">
  <meta name="referrer" content="strict-origin-when-cross-origin">
  <!-- Generated from {html.escape(source_url)}. {html.escape(stamp)}. Do not edit by hand. -->
  <style>
    :root {{
      --paper: #f3eee4;
      --ink: #1c1916;
      --muted: #5c564c;
      --line: #d8d0c3;
      --accent: #7c2d2d;
    }}
    * {{ box-sizing: border-box; }}
    html {{
      background: var(--paper);
      color: var(--ink);
      color-scheme: light;
    }}
    body {{
      margin: 0;
      font-family: Georgia, "Iowan Old Style", "Palatino Linotype", Palatino, "Times New Roman", serif;
      line-height: 1.45;
    }}
    .wrap {{
      max-width: 40rem;
      margin: 0 auto;
      padding: 1.25rem 1.15rem 1.6rem;
    }}
    header {{
      border-bottom: 1px solid var(--ink);
      padding-bottom: 0.8rem;
      margin-bottom: 0.15rem;
    }}
    .kicker {{
      margin: 0;
      font-family: system-ui, "Segoe UI", sans-serif;
      font-size: 0.72rem;
      letter-spacing: 0.16em;
      text-transform: uppercase;
      color: var(--accent);
    }}
    h1 {{
      font-weight: normal;
      font-size: 1.9rem;
      line-height: 1.15;
      margin: 0.25rem 0 0.35rem;
    }}
    .lede {{
      margin: 0;
      color: var(--muted);
      font-size: 1rem;
    }}
    .posts {{
      list-style: none;
      margin: 0;
      padding: 0;
    }}
    .post a {{
      display: grid;
      grid-template-columns: 2rem 1fr;
      gap: 0.15rem 0.7rem;
      padding: 0.85rem 0.15rem;
      border-bottom: 1px solid var(--line);
      text-decoration: none;
      color: inherit;
    }}
    .post a:hover .title,
    .post a:focus-visible .title {{
      color: var(--accent);
    }}
    .post a:focus-visible {{
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }}
    .num {{
      font-family: system-ui, "Segoe UI", sans-serif;
      font-size: 0.75rem;
      letter-spacing: 0.04em;
      color: var(--accent);
      padding-top: 0.28rem;
    }}
    .title {{
      display: block;
      font-size: 1.18rem;
      line-height: 1.25;
    }}
    .dek {{
      display: block;
      margin-top: 0.22rem;
      color: var(--muted);
      font-size: 0.95rem;
    }}
    .meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.35rem 0.75rem;
      margin-top: 0.35rem;
      font-family: system-ui, "Segoe UI", sans-serif;
      font-size: 0.75rem;
      letter-spacing: 0.01em;
      color: var(--muted);
    }}
    .visually-hidden {{
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }}
    .empty {{
      color: var(--muted);
    }}
    .subscribe {{
      margin-top: 1.35rem;
    }}
    .subscribe h2 {{
      font-weight: normal;
      font-size: 1.35rem;
      margin: 0 0 0.3rem;
    }}
    .subscribe p {{
      margin: 0 0 0.75rem;
      color: var(--muted);
      font-size: 0.98rem;
    }}
    .subscribe iframe {{
      display: block;
      width: 100%;
      max-width: 480px;
      height: 320px;
      border: 1px solid #EEE;
      background: #fff;
    }}
    footer {{
      margin-top: 1rem;
      font-family: system-ui, "Segoe UI", sans-serif;
      font-size: 0.75rem;
      color: var(--muted);
    }}
    footer a {{
      color: var(--accent);
    }}
  </style>
</head>
<body>
  <main class="wrap">
    <header>
      <p class="kicker">{html.escape(title)}</p>
      <h1>Latest writing</h1>
      <p class="lede">Essays on the Republic, the Constitution, and the right to keep and bear arms. Each title opens on Substack.</p>
    </header>
    {posts_html}
    <section class="subscribe" aria-label="Subscribe">
      <h2>Subscribe</h2>
      <p>New posts by email, through Substack.</p>
      <iframe src="{SUBSCRIBE_EMBED_URL}" title="Subscribe to {html.escape(title)} on Substack" width="480" height="320" style="border:1px solid #EEE; background:white;" frameborder="0" scrolling="no"></iframe>
    </section>
    <footer>
      <p>Updated {html.escape(stamp)}.
      <a href="{PUBLICATION_URL}" target="_blank" rel="noopener noreferrer">Read everything on Substack</a>.</p>
    </footer>
  </main>
</body>
</html>
"""


def assert_embeddable(page: str, feed: Feed) -> None:
    """Fail the build if the page is missing posts, the subscribe embed, or is unsafe to frame."""
    if "<!DOCTYPE html>" not in page:
        raise RuntimeError("Generated page is missing a doctype")
    if SUBSCRIBE_EMBED_URL not in page:
        raise RuntimeError("Generated page is missing the Substack subscribe embed")
    if FRAME_BUST_RE.search(page):
        raise RuntimeError("Generated page contains frame-busting code")
    if "<script" in page.lower():
        raise RuntimeError("Generated page must not include scripts")
    for post in feed.posts:
        if html.escape(post.title) not in page:
            raise RuntimeError(f"Generated page is missing post: {post.title}")
        if html.escape(post.url, quote=True) not in page:
            raise RuntimeError(f"Generated page is missing link: {post.url}")
    if not feed.posts:
        raise RuntimeError("Feed did not contain any posts")


def write_site(page: str, site_dir: Path) -> None:
    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / ".nojekyll").write_text("", encoding="utf-8")
    (site_dir / "index.html").write_text(page, encoding="utf-8")
    # Same document at /embed.html so either address can be iframed.
    (site_dir / "embed.html").write_text(page, encoding="utf-8")


def build(feed: Feed, generated_at: datetime, site_dir: Path, source_url: str) -> None:
    page = render_page(feed, generated_at, source_url)
    assert_embeddable(page, feed)
    write_site(page, site_dir)


def run_self_test() -> None:
    older = "Mon, 01 Jan 2024 00:00:00 GMT"
    newer = "Tue, 27 Oct 2026 12:00:00 GMT"
    items = []
    for number in range(12):
        items.append(
            f"""<item>
              <title><![CDATA[Post {number:02d}]]></title>
              <link>https://coloforniadreaming.substack.com/p/post-{number:02d}</link>
              <pubDate>{older if number % 2 else newer}</pubDate>
              <description>Summary {number} with extra words.</description>
            </item>"""
        )
    items.append(
        """<item>
          <title><![CDATA[A & B <script>alert(1)</script>]]></title>
          <link>https://coloforniadreaming.substack.com/p/escaped</link>
          <pubDate>Wed, 28 Oct 2026 15:04:00 GMT</pubDate>
          <description><![CDATA[<b>Bold</b> &amp; plain]]></description>
        </item>"""
    )
    items.append(
        """<item>
          <title>Skip me</title>
          <link>javascript:alert(1)</link>
          <pubDate>Wed, 28 Oct 2026 15:04:00 GMT</pubDate>
        </item>"""
    )
    xml_bytes = f"""<?xml version="1.0" encoding="UTF-8"?>
      <rss version="2.0"><channel>
        <title><![CDATA[Colofornia Dreaming]]></title>
        {''.join(items)}
      </channel></rss>""".encode()

    feed = parse_feed(xml_bytes)
    if len(feed.posts) != MAX_POSTS:
        raise AssertionError(f"expected {MAX_POSTS} posts, got {len(feed.posts)}")
    if feed.posts[0].title != "A & B alert(1)":
        raise AssertionError(f"newest post was not first: {feed.posts[0].title}")
    if any("Skip me" in post.title for post in feed.posts):
        raise AssertionError("javascript: link was not dropped")
    if feed.posts[0].summary != "Bold & plain":
        raise AssertionError(f"summary was not plain text: {feed.posts[0].summary}")

    page = render_page(feed, datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc))
    assert_embeddable(page, feed)
    if "<script>" in page.lower():
        raise AssertionError("raw script tag leaked into the page")
    if "A &amp; B alert(1)" not in page:
        raise AssertionError("title was not escaped")
    if "javascript:" in page:
        raise AssertionError("javascript URL leaked into the page")
    if 'class="post"' not in page or page.count('class="post"') != MAX_POSTS:
        raise AssertionError("post markup count mismatch")

    archive = json.dumps(
        [
            {
                "title": "Older <b>one</b>",
                "canonical_url": "https://coloforniadreaming.substack.com/p/older",
                "post_date": "2024-01-01T00:00:00.000Z",
                "subtitle": "old",
            },
            {
                "title": "Newer & fresh",
                "canonical_url": "javascript:alert(1)",
                "slug": "newer",
                "post_date": "2026-10-28T15:04:00.000Z",
                "subtitle": "<i>new</i>",
            },
        ]
    ).encode()
    archived = parse_archive(archive)
    if [post.title for post in archived.posts] != ["Newer & fresh", "Older one"]:
        raise AssertionError(f"archive order or escaping failed: {archived.posts}")
    if archived.posts[0].url != "https://coloforniadreaming.substack.com/p/newer":
        raise AssertionError(f"archive slug fallback failed: {archived.posts[0].url}")
    if archived.posts[0].summary != "new":
        raise AssertionError(f"archive summary failed: {archived.posts[0].summary}")
    print("self-test ok")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Build the Substack embed page.")
    parser.add_argument("--self-test", action="store_true", help="Run parser checks without the network.")
    parser.add_argument("--site-dir", type=Path, default=SITE_DIR, help="Directory to write the static site into.")
    args = parser.parse_args(argv)

    if args.self_test:
        run_self_test()
        return 0

    feed, source_url = load_posts()
    build(feed, datetime.now(timezone.utc), args.site_dir, source_url)
    print(f"Wrote {args.site_dir / 'index.html'}")
    print(f"Wrote {args.site_dir / 'embed.html'}")
    print(f"Posts ({len(feed.posts)}):")
    for post in feed.posts:
        when = post.published.strftime("%Y-%m-%d") if post.published else "undated"
        print(f"- {post.title} ({when})")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
