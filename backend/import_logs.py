"""
Import fishtoy_log*.jsonl files into the dashboard's SQLite database.
Run from the dashboard backend folder.

Usage:
    python import_logs.py path\\to\\fishtoy_log.jsonl
    python import_logs.py path\\to\\fishtoy_log_2026-03-29_213251.jsonl
    python import_logs.py "path\\to\\fishtoy_log*.jsonl"
"""
import sys, json, glob
from pathlib import Path

# Import the dashboard's database module
import database

database.init_db()

if len(sys.argv) < 2:
    print("Usage: python import_logs.py <path_to_jsonl_file(s)>")
    print("Example: python import_logs.py C:\\fishtank-logger\\fishtoy_log*.jsonl")
    sys.exit(1)

# Expand globs on Windows (PowerShell doesn't always expand them)
files = []
for arg in sys.argv[1:]:
    matches = glob.glob(arg)
    if matches:
        files.extend(matches)
    else:
        files.append(arg)

total_imported = 0
total_skipped = 0

for filepath in files:
    path = Path(filepath)
    if not path.exists():
        print(f"[SKIP] {filepath} not found")
        continue

    count = 0
    skipped = 0
    with open(path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                event_type = entry.get("event", "fishtoy:used")
                data = entry.get("data", {})

                if not isinstance(data, dict):
                    skipped += 1
                    continue

                # Check for duplicate by event_id
                event_id = data.get("id")
                if event_id:
                    existing = database.get_events(event_type=event_type, limit=1)
                    # Quick dedup: check if this specific event_id already exists
                    conn = database._get_conn()
                    row = conn.execute(
                        "SELECT id FROM events WHERE event_id = ? AND event_type = ?",
                        (str(event_id), event_type)
                    ).fetchone()
                    if row:
                        skipped += 1
                        continue

                database.store_event(event_type, data)
                count += 1

            except json.JSONDecodeError:
                print(f"  [WARN] {path.name} line {line_num}: invalid JSON, skipping")
                skipped += 1

    print(f"[OK] {path.name}: imported {count}, skipped {skipped} (duplicates/invalid)")
    total_imported += count
    total_skipped += skipped

print(f"\nDone. {total_imported} events imported, {total_skipped} skipped.")
