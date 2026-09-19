"""Listing monitor: watch saved searches on an authenticated site and alert once per new listing.

Design rules this file exists to demonstrate:
  1. The logged-in session is saved to disk (Playwright storage state) and reused; a redirect to the
     login page is the only trigger for re-authenticating.
  2. A listing is keyed by the id parsed from its URL, stored in SQLite. The row is inserted BEFORE
     the alert is sent, so a crash between the two can never produce a duplicate alert.
  3. Every parsed listing is validated against a required-field schema. If a page that normally has
     rows returns nothing, or required fields vanish, the run reports a broken parser instead of
     quietly reporting "no new listings".
  4. Selectors live in the config file, not in the code, so a site redesign is a config edit.

Run one pass:      python3 monitor.py --config config.quotes.json
Run continuously:  python3 monitor.py --config config.quotes.json --loop
Dry run (no send): set no alert credentials; alerts are appended to alerts.log instead.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from _env import load_env_file

load_env_file()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message: str) -> None:
    print(f"[{now_iso()}] {message}", flush=True)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class Config:
    name: str
    base_url: str
    searches: list[str]
    selectors: dict
    required_fields: list[str]
    min_items_per_page: int = 1
    login: dict = field(default_factory=dict)
    filters: dict = field(default_factory=dict)
    alerts: dict = field(default_factory=dict)
    storage_state_path: str = "storage_state.json"
    database_path: str = "seen.sqlite3"
    interval_seconds: int = 900
    jitter_seconds: int = 180
    page_delay_seconds: float = 2.0

    @staticmethod
    def load(path: str) -> "Config":
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        known = {f for f in Config.__dataclass_fields__}
        unknown = set(raw) - known
        if unknown:
            raise SystemExit(f"unknown config keys: {sorted(unknown)}")
        return Config(**raw)


# ---------------------------------------------------------------------------
# Storage: one row per listing, inserted before any alert is sent
# ---------------------------------------------------------------------------


class SeenStore:
    def __init__(self, path: str) -> None:
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_listing (
                listing_id TEXT PRIMARY KEY,
                url        TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                alerted_at TEXT,
                payload    TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS run_health (
                started_at   TEXT PRIMARY KEY,
                items_found  INTEGER NOT NULL,
                parser_ok    INTEGER NOT NULL,
                note         TEXT
            )
            """
        )
        self.connection.commit()

    def claim(self, listing: dict) -> bool:
        """Insert the listing. Returns True only for a row this process actually created,
        so two runs racing on the same listing still produce exactly one alert."""
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO seen_listing (listing_id, url, first_seen, payload) VALUES (?, ?, ?, ?)",
            (listing["id"], listing["url"], now_iso(), json.dumps(listing, ensure_ascii=False)),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def mark_alerted(self, listing_id: str) -> None:
        self.connection.execute(
            "UPDATE seen_listing SET alerted_at = ? WHERE listing_id = ?", (now_iso(), listing_id)
        )
        self.connection.commit()

    def pending_alerts(self) -> list[dict]:
        """Rows inserted but never alerted, which is what a crash between insert and send leaves behind."""
        rows = self.connection.execute(
            "SELECT payload FROM seen_listing WHERE alerted_at IS NULL ORDER BY first_seen"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def record_health(self, items_found: int, parser_ok: bool, note: str = "") -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO run_health (started_at, items_found, parser_ok, note) VALUES (?, ?, ?, ?)",
            (now_iso(), items_found, 1 if parser_ok else 0, note),
        )
        self.connection.commit()

    def count(self) -> int:
        return self.connection.execute("SELECT COUNT(*) FROM seen_listing").fetchone()[0]


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


def send_alert(config: Config, text: str) -> str:
    """Telegram when configured, otherwise append to alerts.log so a demo run still proves the path."""
    token = os.environ.get(config.alerts.get("telegram_token_env", ""), "")
    chat_id = os.environ.get(config.alerts.get("telegram_chat_env", ""), "")
    if token and chat_id:
        payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=payload
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()
        return "telegram"
    fallback = config.alerts.get("fallback_file", "alerts.log")
    with open(fallback, "a", encoding="utf-8") as handle:
        handle.write(f"[{now_iso()}] {text}\n")
    return fallback


# ---------------------------------------------------------------------------
# Session handling
# ---------------------------------------------------------------------------


def looks_logged_out(page, config: Config) -> bool:
    marker = config.login.get("logged_out_selector")
    if not marker:
        return False
    return page.locator(marker).count() > 0


def log_in(page, config: Config) -> None:
    login = config.login
    if not login:
        return
    username = os.environ.get(login.get("username_env", ""), "")
    password = os.environ.get(login.get("password_env", ""), "")
    if not username or not password:
        raise SystemExit(
            f"set {login.get('username_env')} and {login.get('password_env')} in the environment"
        )
    log("session: logging in")
    page.goto(urllib.parse.urljoin(config.base_url, login["url"]), wait_until="domcontentloaded")
    page.fill(login["username_selector"], username)
    page.fill(login["password_selector"], password)
    page.click(login["submit_selector"])
    page.wait_for_load_state("domcontentloaded")
    if looks_logged_out(page, config):
        raise SystemExit("login failed: still seeing the logged out marker")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def listing_id_from_url(url: str, pattern: str | None) -> str:
    if pattern:
        import re

        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return urllib.parse.urlparse(url).path.strip("/").replace("/", ":")


