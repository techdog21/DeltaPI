import sqlite3
import json

DB_PATH = "/var/data/vedirect/vedirect.db"

def is_valid(entry):
    try:
        # Must have valid integers for V, I, PPV, VPV, and H20
        int(str(entry.get("V", "0")).strip().replace("\x00", ""))
        int(str(entry.get("I", "0")).strip().replace("\x00", ""))
        int(str(entry.get("PPV", "0")).strip().replace("\x00", ""))
        int(str(entry.get("VPV", "0")).strip().replace("\x00", ""))
        int(str(entry.get("H20", "0")).strip().replace("\x00", ""))
        return True
    except:
        return False

def clean_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, data FROM logs")
        all_rows = cursor.fetchall()

        for row_id, data_str in all_rows:
            try:
                entry = json.loads(data_str)
                if not is_valid(entry):
                    print(f"Deleting corrupt row: {row_id}")
                    cursor.execute("DELETE FROM logs WHERE id = ?", (row_id,))
            except:
                print(f"Skipping unreadable row: {row_id}")
                cursor.execute("DELETE FROM logs WHERE id = ?", (row_id,))

        conn.commit()

if __name__ == "__main__":
    clean_db()
