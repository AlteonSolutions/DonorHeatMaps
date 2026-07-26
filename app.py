"""
Donation Heat Map API - Static PNG Images using Folium + Playwright
Generates PNG maps with marker clustering
Uses Census Geocoder for full street address geocoding (FREE, unlimited)
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import numpy as np
import folium
from folium.plugins import HeatMap, MarkerCluster
import os
import sys
import asyncio
from playwright.async_api import async_playwright
import requests
import time
import csv
import re
from io import StringIO

# Line-buffer stdout/stderr so progress shows up in the service log in real
# time (NSSM redirects to a file, where Python would otherwise block-buffer).
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except AttributeError:
    pass

app = Flask(__name__)
CORS(app)

NO_MAPS_FILENAME = 'NO MAPS GENERATED.txt'

print("Census Geocoder ready")

# Offline centroid lookups for partial addresses (bundled in data/)
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
ZIP_CENTROIDS = {}
CITY_CENTROIDS = {}

def load_centroids():
    try:
        with open(os.path.join(DATA_DIR, 'zip_centroids.csv'), newline='', encoding='utf-8') as f:
            reader = csv.reader(f)
            next(reader)
            for row in reader:
                ZIP_CENTROIDS[row[0]] = (float(row[1]), float(row[2]))
        with open(os.path.join(DATA_DIR, 'city_centroids.csv'), newline='', encoding='utf-8') as f:
            reader = csv.reader(f)
            next(reader)
            for row in reader:
                CITY_CENTROIDS[(row[0], row[1])] = (float(row[2]), float(row[3]))
        print(f"Loaded {len(ZIP_CENTROIDS)} ZIP centroids, {len(CITY_CENTROIDS)} city centroids")
    except Exception as e:
        print(f"Warning: could not load centroid data: {e}")

load_centroids()

# Full state name -> USPS 2-letter code, so CSVs that spell states out
# ("Illinois") match the 2-letter keys in the city centroid table.
STATE_ABBREV = {
    'ALABAMA': 'AL', 'ALASKA': 'AK', 'ARIZONA': 'AZ', 'ARKANSAS': 'AR',
    'CALIFORNIA': 'CA', 'COLORADO': 'CO', 'CONNECTICUT': 'CT', 'DELAWARE': 'DE',
    'DISTRICT OF COLUMBIA': 'DC', 'FLORIDA': 'FL', 'GEORGIA': 'GA', 'HAWAII': 'HI',
    'IDAHO': 'ID', 'ILLINOIS': 'IL', 'INDIANA': 'IN', 'IOWA': 'IA', 'KANSAS': 'KS',
    'KENTUCKY': 'KY', 'LOUISIANA': 'LA', 'MAINE': 'ME', 'MARYLAND': 'MD',
    'MASSACHUSETTS': 'MA', 'MICHIGAN': 'MI', 'MINNESOTA': 'MN', 'MISSISSIPPI': 'MS',
    'MISSOURI': 'MO', 'MONTANA': 'MT', 'NEBRASKA': 'NE', 'NEVADA': 'NV',
    'NEW HAMPSHIRE': 'NH', 'NEW JERSEY': 'NJ', 'NEW MEXICO': 'NM', 'NEW YORK': 'NY',
    'NORTH CAROLINA': 'NC', 'NORTH DAKOTA': 'ND', 'OHIO': 'OH', 'OKLAHOMA': 'OK',
    'OREGON': 'OR', 'PENNSYLVANIA': 'PA', 'RHODE ISLAND': 'RI', 'SOUTH CAROLINA': 'SC',
    'SOUTH DAKOTA': 'SD', 'TENNESSEE': 'TN', 'TEXAS': 'TX', 'UTAH': 'UT',
    'VERMONT': 'VT', 'VIRGINIA': 'VA', 'WASHINGTON': 'WA', 'WEST VIRGINIA': 'WV',
    'WISCONSIN': 'WI', 'WYOMING': 'WY', 'PUERTO RICO': 'PR',
}

def state_abbrev(state):
    """Return a 2-letter USPS code from either a full name or an abbreviation."""
    s = (state or '').strip().upper()
    if len(s) == 2:
        return s
    return STATE_ABBREV.get(s, s)

def parse_giving(value):
    """Parse a giving amount like '$1,234.56' to float; 0 if unparseable"""
    try:
        cleaned = value.strip().replace('$', '').replace(',', '').replace('(', '-').replace(')', '')
        return float(cleaned) if cleaned else 0.0
    except (ValueError, AttributeError):
        return 0.0

def parse_addresses(data, center_zip=None):
    addresses = []
    center_address = None
    zip_counts = {}

    if isinstance(data, str):
        # Auto-detect delimiter (tab or comma)
        dialect = csv.Sniffer().sniff(data[:1024])
        reader = csv.reader(StringIO(data), dialect=dialect)

        for i, row in enumerate(reader):
            # Skip header
            if i == 0:
                continue

            # Need at least 6 columns: ID, Name, Street, City, State, Zip, Giving
            if len(row) >= 6:
                street = row[2].strip()
                city = row[3].strip()
                state = row[4].strip()
                zipcode = row[5].strip()
                giving = parse_giving(row[6]) if len(row) >= 7 else 0.0

                # Clean ZIP
                if '-' in zipcode:
                    zipcode = zipcode.split('-')[0]
                if zipcode and not zipcode[:5].isdigit():
                    zipcode = ''
                else:
                    zipcode = zipcode[:5].zfill(5) if zipcode else ''

                # Classify by how precisely we can place this donor:
                #   address - full street geocode via Census API
                #   zip     - ZIP centroid lookup (offline)
                #   city    - city/state centroid lookup (offline)
                if street and (zipcode or (city and state)):
                    precision = 'address'
                elif zipcode:
                    precision = 'zip'
                elif city and state:
                    precision = 'city'
                else:
                    continue

                addr = {'street': street, 'city': city, 'state': state,
                        'zip': zipcode, 'giving': giving, 'precision': precision}
                addresses.append(addr)

                if zipcode:
                    zip_counts[zipcode] = zip_counts.get(zipcode, 0) + 1

    print(f"Parsed {len(addresses)} valid addresses from file")

    # Only resolve an explicit center ZIP here. Automatic centering is
    # computed from geocoded coordinates AFTER geocoding (density-based),
    # which is far more robust than picking the single most-common ZIP -
    # a metro spread across many ZIPs would otherwise lose to one dense
    # ZIP (e.g. a Florida retirement community).
    if addresses and center_zip:
        for addr in addresses:
            if addr['zip'].startswith(center_zip):
                center_address = addr
                print(f"Using specified center ZIP: {center_zip}")
                break

    return addresses, center_address

def geocode_address_census(street, city, state, zipcode):
    """Geocode a single address using Census Geocoder API directly"""
    try:
        url = "https://geocoding.geo.census.gov/geocoder/locations/address"

        params = {
            'street': street,
            'city': city,
            'state': state,
            'zip': zipcode,
            'benchmark': 'Public_AR_Current',
            'format': 'json'
        }

        response = requests.get(url, params=params, timeout=10)

        if response.status_code == 200:
            data = response.json()

            if data.get('result', {}).get('addressMatches'):
                match = data['result']['addressMatches'][0]
                lat = match['coordinates']['y']
                lon = match['coordinates']['x']
                return lat, lon

        return None, None
    except Exception as e:
        return None, None

# Census batch geocoder: one request geocodes up to 10,000 addresses,
# vastly faster than one request per address. We chunk below that limit
# and run several chunks concurrently.
BATCH_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
BATCH_CHUNK = 500         # addresses per request (Census batch is far more
                          # reliable with small files than near the 10k cap)
BATCH_WORKERS = 6         # chunks in flight at once
BATCH_TIMEOUT = 120       # seconds per chunk
BATCH_RETRIES = 1         # extra attempts on a failed/timed-out chunk

def _geocode_batch_chunk(indexed_chunk):
    """Geocode one chunk via the Census batch endpoint, retrying on failure.
    indexed_chunk: list of (index, addr). Returns {index: (lat, lon)} for
    matched addresses. Raises only after retries are exhausted."""
    buf = StringIO()
    writer = csv.writer(buf)
    for idx, a in indexed_chunk:
        writer.writerow([idx, a['street'], a['city'], a['state'], a['zip']])
    payload = buf.getvalue().encode('utf-8')

    last_err = None
    for attempt in range(BATCH_RETRIES + 1):
        try:
            resp = requests.post(
                BATCH_URL,
                files={'addressFile': ('addresses.csv', payload, 'text/csv')},
                data={'benchmark': 'Public_AR_Current'},
                timeout=BATCH_TIMEOUT,
            )
            resp.raise_for_status()
            results = {}
            for row in csv.reader(StringIO(resp.text)):
                # Match rows: id, input, "Match", matchtype, matched, "lon,lat", ...
                if len(row) >= 6 and row[2] == 'Match':
                    try:
                        lon, lat = row[5].split(',')
                        results[int(row[0])] = (float(lat), float(lon))
                    except (ValueError, IndexError):
                        continue
            return results
        except Exception as e:
            last_err = e
    raise last_err

def _fallback_per_address(chunk):
    """Parallel per-address geocode for a chunk whose batch request failed,
    so a bad batch doesn't crawl one address at a time."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    out = {}
    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = {ex.submit(geocode_address_census, a['street'], a['city'],
                          a['state'], a['zip']): idx for idx, a in chunk}
        for f in as_completed(futs):
            try:
                lat, lon = f.result()
            except Exception:
                lat, lon = None, None
            if lat and lon:
                out[futs[f]] = (lat, lon)
    return out

