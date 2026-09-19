"""Is the saved session still signed in? Opens one page, types nothing, changes nothing.

    python3 status.py --config config.quotes.json

Exit code 0 when signed in, 1 when signed out or when there is no session file yet, so a timer can
re-run the login before a collection starts.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import sys
import urllib.parse

from playwright.sync_api import sync_playwright

from _env import load_env_file

load_env_file()


def main() -> int:
    parser = argparse.ArgumentParser(description="Report whether the saved session is still signed in")
    parser.add_argument("--config", required=True)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--video", default="", help="directory to record the check into")
    args = parser.parse_args()

    config = json.loads(pathlib.Path(args.config).read_text())
    state_path = pathlib.Path(config.get("storage_state_path", "storage_state.json"))
    login = config.get("login") or {}
    marker = login.get("logged_out_selector")
    check_url = urllib.parse.urljoin(config["base_url"], config.get("status_path", config["searches"][0]))

    print(f"session file  {state_path}")
    if not state_path.exists():
        print("signed in     NO, there is no session file yet")
        print("              run: python3 login.py --config " + args.config)
        return 1

    age = datetime.datetime.now() - datetime.datetime.fromtimestamp(state_path.stat().st_mtime)
    hours = age.total_seconds() / 3600
    cookies = json.loads(state_path.read_text()).get("cookies", [])
    print(f"written       {hours:.1f} hours ago, {len(cookies)} cookies")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headed)
        options: dict = {"storage_state": str(state_path), "viewport": {"width": 1280, "height": 800}}
        if args.video:
            options["record_video_dir"] = args.video
            options["record_video_size"] = {"width": 1280, "height": 800}
        context = browser.new_context(**options)
        page = context.new_page()
        page.goto(check_url, wait_until="domcontentloaded")
        signed_out = bool(marker) and page.locator(marker).count() > 0
        print(f"checked       {page.url}")
        print(f"signed in     {'NO' if signed_out else 'YES'}")
        if signed_out:
            print("              the site is showing the signed-out view; run login.py again")
        context.close()
        browser.close()
    return 1 if signed_out else 0


if __name__ == "__main__":
    sys.exit(main())
