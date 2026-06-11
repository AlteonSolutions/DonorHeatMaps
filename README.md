# Donation Heat Map API

Flask API that turns a CSV of donor addresses into heat maps and clustered marker maps. Addresses are geocoded with the free US Census Geocoder, maps are built with Folium, and static PNGs are rendered with Playwright (headless Chromium). Designed to run as a Windows Service and be triggered from Power Automate, but it runs anywhere Python does.

*Version: 9.0*

---

## Setup

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

**Run the server:**

```bash
python app.py
```

The API listens on `http://localhost:5000`.

## Deploying updates to the production PC

The PC running the service should be a git clone of this repo (one-time setup):

```bash
git clone https://github.com/AlteonSolutions/DonorHeatMaps.git C:\DonorMaps
```

After that, run [`update.bat`](update.bat) on the PC whenever the repo changes — it pulls the latest code, installs any new dependencies, and restarts the `DonorMapsAPI` service. It can be run manually, scheduled in Task Scheduler, or triggered from a Power Automate "Run application" block.

## API

### `GET /api/health`

Health check. Returns service name and version.

### `POST /api/generate-from-file`

Generates all maps from a CSV file on disk.

```json
{
  "csv_file_path": "C:/DonorMaps/donors.csv",
  "output_directory": "C:/DonorMaps/output",
  "center_zip": "19104",
  "major_donors_csv": "C:/DonorMaps/major_donors.csv"
}
```

- `csv_file_path` (required) — path to the donor CSV
- `output_directory` (required) — where generated maps are written (created if missing)
- `center_zip` (optional) — ZIP code to center the Regional/Local maps on
- `major_donors_csv` (optional) — separate CSV for the Major Donors map

**CSV format:** delimiter is auto-detected (comma or tab), first row is treated as a header, and at least 6 columns are expected: `ID, Name, Street, City, State, Zip, Giving`.

**Partial addresses are supported.** Each row is placed as precisely as its data allows:

| Row has | Placement |
|---|---|
| Street + (ZIP or City/State) | Census Geocoder (street-level) |
| ZIP only | ZIP centroid (offline lookup, bundled in `data/`) |
| City + State only | City centroid (offline lookup) |
| None of the above | Skipped |

If the Census Geocoder finds no match for a full address, it falls back to the ZIP/city centroid instead of dropping the donor. Centroid-placed donors get extra positional jitter (~0.5 mi for ZIP, ~1.5 mi for city) so they scatter naturally instead of stacking on one point. The API response includes a `precision` breakdown of how donors were placed.

**Failure signal:** if no maps can be generated for any reason (CSV missing, unreadable, no usable addresses, nothing geocoded, unexpected error), the API writes `NO MAPS GENERATED.txt` into the output directory with the reason. Power Automate flows can wait for either the PNGs or this marker file instead of stalling. The marker is cleared automatically at the start of each run.

### Expected Outputs

Files generated in your Donor Maps folder:
- `All Donors.png`
- `Major Donors.png` (if Major Donors.csv provided)
- `Regional Donors.png`
- `Local Donors.png`
- `Interactive Donor Map.html`

Or, if generation failed:
- `NO MAPS GENERATED.txt` (contains the failure reason)

The interactive HTML includes toggleable layers: **Heat Map (Donors)** (on by default), **Heat Map (by Giving)** (weighted by donation amount, off by default — only present if the Giving column has values), and **Markers**.

---

## Troubleshooting

### Service Won't Start

**Check error log:**

```bash
type C:\DonorMaps\service_error.log
```

**Common fixes:**
- Verify Python path in NSSM configuration
- Reinstall Playwright: `python -m playwright install chromium`
- Check PLAYWRIGHT_BROWSERS_PATH environment variable

### JSON Parsing Error

**Error:** `Invalid \escape` or `Unterminated string`

**Cause:** Backslashes in file path

**Fix:** Use the Replace Text action in Power Automate to convert `\` to `/`

### Geocoding Failures

**Check:**
- Internet connection (Census API requires it)
- Address format (street, city, state, ZIP all required)
- CSV encoding (UTF-8 recommended)

**Common failures:**
- International addresses (non-US)
- PO Boxes (no street address)
- Malformed ZIP codes

### No Maps Generated

**Check:**
- Service is running: `nssm status DonorMapsAPI`
- CSV file exists at specified path
- All required columns present
- At least one address with valid geocoding

---

## Performance Metrics

- **Geocoding rate:** 4-5 addresses/second (Census API dependent)
- **Processing time for 500 addresses:** ~2-3 minutes
- **PNG generation per map:** ~10-15 seconds
- **Total latency (end-to-end):** ~156 seconds for 512 addresses

**Bottleneck:** Census Geocoding API (external, rate varies)

---

## Boundary & Filtering Logic

### Regional & Local Definitions

**Important:** Regional and Local are **NOT geographic boundaries**. They are zoom levels applied to all input data.

- **All Donors:** Continental view (zoom 5.25) at US center
- **Major Donors:** Continental view (zoom 5.25) at US center (separate data)
- **Regional:** Same data as All Donors, zoomed to 9, centered on highest-concentration ZIP
- **Local:** Same data as All Donors, zoomed to 13.25, centered on highest-concentration ZIP

**Geographic filtering happens in your CSV**, not in the API.

### Center Selection

1. **Manual:** Pass `center_zip` parameter (e.g., "19104")
   - System finds first address with that ZIP code
   - Uses as regional/local center

2. **Automatic:** If no `center_zip` provided
   - Counts occurrences of each ZIP code
   - Selects ZIP with most addresses
   - Uses first address with that ZIP as center

### Exclusions

**Addresses are skipped only if:**
- Street, ZIP, and City/State are ALL missing (nothing to place the donor with)
- ZIP is non-numeric AND no other usable location data exists
- Geocoding fails at every level (no Census match, ZIP not in centroid table, city not in centroid table)

Rows missing a street address are no longer skipped — they fall back to ZIP or city centroid placement (see the API section above).

---

## Code

The complete API lives in [`app.py`](app.py).

---

## Project Timeline

- **Nov 25, 2025:** Initial development started
- **Dec 3, 2025:** Core API features finalized
- **Dec 15-16, 2025:** VM deployment and Windows Service setup
- **Dec 18, 2025:** Major Donors feature, auto-center ZIP, map refinements
- **Feb 24, 2026:** New client deployment, encoding fixes
- **Present:** Production operation, continuous refinement

**Total Development Time:** ~30 hours across 6 major phases

---

## Project Status

✅ **PRODUCTION READY**

- API server stable and running 24/7
- Windows Service auto-starts on PC boot
- Power Automate integration tested and working
- Geocoding performance optimized (4-5 addr/sec)
- Map generation quality validated
- Interactive HTML fully featured
- Multiple clients deploying successfully

---

## License & Attribution

- **Flask:** BSD License
- **Folium:** MIT License
- **Playwright:** Apache 2.0 License
- **US Census Geocoder:** Public Domain

---

## Contact & Support

For issues, enhancements, or documentation updates, refer to:
- Service logs: `C:\DonorMaps\service_output.log` and `service_error.log`
- API health check: `http://localhost:5000/api/health`
- Windows Event Viewer: Application logs