def geocode_full_batch(full):
    """Geocode all full street addresses via the Census batch API.
    Returns {index: (lat, lon)} keyed by position in `full`. A chunk that
    fails even after retries falls back to parallel per-address geocoding so
    one bad batch neither loses donors nor crawls."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    chunks = []
    for start in range(0, len(full), BATCH_CHUNK):
        end = min(start + BATCH_CHUNK, len(full))
        chunks.append([(i, full[i]) for i in range(start, end)])

    matches = {}
    done = 0
    with ThreadPoolExecutor(max_workers=BATCH_WORKERS) as executor:
        future_to_chunk = {executor.submit(_geocode_batch_chunk, c): c for c in chunks}
        for future in as_completed(future_to_chunk):
            chunk = future_to_chunk[future]
            try:
                matches.update(future.result())
            except Exception as e:
                print(f"  Batch chunk failed ({e}); per-address fallback for {len(chunk)}")
                matches.update(_fallback_per_address(chunk))
            done += 1
            print(f"  Batch {done}/{len(chunks)} chunks done ({len(matches)} matched so far)")
    return matches

def lookup_centroid(addr):
    """Resolve a partial address to a ZIP or city centroid.
    Returns (lat, lon, precision) or (None, None, None)."""
    if addr['zip'] and addr['zip'] in ZIP_CENTROIDS:
        lat, lon = ZIP_CENTROIDS[addr['zip']]
        return lat, lon, 'zip'
    if addr['city'] and addr['state']:
        key = (addr['city'].strip().upper(), state_abbrev(addr['state']))
        if key in CITY_CENTROIDS:
            lat, lon = CITY_CENTROIDS[key]
            return lat, lon, 'city'
    return None, None, None

# Jitter (std dev in degrees) by placement precision, so centroid-placed
# donors scatter naturally instead of stacking on a single point
JITTER = {'address': 0.0001, 'zip': 0.008, 'city': 0.02}

def geocode_addresses(addresses, center_address=None):
    """Geocode addresses: Census batch API for full street addresses,
    offline ZIP/city centroids for partial ones (and as fallback when the
    Census API finds no match)."""
    geocoded = []
    geocoded_center = None
    stats = {'address': 0, 'zip': 0, 'city': 0, 'failed': 0}

    def place(addr, lat, lon, precision):
        nonlocal geocoded_center
        lat += np.random.normal(0, JITTER[precision])
        lon += np.random.normal(0, JITTER[precision])
        geocoded_addr = {**addr, 'lat': lat, 'lon': lon, 'precision': precision}
        geocoded.append(geocoded_addr)
        stats[precision] += 1
        if center_address is not None and addr is center_address:
            geocoded_center = geocoded_addr

    full = [a for a in addresses if a['precision'] == 'address']
    partial = [a for a in addresses if a['precision'] != 'address']

    # Partial addresses resolve instantly from the bundled centroid tables
    for addr in partial:
        lat, lon, precision = lookup_centroid(addr)
        if lat is not None:
            place(addr, lat, lon, precision)
        else:
            stats['failed'] += 1
            if stats['failed'] <= 5:
                print(f"  No centroid for: {addr['city']}, {addr['state']} {addr['zip']}")

    print(f"Geocoding {len(full)} full addresses via Census batch API "
          f"({len(partial)} partial addresses via centroids)...")
    start_time = time.time()

    matches = geocode_full_batch(full) if full else {}

    for idx, addr in enumerate(full):
        if idx in matches:
            lat, lon = matches[idx]
            place(addr, lat, lon, 'address')
        else:
            # No Census match - fall back to ZIP/city centroid
            lat, lon, precision = lookup_centroid(addr)
            if lat is not None:
                place(addr, lat, lon, precision)
            else:
                stats['failed'] += 1
                if stats['failed'] <= 5:
                    print(f"  Failed: {addr['street']}, {addr['city']}, {addr['state']} {addr['zip']}")

    success_rate = (len(geocoded) / len(addresses)) * 100 if addresses else 0
    total_time = time.time() - start_time

    print(f"  Placed {len(geocoded)}/{len(addresses)} addresses ({success_rate:.1f}% success) "
          f"in {total_time:.1f}s")
    print(f"  Precision: {stats['address']} street, {stats['zip']} ZIP centroid, "
          f"{stats['city']} city centroid, {stats['failed']} failed")

    return geocoded, geocoded_center, stats

def density_center(geocoded, cell=0.35):
    """Find the center of the densest metro cluster of donors.

    Bins points into a ~24-mile grid and scores each cell by the donor
    count in its 3x3 neighborhood, then returns the mean location of the
    points in the winning neighborhood. This favors a metro spread across
    many ZIPs over a single dense ZIP, so a national org's maps center on
    its true population core rather than an outlier concentration.
    Returns (lat, lon, cluster_size)."""
    from collections import defaultdict

    grid = defaultdict(list)
    for d in geocoded:
        key = (int(np.floor(d['lat'] / cell)), int(np.floor(d['lon'] / cell)))
        grid[key].append(d)

    best_key, best_score = None, -1
    for (gy, gx) in grid:
        score = sum(len(grid.get((gy + dy, gx + dx), []))
                    for dy in (-1, 0, 1) for dx in (-1, 0, 1))
        if score > best_score:
            best_score, best_key = score, (gy, gx)

    gy, gx = best_key
    cluster = [d for dy in (-1, 0, 1) for dx in (-1, 0, 1)
               for d in grid.get((gy + dy, gx + dx), [])]
    lat = float(np.mean([d['lat'] for d in cluster]))
    lon = float(np.mean([d['lon'] for d in cluster]))
    return lat, lon, len(cluster)

def nearest_place(geocoded, lat, lon):
    """Return the geocoded donor closest to (lat, lon), for labeling."""
    return min(geocoded, key=lambda d: (d['lat'] - lat) ** 2 + (d['lon'] - lon) ** 2)

def org_name_from_path(output_directory):
    """Best-effort org name from the output path. The expected layout is
    ...\\<Org Name>\\<timestamp>\\Donor Maps, so the org name is the folder
    just before a timestamp segment like '2026.7.22 11.41'. Returns None if
    it can't be determined confidently."""
    if not output_directory:
        return None
    parts = [p for p in re.split(r'[\\/]+', output_directory.strip()) if p]
    if not parts:
        return None
    if parts[-1].lower() == 'donor maps':
        parts = parts[:-1]
    if not parts:
        return None
    ts = re.compile(r'^\d{4}\.\d{1,2}\.\d{1,2}([ ._-].*)?$')
    if len(parts) >= 2 and ts.match(parts[-1]):
        return parts[-2]
    if ts.match(parts[-1]):
        return None
    return parts[-1]

