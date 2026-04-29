# DeltaPI Solar Monitor

![Server Dashboard](static/deltapi.png)

DeltaPI is a lightweight Python project for monitoring solar performance from a Victron VE.Direct compatible charge controller, using a Raspberry Pi Zero and a Flask-based dashboard hosted on Render.com.

The Raspberry Pi Zero is powered via a 12V-to-5V step-down converter and connected to the charge controller via USB. It buffers solar data locally and bulk-uploads to the server roughly every 30 seconds (see `UPLOAD_INTERVAL` in `vedirect_logger.py`).

## Project Components

1. **`vedirect_logger.py`** -- Python client running on the Pi Zero as a systemd service. Reads VE.Direct serial data, logs locally to JSONL, bulk-uploads to the server, monitors Pi system health, and controls a PWM cooling fan.
2. **`app.py`** -- Flask web app that receives, stores, and displays solar metrics on a single-page dashboard with light/dark theme support.
3. **Wi-Fi Connectivity** -- The Pi connects over 2.4GHz Wi-Fi. Starlink dual-band works well for this.

## Dashboard Features

- Real-time solar system summary: voltage, SOC, load, runtime estimates
- Raspberry Pi health: CPU temp, fan speed, Wi-Fi signal, disk/memory, OS updates
- Four Chart.js visualizations: Solar Power, Battery Voltage, Daily Energy (H20), Daily Peak Power (H21)
- Latest readings table
- Light/dark theme toggle with cookie persistence
- Responsive layout for desktop and mobile (iPhone compatible)

## Architecture

- **Pi to Server**: Bulk upload via `/log/bulk` every `UPLOAD_INTERVAL` seconds (default 30s)
- **Local buffering**: Pi logs all frames to JSONL with byte-offset tracking; unsent data survives network outages
- **14-day local archive**: Sent data is archived on the Pi's SD card before pruning
- **Backfill on redeploy**: If the server database is empty after a restart, the Pi replays its full archive automatically
- **Ephemeral server DB**: SQLite on Render free tier resets on restart; Pi repopulates via backfill. Use a Render Disk for persistent storage.
- **Deduplication**: Server skips entries with timestamps already in the database
- **Thread safety**: Cleanup operations are guarded by a threading lock for safe use under gunicorn

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

Within ~30 seconds you should see `[Serial] Connected`, `[Status] POST succeeded`, and `[Log] Frame written locally`. The Pi posts an initial status immediately on startup, so it will appear on the dashboard right away.

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

- Hosted on Render.com free tier with ephemeral filesystem
- SQLite database lives at `/data/vedirect.db` (override with `DB_DIR`); resets on service restart unless a Render Disk is attached
- Pi bulk-uploads repopulate the database within one upload cycle after restart
- The server retains 30 days of solar logs and 7 days of Pi status, pruned daily
- Request bodies are capped at 1 MB (`MAX_CONTENT_LENGTH`)
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
