"""
Database module for persistent event storage.
Uses SQLite with WAL mode for concurrent read/write.
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB_PATH = Path(os.environ.get("FISHTANK_DB_PATH", Path(__file__).parent / "fishtank.db"))

_local = threading.local()


def _get_conn():
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(str(DB_PATH))
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn.execute("PRAGMA temp_store=MEMORY")
        _local.conn.execute("PRAGMA cache_size=-16384")  # 16 MB page cache per connection
    return _local.conn


def init_db():
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            event_id TEXT,
            timestamp_server INTEGER,
            timestamp_local TEXT NOT NULL,
            data JSON NOT NULL
        );
        -- Composite index for keyset pagination (before_id) and poll:vote subqueries
        DROP INDEX IF EXISTS idx_events_type;
        CREATE INDEX IF NOT EXISTS idx_events_type_id ON events(event_type, id);
        CREATE INDEX IF NOT EXISTS idx_events_type_ts_local ON events(event_type, timestamp_local);
        CREATE INDEX IF NOT EXISTS idx_events_ts_local ON events(timestamp_local);
        -- Partial index for fishtoy dedup and superchat ID lookups
        CREATE INDEX IF NOT EXISTS idx_events_event_id ON events(event_id)
            WHERE event_id IS NOT NULL;
        -- Drop unused timestamp_server index (never queried)
        DROP INDEX IF EXISTS idx_events_ts;

        CREATE TABLE IF NOT EXISTS stock_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            ticker TEXT NOT NULL,
            price INTEGER NOT NULL,
            today_open INTEGER,
            last_hour INTEGER,
            last_week INTEGER,
            average_price INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_stock_ticker ON stock_history(ticker);
        CREATE INDEX IF NOT EXISTS idx_stock_ts ON stock_history(timestamp);
        CREATE INDEX IF NOT EXISTS idx_stock_ticker_ts ON stock_history(ticker, timestamp);

        -- Legacy json_extract indexes (kept to avoid DROP+CREATE OOM; harmless)
        CREATE INDEX IF NOT EXISTS idx_events_sentiment ON events(event_type, timestamp_local)
            WHERE json_extract(data, '$.sentiment') IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_chat_user_ts ON events(
            event_type, timestamp_local, json_extract(data, '$.user.displayName')
        ) WHERE event_type = 'chat:message'
          AND json_extract(data, '$.user.displayName') IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_tts_sender_ts ON events(
            event_type, timestamp_local, json_extract(data, '$.displayName')
        ) WHERE event_type = 'tts:update'
          AND json_extract(data, '$.displayName') IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_sfx_sender_ts ON events(
            event_type, timestamp_local, json_extract(data, '$.displayName')
        ) WHERE event_type = 'sfx:update'
          AND json_extract(data, '$.displayName') IS NOT NULL;
    """)

    # Add extracted columns (idempotent — ALTER TABLE ADD COLUMN is a no-op if exists)
    for col in [
        "sentiment REAL",
        "cost INTEGER",
        "display_name TEXT",
        "target TEXT",
        "room TEXT",
        "metadata TEXT",
        "item_id TEXT",
        "feature TEXT",
    ]:
        try:
            conn.execute(f"ALTER TABLE events ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass  # Column already exists

    # Indexes on extracted columns
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_events_ext_sentiment ON events(event_type, sentiment)
            WHERE sentiment IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_events_ext_cost ON events(event_type, cost)
            WHERE cost IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_events_ext_display_name ON events(event_type, display_name)
            WHERE display_name IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_events_ext_target ON events(event_type, target)
            WHERE target IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_events_ext_sender_ts ON events(event_type, timestamp_local, display_name)
            WHERE display_name IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_events_ext_room ON events(event_type, room)
            WHERE room IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_events_ext_metadata ON events(event_type, metadata)
            WHERE metadata IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_events_ext_item_id ON events(event_type, item_id)
            WHERE item_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_events_ext_feature ON events(event_type, feature)
            WHERE feature IS NOT NULL;
    """)
    conn.commit()


def backfill_extracted_columns(batch_size=1000):
    """Backfill extracted columns for existing rows. Processes in small batches to limit memory.

    Uses Python-side extraction instead of json_extract in UPDATE to avoid SQLite
    parsing all JSON blobs in C (which OOM-killed the 1GB container at 5k batch size).
    """
    conn = _get_conn()
    # Check if backfill is needed: look for events with NULL extracted columns
    sample = conn.execute("""
        SELECT id FROM events
        WHERE (event_type = 'tts:update' AND cost IS NULL)
           OR (event_type = 'feature-toggles:update' AND feature IS NULL)
        LIMIT 1
    """).fetchone()
    if not sample:
        return 0  # Already backfilled

    max_id = conn.execute("SELECT MAX(id) FROM events").fetchone()[0]
    if not max_id:
        return 0

    total = 0
    for start in range(1, max_id + 1, batch_size):
        end = start + batch_size - 1
        rows = conn.execute("""
            SELECT id, event_type, data FROM events
            WHERE id BETWEEN ? AND ?
        """, (start, end)).fetchall()

        for row in rows:
            try:
                data = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
            except (json.JSONDecodeError, TypeError):
                data = {}
            sentiment, cost, display_name, target, room, metadata_val, item_id_val, feature = _extract_columns(row["event_type"], data)
            conn.execute("""
                UPDATE events SET sentiment = ?, cost = ?, display_name = ?, target = ?,
                    room = ?, metadata = ?, item_id = ?, feature = ?
                WHERE id = ?
            """, (sentiment, cost, display_name, target, room, metadata_val, item_id_val, feature, row["id"]))

        conn.commit()
        total += len(rows)
        if total % 10000 == 0:
            print(f"[...] Backfill progress: {total} events")

    return total


def _extract_columns(event_type, data):
    """Extract denormalized columns from event data dict."""
    if not isinstance(data, dict):
        return None, None, None, None, None, None, None, None
    sentiment = data.get("sentiment")
    cost_raw = data.get("cost")
    cost = int(cost_raw) if cost_raw is not None else None
    display_name = data.get("displayName")
    if not display_name and isinstance(data.get("user"), dict):
        display_name = data["user"].get("displayName")
    target = data.get("target")
    room = data.get("room")
    metadata_val = data.get("metadata")
    if metadata_val in (None, "null", ""):
        metadata_val = None
    elif not isinstance(metadata_val, str):
        metadata_val = json.dumps(metadata_val)
    item_id_val = data.get("itemId")
    if item_id_val is not None:
        item_id_val = str(item_id_val)
    feature = data.get("feature")
    return sentiment, cost, display_name, target, room, metadata_val, item_id_val, feature


def store_event(event_type: str, data):
    conn = _get_conn()
    event_id = None
    timestamp_server = None

    if isinstance(data, dict):
        event_id = data.get("id")
        timestamp_server = data.get("timestamp") or data.get("createdAt")

    now = datetime.now(timezone.utc).isoformat()
    data_json = json.dumps(data, ensure_ascii=False, default=str)
    sentiment, cost, display_name, target, room, metadata_val, item_id_val, feature = _extract_columns(event_type, data)

    cursor = conn.execute(
        "INSERT INTO events (event_type, event_id, timestamp_server, timestamp_local, data, sentiment, cost, display_name, target, room, metadata, item_id, feature) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (event_type, str(event_id) if event_id else None, timestamp_server, now, data_json, sentiment, cost, display_name, target, room, metadata_val, item_id_val, feature),
    )
    conn.commit()
    return cursor.lastrowid


def get_events(event_type=None, limit=200, since_id=None):
    conn = _get_conn()
    query = "SELECT id, event_type, event_id, timestamp_server, timestamp_local, data FROM events"
    conditions = []
    params = []

    if event_type:
        types = [t.strip() for t in event_type.split(",")]
        placeholders = ",".join("?" for _ in types)
        conditions.append(f"event_type IN ({placeholders})")
        params.extend(types)

    if since_id is not None:
        conditions.append("id > ?")
        params.append(since_id)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    return [
        {
            "id": row["id"],
            "event_type": row["event_type"],
            "event_id": row["event_id"],
            "timestamp_server": row["timestamp_server"],
            "timestamp_local": row["timestamp_local"],
            "data": json.loads(row["data"]),
        }
        for row in rows
    ]


def get_stats(since=None):
    conn = _get_conn()
    since_clause = ""
    since_params = []
    if since:
        since_clause = " AND timestamp_local >= ?"
        since_params = [since]

    total = conn.execute(
        "SELECT COUNT(*) FROM events WHERE 1=1" + since_clause, since_params
    ).fetchone()[0]

    type_counts = conn.execute(
        "SELECT event_type, COUNT(*) as count FROM events WHERE 1=1" + since_clause +
        " GROUP BY event_type ORDER BY count DESC", since_params
    ).fetchall()

    fishtoy_stats = conn.execute("""
        SELECT COUNT(*) as total,
            COALESCE(SUM(cost), 0) as total_cost
        FROM events WHERE event_type LIKE 'fishtoy%%'
    """ + since_clause, since_params).fetchone()

    # Scoped to cost-bearing event types only
    all_spend = conn.execute("""
        SELECT COALESCE(SUM(cost), 0) as total
        FROM events WHERE (event_type IN ('tts:update', 'sfx:update', 'super-chat:new')
            OR event_type LIKE 'fishtoy%')
    """ + since_clause, since_params).fetchone()

    # Poll token spend: sum final vote scores for each completed poll
    poll_tokens_rows = conn.execute("""
        SELECT
            (SELECT COALESCE(SUM(json_extract(v.value, '$.score')), 0)
             FROM json_each(
                 COALESCE(
                     (SELECT data FROM events WHERE event_type = 'poll:vote' AND id < pe.id ORDER BY id DESC LIMIT 1),
                     '[]'
                 )
             ) v
            ) AS poll_total
        FROM events pe WHERE event_type = 'poll:stop'
    """ + since_clause, since_params).fetchall()
    poll_tokens = sum(r["poll_total"] or 0 for r in poll_tokens_rows)

    # Active poll contribution: delta between current votes and snapshot before the since window
    active_start = conn.execute("""
        SELECT id FROM events WHERE event_type = 'poll:start'
        AND NOT EXISTS (
            SELECT 1 FROM events e2 WHERE e2.event_type = 'poll:stop' AND e2.id > events.id
        )
        ORDER BY id DESC LIMIT 1
    """).fetchone()
    if active_start:
        current_vote = conn.execute("""
            SELECT data FROM events WHERE event_type = 'poll:vote' AND id > ?
            ORDER BY id DESC LIMIT 1
        """, (active_start["id"],)).fetchone()
        if current_vote:
            current_total = sum(v.get("score", 0) for v in json.loads(current_vote["data"]))
            baseline_total = 0
            if since:
                baseline_vote = conn.execute("""
                    SELECT data FROM events WHERE event_type = 'poll:vote' AND id > ?
                    AND timestamp_local < ?
                    ORDER BY id DESC LIMIT 1
                """, (active_start["id"], since)).fetchone()
                if baseline_vote:
                    baseline_total = sum(v.get("score", 0) for v in json.loads(baseline_vote["data"]))
            poll_tokens += current_total - baseline_total

    # Superchat token spend
    superchat_stats = conn.execute("""
        SELECT COALESCE(SUM(cost), 0) as total
        FROM events WHERE event_type = 'super-chat:new'
    """ + since_clause, since_params).fetchone()
    superchat_tokens = superchat_stats["total"] if superchat_stats else 0

    top_targets = conn.execute("""
        SELECT target, COUNT(*) as count
        FROM events WHERE event_type LIKE 'fishtoy%%'
            AND target IS NOT NULL
    """ + since_clause + " GROUP BY target ORDER BY count DESC LIMIT 10", since_params).fetchall()

    # Per-type leaderboards and UNION ALL top_senders are expensive (json_extract
    # GROUP BY across 600k+ rows). Only compute when `since` is set (24h sidebar).
    # All-time stats call uses the cheap fishtoy-only top_senders below.
    if since:
        top_senders = conn.execute("""
            SELECT sender, SUM(total_spend) as spend, SUM(total_count) as count FROM (
                SELECT display_name as sender,
                    COALESCE(SUM(cost), 0) as total_spend,
                    COUNT(*) as total_count
                FROM events WHERE event_type LIKE 'fishtoy%%'
                    AND display_name IS NOT NULL
                    AND timestamp_local >= ?
                GROUP BY sender
                UNION ALL
                SELECT display_name as sender,
                    COALESCE(SUM(cost), 0) as total_spend,
                    COUNT(*) as total_count
                FROM events WHERE event_type = 'tts:update'
                    AND display_name IS NOT NULL
                    AND timestamp_local >= ?
                GROUP BY sender
                UNION ALL
                SELECT display_name as sender,
                    COALESCE(SUM(cost), 0) as total_spend,
                    COUNT(*) as total_count
                FROM events WHERE event_type = 'sfx:update'
                    AND display_name IS NOT NULL
                    AND timestamp_local >= ?
                GROUP BY sender
            ) GROUP BY sender ORDER BY spend DESC LIMIT 5
        """, (since, since, since)).fetchall()

        top_tts_senders = conn.execute("""
            SELECT display_name as name, COUNT(*) as count,
                COALESCE(SUM(cost), 0) as spend
            FROM events WHERE event_type = 'tts:update'
                AND display_name IS NOT NULL
        """ + since_clause + " GROUP BY name ORDER BY spend DESC LIMIT 5", since_params).fetchall()

        top_sfx_senders = conn.execute("""
            SELECT display_name as name, COUNT(*) as count,
                COALESCE(SUM(cost), 0) as spend
            FROM events WHERE event_type = 'sfx:update'
                AND display_name IS NOT NULL
        """ + since_clause + " GROUP BY name ORDER BY spend DESC LIMIT 5", since_params).fetchall()

        top_chat_senders = conn.execute("""
            SELECT display_name as name, COUNT(*) as count
            FROM events WHERE event_type = 'chat:message'
                AND display_name IS NOT NULL
        """ + since_clause + " GROUP BY name ORDER BY count DESC LIMIT 5", since_params).fetchall()

        top_fishtoy_senders = conn.execute("""
            SELECT display_name as name, COUNT(*) as count,
                COALESCE(SUM(cost), 0) as spend
            FROM events WHERE event_type LIKE 'fishtoy%%'
                AND display_name IS NOT NULL
        """ + since_clause + " GROUP BY name ORDER BY spend DESC LIMIT 5", since_params).fetchall()
    else:
        top_senders_rows = conn.execute("""
            SELECT display_name as sender, COUNT(*) as count,
                COALESCE(SUM(cost), 0) as spend
            FROM events WHERE event_type LIKE 'fishtoy%%'
                AND display_name IS NOT NULL
            GROUP BY sender ORDER BY count DESC LIMIT 10
        """).fetchall()
        top_senders = top_senders_rows
        top_tts_senders = []
        top_sfx_senders = []
        top_chat_senders = []
        top_fishtoy_senders = []

    return {
        "total_events": total,
        "by_type": {r["event_type"]: r["count"] for r in type_counts},
        "fishtoys": {
            "total": fishtoy_stats["total"] if fishtoy_stats else 0,
            "total_cost": fishtoy_stats["total_cost"] if fishtoy_stats else 0,
        },
        "total_spend": (all_spend["total"] if all_spend else 0) + poll_tokens,
        "poll_tokens": poll_tokens,
        "superchat_tokens": superchat_tokens,
        "top_targets": [{"name": r["target"], "count": r["count"]} for r in top_targets],
        "top_senders": [{"name": r["sender"], "count": r["count"], "spend": r["spend"]} for r in top_senders],
        "top_tts_senders": [{"name": r["name"], "count": r["count"], "spend": r["spend"]} for r in top_tts_senders],
        "top_sfx_senders": [{"name": r["name"], "count": r["count"], "spend": r["spend"]} for r in top_sfx_senders],
        "top_chat_senders": [{"name": r["name"], "count": r["count"]} for r in top_chat_senders],
        "top_fishtoy_senders": [{"name": r["name"], "count": r["count"], "spend": r["spend"]} for r in top_fishtoy_senders],
    }


def get_fishtoys(target=None, item_id=None, search=None, limit=200, offset=0):
    """Get fishtoy events with optional filters."""
    conn = _get_conn()
    query = "SELECT id, event_type, event_id, timestamp_server, timestamp_local, data FROM events"
    conditions = ["event_type LIKE 'fishtoy%'"]
    params = []

    if target:
        conditions.append("target = ?")
        params.append(target)

    if item_id:
        conditions.append("item_id = ?")
        params.append(str(item_id))

    if search:
        conditions.append("(metadata LIKE ? OR display_name LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = conn.execute(query, params).fetchall()
    return [
        {
            "id": row["id"],
            "event_type": row["event_type"],
            "event_id": row["event_id"],
            "timestamp_server": row["timestamp_server"],
            "timestamp_local": row["timestamp_local"],
            "data": json.loads(row["data"]),
        }
        for row in rows
    ]


def get_targets():
    """Get all distinct fishtoy targets with total count and spend from the full event history."""
    conn = _get_conn()
    rows = conn.execute("""
        SELECT target, COUNT(*) as count,
            COALESCE(SUM(cost), 0) as spend
        FROM events
        WHERE event_type LIKE 'fishtoy%'
            AND target IS NOT NULL
        GROUP BY target
        ORDER BY count DESC
    """).fetchall()
    return [{"target": r["target"], "count": r["count"], "spend": r["spend"]} for r in rows]


# ============================================================
# STOCK HISTORY
# ============================================================


def store_stock_snapshot(stocks):
    """Store a snapshot of all stock prices."""
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    for s in stocks:
        conn.execute(
            "INSERT INTO stock_history (timestamp, ticker, price, today_open, last_hour, last_week, average_price) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now, s.get("tickerSymbol"), s.get("currentPrice"), s.get("today"),
             s.get("lastHour"), s.get("lastWeek"), s.get("averagePrice")),
        )
    conn.commit()


def prune_stock_history(retention_days=30):
    """Downsample stock history older than retention_days to one row per ticker per day.

    Keeps daily averages for long-term charts (all/ipo ranges use daily bucketing).
    Deletes per-minute granularity beyond the retention window.
    Returns count of deleted rows.
    """
    conn = _get_conn()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()

    # Check if there's anything to prune (avoid unnecessary work)
    old_count = conn.execute(
        "SELECT COUNT(*) FROM stock_history WHERE timestamp < ?", (cutoff,)
    ).fetchone()[0]
    if old_count == 0:
        return 0

    # For each ticker+day with multiple rows, keep only one row with averaged values.
    # Strategy: delete all old rows, then insert daily summaries.
    # Compute daily summaries first, then replace.
    summaries = conn.execute("""
        SELECT
            strftime('%Y-%m-%dT12:00:00+00:00', timestamp) AS day_ts,
            ticker,
            CAST(ROUND(AVG(price)) AS INTEGER) AS price,
            CAST(ROUND(AVG(today_open)) AS INTEGER) AS today_open,
            CAST(ROUND(AVG(last_hour)) AS INTEGER) AS last_hour,
            CAST(ROUND(AVG(last_week)) AS INTEGER) AS last_week,
            CAST(ROUND(AVG(average_price)) AS INTEGER) AS average_price
        FROM stock_history
        WHERE timestamp < ?
        GROUP BY ticker, strftime('%Y-%m-%d', timestamp)
    """, (cutoff,)).fetchall()

    # Delete all old per-minute rows
    conn.execute("DELETE FROM stock_history WHERE timestamp < ?", (cutoff,))

    # Insert daily summaries
    for s in summaries:
        conn.execute(
            "INSERT INTO stock_history (timestamp, ticker, price, today_open, last_hour, last_week, average_price) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (s["day_ts"], s["ticker"], s["price"], s["today_open"], s["last_hour"], s["last_week"], s["average_price"]),
        )

    conn.commit()
    deleted = old_count - len(summaries)
    return deleted


def get_stock_history(ticker=None, limit=500, since=None):
    """Get stock price history, optionally filtered by ticker and/or time."""
    conn = _get_conn()
    conditions = []
    params = []
    if ticker:
        conditions.append("ticker = ?")
        params.append(ticker)
    if since:
        conditions.append("timestamp >= ?")
        params.append(since)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM stock_history{where} ORDER BY id DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


# ============================================================
# TTS / SFX ANALYTICS
# ============================================================


def get_tts_sfx_analytics(since=None):
    """Aggregate analytics for TTS and SFX events."""
    conn = _get_conn()
    since_clause = ""
    since_params = []
    if since:
        since_clause = " AND timestamp_local >= ?"
        since_params = [since]

    top_rooms = conn.execute("""
        SELECT room, COUNT(*) as count
        FROM events WHERE event_type IN ('tts:update', 'sfx:update')
            AND room IS NOT NULL
    """ + since_clause + " GROUP BY room ORDER BY count DESC LIMIT 10", since_params).fetchall()

    top_tts_senders = conn.execute("""
        SELECT display_name as sender, COUNT(*) as count,
            COALESCE(SUM(cost), 0) as spend
        FROM events WHERE event_type = 'tts:update' AND display_name IS NOT NULL
    """ + since_clause + " GROUP BY sender ORDER BY spend DESC LIMIT 10", since_params).fetchall()

    top_sfx_senders = conn.execute("""
        SELECT display_name as sender, COUNT(*) as count,
            COALESCE(SUM(cost), 0) as spend
        FROM events WHERE event_type = 'sfx:update' AND display_name IS NOT NULL
    """ + since_clause + " GROUP BY sender ORDER BY spend DESC LIMIT 10", since_params).fetchall()

    hourly = conn.execute("""
        SELECT strftime('%H', timestamp_local) as hour,
            strftime('%Y-%m-%dT%H:00:00Z', timestamp_local) as ts,
            COUNT(*) as count
        FROM events WHERE event_type IN ('tts:update', 'sfx:update')
            AND timestamp_local >= datetime('now', '-24 hours')
        GROUP BY hour ORDER BY hour
    """).fetchall()

    return {
        "top_rooms": [{"room": r["room"], "count": r["count"]} for r in top_rooms],
        "top_tts_senders": [{"name": r["sender"], "count": r["count"], "spend": r["spend"]} for r in top_tts_senders],
        "top_sfx_senders": [{"name": r["sender"], "count": r["count"], "spend": r["spend"]} for r in top_sfx_senders],
        "hourly": [{"hour": r["hour"], "ts": r["ts"], "count": r["count"]} for r in hourly],
    }


# ============================================================
# CHAT ANALYTICS
# ============================================================


def get_chat_analytics(since=None):
    """Aggregate analytics for chat messages."""
    conn = _get_conn()
    since_clause = ""
    since_params = []
    if since:
        since_clause = " AND timestamp_local >= ?"
        since_params = [since]

    total = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'chat:message'" + since_clause, since_params
    ).fetchone()[0]

    top_chatters = conn.execute("""
        SELECT display_name as name, COUNT(*) as count
        FROM events WHERE event_type = 'chat:message' AND display_name IS NOT NULL
    """ + since_clause + " GROUP BY name ORDER BY count DESC LIMIT 15", since_params).fetchall()

    hourly = conn.execute("""
        SELECT strftime('%H', timestamp_local) as hour,
            strftime('%Y-%m-%dT%H:00:00Z', timestamp_local) as ts,
            COUNT(*) as count
        FROM events WHERE event_type = 'chat:message'
            AND timestamp_local >= datetime('now', '-24 hours')
        GROUP BY hour ORDER BY hour
    """).fetchall()

    return {
        "total": total,
        "top_chatters": [{"name": r["name"], "count": r["count"]} for r in top_chatters],
        "hourly": [{"hour": r["hour"], "ts": r["ts"], "count": r["count"]} for r in hourly],
    }


# ============================================================
# HIDDEN CONTENT ARCHIVE
# ============================================================


def get_hidden_content(target=None, search=None, limit=200, offset=0):
    """Get only fishtoy events that have metadata (hidden content)."""
    conn = _get_conn()
    conditions = [
        "event_type LIKE 'fishtoy%'",
        "metadata IS NOT NULL",
    ]
    params = []

    if target:
        conditions.append("target = ?")
        params.append(target)

    if search:
        conditions.append("metadata LIKE ?")
        params.append(f"%{search}%")

    query = "SELECT id, event_type, event_id, timestamp_server, timestamp_local, data FROM events"
    query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = conn.execute(query, params).fetchall()
    return [
        {
            "id": row["id"],
            "event_type": row["event_type"],
            "event_id": row["event_id"],
            "timestamp_server": row["timestamp_server"],
            "timestamp_local": row["timestamp_local"],
            "data": json.loads(row["data"]),
        }
        for row in rows
    ]


# ============================================================
# SUPERCHATS
# ============================================================


def get_superchats(limit=50, since=None):
    """Get super-chat:new events with deletion status resolved at query time."""
    conn = _get_conn()
    since_clause = ""
    since_params = []
    if since:
        since_clause = " AND sc.timestamp_local >= ?"
        since_params = [since]

    rows = conn.execute("""
        SELECT sc.id, sc.event_id, sc.timestamp_local, sc.data,
            CASE WHEN del.id IS NOT NULL THEN 1 ELSE 0 END as deleted
        FROM events sc
        LEFT JOIN events del ON del.event_type = 'super-chat:delete'
            AND del.event_id = sc.event_id
        WHERE sc.event_type = 'super-chat:new'
    """ + since_clause + " ORDER BY sc.id DESC LIMIT ?", since_params + [limit]).fetchall()
    return [
        {
            "id": row["id"],
            "event_id": row["event_id"],
            "timestamp_local": row["timestamp_local"],
            "data": json.loads(row["data"]),
            "deleted": bool(row["deleted"]),
        }
        for row in rows
    ]


def get_known_superchat_ids():
    """Return set of event_ids for all stored super-chat:new events."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT event_id FROM events WHERE event_type = 'super-chat:new' AND event_id IS NOT NULL"
    ).fetchall()
    return {r["event_id"] for r in rows}


