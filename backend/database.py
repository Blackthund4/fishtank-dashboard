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

try:
    import orjson
    def fast_loads(s):
        return orjson.loads(s)
    def fast_dumps(obj, **kwargs):
        return orjson.dumps(obj, default=str).decode()
except ImportError:
    def fast_loads(s):
        return json.loads(s)
    def fast_dumps(obj, **kwargs):
        return json.dumps(obj, ensure_ascii=False, default=str)

DB_PATH = Path(os.environ.get("FISHTANK_DB_PATH", Path(__file__).parent / "fishtank.db"))

FISHTOY_TYPE = "fishtoy:used"

_local = threading.local()

# Module-level read-only flag. When True, every subsequently-created
# thread-local connection gets PRAGMA query_only=1, which makes the SQLite
# engine refuse any INSERT/UPDATE/DELETE/CREATE/DROP on that connection.
# Flipped via enable_readonly() from the API process after init_db.
# The ingestion process never touches this flag and stays writable.
READ_ONLY = False


def _get_conn():
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(str(DB_PATH))
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn.execute("PRAGMA temp_store=MEMORY")
        _local.conn.execute("PRAGMA cache_size=-16384")  # 16 MB page cache per connection
        _local.conn.execute("PRAGMA busy_timeout=5000")  # Wait up to 5s for write lock instead of failing immediately
        if READ_ONLY:
            _local.conn.execute("PRAGMA query_only=1")
    return _local.conn


def enable_readonly():
    """Put this process into read-only SQLite mode (defense-in-depth).

    Sets the module-level READ_ONLY flag so every subsequent thread-local
    connection is opened with ``PRAGMA query_only=1``. Also retroactively
    applies the pragma to the current thread's connection if one already
    exists (e.g. the connection created by init_db during process startup).

    Call this in the API process after init_db and before serving traffic.
    Ingestion must never call this — it owns all writes.
    """
    global READ_ONLY
    READ_ONLY = True
    if hasattr(_local, "conn") and _local.conn is not None:
        _local.conn.execute("PRAGMA query_only=1")


def disable_readonly():
    """Clear read-only mode. Used by tests to reset state between runs."""
    global READ_ONLY
    READ_ONLY = False
    if hasattr(_local, "conn") and _local.conn is not None:
        _local.conn.execute("PRAGMA query_only=0")


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

        CREATE TABLE IF NOT EXISTS _notify (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );

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
        "chat_role TEXT",
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
        CREATE INDEX IF NOT EXISTS idx_events_display_name_nocase
            ON events(display_name COLLATE NOCASE) WHERE display_name IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_events_sc_delete
            ON events(event_type, event_id) WHERE event_type = 'super-chat:delete';
        CREATE INDEX IF NOT EXISTS idx_events_sentiment_ts
            ON events(event_type, timestamp_local, sentiment)
            WHERE sentiment IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_tts_sentiment_target
            ON events(event_type, target, sentiment)
            WHERE event_type = 'tts:update' AND sentiment IS NOT NULL AND target IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_fishtoys_target_id
            ON events(event_type, target, id)
            WHERE event_type = 'fishtoy:used' AND target IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_fishtoys_item_id
            ON events(event_type, item_id, id)
            WHERE event_type = 'fishtoy:used' AND item_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_fishtoys_target_sender
            ON events(event_type, target, display_name)
            WHERE event_type = 'fishtoy:used' AND target IS NOT NULL AND display_name IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_fishtoys_target_item
            ON events(event_type, target, item_id)
            WHERE event_type = 'fishtoy:used' AND target IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_events_chat_role
            ON events(chat_role, id)
            WHERE chat_role IS NOT NULL;
    """)
    conn.commit()


def backfill_extracted_columns(batch_size=1000):
    """Backfill extracted columns for existing rows. Processes in small batches to limit memory.

    Uses Python-side extraction instead of json_extract in UPDATE to avoid SQLite
    parsing all JSON blobs in C (which OOM-killed the 1GB container at 5k batch size).
    """
    conn = _get_conn()
    # Check if backfill is needed: look for events with NULL extracted columns
    # First check catches initial backfill (cost never populated),
    # second catches re-backfill after adding new columns (room never populated on TTS)
    sample = conn.execute("""
        SELECT id FROM events
        WHERE (event_type = 'tts:update' AND cost IS NULL)
           OR (event_type = 'tts:update' AND room IS NULL)
           OR (event_type = 'chat:message' AND chat_role IS NULL AND json_extract(data, '$.metadata.isAdmin') = 1)
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
                data = fast_loads(row["data"]) if isinstance(row["data"], str) else row["data"]
            except (json.JSONDecodeError, TypeError):
                data = {}
            ext = _extract_columns(row["event_type"], data)
            conn.execute(_UPDATE_SQL, tuple(ext[c] for c in _EXTRACTED_COLS) + (row["id"],))

        conn.commit()
        total += len(rows)
        if total % 10000 == 0:
            print(f"[...] Backfill progress: {total} events")

    return total


def backfill_poll_vote_costs():
    """One-time backfill: set cost = sum(scores) for poll:vote events missing cost."""
    conn = _get_conn()
    rows = conn.execute("""
        SELECT id, data FROM events
        WHERE event_type = 'poll:vote' AND cost IS NULL
    """).fetchall()
    if not rows:
        return 0
    for row in rows:
        try:
            data = fast_loads(row["data"]) if isinstance(row["data"], str) else row["data"]
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, list):
            total = sum(v.get("score", 0) for v in data if isinstance(v, dict))
            conn.execute("UPDATE events SET cost = ? WHERE id = ?", (total, row["id"]))
    conn.commit()
    print(f"[OK] Backfilled cost for {len(rows)} poll:vote events")
    return len(rows)


_EXTRACTED_COLS = ("sentiment", "cost", "display_name", "target", "room", "metadata", "item_id", "feature", "chat_role")
_EXTRACTED_NONE = {k: None for k in _EXTRACTED_COLS}
_INSERT_SQL = (
    "INSERT INTO events (event_type, event_id, timestamp_server, timestamp_local, data, "
    + ", ".join(_EXTRACTED_COLS)
    + ") VALUES (" + ", ".join("?" for _ in range(5 + len(_EXTRACTED_COLS))) + ")"
)
_UPDATE_SQL = "UPDATE events SET " + ", ".join(f"{c} = ?" for c in _EXTRACTED_COLS) + " WHERE id = ?"