def lookup_org_center(org_name):
    """Look up a nonprofit's HQ city/state via the ProPublica Nonprofit
    Explorer API (free, no key; data sourced from IRS filings) and return a
    center dict, or None if no confident match. Regional/local maps then
    center on where the ORG is, not where its donors happen to be densest."""
    if not org_name:
        return None
    try:
        resp = requests.get(
            "https://projects.propublica.org/nonprofits/api/v2/search.json",
            params={'q': org_name}, timeout=10)
        if resp.status_code != 200:
            print(f"  Org HQ lookup: ProPublica returned HTTP {resp.status_code}")
            return None
        orgs = resp.json().get('organizations', [])
        if not orgs:
            print(f"  Org HQ lookup: no nonprofit match for '{org_name}'")
            return None
        best = orgs[0]
        city = (best.get('city') or '').strip()
        state = (best.get('state') or '').strip()
        if not city or not state:
            return None
        key = (city.upper(), state_abbrev(state))
        if key not in CITY_CENTROIDS:
            print(f"  Org HQ lookup: matched '{best.get('name')}' in {city}, {state} "
                  f"but no centroid for that city")
            return None
        lat, lon = CITY_CENTROIDS[key]
        print(f"  Center: org HQ '{best.get('name')}' in {city}, {state} (ProPublica)")
        return {'lat': lat, 'lon': lon, 'city': city, 'state': state,
                'source': 'org-hq', 'matched_name': best.get('name')}
    except Exception as e:
        print(f"  Org HQ lookup failed: {e}")
        return None

