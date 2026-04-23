#!/usr/bin/env python3
"""
Open House Finder — scrapes Redfin open houses for a neighborhood
and stores results in a local SQLite database.

Usage:
    python main.py "https://www.redfin.com/neighborhood/3040/WA/Seattle/West-Seattle/filter/open-house=anytime"
    python main.py "Ballard, Seattle, WA"
    python main.py              # will prompt you
"""

import sys
import sqlite3
import argparse
import random
import time
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

DB_FILE = 'openhouses.db'

# Injected into every page before any scripts run — removes the most common
# bot-detection signals that sites check for.
STEALTH_JS = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins',   { get: () => [1,2,3,4,5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
    window.chrome = { runtime: {} };
    const _query = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (p) =>
        p.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : _query(p);
"""


def pause(min_s=1.5, max_s=3.5):
    """Random human-paced sleep."""
    time.sleep(random.uniform(min_s, max_s))


def long_pause(min_s=4.0, max_s=9.0):
    """Occasional longer break so the request pattern doesn't look robotic."""
    secs = random.uniform(min_s, max_s)
    print(f'  (pausing {secs:.0f}s…)')
    time.sleep(secs)


def human_scroll(page):
    """Scroll in random increments to mimic a person reading down the page."""
    total = page.evaluate('document.body.scrollHeight')
    pos   = 0
    while pos < total:
        step = random.randint(250, 700)
        pos  = min(pos + step, total)
        page.evaluate(f'window.scrollTo(0, {pos})')
        time.sleep(random.uniform(0.08, 0.25))
    # Pause at the bottom as if reading
    pause(1.0, 2.5)
    # Scroll back up a little — real users do this
    page.evaluate(f'window.scrollTo(0, {int(total * random.uniform(0.7, 0.9))})')
    pause(0.5, 1.2)


def scroll_for_cards(page):
    """Scroll repeatedly until no new listing cards appear (infinite scroll)."""
    prev = 0
    for _ in range(30):
        human_scroll(page)
        cards = page.locator('.HomeCard, [data-rf-test-name="mapHomeCard"]').all()
        if len(cards) == prev:
            break
        prev = len(cards)
    return prev


