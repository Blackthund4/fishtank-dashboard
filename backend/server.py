"""
Fishtank Dashboard Backend

Captures fishtank.live events via two methods:
  - REST polling: /v1/items/recent for fishtoy redemptions
  - Socket.IO: real-time push for chat, TTS, SFX, polls, notifications

Stores everything in SQLite and serves a React dashboard.

Usage (recommended):
    Create backend/.env with FISHTANK_EMAIL and FISHTANK_PASSWORD
    python server.py

Legacy usage:
    export FISHTANK_COOKIE='your_cookie_here'
    python server.py
"""

import asyncio
import json
import logging
import os
import time as _time
import types
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from threading import Thread, Event, Lock

import requests as http_requests
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pathlib import Path

from fishclient import FishClient

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

import database
from auth import AuthManager

_sentiment_analyzer = SentimentIntensityAnalyzer()

# ============================================================
# CONFIG
# ============================================================

auth = AuthManager()

EVENTS = [
    # Fishtoys are polled via REST (/v1/items/recent), not socket events
    # Chat
    "chat:message",
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
# ANALYTICS CACHE
# ============================================================

_analytics_cache = {}  # key -> (timestamp, data)
CACHE_TTL = 60  # seconds
CACHE_MAX_ENTRIES = 50  # max cache entries before pruning


def _cached_query(key, func, *args):
    """Return cached result if fresh, otherwise run func and cache."""
    now = _time.time()
    if key in _analytics_cache:
        cached_at, data = _analytics_cache[key]
        if now - cached_at < CACHE_TTL:
            return data

    # Prune expired entries if cache is getting large
    if len(_analytics_cache) > CACHE_MAX_ENTRIES:
        expired = [k for k, (ts, _) in _analytics_cache.items() if now - ts >= CACHE_TTL]
        for k in expired:
            del _analytics_cache[k]

    result = func(*args)
    _analytics_cache[key] = (now, result)
    return result

# ============================================================
# RATE LIMITING
# ============================================================

MAX_WS_CLIENTS = 50  # Max concurrent WebSocket connections
BUILD_VERSION = os.environ.get("BUILD_VERSION", "dev")

# Lightweight cache for /api/health event count — avoids a COUNT(*) on every monitor ping
_health_event_count: dict = {"value": 0, "ts": 0.0}
_HEALTH_COUNT_TTL = 30  # seconds

# Per-IP rate limiting: requests per window
_rate_limit_lock = Lock()
_rate_limits: dict = defaultdict(deque)  # ip -> deque of timestamps
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX = 120    # max requests per window per IP


def _check_rate_limit(ip: str) -> bool:
    """Return True if the request should be rejected."""
    now = _time.time()
    cutoff = now - RATE_LIMIT_WINDOW
    with _rate_limit_lock:
        dq = _rate_limits[ip]
        # Pop expired entries from the front — O(n_expired) amortised, not O(n_total)
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) >= RATE_LIMIT_MAX:
            return True
        dq.append(now)
        return False


def _prune_rate_limits():
    """Periodic cleanup of stale rate limit entries."""
    now = _time.time()
    cutoff = now - RATE_LIMIT_WINDOW
    with _rate_limit_lock:
        stale_ips = [ip for ip, dq in _rate_limits.items() if not dq or dq[-1] < cutoff]
        for ip in stale_ips:
            del _rate_limits[ip]


# ============================================================
# DATABASE BACKUP
# ============================================================

BACKUP_INTERVAL = 21600  # 6 hours
WAL_CHECKPOINT_INTERVAL = 3600  # 1 hour
_last_backup = None