# ============================================================
# POLLS
# ============================================================


def get_polls(limit=50):
    """Get poll start and stop events (excludes vote tallies)."""
    conn = _get_conn()
    rows = conn.execute("""
        SELECT id, event_type, event_id, timestamp_server, timestamp_local, data
        FROM events WHERE event_type IN ('poll:start', 'poll:stop')
        ORDER BY id DESC LIMIT ?
    """, (limit,)).fetchall()
    return [
        {
            "id": row["id"],
            "event_type": row["event_type"],
            "timestamp_local": row["timestamp_local"],
            "data": json.loads(row["data"]),
        }
        for row in rows
    ]


# ============================================================
# NOTIFICATIONS / DIRECTOR MESSAGES
# ============================================================


def get_notifications(limit=100):
    """Get director messages and announcements."""
    conn = _get_conn()
    rows = conn.execute("""
        SELECT id, event_type, timestamp_local, data
        FROM events WHERE event_type IN ('notification:global', 'announcement')
        ORDER BY id DESC LIMIT ?
    """, (limit,)).fetchall()
    return [
        {
            "id": row["id"],
            "event_type": row["event_type"],
            "timestamp_local": row["timestamp_local"],
            "data": json.loads(row["data"]),
        }
        for row in rows
    ]


# ============================================================
# PRICE CHANGES
# ============================================================


