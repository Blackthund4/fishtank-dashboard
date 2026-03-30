"""
Fishtank Dashboard Backend

Captures fishtank.live events via two methods:
  - REST polling: /v1/items/recent for fishtoy redemptions
  - Socket.IO: real-time push for chat, TTS, SFX

Stores everything in SQLite and serves a React dashboard.

Usage:
    export FISHTANK_COOKIE='your_cookie_here'
    python server.py
"""

import asyncio
import json
import logging
import os
import types
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from threading import Thread, Event

import requests as http_requests
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

from fishclient import FishClient

import database

# ============================================================
# CONFIG
# ============================================================

COOKIE = os.environ.get("FISHTANK_COOKIE", "")

EVENTS = [
    # Fishtoys are polled via REST (/v1/items/recent), not socket events
    "tts:queued",
    "tts:update",
    "sfx:queued",
    "sfx:update",
    "chat:message",
    "happening",
]

FISHTOY_POLL_INTERVAL = 2  # seconds

# ---- Cached catalog data (loaded on startup) ----
_item_catalog = {}  # itemId -> {name, description, icon, type, ...}
_contestants = []   # [{id, name, color, photo, ...}]
_room_map = {}      # room code -> room name (e.g. "hwdn-5" -> "Hallway")
_stocks = []        # [{tickerSymbol, currentPrice, ...}]
CAPTURE_TYPES = {"FISHTOY", "BIGTOY"}

# ============================================================
# FISHCLIENT PATCHES (same as the logger script)
# ============================================================


def _patched_handle_message(self, message):
    if isinstance(message, str):
        if message.startswith("2"):
            self.websocket.send("3")
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
            _logger.info("Reconnecting...")
            try:
                self.connect()
            except Exception as err:
                _logger.error(f"Reconnect failed: {err}")
            break


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


def start_fish_client():
    """Connect to fishtank.live and register event handlers."""
    global fish_client

    if not COOKIE:
        print("[WARNING] No FISHTANK_COOKIE set. Dashboard will run but no live events.")
        return

    cookie = COOKIE.strip().strip("'\"").strip().replace("\n", "").replace("\r", "")

    fish_client = FishClient(cookie=cookie)
    fish_client.handle_message = types.MethodType(_patched_handle_message, fish_client)
    fish_client.listen = types.MethodType(_patched_listen, fish_client)

    for event_name in EVENTS:

        def make_handler(evt):
            def handler(data):
                # Store in database
                db_id = database.store_event(evt, data)

                # Log to console
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                summary = ""
                if isinstance(data, dict):
                    if "chat" in evt:
                        user = data.get("user", {})
                        name = user.get("displayName", "?") if isinstance(user, dict) else "?"
                        msg = str(data.get("message", ""))[:60]
                        summary = f"{name}: {msg}"
                    elif "tts" in evt or "sfx" in evt:
                        summary = f"{data.get('displayName', '?')}"
                    else:
                        summary = str(data)[:80]
                print(f"[{ts}] {evt}: {summary}")

                # Broadcast to browsers
                if _loop and _loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        broadcast_to_browsers(evt, data, db_id), _loop
                    )

            return handler

        fish_client.dispatcher.on(event_name)(make_handler(event_name))

    # Server disconnect/error logging
    @fish_client.dispatcher.on("disconnect")
    def on_disc(data):
        print(f"[!] Server disconnect: {data}")

    @fish_client.dispatcher.on("connect_error")
    def on_err(data):
        print(f"[!] Connection error: {data}")

    try:
        fish_client.connect()
        print("[OK] Connected to fishtank.live")
    except Exception as e:
        print(f"[ERROR] Failed to connect: {e}")
        print("Dashboard will run without live events. Fix cookie and restart.")


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
    if fish_client.socket_thread is not None:
        fish_client.socket_thread.join(timeout=3)
    fish_client = None


# ============================================================
# FISHTOY REST POLLER
# ============================================================

_poller_stop = Event()


def load_catalog():
    """Fetch item catalog, contestants, room mapping, and stocks from fishtank API."""
    global _item_catalog, _contestants, _room_map, _stocks

    if not COOKIE:
        return

    cookie = COOKIE.strip().strip("'\"").strip().replace("\n", "").replace("\r", "")
    session = http_requests.Session()
    session.cookies.set(
        "sb-wcsaaupukpdmqdjcgaoo-auth-token", cookie, domain="api.fishtank.live"
    )

    # Load item catalog
    try:
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
            _contestants.clear()
            _contestants.extend(data.get("contestants", []))
            print(f"[OK] Loaded {len(_contestants)} contestants")
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


