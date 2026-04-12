"""
Fishtank Ingestion Process

Headless process that captures fishtank.live events via Socket.IO and REST
polling, stores them in SQLite, and signals the API server via the _notify
table and _shared_state.json.

This is the sole writer to the database and sole owner of fishtank.live auth.

Usage:
    Create backend/.env with FISHTANK_EMAIL and FISHTANK_PASSWORD
    python ingest.py
"""

import json
import logging
import os
import time as _time
import types
from datetime import datetime, timezone
from threading import Thread, Event, Lock
from pathlib import Path

import requests as http_requests
from fishclient import FishClient

import database
import shared_state
from database import fast_loads, fast_dumps
from auth import AuthManager

_sentiment_analyzer = None


def _get_analyzer():
    global _sentiment_analyzer
    if _sentiment_analyzer is None:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        _sentiment_analyzer = SentimentIntensityAnalyzer()
    return _sentiment_analyzer

# ============================================================
# CONFIG
# ============================================================

auth = AuthManager()

EVENTS = [
    # Fishtoys are polled via REST (/v1/items/recent), not socket events
    # Chat
    "chat:message",
    "chat:room",
    # TTS / SFX (only :update, not :queued, to avoid duplicate logging)
    "tts:update",
    "tts:price",
    "sfx:update",
    "sfx:price",
    # Polls
    "poll:start",
    "poll:stop",
    "poll:vote",
    # Notifications / Director messages
    "notification:global",
    "announcement",
    # Stocks (change events, not periodic prices)
    "stock:update",
    "stock:new",
    "stock:remove",
    "stock:split",
    # System
    "happening",
    "feature-toggles:update",
    # Presence
    "chat:presence",
    # Superchat (pinned messages)
    "super-chat:new",
    "super-chat:delete",
]

FISHTOY_POLL_INTERVAL = 5  # seconds

# ---- Cached catalog data (loaded on startup) ----
_catalog_lock = Lock()
_item_catalog = {}  # itemId -> {name, description, icon, type, ...}
_contestants = []   # [{id, name, color, photo, ...}]
_room_map = {}      # room code -> room name (e.g. "hwdn-5" -> "Hallway")
_stocks = []        # [{tickerSymbol, currentPrice, ...}]
CAPTURE_TYPES = {"FISHTOY", "BIGTOY"}

# ============================================================
# DATABASE BACKUP
# ============================================================

BACKUP_INTERVAL = 21600  # 6 hours
WAL_CHECKPOINT_INTERVAL = 3600  # 1 hour
_last_backup = None

NOTIFY_PRUNE_INTERVAL = 30  # seconds


def db_backup_poller():
    """Periodically back up the SQLite database and checkpoint the WAL."""
    global _last_backup
    import sqlite3

    # First backup after 5 minutes (don't wait 6 hours)
    last_notify_prune = _time.time()
    _poller_stop.wait(300)

    last_checkpoint = _time.time()

    while not _poller_stop.is_set():
        db_path = database.DB_PATH
        if str(db_path) == ":memory:" or not Path(db_path).exists():
            _poller_stop.wait(WAL_CHECKPOINT_INTERVAL)
            continue

        # WAL checkpoint every hour -- prevents WAL from ballooning under
        # continuous write load with many concurrent readers
        now = _time.time()
        if now - last_checkpoint >= WAL_CHECKPOINT_INTERVAL:
            try:
                conn = sqlite3.connect(str(db_path))
                busy, log, checkpointed = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                conn.close()
                if log > 0:
                    print(f"[OK] WAL checkpoint (PASSIVE): {checkpointed}/{log} pages checkpointed")
                last_checkpoint = now
            except Exception as e:
                print(f"[!] WAL checkpoint failed: {e}")

        try:
            backup_path = str(db_path) + ".backup"
            src = sqlite3.connect(str(db_path))
            dst = sqlite3.connect(backup_path)
            src.backup(dst)
            dst.close()
            src.close()

            # Compress the backup to save disk space (~70% smaller)
            import gzip as _gzip
            gz_path = backup_path + ".gz"
            with open(backup_path, "rb") as f_in, _gzip.open(gz_path, "wb") as f_out:
                while chunk := f_in.read(1 << 20):  # 1MB chunks
                    f_out.write(chunk)
            Path(backup_path).unlink()  # remove uncompressed

            _last_backup = datetime.now(timezone.utc)
            size_mb = Path(gz_path).stat().st_size / (1024 * 1024)
            print(f"[OK] Database backup: {gz_path} ({size_mb:.1f} MB)")
            _write_shared_state()
        except Exception as e:
            print(f"[!] Database backup failed: {e}")

        # Prune old stock history to prevent unbounded disk growth
        try:
            deleted = database.prune_stock_history(retention_days=30)
            if deleted > 0:
                print(f"[OK] Stock history pruned: {deleted} rows older than 30 days removed")
        except Exception as e:
            print(f"[!] Stock history prune failed: {e}")

        # Prune old chat messages (highest volume event type)
        try:
            deleted = database.prune_chat_events(retention_days=30)
            if deleted > 0:
                print(f"[OK] Chat pruned: {deleted} non-staff messages older than 30 days removed")
        except Exception as e:
            print(f"[!] Chat prune failed: {e}")

        _poller_stop.wait(BACKUP_INTERVAL)