def get_price_changes(limit=100):
    """Get TTS/SFX price change events."""
    conn = _get_conn()
    rows = conn.execute("""
        SELECT id, event_type, timestamp_local, data
        FROM events WHERE event_type IN ('tts:price', 'sfx:price')
        ORDER BY id DESC LIMIT ?
    """, (limit,)).fetchall()
    return [
        {
            "id": row["id"],
            "event_type": row["event_type"],
            "timestamp_local": row["timestamp_local"],
            "data": json.loads(row["data"]),
        }
        for row in rows
    ]


# ============================================================
# USER SEARCH
# ============================================================


def search_user(username, limit=500):
    """Search across all event types for a specific username (case-insensitive).

    Uses COLLATE NOCASE instead of LOWER() so SQLite can use the covering indexes
    (idx_chat_user_ts, idx_tts_sender_ts, idx_sfx_sender_ts) rather than full table scans.
    """
    conn = _get_conn()
    results = {
        "username": username,
        "chat": [],
        "tts": [],
        "sfx": [],
        "fishtoys": [],
    }

    # Chat messages — uses idx_events_ext_sender_ts
    rows = conn.execute("""
        SELECT id, timestamp_local, data FROM events
        WHERE event_type = 'chat:message'
        AND display_name = ? COLLATE NOCASE
        ORDER BY id DESC LIMIT ?
    """, (username, limit)).fetchall()
    results["chat"] = [{"id": r["id"], "timestamp": r["timestamp_local"], "data": json.loads(r["data"])} for r in rows]

    # TTS
    rows = conn.execute("""
        SELECT id, timestamp_local, data FROM events
        WHERE event_type = 'tts:update'
        AND display_name = ? COLLATE NOCASE
        ORDER BY id DESC LIMIT ?
    """, (username, limit)).fetchall()
    results["tts"] = [{"id": r["id"], "timestamp": r["timestamp_local"], "data": json.loads(r["data"])} for r in rows]

    # SFX
    rows = conn.execute("""
        SELECT id, timestamp_local, data FROM events
        WHERE event_type = 'sfx:update'
        AND display_name = ? COLLATE NOCASE
        ORDER BY id DESC LIMIT ?
    """, (username, limit)).fetchall()
    results["sfx"] = [{"id": r["id"], "timestamp": r["timestamp_local"], "data": json.loads(r["data"])} for r in rows]

    # Fishtoys
    rows = conn.execute("""
        SELECT id, timestamp_local, data FROM events
        WHERE event_type LIKE 'fishtoy%'
        AND display_name = ? COLLATE NOCASE
        Order BY id DESC LIMIT ?
    """, (username, limit)).fetchall()
    results["fishtoys"] = [{"id": r["id"], "timestamp": r["timestamp_local"], "data": json.loads(r["data"])} for r in rows]

    results["totals"] = {
        "chat": len(results["chat"]),
        "tts": len(results["tts"]),
        "sfx": len(results["sfx"]),
        "fishtoys": len(results["fishtoys"]),
    }

    return results