def db_backup_poller():
    """Periodically back up the SQLite database and checkpoint the WAL."""
    global _last_backup
    import sqlite3

    # First backup after 5 minutes (don't wait 6 hours)
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

            _last_backup = datetime.now(timezone.utc)
            size_mb = Path(backup_path).stat().st_size / (1024 * 1024)
            print(f"[OK] Database backup: {backup_path} ({size_mb:.1f} MB)")
        except Exception as e:
            print(f"[!] Database backup failed: {e}")

        # Prune old stock history to prevent unbounded disk growth
        try:
            deleted = database.prune_stock_history(retention_days=30)
            if deleted > 0:
                print(f"[OK] Stock history pruned: {deleted} rows older than 30 days removed")
        except Exception as e:
            print(f"[!] Stock history prune failed: {e}")

        _poller_stop.wait(BACKUP_INTERVAL)

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

            # Block until the listen thread exits (disconnect)
            while client.is_connected and not _poller_stop.is_set():
                _poller_stop.wait(2)

            if _poller_stop.is_set():
                break

            print(f"[!] Socket disconnected. Reconnecting in {backoff}s with fresh tokens...")

        except Exception as e:
            print(f"[!] Connection failed: {e}. Retrying in {backoff}s...")

        # If token might be expired, refresh before reconnecting
        if auth.mode == "auto":
            auth.handle_401()

        _poller_stop.wait(backoff)
        backoff = min(backoff * 2, 60)  # Exponential backoff up to 60s


def _fetch_user_profile(user_id):
    """Fetch displayName and color for a user from fishtank API."""
    session = auth.get_session()
    try:
        resp = session.get(f"https://www.fishtank.live/api/v1/profile/{user_id}", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            profile = data.get("profile", data)
            result = {}
            if profile.get("displayName"):
                result["displayName"] = profile["displayName"]
            if profile.get("color"):
                result["color"] = profile["color"]
            return result
    except Exception as e:
        print(f"[!] Failed to fetch profile for {user_id}: {e}")
    finally:
        session.close()
    return {}


# ============================================================
# BROWSER WEBSOCKET CLIENTS
# ============================================================

_clients_lock = Lock()
browser_clients: set[WebSocket] = set()


async def _ws_ping_loop():
    """Send a no-op ping to all browser clients every 60s to prevent Cloudflare idle timeout (100s)."""
    while True:
        await asyncio.sleep(60)
        with _clients_lock:
            clients_snapshot = set(browser_clients)
        if not clients_snapshot:
            continue
        disconnected = set()
        for ws in clients_snapshot:
            try:
                await ws.send_json({"event_type": "ping"})
            except Exception:
                disconnected.add(ws)
        if disconnected:
            with _clients_lock:
                browser_clients.difference_update(disconnected)


async def broadcast_to_browsers(event_type: str, data, db_id: int):
    """Send event to all connected browser clients."""
    message = json.dumps(
        {"event_type": event_type, "data": data, "db_id": db_id},
        ensure_ascii=False,
        default=str,
    )
    with _clients_lock:
        clients_snapshot = set(browser_clients)
    disconnected = set()
    for ws in clients_snapshot:
        try:
            await ws.send_text(message)
        except Exception:
            disconnected.add(ws)
    if disconnected:
        with _clients_lock:
            browser_clients.difference_update(disconnected)


# ============================================================
# FISHCLIENT BRIDGE
# ============================================================

fish_client: FishClient = None
_loop: asyncio.AbstractEventLoop = None

# Dedup tracking for TTS/SFX (server fires "approved" then "played" for same ID)
_dedup_lock = Lock()
_seen_tts_sfx_ids: dict = {}  # event_id -> timestamp
_DEDUP_WINDOW = 300  # 5 minutes (status transitions can be 30-60s apart)

# Feature toggle state (fishtoys, tts, sfx, etc.)
_feature_toggles: dict = {}  # feature_name -> {enabled, metadata, updated_at}
_fishtank_online = 0  # Live viewer count from chat:presence

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
    """Filter chat messages that are TTS/SFX/emote system echoes."""
    if not isinstance(data, dict):
        return False
    user = data.get("user", {})
    name = user.get("displayName", "") if isinstance(user, dict) else ""
    return name.lower() in ("tts", "sfx", "emote")


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
    return _sentiment_analyzer.polarity_scores(text)["compound"]


def make_event_handler(evt):
    """Create an event handler for a specific socket event type."""
    def handler(data):
        # Track live viewer count (don't store to DB)
        if evt == "chat:presence":
            global _fishtank_online
            if isinstance(data, (int, float)):
                _fishtank_online = int(data)
            return

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

        # Enrich superchat with displayName if missing
        if evt == "super-chat:new" and isinstance(data, dict):
            # Normalize nested user.displayName to top-level
            if not data.get("displayName"):
                user = data.get("user") or {}
                data["displayName"] = user.get("displayName") or data.get("username") or ""
            # Fetch from profile API as last resort
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

        # Broadcast to browsers
        if _loop and _loop.is_running():
            asyncio.run_coroutine_threadsafe(
                broadcast_to_browsers(evt, data, db_id), _loop
            )

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
            # Normalize nested user.displayName to top-level
            if not sc.get("displayName"):
                user = sc.get("user") or {}
                sc["displayName"] = user.get("displayName") or sc.get("username") or ""
            # Fetch from profile API as last resort
            if not sc.get("displayName") and sc.get("userId"):
                profile = _fetch_user_profile(sc["userId"])
                if profile:
                    sc.update(profile)
            database.store_event("super-chat:new", sc)
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

                    # Log to console
                    t = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    name = item.get("displayName", "?")
                    target = item.get("target", "?")
                    item_name = cat_entry.get("name", f"#{iid}")
                    meta = item.get("metadata", "")
                    meta_str = str(meta) if meta else ""
                    prefix = "[BACKFILL] " if first_poll else ""
                    print(f"[{t}] {prefix}{item_type or '?'}: {name} -> {target} ({item_name}){f' [{meta_str[:50]}]' if meta_str else ''}")

                    # Broadcast to browsers
                    if _loop and _loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            broadcast_to_browsers("fishtoy:used", item, db_id), _loop
                        )

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
            except http_requests.RequestException:
                pass

            _poller_stop.wait(60)
    finally:
        session.close()