# ============================================================
# NOTIFY PRUNE LOOP
# ============================================================


def _notify_prune_loop():
    """Prune _notify table every 30 seconds."""
    while not _poller_stop.is_set():
        try:
            database.prune_notify()
        except Exception:
            pass
        _poller_stop.wait(NOTIFY_PRUNE_INTERVAL)

# ============================================================
# FISHCLIENT PATCHES (same as the logger script)
# ============================================================


def _patched_handle_message(self, message):
    if isinstance(message, str):
        if message.startswith("2"):
            try:
                self.websocket.send("3")
            except Exception:
                pass
    elif isinstance(message, bytes):
        try:
            self.handle_packed(message)
        except Exception:
            pass


def _patched_listen(self):
    if self.websocket is None:
        return
    _logger = logging.getLogger("fishclient.client")
    while self.is_connected:
        try:
            message = self.websocket.recv()
            self.handle_message(message)
        except Exception as e:
            if not self.is_connected:
                break
            _logger.error(f"Error receiving message: {e}")
            self.is_connected = False  # Signal reconnect_loop to reconnect
            break


def reconnect_loop():
    """Continuously maintain the fishclient connection with fresh tokens."""
    global fish_client, _socket_connected_at
    _logger = logging.getLogger("fishclient.reconnect")
    backoff = 5

    while not _poller_stop.is_set():
        if not auth.is_configured:
            _poller_stop.wait(30)
            continue

        cookie = auth.get_fishclient_cookie()
        if not cookie:
            _logger.warning("No cookie available, retrying in 30s...")
            _poller_stop.wait(30)
            continue

        try:
            client = FishClient(cookie=cookie)
            client.handle_message = types.MethodType(_patched_handle_message, client)
            client.listen = types.MethodType(_patched_listen, client)

            # Register all event handlers
            for event_name in EVENTS:
                client.dispatcher.on(event_name)(make_event_handler(event_name))

            @client.dispatcher.on("disconnect")
            def on_disc(data):
                _logger.warning(f"Server disconnect: {data}")

            @client.dispatcher.on("connect_error")
            def on_err(data):
                _logger.error(f"Connection error: {data}")

            client.connect()
            fish_client = client
            _socket_connected_at = datetime.now(timezone.utc)
            backoff = 5  # Reset backoff on successful connect
            print(f"[OK] Connected to fishtank.live")
            _write_shared_state()

            # Block until the listen thread exits (disconnect)
            while client.is_connected and not _poller_stop.is_set():
                _poller_stop.wait(2)

            if _poller_stop.is_set():
                break

            _socket_connected_at = None
            _write_shared_state()
            print(f"[!] Socket disconnected. Reconnecting in {backoff}s with fresh tokens...")

        except Exception as e:
            print(f"[!] Connection failed: {e}. Retrying in {backoff}s...")

        # If token might be expired, refresh before reconnecting
        if auth.mode == "auto":
            auth.handle_401()

        _poller_stop.wait(backoff)
        backoff = min(backoff * 2, 60)  # Exponential backoff up to 60s


