from __future__ import annotations

import logging
import os
import re
import sqlite3
import traceback
from hashlib import sha256
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from flask import Flask, redirect, render_template, request, url_for
from matcher import normalize_line, similarity_score
from werkzeug.exceptions import HTTPException

BASE_DIR = Path(__file__).parent
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "data.sqlite3")))
RSS_URL = os.getenv("BUNGIE_RSS_URL", "https://www.bungie.net/en/rss/news?currentPage=0")
RSS_BASE_URL = os.getenv("BUNGIE_BASE_URL", "https://www.bungie.net")
RSS_MAX_PAGES = int(os.getenv("RSS_MAX_PAGES", "3"))
PORT = int(os.getenv("PORT", "8000"))
LOG_PATH = Path(os.getenv("LOG_PATH", str(BASE_DIR / "logs" / "app.log")))
HTTP_USER_AGENT = os.getenv(
    "HTTP_USER_AGENT",
    "player-support-report/1.0 (+https://github.com/)",
)

TWID_HEADING_HINTS = {
    "known issues",
    "known issue",
    "patch preview",
    "patch notes preview",
    "preview",
    "sandbox",
    "balance",
    "changes",
    "tuning",
    "weapon",
    "weapons",
    "armor",
    "ability",
    "abilities",
    "class",
    "subclass",
    "exotic",
}

app = Flask(__name__)