# ============================================================
# USER AUTOCOMPLETE
# ============================================================


def suggest_users(prefix, limit=10):
    """Return distinct displayNames matching a prefix (case-insensitive).

    Uses COLLATE NOCASE on the display_name extracted column for prefix LIKE lookups.
    """
    conn = _get_conn()
    prefix_pattern = prefix + "%"

    rows = conn.execute("""
        SELECT DISTINCT display_name as name FROM events
        WHERE display_name LIKE ? COLLATE NOCASE
        AND display_name IS NOT NULL
        LIMIT ?
    """, (prefix_pattern, limit)).fetchall()
    return [row["name"] for row in rows]


# ============================================================
# STOCK SNAPSHOT COUNT
# ============================================================


def get_stock_snapshot_count():
    """Return actual count of stock history rows."""
    conn = _get_conn()
    return conn.execute("SELECT COUNT(*) FROM stock_history").fetchone()[0]


# ============================================================
# POLL STATE RECONSTRUCTION
# ============================================================


def get_latest_poll_state():
    """Reconstruct the current/latest poll state from database.

    Returns the most recent poll:start, its latest vote tallies,
    and the poll:stop if it exists.
    """
    conn = _get_conn()

    # Get the most recent poll:start
    start = conn.execute("""
        SELECT id, timestamp_local, data FROM events
        WHERE event_type = 'poll:start'
        ORDER BY id DESC LIMIT 1
    """).fetchone()

    if not start:
        return None

    start_data = json.loads(start["data"])
    poll_info = start_data.get("poll", start_data)
    pid = poll_info.get("pid", "")

    # Check if there's a poll:stop after this start
    stop = conn.execute("""
        SELECT id, timestamp_local, data FROM events
        WHERE event_type = 'poll:stop' AND id > ?
        ORDER BY id ASC LIMIT 1
    """, (start["id"],)).fetchone()

    # Get the latest vote tallies after this start
    last_vote = conn.execute("""
        SELECT data FROM events
        WHERE event_type = 'poll:vote' AND id > ?
        ORDER BY id DESC LIMIT 1
    """, (start["id"],)).fetchone()

    result = {
        "question": poll_info.get("question"),
        "answers": poll_info.get("answers", []),
        "pid": pid,
        "started_at": start["timestamp_local"],
        "active": stop is None,
    }

    if last_vote:
        result["votes"] = json.loads(last_vote["data"])

    if stop:
        stop_data = json.loads(stop["data"])
        result["winner"] = stop_data.get("winner")
        result["ended_at"] = stop["timestamp_local"]
        result["active"] = False

    return result