def catalog_refresh_poller():
    """Refresh contestants and item catalog every 30 minutes."""
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
        finally:
            session.close()

        _poller_stop.wait(600)  # 10 minutes


# ============================================================
# FASTAPI APP
# ============================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _loop
    _loop = asyncio.get_running_loop()

    database.init_db()

    # Backfill extracted columns in background (one-time migration, batched)
    def _run_backfill():
        backfilled = database.backfill_extracted_columns()
        if backfilled:
            print(f"[OK] Backfilled extracted columns for {backfilled} events")
    Thread(target=_run_backfill, daemon=True).start()

    # Load item catalog and contestants from fishtank API
    load_catalog()

    # Seed any active superchats from REST (may be missed if server restarted)
    seed_superchats_from_rest()

    # Start fishclient reconnect loop (Socket.IO for chat/TTS/SFX/polls/notifications)
    Thread(target=reconnect_loop, daemon=True).start()

    # Start fishtoy REST poller in background thread
    Thread(target=fishtoy_poller, daemon=True).start()

    # Start stock price history poller in background thread
    Thread(target=stock_poller, daemon=True).start()

    # Start catalog refresh poller (contestants + items every 10 min)
    Thread(target=catalog_refresh_poller, daemon=True).start()

    # Start database backup poller (every 6 hours)
    Thread(target=db_backup_poller, daemon=True).start()

    # Keep browser WebSocket connections alive through Cloudflare's 100s idle timeout
    ping_task = asyncio.create_task(_ws_ping_loop())

    yield

    _poller_stop.set()
    stop_fish_client()
    ping_task.cancel()


app = FastAPI(title="Fishtank Dashboard", lifespan=lifespan)

# CORS: configurable via ALLOWED_ORIGINS env var (comma-separated)
_allowed_origins = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _allowed_origins],
    allow_methods=["GET", "HEAD"],
    allow_headers=["*"],
)


# Rate limiting middleware
@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    # Skip rate limiting for static files
    if request.url.path.startswith("/assets") or request.url.path == "/":
        return await call_next(request)

    ip = request.client.host if request.client else "unknown"
    if _check_rate_limit(ip):
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many requests. Try again later."},
        )

    # Periodic cleanup
    if len(_rate_limits) > 1000:
        _prune_rate_limits()

    return await call_next(request)