@dataclass
class SectionItem:
    title: str
    body: str


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            article_type TEXT NOT NULL CHECK(article_type IN ('twid','patch','other')),
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            published_at TEXT,
            content_text TEXT NOT NULL DEFAULT '',
            content_markdown TEXT NOT NULL DEFAULT '',
            content_normalized TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL DEFAULT '',
            content_html TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS extracted_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
            section TEXT NOT NULL,
            item_text TEXT NOT NULL,
            normalized_item TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_articles_type_pub ON articles(article_type, published_at);
        CREATE INDEX IF NOT EXISTS idx_items_section ON extracted_items(section);

        CREATE TABLE IF NOT EXISTS app_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            level TEXT NOT NULL,
            message TEXT NOT NULL,
            details TEXT
        );

        CREATE TABLE IF NOT EXISTS article_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
            fetched_at TEXT NOT NULL,
            title TEXT NOT NULL,
            published_at TEXT,
            content_hash TEXT NOT NULL,
            content_text TEXT NOT NULL,
            content_markdown TEXT NOT NULL,
            content_normalized TEXT NOT NULL,
            content_html TEXT NOT NULL,
            is_changed INTEGER NOT NULL CHECK(is_changed IN (0,1))
        );

        CREATE INDEX IF NOT EXISTS idx_versions_article_fetched ON article_versions(article_id, fetched_at DESC);
        """
    )
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(articles)").fetchall()}
    if "content_text" not in existing_cols:
        conn.execute("ALTER TABLE articles ADD COLUMN content_text TEXT NOT NULL DEFAULT ''")
    if "content_normalized" not in existing_cols:
        conn.execute("ALTER TABLE articles ADD COLUMN content_normalized TEXT NOT NULL DEFAULT ''")
    if "content_markdown" not in existing_cols:
        conn.execute("ALTER TABLE articles ADD COLUMN content_markdown TEXT NOT NULL DEFAULT ''")
    if "content_hash" not in existing_cols:
        conn.execute("ALTER TABLE articles ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''")
    conn.commit()
    conn.close()


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
    )


def add_log(level: str, message: str, details: str | None = None) -> None:
    level_upper = level.upper()
    logging.log(getattr(logging, level_upper, logging.INFO), message)
    conn = get_db()
    conn.execute(
        "INSERT INTO app_logs(created_at, level, message, details) VALUES(?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), level_upper, message, details),
    )
    conn.commit()
    conn.close()


def classify_article(url: str, title: str) -> str:
    lower_url = url.lower()
    lower_title = title.lower()
    if "twid-" in lower_url or "this week in destiny" in lower_title:
        return "twid"
    if "destiny_update_" in lower_url or "update" in lower_title and "destiny" in lower_title:
        return "patch"
    return "other"


def slug_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def fetch_article_html(url: str) -> str:
    resp = requests.get(url, timeout=30, headers={"User-Agent": HTTP_USER_AGENT})
    resp.raise_for_status()
    return resp.text


def canonical_bungie_url(link_value: str) -> str:
    return urljoin(RSS_BASE_URL, link_value)


def gather_feed_entries() -> list[feedparser.FeedParserDict]:
    entries: list[feedparser.FeedParserDict] = []
    next_url = RSS_URL
    seen = set()

    for _ in range(RSS_MAX_PAGES):
        if not next_url or next_url in seen:
            break
        seen.add(next_url)
        add_log("info", f"Fetching RSS page url={next_url}")
        response = requests.get(next_url, timeout=30, headers={"User-Agent": HTTP_USER_AGENT})
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        if getattr(feed, "bozo", False):
            add_log("warning", "Feed parser reported malformed RSS payload", str(getattr(feed, "bozo_exception", "")))

        page_entries = list(getattr(feed, "entries", []))
        entries.extend(page_entries)
        add_log("info", f"Loaded RSS page entries={len(page_entries)} url={next_url}")

        next_url = None
        for link in getattr(feed, "feed", {}).get("links", []):
            if link.get("rel") == "next" and link.get("href"):
                next_url = canonical_bungie_url(link["href"])
                break

    return entries


def extract_section_items(html: str, article_type: str) -> list[tuple[str, str, str]]:
    soup = BeautifulSoup(html, "html.parser")

    if article_type == "patch":
        target_sections = {"combatant", "abilities", "general", "weapons", "armor", "activities", "fixes"}
    elif article_type != "twid":
        return []

    output: list[tuple[str, str, str]] = []
    headings = soup.find_all(re.compile(r"^h[1-4]$"))
    for heading in headings:
        heading_text = normalize_line(heading.get_text(" ", strip=True))
        if article_type == "twid":
            if not any(hint in heading_text for hint in TWID_HEADING_HINTS):
                continue
            section_name = heading.get_text(" ", strip=True)
            sibling = heading.find_next_sibling()
            while sibling and sibling.name not in ["h1", "h2", "h3", "h4"]:
                list_items = sibling.find_all("li")
                if list_items:
                    for li in list_items:
                        item = li.get_text(" ", strip=True)
                        if len(item) > 10:
                            output.append((section_name, item, normalize_line(item)))
                elif sibling.name == "p":
                    paragraph = sibling.get_text(" ", strip=True)
                    if len(paragraph) > 40 and any(token in normalize_line(paragraph) for token in {"increased", "decreased", "fixed", "reduced", "now"}):
                        output.append((section_name, paragraph, normalize_line(paragraph)))
                sibling = sibling.find_next_sibling()
        else:
            section_name = heading.get_text(" ", strip=True)
            sibling = heading.find_next_sibling()
            while sibling and sibling.name not in ["h1", "h2", "h3", "h4"]:
                for li in sibling.find_all("li"):
                    item = li.get_text(" ", strip=True)
                    if len(item) > 10:
                        output.append((section_name, item, normalize_line(item)))
                sibling = sibling.find_next_sibling()

    # fallback in case lists are flattened in custom markup
    if not output:
        for li in soup.find_all("li"):
            item = li.get_text(" ", strip=True)
            if len(item) > 15:
                output.append(("Uncategorized", item, normalize_line(item)))

    return output


def html_to_markdown(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("article") or soup.find("main") or soup.body or soup
    lines: list[str] = []
    for node in root.find_all(["h1", "h2", "h3", "h4", "p", "li", "blockquote"]):
        text = node.get_text(" ", strip=True)
        if not text:
            continue
        if node.name == "h1":
            lines.append(f"# {text}")
        elif node.name == "h2":
            lines.append(f"## {text}")
        elif node.name == "h3":
            lines.append(f"### {text}")
        elif node.name == "h4":
            lines.append(f"#### {text}")
        elif node.name == "li":
            lines.append(f"- {text}")
        elif node.name == "blockquote":
            lines.append(f"> {text}")
        else:
            lines.append(text)
    return "\n\n".join(lines)


def upsert_article(conn: sqlite3.Connection, *, title: str, url: str, published_at: str | None, html: str) -> int:
    slug = slug_from_url(url)
    article_type = classify_article(url, title)
    fetched_at = datetime.now(timezone.utc).isoformat()
    content_text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    content_markdown = html_to_markdown(html)
    content_normalized = normalize_line(content_markdown or content_text)
    content_hash = sha256(content_normalized.encode("utf-8")).hexdigest()
    previous = conn.execute("SELECT content_hash FROM articles WHERE slug = ?", (slug,)).fetchone()
    is_changed = 0 if previous and previous["content_hash"] == content_hash else 1
    conn.execute(
        """
        INSERT INTO articles(slug, article_type, title, url, published_at, content_text, content_markdown, content_normalized, content_hash, content_html, fetched_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(slug)
        DO UPDATE SET
          title=excluded.title,
          article_type=excluded.article_type,
          url=excluded.url,
          published_at=excluded.published_at,
          content_text=excluded.content_text,
          content_markdown=excluded.content_markdown,
          content_normalized=excluded.content_normalized,
          content_hash=excluded.content_hash,
          content_html=excluded.content_html,
          fetched_at=excluded.fetched_at
        """,
        (slug, article_type, title, url, published_at, content_text, content_markdown, content_normalized, content_hash, html, fetched_at),
    )
    row = conn.execute("SELECT id FROM articles WHERE slug = ?", (slug,)).fetchone()
    assert row is not None
    article_id = int(row["id"])
    conn.execute(
        """
        INSERT INTO article_versions(article_id, fetched_at, title, published_at, content_hash, content_text, content_markdown, content_normalized, content_html, is_changed)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (article_id, fetched_at, title, published_at, content_hash, content_text, content_markdown, content_normalized, html, is_changed),
    )

    conn.execute("DELETE FROM extracted_items WHERE article_id = ?", (article_id,))
    for section, item, normalized in extract_section_items(html, article_type):
        conn.execute(
            "INSERT INTO extracted_items(article_id, section, item_text, normalized_item) VALUES(?,?,?,?)",
            (article_id, section, item, normalized),
        )
    return article_id


