# DeltaPI

![Server Dashboard](static/deltapi.png)
![More1](/static/deltapi2.png)
![More2](/static/deltapi3.png)

DeltaPI is a lightweight Python project for monitoring solar performance from a Victron VE.Direct compatible charge controller, using a Raspberry Pi Zero and a Flask-based dashboard.

The Raspberry Pi Zero is powered via a 12V-to-5V step-down converter and connected to the charge controller via USB. It transmits solar data every minute to a Flask server hosted on Render.com.

## Project Components

1. **`vedirect_logger.py`** – Python client running on the Pi Zero. Reads VE.Direct serial data and posts it to the server.
2. **`app.py`** – Flask web app that receives, stores, and displays solar metrics.
3. **Wi-Fi Connectivity** – The Pi connects over 2.4GHz Wi-Fi. Starlink dual-band works well for this.

## Requirements

* Python 3.8+
* Raspberry Pi with VE.Direct-compatible charge controller
* Packages: `pyserial`, `requests`, `flask`, `gunicorn`, `cryptography`, `flask-limiter`

## Running the Logger

Ensure the VE.Direct cable is plugged into `/dev/ttyUSB0` and run:

```bash
python vedirect_logger.py
```

The script logs locally and retries failed network sends. Logs are stored in `/var/log/vedirect/`.

## Running the Server

Install dependencies and run the app locally:

```bash
python app.py
```

Or for production (e.g., Render):

```bash
gunicorn app:app
```

The dashboard is served at `/`, and POST data is received at `/log`. CSV data export is available at `/export.csv`. Tokens are encrypted via `/encrypt_days`.

## Deployment Notes

* All data is stored in SQLite at `/var/data/vedirect/vedirect.db`
* Authentication is enforced via a bearer token for `/log`
* Rate-limiting and HTTPS checks help protect the endpoint

## Security Features

DeltaPI includes multiple safeguards to ensure secure and reliable data ingestion:

* **Token-based Authentication**: The `/log` endpoint requires a bearer token in the `Authorization` header. Unauthorized requests are rejected.
* **HTTPS Enforcement**: The server verifies that all data submissions occur over HTTPS to prevent man-in-the-middle attacks.
* **Rate Limiting**: Requests to `/log` are rate-limited to 3 per minute per IP to prevent flooding or abuse.
* **Input Validation**: Incoming JSON is checked for required fields (`V`, `I`, `PPV`, `VPV`, `timestamp`) before being stored.
* **Encrypted Tokens**: Data range selection uses encrypted tokens via the `/encrypt_days` endpoint, preventing client tampering.

These protections help ensure that only your trusted device can send valid data, and reduce the attack surface of the service.


