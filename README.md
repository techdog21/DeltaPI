# DeltaPI Solar Monitor

![Server Dashboard](static/deltapi.png)

DeltaPI is a lightweight Python project for monitoring solar performance from a Victron VE.Direct compatible charge controller, using a Raspberry Pi Zero and a Flask-based dashboard hosted on Render.com.

The Raspberry Pi Zero is powered via a 12V-to-5V step-down converter and connected to the charge controller via USB. It buffers solar data locally and bulk-uploads to the server roughly every 30 seconds (see `UPLOAD_INTERVAL` in `vedirect_logger.py`).

## Project Components

1. **`vedirect_logger.py`** -- Python client running on the Pi Zero as a systemd service. Reads VE.Direct serial data, logs locally to JSONL, bulk-uploads to the server, monitors Pi system health, and controls a PWM cooling fan.
2. **`app.py`** -- Flask web app that receives, stores, and displays solar metrics on a single-page dashboard with light/dark theme support.
3. **Wi-Fi Connectivity** -- The Pi connects over 2.4GHz Wi-Fi. Starlink dual-band works well for this.

## Dashboard Features

Three summary panels plus charts and a readings table:

- **Battery Array** (measured over Bluetooth): SOC, battery voltage, per-battery
  feed health pill, temperature (with a LiFePO4 cold-charge warning), cell balance,
  consumption, runtime, and time-to-full.
- **Solar System**: data-freshness, controller health (from the VE.Direct `ERR`
  code), charge mode (Bulk/Absorption/Float), solar power, yield today, and panel
  voltage.