_profile_cache = {}  # user_id -> (timestamp, result)
_PROFILE_CACHE_TTL = 3600  # 1 hour
_PROFILE_CACHE_MAX = 500   # evict oldest when full (~100 KB at capacity)

def _fetch_user_profile(user_id):
    """Fetch displayName and color for a user from fishtank API (cached 1h)."""
    import time
    now = time.time()
    cached = _profile_cache.get(user_id)
    if cached and now - cached[0] < _PROFILE_CACHE_TTL:
        return cached[1]
    session = auth.get_session()
    try:
        resp = session.get(f"https://api.fishtank.live/v1/profile/{user_id}", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            profile = data.get("profile", data)
            result = {}
            dn = profile.get("displayName") or profile.get("username") or ""
            if dn:
                result["displayName"] = dn
            if profile.get("color"):
                result["color"] = profile["color"]
            if len(_profile_cache) >= _PROFILE_CACHE_MAX:
                oldest = min(_profile_cache, key=lambda k: _profile_cache[k][0])
                del _profile_cache[oldest]
            _profile_cache[user_id] = (now, result)
            return result
    except Exception as e:
        print(f"[!] Failed to fetch profile for {user_id}: {e}")
    finally:
        session.close()
    return {}


# ============================================================
# FISHCLIENT BRIDGE
# ============================================================

fish_client: FishClient = None

# Dedup tracking for TTS/SFX (server fires "approved" then "played" for same ID)
_dedup_lock = Lock()
_seen_tts_sfx_ids: dict = {}  # event_id -> timestamp
_DEDUP_WINDOW = 300  # 5 minutes (status transitions can be 30-60s apart)

# Feature toggle state (fishtoys, tts, sfx, etc.)
_feature_toggles: dict = {}  # feature_name -> {enabled, metadata, updated_at}
_fishtank_online = 0  # Live viewer count from chat:presence
_chat_room = ""       # Current chat room from chat:room event

def _is_duplicate(evt, data):
    """Check if this TTS/SFX event ID has already been seen."""
    if evt not in ("tts:update", "sfx:update"):
        return False
    if not isinstance(data, dict):
        return False
    event_id = data.get("id")
    if not event_id:
        return False
    event_id = str(event_id)
    now = datetime.now(timezone.utc).timestamp()
    with _dedup_lock:
        if event_id in _seen_tts_sfx_ids:
            return True
        _seen_tts_sfx_ids[event_id] = now
        if len(_seen_tts_sfx_ids) > 500:
            cutoff = now - _DEDUP_WINDOW
            stale = [k for k, v in _seen_tts_sfx_ids.items() if v < cutoff]
            for k in stale:
                del _seen_tts_sfx_ids[k]
        return False


def _should_filter_chat(data):
    """Filter chat messages that are system echoes (TTS/SFX/emote/happening)."""
    if not isinstance(data, dict):
        return False
    user = data.get("user", {})
    name = user.get("displayName", "") if isinstance(user, dict) else ""
    return name.lower() in ("tts", "sfx", "emote", "happening")


def _should_filter_notification(data):
    """Filter season pass gift notifications."""
    text = str(data).lower() if data else ""
    return "gifted" in text and "season pass" in text


def _track_feature_toggle(data):
    """Track feature toggle state changes."""
    if not isinstance(data, dict):
        return
    feature = data.get("feature", "")
    if feature:
        _feature_toggles[feature] = {
            "enabled": data.get("enabled", False),
            "metadata": data.get("metadata"),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }


def _score_sentiment(text):
    """Return VADER compound sentiment score (-1.0 to 1.0). Returns 0.0 for empty/None text."""
    if not text or not isinstance(text, str):
        return 0.0
    return _get_analyzer().polarity_scores(text)["compound"]


# In-memory poll vote accumulator: always holds the full list of {value, score} dicts
# for the current poll. Initialized from poll:start scores, updated by poll:vote dicts.
_poll_vote_state = []


def _seed_poll_vote_state():
    """Seed _poll_vote_state from DB on startup so mid-poll restarts don't lose votes."""
    global _poll_vote_state
    state = database.get_latest_poll_state()
    if state and state.get("active") and isinstance(state.get("votes"), list):
        _poll_vote_state = state["votes"]
        print(f"[OK] Seeded poll vote state: {len(_poll_vote_state)} options")
    elif state and state.get("active") and state.get("answers"):
        # No votes yet, seed from answer list with zero scores
        _poll_vote_state = [{"value": a, "score": 0} for a in state["answers"]]
        print(f"[OK] Seeded poll vote state from answers: {len(_poll_vote_state)} options")


def _normalize_poll_vote(data):
    """Normalize poll:vote data to always be a full list of all options.

    Old API format: list of all options (pass through, update tracked state).
    New API format: single dict per option (merge into tracked state, return full list).
    The list unwrap code would otherwise split list payloads into individual dicts,
    so this runs first to keep poll:vote data as a complete snapshot.
    """
    global _poll_vote_state
    if isinstance(data, list):
        # Old format or list-wrapped: full snapshot — adopt as current state
        _poll_vote_state = [v for v in data if isinstance(v, dict) and "value" in v]
        return list(_poll_vote_state)
    if isinstance(data, dict) and "value" in data:
        # New format: single option update — merge into tracked state
        merged = False
        for i, v in enumerate(_poll_vote_state):
            if v.get("value") == data["value"]:
                _poll_vote_state[i] = data
                merged = True
                break
        if not merged:
            _poll_vote_state.append(data)
        return list(_poll_vote_state)
    return data


def make_event_handler(evt):
    """Create an event handler for a specific socket event type."""
    def handler(data):
        # Track live viewer count (don't store to DB)
        if evt == "chat:presence":
            global _fishtank_online
            if isinstance(data, (int, float)):
                _fishtank_online = int(data)
            _write_shared_state()
            return

        # Track current chat room (don't store to DB)
        if evt == "chat:room":
            global _chat_room
            _chat_room = str(data) if data else ""
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] chat:room: {_chat_room}")
            _write_shared_state()
            return

        # Normalize poll:vote to full list BEFORE the list unwrap splits it
        if evt == "poll:vote":
            data = _normalize_poll_vote(data)

        # Initialize poll vote state from poll:start scores
        if evt == "poll:start" and isinstance(data, dict):
            scores = (data.get("poll") or data).get("scores", [])
            global _poll_vote_state
            _poll_vote_state = [v for v in scores if isinstance(v, dict) and "value" in v]

        # Unwrap list-wrapped payloads (fishtank sometimes sends [{...}, ...] instead of {...})
        # Multi-element lists are batched messages — process each individually then return.
        # poll:vote is already normalized above so its lists are intentional snapshots.
        if isinstance(data, list) and evt != "poll:vote":
            items = [d for d in data if isinstance(d, dict)]
            if len(items) == 1:
                data = items[0]
            elif len(items) > 1:
                for d in items:
                    handler(d)
                return
            # If no dicts found, fall through to store the raw list as-is

        # Filter TTS/SFX/emote system echo from chat
        if evt == "chat:message" and _should_filter_chat(data):
            return

        # Filter season pass gift notifications
        if evt == "notification:global" and _should_filter_notification(data):
            return

        # Dedup TTS/SFX (server sends "approved" then "played" for same ID)
        if _is_duplicate(evt, data):
            return

        # Track feature toggle state
        if evt == "feature-toggles:update":
            _track_feature_toggle(data)

        # Superchat displayName: fishtank sometimes sends empty displayName.
        # Fetch from profile API (api.fishtank.live, not www) as fallback.
        if evt == "super-chat:new" and isinstance(data, dict):
            if not data.get("displayName"):
                user_id = data.get("userId")
                if user_id:
                    profile = _fetch_user_profile(user_id)
                    if profile:
                        data.update(profile)

        # Score sentiment for chat and TTS messages
        if isinstance(data, dict):
            if evt in ("chat:message", "tts:update"):
                data["sentiment"] = _score_sentiment(data.get("message"))

        # Store in database
        db_id = database.store_event(evt, data)

        # Notify API server for WS fan-out
        database.notify_new_event(db_id, evt)

        # Log to console
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        summary = ""
        if evt == "chat:message" and isinstance(data, dict):
            user = data.get("user", {})
            name = user.get("displayName", "?") if isinstance(user, dict) else "?"
            msg = str(data.get("message", ""))[:60]
            summary = f"{name}: {msg}"
        elif evt == "notification:global" or evt == "announcement":
            summary = str(data)[:120]
        elif evt == "poll:start" and isinstance(data, dict):
            summary = f"Q: {data.get('question', '?')} | {len(data.get('answers', []))} options"
        elif evt == "poll:stop" and isinstance(data, dict):
            summary = f"Winner: {data.get('winner', '?')} | Q: {data.get('question', '?')}"
        elif evt == "poll:vote" and isinstance(data, list):
            parts = [f"{v.get('value','?')}:{v.get('score',0)}" for v in data[:5]]
            summary = " | ".join(parts)
        elif "stock:" in evt and isinstance(data, dict):
            summary = f"{data.get('tickerSymbol', '?')} {str(data)[:80]}"
        elif evt == "super-chat:new" and isinstance(data, dict):
            name = data.get("displayName", data.get("userId", "?"))
            cost = data.get("cost", "?")
            dur = data.get("duration", "?")
            summary = f"{name} ({cost}t, {dur}min): {str(data.get('message', ''))[:60]}"
        elif evt == "super-chat:delete" and isinstance(data, dict):
            summary = f"Deleted SC {data.get('id', '?')}"
        elif ("tts:price" in evt or "sfx:price" in evt):
            summary = str(data)[:80]
        elif ("tts" in evt or "sfx" in evt) and isinstance(data, dict):
            summary = data.get("displayName", "?")
        elif isinstance(data, dict):
            summary = str(data)[:80]
        else:
            summary = str(data)[:80]
        print(f"[{ts}] {evt}: {summary}")

    return handler