# Suppress stack traces in production
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# --- WebSocket for live browser updates ---


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    with _clients_lock:
        if len(browser_clients) >= MAX_WS_CLIENTS:
            await ws.close(code=1013, reason="Too many connections")
            return
        await ws.accept()
        browser_clients.add(ws)
    try:
        await ws.send_text(json.dumps({"event_type": "server:hello", "data": {"version": BUILD_VERSION}}))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        with _clients_lock:
            browser_clients.discard(ws)


# --- REST endpoints ---


@app.get("/api/events")
def api_events(
    type: str = Query(None, description="Filter by event type (comma-separated)"),
    limit: int = Query(200, le=1000),
    since_id: int = Query(None, description="Only return events with id > this"),
    before_id: int = Query(None, description="Only return events with id < this (keyset pagination)"),
):
    return database.get_events(event_type=type, limit=limit, since_id=since_id, before_id=before_id)


@app.get("/api/stats")
def api_stats(since: str = Query(None, description="ISO timestamp to filter from")):
    # Truncate to minute precision so clients with slightly different since values
    # share the same cache entry instead of each triggering a full DB scan.
    if since and len(since) > 16:
        since = since[:16] + ":00"
    return _cached_query(f"stats:{since}", database.get_stats, since)


@app.get("/api/status")
def api_status():
    fc = fish_client  # Local ref to avoid race condition
    return {
        "connected": fc is not None and fc.is_connected,
        "browser_clients": len(browser_clients),
        "fishtank_online": _fishtank_online,
        "auth_mode": auth.mode,
        "auth_configured": auth.is_configured,
    }


@app.api_route("/api/health", methods=["GET", "HEAD"])
def api_health():
    """Health check for monitoring. Sensitive details omitted."""
    now = datetime.now(timezone.utc)
    fc = fish_client

    # Socket health
    socket_connected = fc is not None and fc.is_connected
    socket_uptime = None
    if socket_connected and _socket_connected_at:
        socket_uptime = int((now - _socket_connected_at).total_seconds())

    # Poller health
    fishtoy_age = None
    if _last_fishtoy_poll:
        fishtoy_age = int((now - _last_fishtoy_poll).total_seconds())
    stock_age = None
    if _last_stock_poll:
        stock_age = int((now - _last_stock_poll).total_seconds())

    # Database health — count cached 30s to avoid a COUNT(*) on every monitor ping
    now_ts = _time.time()
    if now_ts - _health_event_count["ts"] >= _HEALTH_COUNT_TTL:
        try:
            _health_event_count["value"] = database.get_event_count()
            _health_event_count["ts"] = now_ts
            db_ok = True
        except Exception:
            db_ok = False
    else:
        db_ok = True
    total_events = _health_event_count["value"]

    # Overall status
    issues = []
    if not socket_connected:
        issues.append("socket disconnected")
    if fishtoy_age is not None and fishtoy_age > 30:
        issues.append(f"fishtoy poller stale ({fishtoy_age}s)")
    elif fishtoy_age is None and auth.is_configured:
        issues.append("fishtoy poller not started")
    if stock_age is not None and stock_age > 90:
        issues.append(f"stock poller stale ({stock_age}s)")
    elif stock_age is None and auth.is_configured:
        issues.append("stock poller not started")
    if not db_ok:
        issues.append("database error")

    return {
        "status": "healthy" if not issues else "degraded",
        "issues": issues,
        "socket_connected": socket_connected,
        "socket_uptime_seconds": socket_uptime,
        "pollers": {
            "fishtoy_age_seconds": fishtoy_age,
            "stock_age_seconds": stock_age,
        },
        "database": {
            "ok": db_ok,
            "total_events": total_events,
        },
        "backup": {
            "last_backup": _last_backup.isoformat() if _last_backup else None,
        },
        "browser_clients": len(browser_clients),
        "checked_at": now.isoformat(),
    }


@app.get("/api/items")
def api_items():
    """Return the item catalog (itemId -> name/description/icon)."""
    with _catalog_lock:
        return dict(_item_catalog)


@app.get("/api/contestants")
def api_contestants():
    """Return the contestant list."""
    with _catalog_lock:
        return list(_contestants)


@app.get("/api/rooms")
def api_rooms():
    """Return room code -> name mapping."""
    with _catalog_lock:
        return dict(_room_map)