def compute_center(geocoded, geocoded_center=None, center_city=None,
                   center_state=None, center_lat=None, center_lon=None,
                   org_name=None):
    """Decide the Regional/Local map center. Priority:
       1. center_zip (already matched to a real donor upstream)
       2. explicit center_lat / center_lon
       3. center_city + center_state (offline centroid)
       4. org_name -> nonprofit HQ via ProPublica (where the org IS)
       5. density-based auto-detect (densest metro cluster)."""
    if geocoded_center:
        return geocoded_center

    if center_lat is not None and center_lon is not None:
        try:
            print(f"  Center: explicit coordinates {center_lat}, {center_lon}")
            return {'lat': float(center_lat), 'lon': float(center_lon),
                    'city': '', 'state': '', 'source': 'coordinates'}
        except (TypeError, ValueError):
            print("  Ignoring invalid center_lat/center_lon")

    if center_city and center_state:
        key = (center_city.strip().upper(), state_abbrev(center_state))
        if key in CITY_CENTROIDS:
            lat, lon = CITY_CENTROIDS[key]
            print(f"  Center: specified city {center_city}, {center_state}")
            return {'lat': lat, 'lon': lon, 'city': center_city,
                    'state': center_state, 'source': 'city'}
        print(f"  Specified center city not found: {center_city}, {center_state} - using auto-detect")

    if org_name:
        org_center = lookup_org_center(org_name)
        if org_center:
            return org_center
        print(f"  Falling back to density auto-detect for '{org_name}'")

    lat, lon, size = density_center(geocoded)
    place = nearest_place(geocoded, lat, lon)
    print(f"  Center: auto-detected near {place.get('city', '')}, {place.get('state', '')} "
          f"({size} donors in cluster)")
    return {'lat': lat, 'lon': lon, 'city': place.get('city', ''),
            'state': place.get('state', ''), 'source': 'auto-density'}