# ============================================================
# FEATURE TOGGLES
# ============================================================


def get_latest_feature_toggles():
    """Get the most recent state for each feature toggle."""
    conn = _get_conn()
    rows = conn.execute("""
        SELECT e.data FROM events e
        INNER JOIN (
            SELECT feature, MAX(id) as max_id
            FROM events WHERE event_type = 'feature-toggles:update'
                AND feature IS NOT NULL
            GROUP BY feature
        ) latest ON e.id = latest.max_id
    """).fetchall()
    result = {}
    for row in rows:
        d = json.loads(row["data"])
        feature = d.get("feature", "")
        if feature:
            result[feature] = {
                "enabled": d.get("enabled", False),
                "metadata": d.get("metadata"),
                "updated_at": d.get("updatedAt"),
            }
    return result


# ============================================================
# DATA CLEANUP
# ============================================================


def dedup_tts_sfx():
    """Remove duplicate TTS/SFX events, keeping the first entry per event_id.
    Returns count of deleted rows."""
    conn = _get_conn()
    # Find duplicates: same event_id, keep lowest db id
    result = conn.execute("""
        DELETE FROM events WHERE id IN (
            SELECT e.id FROM events e
            INNER JOIN (
                SELECT event_id, MIN(id) as keep_id
                FROM events
                WHERE event_type IN ('tts:update', 'sfx:update')
                AND event_id IS NOT NULL
                GROUP BY event_id
                HAVING COUNT(*) > 1
            ) dups ON e.event_id = dups.event_id AND e.id != dups.keep_id
            WHERE e.event_type IN ('tts:update', 'sfx:update')
        )
    """)
    deleted = result.rowcount
    conn.commit()
    return deleted


