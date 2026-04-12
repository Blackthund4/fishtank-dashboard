"""
One-time frequency audit: show top 100 raw words in chat messages.

Tokenizes chat messages with NO stopword filtering (alpha 3+ chars only)
to reveal which words actually dominate fishtank chat. Use the output to
calibrate the stopword list in tokenizer.py before deploying keyword analysis.

Processes in chunks to stay memory-safe on large datasets.
Supports an optional --since flag to limit the time window.

Usage:
    cd backend
    python audit_keywords.py                    # all chat (30-day retention window)
    python audit_keywords.py --since 7          # last 7 days only
    python audit_keywords.py --since 1 --top 50 # last 24h, top 50

Delete this script after the stopword list is finalized.
"""

import argparse
import re
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta

import database

# Alpha tokens of 3+ characters, no stopword filtering
_WORD_RE = re.compile(r"[a-z]{3,}")
CHUNK_SIZE = 10_000


def audit(since_days=None, top_n=100):
    database.init_db()
    conn = database._get_conn()

    since_clause = ""
    since_params = []
    if since_days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
        since_clause = " AND timestamp_local >= ?"
        since_params = [cutoff]

    # Total count for progress
    total = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'chat:message'" + since_clause,
        since_params,
    ).fetchone()[0]

    if total == 0:
        print("No chat messages found in the specified window.")
        return

    window_label = f"last {since_days} day(s)" if since_days else "all time (retention window)"
    print("=" * 60)
    print("  Chat Keyword Frequency Audit")
    print("=" * 60)
    print()
    print(f"  Window:   {window_label}")
    print(f"  Messages: {total:,}")
    print(f"  Top:      {top_n}")
    print()

    counts = Counter()
    processed = 0
    last_id = 0

    while processed < total:
        rows = conn.execute(
            """SELECT id, data FROM events
               WHERE event_type = 'chat:message'
                 AND id > ?"""
            + since_clause
            + " ORDER BY id LIMIT ?",
            [last_id] + since_params + [CHUNK_SIZE],
        ).fetchall()

        if not rows:
            break

        for row in rows:
            try:
                data = database.fast_loads(row["data"]) if isinstance(row["data"], str) else row["data"]
                text = data.get("message", "") if isinstance(data, dict) else ""
                if text and isinstance(text, str):
                    counts.update(_WORD_RE.findall(text.lower()))
            except Exception:
                pass

        last_id = rows[-1]["id"]
        processed += len(rows)
        pct = (processed / total) * 100
        print(f"\r  Processing: {processed:,}/{total:,} ({pct:.0f}%)", end="", flush=True)

    print("\r" + " " * 60 + "\r", end="")

    # Results
    top = counts.most_common(top_n)
    total_tokens = sum(counts.values())
    unique_words = len(counts)

    print(f"  Total tokens:  {total_tokens:,}")
    print(f"  Unique words:  {unique_words:,}")
    print()
    print(f"  {'Rank':<6} {'Word':<25} {'Count':>10} {'% of tokens':>12}")
    print(f"  {'-'*6} {'-'*25} {'-'*10} {'-'*12}")

    for i, (word, count) in enumerate(top, 1):
        pct = (count / total_tokens) * 100
        print(f"  {i:<6} {word:<25} {count:>10,} {pct:>11.2f}%")

    print()
    print("  Words above ~0.5% are strong stopword candidates.")
    print("  Words that are high-frequency but topically relevant")
    print("  (contestant names, show terms) should be kept.")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit raw word frequencies in chat messages")
    parser.add_argument("--since", type=int, default=None, help="Only look at the last N days")
    parser.add_argument("--top", type=int, default=100, help="How many top words to show (default 100)")
    args = parser.parse_args()
    audit(since_days=args.since, top_n=args.top)