def build_folium_map(data, zoom_level='continental', output_file='heatmap.png', center_address=None):
    """Build the Folium map and save it as HTML. Returns (html_file, png_file)
    for deferred rendering, or None if there is no data. Rendering is separated
    out so all maps can be screenshotted concurrently in one browser."""

    if not data:
        return None

    lats = [d['lat'] for d in data]
    lons = [d['lon'] for d in data]

    if center_address and zoom_level in ['regional', 'local']:
        center_lat = center_address['lat']
        center_lon = center_address['lon']
        print(f"  Using center: {center_address.get('city', '')}, {center_address.get('state', '')}")
    else:
        center_lat = np.median(lats)
        center_lon = np.median(lons)

    if zoom_level == 'continental':
        zoom = 5.25
        center_lat = 39.0
        center_lon = -96.0
    elif zoom_level == 'regional':
        zoom = 9
    else:
        zoom = 13.25

    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=zoom,
        tiles='https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png',
        attr=' ',
        zoom_control=False,
        scrollWheelZoom=False,
        doubleClickZoom=False,
        dragging=False
    )

    if zoom_level in ['regional', 'local']:
        heat_data = [[d['lat'], d['lon'], 1] for d in data]

        HeatMap(
            heat_data,
            min_opacity=0.08,
            max_opacity=0.18,
            radius=40,
            blur=40,
            gradient={
                0.0: 'rgba(0, 0, 255, 0.12)',
                0.2: 'rgba(0, 255, 255, 0.12)',
                0.4: 'rgba(0, 255, 0, 0.12)',
                0.6: 'rgba(255, 255, 0, 0.12)',
                0.8: 'rgba(255, 165, 0, 0.12)',
                1.0: 'rgba(255, 0, 0, 0.12)'
            }
        ).add_to(m)

    if zoom_level == 'continental':
        cluster_radius = 80
    elif zoom_level == 'regional':
        cluster_radius = 80
    else:
        cluster_radius = 60

    marker_cluster = MarkerCluster(
        max_cluster_radius=cluster_radius,
        disable_clustering_at_zoom=18,
        spiderfyOnMaxZoom=False
    ).add_to(m)

    for d in data:
        folium.Marker(
            location=[d['lat'], d['lon']],
            icon=folium.Icon(color='blue', icon='circle', prefix='fa')
        ).add_to(marker_cluster)

    html_file = output_file.replace('.png', '.html')
    m.save(html_file)
    print(f"  Built map HTML: {os.path.basename(html_file)}")

    return (html_file, output_file)