def stop_fish_client():
    global fish_client
    if fish_client is None:
        return
    fish_client.is_connected = False
    if fish_client.websocket is not None:
        try:
            fish_client.websocket.close()
        except Exception:
            pass
    fish_client = None


# ============================================================
# FISHTOY REST POLLER
# ============================================================

_poller_stop = Event()

# Health tracking
_last_fishtoy_poll = None   # datetime of last successful fishtoy poll
_last_stock_poll = None     # datetime of last successful stock poll
_socket_connected_at = None # datetime when current socket connection was established


def load_catalog():
    """Fetch item catalog, contestants, room mapping, and stocks from fishtank API."""
    global _item_catalog, _contestants, _room_map, _stocks

    if not auth.is_configured:
        return

    session = auth.get_session()
    try:
        # Load item catalog
        try:
            r = session.get("https://api.fishtank.live/v1/items", timeout=10)
            if r.status_code in (401, 403):
                print("[!] Catalog load: auth expired. Attempting re-auth...")
                if auth.handle_401():
                    session.close()
                    session = auth.get_session()
                    r = session.get("https://api.fishtank.live/v1/items", timeout=10)
            if r.status_code == 200:
                raw = r.json()
                with _catalog_lock:
                    for key, val in raw.items():
                        if isinstance(val, dict) and "id" in val:
                            _item_catalog[str(val["id"])] = val
                print(f"[OK] Loaded {len(_item_catalog)} items ({sum(1 for v in _item_catalog.values() if v.get('type') in CAPTURE_TYPES)} fishtoys/bigtoys)")
        except Exception as e:
            print(f"[WARN] Could not load item catalog: {e}")

        # Load contestants
        try:
            r = session.get("https://api.fishtank.live/v1/contestants", timeout=10)
            if r.status_code == 200:
                data = r.json()
                all_contestants = data.get("contestants", [])
                season_5 = [c for c in all_contestants if str(c.get("season", "")) == "5"]
                with _catalog_lock:
                    _contestants.clear()
                    _contestants.extend(season_5 if season_5 else all_contestants)
                print(f"[OK] Loaded {len(_contestants)} contestants (season 5: {len(season_5)}, total: {len(all_contestants)})")
        except Exception as e:
            print(f"[WARN] Could not load contestants: {e}")

        # Load room mapping from live-streams
        try:
            r = session.get("https://api.fishtank.live/v1/live-streams", timeout=10)
            if r.status_code == 200:
                data = r.json()
                streams = data.get("liveStreams", [])
                with _catalog_lock:
                    _room_map.clear()
                    for stream in streams:
                        sid = stream.get("id", "")
                        name = stream.get("name", sid)
                        _room_map[sid] = name
                print(f"[OK] Loaded {len(_room_map)} room mappings")
        except Exception as e:
            print(f"[WARN] Could not load room mappings: {e}")

        # Load stocks
        try:
            r = session.get("https://api.fishtank.live/v1/stocks", timeout=10)
            if r.status_code == 200:
                data = r.json()
                with _catalog_lock:
                    _stocks.clear()
                    _stocks.extend(data.get("stocks", []))
                print(f"[OK] Loaded {len(_stocks)} stocks")
        except Exception as e:
            print(f"[WARN] Could not load stocks: {e}")

        # Load feature toggle state from database
        try:
            toggles = database.get_latest_feature_toggles()
            _feature_toggles.update(toggles)
            if toggles:
                status_parts = [f"{k}={'ON' if v['enabled'] else 'OFF'}" for k, v in toggles.items()]
                print(f"[OK] Loaded {len(toggles)} feature toggle states: {', '.join(status_parts)}")
        except Exception as e:
            print(f"[WARN] Could not load feature toggles: {e}")
    finally:
        session.close()