- **Starlink** (via the dish's local gRPC API): connection status, obstruction %,
  alerts (mast-not-vertical, thermal, roaming, water, …), throughput, latency.
- **Weather** (Open-Meteo, located from the dish's GPS): current conditions, cloud
  cover, tomorrow's charging outlook, and a freeze warning.
- **Pi Health**: serial link, OS, uptime, last check-in, CPU/fan, updates,
  memory/disk, Wi-Fi, container disk.
- **Charts** (Chart.js): Solar Power (W), Battery Voltage (V), Daily Energy (kWh),
  Battery SOC (%) with a red danger floor, Consumption (W), Charge Power (W, MPPT
  output to the battery), Battery Temp (°F) with a freezing line, and Daily
  Consumption (kWh).
- Latest readings table, light/dark theme toggle (cookie-persisted), and a
  responsive layout that stacks to a single scrolling column on iPhone.

Status pills follow a consistent meaning — green = good, yellow = warning,
red = problem, gray = informational/idle — except solar production (no "bad"
state: green = producing, gray = off).

## Architecture

- **Pi to Server**: Bulk upload via `/log/bulk` every `UPLOAD_INTERVAL` seconds (default 30s)
- **Local buffering**: Pi logs all frames to JSONL with byte-offset tracking; unsent data survives network outages. Uploads are chunked under the server's 1 MB limit, so a large backlog drains across several requests instead of failing as one oversized POST.
- **14-day local archive**: Sent data is archived on the Pi's SD card (pruned at most once a day to limit SD-card wear)
- **Backfill on redeploy**: If the server database is ever empty, the Pi replays its full archive automatically
- **Persistent server DB**: SQLite lives on a Render Disk mounted at `/data`, so it survives restarts and redeploys
- **Deduplication**: Server skips entries with timestamps already in the database
- **Thread safety**: Cleanup operations are guarded by a threading lock for safe use under gunicorn

## Runtime Estimation

The MPPT controller reports only charge current, not house load, so runtime can't be read from it directly. Two sources, in priority order:

1. **Measured (preferred)** — when the Bluetooth battery feed is live (see below), the dashboard uses the batteries' true coulomb-counted SOC and, while discharging, their actual net current as the real house load. Runtime = usable charge (down to a ~10% floor) ÷ measured load.
2. **Estimated (fallback)** — if the battery feed is stale or offline, the server falls back to an **energy balance** over a trailing 72-hour window: solar harvested (the controller's lifetime yield counter `H19`) minus the change in voltage-based stored energy. Clearly labeled `(est)` on the dashboard.

## Battery Monitoring (Bluetooth)

The Renogy Pro smart batteries (e.g. `RBT12100LFP-BT`) expose true SOC, current, voltage, temperature, and per-cell voltages over Bluetooth. `renogy_ble.py` runs as its own systemd service (`renogy_ble`), polling each battery every ~60 s (connect → read → disconnect) and writing `/var/log/vedirect/battery_state.json`. The main logger merges that file into its uploads, so the dashboard shows **measured** SOC/load and a **battery-feed health pill** (🟢 Live / 🟡 Stale / 🔴 Offline) — if the feed breaks you see it immediately instead of trusting old numbers.

Implementation notes:
- Uses a **vendored, patched** copy of [`cyrils/renogy-bt`](https://github.com/cyrils/renogy-bt) in `renogybt/` — patched so the Pro batteries' duplicate GATT characteristic UUIDs don't break the notify subscription.
- Runs in a **separate venv** on the Pi (`deploy/requirements-ble.txt` → `bleak`); deliberately **not** in the top-level `requirements.txt`, so the Render server never builds Bluetooth packages.
- Setup: install the venv + deps, edit the battery MAC addresses/aliases at the top of `renogy_ble.py`, then install `deploy/renogy_ble.service`. The Pi's `bluetooth` service must be enabled (`sudo systemctl enable --now bluetooth`).
- One battery BLE connection at a time: while the Pi is polling, the Renogy phone app may not connect (and vice-versa).

## Starlink & Weather

`starlink_poll.py` runs as its own systemd service (`starlink_poll`), querying the
Starlink dish's local gRPC API (`192.168.100.1:9200`) every ~60 s and writing
`/var/log/vedirect/starlink_state.json`, which the logger merges into uploads (same
pattern as the battery feed). It powers the **Starlink** panel (status / obstruction
/ alerts / throughput / latency) and provides the dish's **GPS location**, which the
server uses to fetch **weather** from [Open-Meteo](https://open-meteo.com) (free, no
API key, cached ~20 min) for the **Weather** panel.

Setup:
- Clone the gRPC tool and install Pi-side deps (not vendored — large / unclear license):
  `git clone https://github.com/sparky8512/starlink-grpc-tools ~/starlink-grpc-tools`
  then `deltapi-venv/bin/pip install -r deploy/requirements-starlink.txt`.
- Install `deploy/starlink_poll.service` (adjust user/paths) and enable it.
- Status/obstruction/alerts work immediately. **Location/weather** additionally
  requires enabling **"Allow access on local network"** in the Starlink app — works
  on both the round/standard dish and the Mini (both have GPS + the same API).

## Environment Variables

| Variable | Where | Required | Purpose |
|----------|-------|----------|---------|
| `POST_SECRET` | Server + Pi | Yes | Bearer token for authenticating POST requests |
| `FERNET_KEY` | Server only | Yes | Fernet encryption key for date range tokens |
| `BASE_URL` | Pi only | Yes | Server URL the logger uploads to |
| `DB_DIR` | Server only | No | Database/log directory (default `/data`); useful for local development |

Both `POST_SECRET` and `FERNET_KEY` are validated at startup. The server logs warnings if either is missing; the Pi exits immediately if `POST_SECRET` or `BASE_URL` is not set.

## Requirements

- Python 3.9+
- Raspberry Pi with VE.Direct-compatible Victron charge controller
- Server packages: `flask`, `gunicorn`, `cryptography`, `flask-limiter`, `requests`, `python-dateutil`
- Pi packages: `pyserial`, `requests`, `RPi.GPIO`

## Pi Installation (fresh setup)

Steps to bring up a brand-new Raspberry Pi from a clean Raspberry Pi OS install.

### 1. System packages

```bash
sudo apt update
sudo apt install -y python3-pip python3-rpi.gpio git
```

### 2. Python packages

```bash
pip3 install pyserial requests
```

(`RPi.GPIO` is installed via apt above — pip can be unreliable on Pi Zero.)

### 3. Create the log directory

The logger writes to `/var/log/vedirect/` and will not create the parent.

```bash
sudo mkdir -p /var/log/vedirect
sudo chown pi:pi /var/log/vedirect
```

### 4. Confirm the serial port

Plug the VE.Direct USB cable into the Pi, then check:

```bash
ls /dev/ttyUSB*
```

Should show `/dev/ttyUSB0`. If it shows a different device, edit the path in `vedirect_logger.py` (`main()` opens `/dev/ttyUSB0`).

Add the `pi` user to the `dialout` group so it can read serial without sudo, then reboot:

```bash
sudo usermod -aG dialout pi
sudo reboot
```

### 5. Clone the repo

```bash
cd /home/pi
git clone https://github.com/techdog21/deltapi.git
```

### 6. Create the systemd service

Create `/etc/systemd/system/vedirect_logger.service`:

```ini
[Unit]
Description=DeltaPI VE.Direct Logger
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/deltapi
Environment="POST_SECRET=your-secret-here"
Environment="BASE_URL=https://your-server.example.com"
ExecStart=/usr/bin/python3 /home/pi/deltapi/vedirect_logger.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Both `POST_SECRET` and `BASE_URL` are required — the script exits immediately if either is missing.

### 7. Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable vedirect_logger
sudo systemctl start vedirect_logger
sudo systemctl status vedirect_logger
```

### 8. Verify

```bash
tail -f /var/log/vedirect/vedirect_error.log
```

Within ~30 seconds you should see `[Serial] Connected`, `[Status] POST succeeded`, and `[Upload] Bulk POST succeeded`. The Pi posts an initial status immediately on startup, so it will appear on the dashboard right away.

### Common gotchas

- **No fan wired** — `setup_fan` logs a warning and the script continues fine.
- **`vcgencmd` missing** — only on Raspberry Pi OS, not Ubuntu. CPU temp/fan logic returns "unknown".
- **Wrong system time** — verify with `timedatectl`. Bad clock = bad timestamps in the database.
- **Controller in shop** — the script no longer hangs on missing serial; it runs in "no controller" mode and reports it on the dashboard.

## Running the Logger (Pi)

After installation, manage the service with:

```bash
sudo systemctl start vedirect_logger
sudo systemctl status vedirect_logger
```

Logs are stored in `/var/log/vedirect/`:
- `solar_log.jsonl` -- buffered VE.Direct frames
- `solar_archive.jsonl` -- 14-day rolling archive of uploaded data
- `vedirect_error.log` -- operational log (errors and status messages)
- `sent_offset.txt` -- upload byte-offset tracker

Install log rotation so `vedirect_error.log` doesn't grow unbounded:

```bash
sudo cp deploy/vedirect-logrotate.conf /etc/logrotate.d/vedirect
sudo chown root:root /etc/logrotate.d/vedirect
sudo chmod 644 /etc/logrotate.d/vedirect
```

Edit the `su` line in that file to match the user/group that owns `/var/log/vedirect`.

## Running the Server

For local development:
```bash
export POST_SECRET="your-secret"
export FERNET_KEY="your-fernet-key"
python app.py
```

For production on Render:
```bash
gunicorn app:app
```

## Routes

| Route | Method | Rate Limit | Description |
|-------|--------|------------|-------------|
| `/` | GET | -- | Single-page dashboard |
| `/log` | POST | 3/min | Accept a single solar data entry |
| `/log/bulk` | POST | 5/min | Accept multiple solar data entries (primary ingestion) |
| `/status` | POST | 2/min | Accept Pi system health stats |
| `/encrypt_days` | GET | 10/min | Generate encrypted token for date range selection |

## Deployment Notes

- Hosted on Render.com (Starter plan) with a 1 GB persistent Disk mounted at `/data`
- SQLite database lives at `/data/vedirect.db` (override with `DB_DIR`) and persists across restarts and redeploys
- If the database is ever empty, Pi bulk-uploads/backfill repopulate it within one upload cycle
- The server retains 30 days of solar logs and 7 days of Pi status, pruned daily
- Request bodies are capped at 1 MB (`MAX_CONTENT_LENGTH`); oversized bodies receive a `413`
- Environment variables must be configured in the Render dashboard and the Pi systemd service file

## Security Features

- **Startup validation**: Server warns on missing secrets; Pi refuses to start without `POST_SECRET` or `BASE_URL`
- **Token-based authentication**: All POST endpoints require a bearer token in the `Authorization` header, compared in constant time via `hmac.compare_digest`
- **HTTPS enforcement**: The server rejects all non-HTTPS POST requests
- **Rate limiting**: All ingestion and token endpoints are rate-limited via Flask-Limiter
- **Request size cap**: Bodies above 1 MB are rejected before parsing
- **Input validation**: Incoming JSON is checked for required fields before storage
- **HTML escaping**: All Pi-reported and VE.Direct fields are escaped before being rendered on the dashboard
- **Encrypted tokens**: Date range selection uses Fernet-encrypted tokens to prevent URL tampering
- **Thread-safe cleanup**: Database cleanup uses a threading lock to prevent race conditions under gunicorn
- **No hardcoded secrets**: All secrets are loaded from environment variables

## Fan Control (Pi)

The logger controls a PWM fan on GPIO 18 based on CPU temperature:
- Below 30C: fan off
- 30-50C: linear ramp from 20% to 100% duty cycle
- Above 50C: full speed
- If temperature cannot be read, the fan holds its current speed

## License

MIT License -- see [LICENSE](LICENSE) for details.

## Author

DeltaPI Project -- Jerry Craft
