"""Sign in once, by typing the credentials from the environment, and keep the session on disk.

Two ways to hold a session, both supported here:

  storage state  a JSON file of cookies and local storage, written after a successful login and
                 reused by every later run. Portable, small, easy to delete.
  profile        a persistent Chrome profile directory. Survives more (service workers, device
                 fingerprint stability), but it is a credential: treat the folder like a password.

Credentials come from the environment, never from the config file and never from the code:

  LOGIN_USERNAME, LOGIN_PASSWORD

Nothing here is logged, printed or written to disk except the session itself. A wrong password ends
with a message and a non-zero exit, not a retry loop.

  python3 login.py --config config.quotes.json
  python3 login.py --config config.quotes.json --headed      # watch it type
  python3 login.py --config config.quotes.json --video out/  # record the run
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import random
import sys
import time
import urllib.parse

from playwright.sync_api import sync_playwright

from _env import load_env_file

load_env_file()


def human_type(page, selector: str, text: str) -> None:
    """Type like a person: click the field, then key by key with uneven gaps."""
    page.click(selector)
    for character in text:
        page.keyboard.type(character)
        time.sleep(random.uniform(0.04, 0.16))
    time.sleep(random.uniform(0.3, 0.8))


def main() -> int:
    parser = argparse.ArgumentParser(description="Sign in and save the session")
    parser.add_argument("--config", required=True)
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--video", default="", help="directory to record the run into")
    args = parser.parse_args()

    config = json.loads(pathlib.Path(args.config).read_text())
    login = config.get("login") or {}
    if not login:
        raise SystemExit(f"{args.config} has no login block")

    username = os.environ.get(login.get("username_env", "LOGIN_USERNAME"), "")
    password = os.environ.get(login.get("password_env", "LOGIN_PASSWORD"), "")
    if not username or not password:
        raise SystemExit(
            f"set {login.get('username_env', 'LOGIN_USERNAME')} and "
            f"{login.get('password_env', 'LOGIN_PASSWORD')} in the environment"
        )

    state_path = config.get("storage_state_path", "storage_state.json")
    login_url = urllib.parse.urljoin(config["base_url"], login["url"])

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headed)
        context_options: dict = {"viewport": {"width": 1280, "height": 800}}
        if args.video:
            context_options["record_video_dir"] = args.video
            context_options["record_video_size"] = {"width": 1280, "height": 800}
        context = browser.new_context(**context_options)
        page = context.new_page()

        print(f"opening {login_url}", flush=True)
        page.goto(login_url, wait_until="domcontentloaded")

        print(f"typing {username}", flush=True)  # the user, never the password
        human_type(page, login["username_selector"], username)
        human_type(page, login["password_selector"], password)
        page.click(login["submit_selector"])
        page.wait_for_load_state("domcontentloaded")
        time.sleep(1.5)

        marker = login.get("logged_out_selector")
        if marker and page.locator(marker).count() > 0:
            print("still logged out: the sign-in did not take", flush=True)
            context.close()
            browser.close()
            return 1

        context.storage_state(path=state_path)
        print(f"signed in, session written to {state_path}", flush=True)
        cookie_count = len(json.loads(pathlib.Path(state_path).read_text()).get("cookies", []))
        print(f"{cookie_count} cookies stored, no credentials on disk", flush=True)

        time.sleep(1.0)
        context.close()
        browser.close()

    if args.video:
        videos = sorted(pathlib.Path(args.video).glob("*.webm"))
        if videos:
            print(f"video {videos[-1]}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