def _backfill_empty_sc_names():
    """Backfill empty superchat displayNames using the profile API."""
    if not auth.is_configured:
        return
    conn = database._get_conn()
    rows = conn.execute("""
        SELECT id, data FROM events
        WHERE event_type = 'super-chat:new' AND (display_name IS NULL OR display_name = '')
    """).fetchall()
    if not rows:
        return
    fixed = 0
    for row in rows:
        try:
            data = fast_loads(row["data"]) if isinstance(row["data"], str) else row["data"]
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        user_id = data.get("userId")
        if not user_id:
            continue
        profile = _fetch_user_profile(user_id)
        if not profile or not profile.get("displayName"):
            continue
        data.update(profile)
        conn.execute(
            "UPDATE events SET data = ?, display_name = ? WHERE id = ?",
            (fast_dumps(data), profile["displayName"], row["id"])
        )
        fixed += 1
    if fixed:
        conn.commit()
        print(f"[OK] Backfilled displayName for {fixed} superchats via profile API")


def seed_superchats_from_rest():
    """Fetch active superchats from REST API and store any we haven't seen via Socket.IO."""
    if not auth.is_configured:
        return
    session = auth.get_session()
    try:
        r = session.get("https://api.fishtank.live/v1/super-chat", timeout=10)
        if r.status_code != 200:
            print(f"[WARN] Superchat seed: HTTP {r.status_code}")
            return
        data = r.json()
        chats = data if isinstance(data, list) else data.get("superChats", [])
        if not chats:
            return
        known_ids = database.get_known_superchat_ids()
        new_count = 0
        for sc in chats:
            sc_id = str(sc.get("id", ""))
            if not sc_id or sc_id in known_ids:
                continue
            # Fetch displayName from profile API if fishtank sent it empty
            if not sc.get("displayName") and sc.get("userId"):
                profile = _fetch_user_profile(sc["userId"])
                if profile:
                    sc.update(profile)
            db_id = database.store_event("super-chat:new", sc)
            database.notify_new_event(db_id, "super-chat:new")
            new_count += 1
        if new_count:
            print(f"[OK] Seeded {new_count} superchats from REST API")
    except Exception as e:
        print(f"[WARN] Superchat seed failed: {e}")
    finally:
        session.close()


