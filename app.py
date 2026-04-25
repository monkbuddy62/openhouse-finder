#!/usr/bin/env python3
"""
Open House Finder — web UI
Run: python app.py   then open http://localhost:5000
"""

import json
import math
import queue
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

from flask import Flask, Response, jsonify, render_template, request

from main import DB_FILE, DAY_ORDER, init_db, run as scraper_run

app = Flask(__name__)

_q       = queue.Queue()
_running = False


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    init_db()   # ensures schema + migrations
    return conn


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/run', methods=['POST'])
def api_run():
    global _running
    if _running:
        return jsonify({'error': 'A scrape is already running'}), 409

    url = (request.json or {}).get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    _running = True

    def log(msg):
        _q.put({'type': 'log', 'msg': str(msg)})

    def target():
        global _running
        try:
            scraper_run(url, log=log)
            _q.put({'type': 'done'})
        except Exception as e:
            _q.put({'type': 'error', 'msg': str(e)})
        finally:
            _running = False

    threading.Thread(target=target, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/progress')
def api_progress():
    def generate():
        while True:
            try:
                msg = _q.get(timeout=25)
                yield f'data: {json.dumps(msg)}\n\n'
                if msg.get('type') in ('done', 'error'):
                    break
            except queue.Empty:
                yield f'data: {json.dumps({"type": "ping"})}\n\n'
    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.route('/api/listings')
def api_listings():
    try:
        conn = get_db()
        rows = conn.execute(
            'SELECT id, address, agent_name, open_date, open_time, listing_url, excluded, lat, lng, scraped_week '
            'FROM openhouses'
        ).fetchall()
        conn.close()
        data = [dict(r) for r in rows]
        data.sort(key=lambda r: (DAY_ORDER.get((r['open_date'] or '').upper(), 99), r['open_time'] or ''))
        return jsonify(data)
    except Exception:
        return jsonify([])


@app.route('/api/exclude', methods=['POST'])
def api_exclude():
    data = request.json or {}
    conn = get_db()
    conn.execute('UPDATE openhouses SET excluded=? WHERE id=?', (data['excluded'], data['id']))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/geocode', methods=['POST'])
def api_geocode():
    address = (request.json or {}).get('address', '')
    result  = _geocode(address)
    if result:
        return jsonify(result)
    return jsonify({'error': 'Not found'}), 404


@app.route('/api/route', methods=['POST'])
def api_route():
    data = request.json or {}
    home = data.get('home')          # {lat, lng, address}
    ids  = data.get('ids', [])       # listing ids to include

    if not home or not ids:
        return jsonify({'error': 'Missing home or ids'}), 400

    conn  = get_db()
    placeholders = ','.join('?' * len(ids))
    rows  = conn.execute(
        f'SELECT id, address, agent_name, open_date, open_time, lat, lng FROM openhouses WHERE id IN ({placeholders})',
        ids,
    ).fetchall()

    stops = []
    for row in rows:
        r = dict(row)
        if not r['lat'] or not r['lng']:
            coords = _geocode(r['address'])
            if coords:
                r['lat'] = coords['lat']
                r['lng']  = coords['lng']
                conn.execute('UPDATE openhouses SET lat=?, lng=? WHERE id=?',
                             (r['lat'], r['lng'], r['id']))
                conn.commit()
                time.sleep(1.1)   # Nominatim rate limit: 1 req/s
        if r['lat'] and r['lng']:
            stops.append(r)

    conn.close()

    if not stops:
        return jsonify({'error': 'Could not geocode any stops'}), 400

    ordered, n_skipped = _time_aware_route(home, stops)
    maps_url = _google_maps_url(home['address'], [s['address'] for s in ordered if not s.get('_skipped')])

    return jsonify({'route': ordered, 'maps_url': maps_url, 'skipped': n_skipped})


# ── Geo helpers ───────────────────────────────────────────────────────────────

def _geocode(address):
    """Geocode via Nominatim (OSM). Returns {lat, lng} or None."""
    try:
        params = urllib.parse.urlencode({'q': address, 'format': 'json', 'limit': 1})
        req = urllib.request.Request(
            f'https://nominatim.openstreetmap.org/search?{params}',
            headers={'User-Agent': 'OpenHouseFinder/1.0 patrickjmccaffrey@gmail.com'},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
            if data:
                return {'lat': float(data[0]['lat']), 'lng': float(data[0]['lon'])}
    except Exception:
        pass
    return None


def _haversine(a, b):
    R = 6371
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(h))


_AVG_SPEED_KMH = 40   # assumed urban driving speed
_VISIT_MIN     = 5    # minutes spent at each open house


def _parse_open_time(s):
    """Parse '1–3PM', '10AM–12PM', '1:30–3PM' → (start_min, end_min) from midnight.
    Returns (None, None) if unparseable."""
    import re
    if not s:
        return None, None
    s = s.replace('–', '-').replace('—', '-').strip()
    m = re.match(r'^(\d+(?::\d+)?)\s*(AM|PM)?\s*-\s*(\d+(?::\d+)?)\s*(AM|PM)?$', s, re.IGNORECASE)
    if not m:
        return None, None
    sh, sa, eh, ea = m.groups()
    ea = (ea or '').upper()
    sa = (sa or ea).upper()  # start inherits AM/PM from end when absent

    def to_min(h_str, ampm):
        parts = h_str.split(':')
        h, mi = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        if ampm == 'PM' and h != 12:
            h += 12
        elif ampm == 'AM' and h == 12:
            h = 0
        return h * 60 + mi

    return to_min(sh, sa), to_min(eh, ea)


def _time_aware_route(home, stops):
    """Time-aware route: Earliest Deadline First among reachable stops.
    Stops with no time info are appended at the end via nearest-neighbor."""
    timed, no_time = [], []
    for s in stops:
        s = dict(s)
        s['_start'], s['_end'] = _parse_open_time(s.get('open_time', ''))
        (timed if s['_start'] is not None else no_time).append(s)

    route    = []
    skipped  = []
    pos      = (home['lat'], home['lng'])
    now      = min((s['_start'] for s in timed), default=9 * 60)
    remaining = list(timed)

    while remaining:
        reachable = []
        for s in remaining:
            km      = _haversine(pos, (s['lat'], s['lng']))
            travel  = (km / _AVG_SPEED_KMH) * 60
            arrival = now + travel
            if arrival <= s['_end']:
                reachable.append((s, arrival, travel))

        if not reachable:
            future = [s for s in remaining if s['_start'] > now]
            if not future:
                skipped.extend(remaining)
                break
            now = min(s['_start'] for s in future)
            continue

        # Earliest deadline first; break ties by travel time
        best, arrival, travel = min(reachable, key=lambda x: (x[0]['_end'], x[2]))
        best['_skipped'] = False
        route.append(best)
        remaining.remove(best)
        pos = (best['lat'], best['lng'])
        now = max(arrival, best['_start']) + _VISIT_MIN

    for s in skipped:
        s['_skipped'] = True
        route.append(s)

    # No-time stops: nearest-neighbor from wherever the route ends
    while no_time:
        nearest = min(no_time, key=lambda s: _haversine(pos, (s['lat'], s['lng'])))
        nearest['_skipped'] = False
        route.append(nearest)
        no_time.remove(nearest)
        pos = (nearest['lat'], nearest['lng'])

    return route, len(skipped)


def _google_maps_url(home_address, stop_addresses):
    """Build a Google Maps directions URL with all stops in order."""
    parts = [urllib.parse.quote(home_address)] + \
            [urllib.parse.quote(a) for a in stop_addresses] + \
            [urllib.parse.quote(home_address)]
    return 'https://www.google.com/maps/dir/' + '/'.join(parts)


if __name__ == '__main__':
    print('Open House Finder running at http://localhost:5000')
    app.run(debug=False, port=5000, threaded=True)
