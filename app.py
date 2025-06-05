from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import sqlite3
import os
import json

app = Flask(__name__)
DB_PATH = "vedirect.db"

# Create table if it doesn't exist
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                received TEXT,
                data TEXT
            )
        """)

init_db()

# POST endpoint for VE.Direct logger
@app.route("/log", methods=["POST"])
def log():
    entry = request.get_json()
    if not entry:
        return jsonify({"error": "No data received"}), 400

    timestamp = entry.get("timestamp", datetime.utcnow().isoformat())
    received = datetime.utcnow().isoformat()
    data_str = json.dumps(entry)

    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO logs (timestamp, received, data) VALUES (?, ?, ?)",
                (timestamp, received, data_str)
            )
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Latest single reading
@app.route("/latest", methods=["GET"])
def latest():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT timestamp, received, data FROM logs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row:
            return jsonify({
                "timestamp": row[0],
                "received": row[1],
                "data": json.loads(row[2])
            })
        return jsonify({"error": "No data"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Homepage with 24h graph and table
@app.route("/")
def index():
    try:
        now = datetime.utcnow()
        since = now - timedelta(hours=24)

        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT timestamp, received, data FROM logs ORDER BY id ASC"
            ).fetchall()
    except Exception as e:
        return f"<p>Error reading database: {e}</p>"

    parsed = []
    for row in rows:
        try:
            data = json.loads(row[2])
            ts = data.get("timestamp", row[0])
            ts_dt = datetime.fromisoformat(ts)
            if ts_dt >= since:
                v = int(data.get("V", 0)) / 1000
                i = int(data.get("I", 0)) / 1000
                parsed.append((ts, v, i))
        except:
            continue

    timestamps = [x[0] for x in parsed]
    voltages = [x[1] for x in parsed]
    currents = [x[2] for x in parsed]

    html = """
    <html>
    <head>
        <title>VE.Direct Dashboard - 24 Hours</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            body {
                font-family: sans-serif;
                margin: 1em;
            }
            #chart-container {
                width: 100%;
                max-width: 900px;
                margin: auto;
            }
            canvas {
                width: 100% !important;
                height: 400px !important;
            }
            .table-container {
                overflow-x: auto;
                margin-top: 2em;
            }
            table {
                width: 100%;
                border-collapse: collapse;
                font-size: 0.95em;
            }
            th, td {
                border: 1px solid #ccc;
                padding: 8px;
                text-align: left;
            }
            th {
                background-color: #eee;
            }
        </style>
    </head>
    <body>
        <h2>VE.Direct Solar Data (Last 24 Hours)</h2>

        <div id="chart-container">
            <canvas id="chart"></canvas>
        </div>

        <div class="table-container">
            <table>
                <tr><th>Timestamp</th><th>Voltage (V)</th><th>Current (A)</th></tr>
    """

    for ts, v, i in parsed:
        html += f"<tr><td>{ts}</td><td>{v:.2f}</td><td>{i:.2f}</td></tr>"

    html += """
            </table>
        </div>

        <script>
            const ctx = document.getElementById('chart').getContext('2d');
            const chart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: """ + json.dumps(timestamps) + """,
                    datasets: [
                        {
                            label: 'Voltage (V)',
                            data: """ + json.dumps(voltages) + """,
                            borderColor: 'blue',
                            borderWidth: 2,
                            fill: false
                        },
                        {
                            label: 'Current (A)',
                            data: """ + json.dumps(currents) + """,
                            borderColor: 'green',
                            borderWidth: 2,
                            fill: false
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { position: 'top' }
                    },
                    scales: {
                        y: { beginAtZero: true }
                    }
                }
            });
        </script>
    </body>
    </html>
    """

    return html

# Debug route to inspect last 5 entries
@app.route("/debug")
def debug():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT id, timestamp, received, data FROM logs ORDER BY id DESC LIMIT 5"
            ).fetchall()
    except Exception as e:
        return f"<p>Error reading database: {e}</p>"

    html = "<h2>Debug: Last 5 Entries</h2><pre>\n"
    for row in rows:
        db_ts = row[1]
        try:
            data = json.loads(row[3])
            json_ts = data.get("timestamp", "N/A")
            v = int(data.get("V", 0)) / 1000
            i = int(data.get("I", 0)) / 1000
        except:
            json_ts = "Parse error"
            v = i = "?"
        html += f"ID: {row[0]}\nDB timestamp: {db_ts}\nJSON timestamp: {json_ts}\nV: {v} V, I: {i} A\n---\n"

    html += "</pre>"
    return html

if __name__ == "__main__":
    app.run()