def fishtoy_poller():
    """Poll /v1/items/recent for fishtoy redemptions."""
    global _last_fishtoy_poll
    if not auth.is_configured:
        return

    session = auth.get_session()
    seen_ids = set()
    prev_poll_ids = []
    first_poll = True

    # Load known fishtoy IDs from database for backfill detection
    known_ids = database.get_known_fishtoy_ids()

    try:
        while not _poller_stop.is_set():
            try:
                r = session.get("https://api.fishtank.live/v1/items/recent", timeout=10)
                if r.status_code in (401, 403):
                    print(f"[!] Fishtoy poller: auth expired (HTTP {r.status_code}). Attempting re-auth...")
                    if auth.handle_401():
                        session.close()
                        session = auth.get_session()
                        print("[OK] Fishtoy poller: re-auth successful, resuming.")
                    else:
                        print("[!] Fishtoy poller: re-auth failed. Will retry in 30s.")
                        _poller_stop.wait(30)
                    continue
                if r.status_code != 200:
                    _poller_stop.wait(FISHTOY_POLL_INTERVAL)
                    continue

                items = r.json().get("items", [])
                _last_fishtoy_poll = datetime.now(timezone.utc)
                this_poll_ids = set()
                backfilled = 0

                for item in items:
                    item_id = item.get("id")
                    this_poll_ids.add(item_id)

                    if item_id in seen_ids:
                        continue

                    # On first poll, check DB for backfill instead of skipping
                    if first_poll:
                        if str(item_id) in known_ids:
                            continue
                        # This item happened while we were down - backfill it
                        backfilled += 1

                    # Filter: only capture FISHTOY and BIGTOY
                    iid = str(item.get("itemId", ""))
                    cat_entry = _item_catalog.get(iid, {})
                    item_type = cat_entry.get("type")
                    if cat_entry and item_type not in CAPTURE_TYPES:
                        continue

                    # Store in database
                    db_id = database.store_event("fishtoy:used", item)

                    # Notify API server for WS fan-out
                    database.notify_new_event(db_id, "fishtoy:used")

                    # Log to console
                    t = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    name = item.get("displayName", "?")
                    target = item.get("target", "?")
                    item_name = cat_entry.get("name", f"#{iid}")
                    meta = item.get("metadata", "")
                    meta_str = str(meta) if meta else ""
                    prefix = "[BACKFILL] " if first_poll else ""
                    print(f"[{t}] {prefix}{item_type or '?'}: {name} -> {target} ({item_name}){f' [{meta_str[:50]}]' if meta_str else ''}")

                # Prune seen_ids to last 3 polls
                prev_poll_ids.append(this_poll_ids)
                if len(prev_poll_ids) > 3:
                    prev_poll_ids.pop(0)
                seen_ids = set().union(*prev_poll_ids)

                if first_poll:
                    first_poll = False
                    if backfilled:
                        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Fishtoy poller: backfilled {backfilled} missed events from {len(items)} items")
                    else:
                        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Fishtoy poller: {len(items)} items in snapshot, no gaps detected. Watching...")

            except http_requests.RequestException as e:
                print(f"[!] Fishtoy poll error: {e}")

            _poller_stop.wait(FISHTOY_POLL_INTERVAL)
    finally:
        session.close()