def fishtoy_poller():
    """Poll /v1/items/recent for fishtoy redemptions."""
    if not COOKIE:
        return

    cookie = COOKIE.strip().strip("'\"").strip().replace("\n", "").replace("\r", "")
    session = http_requests.Session()
    session.cookies.set(
        "sb-wcsaaupukpdmqdjcgaoo-auth-token", cookie, domain="api.fishtank.live"
    )

    seen_ids = set()
    prev_poll_ids = []
    first_poll = True

    while not _poller_stop.is_set():
        try:
            r = session.get("https://api.fishtank.live/v1/items/recent", timeout=10)
            if r.status_code in (401, 403):
                print(f"[!] Fishtoy poller: auth expired (HTTP {r.status_code}). Restart with fresh cookie.")
                _poller_stop.wait(FISHTOY_POLL_INTERVAL)
                continue
            if r.status_code != 200:
                _poller_stop.wait(FISHTOY_POLL_INTERVAL)
                continue

            items = r.json().get("items", [])
            this_poll_ids = set()

            for item in items:
                item_id = item.get("id")
                this_poll_ids.add(item_id)

                if item_id in seen_ids:
                    continue

                if first_poll:
                    continue

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
                print(f"[{t}] {item_type or '?'}: {name} -> {target} ({item_name}){f' [{meta[:50]}]' if meta else ''}")

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
                print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Fishtoy poller: {len(items)} items in snapshot. Watching...")

        except http_requests.RequestException as e:
            print(f"[!] Fishtoy poll error: {e}")

        _poller_stop.wait(FISHTOY_POLL_INTERVAL)


def stock_poller():
    """Poll /v1/stocks every 60s and store price history."""
    if not COOKIE:
        return

    cookie = COOKIE.strip().strip("'\"").strip().replace("\n", "").replace("\r", "")
    session = http_requests.Session()
    session.cookies.set(
        "sb-wcsaaupukpdmqdjcgaoo-auth-token", cookie, domain="api.fishtank.live"
    )

    while not _poller_stop.is_set():
        try:
            r = session.get("https://api.fishtank.live/v1/stocks", timeout=10)
            if r.status_code == 200:
                data = r.json()
                stocks = data.get("stocks", [])
                if stocks:
                    _stocks.clear()
                    _stocks.extend(stocks)
                    database.store_stock_snapshot(stocks)
        except http_requests.RequestException:
            pass

        _poller_stop.wait(60)


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

    # Start fishclient (Socket.IO for chat/TTS/SFX) in background thread
    Thread(target=start_fish_client, daemon=True).start()

    # Start fishtoy REST poller in background thread
    Thread(target=fishtoy_poller, daemon=True).start()

    # Start stock price history poller in background thread
    Thread(target=stock_poller, daemon=True).start()

    yield

    _poller_stop.set()
    stop_fish_client()


app = FastAPI(title="Fishtank Dashboard", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- WebSocket for live browser updates ---


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    browser_clients.add(ws)
    try:
        while True:
            # Keep connection alive, ignore incoming messages
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
def api_stats():
    return database.get_stats()


@app.get("/api/status")
def api_status():
    return {
        "connected": fish_client is not None and fish_client.is_connected,
        "browser_clients": len(browser_clients),
        "cookie_set": bool(COOKIE),
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
    """Return current stock data. Refreshes from API on each call."""
    if not COOKIE:
        return _stocks

    cookie = COOKIE.strip().strip("'\"").strip().replace("\n", "").replace("\r", "")
    session = http_requests.Session()
    session.cookies.set(
        "sb-wcsaaupukpdmqdjcgaoo-auth-token", cookie, domain="api.fishtank.live"
    )
    try:
        r = session.get("https://api.fishtank.live/v1/stocks", timeout=10)
        if r.status_code == 200:
            data = r.json()
            _stocks.clear()
            _stocks.extend(data.get("stocks", []))
    except Exception:
        pass
    return _stocks


@app.get("/api/stocks/history")
def api_stock_history(
    ticker: str = Query(None, description="Filter by ticker symbol"),
    limit: int = Query(500, le=5000),
):
    """Return stock price history."""
    return database.get_stock_history(ticker=ticker, limit=limit)


@app.get("/api/analytics/tts-sfx")
def api_tts_sfx_analytics():
    """TTS and SFX analytics: top rooms, top senders, hourly activity."""
    return database.get_tts_sfx_analytics()


@app.get("/api/analytics/chat")
def api_chat_analytics():
    """Chat analytics: top chatters, hourly volume."""
    return database.get_chat_analytics()


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


# --- Serve frontend static files ---

FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"

if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def serve_frontend(full_path: str):
        file_path = FRONTEND_DIST / full_path
        if file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(FRONTEND_DIST / "index.html")


# ============================================================
# ENTRYPOINT
# ============================================================

if __name__ == "__main__":
    import uvicorn

    if not COOKIE:
        print("=" * 60)
        print("  WARNING: FISHTANK_COOKIE not set")
        print("  Set it to enable live event streaming:")
        print("    export FISHTANK_COOKIE='your_cookie_here'  (Linux/Mac)")
        print("    $env:FISHTANK_COOKIE = 'your_cookie'       (PowerShell)")
        print("=" * 60)

    print("Starting Fishtank Dashboard on http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
