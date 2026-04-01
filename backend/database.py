"""
Database module for persistent event storage.
Uses SQLite with WAL mode for concurrent read/write.
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "fishtank.db"

_local = threading.local()


def _get_conn():
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(str(DB_PATH))
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
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
        CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
        CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp_server);

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


def get_stats():
    conn = _get_conn()
    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    type_counts = conn.execute(
        "SELECT event_type, COUNT(*) as count FROM events GROUP BY event_type ORDER BY count DESC"
    ).fetchall()

    fishtoy_stats = conn.execute("""
        SELECT COUNT(*) as total,
            COALESCE(SUM(CASE WHEN json_extract(data, '$.cost') IS NOT NULL
                THEN CAST(json_extract(data, '$.cost') AS INTEGER) ELSE 0 END), 0) as total_cost
        FROM events WHERE event_type LIKE 'fishtoy%'
    """).fetchone()

    all_spend = conn.execute("""
        SELECT COALESCE(SUM(CASE WHEN json_extract(data, '$.cost') IS NOT NULL
            THEN CAST(json_extract(data, '$.cost') AS INTEGER) ELSE 0 END), 0) as total
        FROM events
    """).fetchone()

    top_targets = conn.execute("""
        SELECT json_extract(data, '$.target') as target, COUNT(*) as count
        FROM events WHERE event_type LIKE 'fishtoy%' AND target IS NOT NULL
        GROUP BY target ORDER BY count DESC LIMIT 10
    """).fetchall()

    top_senders = conn.execute("""
        SELECT json_extract(data, '$.displayName') as sender, COUNT(*) as count
        FROM events WHERE event_type LIKE 'fishtoy%' AND sender IS NOT NULL
        GROUP BY sender ORDER BY count DESC LIMIT 10
    """).fetchall()

    return {
        "total_events": total,
        "by_type": {r["event_type"]: r["count"] for r in type_counts},
        "fishtoys": {
            "total": fishtoy_stats["total"] if fishtoy_stats else 0,
            "total_cost": fishtoy_stats["total_cost"] if fishtoy_stats else 0,
        },
        "total_spend": all_spend["total"] if all_spend else 0,
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


def get_stock_history(ticker=None, limit=500):
    """Get stock price history, optionally filtered by ticker."""
    conn = _get_conn()
    if ticker:
        rows = conn.execute(
            "SELECT * FROM stock_history WHERE ticker = ? ORDER BY id DESC LIMIT ?",
            (ticker, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM stock_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


# ============================================================
# TTS / SFX ANALYTICS
# ============================================================


def get_tts_sfx_analytics():
    """Aggregate analytics for TTS and SFX events."""
    conn = _get_conn()

    top_rooms = conn.execute("""
        SELECT json_extract(data, '$.room') as room, COUNT(*) as count
        FROM events WHERE event_type IN ('tts:update', 'sfx:update') AND room IS NOT NULL
        GROUP BY room ORDER BY count DESC LIMIT 10
    """).fetchall()

    top_tts_senders = conn.execute("""
        SELECT json_extract(data, '$.displayName') as sender,
            COUNT(*) as count,
            COALESCE(SUM(CAST(json_extract(data, '$.cost') AS INTEGER)), 0) as spend
        FROM events WHERE event_type = 'tts:update' AND sender IS NOT NULL
        GROUP BY sender ORDER BY spend DESC LIMIT 10
    """).fetchall()

    top_sfx_senders = conn.execute("""
        SELECT json_extract(data, '$.displayName') as sender,
            COUNT(*) as count,
            COALESCE(SUM(CAST(json_extract(data, '$.cost') AS INTEGER)), 0) as spend
        FROM events WHERE event_type = 'sfx:update' AND sender IS NOT NULL
        GROUP BY sender ORDER BY spend DESC LIMIT 10
    """).fetchall()

    hourly = conn.execute("""
        SELECT strftime('%H', timestamp_local) as hour, COUNT(*) as count
        FROM events WHERE event_type IN ('tts:update', 'sfx:update')
        GROUP BY hour ORDER BY hour
    """).fetchall()

    return {
        "top_rooms": [{"room": r["room"], "count": r["count"]} for r in top_rooms],
        "top_tts_senders": [{"name": r["sender"], "count": r["count"], "spend": r["spend"]} for r in top_tts_senders],
        "top_sfx_senders": [{"name": r["sender"], "count": r["count"], "spend": r["spend"]} for r in top_sfx_senders],
        "hourly": [{"hour": r["hour"], "count": r["count"]} for r in hourly],
    }


# ============================================================
# CHAT ANALYTICS
# ============================================================


def get_chat_analytics():
    """Aggregate analytics for chat messages."""
    conn = _get_conn()

    total = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'chat:message'"
    ).fetchone()[0]

    top_chatters = conn.execute("""
        SELECT json_extract(data, '$.user.displayName') as name, COUNT(*) as count
        FROM events WHERE event_type = 'chat:message' AND name IS NOT NULL
        GROUP BY name ORDER BY count DESC LIMIT 15
    """).fetchall()

    hourly = conn.execute("""
        SELECT strftime('%H', timestamp_local) as hour, COUNT(*) as count
        FROM events WHERE event_type = 'chat:message'
        GROUP BY hour ORDER BY hour
    """).fetchall()

    return {
        "total": total,
        "top_chatters": [{"name": r["name"], "count": r["count"]} for r in top_chatters],
        "hourly": [{"hour": r["hour"], "count": r["count"]} for r in hourly],
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
    """Get poll events (start, stop, vote)."""
    conn = _get_conn()
    rows = conn.execute("""
        SELECT id, event_type, event_id, timestamp_server, timestamp_local, data
        FROM events WHERE event_type IN ('poll:start', 'poll:stop', 'poll:vote')
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
    """Search across all event types for a specific username (case-insensitive)."""
    conn = _get_conn()
    username_lower = username.lower()
    results = {
        "username": username,
        "chat": [],
        "tts": [],
        "sfx": [],
        "fishtoys": [],
    }

    # Chat messages
    rows = conn.execute("""
        SELECT id, timestamp_local, data FROM events
        WHERE event_type = 'chat:message'
        AND LOWER(json_extract(data, '$.user.displayName')) = ?
        ORDER BY id DESC LIMIT ?
    """, (username_lower, limit)).fetchall()
    results["chat"] = [{"id": r["id"], "timestamp": r["timestamp_local"], "data": json.loads(r["data"])} for r in rows]

    # TTS
    rows = conn.execute("""
        SELECT id, timestamp_local, data FROM events
        WHERE event_type = 'tts:update'
        AND LOWER(json_extract(data, '$.displayName')) = ?
        ORDER BY id DESC LIMIT ?
    """, (username_lower, limit)).fetchall()
    results["tts"] = [{"id": r["id"], "timestamp": r["timestamp_local"], "data": json.loads(r["data"])} for r in rows]

    # SFX
    rows = conn.execute("""
        SELECT id, timestamp_local, data FROM events
        WHERE event_type = 'sfx:update'
        AND LOWER(json_extract(data, '$.displayName')) = ?
        ORDER BY id DESC LIMIT ?
    """, (username_lower, limit)).fetchall()
    results["sfx"] = [{"id": r["id"], "timestamp": r["timestamp_local"], "data": json.loads(r["data"])} for r in rows]

    # Fishtoys
    rows = conn.execute("""
        SELECT id, timestamp_local, data FROM events
        WHERE event_type LIKE 'fishtoy%'
        AND LOWER(json_extract(data, '$.displayName')) = ?
        ORDER BY id DESC LIMIT ?
    """, (username_lower, limit)).fetchall()
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
    """Return distinct displayNames matching a prefix (case-insensitive)."""
    conn = _get_conn()
    prefix_lower = prefix.lower() + "%"

    # Search across chat and TTS/SFX senders
    rows = conn.execute("""
        SELECT DISTINCT name FROM (
            SELECT json_extract(data, '$.user.displayName') as name
            FROM events WHERE event_type = 'chat:message'
            AND LOWER(name) LIKE ?
            UNION
            SELECT json_extract(data, '$.displayName') as name
            FROM events WHERE event_type IN ('tts:update', 'sfx:update')
            AND LOWER(name) LIKE ?
            UNION
            SELECT json_extract(data, '$.displayName') as name
            FROM events WHERE event_type LIKE 'fishtoy%'
            AND LOWER(name) LIKE ?
        ) WHERE name IS NOT NULL
        LIMIT ?
    """, (prefix_lower, prefix_lower, prefix_lower, limit)).fetchall()
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
