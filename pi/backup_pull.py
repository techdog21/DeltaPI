#!/usr/bin/env python3
"""
DeltaPI — off-box database backup puller.

Downloads a consistent SQLite snapshot from the server's authenticated /backup
endpoint once a day and keeps the most recent N copies locally, so the Pi is an
off-site backup of the Render database. The server DB is otherwise a single copy
on one Render disk: a lost/corrupted disk or a bad delete would be unrecoverable
beyond the ~7 days of solar frames the logger keeps. This closes that gap.

Runs as a long-lived systemd service (see pi/deploy/backup_pull.service),
matching the other DeltaPI services. On start it pulls immediately if there's no
recent backup (catch-up after a reboot/deploy), then pulls daily at BACKUP_HOUR.
A bad download is discarded (verified with PRAGMA quick_check) BEFORE any old,
known-good backup is rotated away.

Environment (from the shared EnvironmentFile, default /etc/deltapi.env):
    BASE_URL      required — e.g. https://deltapi.onrender.com
    POST_SECRET   required — bearer token, same one the logger uses
    BACKUP_DIR    optional — default /var/log/vedirect/backups (falls back to
                             ~/deltapi-backups if that isn't writable)
    BACKUP_KEEP   optional — number of copies to retain (default 14)
    BACKUP_HOUR   optional — local hour of day to pull (0-23, default 3)

Stdlib + requests only (requests is already present for the system python3 the
logger runs under).
"""
import glob
import gzip
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
POST_SECRET = os.environ.get("POST_SECRET")
BACKUP_DIR_PREF = os.environ.get("BACKUP_DIR", "/var/log/vedirect/backups")
KEEP_COPIES = int(os.environ.get("BACKUP_KEEP", "14"))
BACKUP_HOUR = int(os.environ.get("BACKUP_HOUR", "3"))

GLOB = "vedirect-backup-*.db.gz"
DOWNLOAD_TIMEOUT = 180          # seconds; the DB can be sizeable over a slow link
STARTUP_SKIP_AGE = 20 * 3600   # skip the startup pull if a backup is younger than this


def log(msg):
    print(f"[backup_pull] {msg}", flush=True)


def resolve_backup_dir():
    """Return the first writable backup directory, creating it if needed."""
    for d in (BACKUP_DIR_PREF, os.path.expanduser("~/deltapi-backups")):
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".write-test")
            with open(probe, "w"):
                pass
            os.remove(probe)
            return d
        except OSError:
            continue
    log("FATAL: no writable backup directory (tried "
        f"{BACKUP_DIR_PREF} and ~/deltapi-backups)")
    sys.exit(1)


BACKUP_DIR = None  # set in main()


def newest_backup_age():
    """Age in seconds of the most recent backup, or None if there are none."""
    files = glob.glob(os.path.join(BACKUP_DIR, GLOB))
    if not files:
        return None
    return time.time() - max(os.path.getmtime(f) for f in files)


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def rotate():
    """Keep only the newest KEEP_COPIES backups."""
    if KEEP_COPIES <= 0:
        return
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, GLOB)))
    for old in files[:-KEEP_COPIES]:
        try:
            os.remove(old)
            log(f"pruned old backup {old}")
        except OSError as e:
            log(f"could not prune {old}: {e}")


def is_valid_sqlite(path):
    """True if `path` is a non-corrupt SQLite database (guards against saving an
    error page or a truncated download as if it were a backup)."""
    try:
        conn = sqlite3.connect(path)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
        finally:
            conn.close()
    except Exception as e:
        log(f"integrity open failed: {e}")
        return False
    return bool(row) and row[0] == "ok"


def pull_once():
    """Download one snapshot, verify it, save it, and rotate. Returns True on
    success. Never raises — failures are logged and the daily loop continues."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = os.path.join(BACKUP_DIR, f"vedirect-backup-{ts}.db.gz")
    tmp = dest + ".part"
    headers = {"Authorization": f"Bearer {POST_SECRET}"}
    try:
        with requests.get(BASE_URL + "/backup", headers=headers,
                          stream=True, timeout=DOWNLOAD_TIMEOUT) as r:
            r.raise_for_status()
            total = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        total += len(chunk)
    except Exception as e:
        _rm(tmp)
        log(f"pull failed: {e}")
        return False

    # Verify: decompress to a temp .db and integrity-check it before trusting
    # the download (guards against a truncated stream or an error page).
    check = os.path.join(BACKUP_DIR, f".check-{ts}.db")
    valid = False
    try:
        with gzip.open(tmp, "rb") as gz, open(check, "wb") as out:
            shutil.copyfileobj(gz, out, 1024 * 1024)
        valid = is_valid_sqlite(check)
    except Exception as e:
        log(f"decompress/verify failed: {e}")
    finally:
        _rm(check)

    if not valid:
        _rm(tmp)
        log(f"integrity check failed — discarding {total} bytes, keeping prior backups")
        return False

    os.replace(tmp, dest)
    log(f"saved {dest} ({total} bytes compressed)")
    rotate()
    return True


def seconds_until_next_run():
    """Seconds until the next BACKUP_HOUR:00 in the Pi's local time."""
    now = datetime.now().astimezone()
    target = now.replace(hour=BACKUP_HOUR, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def main():
    global BACKUP_DIR
    if not BASE_URL or not POST_SECRET:
        log("FATAL: BASE_URL and POST_SECRET must be set (see /etc/deltapi.env)")
        sys.exit(1)
    BACKUP_DIR = resolve_backup_dir()
    log(f"up — dir={BACKUP_DIR} keep={KEEP_COPIES} daily@{BACKUP_HOUR:02d}:00 local")

    # Catch-up: pull now if there's no recent backup (fresh Pi, reboot, or deploy).
    age = newest_backup_age()
    if age is None or age > STARTUP_SKIP_AGE:
        log("no recent backup — pulling at startup")
        pull_once()
    else:
        log(f"recent backup exists ({int(age / 3600)}h old) — skipping startup pull")

    while True:
        sleep_s = seconds_until_next_run()
        log(f"next pull in {int(sleep_s // 3600)}h{int((sleep_s % 3600) // 60)}m")
        time.sleep(sleep_s)
        pull_once()


if __name__ == "__main__":
    main()