@app.get("/api/stocks")
def api_stocks():
    """Return current stock data (updated by stock_poller every 60s)."""
    with _catalog_lock:
        return list(_stocks)


@app.get("/api/feature-toggles")
def api_feature_toggles():
    """Return current feature toggle states."""
    return _feature_toggles


@app.get("/api/stocks/history")
def api_stock_history(
    ticker: str = Query(None, description="Filter by ticker symbol"),
    limit: int = Query(500, le=5000),
    since: str = Query(None, description="ISO timestamp to filter history from"),
):
    """Return stock price history."""
    return database.get_stock_history(ticker=ticker, limit=limit, since=since)


@app.get("/api/analytics/tts-sfx")
def api_tts_sfx_analytics(since: str = Query(None)):
    """TTS and SFX analytics: top rooms, top senders, hourly activity."""
    return _cached_query(f"tts-sfx:{since}", database.get_tts_sfx_analytics, since)


@app.get("/api/analytics/chat")
def api_chat_analytics(since: str = Query(None)):
    """Chat analytics: top chatters, hourly volume."""
    return _cached_query(f"chat:{since}", database.get_chat_analytics, since)


@app.get("/api/analytics/chat-sentiment")
def api_chat_sentiment(since: str = Query(None)):
    """Chat sentiment analytics: overall mood, hourly breakdown."""
    return _cached_query(f"chat-sentiment:{since}", database.get_chat_sentiment, since)


@app.get("/api/analytics/tts-sentiment")
def api_tts_sentiment(since: str = Query(None)):
    """TTS sentiment analytics: overall mood, hourly breakdown, mood by contestant."""
    return _cached_query(f"tts-sentiment:{since}", database.get_tts_sentiment, since)


@app.get("/api/analytics/peak-hours")
def api_peak_hours():
    """Combined hourly activity across all event types with peak/quietest hours."""
    return _cached_query("peak-hours", database.get_peak_hours)


@app.get("/api/hidden-content")
def api_hidden_content(
    target: str = Query(None),
    search: str = Query(None),
    limit: int = Query(200, le=1000),
    offset: int = Query(0, ge=0),
):
    """Get fishtoy events with hidden metadata content."""
    return database.get_hidden_content(target=target, search=search, limit=limit, offset=offset)


@app.get("/api/hidden-content/targets")
def api_hidden_content_targets():
    """Get target counts for hidden content from full DB history."""
    return _cached_query("hidden-content-targets", database.get_hidden_content_targets)


@app.get("/api/fishtoy-availability")
def api_fishtoy_availability():
    """Return fishtoy/bigtoy items with their enabled/cooldown status."""
    return [
        {
            "id": v.get("id"),
            "name": v.get("name"),
            "type": v.get("type"),
            "cost": v.get("cost"),
            "enabled": v.get("enabled"),
            "cooldown": v.get("cooldown"),
            "targets": v.get("targets"),
            "rarity": v.get("rarity"),
            "hasCustomText": v.get("hasCustomText"),
            "description": v.get("description"),
        }
        for v in _item_catalog.values()
        if v.get("type") in CAPTURE_TYPES
    ]


@app.get("/api/superchats")
def api_superchats(
    limit: int = Query(50, le=500),
    since: str = Query(None, description="ISO timestamp to filter from"),
):
    """Get superchat events with deletion status."""
    return database.get_superchats(limit=limit, since=since)


@app.get("/api/targets")
def api_targets():
    """Get all fishtoy targets with total count and spend."""
    return _cached_query("targets", database.get_targets)


@app.get("/api/target-stats")
def api_target_stats(target: str = Query(..., description="Contestant target name")):
    """Get detailed stats for a specific target from full DB history."""
    return _cached_query(f"target-stats:{target}", database.get_target_stats, target)


@app.get("/api/fishtoys")
def api_fishtoys(
    target: str = Query(None, description="Filter by contestant target name"),
    item_id: str = Query(None, description="Filter by item ID"),
    search: str = Query(None, description="Search metadata and sender name"),
    limit: int = Query(200, le=1000),
    before_id: int = Query(None, description="Only return events with id < this (keyset pagination)"),
):
    """Get fishtoy events with optional filters."""
    return database.get_fishtoys(
        target=target, item_id=item_id, search=search,
        limit=limit, before_id=before_id,
    )


