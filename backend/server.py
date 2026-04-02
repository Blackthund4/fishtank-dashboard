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
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from threading import Thread, Event

import requests as http_requests
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pathlib import Path

from fishclient import FishClient

import database
from auth import AuthManager

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
]

FISHTOY_POLL_INTERVAL = 2  # seconds

# ---- Cached catalog data (loaded on startup) ----
_item_catalog = {}  # itemId -> {name, description, icon, type, ...}
_contestants = []   # [{id, name, color, photo, ...}]
_room_map = {}      # room code -> room name (e.g. "hwdn-5" -> "Hallway")
_stocks = []        # [{tickerSymbol, currentPrice, ...}]
CAPTURE_TYPES = {"FISHTOY", "BIGTOY"}

# ============================================================
# RATE LIMITING
# ============================================================

MAX_WS_CLIENTS = 50  # Max concurrent WebSocket connections

# Per-IP rate limiting: requests per window
_rate_limits: dict = defaultdict(list)  # ip -> [timestamps]
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX = 120    # max requests per window per IP


def _check_rate_limit(ip: str) -> bool:
    """Return True if the request should be rejected."""
    now = _time.time()
    timestamps = _rate_limits[ip]
    # Prune old entries
    cutoff = now - RATE_LIMIT_WINDOW
    _rate_limits[ip] = [t for t in timestamps if t > cutoff]
    if len(_rate_limits[ip]) >= RATE_LIMIT_MAX:
        return True
    _rate_limits[ip].append(now)
    return False


def _prune_rate_limits():
    """Periodic cleanup of stale rate limit entries."""
    now = _time.time()
    cutoff = now - RATE_LIMIT_WINDOW
    stale_ips = [ip for ip, ts in _rate_limits.items() if not ts or ts[-1] < cutoff]
    for ip in stale_ips:
        del _rate_limits[ip]


# ============================================================
# DATABASE BACKUP
# ============================================================

BACKUP_INTERVAL = 21600  # 6 hours
_last_backup = None


def db_backup_poller():
    """Periodically back up the SQLite database using SQLite's online backup API."""
    global _last_backup
    import sqlite3

    # First backup after 5 minutes (don't wait 6 hours)
    _poller_stop.wait(300)

    while not _poller_stop.is_set():
        try:
            db_path = database.DB_PATH
            if str(db_path) == ":memory:" or not Path(db_path).exists():
                _poller_stop.wait(BACKUP_INTERVAL)
                continue

            backup_path = str(db_path) + ".backup"
            # Use SQLite online backup API for a consistent copy
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


# ============================================================
# BROWSER WEBSOCKET CLIENTS
# ============================================================

browser_clients: set[WebSocket] = set()


async def broadcast_to_browsers(event_type: str, data, db_id: int):
    """Send event to all connected browser clients."""
    message = json.dumps(
        {"event_type": event_type, "data": data, "db_id": db_id},
        ensure_ascii=False,
        default=str,
    )
    disconnected = set()
    for ws in browser_clients:
        try:
            await ws.send_text(message)
        except Exception:
            disconnected.add(ws)
    browser_clients.difference_update(disconnected)


# ============================================================
# FISHCLIENT BRIDGE
# ============================================================

fish_client: FishClient = None
_loop: asyncio.AbstractEventLoop = None

# Dedup tracking for TTS/SFX (server fires "approved" then "played" for same ID)
_seen_tts_sfx_ids: dict = {}  # event_id -> timestamp
_DEDUP_WINDOW = 300  # 5 minutes (status transitions can be 30-60s apart)

# Feature toggle state (fishtoys, tts, sfx, etc.)
_feature_toggles: dict = {}  # feature_name -> {enabled, metadata, updated_at}


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
    if event_id in _seen_tts_sfx_ids:
        return True
    _seen_tts_sfx_ids[event_id] = now
    # Prune old entries
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


def make_event_handler(evt):
    """Create an event handler for a specific socket event type."""
    def handler(data):
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

    # Load item catalog
    try:
        r = session.get("https://api.fishtank.live/v1/items", timeout=10)
        if r.status_code in (401, 403):
            print("[!] Catalog load: auth expired. Attempting re-auth...")
            if auth.handle_401():
                session = auth.get_session()
                r = session.get("https://api.fishtank.live/v1/items", timeout=10)
        if r.status_code == 200:
            raw = r.json()
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
            # Filter to current season (5) only
            season_5 = [c for c in all_contestants if str(c.get("season", "")) == "5"]
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

    while not _poller_stop.is_set():
        try:
            r = session.get("https://api.fishtank.live/v1/items/recent", timeout=10)
            if r.status_code in (401, 403):
                print(f"[!] Fishtoy poller: auth expired (HTTP {r.status_code}). Attempting re-auth...")
                if auth.handle_401():
                    # Rebuild session with new cookie
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


def stock_poller():
    """Poll /v1/stocks every 60s and store price history."""
    global _last_stock_poll
    if not auth.is_configured:
        return

    session = auth.get_session()

    while not _poller_stop.is_set():
        try:
            r = session.get("https://api.fishtank.live/v1/stocks", timeout=10)
            if r.status_code in (401, 403):
                print(f"[!] Stock poller: auth expired (HTTP {r.status_code}). Attempting re-auth...")
                if auth.handle_401():
                    session = auth.get_session()
                    print("[OK] Stock poller: re-auth successful.")
            elif r.status_code == 200:
                data = r.json()
                stocks = data.get("stocks", [])
                if stocks:
                    _stocks.clear()
                    _stocks.extend(stocks)
                    database.store_stock_snapshot(stocks)
                    _last_stock_poll = datetime.now(timezone.utc)
        except http_requests.RequestException:
            pass

        _poller_stop.wait(60)