def parse_feed_date(entry: feedparser.FeedParserDict) -> str | None:
    if getattr(entry, "published_parsed", None):
        dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        return dt.isoformat()
    return None


def sync_from_rss() -> dict[str, int]:
    add_log("info", f"Starting RSS sync from {RSS_URL}")
    conn = get_db()
    imported = 0
    skipped = 0
    unchanged = 0
    failed = 0
    feed_entries = gather_feed_entries()
    if not feed_entries:
        add_log("warning", "RSS feed returned no entries")
    seen_slugs: set[str] = set()
    for entry in feed_entries:
        title = entry.get("title", "Untitled")
        link_value = entry.get("link")
        if not link_value:
            skipped += 1
            add_log("warning", f"Skipping entry without link: {title}")
            continue
        url = canonical_bungie_url(link_value)
        slug = slug_from_url(url)
        if slug in seen_slugs:
            skipped += 1
            continue
        seen_slugs.add(slug)
        try:
            previous = conn.execute("SELECT content_hash FROM articles WHERE slug = ?", (slug,)).fetchone()
            html = fetch_article_html(url)
            upsert_article(conn, title=title, url=url, published_at=parse_feed_date(entry), html=html)
            normalized_text = normalize_line(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))
            current_hash = sha256(normalized_text.encode("utf-8")).hexdigest()
            if previous and previous["content_hash"] == current_hash:
                unchanged += 1
            imported += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            add_log("error", f"Failed to import article: {title}", f"url={url}\nerror={exc}")

    conn.commit()
    conn.close()
    add_log("info", f"RSS sync complete: imported={imported}, skipped={skipped}, unchanged={unchanged}, failed={failed}")
    return {"imported": imported, "skipped": skipped, "unchanged": unchanged, "failed": failed}


def parse_search_terms(query: str) -> tuple[list[str], list[str]]:
    quoted = [normalize_line(match) for match in re.findall(r'"([^"]+)"', query)]
    cleaned = re.sub(r'"[^"]+"', " ", query)
    keywords = [normalize_line(part) for part in re.split(r"[\s,]+", cleaned) if normalize_line(part)]
    return [term for term in quoted if term], [term for term in keywords if term]


def parse_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def find_latest(conn: sqlite3.Connection, article_type: str):
    return conn.execute(
        "SELECT * FROM articles WHERE article_type = ? ORDER BY published_at DESC NULLS LAST, id DESC LIMIT 1",
        (article_type,),
    ).fetchone()


