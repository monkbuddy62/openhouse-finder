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

        # ── Search ──────────────────────────────────────────────────────────
        print(f'\nSearching Redfin for: {neighborhood}')
        page.goto('https://www.redfin.com', wait_until='domcontentloaded')

        search_box = page.locator('input[placeholder*="Search"], #search-box-input').first
        search_box.wait_for(timeout=12000)
        search_box.fill(neighborhood)
        page.wait_for_timeout(1400)

        # Pick first autocomplete suggestion
        suggestion = page.locator('.clickable.suggestion, .autocomplete-suggestion').first
        suggestion.wait_for(timeout=8000)
        suggestion.click()
        page.wait_for_load_state('domcontentloaded')
        page.wait_for_timeout(2200)

        # ── Open House filter ────────────────────────────────────────────────
        print('Applying Open House filter...')
        applied = False

        # Try a top-level "Open Houses" button first (sometimes visible without opening filter panel)
        for btn_text in ['Open Houses', 'Open House']:
            try:
                page.get_by_role('button', name=btn_text).first.click(timeout=4000)
                applied = True
                break
            except Exception:
                pass

        if not applied:
            # Open the full filter panel and look inside
            try:
                page.locator('[data-rf-test-name="filterButton"], button:has-text("Filters"), button:has-text("Filter")').first.click(timeout=6000)
                page.wait_for_timeout(900)
                page.get_by_text('Open Houses', exact=False).first.click(timeout=5000)
                # Apply / Done button
                for done in ['Apply', 'Done', 'See homes']:
                    try:
                        page.get_by_role('button', name=done).first.click(timeout=3000)
                        break
                    except Exception:
                        pass
            except Exception as e:
                print(f'  Warning: could not auto-apply filter ({e})')
                print('  Please apply the Open Houses filter manually in the browser, then press Enter.')
                input()

        page.wait_for_load_state('domcontentloaded')
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
                page.wait_for_timeout(1600)

                # Address
                street = get_text(page, [
                    '[data-rf-test-name="abp-streetLine"]',
                    '.street-address',
                    'h1.address',
                    '.homeAddress span:first-child',
                ])
                city_state = get_text(page, [
                    '[data-rf-test-name="abp-cityStateZip"]',
                    '.cityStateZip',
                    '.homeAddress span:last-child',
                ])
                address = ', '.join(filter(None, [street, city_state])) or url

                # Open house date / time
                # Redfin shows "Sat, Apr 26 · 1pm – 3pm" or similar
                oh_text = get_text(page, [
                    '.open-house-row',
                    '[data-rf-test-name="openHouseRow"]',
                    '.open-house-info',
                    'div:has-text("Open House"):not(button)',
                ])
                if '·' in oh_text:
                    parts = oh_text.split('·', 1)
                    open_date = parts[0].strip()
                    open_time = parts[1].strip()
                elif oh_text:
                    open_date = oh_text
                    open_time = ''
                else:
                    open_date = open_time = ''

                # Listing agent
                agent_name = get_text(page, [
                    '[data-rf-test-name="listingAgentName"]',
                    '.listing-agent .agent-name',
                    '.agent-basic-details--heading',
                    'span.agent-name',
                    '.listing-agent-name',
                ])

                row = (address, agent_name, open_date, open_time, url, datetime.now().isoformat())
                upsert(conn, row)
                results.append(row)
                print(f'  address : {address or "?"}')
                print(f'  agent   : {agent_name or "not found"}')
                print(f'  when    : {open_date} {open_time}\n')

            except PlaywrightTimeout:
                print('  Timed out — skipping\n')
            except Exception as e:
                print(f'  Error: {e}\n')

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
    parser.add_argument('--slow', type=int, default=700,
                        help='Milliseconds between actions — increase to watch more easily (default: 700)')
    args = parser.parse_args()

    neighborhood = args.neighborhood
    if not neighborhood:
        neighborhood = input('Enter neighborhood, city, or zip: ').strip()
    if not neighborhood:
        sys.exit('No neighborhood provided.')

    run(neighborhood, slow_mo=args.slow)
