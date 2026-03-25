from __future__ import annotations

import logging
import os
import re
import sqlite3
import traceback
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

BASE_DIR = Path(__file__).parent
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "data.sqlite3")))
RSS_URL = os.getenv("BUNGIE_RSS_URL", "https://www.bungie.net/7/en/News/rss")
RSS_BASE_URL = os.getenv("BUNGIE_BASE_URL", "https://www.bungie.net")
RSS_MAX_PAGES = int(os.getenv("RSS_MAX_PAGES", "3"))
PORT = int(os.getenv("PORT", "7777"))
LOG_PATH = Path(os.getenv("LOG_PATH", str(BASE_DIR / "logs" / "app.log")))

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
        """
    )
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
    resp = requests.get(url, timeout=30)
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
        feed = feedparser.parse(next_url)
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

    if article_type == "twid":
        target_sections = {"known issues", "patch preview", "patch notes preview", "sandbox preview"}
    elif article_type == "patch":
        target_sections = {"combatant", "abilities", "general", "weapons", "armor", "activities", "fixes"}
    else:
        return []

    output: list[tuple[str, str, str]] = []
    headings = soup.find_all(re.compile(r"^h[1-4]$"))
    for heading in headings:
        heading_text = normalize_line(heading.get_text(" ", strip=True))
        if article_type == "twid":
            if not any(section in heading_text for section in target_sections):
                continue
            section_name = heading.get_text(" ", strip=True)
            sibling = heading.find_next_sibling()
            while sibling and sibling.name not in ["h1", "h2", "h3", "h4"]:
                for li in sibling.find_all("li"):
                    item = li.get_text(" ", strip=True)
                    if len(item) > 10:
                        output.append((section_name, item, normalize_line(item)))
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


def upsert_article(conn: sqlite3.Connection, *, title: str, url: str, published_at: str | None, html: str) -> int:
    slug = slug_from_url(url)
    article_type = classify_article(url, title)
    fetched_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO articles(slug, article_type, title, url, published_at, content_html, fetched_at)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(slug)
        DO UPDATE SET
          title=excluded.title,
          article_type=excluded.article_type,
          url=excluded.url,
          published_at=excluded.published_at,
          content_html=excluded.content_html,
          fetched_at=excluded.fetched_at
        """,
        (slug, article_type, title, url, published_at, html, fetched_at),
    )
    row = conn.execute("SELECT id FROM articles WHERE slug = ?", (slug,)).fetchone()
    assert row is not None

    conn.execute("DELETE FROM extracted_items WHERE article_id = ?", (row["id"],))
    for section, item, normalized in extract_section_items(html, article_type):
        conn.execute(
            "INSERT INTO extracted_items(article_id, section, item_text, normalized_item) VALUES(?,?,?,?)",
            (row["id"], section, item, normalized),
        )
    return int(row["id"])


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
    failed = 0
    feed_entries = gather_feed_entries()
    if not feed_entries:
        add_log("warning", "RSS feed returned no entries")
    for entry in feed_entries:
        title = entry.get("title", "Untitled")
        link_value = entry.get("link")
        if not link_value:
            skipped += 1
            add_log("warning", f"Skipping entry without link: {title}")
            continue
        url = canonical_bungie_url(link_value)
        article_type = classify_article(url, title)
        if article_type == "other":
            skipped += 1
            continue
        try:
            html = fetch_article_html(url)
            upsert_article(conn, title=title, url=url, published_at=parse_feed_date(entry), html=html)
            imported += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            add_log("error", f"Failed to import article: {title}", f"url={url}\nerror={exc}")

    conn.commit()
    conn.close()
    add_log("info", f"RSS sync complete: imported={imported}, skipped={skipped}, failed={failed}")
    return {"imported": imported, "skipped": skipped, "failed": failed}


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
        "SELECT id, article_type, title, url, published_at FROM articles ORDER BY published_at DESC NULLS LAST, id DESC LIMIT 30"
    ).fetchall()
    conn.close()
    return render_template(
        "index.html",
        latest_twid=latest_twid,
        latest_patch=latest_patch,
        recent_articles=recent_articles,
        imported=request.args.get("imported"),
        skipped=request.args.get("skipped"),
        failed=request.args.get("failed"),
        sync_error=request.args.get("sync_error"),
    )


@app.route("/sync")
def sync():
    try:
        result = sync_from_rss()
        return redirect(
            url_for("index", imported=result["imported"], skipped=result["skipped"], failed=result["failed"])
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
    conn.close()
    if not latest_twid or not latest_patch:
        return render_template("compare.html", error="Need at least one TWID and one Patch article."), 400

    threshold = int(request.args.get("threshold", 82))
    results = find_matches(latest_patch["id"], latest_twid["id"], threshold=threshold)
    teased_count = sum(1 for row in results if row["teased"])
    return render_template(
        "compare.html",
        results=results,
        twid=latest_twid,
        patch=latest_patch,
        threshold=threshold,
        teased_count=teased_count,
    )


@app.route("/search")
def search():
    query = request.args.get("q", "").strip()
    rows: Iterable[sqlite3.Row] = []
    if query:
        conn = get_db()
        rows = conn.execute(
            """
            SELECT a.title, a.url, a.article_type, a.published_at, e.section, e.item_text
            FROM extracted_items e
            JOIN articles a ON a.id = e.article_id
            WHERE e.normalized_item LIKE ?
            ORDER BY a.published_at DESC NULLS LAST
            LIMIT 200
            """,
            (f"%{normalize_line(query)}%",),
        ).fetchall()
        conn.close()
    return render_template("search.html", query=query, rows=rows)


@app.route("/healthz")
def healthz():
    return {"status": "ok"}, 200


@app.route("/logs")
def logs():
    conn = get_db()
    rows = conn.execute("SELECT created_at, level, message, details FROM app_logs ORDER BY id DESC LIMIT 300").fetchall()
    conn.close()
    return render_template("logs.html", rows=rows, log_path=str(LOG_PATH))


@app.errorhandler(Exception)
def handle_unexpected_error(exc):  # noqa: ANN001
    add_log("error", "Unhandled application exception", traceback.format_exc())
    return render_template("error.html", error=str(exc)), 500


setup_logging()
init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