def _extract_columns(event_type, data):
    """Extract denormalized columns from event data dict. Returns dict keyed by column name."""
    if not isinstance(data, dict):
        return dict(_EXTRACTED_NONE)
    cost_raw = data.get("cost")
    display_name = data.get("displayName")
    if not display_name and isinstance(data.get("user"), dict):
        display_name = data["user"].get("displayName")
    metadata_val = data.get("metadata")
    if metadata_val in (None, "null", ""):
        metadata_val = None
    elif not isinstance(metadata_val, str):
        metadata_val = fast_dumps(metadata_val)
    item_id_val = data.get("itemId")
    if item_id_val is not None:
        item_id_val = str(item_id_val)
    # For poll:vote events, cost = total vote score (sum of all option scores)
    # This enables SUM(cost) in spend queries without json_extract at query time
    cost = int(cost_raw) if cost_raw is not None else None
    if event_type == "poll:vote" and isinstance(data, list):
        cost = sum(v.get("score", 0) for v in data if isinstance(v, dict))

    # Extract chat role from metadata flags (priority order)
    chat_role = None
    if event_type == "chat:message":
        meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        if meta.get("isAdmin"):
            chat_role = "admin"
        elif meta.get("isMod"):
            chat_role = "mod"
        elif meta.get("isFish"):
            chat_role = "fish"
        elif meta.get("isGrandMarshall"):
            chat_role = "gm"
        elif meta.get("isEpic"):
            chat_role = "epic"

    return {
        "sentiment": data.get("sentiment"),
        "cost": cost,
        "display_name": display_name,
        "target": data.get("target"),
        "room": data.get("room"),
        "metadata": metadata_val,
        "item_id": item_id_val,
        "feature": data.get("feature"),
        "chat_role": chat_role,
    }


def store_event(event_type: str, data):
    conn = _get_conn()
    event_id = None
    timestamp_server = None

    if isinstance(data, dict):
        event_id = data.get("id")
        timestamp_server = data.get("timestamp") or data.get("createdAt")

    now = datetime.now(timezone.utc).isoformat()
    data_json = fast_dumps(data)
    ext = _extract_columns(event_type, data)

    cursor = conn.execute(
        _INSERT_SQL,
        (event_type, str(event_id) if event_id else None, timestamp_server, now, data_json,
         *(ext[c] for c in _EXTRACTED_COLS)),
    )
    conn.commit()
    return cursor.lastrowid


def get_events(event_type=None, limit=200, since_id=None, before_id=None,
               target=None, item_id=None, search=None, around_ts=None, role=None):
    conn = _get_conn()
    query = "SELECT id, event_type, event_id, timestamp_server, timestamp_local, data FROM events"
    conditions = []
    params = []

    if event_type:
        types = [t.strip() for t in event_type.split(",")]
        placeholders = ",".join("?" for _ in types)
        conditions.append(f"event_type IN ({placeholders})")
        params.extend(types)

    # Anchor to a point in time (find nearest event id for the given timestamp)
    if around_ts is not None and before_id is None and since_id is None:
        anchor_query = "SELECT id FROM events"
        anchor_conditions = []
        anchor_params = []
        if event_type:
            anchor_conditions.append(f"event_type IN ({placeholders})")
            anchor_params.extend(types)
        anchor_conditions.append("timestamp_local <= ?")
        anchor_params.append(around_ts)
        anchor_query += " WHERE " + " AND ".join(anchor_conditions)
        anchor_query += " ORDER BY timestamp_local DESC LIMIT 1"
        anchor_row = conn.execute(anchor_query, anchor_params).fetchone()
        if anchor_row:
            conditions.append("id <= ?")
            params.append(anchor_row["id"])

    if since_id is not None:
        conditions.append("id > ?")
        params.append(since_id)

    if before_id is not None:
        conditions.append("id < ?")
        params.append(before_id)

    if target:
        conditions.append("target = ?")
        params.append(target)

    if item_id:
        conditions.append("item_id = ?")
        params.append(str(item_id))

    if search:
        conditions.append("(metadata LIKE ? OR display_name LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    if role:
        # Uses the extracted chat_role column with partial index — instant seek
        valid_roles = {"admin", "mod", "fish", "gm", "epic"}
        if role in valid_roles:
            conditions.append("chat_role = ?")
            params.append(role)

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
            "data": fast_loads(row["data"]),
        }
        for row in rows
    ]


