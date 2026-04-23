#!/usr/bin/env python3
"""
Open House Finder — scrapes Redfin open houses for a neighborhood
and stores results in a local SQLite database.

Usage:
    python main.py "https://www.redfin.com/neighborhood/3040/WA/Seattle/West-Seattle/filter/open-house=anytime"
    python main.py "Ballard, Seattle, WA"
    python main.py              # will prompt you
"""

import re
import sys
import sqlite3
import argparse
import random
import time
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

DB_FILE = 'openhouses.db'

DAY_ORDER = {'MON': 0, 'TUE': 1, 'WED': 2, 'THU': 3, 'FRI': 4, 'SAT': 5, 'SUN': 6}

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
    time.sleep(random.uniform(min_s, max_s))


def long_pause(min_s=4.0, max_s=9.0, log=print):
    secs = random.uniform(min_s, max_s)
    log(f'  (pausing {secs:.0f}s…)')
    time.sleep(secs)


def human_scroll(page):
    total = page.evaluate('document.body.scrollHeight')
    pos   = 0
    while pos < total:
        step = random.randint(250, 700)
        pos  = min(pos + step, total)
        page.evaluate(f'window.scrollTo(0, {pos})')
        time.sleep(random.uniform(0.08, 0.25))
    pause(1.0, 2.5)
    page.evaluate(f'window.scrollTo(0, {int(total * random.uniform(0.7, 0.9))})')
    pause(0.5, 1.2)