def purge_system_chat():
    """Remove chat messages from system users (tts, sfx, emote).
    Returns count of deleted rows."""
    conn = _get_conn()
    result = conn.execute("""
        DELETE FROM events WHERE event_type = 'chat:message'
        AND LOWER(display_name) IN ('tts', 'sfx', 'emote')
    """)
    deleted = result.rowcount
    conn.commit()
    return deleted


def purge_gift_notifications():
    """Remove season pass gift notifications.
    Returns count of deleted rows."""
    conn = _get_conn()
    result = conn.execute("""
        DELETE FROM events WHERE event_type = 'notification:global'
        AND LOWER(json_extract(data, '$.message')) LIKE '%gifted%'
        AND LOWER(json_extract(data, '$.message')) LIKE '%season pass%'
    """)
    deleted = result.rowcount
    if deleted == 0:
        # Try with data as string
        result = conn.execute("""
            DELETE FROM events WHERE event_type = 'notification:global'
            AND LOWER(data) LIKE '%gifted%'
            AND LOWER(data) LIKE '%season pass%'
        """)
        deleted = result.rowcount
        conn.commit()
    return deleted


# ============================================================
# HEALTH
# ============================================================


def get_last_event_per_type():
    """Return the most recent timestamp for each event type."""
    conn = _get_conn()
    rows = conn.execute("""
        SELECT event_type, MAX(timestamp_local) as last_seen, COUNT(*) as total
        FROM events GROUP BY event_type
    """).fetchall()
    return {row["event_type"]: {"last_seen": row["last_seen"], "total": row["total"]} for row in rows}