def catalog_refresh_poller():
    """Refresh contestants and item catalog every 30 minutes."""
    if not auth.is_configured:
        return

    # Wait 10 minutes before first refresh (load_catalog already ran on startup)
    _poller_stop.wait(600)

    while not _poller_stop.is_set():
        session = auth.get_session()

        # Refresh item catalog
        try:
            r = session.get("https://api.fishtank.live/v1/items", timeout=10)
            if r.status_code in (401, 403):
                if auth.handle_401():
                    session = auth.get_session()
                    r = session.get("https://api.fishtank.live/v1/items", timeout=10)
            if r.status_code == 200:
                raw = r.json()
                new_count = 0
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
                old_count = len(_contestants)
                _contestants.clear()
                _contestants.extend(new_list)
                if len(_contestants) != old_count:
                    print(f"[OK] Catalog refresh: contestants updated ({old_count} -> {len(_contestants)})")
        except Exception as e:
            print(f"[WARN] Catalog refresh failed (contestants): {e}")

        _poller_stop.wait(600)  # 10 minutes


# ============================================================
# FASTAPI APP
# ============================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _loop
    _loop = asyncio.get_running_loop()

    database.init_db()

    # Load item catalog and contestants from fishtank API
    load_catalog()

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

    yield

    _poller_stop.set()
    stop_fish_client()


app = FastAPI(title="Fishtank Dashboard", lifespan=lifespan)

# CORS: configurable via ALLOWED_ORIGINS env var (comma-separated)
_allowed_origins = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _allowed_origins],
    allow_methods=["GET"],
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
    if len(browser_clients) >= MAX_WS_CLIENTS:
        await ws.close(code=1013, reason="Too many connections")
        return
    await ws.accept()
    browser_clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        browser_clients.discard(ws)


# --- REST endpoints ---


@app.get("/api/events")
def api_events(
    type: str = Query(None, description="Filter by event type (comma-separated)"),
    limit: int = Query(200, le=1000),
    since_id: int = Query(None, description="Only return events with id > this"),
):
    return database.get_events(event_type=type, limit=limit, since_id=since_id)


@app.get("/api/stats")
def api_stats(since: str = Query(None, description="ISO timestamp to filter from")):
    return database.get_stats(since=since)


@app.get("/api/status")
def api_status():
    fc = fish_client  # Local ref to avoid race condition
    return {
        "connected": fc is not None and fc.is_connected,
        "browser_clients": len(browser_clients),
        "auth_mode": auth.mode,
        "auth_configured": auth.is_configured,
    }


@app.get("/api/health")
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

    # Database health
    try:
        total_events = database.get_event_count()
        db_ok = True
    except Exception:
        total_events = 0
        db_ok = False

    # Overall status
    issues = []
    if not socket_connected:
        issues.append("socket disconnected")
    if fishtoy_age is not None and fishtoy_age > 30:
        issues.append(f"fishtoy poller stale ({fishtoy_age}s)")
    elif fishtoy_age is None and auth.is_configured:
        issues.append("fishtoy poller not started")
    if stock_age is not None and stock_age > 120:
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
    return _item_catalog


@app.get("/api/contestants")
def api_contestants():
    """Return the contestant list."""
    return _contestants


@app.get("/api/rooms")
def api_rooms():
    """Return room code -> name mapping."""
    return _room_map


@app.get("/api/stocks")
def api_stocks():
    """Return current stock data (updated by stock_poller every 60s)."""
    return _stocks


@app.get("/api/feature-toggles")
def api_feature_toggles():
    """Return current feature toggle states."""
    return _feature_toggles


@app.get("/api/stocks/history")
def api_stock_history(
    ticker: str = Query(None, description="Filter by ticker symbol"),
    limit: int = Query(500, le=5000),
):
    """Return stock price history."""
    return database.get_stock_history(ticker=ticker, limit=limit)


@app.get("/api/analytics/tts-sfx")
def api_tts_sfx_analytics(since: str = Query(None)):
    """TTS and SFX analytics: top rooms, top senders, hourly activity."""
    return database.get_tts_sfx_analytics(since=since)


@app.get("/api/analytics/chat")
def api_chat_analytics(since: str = Query(None)):
    """Chat analytics: top chatters, hourly volume."""
    return database.get_chat_analytics(since=since)


@app.get("/api/analytics/peak-hours")
def api_peak_hours():
    """Combined hourly activity across all event types with peak/quietest hours."""
    return database.get_peak_hours()


@app.get("/api/hidden-content")
def api_hidden_content(
    target: str = Query(None),
    search: str = Query(None),
    limit: int = Query(200, le=1000),
    offset: int = Query(0, ge=0),
):
    """Get fishtoy events with hidden metadata content."""
    return database.get_hidden_content(target=target, search=search, limit=limit, offset=offset)


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


@app.get("/api/fishtoys")
def api_fishtoys(
    target: str = Query(None, description="Filter by contestant target name"),
    item_id: str = Query(None, description="Filter by item ID"),
    search: str = Query(None, description="Search metadata and sender name"),
    limit: int = Query(200, le=1000),
    offset: int = Query(0, ge=0),
):
    """Get fishtoy events with optional filters."""
    return database.get_fishtoys(
        target=target, item_id=item_id, search=search,
        limit=limit, offset=offset,
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
    return database.suggest_users(prefix=q, limit=10)


@app.get("/api/stocks/count")
def api_stock_count():
    """Return actual count of stock history snapshots."""
    return {"count": database.get_stock_snapshot_count()}


@app.get("/api/polls/latest")
def api_poll_latest():
    """Return reconstructed state of the most recent poll."""
    return database.get_latest_poll_state()


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

    print("Starting Fishtank Dashboard on http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