def get_event_by_id(event_id):
    """Fetch a single event by its primary key. Returns dict or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, event_type, event_id, timestamp_server, timestamp_local, data FROM events WHERE id = ?",
        (event_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "event_type": row["event_type"],
        "event_id": row["event_id"],
        "timestamp_server": row["timestamp_server"],
        "timestamp_local": row["timestamp_local"],
        "data": fast_loads(row["data"]),
    }


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
        FROM events WHERE event_type = ?
    """ + since_clause, [FISHTOY_TYPE] + since_params).fetchone()

    # Scoped to cost-bearing event types only — per-type for index usage
    all_spend_total = 0
    for _et in ('tts:update', 'sfx:update', 'super-chat:new', FISHTOY_TYPE):
        row = conn.execute(
            "SELECT COALESCE(SUM(cost), 0) as total FROM events WHERE event_type = ?" + since_clause,
            [_et] + since_params
        ).fetchone()
        all_spend_total += row["total"]

    # Poll token spend: for each completed poll, get the last poll:vote's cost (= total scores)
    # Uses the extracted cost column on poll:vote events (backfilled by backfill_poll_vote_costs)
    poll_tokens_rows = conn.execute("""
        SELECT
            (SELECT COALESCE(cost, 0)
             FROM events WHERE event_type = 'poll:vote' AND id < pe.id
             ORDER BY id DESC LIMIT 1
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
            SELECT cost FROM events WHERE event_type = 'poll:vote' AND id > ?
            ORDER BY id DESC LIMIT 1
        """, (active_start["id"],)).fetchone()
        if current_vote and current_vote["cost"]:
            current_total = current_vote["cost"]
            baseline_total = 0
            if since:
                baseline_vote = conn.execute("""
                    SELECT cost FROM events WHERE event_type = 'poll:vote' AND id > ?
                    AND timestamp_local < ?
                    ORDER BY id DESC LIMIT 1
                """, (active_start["id"], since)).fetchone()
                if baseline_vote and baseline_vote["cost"]:
                    baseline_total = baseline_vote["cost"]
            poll_tokens += current_total - baseline_total

    # Superchat token spend
    superchat_stats = conn.execute("""
        SELECT COALESCE(SUM(cost), 0) as total
        FROM events WHERE event_type = 'super-chat:new'
    """ + since_clause, since_params).fetchone()
    superchat_tokens = superchat_stats["total"] if superchat_stats else 0

    top_targets = conn.execute("""
        SELECT target, COUNT(*) as count
        FROM events WHERE event_type = ?
            AND target IS NOT NULL
    """ + since_clause + " GROUP BY target ORDER BY count DESC LIMIT 10", [FISHTOY_TYPE] + since_params).fetchall()

    # Per-type leaderboards and UNION ALL top_senders are expensive (json_extract
    # GROUP BY across 600k+ rows). Only compute when `since` is set (24h sidebar).
    # All-time stats call uses the cheap fishtoy-only top_senders below.
    if since:
        top_senders = conn.execute("""
            SELECT sender, SUM(total_spend) as spend, SUM(total_count) as count FROM (
                SELECT display_name as sender,
                    COALESCE(SUM(cost), 0) as total_spend,
                    COUNT(*) as total_count
                FROM events WHERE event_type = ?
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
        """, (FISHTOY_TYPE, since, since, since)).fetchall()

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
            FROM events WHERE event_type = ?
                AND display_name IS NOT NULL
        """ + since_clause + " GROUP BY name ORDER BY spend DESC LIMIT 5", [FISHTOY_TYPE] + since_params).fetchall()
    else:
        top_senders_rows = conn.execute("""
            SELECT display_name as sender, COUNT(*) as count,
                COALESCE(SUM(cost), 0) as spend
            FROM events WHERE event_type = ?
                AND display_name IS NOT NULL
            GROUP BY sender ORDER BY count DESC LIMIT 10
        """, (FISHTOY_TYPE,)).fetchall()
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
        "total_spend": all_spend_total + poll_tokens,
        "poll_tokens": poll_tokens,
        "superchat_tokens": superchat_tokens,
        "top_targets": [{"name": r["target"], "count": r["count"]} for r in top_targets],
        "top_senders": [{"name": r["sender"], "count": r["count"], "spend": r["spend"]} for r in top_senders],
        "top_tts_senders": [{"name": r["name"], "count": r["count"], "spend": r["spend"]} for r in top_tts_senders],
        "top_sfx_senders": [{"name": r["name"], "count": r["count"], "spend": r["spend"]} for r in top_sfx_senders],
        "top_chat_senders": [{"name": r["name"], "count": r["count"]} for r in top_chat_senders],
        "top_fishtoy_senders": [{"name": r["name"], "count": r["count"], "spend": r["spend"]} for r in top_fishtoy_senders],
    }


def get_fishtoys(target=None, item_id=None, search=None, limit=200, offset=0, before_id=None):
    """Get fishtoy events with optional filters."""
    conn = _get_conn()
    query = "SELECT id, event_type, event_id, timestamp_server, timestamp_local, data FROM events"
    conditions = ["event_type = ?"]
    params = [FISHTOY_TYPE]

    if target:
        conditions.append("target = ?")
        params.append(target)

    if item_id:
        conditions.append("item_id = ?")
        params.append(str(item_id))

    if search:
        conditions.append("(metadata LIKE ? OR display_name LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    if before_id is not None:
        conditions.append("id < ?")
        params.append(before_id)

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
            "data": fast_loads(row["data"]),
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
        WHERE event_type = ?
            AND target IS NOT NULL
        GROUP BY target
        ORDER BY count DESC
    """, (FISHTOY_TYPE,)).fetchall()
    return [{"target": r["target"], "count": r["count"], "spend": r["spend"]} for r in rows]


def get_target_stats(target):
    """Get detailed stats for a specific fishtoy target from full DB history.
    Single query fetches items, senders, and totals via UNION ALL."""
    conn = _get_conn()

    rows = conn.execute("""
        SELECT 'item' as qtype, item_id as key, COUNT(*) as count,
            COALESCE(SUM(cost), 0) as spend
        FROM events
        WHERE event_type = ? AND target = ?
        GROUP BY item_id
        UNION ALL
        SELECT 'sender' as qtype, display_name as key, COUNT(*) as count, 0 as spend
        FROM events
        WHERE event_type = ? AND target = ? AND display_name IS NOT NULL
        GROUP BY display_name
    """, (FISHTOY_TYPE, target, FISHTOY_TYPE, target)).fetchall()

    items = []
    senders = []
    total = 0
    total_spend = 0
    with_meta = 0
    for r in rows:
        if r["qtype"] == "item":
            items.append({"id": r["key"], "count": r["count"], "spend": r["spend"]})
            total += r["count"]
            total_spend += r["spend"]
        else:
            senders.append({"name": r["key"], "count": r["count"]})

    # Metadata count — lightweight single scan on the target index
    meta_row = conn.execute("""
        SELECT SUM(CASE WHEN metadata IS NOT NULL THEN 1 ELSE 0 END) as with_meta
        FROM events WHERE event_type = ? AND target = ?
    """, (FISHTOY_TYPE, target)).fetchone()
    with_meta = meta_row["with_meta"] or 0

    items.sort(key=lambda x: x["count"], reverse=True)
    senders.sort(key=lambda x: x["count"], reverse=True)

    return {
        "total": total,
        "totalSpend": total_spend,
        "withMeta": with_meta,
        "topItems": items,
        "topSenders": senders[:10],
    }


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


def notify_new_event(event_id, event_type):
    """Insert a notification row for the API server's WS fan-out poller."""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO _notify (event_id, event_type) VALUES (?, ?)",
        (event_id, event_type),
    )
    conn.commit()