def scroll_for_cards(page):
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
            scraped_at   TEXT,
            excluded     INTEGER DEFAULT 0,
            lat          REAL,
            lng          REAL,
            scraped_week TEXT
        )
    ''')
    # Migrate existing DBs that predate these columns
    for col, defn in [
        ('excluded',     'INTEGER DEFAULT 0'),
        ('lat',          'REAL'),
        ('lng',          'REAL'),
        ('scraped_week', 'TEXT'),
    ]:
        try:
            conn.execute(f'ALTER TABLE openhouses ADD COLUMN {col} {defn}')
        except Exception:
            pass
    conn.commit()
    return conn


def upsert(conn, row):
    week = datetime.now().strftime('%G-W%V')
    conn.execute('''
        INSERT INTO openhouses (address, agent_name, open_date, open_time, listing_url, scraped_at, scraped_week)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(listing_url) DO UPDATE SET
            agent_name   = excluded.agent_name,
            open_date    = excluded.open_date,
            open_time    = excluded.open_time,
            scraped_at   = excluded.scraped_at,
            scraped_week = excluded.scraped_week
    ''', row + (week,))
    conn.commit()


def dismiss_popups(page):
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
    """Redfin URLs: /STATE/CITY/[neighborhood/]STREET/[unit-X/]home/ID
    State is always segment index 3, city always index 4.
    """
    parts = url.rstrip('/').split('/')
    try:
        home_idx = parts.index('home')
        state = parts[3]
        city  = parts[4]
        street_part = parts[home_idx - 1]
        if street_part.lower().startswith('unit'):
            unit   = street_part.replace('-', ' ')
            street = parts[home_idx - 2].replace('-', ' ')
            return f'{street}, {unit}, {city}, {state}'
        else:
            street = street_part.replace('-', ' ')
            return f'{street}, {city}, {state}'
    except Exception:
        return url


def parse_badge(text):
    """'REDFIN OPEN SAT, 1–3PM' → ('SAT', '1–3PM'); 'OPEN SUN' → ('SUN', '')"""
    if not text:
        return '', ''
    t = text.upper().strip()
    if t.startswith('REDFIN '):
        t = t[7:]
    if t.startswith('OPEN '):
        t = t[5:]
    if ',' in t:
        day, time_part = t.split(',', 1)
        return day.strip(), time_part.strip()
    return t.strip(), ''


def resolve_day(day_str):
    """Replace TODAY/TOMORROW with the actual short day name."""
    d = day_str.upper()
    if d == 'TODAY':
        return datetime.now().strftime('%a').upper()
    if d == 'TOMORROW':
        return (datetime.now() + timedelta(days=1)).strftime('%a').upper()
    return day_str


def clean_agent(name):
    return re.sub(r'[\s•·,\.]+$', '', name).strip(' •·,')


def run(neighborhood, slow_mo=0, log=print):
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
        ctx.add_init_script(STEALTH_JS)
        page = ctx.new_page()

        # ── Navigate to open house results ───────────────────────────────────
        if neighborhood.startswith('http'):
            start_url = neighborhood
        else:
            log(f'Searching Redfin for: {neighborhood}')
            page.goto('https://www.redfin.com', wait_until='domcontentloaded')
            pause(2, 4)

            search_box = page.locator('input[placeholder*="Search"], #search-box-input').first
            search_box.wait_for(timeout=12000)
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

        log(f'Going to: {start_url}')
        page.goto(start_url, wait_until='domcontentloaded')
        pause(3, 5)
        dismiss_popups(page)

        log('Loading all listing cards...')
        count = scroll_for_cards(page)
        log(f'  Found {count} cards')

        cards = page.locator('.HomeCard, [data-rf-test-name="mapHomeCard"]').all()
        seen, listings = set(), []

        for card in cards:
            try:
                href = card.locator('a[href*="/home/"]').first.get_attribute('href', timeout=1000)
                if not href or href in seen:
                    continue
                seen.add(href)
                url = href if href.startswith('http') else 'https://www.redfin.com' + href

                open_text = ''
                try:
                    badge = card.locator(
                        '[class*="open-house" i], [class*="OpenHouse" i], '
                        '[class*="openHouse" i], [class*="badge" i]'
                    ).first
                    if badge.count():
                        open_text = badge.inner_text(timeout=800).strip()
                except Exception:
                    pass

                if not open_text:
                    try:
                        card_text = card.inner_text(timeout=1000)
                        for line in card_text.split('\n'):
                            if 'open' in line.lower() and any(c.isdigit() for c in line):
                                open_text = line.strip()
                                break
                    except Exception:
                        pass

                listings.append({'url': url, 'open_text': open_text})
            except Exception:
                pass

        random.shuffle(listings)
        log(f'  Collected {len(listings)} unique listings')

        # ── Visit each listing for agent name ───────────────────────────────
        for i, listing in enumerate(listings, 1):
            url       = listing['url']
            open_text = listing['open_text']
            log(f'[{i}/{len(listings)}] {url}')
            if open_text:
                log(f'  badge: {open_text}')

            if i > 1 and i % random.randint(4, 8) == 0:
                long_pause(log=log)

            try:
                page.goto(url, wait_until='domcontentloaded', timeout=30000)
                pause(2.5, 5.0)
                dismiss_popups(page)

                page.evaluate(f'window.scrollTo(0, {random.randint(200, 600)})')
                pause(0.8, 2.0)

                address = address_from_url(url)

                agent_name = ''
                try:
                    block = page.locator('text=/Listed by/i').first.evaluate(
                        'el => el.closest("div,li,span,p").innerText', timeout=4000
                    )
                    agent_name = clean_agent(block.replace('Listed by', '').split('\n')[0])
                except Exception:
                    pass

                open_date, open_time = parse_badge(open_text)
                open_date = resolve_day(open_date)

                row = (address, agent_name, open_date, open_time, url, datetime.now().isoformat())
                upsert(conn, row)
                results.append(row)
                log(f'  address : {address}')
                log(f'  agent   : {agent_name or "not found"}')
                log(f'  when    : {open_date} {open_time}'.strip() or '(not found)')

            except PlaywrightTimeout:
                log('  Timed out — skipping')
                pause(3, 6)
            except Exception as e:
                err = str(e)
                log(f'  Error: {err[:120]}')
                if 'closed' in err.lower():
                    log('Browser was closed — stopping early.')
                    break
                pause(3, 6)

        browser.close()

    log(f'Done — {len(results)} listings saved.')
    conn.close()
    return results


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

    # CLI summary
    conn = init_db()
    rows = conn.execute(
        'SELECT address, agent_name, open_date, open_time FROM openhouses'
    ).fetchall()
    rows_sorted = sorted(rows, key=lambda r: (DAY_ORDER.get(r[2].upper(), 99), r[3]))
    print('=' * 60)
    for r in rows_sorted:
        date_time = f'{r[2]} {r[3]}'.strip()
        print(f'  {date_time:<28}  {r[0]}')
        if r[1]:
            print(f'  {"":28}  Agent: {r[1]}')
        print()
    conn.close()
