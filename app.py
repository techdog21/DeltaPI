from flask import Flask, request, jsonify
from datetime import datetime
import sqlite3
import os
import json

app = Flask(__name__)
DB_PATH = "vedirect.db"

# Create database and table if they don't exist
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
                "data": json.loads(row[2])  # ← convert from string to object
            })
        return jsonify({"error": "No data"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500
@app.route("/")
def index():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT timestamp, received, data FROM logs ORDER BY id DESC LIMIT 10"
            ).fetchall()
    except Exception as e:
        return f"<p>Error reading database: {e}</p>"

    # Parse and reverse rows (oldest first)
    parsed = []
    for row in reversed(rows):
        try:
            data = json.loads(row[2])
            v = int(data.get("V", 0)) / 1000  # mV → V
            i = int(data.get("I", 0)) / 1000  # mA → A
            ts = data.get("timestamp", row[0])
            parsed.append((ts, v, i))
        except:
            continue

    # Prepare chart data
    timestamps = [x[0] for x in parsed]
    voltages = [x[1] for x in parsed]
    currents = [x[2] for x in parsed]

    # Begin HTML
    html = """
    <html><head><title>VE.Direct Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { font-family: sans-serif; margin: 2em; }
        table { border-collapse: collapse; margin-top: 2em; }
        th, td { border: 1px solid #ccc; padding: 6px 12px; }
        th { background-color: #eee; }
    </style>
    </head><body>
    <h1>VE.Direct Solar Data</h1>

    <!-- Chart at top -->
    <canvas id="chart" width="800" height="300"></canvas>

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
                    fill: false
                },
                {
                    label: 'Current (A)',
                    data: """ + json.dumps(currents) + """,
                    borderColor: 'green',
                    fill: false
                }
            ]
        },
        options: {
            responsive: true,
            plugins: { legend: { position: 'top' }},
            scales: { y: { beginAtZero: true }}
        }
    });
    </script>
    """

    # Table of values
    html += """
    <h2>Last 10 Readings</h2>
    <table>
    <tr><th>Timestamp</th><th>Voltage (V)</th><th>Current (A)</th></tr>
    """
    for ts, v, i in parsed:
        html += f"<tr><td>{ts}</td><td>{v:.2f}</td><td>{i:.2f}</td></tr>"
    html += "</table></body></html>"

    return html

if __name__ == "__main__":
    app.run()
