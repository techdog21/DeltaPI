import serial
import json
from datetime import datetime
import time
import requests
import os

LOG_PATH = "/var/log/vedirect/solar_log.jsonl"
ERROR_LOG = "/var/log/vedirect/vedirect_error.log"
POST_URL = "https://deltapi-k3bf.onrender.com/log"

def read_frame(ser):
    frame = {}
    while True:
        line = ser.readline().decode('utf-8', errors='ignore').strip()
        if not line:
            continue
        if line.startswith("Checksum"):
            return frame
        parts = line.split(None, 1)
        if len(parts) == 2:
            key, value = parts
            frame[key] = value

def log_error(message):
    with open(ERROR_LOG, "a") as errlog:
        errlog.write(f"[{datetime.utcnow().isoformat()}] {message}\n")

def main():
    ser = None
    while ser is None:
        try:
            ser = serial.Serial('/dev/ttyUSB0', 19200, timeout=5)
        except Exception as e:
            log_error(f"Serial open failed: {e}")
            time.sleep(30)  # wait before retrying
    while True:
        try:
            frame = read_frame(ser)
            if frame:
                frame["timestamp"] = datetime.utcnow().isoformat()

                # Optional: log locally
                try:
                    with open(LOG_PATH, "a") as f:
                        json.dump(frame, f)
                        f.write("\n")
                except Exception as e:
                    log_error(f"Local log failed: {e}")

                # Send to remote server
                try:
                    requests.post(POST_URL, json=frame, timeout=5)
                except Exception as e:
                    log_error(f"POST failed: {e}")
        except Exception as e:
            log_error(f"Read loop error: {e}")

        time.sleep(60)

if __name__ == "__main__":
    main()