def find_matches(patch_id: int, twid_id: int, threshold: int = 82) -> list[sqlite3.Row]:
    conn = get_db()
    patch_rows = conn.execute(
        "SELECT id, section, item_text, normalized_item FROM extracted_items WHERE article_id = ?",
        (patch_id,),
    ).fetchall()
    twid_rows = conn.execute(
        "SELECT id, section, item_text, normalized_item FROM extracted_items WHERE article_id = ?",
        (twid_id,),
    ).fetchall()
    if not patch_rows or not twid_rows:
        patch_article = conn.execute("SELECT content_markdown FROM articles WHERE id = ?", (patch_id,)).fetchone()
        twid_article = conn.execute("SELECT content_markdown FROM articles WHERE id = ?", (twid_id,)).fetchone()
        patch_lines = [line for line in (patch_article["content_markdown"] if patch_article else "").splitlines() if len(normalize_line(line)) > 20]
        twid_lines = [line for line in (twid_article["content_markdown"] if twid_article else "").splitlines() if len(normalize_line(line)) > 20]
        patch_rows = [{"section": "Body", "item_text": line, "normalized_item": normalize_line(line)} for line in patch_lines]
        twid_rows = [{"section": "Body", "item_text": line, "normalized_item": normalize_line(line)} for line in twid_lines]
    results = []
    for patch in patch_rows:
        best_score = 0
        best = None
        for twid in twid_rows:
            score = similarity_score(patch["normalized_item"], twid["normalized_item"])
            if score > best_score:
                best_score = score
                best = twid
        results.append(
            {
                "patch_item": patch["item_text"],
                "patch_section": patch["section"],
                "best_twid_item": best["item_text"] if best else None,
                "score": best_score,
                "teased": best_score >= threshold,
            }
        )
    conn.close()
    return results


@app.route("/")
def index():
    conn = get_db()
    latest_twid = find_latest(conn, "twid")
    latest_patch = find_latest(conn, "patch")
    recent_articles = conn.execute(
        "SELECT id, slug, article_type, title, url, published_at FROM articles ORDER BY published_at DESC NULLS LAST, id DESC LIMIT 30"
    ).fetchall()
    conn.close()
    return render_template(
        "index.html",
        latest_twid=latest_twid,
        latest_patch=latest_patch,
        recent_articles=recent_articles,
        imported=request.args.get("imported"),
        skipped=request.args.get("skipped"),
        unchanged=request.args.get("unchanged"),
        failed=request.args.get("failed"),
        sync_error=request.args.get("sync_error"),
    )


@app.route("/sync")
def sync():
    try:
        result = sync_from_rss()
        return redirect(
            url_for(
                "index",
                imported=result["imported"],
                skipped=result["skipped"],
                unchanged=result["unchanged"],
                failed=result["failed"],
            )
        )
    except Exception as exc:  # noqa: BLE001
        details = traceback.format_exc()
        add_log("error", "Sync endpoint failed unexpectedly", f"{exc}\n{details}")
        return redirect(url_for("index", sync_error=str(exc)))


@app.route("/compare")
def compare_latest():
    conn = get_db()
    latest_twid = find_latest(conn, "twid")
    latest_patch = find_latest(conn, "patch")
    twids = conn.execute(
        "SELECT id, title, published_at FROM articles WHERE article_type = 'twid' ORDER BY published_at DESC NULLS LAST, id DESC LIMIT 100"
    ).fetchall()
    patches = conn.execute(
        "SELECT id, title, published_at FROM articles WHERE article_type = 'patch' ORDER BY published_at DESC NULLS LAST, id DESC LIMIT 100"
    ).fetchall()
    twid_id = parse_int(request.args.get("twid_id"), latest_twid["id"] if latest_twid else 0)
    patch_id = parse_int(request.args.get("patch_id"), latest_patch["id"] if latest_patch else 0)
    selected_twid = conn.execute("SELECT * FROM articles WHERE id = ?", (twid_id,)).fetchone() if twid_id else latest_twid
    selected_patch = conn.execute("SELECT * FROM articles WHERE id = ?", (patch_id,)).fetchone() if patch_id else latest_patch
    conn.close()
    if not selected_twid or not selected_patch:
        return render_template("compare.html", error="Need at least one TWID and one Patch article."), 400

    threshold = int(request.args.get("threshold", 82))
    results = find_matches(selected_patch["id"], selected_twid["id"], threshold=threshold)
    teased_count = sum(1 for row in results if row["teased"])
    return render_template(
        "compare.html",
        results=results,
        twid=selected_twid,
        patch=selected_patch,
        twids=twids,
        patches=patches,
        threshold=threshold,
        teased_count=teased_count,
    )