def get_event_count():
    """Return total event count."""
    conn = _get_conn()
    return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


# ============================================================
# BACKFILL DETECTION
# ============================================================


def get_known_fishtoy_ids(limit=200):
    """Return set of event_ids for recent fishtoy events."""
    conn = _get_conn()
    rows = conn.execute("""
        SELECT event_id FROM events
        WHERE event_type LIKE 'fishtoy%%' AND event_id IS NOT NULL
        ORDER BY id DESC LIMIT ?
    """, (limit,)).fetchall()
    return {row["event_id"] for row in rows}


# ============================================================
# PEAK HOURS
# ============================================================


def get_peak_hours():
    """Return combined hourly activity across all event types."""
    conn = _get_conn()

    hourly = conn.execute("""
        SELECT strftime('%H', timestamp_local) as hour,
            strftime('%Y-%m-%dT%H:00:00Z', timestamp_local) as ts,
            SUM(CASE WHEN event_type = 'tts:update' THEN 1 ELSE 0 END) as tts,
            SUM(CASE WHEN event_type = 'sfx:update' THEN 1 ELSE 0 END) as sfx,
            SUM(CASE WHEN event_type LIKE 'fishtoy%%' THEN 1 ELSE 0 END) as fishtoys,
            COUNT(*) as total
        FROM events
        WHERE (event_type IN ('tts:update', 'sfx:update')
            OR event_type LIKE 'fishtoy%%')
            AND timestamp_local >= datetime('now', '-24 hours')
        GROUP BY hour ORDER BY hour
    """).fetchall()

    hours = [{"hour": r["hour"], "ts": r["ts"], "tts": r["tts"],
              "sfx": r["sfx"], "fishtoys": r["fishtoys"], "total": r["total"]}
             for r in hourly]

    # Find peak hours
    if hours:
        peak = sorted(hours, key=lambda h: h["total"], reverse=True)[:3]
        quietest = sorted(hours, key=lambda h: h["total"])[:3]
    else:
        peak = []
        quietest = []

    return {
        "hourly": hours,
        "peak": [{"hour": h["hour"], "ts": h["ts"], "total": h["total"]} for h in peak],
        "quietest": [{"hour": h["hour"], "ts": h["ts"], "total": h["total"]} for h in quietest],
    }