def stock_poller():
    """Poll /v1/stocks every 60s and store price history."""
    global _last_stock_poll
    if not auth.is_configured:
        return

    session = auth.get_session()

    try:
        while not _poller_stop.is_set():
            try:
                r = session.get("https://api.fishtank.live/v1/stocks", timeout=10)
                if r.status_code in (401, 403):
                    print(f"[!] Stock poller: auth expired (HTTP {r.status_code}). Attempting re-auth...")
                    if auth.handle_401():
                        session.close()
                        session = auth.get_session()
                        print("[OK] Stock poller: re-auth successful.")
                elif r.status_code == 200:
                    data = r.json()
                    stocks = data.get("stocks", [])
                    if stocks:
                        with _catalog_lock:
                            _stocks.clear()
                            _stocks.extend(stocks)
                        database.store_stock_snapshot(stocks)
                        _last_stock_poll = datetime.now(timezone.utc)
                        _write_shared_state()
            except http_requests.RequestException:
                pass

            _poller_stop.wait(60)
    finally:
        session.close()


def catalog_refresh_poller():
    """Refresh contestants and item catalog every 10 minutes."""
    if not auth.is_configured:
        return

    # Wait 10 minutes before first refresh (load_catalog already ran on startup)
    _poller_stop.wait(600)

    while not _poller_stop.is_set():
        session = auth.get_session()
        try:
            # Refresh item catalog
            try:
                r = session.get("https://api.fishtank.live/v1/items", timeout=10)
                if r.status_code in (401, 403):
                    if auth.handle_401():
                        session.close()
                        session = auth.get_session()
                        r = session.get("https://api.fishtank.live/v1/items", timeout=10)
                if r.status_code == 200:
                    raw = r.json()
                    new_count = 0
                    with _catalog_lock:
                        for key, val in raw.items():
                            if isinstance(val, dict) and "id" in val:
                                sid = str(val["id"])
                                if sid not in _item_catalog:
                                    new_count += 1
                                _item_catalog[sid] = val
                    if new_count:
                        print(f"[OK] Catalog refresh: {new_count} new items added ({len(_item_catalog)} total)")
            except Exception as e:
                print(f"[WARN] Catalog refresh failed (items): {e}")

            # Refresh contestants
            try:
                r = session.get("https://api.fishtank.live/v1/contestants", timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    all_contestants = data.get("contestants", [])
                    season_5 = [c for c in all_contestants if str(c.get("season", "")) == "5"]
                    new_list = season_5 if season_5 else all_contestants
                    with _catalog_lock:
                        old_count = len(_contestants)
                        _contestants.clear()
                        _contestants.extend(new_list)
                    if len(_contestants) != old_count:
                        print(f"[OK] Catalog refresh: contestants updated ({old_count} -> {len(_contestants)})")
            except Exception as e:
                print(f"[WARN] Catalog refresh failed (contestants): {e}")

            _write_shared_state()
        finally:
            session.close()

        _poller_stop.wait(600)  # 10 minutes


# ============================================================
# SHARED STATE
# ============================================================

_SHARED_STATE_PATH = os.environ.get(
    "FISHTANK_SHARED_STATE",
    str(Path(__file__).parent / "_shared_state.json"),
)


def _write_shared_state():
    with _catalog_lock:
        state = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "item_catalog": dict(_item_catalog),
            "contestants": list(_contestants),
            "room_map": dict(_room_map),
            "stocks": list(_stocks),
            "feature_toggles": dict(_feature_toggles),
            "socket_connected_at": _socket_connected_at.isoformat() if _socket_connected_at else None,
            "fishtank_online": _fishtank_online,
            "auth_mode": auth.mode,
            "auth_configured": auth.is_configured,
            "last_fishtoy_poll": _last_fishtoy_poll.isoformat() if _last_fishtoy_poll else None,
            "last_stock_poll": _last_stock_poll.isoformat() if _last_stock_poll else None,
            "last_backup": _last_backup.isoformat() if _last_backup else None,
            "chat_room": _chat_room,
        }
    shared_state.write_state(_SHARED_STATE_PATH, state)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    database.init_db()

    # Backfill extracted columns (one-time migration, batched)
    backfilled = database.backfill_extracted_columns()
    if backfilled:
        print(f"[OK] Backfilled extracted columns for {backfilled} events")
    database.backfill_poll_vote_costs()
    _backfill_empty_sc_names()

    load_catalog()
    _seed_poll_vote_state()
    seed_superchats_from_rest()
    _write_shared_state()

    Thread(target=reconnect_loop, daemon=True).start()
    Thread(target=fishtoy_poller, daemon=True).start()
    Thread(target=stock_poller, daemon=True).start()
    Thread(target=catalog_refresh_poller, daemon=True).start()
    Thread(target=db_backup_poller, daemon=True).start()
    Thread(target=_notify_prune_loop, daemon=True).start()

    print(f"[OK] Ingestion process started (auth: {auth.mode})")

    try:
        _poller_stop.wait()
    except KeyboardInterrupt:
        print("\n[OK] Shutting down...")
        _poller_stop.set()
        stop_fish_client()
