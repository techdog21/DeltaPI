# DeltaPI

DeltaPI is a small Python project for collecting data from a Victron VE.Direct compatible device and presenting the results in a simple web dashboard.

The project contains two main components:

1. **`vedirect_logger.py`** – reads serial frames from the VE.Direct port and sends them to the web service.
2. **`app.py`** – a Flask application that stores the data in SQLite and serves the dashboard.

## Requirements

* Python 3.8+
* The `pyserial` and `requests` packages for the logger.
* `flask` and `gunicorn` for the web API (listed in `requirements.txt`).

## Running the Logger

The logger should be run on a machine connected to the VE.Direct serial port (e.g. a Raspberry Pi). It expects the device to appear as `/dev/ttyUSB0` and posts each frame to the web service at `https://deltapi-k3bf.onrender.com/log`.

```bash
python vedirect_logger.py
```

Log files are written to `/var/log/vedirect/` by default. The script will keep trying to read from the serial port and will retry posting if a network error occurs.

## Running the Web Service

On the server side, install dependencies and launch the Flask application. During development you can run it directly:

```bash
python app.py
```

For production use (e.g. on Render) start it with Gunicorn:

```bash
gunicorn app:app
```

Data is stored in an SQLite database at `/var/data/vedirect/vedirect.db`. The dashboard is available at the root URL (`/`), while `/log` accepts POST requests from the logger.
You can also open `/debug` to view the last few log entries for troubleshooting.

## Deployment

A simple example of a Render.com service configuration is provided in `render.yaml`. It installs requirements from `requirements.txt` and launches the app using Gunicorn.

## Next Steps

* Customize file paths or URLs by editing constants in the scripts.
* Secure the `/log` endpoint if exposing the service on the public internet.
* Expand the dashboard with additional metrics or historical views.