def render_maps(jobs):
    """Render a list of (html_file, png_file) pairs to PNG concurrently using a
    single headless browser, then delete the intermediate HTML files. Rendering
    all maps together overlaps the per-map tile-download waits and avoids
    launching Chromium once per map."""
    jobs = [j for j in jobs if j]
    if not jobs:
        return
    asyncio.run(_render_maps_async(jobs))
    for html_file, _ in jobs:
        try:
            os.remove(html_file)
        except OSError:
            pass

async def _render_maps_async(jobs):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            await asyncio.gather(*[_render_one(browser, h, png) for h, png in jobs])
        finally:
            await browser.close()

async def _render_one(browser, html_file, png_file):
    page = await browser.new_page(viewport={'width': 1920, 'height': 1080})
    try:
        file_url = f'file:///{os.path.abspath(html_file).replace(chr(92), "/")}'
        # Wait for map tiles to finish downloading rather than a fixed delay,
        # so slow tile servers don't produce half-rendered PNGs
        try:
            await page.goto(file_url, wait_until='networkidle', timeout=30000)
        except Exception:
            await page.goto(file_url)
            await page.wait_for_timeout(5000)
        await page.wait_for_timeout(2000)
        await page.screenshot(path=png_file, full_page=False)
        print(f"  Rendered PNG: {os.path.basename(png_file)}")
    finally:
        await page.close()

def create_interactive_html(data, output_file='interactive_map.html', center_address=None):
    """Create interactive HTML map with heat map toggle"""

    if not data:
        return None

    lats = [d['lat'] for d in data]
    lons = [d['lon'] for d in data]

    if center_address:
        center_lat = center_address['lat']
        center_lon = center_address['lon']
    else:
        center_lat = np.median(lats)
        center_lon = np.median(lons)

    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=12.5,
        tiles=None,
        control_scale=True,
        zoom_snap=0.1,
        zoom_delta=0.5,
        wheel_pxPerZoomLevel=120
    )

    folium.TileLayer(
        tiles='https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png',
        attr=' ',
        name='Base Map',
        overlay=False,
        control=False
    ).add_to(m)

    heat_gradient = {
        0.0: 'rgba(0, 0, 255, 0.12)',
        0.2: 'rgba(0, 255, 255, 0.12)',
        0.4: 'rgba(0, 255, 0, 0.12)',
        0.6: 'rgba(255, 255, 0, 0.12)',
        0.8: 'rgba(255, 165, 0, 0.12)',
        1.0: 'rgba(255, 0, 0, 0.12)'
    }

    heat_data = [[d['lat'], d['lon'], 1] for d in data]
    heat_layer = folium.FeatureGroup(name='Heat Map (Donors)', show=True)

    HeatMap(
        heat_data,
        min_opacity=0.08,
        max_opacity=0.18,
        radius=40,
        blur=40,
        gradient=heat_gradient
    ).add_to(heat_layer)

    heat_layer.add_to(m)

    # Optional layer weighted by giving amount instead of donor count
    max_giving = max((d.get('giving', 0) for d in data), default=0)
    if max_giving > 0:
        giving_data = [[d['lat'], d['lon'], d.get('giving', 0) / max_giving]
                       for d in data if d.get('giving', 0) > 0]
        giving_layer = folium.FeatureGroup(name='Heat Map (by Giving)', show=False)

        HeatMap(
            giving_data,
            min_opacity=0.08,
            max_opacity=0.18,
            radius=40,
            blur=40,
            gradient=heat_gradient
        ).add_to(giving_layer)

        giving_layer.add_to(m)

    marker_layer = folium.FeatureGroup(name='Markers', show=True)
    marker_cluster = MarkerCluster().add_to(marker_layer)

    for d in data:
        line1 = d['street'] if d['street'] else '(approximate location)'
        popup_html = f"""
        <div style="white-space: nowrap;">
            <b>{line1}</b><br>
            {d['city']}{',' if d['city'] else ''} {d['state']} {d['zip']}
        </div>
        """

        folium.Marker(
            location=[d['lat'], d['lon']],
            popup=folium.Popup(popup_html, max_width=300),
            icon=folium.Icon(color='blue', icon='circle', prefix='fa')
        ).add_to(marker_cluster)

    marker_layer.add_to(m)

    folium.LayerControl(collapsed=False, position='topright').add_to(m)

    m.save(output_file)
    print(f"  Saved interactive HTML: {output_file}")

    return output_file