@app.route("/search")
def search():
    query = request.args.get("q", "").strip()
    rows: Iterable[sqlite3.Row] = []
    if query:
        phrases, keywords = parse_search_terms(query)
        if phrases or keywords:
            where_parts: list[str] = []
            params: list[str] = []
            for phrase in phrases:
                where_parts.append("(a.content_normalized LIKE ? OR lower(a.title) LIKE ?)")
                params.extend([f"%{phrase}%", f"%{phrase}%"])
            for keyword in keywords:
                where_parts.append("(a.content_normalized LIKE ? OR lower(a.title) LIKE ?)")
                params.extend([f"%{keyword}%", f"%{keyword}%"])
            where = " OR ".join(where_parts)
            conn = get_db()
            rows = conn.execute(
                f"""
                SELECT
                    a.id,
                    a.slug,
                    a.title,
                    a.url,
                    a.article_type,
                    a.published_at,
                    (
                      SELECT COUNT(*)
                      FROM article_versions v
                      WHERE v.article_id = a.id
                    ) AS version_count,
                    (
                      SELECT COUNT(*)
                      FROM article_versions v
                      WHERE v.article_id = a.id AND v.is_changed = 1
                    ) AS changed_versions,
                    substr(a.content_markdown, 1, 450) AS excerpt
                FROM articles a
                WHERE {where}
                ORDER BY a.published_at DESC NULLS LAST
                LIMIT 200
                """,
                params,
            ).fetchall()
            conn.close()
    return render_template("search.html", query=query, rows=rows)


@app.route("/history/<slug>")
def history(slug: str):
    conn = get_db()
    article = conn.execute("SELECT id, title, url FROM articles WHERE slug = ?", (slug,)).fetchone()
    if not article:
        conn.close()
        return render_template("error.html", error=f"No article found for slug '{slug}'"), 404
    versions = conn.execute(
        """
        SELECT fetched_at, published_at, content_hash, is_changed
        FROM article_versions
        WHERE article_id = ?
        ORDER BY fetched_at DESC
        """,
        (article["id"],),
    ).fetchall()
    conn.close()
    return render_template("history.html", article=article, versions=versions)


@app.route("/article/<int:article_id>")
def article_detail(article_id: int):
    conn = get_db()
    article = conn.execute(
        "SELECT id, title, url, article_type, published_at, content_markdown FROM articles WHERE id = ?",
        (article_id,),
    ).fetchone()
    conn.close()
    if not article:
        return render_template("error.html", error=f"No article found for id '{article_id}'"), 404
    return render_template("article.html", article=article)


@app.route("/healthz")
def healthz():
    return {"status": "ok"}, 200


@app.route("/logs")
def logs():
    include_404 = request.args.get("include_404", "0") == "1"
    conn = get_db()
    if include_404:
        rows = conn.execute("SELECT created_at, level, message, details FROM app_logs ORDER BY id DESC LIMIT 300").fetchall()
    else:
        rows = conn.execute(
            """
            SELECT created_at, level, message, details
            FROM app_logs
            WHERE NOT (
                message = 'Unhandled application exception'
                AND coalesce(details, '') LIKE '%werkzeug.exceptions.NotFound%'
            )
            ORDER BY id DESC
            LIMIT 300
            """
        ).fetchall()
    conn.close()
    return render_template("logs.html", rows=rows, log_path=str(LOG_PATH), include_404=include_404)


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.errorhandler(HTTPException)
def handle_http_exception(exc: HTTPException):
    if exc.code == 404:
        return render_template("error.html", error=f"Not found: {request.path}"), 404
    return exc


@app.errorhandler(Exception)
def handle_unexpected_error(exc: Exception):
    add_log("error", "Unhandled application exception", traceback.format_exc())
    return render_template("error.html", error=str(exc)), 500


setup_logging()
init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