def poll_notify(last_seen_id=0):
    """Poll for new notify rows since last_seen_id. Returns list of dicts."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, event_id, event_type FROM _notify WHERE id > ? ORDER BY id",
        (last_seen_id,),
    ).fetchall()
    return [{"id": r["id"], "event_id": r["event_id"], "event_type": r["event_type"]} for r in rows]


def prune_notify():
    """Delete _notify rows older than 60 seconds."""
    conn = _get_conn()
    conn.execute(
        "DELETE FROM _notify WHERE created_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-60 seconds')"
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


def prune_chat_events(retention_days=30):
    """Delete ordinary chat messages older than retention_days.

    Retains admin, mod, and fish messages forever (production value).
    Prunes regular, epic, and grand marshall messages to keep the DB
    manageable under Global chat volume (~240k msgs/day).

    Uses the extracted `chat_role` column — no json_extract needed.
    Returns count of deleted rows.
    """
    conn = _get_conn()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()

    count = conn.execute(
        """SELECT COUNT(*) FROM events
           WHERE event_type = 'chat:message'
             AND timestamp_local < ?
             AND (chat_role IS NULL OR chat_role NOT IN ('admin', 'mod', 'fish'))""",
        (cutoff,),
    ).fetchone()[0]
    if count == 0:
        return 0

    conn.execute(
        """DELETE FROM events
           WHERE event_type = 'chat:message'
             AND timestamp_local < ?
             AND (chat_role IS NULL OR chat_role NOT IN ('admin', 'mod', 'fish'))""",
        (cutoff,),
    )
    conn.commit()
    return count


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
# CHART DATA
# ============================================================


def _range_to_since_and_granularity(range_str, config, anchor=None):
    """Convert a range string to (since_iso, until_iso, granularity) using a config map.
    When anchor is provided, the window ends at anchor instead of now."""
    delta, granularity = config.get(range_str, config.get('24h'))
    if anchor:
        ref = datetime.fromisoformat(anchor)
        if delta:
            since = (ref - delta).isoformat()
        else:
            # 'all' with anchor: 30-day window ending at anchor
            since = (ref - timedelta(days=30)).isoformat()
        return since, anchor, granularity
    else:
        now = datetime.now(timezone.utc)
        since = (now - delta).isoformat() if delta else None
        return since, None, granularity


def _apply_bucket(ts_str, granularity):
    """Apply time bucketing to an ISO timestamp string (Python-side equivalent of _time_bucket_expr)."""
    # ts_str like "2026-04-05T08:05:19.924729+00:00" or "2026-04-05 08:05:19..."
    ts = ts_str.replace('T', ' ')[:19]  # "2026-04-05 08:05:19"
    if granularity == '5min':
        m = int(ts[14:16])
        return ts[:14] + f"{(m // 5) * 5:02d}:00"
    if granularity == '15min':
        m = int(ts[14:16])
        return ts[:14] + f"{(m // 15) * 15:02d}:00"
    if granularity == 'hourly':
        return ts[:13] + ":00:00"
    return ts[:10]  # daily


def _time_bucket_expr(granularity, col='timestamp_local'):
    """Return SQL expression for time bucketing."""
    if granularity == '5min':
        return (
            f"strftime('%Y-%m-%d %H:', {col}) || "
            f"printf('%02d', (CAST(strftime('%M', {col}) AS INTEGER) / 5) * 5) || ':00'"
        )
    if granularity == '15min':
        return (
            f"strftime('%Y-%m-%d %H:', {col}) || "
            f"printf('%02d', (CAST(strftime('%M', {col}) AS INTEGER) / 15) * 15) || ':00'"
        )
    if granularity == 'hourly':
        return f"strftime('%Y-%m-%d %H:00:00', {col})"
    return f"strftime('%Y-%m-%d', {col})"


def get_stock_history_chart(range_str='24h', anchor=None):
    """Stock price history with automatic downsampling for chart display."""
    conn = _get_conn()
    config = {
        '30m': (timedelta(minutes=30), 'raw'),
        '1h':  (timedelta(hours=1),  'raw'),
        '2h':  (timedelta(hours=2),  '5min'),
        '6h':  (timedelta(hours=6),  '5min'),
        '12h': (timedelta(hours=12), '15min'),
        '24h': (timedelta(hours=24), '15min'),
        '3d':  (timedelta(days=3),   'hourly'),
        '7d':  (timedelta(days=7),   'hourly'),
        'all': (None,                 'daily'),
    }
    since, until, granularity = _range_to_since_and_granularity(range_str, config, anchor)
    clauses = []
    params = []
    if since:
        clauses.append("timestamp >= ?")
        params.append(since)
    if until:
        clauses.append("timestamp < ?")
        params.append(until)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    if granularity == 'raw':
        sql = f"SELECT ticker, timestamp AS ts, price FROM stock_history {where} ORDER BY timestamp ASC"
    else:
        bucket = _time_bucket_expr(granularity, 'timestamp')
        sql = f"""
            SELECT ticker, {bucket} AS ts, CAST(ROUND(AVG(price)) AS INTEGER) AS price
            FROM stock_history {where}
            GROUP BY ticker, ts ORDER BY ts ASC
        """

    rows = conn.execute(sql, params).fetchall()
    result = {}
    for row in rows:
        t = row['ticker']
        if t not in result:
            result[t] = []
        result[t].append({'ts': row['ts'], 'price': row['price']})
    return result


def get_stock_deltas(range_str):
    """Get earliest recorded price per ticker within a time range for delta calculation."""
    conn = _get_conn()
    now = datetime.now(timezone.utc)
    windows = {'3h': 3, '12h': 12, '3d': 72}
    hours = windows.get(range_str)
    if not hours:
        return {}
    since = (now - timedelta(hours=hours)).isoformat()
    rows = conn.execute("""
        SELECT sh.ticker, sh.price
        FROM stock_history sh
        INNER JOIN (
            SELECT ticker, MIN(timestamp) AS min_ts
            FROM stock_history WHERE timestamp >= ?
            GROUP BY ticker
        ) m ON sh.ticker = m.ticker AND sh.timestamp = m.min_ts
    """, [since]).fetchall()
    return {row['ticker']: row['price'] for row in rows}


def get_stock_sparklines(range_str='today'):
    """Get price arrays per ticker for sparkline rendering. Returns {ticker: [price, ...]}."""
    conn = _get_conn()
    now = datetime.now(timezone.utc)
    config = {
        '1h':  (now - timedelta(hours=1),  'raw'),
        '3h':  (now - timedelta(hours=3),  '5min'),
        '12h': (now - timedelta(hours=12), '15min'),
        'today': (now - timedelta(hours=24), '15min'),
        '3d':  (now - timedelta(days=3),   'hourly'),
        '1w':  (now - timedelta(days=7),   'hourly'),
        'ipo': (None,                       'daily'),
    }
    since_dt, granularity = config.get(range_str, config['today'])
    since = since_dt.isoformat() if since_dt else None
    where = "WHERE timestamp >= ?" if since else ""
    params = [since] if since else []

    if granularity == 'raw':
        sql = f"SELECT ticker, price FROM stock_history {where} ORDER BY timestamp ASC"
    else:
        bucket = _time_bucket_expr(granularity, 'timestamp')
        sql = f"""
            SELECT ticker, CAST(ROUND(AVG(price)) AS INTEGER) AS price
            FROM stock_history {where}
            GROUP BY ticker, {bucket} ORDER BY {bucket} ASC
        """

    rows = conn.execute(sql, params).fetchall()
    result = {}
    for row in rows:
        t = row['ticker']
        if t not in result:
            result[t] = []
        result[t].append(row['price'])
    return result


def get_spend_trends(range_str='24h', anchor=None):
    """Token spend over time bucketed by event type, using extracted cost column."""
    conn = _get_conn()
    config = {
        '30m': (timedelta(minutes=30), '5min'),
        '1h':  (timedelta(hours=1),  '5min'),
        '2h':  (timedelta(hours=2),  '5min'),
        '6h':  (timedelta(hours=6),  'hourly'),
        '12h': (timedelta(hours=12), 'hourly'),
        '24h': (timedelta(hours=24), 'hourly'),
        '3d':  (timedelta(days=3),   'hourly'),
        '7d':  (timedelta(days=7),   'daily'),
        'all': (None,                 'daily'),
    }
    since, until, granularity = _range_to_since_and_granularity(range_str, config, anchor)
    since_clause = "AND timestamp_local >= ?" if since else ""
    until_clause = "AND timestamp_local < ?" if until else ""
    window_clause = f"{since_clause} {until_clause}"
    since_params = [since] if since else []
    until_params = [until] if until else []
    window_params = since_params + until_params
    bucket = _time_bucket_expr(granularity)

    # Per-type queries for proper index usage on idx_events_type_ts_local
    rows = conn.execute(f"""
        SELECT {bucket} AS ts, event_type,
            COALESCE(SUM(cost), 0) AS spend, COUNT(*) AS count
        FROM events WHERE event_type = 'tts:update' {window_clause}
        GROUP BY ts
        UNION ALL
        SELECT {bucket} AS ts, event_type,
            COALESCE(SUM(cost), 0) AS spend, COUNT(*) AS count
        FROM events WHERE event_type = 'sfx:update' {window_clause}
        GROUP BY ts
        UNION ALL
        SELECT {bucket} AS ts, event_type,
            COALESCE(SUM(cost), 0) AS spend, COUNT(*) AS count
        FROM events WHERE event_type = 'fishtoy:used' {window_clause}
        GROUP BY ts
        UNION ALL
        SELECT {bucket} AS ts, event_type,
            COALESCE(SUM(cost), 0) AS spend, COUNT(*) AS count
        FROM events WHERE event_type = 'super-chat:new' {window_clause}
        GROUP BY ts
        ORDER BY ts ASC
    """, window_params * 4).fetchall()

    buckets = {}
    for row in rows:
        ts = row['ts']
        if ts not in buckets:
            buckets[ts] = {'ts': ts, 'tts': 0, 'sfx': 0, 'fishtoy': 0, 'poll': 0, 'superchat': 0}
        et = row['event_type']
        key = 'tts' if et == 'tts:update' else 'sfx' if et == 'sfx:update' else 'superchat' if et == 'super-chat:new' else 'fishtoy'
        buckets[ts][key] = row['spend']

    # Poll tokens: bucket vote deltas from poll:vote events
    # Each poll:vote has cost = cumulative total scores (extracted at insert time).
    # We compute deltas between consecutive votes, reset at poll:start boundaries.
    poll_vote_rows = conn.execute(f"""
        SELECT id, timestamp_local, cost FROM events
        WHERE event_type = 'poll:vote' AND cost IS NOT NULL
        {window_clause}
        ORDER BY id ASC
    """, window_params).fetchall()

    prev_total = 0
    # If we have a since filter, get the baseline (last vote before the window)
    if since:
        baseline = conn.execute("""
            SELECT cost FROM events
            WHERE event_type = 'poll:vote' AND cost IS NOT NULL AND timestamp_local < ?
            ORDER BY id DESC LIMIT 1
        """, [since]).fetchone()
        if baseline:
            prev_total = baseline["cost"]

    # Also reset prev_total at each poll:start boundary
    poll_starts = conn.execute(f"""
        SELECT id FROM events WHERE event_type = 'poll:start'
        {window_clause}
        ORDER BY id ASC
    """, window_params).fetchall()
    start_id_list = sorted(row["id"] for row in poll_starts)

    start_idx = 0
    for row in poll_vote_rows:
        current_total = row["cost"]
        # Reset baseline if a poll:start occurred before this vote
        while start_idx < len(start_id_list) and start_id_list[start_idx] < row["id"]:
            prev_total = 0
            start_idx += 1
        delta = max(0, current_total - prev_total)
        prev_total = current_total
        if delta > 0:
            ts = _apply_bucket(row["timestamp_local"], granularity)
            if ts not in buckets:
                buckets[ts] = {'ts': ts, 'tts': 0, 'sfx': 0, 'fishtoy': 0, 'poll': 0, 'superchat': 0}
            buckets[ts]['poll'] += delta

    return {'granularity': granularity, 'data': sorted(buckets.values(), key=lambda x: x['ts'])}


def get_chat_chart(range_str='24h', anchor=None):
    """Chat message volume over time + top chatters, using extracted display_name column."""
    conn = _get_conn()
    config = {
        '30m': (timedelta(minutes=30), '5min'),
        '1h':  (timedelta(hours=1),  '5min'),
        '2h':  (timedelta(hours=2),  '5min'),
        '6h':  (timedelta(hours=6),  'hourly'),
        '12h': (timedelta(hours=12), 'hourly'),
        '24h': (timedelta(hours=24), 'hourly'),
        '3d':  (timedelta(days=3),   'hourly'),
        '7d':  (timedelta(days=7),   'daily'),
        'all': (None,                 'daily'),
    }
    since, until, granularity = _range_to_since_and_granularity(range_str, config, anchor)
    since_clause = "AND timestamp_local >= ?" if since else ""
    until_clause = "AND timestamp_local < ?" if until else ""
    window_clause = f"{since_clause} {until_clause}"
    since_params = [since] if since else []
    until_params = [until] if until else []
    params = since_params + until_params
    bucket = _time_bucket_expr(granularity)

    volume_rows = conn.execute(f"""
        SELECT {bucket} AS ts, COUNT(*) AS count
        FROM events WHERE event_type = 'chat:message' {window_clause}
        GROUP BY ts ORDER BY ts ASC
    """, params).fetchall()

    top_rows = conn.execute(f"""
        SELECT display_name AS name, COUNT(*) AS count
        FROM events
        WHERE event_type = 'chat:message' AND display_name IS NOT NULL
        {window_clause}
        GROUP BY display_name ORDER BY count DESC LIMIT 10
    """, params).fetchall()

    return {
        'granularity': granularity,
        'data': [{'ts': r['ts'], 'count': r['count']} for r in volume_rows],
        'top_chatters': [{'name': r['name'], 'count': r['count']} for r in top_rows],
    }


# ============================================================
# TTS / SFX ANALYTICS
# ============================================================


def get_tts_sfx_analytics(since=None, until=None):
    """Aggregate analytics for TTS and SFX events."""
    conn = _get_conn()
    since_clause = ""
    until_clause = ""
    since_params = []
    until_params = []
    if since:
        since_clause = " AND timestamp_local >= ?"
        since_params = [since]
    if until:
        until_clause = " AND timestamp_local < ?"
        until_params = [until]
    window_clause = since_clause + until_clause
    window_params = since_params + until_params

    top_rooms = conn.execute("""
        SELECT room, SUM(count) as count FROM (
            SELECT room, COUNT(*) as count
            FROM events WHERE event_type = 'tts:update' AND room IS NOT NULL
        """ + window_clause + """ GROUP BY room
            UNION ALL
            SELECT room, COUNT(*) as count
            FROM events WHERE event_type = 'sfx:update' AND room IS NOT NULL
        """ + window_clause + """  GROUP BY room
        ) GROUP BY room ORDER BY count DESC LIMIT 10
    """, window_params + window_params).fetchall()

    top_tts_senders = conn.execute("""
        SELECT display_name as sender, COUNT(*) as count,
            COALESCE(SUM(cost), 0) as spend
        FROM events WHERE event_type = 'tts:update' AND display_name IS NOT NULL
    """ + window_clause + " GROUP BY sender ORDER BY spend DESC LIMIT 10", window_params).fetchall()

    top_sfx_senders = conn.execute("""
        SELECT display_name as sender, COUNT(*) as count,
            COALESCE(SUM(cost), 0) as spend
        FROM events WHERE event_type = 'sfx:update' AND display_name IS NOT NULL
    """ + window_clause + " GROUP BY sender ORDER BY spend DESC LIMIT 10", window_params).fetchall()

    hourly_clause = window_clause if (since or until) else " AND timestamp_local >= datetime('now', '-24 hours')"
    hourly_params = window_params if (since or until) else []
    hourly_raw = conn.execute("""
        SELECT strftime('%H', timestamp_local) as hour,
            strftime('%Y-%m-%dT%H:00:00Z', timestamp_local) as ts,
            COUNT(*) as count
        FROM events WHERE event_type = 'tts:update'
    """ + hourly_clause + """ GROUP BY hour
        UNION ALL
        SELECT strftime('%H', timestamp_local) as hour,
            strftime('%Y-%m-%dT%H:00:00Z', timestamp_local) as ts,
            COUNT(*) as count
        FROM events WHERE event_type = 'sfx:update'
    """ + hourly_clause + " GROUP BY hour", hourly_params + hourly_params).fetchall()
    hourly_map = {}
    for r in hourly_raw:
        h = r["hour"]
        if h not in hourly_map:
            hourly_map[h] = {"hour": h, "ts": r["ts"], "count": 0}
        hourly_map[h]["count"] += r["count"]
    hourly = sorted(hourly_map.values(), key=lambda x: x["hour"])

    return {
        "top_rooms": [{"room": r["room"], "count": r["count"]} for r in top_rooms],
        "top_tts_senders": [{"name": r["sender"], "count": r["count"], "spend": r["spend"]} for r in top_tts_senders],
        "top_sfx_senders": [{"name": r["sender"], "count": r["count"], "spend": r["spend"]} for r in top_sfx_senders],
        "hourly": hourly,
    }


# ============================================================
# CHAT ANALYTICS
# ============================================================


def get_chat_analytics(since=None, until=None):
    """Aggregate analytics for chat messages."""
    conn = _get_conn()
    since_clause = ""
    until_clause = ""
    since_params = []
    until_params = []
    if since:
        since_clause = " AND timestamp_local >= ?"
        since_params = [since]
    if until:
        until_clause = " AND timestamp_local < ?"
        until_params = [until]
    window_clause = since_clause + until_clause
    window_params = since_params + until_params

    total = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'chat:message'" + window_clause, window_params
    ).fetchone()[0]

    top_chatters = conn.execute("""
        SELECT display_name as name, COUNT(*) as count
        FROM events WHERE event_type = 'chat:message' AND display_name IS NOT NULL
    """ + window_clause + " GROUP BY name ORDER BY count DESC LIMIT 15", window_params).fetchall()

    hourly_clause = window_clause if (since or until) else " AND timestamp_local >= datetime('now', '-24 hours')"
    hourly_params = window_params if (since or until) else []
    hourly = conn.execute("""
        SELECT strftime('%H', timestamp_local) as hour,
            strftime('%Y-%m-%dT%H:00:00Z', timestamp_local) as ts,
            COUNT(*) as count
        FROM events WHERE event_type = 'chat:message'
    """ + hourly_clause + " GROUP BY hour ORDER BY hour", hourly_params).fetchall()

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
        "event_type = ?",
        "metadata IS NOT NULL",
    ]
    params = [FISHTOY_TYPE]

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
            "data": fast_loads(row["data"]),
        }
        for row in rows
    ]


def get_hidden_content_targets():
    """Get target counts for fishtoy events with metadata (hidden content) from full DB."""
    conn = _get_conn()
    rows = conn.execute("""
        SELECT target, COUNT(*) as count
        FROM events
        WHERE event_type = ? AND metadata IS NOT NULL
        GROUP BY target ORDER BY count DESC
    """, (FISHTOY_TYPE,)).fetchall()
    targets = [{"target": r["target"], "count": r["count"]} for r in rows if r["target"] is not None]
    total = sum(r["count"] for r in rows)
    return {"total": total, "targets": targets}


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
            "data": fast_loads(row["data"]),
            "deleted": bool(row["deleted"]),
        }
        for row in rows
    ]


def get_known_superchat_ids():
    """Return set of event_ids for recent super-chat:new events (7-day window for dedup)."""
    conn = _get_conn()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    rows = conn.execute(
        "SELECT event_id FROM events WHERE event_type = 'super-chat:new' AND event_id IS NOT NULL AND timestamp_local >= ?",
        (cutoff,)
    ).fetchall()
    return {r["event_id"] for r in rows}


# ============================================================
# POLLS
# ============================================================


def get_polls(limit=50):
    """Get poll events. Filters out poll:start that has a subsequent poll:stop."""
    conn = _get_conn()
    rows = conn.execute("""
        SELECT id, event_type, event_id, timestamp_server, timestamp_local, data
        FROM events WHERE event_type IN ('poll:start', 'poll:stop')
        ORDER BY id DESC LIMIT ?
    """, (limit,)).fetchall()
    # Collect poll:stop pids so we can filter out their matching poll:start
    stop_pids = set()
    seen_active_start = False
    results = []
    for row in rows:
        data = fast_loads(row["data"])
        pid = data.get("pid") or (data.get("poll") or {}).get("pid")
        evt = {
            "id": row["id"],
            "event_type": row["event_type"],
            "timestamp_local": row["timestamp_local"],
            "data": data,
        }
        if row["event_type"] == "poll:stop":
            if pid:
                stop_pids.add(pid)
            # Find the poll:start preceding this stop (need id + answers)
            start_row = conn.execute(
                "SELECT id, data FROM events WHERE event_type = 'poll:start' AND id < ? ORDER BY id DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            start_id = start_row["id"] if start_row else 0
            # Scoped: only votes between this poll's start and stop
            vote_row = conn.execute(
                "SELECT data FROM events WHERE event_type = 'poll:vote' AND id > ? AND id < ? ORDER BY id DESC LIMIT 1",
                (start_id, row["id"]),
            ).fetchone()
            if vote_row:
                vote_data = fast_loads(vote_row["data"])
                if isinstance(vote_data, list):
                    evt["data"]["votes"] = vote_data
                elif isinstance(vote_data, dict) and "value" in vote_data:
                    # New format: collect latest per option
                    # Source answers from poll:start, not poll:stop
                    answers = []
                    if start_row:
                        start_data = fast_loads(start_row["data"])
                        answers = (start_data.get("poll") or start_data).get("answers", [])
                    vote_rows = conn.execute(
                        "SELECT data FROM events WHERE event_type = 'poll:vote' AND id > ? AND id < ? ORDER BY id DESC",
                        (start_id, row["id"]),
                    ).fetchall()
                    latest_by_option = {}
                    for vr in vote_rows:
                        vd = fast_loads(vr["data"])
                        if isinstance(vd, dict) and "value" in vd:
                            opt = vd["value"]
                            if opt not in latest_by_option:
                                latest_by_option[opt] = vd
                    if latest_by_option:
                        if answers:
                            evt["data"]["votes"] = [
                                latest_by_option.get(a, {"value": a, "score": 0}) for a in answers
                            ]
                        else:
                            evt["data"]["votes"] = list(latest_by_option.values())
            elif start_row:
                # No vote events — fall back to initial scores from poll:start
                start_data = fast_loads(start_row["data"])
                scores = (start_data.get("poll") or start_data).get("scores", [])
                if scores:
                    evt["data"]["votes"] = scores
            results.append(evt)
        else:
            # poll:start — only include if no matching poll:stop and no newer active start
            if pid and pid not in stop_pids and not seen_active_start:
                results.append(evt)
                seen_active_start = True
    return results


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
            "data": fast_loads(row["data"]),
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
            "data": fast_loads(row["data"]),
        }
        for row in rows
    ]


# ============================================================
# USER SEARCH
# ============================================================


def search_user(username, limit=500, before_id=None):
    """Search across all event types for a specific username (case-insensitive).

    Single UNION ALL query — each branch uses idx_events_ext_sender_ts independently.
    Optional before_id for keyset pagination.
    """
    conn = _get_conn()
    before_clause = " AND id < ?" if before_id is not None else ""
    before_params = (before_id,) if before_id is not None else ()
    rows = conn.execute(f"""
        SELECT * FROM (
            SELECT event_type, id, timestamp_local, data FROM events
            WHERE event_type = 'chat:message' AND display_name = ? COLLATE NOCASE{before_clause}
            ORDER BY id DESC LIMIT ?
        )
        UNION ALL
        SELECT * FROM (
            SELECT event_type, id, timestamp_local, data FROM events
            WHERE event_type = 'tts:update' AND display_name = ? COLLATE NOCASE{before_clause}
            ORDER BY id DESC LIMIT ?
        )
        UNION ALL
        SELECT * FROM (
            SELECT event_type, id, timestamp_local, data FROM events
            WHERE event_type = 'sfx:update' AND display_name = ? COLLATE NOCASE{before_clause}
            ORDER BY id DESC LIMIT ?
        )
        UNION ALL
        SELECT * FROM (
            SELECT event_type, id, timestamp_local, data FROM events
            WHERE event_type = ? AND display_name = ? COLLATE NOCASE{before_clause}
            ORDER BY id DESC LIMIT ?
        )
    """, (username, *before_params, limit,
          username, *before_params, limit,
          username, *before_params, limit,
          FISHTOY_TYPE, username, *before_params, limit)).fetchall()

    results = {"username": username, "chat": [], "tts": [], "sfx": [], "fishtoys": []}
    type_map = {"chat:message": "chat", "tts:update": "tts", "sfx:update": "sfx", FISHTOY_TYPE: "fishtoys"}
    for r in rows:
        key = type_map.get(r["event_type"])
        if key:
            results[key].append({"id": r["id"], "timestamp": r["timestamp_local"], "data": fast_loads(r["data"])})

    results["totals"] = {k: len(results[k]) for k in ("chat", "tts", "sfx", "fishtoys")}
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

    Single query fetches the most recent poll:start, its stop (if any),
    and the latest vote tallies — all in one round-trip.
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

    start_data = fast_loads(start["data"])
    poll_info = start_data.get("poll", start_data)

    # Fetch stop event and vote events after this poll:start
    rows = conn.execute("""
        SELECT event_type, timestamp_local, data FROM events
        WHERE event_type IN ('poll:stop', 'poll:vote') AND id > ?
        ORDER BY id DESC
    """, (start["id"],)).fetchall()

    stop = None
    vote_rows = []
    for row in rows:
        if row["event_type"] == "poll:stop" and not stop:
            stop = row
        elif row["event_type"] == "poll:vote":
            vote_rows.append(row)

    result = {
        "question": poll_info.get("question"),
        "answers": poll_info.get("answers", []),
        "pid": poll_info.get("pid", ""),
        "started_at": start["timestamp_local"],
        "active": stop is None,
    }

    # Reconstruct votes: handle both old format (list of all options) and
    # new format (individual dict per option, one event per changed option)
    if vote_rows:
        last_data = fast_loads(vote_rows[0]["data"])
        if isinstance(last_data, list):
            # Old format: single event with all options
            result["votes"] = last_data
        elif isinstance(last_data, dict) and "value" in last_data:
            # New format: individual vote events per option — collect latest per option
            latest_by_option = {}
            for vr in vote_rows:  # already DESC by id
                vd = fast_loads(vr["data"])
                if isinstance(vd, dict) and "value" in vd:
                    opt = vd["value"]
                    if opt not in latest_by_option:
                        latest_by_option[opt] = vd
            # Preserve answer order from poll:start
            answers = poll_info.get("answers", [])
            result["votes"] = [
                latest_by_option.get(a, {"value": a, "score": 0}) for a in answers
            ]

    if stop:
        stop_data = fast_loads(stop["data"])
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
        d = fast_loads(row["data"])
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
        WHERE event_type = ? AND event_id IS NOT NULL
        ORDER BY id DESC LIMIT ?
    """, (FISHTOY_TYPE, limit)).fetchall()
    return {row["event_id"] for row in rows}