def _mood_label(avg):
    """Map compound score to a mood label."""
    if avg >= 0.5: return "Excited"
    if avg >= 0.15: return "Happy"
    if avg >= -0.15: return "Neutral"
    if avg >= -0.5: return "Grumpy"
    return "Hostile"


def _sentiment_base(conn, type_clause, since=None):
    """Shared sentiment query logic for a given event type filter."""
    since_clause = ""
    params = []
    if since:
        since_clause = " AND timestamp_local >= ?"
        params.append(since)

    base_where = f"{type_clause} AND sentiment IS NOT NULL"

    hourly = conn.execute(f"""
        SELECT strftime('%H', timestamp_local) as hour,
            strftime('%Y-%m-%dT%H:00:00Z', timestamp_local) as ts,
            AVG(sentiment) as avg_sentiment,
            COUNT(*) as message_count
        FROM events
        WHERE {base_where}
            AND timestamp_local >= datetime('now', '-24 hours')
        GROUP BY hour ORDER BY hour
    """).fetchall()

    overall_row = conn.execute(f"""
        SELECT
            AVG(sentiment) as avg,
            SUM(CASE WHEN sentiment >= 0.05 THEN 1 ELSE 0 END) as positive,
            SUM(CASE WHEN sentiment <= -0.05 THEN 1 ELSE 0 END) as negative,
            SUM(CASE WHEN sentiment > -0.05 AND sentiment < 0.05 THEN 1 ELSE 0 END) as neutral,
            COUNT(*) as total
        FROM events
        WHERE {base_where}{since_clause}
    """, params).fetchone()

    total = overall_row["total"] if overall_row["total"] else 0
    avg = round(overall_row["avg"] or 0, 4)
    overall = {
        "avg": avg,
        "positive_pct": round((overall_row["positive"] or 0) / total * 100, 1) if total > 0 else 0,
        "neutral_pct": round((overall_row["neutral"] or 0) / total * 100, 1) if total > 0 else 0,
        "negative_pct": round((overall_row["negative"] or 0) / total * 100, 1) if total > 0 else 0,
    }

    return {
        "hourly": [{"hour": r["hour"], "ts": r["ts"], "avg_sentiment": round(r["avg_sentiment"], 4), "message_count": r["message_count"]} for r in hourly],
        "overall": overall,
        "label": _mood_label(avg),
    }


def get_chat_sentiment(since=None):
    """Sentiment analytics for chat messages."""
    conn = _get_conn()
    return _sentiment_base(conn, "event_type = 'chat:message'", since)


def get_tts_sentiment(since=None):
    """Sentiment analytics for TTS messages, including per-target breakdown."""
    conn = _get_conn()
    result = _sentiment_base(conn, "event_type = 'tts:update'", since)

    since_clause = ""
    params = []
    if since:
        since_clause = " AND timestamp_local >= ?"
        params.append(since)

    by_target = conn.execute(f"""
        SELECT target,
            AVG(sentiment) as avg_sentiment,
            COUNT(*) as message_count
        FROM events
        WHERE event_type = 'tts:update'
            AND sentiment IS NOT NULL
            AND target IS NOT NULL
            {since_clause}
        GROUP BY target ORDER BY avg_sentiment DESC
    """, params).fetchall()

    result["by_target"] = [{"target": r["target"], "avg_sentiment": round(r["avg_sentiment"], 4), "message_count": r["message_count"]} for r in by_target]
    return result
