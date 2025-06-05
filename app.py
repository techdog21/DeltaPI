from flask import Flask, request, jsonify
from datetime import datetime
import sqlite3
import os

app = Flask(__name__)
DB_PATH = "vedirect.db"

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

@app.route("/log", methods=["POST"])
def log():
    entry = request.get_json()
    if not entry:
        return jsonify({"error": "No data"}), 400

    timestamp = entry.get("timestamp", datetime.utcnow().isoformat())
    received = datetime.utcnow().isoformat()
    data_str = str(entry)

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO logs (timestamp, received, data) VALUES (?, ?, ?)",
            (timestamp, received, data_str)
        )

    return jsonify({"status": "ok"}), 200

@app.route("/latest")
def latest():
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT timestamp, received, data FROM logs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row:
        return jsonify({
            "timestamp": row[0],
            "received": row[1],
            "data": row[2]
        })
    return jsonify({"error": "No data"}), 404

@app.route("/")
def index():
    return "<h1>VE.Direct Logger</h1><p>POST to /log</p>"

if __name__ == "__main__":
    init_db()
    app.run()
