from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import sqlite3
import json

app = Flask(__name__)
DB_PATH = "vedirect.db"

CS_MAP = {
    "0": "Off",
    "1": "Low Power",
    "2": "Fault",
    "3": "Bulk",
    "4": "Absorption",
    "5": "Float"
}

# Ensure database and table
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
                ppv = int(data.get("PPV", 0))
                vpv = int(data.get("VPV", 0)) / 1000
                load = data.get("LOAD", "N/A")
                cs = CS_MAP.get(data.get("CS", "0"), f"Unknown ({data.get('CS')})")
                err = data.get("ERR", "0")
                h20 = int(data.get("H20", 0)) / 100
                parsed.append((ts, v, i, ppv, vpv, load, cs, err, h20))
        except:
            continue

    html = """
    <html>
    <head>
        <title>VE.Direct Dashboard - 24 Hours</title>
        <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
        <script src=\"https://cdn.jsdelivr.net/npm/chart.js\"></script>
        <style>
            body { font-family: sans-serif; margin: 1em; }
            #chart-container { width: 100%; max-width: 900px; margin: auto; height: 400px; }
            canvas { width: 100% !important; height: 100% !important; }
            .table-container { overflow-x: auto; margin-top: 2em; }
            table { width: 100%; border-collapse: collapse; font-size: 0.9em; }
            th, td { border: 1px solid #ccc; padding: 6px; text-align: left; }
            th { background-color: #eee; }
        </style>
    </head>
    <body>
        <h2>VE.Direct Solar Data (Last 24 Hours)</h2>
        <div id=\"chart-container\">
            <canvas id=\"chart\"></canvas>
        </div>
        <div class=\"table-container\">
            <table>
                <tr>
                    <th>Time</th><th>Battery Voltage (V)</th><th>Battery Current (A)</th><th>Solar Power (W)</th><th>Panel Voltage (V)</th>
                    <th>Load Output</th><th>Charge Mode</th><th>Error Code</th><th>Energy Today (kWh)</th>
                </tr>
    """

    timestamps = []
    voltages = []
    currents = []
    powers = []

    for row in parsed:
        ts, v, i, ppv, vpv, load, cs, err, h20 = row
        html += f"<tr><td>{ts}</td><td>{v:.2f}</td><td>{i:.2f}</td><td>{ppv}</td><td>{vpv:.2f}</td><td>{load}</td><td>{cs}</td><td>{err}</td><td>{h20:.2f}</td></tr>"
        timestamps.append(ts)
        voltages.append(v)
        currents.append(i)
        powers.append(ppv)

    html += """
            </table>
        </div>
    """

    if len(timestamps) >= 2:
    html += f"""
    <script>
    document.addEventListener("DOMContentLoaded", function () {{
        const ctx = document.getElementById('chart').getContext('2d');
        const chart = new Chart(ctx, {{
            type: 'line',
            data: {{
                labels: {json.dumps(timestamps)},
                datasets: [
                    {{
                        label: 'Battery Voltage (V)',
                        data: {json.dumps(voltages)},
                        borderColor: 'blue',
                        fill: false,
                        tension: 0.1
                    }},
                    {{
                        label: 'Battery Current (A)',
                        data: {json.dumps(currents)},
                        borderColor: 'green',
                        fill: false,
                        tension: 0.1
                    }},
                    {{
                        label: 'Solar Power (W)',
                        data: {json.dumps(powers)},
                        borderColor: 'orange',
                        fill: false,
                        tension: 0.1
                    }}
                ]
            }},
            options: {{
                responsive: true,
                plugins: {{
                    legend: {{ position: 'top' }}
                }},
                scales: {{
                    x: {{ display: true, title: {{ display: true, text: 'Timestamp' }} }},
                    y: {{ beginAtZero: true, title: {{ display: true, text: 'Value' }} }}
                }}
            }}
        }});
    }});
    </script>
    """

        """
    else:
        html += "<p><strong>Waiting for more data to display the graph...</strong></p>"

    html += "</body></html>"
    return html

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

@app.route("/latest")
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

if __name__ == "__main__":
    app.run()
