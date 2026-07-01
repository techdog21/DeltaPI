# DeltaPI — Off-Grid Camp Monitor

![Dashboard — battery, solar & Starlink](server/static/deltapi.png)
![Dashboard — weather, Pi health & charts](server/static/deltapi1.png)

DeltaPI started as a Victron solar monitor and grew into a single-screen **camp
systems dashboard** for an off-grid RV: **power, connectivity, and weather** at a
glance. A Raspberry Pi in the rig gathers data from several sources and bulk-uploads
to a small Flask app on Render.com, which renders a fast, mobile-friendly dashboard.

## Data sources

| Source | How | Provides |
|---|---|---|
| **Victron MPPT** (SmartSolar) | VE.Direct over USB serial | Solar power, panel voltage, charge current, charge mode, daily/lifetime yield, controller errors |
| **Renogy Pro batteries** (`RBT12100LFP-BT`) | Bluetooth LE (BMS) | True coulomb-counted SOC, current, voltage, temperature, per-cell voltages |
| **Starlink dish** (round + Mini) | local gRPC API (`192.168.100.1:9200`) | Connection status, obstruction %, alerts, throughput, latency, GPS location |
| **Open-Meteo** | server-side HTTP (free, no key) | Current conditions, cloud cover, solar-radiation forecast, sunrise/sunset, air quality (US AQI + PM2.5) |
| **NWS** (`api.weather.gov`) | server-side HTTP (free, US only) | Active severe-weather watches/warnings for the current location |