def parse_page(page, config: Config) -> list[dict]:
    # Links are relative to the page they were found on, not to base_url.
    selectors = config.selectors
    items = []
    rows = page.locator(selectors["item"])
    for index in range(rows.count()):
        row = rows.nth(index)
        listing: dict = {}
        for field_name, selector in selectors.get("fields", {}).items():
            node = row.locator(selector)
            listing[field_name] = node.inner_text().strip() if node.count() else None
        link = row.locator(selectors["link"])
        href = link.get_attribute("href") if link.count() else None
        if href:
            listing["url"] = urllib.parse.urljoin(page.url, href)
            listing["id"] = listing_id_from_url(listing["url"], selectors.get("id_pattern"))
        items.append(listing)
    return items


def validate(listing: dict, config: Config) -> list[str]:
    missing = [name for name in config.required_fields if not listing.get(name)]
    return missing


def passes_filters(listing: dict, config: Config) -> bool:
    filters = config.filters or {}
    contains = filters.get("text_contains")
    if contains:
        haystack = " ".join(str(value) for value in listing.values() if value).lower()
        if contains.lower() not in haystack:
            return False
    minimum_length = filters.get("min_text_length")
    if minimum_length:
        text = str(listing.get(filters.get("length_field", "title")) or "")
        if len(text) < int(minimum_length):
            return False
    return True


# ---------------------------------------------------------------------------
# One pass
# ---------------------------------------------------------------------------


def run_once(config: Config, headless: bool = True, video_dir: str = "") -> dict:
    store = SeenStore(config.database_path)
    summary = {"pages": 0, "found": 0, "new": 0, "filtered": 0, "invalid": 0, "parser_ok": True}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        state = config.storage_state_path if os.path.exists(config.storage_state_path) else None
        context_options: dict = {"storage_state": state, "viewport": {"width": 1280, "height": 800}}
        if video_dir:
            context_options["record_video_dir"] = video_dir
            context_options["record_video_size"] = {"width": 1280, "height": 800}
        context = browser.new_context(**context_options)
        page = context.new_page()

        if config.login:
            page.goto(urllib.parse.urljoin(config.base_url, config.searches[0]), wait_until="domcontentloaded")
            if looks_logged_out(page, config):
                log("session: storage state is stale")
                log_in(page, config)
                context.storage_state(path=config.storage_state_path)
                log(f"session: saved to {config.storage_state_path}")
            else:
                log("session: reused saved storage state")

        for search_path in config.searches:
            url = urllib.parse.urljoin(config.base_url, search_path)
            log(f"page: {url}")
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector(config.selectors["item"], timeout=10000)
            except PlaywrightTimeoutError:
                summary["parser_ok"] = False
                log("parser: expected rows never appeared")
                continue
            summary["pages"] += 1
            listings = parse_page(page, config)
            summary["found"] += len(listings)
            if len(listings) < config.min_items_per_page:
                summary["parser_ok"] = False
                log(f"parser: only {len(listings)} rows, expected at least {config.min_items_per_page}")
                continue

            for listing in listings:
                missing = validate(listing, config)
                if missing:
                    summary["invalid"] += 1
                    log(f"parser: listing missing {missing}")
                    continue
                if not passes_filters(listing, config):
                    summary["filtered"] += 1
                    continue
                if store.claim(listing):
                    summary["new"] += 1
            time.sleep(config.page_delay_seconds)

        context.close()
        browser.close()

    if summary["invalid"] and summary["found"] and summary["invalid"] == summary["found"]:
        summary["parser_ok"] = False

    store.record_health(summary["found"], summary["parser_ok"])

    if not summary["parser_ok"]:
        channel = send_alert(
            config,
            f"{config.name}: parser looks broken. Pages fetched but rows or required fields are missing. "
            f"Found {summary['found']} rows across {summary['pages']} pages. No listings were marked as seen.",
        )
        log(f"alert: parser warning sent via {channel}")
        return summary

    # Alerts are sent from the table, so a crash between insert and send is recovered on the next run.
    for listing in store.pending_alerts():
        text = "\n".join(
            [f"{config.name}: new listing"]
            + [f"{key}: {value}" for key, value in listing.items() if key != "id" and value]
        )
        channel = send_alert(config, text)
        store.mark_alerted(listing["id"])
        log(f"alert: {listing['id']} sent via {channel}")

    log(
        f"pass done: pages={summary['pages']} found={summary['found']} new={summary['new']} "
        f"filtered={summary['filtered']} invalid={summary['invalid']} total_seen={store.count()}"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor saved searches and alert once per new listing")
    parser.add_argument("--config", required=True)
    parser.add_argument("--loop", action="store_true", help="keep running on the configured interval")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--video", default="", help="directory to record the run into")
    args = parser.parse_args()

    config = Config.load(args.config)
    if not args.loop:
        summary = run_once(config, headless=not args.headed, video_dir=args.video)
        return 0 if summary["parser_ok"] else 2

    while True:
        try:
            run_once(config, headless=not args.headed, video_dir=args.video)
        except Exception as error:  # a bad run must not kill the watcher
            log(f"run failed: {error!r}")
            send_alert(config, f"{config.name}: run failed: {error!r}")
        delay = config.interval_seconds + random.randint(0, config.jitter_seconds)
        log(f"sleeping {delay}s")
        time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