# ============================================================
# PEAK HOURS
# ============================================================


def get_peak_hours():
    """Return combined hourly activity across all event types."""
    conn = _get_conn()

    # Per-type queries use idx_events_type_ts_local efficiently; merged in Python
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    hourly_raw = conn.execute("""
        SELECT strftime('%H', timestamp_local) as hour,
            strftime('%Y-%m-%dT%H:00:00Z', timestamp_local) as ts,
            event_type, COUNT(*) as count
        FROM events
        WHERE event_type = 'tts:update' AND timestamp_local >= ?
        GROUP BY hour
        UNION ALL
        SELECT strftime('%H', timestamp_local) as hour,
            strftime('%Y-%m-%dT%H:00:00Z', timestamp_local) as ts,
            event_type, COUNT(*) as count
        FROM events
        WHERE event_type = 'sfx:update' AND timestamp_local >= ?
        GROUP BY hour
        UNION ALL
        SELECT strftime('%H', timestamp_local) as hour,
            strftime('%Y-%m-%dT%H:00:00Z', timestamp_local) as ts,
            event_type, COUNT(*) as count
        FROM events
        WHERE event_type = 'fishtoy:used' AND timestamp_local >= ?
        GROUP BY hour
    """, (cutoff, cutoff, cutoff)).fetchall()
    # Merge per-type counts into combined hourly buckets
    hourly_map = {}
    for r in hourly_raw:
        h = r["hour"]
        if h not in hourly_map:
            hourly_map[h] = {"hour": h, "ts": r["ts"], "tts": 0, "sfx": 0, "fishtoys": 0, "total": 0}
        key = "tts" if r["event_type"] == "tts:update" else "sfx" if r["event_type"] == "sfx:update" else "fishtoys"
        hourly_map[h][key] = r["count"]
        hourly_map[h]["total"] += r["count"]
    hourly = sorted(hourly_map.values(), key=lambda x: x["hour"])

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


