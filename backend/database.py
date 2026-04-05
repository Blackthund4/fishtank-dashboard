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
        CREATE INDEX IF NOT EXISTS idx_events_sentiment ON events(event_type, timestamp_local)
            WHERE json_extract(data, '$.sentiment') IS NOT NULL;

        -- Covering indexes for user search (COLLATE NOCASE) and analytics
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
    conn.commit()


def store_event(event_type: str, data):
    conn = _get_conn()
    event_id = None
    timestamp_server = None

    if isinstance(data, dict):
        event_id = data.get("id")
        timestamp_server = data.get("timestamp") or data.get("createdAt")

    now = datetime.now(timezone.utc).isoformat()
    data_json = json.dumps(data, ensure_ascii=False, default=str)

    cursor = conn.execute(
        "INSERT INTO events (event_type, event_id, timestamp_server, timestamp_local, data) VALUES (?, ?, ?, ?, ?)",
        (event_type, str(event_id) if event_id else None, timestamp_server, now, data_json),
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
            COALESCE(SUM(CASE WHEN json_extract(data, '$.cost') IS NOT NULL
                THEN CAST(json_extract(data, '$.cost') AS INTEGER) ELSE 0 END), 0) as total_cost
        FROM events WHERE event_type LIKE 'fishtoy%%'
    """ + since_clause, since_params).fetchone()

    # Scoped to cost-bearing event types only — scanning all events with json_extract
    # on a 600k+ row DB took ~10s per call. Do not revert to WHERE 1=1.
    all_spend = conn.execute("""
        SELECT COALESCE(SUM(CAST(json_extract(data, '$.cost') AS INTEGER)), 0) as total
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
        SELECT COALESCE(SUM(CAST(json_extract(data, '$.cost') AS INTEGER)), 0) as total
        FROM events WHERE event_type = 'super-chat:new'
    """ + since_clause, since_params).fetchone()
    superchat_tokens = superchat_stats["total"] if superchat_stats else 0

    top_targets = conn.execute("""
        SELECT json_extract(data, '$.target') as target, COUNT(*) as count
        FROM events WHERE event_type LIKE 'fishtoy%%' AND target IS NOT NULL
    """ + since_clause + " GROUP BY target ORDER BY count DESC LIMIT 10", since_params).fetchall()

    top_senders = conn.execute("""
        SELECT json_extract(data, '$.displayName') as sender, COUNT(*) as count
        FROM events WHERE event_type LIKE 'fishtoy%%' AND sender IS NOT NULL
    """ + since_clause + " GROUP BY sender ORDER BY count DESC LIMIT 10", since_params).fetchall()

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
        "top_senders": [{"name": r["sender"], "count": r["count"]} for r in top_senders],
    }


def get_fishtoys(target=None, item_id=None, search=None, limit=200, offset=0):
    """Get fishtoy events with optional filters."""
    conn = _get_conn()
    query = "SELECT id, event_type, event_id, timestamp_server, timestamp_local, data FROM events"
    conditions = ["event_type LIKE 'fishtoy%'"]
    params = []

    if target:
        conditions.append("json_extract(data, '$.target') = ?")
        params.append(target)

    if item_id:
        conditions.append("json_extract(data, '$.itemId') = ?")
        params.append(str(item_id))

    if search:
        conditions.append("(json_extract(data, '$.metadata') LIKE ? OR json_extract(data, '$.displayName') LIKE ?)")
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
        SELECT json_extract(data, '$.room') as room, COUNT(*) as count
        FROM events WHERE event_type IN ('tts:update', 'sfx:update')
            AND json_extract(data, '$.room') IS NOT NULL
    """ + since_clause + " GROUP BY json_extract(data, '$.room') ORDER BY count DESC LIMIT 10", since_params).fetchall()

    top_tts_senders = conn.execute("""
        SELECT json_extract(data, '$.displayName') as sender,
            COUNT(*) as count,
            COALESCE(SUM(CAST(json_extract(data, '$.cost') AS INTEGER)), 0) as spend
        FROM events WHERE event_type = 'tts:update' AND sender IS NOT NULL
    """ + since_clause + " GROUP BY sender ORDER BY spend DESC LIMIT 10", since_params).fetchall()

    top_sfx_senders = conn.execute("""
        SELECT json_extract(data, '$.displayName') as sender,
            COUNT(*) as count,
            COALESCE(SUM(CAST(json_extract(data, '$.cost') AS INTEGER)), 0) as spend
        FROM events WHERE event_type = 'sfx:update' AND sender IS NOT NULL
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
        SELECT json_extract(data, '$.user.displayName') as name, COUNT(*) as count
        FROM events WHERE event_type = 'chat:message' AND name IS NOT NULL
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
        "json_extract(data, '$.metadata') IS NOT NULL",
        "json_extract(data, '$.metadata') != 'null'",
        "json_extract(data, '$.metadata') != ''",
    ]
    params = []

    if target:
        conditions.append("json_extract(data, '$.target') = ?")
        params.append(target)

    if search:
        conditions.append("json_extract(data, '$.metadata') LIKE ?")
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

    # Chat messages — uses idx_chat_user_ts covering index
    rows = conn.execute("""
        SELECT id, timestamp_local, data FROM events
        WHERE event_type = 'chat:message'
        AND json_extract(data, '$.user.displayName') = ? COLLATE NOCASE
        ORDER BY id DESC LIMIT ?
    """, (username, limit)).fetchall()
    results["chat"] = [{"id": r["id"], "timestamp": r["timestamp_local"], "data": json.loads(r["data"])} for r in rows]

    # TTS — uses idx_tts_sender_ts covering index
    rows = conn.execute("""
        SELECT id, timestamp_local, data FROM events
        WHERE event_type = 'tts:update'
        AND json_extract(data, '$.displayName') = ? COLLATE NOCASE
        Order BY id DESC LIMIT ?
    """, (username, limit)).fetchall()
    results["tts"] = [{"id": r["id"], "timestamp": r["timestamp_local"], "data": json.loads(r["data"])} for r in rows]

    # SFX — uses idx_sfx_sender_ts covering index
    rows = conn.execute("""
        SELECT id, timestamp_local, data FROM events
        WHERE event_type = 'sfx:update'
        AND json_extract(data, '$.displayName') = ? COLLATE NOCASE
        ORDER BY id DESC LIMIT ?
    """, (username, limit)).fetchall()
    results["sfx"] = [{"id": r["id"], "timestamp": r["timestamp_local"], "data": json.loads(r["data"])} for r in rows]

    # Fishtoys — uses idx_events_type on event_type, then NOCASE on displayName
    rows = conn.execute("""
        SELECT id, timestamp_local, data FROM events
        WHERE event_type LIKE 'fishtoy%'
        AND json_extract(data, '$.displayName') = ? COLLATE NOCASE
        ORDER BY id DESC LIMIT ?
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

    Uses COLLATE NOCASE on the json_extract expression directly so SQLite can use
    the covering indexes for prefix LIKE lookups, and avoids the broken LOWER(alias)
    pattern which referenced a SELECT alias in WHERE (undefined behaviour in SQLite).
    """
    conn = _get_conn()
    prefix_pattern = prefix + "%"

    rows = conn.execute("""
        SELECT DISTINCT name FROM (
            SELECT json_extract(data, '$.user.displayName') as name
            FROM events WHERE event_type = 'chat:message'
            AND json_extract(data, '$.user.displayName') LIKE ? COLLATE NOCASE
            UNION
            SELECT json_extract(data, '$.displayName') as name
            FROM events WHERE event_type IN ('tts:update', 'sfx:update')
            AND json_extract(data, '$.displayName') LIKE ? COLLATE NOCASE
            UNION
            SELECT json_extract(data, '$.displayName') as name
            FROM events WHERE event_type LIKE 'fishtoy%'
            AND json_extract(data, '$.displayName') LIKE ? COLLATE NOCASE
        ) WHERE name IS NOT NULL
        LIMIT ?
    """, (prefix_pattern, prefix_pattern, prefix_pattern, limit)).fetchall()
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
            SELECT json_extract(data, '$.feature') as feature, MAX(id) as max_id
            FROM events WHERE event_type = 'feature-toggles:update'
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
        AND LOWER(json_extract(data, '$.user.displayName')) IN ('tts', 'sfx', 'emote')
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

    base_where = f"{type_clause} AND json_extract(data, '$.sentiment') IS NOT NULL"

    hourly = conn.execute(f"""
        SELECT strftime('%H', timestamp_local) as hour,
            strftime('%Y-%m-%dT%H:00:00Z', timestamp_local) as ts,
            AVG(CAST(json_extract(data, '$.sentiment') AS REAL)) as avg_sentiment,
            COUNT(*) as message_count
        FROM events
        WHERE {base_where}
            AND timestamp_local >= datetime('now', '-24 hours')
        GROUP BY hour ORDER BY hour
    """).fetchall()

    overall_row = conn.execute(f"""
        SELECT
            AVG(CAST(json_extract(data, '$.sentiment') AS REAL)) as avg,
            SUM(CASE WHEN CAST(json_extract(data, '$.sentiment') AS REAL) >= 0.05 THEN 1 ELSE 0 END) as positive,
            SUM(CASE WHEN CAST(json_extract(data, '$.sentiment') AS REAL) <= -0.05 THEN 1 ELSE 0 END) as negative,
            SUM(CASE WHEN CAST(json_extract(data, '$.sentiment') AS REAL) > -0.05 AND CAST(json_extract(data, '$.sentiment') AS REAL) < 0.05 THEN 1 ELSE 0 END) as neutral,
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
        SELECT json_extract(data, '$.target') as target,
            AVG(CAST(json_extract(data, '$.sentiment') AS REAL)) as avg_sentiment,
            COUNT(*) as message_count
        FROM events
        WHERE event_type = 'tts:update'
            AND json_extract(data, '$.sentiment') IS NOT NULL
            AND json_extract(data, '$.target') IS NOT NULL
            {since_clause}
        GROUP BY target ORDER BY avg_sentiment DESC
    """, params).fetchall()

    result["by_target"] = [{"target": r["target"], "avg_sentiment": round(r["avg_sentiment"], 4), "message_count": r["message_count"]} for r in by_target]
    return result
