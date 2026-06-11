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
import asyncio
from playwright.async_api import async_playwright
import requests
import time
import csv
from io import StringIO

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

    # Find the ZIP with the most addresses
    if addresses:
        if center_zip:
            for addr in addresses:
                if addr['zip'].startswith(center_zip):
                    center_address = addr
                    print(f"Using specified center ZIP: {center_zip}")
                    break

        if not center_address and zip_counts:
            most_common_zip = max(zip_counts, key=zip_counts.get)
            count = zip_counts[most_common_zip]
            print(f"Auto-selected center ZIP: {most_common_zip} ({count} addresses)")

            for addr in addresses:
                if addr['zip'] == most_common_zip:
                    center_address = addr
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

def lookup_centroid(addr):
    """Resolve a partial address to a ZIP or city centroid.
    Returns (lat, lon, precision) or (None, None, None)."""
    if addr['zip'] and addr['zip'] in ZIP_CENTROIDS:
        lat, lon = ZIP_CENTROIDS[addr['zip']]
        return lat, lon, 'zip'
    key = (addr['city'].upper(), addr['state'].upper())
    if addr['city'] and addr['state'] and key in CITY_CENTROIDS:
        lat, lon = CITY_CENTROIDS[key]
        return lat, lon, 'city'
    return None, None, None

# Jitter (std dev in degrees) by placement precision, so centroid-placed
# donors scatter naturally instead of stacking on a single point
JITTER = {'address': 0.0001, 'zip': 0.008, 'city': 0.02}

def geocode_addresses(addresses, center_address=None):
    """Geocode addresses: Census API for full street addresses (parallel),
    offline ZIP/city centroids for partial ones (and as fallback when the
    Census API finds no match)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

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

    print(f"Geocoding {len(full)} full addresses using Census API "
          f"({len(partial)} partial addresses via centroids)...")
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=20) as executor:
        future_to_addr = {
            executor.submit(geocode_address_census, addr['street'], addr['city'], addr['state'], addr['zip']): addr
            for addr in full
        }

        completed = 0
        for future in as_completed(future_to_addr):
            addr = future_to_addr[future]
            completed += 1

            if completed % 100 == 0 or completed == len(full):
                elapsed = time.time() - start_time
                rate = completed / elapsed if elapsed > 0 else 0
                print(f"  Progress: {completed}/{len(full)} ({rate:.1f} req/sec, {elapsed:.1f}s elapsed)")

            try:
                lat, lon = future.result()
            except Exception:
                lat, lon = None, None

            if lat and lon:
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

def create_folium_map(data, zoom_level='continental', output_file='heatmap.png', center_address=None):
    """Create map using Folium and convert to PNG using Playwright"""

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
    print(f"  Saved HTML: {html_file}")

    try:
        asyncio.run(html_to_png(html_file, output_file))
        print(f"  Converted to PNG: {output_file}")
        os.remove(html_file)
    except Exception as e:
        print(f"  Error converting to PNG")
        raise

    return output_file

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

async def html_to_png(html_file, png_file):
    """Convert HTML to PNG using Playwright"""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={'width': 1920, 'height': 1080})

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

        await browser.close()

@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'ok', 'service': 'Donation Heat Map API', 'version': '9.0'})

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

        if geocoded_center:
            print(f"Center: {geocoded_center['city']}, {geocoded_center['state']}")

        print("\nGenerating maps...")
        print("  All Donors view...")
        continental_file = os.path.join(output_directory, 'All Donors.png')
        create_folium_map(geocoded, 'continental', continental_file, geocoded_center)

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
                            create_folium_map(major_geocoded, 'continental', major_donors_file, major_geocoded_center)
                        else:
                            print("    No major donor addresses could be geocoded")
                    else:
                        print("    No valid major donor addresses found")
            else:
                print(f"    Major Donors file not found: {major_donors_csv}")

        print("  Regional view...")
        regional_file = os.path.join(output_directory, 'Regional Donors.png')
        create_folium_map(geocoded, 'regional', regional_file, geocoded_center)

        print("  Local view...")
        local_file = os.path.join(output_directory, 'Local Donors.png')
        create_folium_map(geocoded, 'local', local_file, geocoded_center)

        print("  Interactive HTML...")
        interactive_file = os.path.join(output_directory, 'Interactive Donor Map.html')
        create_interactive_html(geocoded, interactive_file, geocoded_center)

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
            'processing_time': round(total_time, 1)
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