def _chat_mood_label(avg):
    """Map compound score to a mood label (tighter ranges for chat)."""
    if avg >= 0.15: return "Excited"
    if avg >= 0.03: return "Happy"
    if avg >= -0.03: return "Neutral"
    if avg >= -0.15: return "Grumpy"
    return "Hostile"


def _sentiment_base(conn, type_clause, since=None, until=None, label_fn=None):
    """Shared sentiment query logic for a given event type filter.
    Single query computes both hourly breakdown and overall stats."""
    since_clause = ""
    until_clause = ""
    params = []
    if since:
        since_clause = " AND timestamp_local >= ?"
        params.append(since)
    if until:
        until_clause = " AND timestamp_local < ?"
        params.append(until)

    base_where = f"{type_clause} AND sentiment IS NOT NULL"

    hourly_clause = (since_clause + until_clause) if (since or until) else " AND timestamp_local >= datetime('now', '-24 hours')"
    hourly_params = list(params) if (since or until) else []

    # Single query: hourly rows + one overall row via UNION ALL
    rows = conn.execute(f"""
        SELECT strftime('%H', timestamp_local) as hour,
            strftime('%Y-%m-%dT%H:00:00Z', timestamp_local) as ts,
            AVG(sentiment) as avg_sentiment,
            COUNT(*) as message_count,
            SUM(CASE WHEN sentiment >= 0.05 THEN 1 ELSE 0 END) as positive,
            SUM(CASE WHEN sentiment <= -0.05 THEN 1 ELSE 0 END) as negative,
            SUM(CASE WHEN sentiment > -0.05 AND sentiment < 0.05 THEN 1 ELSE 0 END) as neutral
        FROM events
        WHERE {base_where}
    """ + hourly_clause + " GROUP BY hour ORDER BY hour", hourly_params).fetchall()

    # Compute overall from hourly rows (avoids second table scan)
    total = sum(r["message_count"] for r in rows)
    if total > 0:
        weighted_sum = sum(r["avg_sentiment"] * r["message_count"] for r in rows)
        avg = round(weighted_sum / total, 4)
        pos = sum(r["positive"] for r in rows)
        neg = sum(r["negative"] for r in rows)
        neu = sum(r["neutral"] for r in rows)
    else:
        avg = 0
        pos = neg = neu = 0

    overall = {
        "avg": avg,
        "positive_pct": round(pos / total * 100, 1) if total > 0 else 0,
        "neutral_pct": round(neu / total * 100, 1) if total > 0 else 0,
        "negative_pct": round(neg / total * 100, 1) if total > 0 else 0,
    }

    return {
        "hourly": [{"hour": r["hour"], "ts": r["ts"], "avg_sentiment": round(r["avg_sentiment"], 4), "message_count": r["message_count"]} for r in rows],
        "overall": overall,
        "label": (label_fn or _mood_label)(avg),
    }


def get_chat_sentiment(since=None, until=None):
    """Sentiment analytics for chat messages."""
    conn = _get_conn()
    return _sentiment_base(conn, "event_type = 'chat:message'", since, until=until, label_fn=_chat_mood_label)


def get_tts_sentiment(since=None, until=None):
    """Sentiment analytics for TTS messages, including per-target breakdown."""
    conn = _get_conn()
    result = _sentiment_base(conn, "event_type = 'tts:update'", since, until=until)

    since_clause = ""
    until_clause = ""
    params = []
    if since:
        since_clause = " AND timestamp_local >= ?"
        params.append(since)
    if until:
        until_clause = " AND timestamp_local < ?"
        params.append(until)

    by_target = conn.execute(f"""
        SELECT target,
            AVG(sentiment) as avg_sentiment,
            COUNT(*) as message_count
        FROM events
        WHERE event_type = 'tts:update'
            AND sentiment IS NOT NULL
            AND target IS NOT NULL
            {since_clause}
            {until_clause}
        GROUP BY target ORDER BY avg_sentiment DESC
    """, params).fetchall()

    result["by_target"] = [{"target": r["target"], "avg_sentiment": round(r["avg_sentiment"], 4), "message_count": r["message_count"]} for r in by_target]
    return result
