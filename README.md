# DeltaPI Solar Monitor

![Server Dashboard](static/deltapi.png)

DeltaPI is a lightweight Python project for monitoring solar performance from a Victron VE.Direct compatible charge controller, using a Raspberry Pi Zero and a Flask-based dashboard hosted on Render.com.

The Raspberry Pi Zero is powered via a 12V-to-5V step-down converter and connected to the charge controller via USB. It buffers solar data locally and bulk-uploads to the server every 2 minutes.

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

- **Pi to Server**: Bulk upload every 2 minutes via `/log/bulk`
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

Both variables are validated at startup. The server logs warnings if either is missing; the Pi exits immediately if `POST_SECRET` is not set.

## Requirements

- Python 3.9+
- Raspberry Pi with VE.Direct-compatible Victron charge controller
- Server packages: `flask`, `gunicorn`, `cryptography`, `flask-limiter`, `requests`, `python-dateutil`
- Pi packages: `pyserial`, `requests`, `RPi.GPIO`

## Running the Logger (Pi)

The logger runs as a systemd service at `/etc/systemd/system/vedirect_logger.service`. The `POST_SECRET` environment variable must be set in the service file.

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
- SQLite database lives at `/data/vedirect.db`; resets on service restart unless a Render Disk is attached
- Pi bulk-uploads repopulate the database within one upload cycle after restart
- The server stores up to 60 days of data; records older than 30 days are cleaned up daily
- Environment variables must be configured in the Render dashboard and the Pi systemd service file

## Security Features

- **Startup validation**: Server warns on missing secrets; Pi refuses to start without `POST_SECRET`
- **Token-based authentication**: All POST endpoints require a bearer token in the `Authorization` header
- **HTTPS enforcement**: The server rejects all non-HTTPS POST requests
- **Rate limiting**: All ingestion and token endpoints are rate-limited via Flask-Limiter
- **Input validation**: Incoming JSON is checked for required fields before storage
- **Encrypted tokens**: Date range selection uses Fernet-encrypted tokens to prevent URL tampering
- **Thread-safe cleanup**: Database cleanup uses a threading lock to prevent race conditions under gunicorn
- **No hardcoded secrets**: All secrets are loaded from environment variables

## Fan Control (Pi)

The logger controls a PWM fan on GPIO 18 based on CPU temperature:
- Below 30C: fan off
- 30-50C: linear ramp from 20% to 100% duty cycle
- Above 50C: full speed
- If temperature cannot be read, the fan holds its current speed

## Author

DeltaPI Project -- Jerry Craft