def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS openhouses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            address     TEXT,
            agent_name  TEXT,
            open_date   TEXT,
            open_time   TEXT,
            listing_url TEXT UNIQUE,
            scraped_at  TEXT
        )
    ''')
    conn.commit()
    return conn


def upsert(conn, row):
    conn.execute('''
        INSERT INTO openhouses (address, agent_name, open_date, open_time, listing_url, scraped_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(listing_url) DO UPDATE SET
            agent_name = excluded.agent_name,
            open_date  = excluded.open_date,
            open_time  = excluded.open_time,
            scraped_at = excluded.scraped_at
    ''', row)
    conn.commit()


def dismiss_popups(page):
    """Close any modal or overlay that might be blocking the page."""
    selectors = [
        'button[aria-label="Close"]',
        'button[aria-label="close"]',
        'button[aria-label="Dismiss"]',
        '[data-rf-test-name="modal-close-button"]',
        '.modal-close-button',
        'button.close',
        'button:has-text("✕")',
        'button:has-text("×")',
        'button:has-text("No thanks")',
        'button:has-text("Not now")',
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=600):
                btn.click()
                time.sleep(0.4)
        except Exception:
            pass


def address_from_url(url):
    """Parse a human-readable address out of a Redfin listing URL.
    Handles both /street/home/id and /street/unit-X/home/id patterns.
    """
    parts = url.rstrip('/').split('/')
    try:
        home_idx = parts.index('home')
        street_part = parts[home_idx - 1]
        # If the segment before 'home' looks like a unit (unit-A, unit-3C…), back up one more
        if street_part.lower().startswith('unit'):
            unit    = street_part.replace('-', ' ')
            raw     = parts[home_idx - 2]
            city    = parts[home_idx - 3]
            state   = parts[home_idx - 4]
            return f'{raw.replace("-", " ")}, {unit}, {city}, {state}'
        else:
            city  = parts[home_idx - 2]
            state = parts[home_idx - 3]
            return f'{street_part.replace("-", " ")}, {city}, {state}'
    except Exception:
        return url


def run(neighborhood, slow_mo=0):
    conn    = init_db()
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            slow_mo=slow_mo,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-first-run',
                '--no-default-browser-check',
            ],
        )
        ctx = browser.new_context(
            viewport={'width': 1440, 'height': 900},
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0.0.0 Safari/537.36'
            ),
            locale='en-US',
            timezone_id='America/Los_Angeles',
        )
        # Patch every page before any JS runs
        ctx.add_init_script(STEALTH_JS)
        page = ctx.new_page()

        # ── Navigate to open house results ───────────────────────────────────
        if neighborhood.startswith('http'):
            start_url = neighborhood
        else:
            print(f'\nSearching Redfin for: {neighborhood}')
            page.goto('https://www.redfin.com', wait_until='domcontentloaded')
            pause(2, 4)

            search_box = page.locator('input[placeholder*="Search"], #search-box-input').first
            search_box.wait_for(timeout=12000)
            # Type like a human — character by character with small random delays
            for ch in neighborhood:
                search_box.type(ch, delay=random.randint(60, 160))
            pause(1.2, 2.0)

            try:
                suggestion = page.locator(
                    '.clickable.suggestion, .autocomplete-suggestion, '
                    '[role="option"], [role="listbox"] li, .SearchTypeaheadRow'
                ).first
                suggestion.wait_for(timeout=4000)
                suggestion.click()
            except Exception:
                search_box.press('Enter')

            page.wait_for_load_state('domcontentloaded')
            pause(2.5, 4.0)

            current_url = page.url
            if 'open-house' not in current_url:
                if '/filter/' in current_url:
                    start_url = current_url.replace('/filter/', '/filter/open-house=anytime,')
                else:
                    start_url = current_url.rstrip('/') + '/filter/open-house=anytime'
            else:
                start_url = current_url

        print(f'\nGoing to: {start_url}')
        page.goto(start_url, wait_until='domcontentloaded')
        pause(3, 5)
        dismiss_popups(page)

        # ── Scroll to load all cards ─────────────────────────────────────────
        print('Loading all listing cards...')
        count = scroll_for_cards(page)
        print(f'  Found {count} cards')

        # ── Collect listing URLs ─────────────────────────────────────────────
        links = page.locator('a[href*="/home/"]').all()
        seen, listing_urls = set(), []
        for link in links:
            try:
                href = link.get_attribute('href', timeout=1000)
                if href and href not in seen:
                    seen.add(href)
                    url = href if href.startswith('http') else 'https://www.redfin.com' + href
                    listing_urls.append(url)
            except Exception:
                pass

        # Shuffle slightly — perfectly sequential requests look robotic
        random.shuffle(listing_urls)
        print(f'  Collected {len(listing_urls)} unique listings\n')

        # ── Visit each listing ───────────────────────────────────────────────
        for i, url in enumerate(listing_urls, 1):
            print(f'[{i}/{len(listing_urls)}] {url}')

            # Occasional longer break every 4-8 listings
            if i > 1 and i % random.randint(4, 8) == 0:
                long_pause()

            try:
                page.goto(url, wait_until='domcontentloaded', timeout=30000)
                pause(2.5, 5.0)
                dismiss_popups(page)

                # Scroll a bit as if reading the page
                page.evaluate(f'window.scrollTo(0, {random.randint(200, 600)})')
                pause(0.8, 2.0)

                address = address_from_url(url)

                # Open house date / time
                # The page has a heading "Open house schedule" then the actual times below it.
                # We grab the whole section and filter out the heading line.
                open_date = open_time = ''
                try:
                    section_text = page.locator('text=/Open House/i').first.evaluate(
                        'el => el.closest("section,div,article,li").innerText', timeout=4000
                    )
                    lines = [
                        l.strip() for l in section_text.split('\n')
                        if l.strip() and 'open house' not in l.strip().lower()
                    ]
                    if lines:
                        oh_line = lines[0]
                        if '·' in oh_line:
                            d, t = oh_line.split('·', 1)
                            open_date, open_time = d.strip(), t.strip()
                        else:
                            open_date = oh_line[:80]
                except Exception:
                    pass

                # Agent — find "Listed by" text
                agent_name = ''
                try:
                    block = page.locator('text=/Listed by/i').first.evaluate(
                        'el => el.closest("div,li,span,p").innerText', timeout=4000
                    )
                    agent_name = block.replace('Listed by', '').split('\n')[0].strip(' •·,')
                except Exception:
                    pass

                row = (address, agent_name, open_date, open_time, url, datetime.now().isoformat())
                upsert(conn, row)
                results.append(row)
                print(f'  address : {address}')
                print(f'  agent   : {agent_name or "not found"}')
                print(f'  when    : {open_date} {open_time}\n')

            except PlaywrightTimeout:
                print('  Timed out — skipping\n')
                pause(3, 6)   # back off before next request
            except Exception as e:
                err = str(e)
                print(f'  Error: {err[:120]}\n')
                if 'closed' in err.lower():
                    print('Browser was closed — stopping early.')
                    break
                pause(3, 6)

        browser.close()

    # ── Summary ──────────────────────────────────────────────────────────────
    print('=' * 60)
    print(f'  {len(results)} open houses saved to {DB_FILE}')
    print('=' * 60)
    rows = conn.execute(
        'SELECT address, agent_name, open_date, open_time FROM openhouses ORDER BY open_date, open_time'
    ).fetchall()
    for r in rows:
        date_time = f'{r[2]} {r[3]}'.strip()
        print(f'  {date_time:<28}  {r[0]}')
        if r[1]:
            print(f'  {"":28}  Agent: {r[1]}')
        print()
    conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Find Redfin open houses for a neighborhood')
    parser.add_argument('neighborhood', nargs='?', help='Redfin URL or neighborhood name')
    parser.add_argument('--slow', type=int, default=0,
                        help='Extra slow-mo delay in ms on top of random pauses (default: 0)')
    args = parser.parse_args()

    neighborhood = args.neighborhood
    if not neighborhood:
        neighborhood = input('Enter Redfin URL or neighborhood name: ').strip()
    if not neighborhood:
        sys.exit('No input provided.')

    run(neighborhood, slow_mo=args.slow)
