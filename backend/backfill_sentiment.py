"""
One-time backfill script: add VADER sentiment scores to existing events.

Reads all chat:message, tts:update, and sfx:update events that don't
already have a sentiment field, scores them with VADER, and updates
the data column in-place.

Usage:
    cd backend
    python backfill_sentiment.py
"""

import json
import database
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

database.init_db()
analyzer = SentimentIntensityAnalyzer()
conn = database._get_conn()

print("=" * 60)
print("  Sentiment Backfill")
print("=" * 60)
print()

# Find events without sentiment scores
rows = conn.execute("""
    SELECT id, event_type, data FROM events
    WHERE event_type IN ('chat:message', 'tts:update')
      AND json_extract(data, '$.sentiment') IS NULL
    ORDER BY id
""").fetchall()

print(f"Found {len(rows)} events without sentiment scores")

if not rows:
    print("Nothing to backfill.")
    exit()

updated = 0
batch_size = 1000

for i, row in enumerate(rows):
    try:
        data = json.loads(row["data"])
        message = data.get("message", "")
        if message and isinstance(message, str):
            score = analyzer.polarity_scores(message)["compound"]
        else:
            score = 0.0
        data["sentiment"] = score
        conn.execute(
            "UPDATE events SET data = ? WHERE id = ?",
            (json.dumps(data, ensure_ascii=False), row["id"]),
        )
        updated += 1
    except Exception as e:
        print(f"  [WARN] Skipped event {row['id']}: {e}")

    # Commit in batches
    if (i + 1) % batch_size == 0:
        conn.commit()
        print(f"  Processed {i + 1}/{len(rows)} ({updated} updated)")

conn.commit()

print()
print(f"Done. Updated {updated}/{len(rows)} events with sentiment scores.")