@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'ok', 'service': 'Donation Heat Map API', 'version': '9.2-render'})

def write_no_maps_marker(output_directory, reason):
    """Write a marker file so Power Automate can tell maps were not generated"""
    try:
        os.makedirs(output_directory, exist_ok=True)
        marker = os.path.join(output_directory, NO_MAPS_FILENAME)
        with open(marker, 'w', encoding='utf-8') as f:
            f.write(f"No maps were generated.\nReason: {reason}\n")
        print(f"Wrote marker: {marker}")
    except Exception as e:
        print(f"Could not write no-maps marker: {e}")

@app.route('/api/generate-from-file', methods=['POST'])
def generate_from_file():
    """Generate maps from CSV file path and save to specified output directory"""
    output_directory = None
    try:
        data = request.json

        # Get file paths from request
        csv_file_path = data.get('csv_file_path')
        output_directory = data.get('output_directory')
        center_zip = data.get('center_zip', None)
        center_city = data.get('center_city', None)
        center_state = data.get('center_state', None)
        center_lat = data.get('center_lat', None)
        center_lon = data.get('center_lon', None)
        org_name = data.get('org_name', None)
        major_donors_csv = data.get('major_donors_csv', None)

        if not csv_file_path or not output_directory:
            return jsonify({
                'success': False,
                'error': 'Missing csv_file_path or output_directory'
            }), 400

        os.makedirs(output_directory, exist_ok=True)

        # Clear any marker left over from a previous failed run
        stale_marker = os.path.join(output_directory, NO_MAPS_FILENAME)
        if os.path.exists(stale_marker):
            os.remove(stale_marker)

        if not os.path.exists(csv_file_path):
            write_no_maps_marker(output_directory, f'CSV file not found: {csv_file_path}')
            return jsonify({
                'success': False,
                'error': f'CSV file not found: {csv_file_path}'
            }), 400

        # Try multiple encodings
        encodings = ['utf-8', 'latin-1', 'cp1252', 'iso-8859-1']
        csv_text = None
        for encoding in encodings:
            try:
                with open(csv_file_path, 'r', encoding=encoding) as f:
                    csv_text = f.read()
                print(f"Successfully read file with {encoding} encoding")
                break
            except UnicodeDecodeError:
                continue

        if csv_text is None:
            write_no_maps_marker(output_directory, 'Could not decode CSV file with any supported encoding')
            return jsonify({'success': False, 'error': 'Could not decode CSV file with any supported encoding'}), 400

        addresses, center_address = parse_addresses(csv_text, center_zip)

        if not addresses:
            write_no_maps_marker(output_directory, 'No valid addresses found in CSV')
            return jsonify({'success': False, 'error': 'No valid addresses found'}), 400

        print(f"\n{'='*70}")
        print(f"Processing {len(addresses)} addresses from: {csv_file_path}")
        print(f"Output directory: {output_directory}")
        if center_zip:
            print(f"Center ZIP requested: {center_zip}")
        print(f"{'='*70}")

        start_time = time.time()
        geocoded, geocoded_center, geocode_stats = geocode_addresses(addresses, center_address)
        geocode_time = time.time() - start_time

        print(f"Geocoding completed in {geocode_time:.1f} seconds")

        if not geocoded:
            write_no_maps_marker(output_directory, 'No addresses could be geocoded')
            return jsonify({'success': False, 'error': 'No addresses could be geocoded'}), 400

        # If no explicit center was given, derive the org name from the
        # output path and look up its HQ so national orgs center on home
        # base rather than their densest donor metro.
        if not org_name:
            org_name = org_name_from_path(output_directory)
            if org_name:
                print(f"Org name from path: {org_name}")

        center_point = compute_center(geocoded, geocoded_center, center_city,
                                      center_state, center_lat, center_lon,
                                      org_name)

        print("\nGenerating maps...")
        # Build each map's HTML, then render all PNGs concurrently in one
        # browser at the end (rendering is the bulk of the runtime).
        render_jobs = []

        print("  All Donors view...")
        continental_file = os.path.join(output_directory, 'All Donors.png')
        render_jobs.append(build_folium_map(geocoded, 'continental', continental_file, center_point))

        # Generate Major Donors map if file provided
        major_donors_file = None
        if major_donors_csv:
            print("  Major Donors view...")
            if os.path.exists(major_donors_csv):
                major_csv_text = None
                for encoding in encodings:
                    try:
                        with open(major_donors_csv, 'r', encoding=encoding) as f:
                            major_csv_text = f.read()
                        break
                    except UnicodeDecodeError:
                        continue

                if not major_csv_text:
                    print(f"    Could not decode Major Donors file")
                else:
                    major_addresses, major_center = parse_addresses(major_csv_text, center_zip)

                    if major_addresses:
                        print(f"    Processing {len(major_addresses)} major donor addresses...")
                        major_geocoded, major_geocoded_center, _ = geocode_addresses(major_addresses, major_center)

                        if major_geocoded:
                            major_donors_file = os.path.join(output_directory, 'Major Donors.png')
                            render_jobs.append(build_folium_map(major_geocoded, 'continental', major_donors_file, major_geocoded_center))
                        else:
                            print("    No major donor addresses could be geocoded")
                    else:
                        print("    No valid major donor addresses found")
            else:
                print(f"    Major Donors file not found: {major_donors_csv}")

        print("  Regional view...")
        regional_file = os.path.join(output_directory, 'Regional Donors.png')
        render_jobs.append(build_folium_map(geocoded, 'regional', regional_file, center_point))

        print("  Local view...")
        local_file = os.path.join(output_directory, 'Local Donors.png')
        render_jobs.append(build_folium_map(geocoded, 'local', local_file, center_point))

        print("  Rendering PNGs concurrently...")
        render_maps(render_jobs)

        print("  Interactive HTML...")
        interactive_file = os.path.join(output_directory, 'Interactive Donor Map.html')
        create_interactive_html(geocoded, interactive_file, center_point)

        total_time = time.time() - start_time
        print(f"\n{'='*70}")
        print(f"Complete! Generated files in {total_time:.1f} seconds")
        print(f"{'='*70}\n")

        files_generated = [
            continental_file,
            regional_file,
            local_file,
            interactive_file
        ]

        if major_donors_file:
            files_generated.insert(1, major_donors_file)

        return jsonify({
            'success': True,
            'files': files_generated,
            'addresses_processed': len(geocoded),
            'total_addresses': len(addresses),
            'precision': geocode_stats,
            'center': {
                'lat': center_point['lat'],
                'lon': center_point['lon'],
                'city': center_point.get('city', ''),
                'state': center_point.get('state', ''),
                'source': center_point.get('source', '')
            },
            'processing_time': round(total_time, 1),
            'timings': {
                'geocode_seconds': round(geocode_time, 1),
                'render_seconds': round(total_time - geocode_time, 1),
                'total_seconds': round(total_time, 1)
            }
        })

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        if output_directory:
            write_no_maps_marker(output_directory, f'Unexpected error: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500

if __name__ == '__main__':
    print("=" * 70)
    print("DONATION HEAT MAP API")
    print("=" * 70)
    print("\nGenerates PNG images + interactive HTML:")
    print("  1. All Donors (zoom 5.25)")
    print("  2. Major Donors (zoom 5.25) - optional")
    print("  3. Regional (zoom 9)")
    print("  4. Local (zoom 13.25)")
    print("  5. Interactive HTML (zoom 12.5, toggleable layers)")
    print("\nPress CTRL+C to quit")
    print("=" * 70 + "\n")
    from waitress import serve
    serve(app, host='0.0.0.0', port=5000, threads=4)