The MPPT only measures *charge* current, never house load — so consumption and
runtime are derived from the **batteries' measured net current** (see
[Runtime & consumption](#runtime--consumption)).

## Dashboard

Six info panels (an even 3×2 grid), eight charts, and a readings table. Status pills are consistent:
**green = good, yellow = warning, red = problem, gray = informational/idle** (except
solar production, which has no "bad" state — green = producing, gray = off).

- **Battery Array** (measured over BLE): SOC, battery voltage, per-battery feed-health
  pill, temperature (with a LiFePO4 cold-charge warning), cell balance, consumption
  (W and A), runtime, time-to-full.
- **Solar System**: data freshness, controller health (VE.Direct `ERR`), charge mode
  (Bulk/Absorption/Float), solar power (W and A), yield today, panel voltage, and a
  **Sustainability Outlook** — Self-sufficient / Sustaining / Drawing down ~N days /
  Critical — fusing measured daily harvest vs. consumption with the solar forecast
  (see [Runtime & consumption](#runtime--consumption)).
- **Starlink**: connection status, obstruction %, alerts (mast-not-vertical, thermal,
  roaming, water…), throughput, latency.
- **Weather**: current conditions, cloud cover, tomorrow's charging outlook, freeze
  warning. Located from the dish's GPS, or a configured home fallback.
- **Environment**: severe-weather alert (NWS watches/warnings — storm/wind/flood/fire),
  air quality (US AQI + PM2.5 as a wildfire-smoke proxy), wind + gusts, humidity + dew
  point (with a condensation-risk flag), tonight's low, today's solar window
  (sunrise–sunset + hours of sun left), site elevation, and moon phase + illumination.
- **Pi Health**: serial link, OS, uptime, last check-in, CPU/fan, updates, mem/disk,
  Wi-Fi, container disk.
- **Charts** (Chart.js): Solar Power, Battery Voltage, Daily Energy (kWh), Battery SOC
  (with a red danger floor), Consumption, Charge Power, Battery Temp (with a freezing
  line), Daily Consumption.
- Latest-readings table, light/dark theme (cookie-persisted), auto-refresh, and a
  responsive layout that stacks to one scrolling column on a phone.

## Repo layout

| Path | Runs on | Purpose |
|---|---|---|
| `server/app.py` | Render | Flask entry point: ingestion routes (`/log`, `/log/bulk`, `/status`), `/encrypt_days`, and the dashboard route |
| `server/config.py` | Render | Env-derived settings and static lookup tables (VE.Direct/WMO/AQI/flood maps) |
| `server/util.py` | Render | Cross-cutting helpers: logging, formatters, token decryption, geo + moon math |
| `server/db.py` | Render | SQLite schema, per-request connections, and throttled retention cleanup |
| `server/integrations.py` | Render | Cached, failure-tolerant external providers (weather, AQI, NWS, fire, quake, aurora, river, geocode) |
| `server/energy.py` | Render | Battery/solar model: SOC estimate, runtime, sustainability outlook, empirical load |
| `server/dashboard.py` | Render | Builds the template context from log rows + Pi status |
| `server/templates/index.html`, `server/static/*` | Render | Dashboard markup, styles, and Chart.js wiring |
| `server/requirements.txt` | Render | Server Python dependencies |
| `vedirect_logger.py` | Pi (systemd `vedirect_logger`) | Read VE.Direct serial, buffer/upload, post Pi health, control the cooling fan, merge battery + Starlink state into uploads |
| `renogy_ble.py` | Pi (systemd `renogy_ble`) | Poll the batteries over BLE → `battery_state.json` |
| `starlink_poll.py` | Pi (systemd `starlink_poll`) | Poll the Starlink dish over gRPC → `starlink_state.json` |
| `renogybt/` | Pi | Vendored, trimmed + patched [`cyrils/renogy-bt`](https://github.com/cyrils/renogy-bt) (battery path only; fixes the Pro batteries' duplicate-GATT-UUID notify bug) |
| `deploy/` | Pi | systemd units, log-rotation config, and Pi-only requirement lists |

## Architecture

Three independent pollers on the Pi each write a small JSON state file; the logger
merges them and uploads. Decoupling means a Bluetooth or Starlink hiccup never
affects serial logging or uploads.

- **Pi → server**: bulk upload to `/log/bulk` every `UPLOAD_INTERVAL` (30 s). Uploads
  are chunked under the server's 1 MB limit so a backlog drains across requests
  instead of failing as one oversized POST.
- **Merged feeds**: each uploaded frame carries the latest `battery` and `starlink`
  snapshots (with their own timestamps), so the dashboard can show whether each feed
  is live, stale, or down rather than trusting old numbers.
- **Local buffering / archive**: frames are written to JSONL with byte-offset
  tracking (survives outages) and archived 14 days on the SD card (pruned at most
  once a day to limit wear).
- **Backfill**: if the server DB is ever empty, the Pi replays its archive.
- **Persistent server DB**: SQLite on a Render Disk at `/data`; 365 days of solar logs,
  7 days of Pi status, pruned daily.

## Runtime & consumption

The MPPT can't see house load, so:

1. **Measured (preferred)** — with a live BLE feed, **house load = MPPT output power −
   battery net power** (valid charging or discharging), and SOC is the batteries' true
   coulomb count. Runtime = usable charge (down to a ~10% floor) ÷ measured load.
2. **Estimated (fallback)** — if the battery feed is offline, an energy balance over a
   trailing 72 h window (lifetime yield `H19` minus voltage-based stored-energy change),
   shown labeled `(est)`. Measured last-known values are preferred over this when
   available, so the blunt estimate rarely appears.

`Runtime` is deliberately a *battery-only, "if the sun vanished now"* figure. For the
question that actually matters off-grid — **am I in surplus or deficit going forward?**
— the Solar panel's **Sustainability Outlook** fuses three horizons: *now* (is the
battery charging?), *multi-day* (measured daily harvest vs. consumption → a buffer in
**days**, not hours, when in deficit), and the *solar forecast* — today's remaining
sun (radiation × daylight left) **or** tomorrow's (can the sun keep up?).
States: **Self-sufficient** (building/holding surplus), **Sustaining** (break-even),
**Drawing down ~N days** (deficit; annotated *recovering* when the forecast turns it
around), and **Critical** (low SOC with no sun coming). It shows **Gathering data**
until there's enough daily history to judge.

## Environment variables

| Variable | Where | Required | Purpose |
|---|---|---|---|
| `POST_SECRET` | Server + Pi | Yes | Bearer token authenticating POST requests |
| `FERNET_KEY` | Server | Yes | Fernet key for date-range tokens |
| `BASE_URL` | Pi | Yes | Server URL the logger uploads to |
| `DB_DIR` | Server | No | DB/log dir (default `/data`) |
| `HOME_LAT` / `HOME_LON` | Server | No | Home weather location (decimal degrees) used when the dish isn't sharing GPS. Kept out of the repo for privacy. |
| `HOME_DISH_ID` | Server | No | Home dish id; weather hides rather than showing home coords when roaming on a different, no-GPS dish |

`POST_SECRET` and `FERNET_KEY` are validated at startup; the Pi exits if `POST_SECRET`
or `BASE_URL` is missing.

## Pi setup

Assumes a clean Raspberry Pi OS. Replace `pi` / paths to match your user.

**1. System + serial**
```bash
sudo apt update && sudo apt install -y python3-venv python3-rpi.gpio git
sudo mkdir -p /var/log/vedirect && sudo chown $USER:$USER /var/log/vedirect
sudo usermod -aG dialout $USER          # serial access; re-login after
ls /dev/ttyUSB*                          # confirm /dev/ttyUSB0 (the MPPT)
git clone https://github.com/techdog21/deltapi.git ~/deltapi
```

**2. Core logger** — `pip3 install pyserial requests`, then create
`/etc/systemd/system/vedirect_logger.service`:
```ini
[Unit]
Description=DeltaPI VE.Direct Logger
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/deltapi
Environment="POST_SECRET=your-secret"
Environment="BASE_URL=https://your-server.example.com"
ExecStart=/usr/bin/python3 /home/pi/deltapi/vedirect_logger.py
Restart=on-failure
RestartSec=10
[Install]
WantedBy=multi-user.target
```
`sudo systemctl enable --now vedirect_logger`. Within ~30 s,
`tail -f /var/log/vedirect/vedirect_error.log` should show `[Serial] Connected`,
`[Status] POST succeeded`, `[Upload] Bulk POST succeeded`.

**3. Log rotation**
```bash
sudo cp deploy/vedirect-logrotate.conf /etc/logrotate.d/vedirect   # edit its su line
```

**4. Battery (BLE) poller** — needs a venv (kept off the Render server):
```bash
python3 -m venv ~/deltapi-venv
~/deltapi-venv/bin/pip install -r ~/deltapi/deploy/requirements-ble.txt
sudo systemctl enable --now bluetooth
# edit the battery MACs/aliases at the top of renogy_ble.py, then:
sudo cp deploy/renogy_ble.service /etc/systemd/system/   # adjust user/paths
sudo systemctl enable --now renogy_ble
```

**5. Starlink poller**
```bash
git clone https://github.com/sparky8512/starlink-grpc-tools ~/starlink-grpc-tools
~/deltapi-venv/bin/pip install -r ~/deltapi/deploy/requirements-starlink.txt
sudo cp deploy/starlink_poll.service /etc/systemd/system/   # adjust user/paths
sudo systemctl enable --now starlink_poll
```
Status/obstruction/alerts work immediately. **Location → weather** also needs the
"Starlink location" / "Allow access on local network" setting enabled in the Starlink
app (the Mini exposes it; the older round dish may not — use `HOME_LAT/LON` there).

## Server (Render)

- Build: `pip install -r server/requirements.txt` · Start: `gunicorn --chdir server app:app`
- Starter plan with a 1 GB Disk mounted at `/data`
- Set the env vars above in the Render dashboard
- Local dev: `cd server && POST_SECRET=x FERNET_KEY=$(...) python app.py`

## Routes

| Route | Method | Rate limit | Description |
|---|---|---|---|
| `/` | GET | — | Dashboard |
| `/log` | POST | 3/min | Single solar entry |
| `/log/bulk` | POST | 5/min | Bulk entries (primary ingestion) |
| `/status` | POST | 2/min | Pi health + merged feeds |
| `/encrypt_days` | GET | 10/min | Encrypted date-range token |

## Security

Bearer-token auth (constant-time) on all POSTs, HTTPS enforced, Flask-Limiter rate
limits, 1 MB body cap (→ `413`), input validation, HTML escaping of all device-reported
fields, Fernet-encrypted date tokens, thread-safe DB cleanup, and no secrets in the repo.

## Fan control (Pi)

PWM fan on GPIO 18 by CPU temp: off below 30 °C, linear 20→100 % from 30→50 °C, full
above 50 °C; holds last speed if the temperature can't be read.

## License

MIT — see [LICENSE](LICENSE).

## Author

DeltaPI Project — Jerry Craft