@app.get("/api/polls")
def api_polls(limit: int = Query(50, le=500)):
    """Get poll events (start, stop, vote)."""
    return database.get_polls(limit=limit)


@app.get("/api/notifications")
def api_notifications(limit: int = Query(100, le=500)):
    """Get director messages and announcements."""
    return database.get_notifications(limit=limit)


@app.get("/api/price-changes")
def api_price_changes(limit: int = Query(100, le=500)):
    """Get TTS/SFX price change history."""
    return database.get_price_changes(limit=limit)


@app.get("/api/user/{username}")
def api_user_search(username: str, limit: int = Query(500, le=2000)):
    """Search all event types for a specific user."""
    return database.search_user(username=username, limit=limit)


@app.get("/api/users/suggest")
def api_user_suggest(q: str = Query("", description="Username prefix")):
    """Autocomplete suggestions for usernames."""
    if len(q) < 2:
        return []
    return _cached_query(f"suggest:{q.lower()}", database.suggest_users, q, 10)


@app.get("/api/stocks/count")
def api_stock_count():
    """Return actual count of stock history snapshots."""
    return {"count": database.get_stock_snapshot_count()}


@app.get("/api/polls/latest")
def api_poll_latest():
    """Return reconstructed state of the most recent poll."""
    return database.get_latest_poll_state()


@app.get("/api/stocks/delta")
def api_stock_deltas(range: str = Query('3h')):
    """Return base prices per ticker for custom time range delta calculation."""
    if range not in {'3h', '12h', '3d'}:
        range = '3h'
    return _cached_query(f"stock-delta:{range}", database.get_stock_deltas, range)


@app.get("/api/charts/stocks")
def api_charts_stocks(range: str = Query('24h')):
    if range not in {'30m', '1h', '2h', '6h', '12h', '24h', '3d', '7d', 'all'}:
        range = '24h'
    return _cached_query(f"charts-stocks:{range}", database.get_stock_history_chart, range)


@app.get("/api/charts/spend")
def api_charts_spend(range: str = Query('24h')):
    if range not in {'30m', '1h', '2h', '6h', '12h', '24h', '3d', '7d', 'all'}:
        range = '24h'
    return _cached_query(f"charts-spend:{range}", database.get_spend_trends, range)


@app.get("/api/charts/chatters")
def api_charts_chatters(range: str = Query('24h')):
    if range not in {'30m', '1h', '2h', '6h', '12h', '24h', '3d', '7d', 'all'}:
        range = '24h'
    return _cached_query(f"charts-chatters:{range}", database.get_chat_chart, range)


# --- Serve frontend static files ---

FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"

if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def serve_frontend(full_path: str):
        file_path = (FRONTEND_DIST / full_path).resolve()
        # Prevent path traversal outside dist directory
        if not str(file_path).startswith(str(FRONTEND_DIST.resolve())):
            return FileResponse(FRONTEND_DIST / "index.html")
        if file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(FRONTEND_DIST / "index.html")


# ============================================================
# ENTRYPOINT
# ============================================================

if __name__ == "__main__":
    import uvicorn

    if not auth.is_configured:
        print("=" * 60)
        print("  WARNING: No auth credentials configured")
        print()
        print("  Option 1 (recommended): Create backend/.env file:")
        print("    FISHTANK_EMAIL=your_email@example.com")
        print("    FISHTANK_PASSWORD=your_password")
        print()
        print("  Option 2 (legacy): Set cookie manually:")
        print("    $env:FISHTANK_COOKIE = 'your_cookie'  (PowerShell)")
        print()
        print("  See .env.example for details.")
        print("=" * 60)

    ssl_keyfile = os.environ.get("SSL_KEYFILE")
    ssl_certfile = os.environ.get("SSL_CERTFILE")
    scheme = "https" if ssl_certfile else "http"
    print(f"Starting Fishtank Dashboard on {scheme}://localhost:8000")
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="warning",
        ssl_keyfile=ssl_keyfile or None,
        ssl_certfile=ssl_certfile or None,
    )
