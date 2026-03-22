# DeltaPI Solar Monitor

![Server Dashboard](static/deltapi.png)

DeltaPI is a lightweight Python project for monitoring solar performance from a Victron VE.Direct compatible charge controller, using a Raspberry Pi Zero and a Flask-based dashboard hosted on Render.com.

The Raspberry Pi Zero is powered via a 12V-to-5V step-down converter and connected to the charge controller via USB. It buffers solar data locally and bulk-uploads to the server every 10 minutes (trip mode) or 30 minutes (dormant/stored mode).

## Project Components

1. **`vedirect_logger.py`** – Python client running on the Pi Zero as a systemd service. Reads VE.Direct serial data, logs locally to JSONL, bulk-uploads to the server, monitors Pi system health, and controls a PWM cooling fan.
2. **`app.py`** – Flask web app that receives, stores, and displays solar metrics on a single-page dashboard with light/dark theme support.
3. **Wi-Fi Connectivity** – The Pi connects over 2.4GHz Wi-Fi. Starlink dual-band works well for this.

## Dashboard Features

- Real-time solar system summary: voltage, SOC, load, runtime estimates
- Raspberry Pi health: CPU temp, fan speed, Wi-Fi signal, disk/memory, OS updates
- Four Chart.js visualizations: Solar Power, Battery Voltage, Daily Energy (H20), Daily Peak Power (H21)
- Latest readings table
- Light/dark theme toggle with cookie persistence
- Responsive layout for desktop and mobile (iPhone compatible)

## Architecture

- **Pi → Server**: Bulk upload every 10 min (trip mode) or 30 min (dormant mode), controlled by a `trip_mode` file toggle
- **Local buffering**: Pi logs all frames to JSONL with offset tracking; unsent data survives network outages
- **14-day local archive**: Sent data is archived on the Pi's SD card before pruning
- **Ephemeral server DB**: SQLite on Render free tier resets on restart; Pi repopulates on next upload cycle. Use Disk if you want to keep longer records.
- **Deduplication**: Server skips entries with timestamps already in the database

## Requirements

- Python 3.8+
- Raspberry Pi with VE.Direct-compatible Victron charge controller
- Server packages: `flask`, `gunicorn`, `cryptography`, `flask-limiter`, `python-dateutil`
- Pi packages: `pyserial`, `requests`, `RPi.GPIO`

## Running the Logger (Pi)

The logger runs as a systemd service at `/etc/systemd/system/vedirect_logger.service`. The `POST_SECRET` environment variable is set in the service file.

```bash
sudo systemctl start vedirect_logger
sudo systemctl status vedirect_logger
```

To enable trip mode (10-minute uploads, keeps server awake):
```bash
touch /home/jerry/trip_mode
```

To switch to dormant mode (30-minute uploads, server sleeps between):
```bash
rm /home/jerry/trip_mode
```

Logs are stored in `/var/log/vedirect/`:
- `solar_log.jsonl` – buffered VE.Direct frames
- `solar_archive.jsonl` – 14-day rolling archive of uploaded data
- `vedirect_error.log` – operational log
- `sent_offset.txt` – upload offset tracker

## Running the Server

For local development:
```bash
python app.py
```

For production on Render:
```bash
gunicorn app:app
```

## Routes

| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Single-page dashboard |
| `/log` | POST | Accept a single solar data entry |
| `/log/bulk` | POST | Accept multiple solar data entries (primary ingestion) |
| `/status` | POST | Accept Pi system health stats |
| `/encrypt_days` | GET | Generate encrypted token for date range selection |

## Deployment Notes

- Hosted on Render.com free tier with ephemeral filesystem
- SQLite database lives in the project root directory; resets on any service restart
- Pi bulk-uploads repopulate the database within one upload cycle after restart
- Environment variables required on Render: `POST_SECRET`, `FERNET_KEY`
- Environment variable required on Pi (in systemd service file): `POST_SECRET`

## Security Features

- **Token-based Authentication**: All POST endpoints require a bearer token in the `Authorization` header
- **HTTPS Enforcement**: The server rejects all non-HTTPS requests
- **Rate Limiting**: `/log` at 3/min, `/log/bulk` at 2/min, `/status` at 2/min, `/encrypt_days` at 10/min
- **Input Validation**: Incoming JSON is checked for required fields before storage
- **Encrypted Tokens**: Date range selection uses Fernet-encrypted tokens to prevent URL tampering
- **No hardcoded secrets**: `POST_SECRET` is loaded from environment variables on both Pi and server

## Author

DeltaPI Project – Jerry Craft