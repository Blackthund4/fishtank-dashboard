"""Backfill keyword_counts from historical chat:message events.

Populates hourly keyword buckets for messages that predate the
keyword_agg_thread's first-run 24h cutoff.

Usage (on VPS, in the ingestion container):
    docker compose exec ingestion python backfill_keywords.py
    docker compose exec ingestion python backfill_keywords.py --dry-run
"""

import argparse
import time
from collections import Counter, defaultdict

import database
from tokenizer import tokenize

BATCH_SIZE = 50_000


def main():
    parser = argparse.ArgumentParser(description="Backfill keyword_counts from historical chat messages")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be processed without writing")
    args = parser.parse_args()

    print("WARNING: Running this twice will double-count historical keywords. Only run once.")
    if args.dry_run:
        print("DRY RUN: no data will be written")
    print()

    database.init_db()
    conn = database._get_conn()

    # Find the earliest existing bucket
    row = conn.execute("SELECT MIN(bucket) FROM keyword_counts").fetchone()
    if row and row[0]:
        cutoff = row[0]
    else:
        from datetime import datetime, timezone
        cutoff = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")

    # The cutoff bucket looks like "2026-04-11T14". Messages with
    # timestamp_local starting before that hour are what we need.
    cutoff_ts = cutoff + ":00:00+00:00"
    print(f"Backfilling keyword_counts for messages before {cutoff}")

    # Count total messages to process
    total_row = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'chat:message' "
        "AND message_text IS NOT NULL AND timestamp_local < ?",
        (cutoff_ts,)
    ).fetchone()
    total_to_process = total_row[0] if total_row else 0
    print(f"Messages to process: {total_to_process:,}")
    if total_to_process == 0:
        print("Nothing to backfill.")
        return

    t0 = time.time()
    last_id = 0
    batch_num = 0
    total_processed = 0

    while True:
        rows = conn.execute(
            "SELECT id, message_text, timestamp_local FROM events "
            "WHERE event_type = 'chat:message' "
            "AND message_text IS NOT NULL "
            "AND timestamp_local < ? "
            "AND id > ? "
            "ORDER BY id LIMIT ?",
            (cutoff_ts, last_id, BATCH_SIZE)
        ).fetchall()

        if not rows:
            break

        batch_num += 1
        bucket_counts = defaultdict(Counter)
        for row in rows:
            words = tokenize(row["message_text"])
            if words:
                bucket = row["timestamp_local"][:13]
                bucket_counts[bucket].update(words)

        upsert_rows = []
        for bucket, counter in bucket_counts.items():
            for word, count in counter.items():
                upsert_rows.append((bucket, word, count))

        if upsert_rows and not args.dry_run:
            database.upsert_keyword_counts(upsert_rows)

        last_id = rows[-1]["id"]
        total_processed += len(rows)
        print(f"Batch {batch_num}: processed {len(rows):,} messages, "
              f"{len(upsert_rows):,} word-bucket pairs"
              f"{' (dry run)' if args.dry_run else ''}")

    elapsed = time.time() - t0
    print(f"\nDone. Processed {total_processed:,} messages in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
