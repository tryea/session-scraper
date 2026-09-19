# session-scraper

A small watcher for a site you have an account on. It signs in, polls the pages you name, alerts once
per item it has never seen, and tells you when it is broken instead of quietly reporting that nothing
is new.

One Python file for the watcher, one for the login, one JSON config per site. SQLite for state. No
framework, no dashboard, no queue server.

## Proof, not promises

![Signing in, finding 31 new listings, finding zero on the second pass, then raising a parser alarm when the selectors are renamed](proof/session-scraper-proof.gif)

That is the real thing running against public scraping sandboxes, sped up slightly. The full speed
recording is [`proof/session-scraper-proof.mp4`](proof/session-scraper-proof.mp4) and the console
output of the same run is [`proof/run-log.txt`](proof/run-log.txt).

| Time | What you are watching |
| --- | --- |
| 0:00 | signing in, the password typed key by key from an environment variable |
| 0:07 | first pass, 31 listings found and 31 alerts sent |
| 0:12 | second pass over the same pages, 31 found, **zero** new, zero alerts |
| 0:17 | the row selector renamed to simulate a site redesign: the run raises a parser alarm and marks nothing as seen |

```
=== PASS 1: empty database
pass done: pages=2 found=31 new=31 filtered=0 invalid=0 total_seen=31

=== PASS 2: same pages again
pass done: pages=2 found=31 new=0 filtered=0 invalid=0 total_seen=31

=== PASS 3: selectors renamed, as if the site redesigned
parser: expected rows never appeared
alert: parser warning sent via alerts_broken.log
```

## What it gets right

**The session is a file, not a password in code.** `login.py` types the credentials from
`LOGIN_USERNAME` and `LOGIN_PASSWORD`, then writes Playwright storage state to disk. Every later run
reuses that file. The password is never logged, never printed, never stored.

**Alert once, ever.** The item id parsed from its URL is the key in SQLite. The row is inserted
before the alert is sent, and only an insert that actually created a row triggers one, so two
overlapping runs cannot both announce the same item and a crash between insert and send is recovered
on the next pass.

**Silence is a bug.** Every item is validated against a required-field list, and a page that normally
has rows returning none raises a parser alarm and marks nothing as seen. Exit code 2, so a timer or a
supervisor can act on it.

**Selectors are config.** A site redesign is a config edit, not a code change.

**Polite by default.** One page at a time, a delay between pages, a randomised interval between
passes, and no attempt to get around a CAPTCHA or any other challenge. Use it on sites whose terms
allow it, with an account you own.

## Run it

```bash
pip install playwright
python3 -m playwright install chromium

# a sandbox with a login form
export LOGIN_USERNAME=demo LOGIN_PASSWORD=demo123
python3 login.py   --config config.quotes.json
python3 monitor.py --config config.quotes.json

# a sandbox without one
python3 monitor.py --config config.books.json
python3 monitor.py --config config.books.json --loop     # keep watching
python3 monitor.py --config config.books.json --video out/   # record the run
```

Alerts go to Telegram when `MONITOR_TELEGRAM_TOKEN` and `MONITOR_TELEGRAM_CHAT` are set, otherwise to
the fallback file named in the config.

## Config

| Key | Meaning |
| --- | --- |
| `searches` | the result pages to poll |
| `selectors.item` | the row element for one item |
| `selectors.link` | the anchor inside the row pointing at the item |
| `selectors.id_pattern` | regex whose first group is the stable id in the URL |
| `selectors.fields` | field name to selector, relative to the row |
| `required_fields` | an item missing any of these is reported, not stored |
| `min_items_per_page` | fewer rows than this means the parser is treated as broken |
| `login` | urls, selectors, and the names of the environment variables holding the credentials |
| `filters` | text and length filters applied before alerting |
| `interval_seconds`, `jitter_seconds`, `page_delay_seconds` | polite polling |

`config.books.json` and `config.quotes.json` are working examples against
[books.toscrape.com](https://books.toscrape.com) and [quotes.toscrape.com](https://quotes.toscrape.com),
two sites published for exactly this kind of practice.

## Licence

MIT.
