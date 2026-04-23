#!/usr/bin/env python3
"""
Open House Finder — scrapes Redfin open houses for a neighborhood
and stores results in a local SQLite database.

Usage:
    python main.py "Ballard, Seattle, WA"
    python main.py "98107"
    python main.py              # will prompt you
    python main.py "Capitol Hill, Seattle" --slow 1200
"""

import sys
import sqlite3
import argparse
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

DB_FILE = 'openhouses.db'


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


def scroll_to_bottom(page):
    """Repeatedly scroll to trigger Redfin's infinite scroll until no new cards appear."""
    prev_count = 0
    for _ in range(25):
        page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
        page.wait_for_timeout(1800)
        cards = page.locator('.HomeCard, [data-rf-test-name="mapHomeCard"]').all()
        if len(cards) == prev_count:
            break
        prev_count = len(cards)
    return prev_count


def get_text(page, selectors):
    """Try a list of selectors, return inner text of the first match."""
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.count() > 0:
                return el.inner_text(timeout=2000).strip()
        except Exception:
            pass
    return ''


def run(neighborhood, slow_mo=700):
    conn = init_db()
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=slow_mo)
        ctx  = browser.new_context(viewport={'width': 1440, 'height': 900})
        page = ctx.new_page()

        # ── Navigate to open house results ───────────────────────────────────
        if neighborhood.startswith('http'):
            # User passed a direct Redfin URL — just go there
            start_url = neighborhood
        else:
            # Build search URL and append open house filter
            # Tip: search manually on redfin.com, copy the URL, and pass that instead
            print(f'\nSearching Redfin for: {neighborhood}')
            page.goto('https://www.redfin.com', wait_until='domcontentloaded')

            search_box = page.locator('input[placeholder*="Search"], #search-box-input').first
            search_box.wait_for(timeout=12000)
            search_box.fill(neighborhood)
            page.wait_for_timeout(1400)

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
            page.wait_for_timeout(2200)

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
        page.wait_for_timeout(2500)

        # ── Scroll to load all cards ─────────────────────────────────────────
        print('Loading all listing cards...')
        count = scroll_to_bottom(page)
        print(f'  Found {count} cards')

        # ── Collect listing URLs ─────────────────────────────────────────────
        links = page.locator('a[href*="/home/"]').all()
        seen = set()
        listing_urls = []
        for link in links:
            try:
                href = link.get_attribute('href', timeout=1000)
                if href and href not in seen:
                    seen.add(href)
                    url = href if href.startswith('http') else 'https://www.redfin.com' + href
                    listing_urls.append(url)
            except Exception:
                pass

        print(f'  Collected {len(listing_urls)} unique listings\n')

        # ── Visit each listing ───────────────────────────────────────────────
        for i, url in enumerate(listing_urls, 1):
            print(f'[{i}/{len(listing_urls)}] {url}')
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=25000)
                page.wait_for_timeout(2800)

                # Address — parse reliably from URL
                # URL pattern: /WA/Seattle/1241-SW-Myrtle-St-98106/home/...
                parts = url.rstrip('/').split('/')
                try:
                    home_idx = parts.index('home')
                    raw = parts[home_idx - 1]          # e.g. "1241-SW-Myrtle-St-98106"
                    city = parts[home_idx - 2]         # e.g. "Seattle"
                    state = parts[home_idx - 3]        # e.g. "WA"
                    street_zip = raw.replace('-', ' ')
                    address = f'{street_zip}, {city}, {state}'
                except Exception:
                    address = url

                # Open house date / time — search page text for "Open House" block
                open_date = open_time = ''
                try:
                    # Grab all text on the page and hunt for the open house line
                    oh_text = page.locator(
                        'text=/Open House/i'
                    ).first.evaluate('el => el.closest("div,li,span,section").innerText', timeout=3000)
                    oh_text = oh_text.strip()
                    if '·' in oh_text:
                        d, t = oh_text.split('·', 1)
                        open_date = d.strip()
                        open_time = t.strip()
                    elif oh_text:
                        open_date = oh_text[:80]
                except Exception:
                    pass

                # Agent — look for "Listed by" text anywhere on page
                agent_name = ''
                try:
                    lb = page.locator('text=/Listed by/i').first
                    block = lb.evaluate('el => el.closest("div,li,span,p").innerText', timeout=3000)
                    # Strip the "Listed by" prefix
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
            except Exception as e:
                err = str(e)
                print(f'  Error: {err[:120]}\n')
                if 'closed' in err.lower():
                    print('Browser was closed — stopping early.')
                    break

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
    parser.add_argument('neighborhood', nargs='?', help='Neighborhood, city, or zip code')
    parser.add_argument('--slow', type=int, default=1200,
                        help='Milliseconds between actions — increase to watch more easily (default: 1200)')
    args = parser.parse_args()

    neighborhood = args.neighborhood
    if not neighborhood:
        neighborhood = input('Enter neighborhood, city, or zip: ').strip()
    if not neighborhood:
        sys.exit('No neighborhood provided.')

    run(neighborhood, slow_mo=args.slow)
