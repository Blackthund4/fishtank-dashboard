"""
Fishtank Dashboard API Server

Read-only API server for the React dashboard. Serves REST endpoints,
WebSocket live updates, and static files. Reads events from SQLite
(written by ingest.py) and catalog/status from _shared_state.json.

Usage:
    python server.py
"""

import asyncio
import logging
import os
import time as _time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from threading import Thread, Lock

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pathlib import Path

import database
import shared_state
from database import fast_loads, fast_dumps

# ============================================================
# LOGGING
# ============================================================

# Single structured logger for the API process. Uvicorn's access log is left
# at log_level="warning" (see uvicorn.run below) to avoid double-logging; this
# logger replaces it with a one-line-per-request access log plus explicit
# exception logging.
logger = logging.getLogger("fishtank.api")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    # Propagate to root so pytest caplog and other test hooks can observe
    # records. Uvicorn does not attach handlers to root by default, so this
    # does not cause duplicate output in production.
    logger.propagate = True


# ============================================================
# CONFIG
# ============================================================

CAPTURE_TYPES = {"FISHTOY", "BIGTOY"}

_SHARED_STATE_PATH = os.environ.get(
    "FISHTANK_SHARED_STATE",
    str(Path(__file__).parent / "_shared_state.json"),
)

# ============================================================
# ANALYTICS CACHE
# ============================================================

_analytics_cache = {}  # key -> (timestamp, data)
CACHE_TTL = 60  # seconds
CACHE_MAX_ENTRIES = 50  # max cache entries before pruning


CACHE_TTL_CHAT = 180  # seconds -- chat analytics/sentiment/charts are expensive GROUP BY queries


def _cached_query(key, func, *args, ttl=None):
    """Return cached result if fresh, otherwise run func and cache."""
    effective_ttl = ttl or CACHE_TTL
    now = _time.time()
    if key in _analytics_cache:
        cached_at, data = _analytics_cache[key]
        if now - cached_at < effective_ttl:
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


def _get_client_ip(scope_obj) -> str:
    """Return the real client IP for a Request or WebSocket.

    Uvicorn rewrites ``request.client.host`` to the X-Forwarded-For value when
    started with ``proxy_headers=True`` and ``forwarded_allow_ips="*"``. On the
    production VPS, UFW restricts port 443 ingress to published Cloudflare IP
    ranges, so the XFF header cannot be forged by an untrusted peer.
    """
    client = getattr(scope_obj, "client", None)
    if client and client.host:
        return client.host
    return "unknown"


# ============================================================
# BROWSER WEBSOCKET CLIENTS
# ============================================================

_clients_lock = Lock()
browser_clients: set[WebSocket] = set()

# Per-IP WebSocket connection cap — prevents one IP from exhausting the
# global 50-client pool. Uses the XFF-corrected IP from _get_client_ip.
MAX_WS_PER_IP = 3
_ws_ip_counts: dict = defaultdict(int)


def _is_origin_allowed(origin) -> bool:
    """Return True iff the WebSocket Origin header matches _allowed_origins.

    WebSockets are not subject to the browser same-origin policy, so without
    an Origin check any page on the internet can open ``wss://fish-dash.com/ws``
    and receive live events (CSWSH). Browsers always send Origin on WS
    handshakes; a missing Origin indicates a non-browser client, which this
    read-only public dashboard does not need to support and we reject to stay
    strict.
    """
    if not origin:
        return False
    if "*" in _allowed_origins:
        return True
    return origin in _allowed_origins


def _try_reserve_ws_slot(ip: str) -> tuple[bool, str]:
    """Atomically reserve a WebSocket slot for ``ip``.

    Returns ``(True, "ok")`` on success — caller MUST call
    ``_release_ws_slot(ip)`` when the connection closes. Returns
    ``(False, reason)`` on failure with one of ``"global"`` (MAX_WS_CLIENTS
    pool exhausted) or ``"per-ip"`` (this IP already has MAX_WS_PER_IP
    connections).
    """
    with _clients_lock:
        if len(browser_clients) >= MAX_WS_CLIENTS:
            return (False, "global")
        if _ws_ip_counts[ip] >= MAX_WS_PER_IP:
            return (False, "per-ip")
        _ws_ip_counts[ip] += 1
        return (True, "ok")


def _release_ws_slot(ip: str) -> None:
    """Release a previously-reserved WebSocket slot for ``ip``."""
    with _clients_lock:
        _ws_ip_counts[ip] -= 1
        if _ws_ip_counts[ip] <= 0:
            _ws_ip_counts.pop(ip, None)


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
    message = fast_dumps({"event_type": event_type, "data": data, "db_id": db_id})
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
# NOTIFY POLLER
# ============================================================


async def _notify_poller():
    """Poll _notify table every 200ms and broadcast new events to browser WebSockets."""
    last_seen_id = 0
    while True:
        try:
            rows = database.poll_notify(last_seen_id)
            for row in rows:
                last_seen_id = row["id"]
                evt = database.get_event_by_id(row["event_id"])
                if evt:
                    await broadcast_to_browsers(
                        row["event_type"],
                        evt["data"],
                        evt["id"],
                    )
        except Exception as e:
            print(f"[WARN] Notify poller error: {e}")
        await asyncio.sleep(0.2)


# ============================================================
# FASTAPI APP
# ============================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Idempotent schema ensure — safe if ingestion already created the tables.
    # On split-architecture the sole schema owner is ingest.py, but we keep
    # this defensive call to guard against api starting before ingest on a
    # fresh volume.
    database.init_db()

    # Flip to read-only SQLite mode for the rest of this process's lifetime.
    # Defense-in-depth: even a bug or exploit path cannot write through the
    # API's database handles. Must be called AFTER init_db (which writes
    # DDL) and BEFORE any request handler opens a thread-local connection.
    # Backfill lives on ingest.py; this process does not perform migrations.
    database.enable_readonly()

    # Pre-warm analytics cache so first browser connect doesn't trigger a CPU spike
    def _warm_caches():
        _time.sleep(5)  # Let ingestion threads stabilize first
        warmup = [
            ("stats:None", database.get_stats, (None,), None),
            ("targets", database.get_targets, (), None),
            ("peak-hours", database.get_peak_hours, (), None),
            ("tts-sfx:None:None", database.get_tts_sfx_analytics, (None, None), None),
            ("chat:None:None", database.get_chat_analytics, (None, None), CACHE_TTL_CHAT),
        ]
        for key, fn, args, ttl in warmup:
            try:
                _cached_query(key, fn, *args, ttl=ttl)
            except Exception:
                pass
        print("[OK] Analytics cache pre-warmed")
    Thread(target=_warm_caches, daemon=True).start()

    # Keep browser WebSocket connections alive through Cloudflare's 100s idle timeout
    ping_task = asyncio.create_task(_ws_ping_loop())

    # Poll _notify table for new events from ingestion process
    notify_task = asyncio.create_task(_notify_poller())

    yield

    ping_task.cancel()
    notify_task.cancel()


app = FastAPI(title="Fishtank Dashboard", lifespan=lifespan)


def _parse_allowed_origins(raw: str) -> list[str]:
    """Parse a comma-separated ALLOWED_ORIGINS env value into a clean list.

    Fail-closed: unset, blank, or whitespace-only → ``[]`` (rejects every
    cross-origin request). Previously defaulted to ``"*"`` which was silently
    insecure if the env var was forgotten.
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    return [o.strip() for o in raw.split(",") if o.strip()]


# CORS: configurable via ALLOWED_ORIGINS env var (comma-separated).
# Default is empty list — fail closed. Production sets this explicitly via
# docker-compose.yml. An unset value logs a startup warning.
_allowed_origins = _parse_allowed_origins(os.environ.get("ALLOWED_ORIGINS", ""))
if not _allowed_origins:
    logger.warning(
        "ALLOWED_ORIGINS is unset or empty — CORS will reject all "
        "cross-origin requests. Set ALLOWED_ORIGINS to a comma-separated "
        "list of origins (e.g. https://fish-dash.com) in production."
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "HEAD"],
    allow_headers=["content-type"],
    max_age=3600,
)


# Rate limiting middleware
@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    # Skip rate limiting for static files
    if request.url.path.startswith("/assets") or request.url.path == "/":
        return await call_next(request)

    ip = _get_client_ip(request)
    if _check_rate_limit(ip):
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many requests. Try again later."},
        )

    # Periodic cleanup
    if len(_rate_limits) > 200:
        _prune_rate_limits()

    return await call_next(request)


# ============================================================
# SECURITY HEADERS
# ============================================================

# Emit HSTS only when TLS is terminated at this process. Behind Cloudflare in
# production the api process serves HTTPS via SSL_CERTFILE; locally it serves
# plain HTTP and HSTS would poison the browser cache.
_HAS_SSL = bool(os.environ.get("SSL_CERTFILE"))

# Content-Security-Policy (Report-Only for now — flip to enforcing in a
# follow-up once live-site violations have been observed).
#   script-src 'self'       — requires the SW registration in index.html to be
#                             an external file, not inline.
#   style-src   'unsafe-inline' is unavoidable: React inline styles and
#                             recharts emit inline style attributes.
#   connect-src wss: https: — WebSocket upgrade + future cross-origin fetches.
_CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data: https://cdn.fishtank.live https://fishtank.b-cdn.net https://cdn2.mondomegabits.com; "
    "connect-src 'self' wss: https:; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "object-src 'none'"
)


# Registered after rate_limit_middleware so it runs *outer* — it stamps
# responses from the rate limiter (429) and the exception handler (500) too.
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), interest-cohort=()",
    )
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    h.setdefault("Content-Security-Policy-Report-Only", _CSP_POLICY)
    if _HAS_SSL:
        h.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return response


# Access log middleware — registered last so it runs OUTERMOST and captures
# the final stamped response (including rate-limit 429s and exception 500s).
# Emits a single line per request to the fishtank.api logger.
@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    start = _time.perf_counter()
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception:
        # The generic_exception_handler will run after this and turn it into
        # a 500 response, but call_next raised, so we only see the exception
        # here. Log the access line with status=500 and re-raise.
        status = 500
        raise
    finally:
        duration_ms = (_time.perf_counter() - start) * 1000.0
        logger.info(
            "%s %s %s %.1fms %s",
            request.method,
            request.url.path,
            status,
            duration_ms,
            _get_client_ip(request),
        )
    return response


# Generic exception handler — returns a neutral 500 to the client but logs
# the full traceback via fishtank.api so issues are actually visible in
# docker logs (previously the exception was silently swallowed).
# Uses exc_info=exc (the exception instance) instead of logger.exception()
# so the traceback is captured from the passed object rather than the
# ambient sys.exc_info(), which may be empty when called from non-handler
# contexts (e.g. unit tests).
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error(
        "Unhandled exception handling %s %s",
        request.method,
        request.url.path,
        exc_info=exc,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# --- WebSocket for live browser updates ---


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # CSWSH defense — reject cross-origin WebSocket handshakes. Browsers
    # always send Origin on WS; missing Origin is treated as non-browser.
    origin = ws.headers.get("origin")
    if not _is_origin_allowed(origin):
        await ws.close(code=1008, reason="Origin not allowed")
        return

    ip = _get_client_ip(ws)
    ok, reason = _try_reserve_ws_slot(ip)
    if not ok:
        await ws.close(code=1013, reason=f"Too many connections ({reason})")
        return

    try:
        await ws.accept()
    except Exception:
        _release_ws_slot(ip)
        raise

    with _clients_lock:
        browser_clients.add(ws)

    try:
        state = shared_state.read_state(_SHARED_STATE_PATH)
        await ws.send_text(fast_dumps({"event_type": "server:hello", "data": {"version": BUILD_VERSION, "chatRoom": state.get("chat_room", "")}}))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        with _clients_lock:
            browser_clients.discard(ws)
        _release_ws_slot(ip)


# --- REST endpoints ---


@app.get("/api/events")
def api_events(
    type: str = Query(None, description="Filter by event type (comma-separated)"),
    limit: int = Query(200, le=1000),
    since_id: int = Query(None, description="Only return events with id > this"),
    before_id: int = Query(None, description="Only return events with id < this (keyset pagination)"),
    target: str = Query(None, description="Filter by contestant target name"),
    item_id: str = Query(None, description="Filter by item ID"),
    search: str = Query(None, description="Search metadata and sender name"),
    around_ts: str = Query(None, description="Jump to events around this ISO timestamp"),
    role: str = Query(None, description="Filter chat by role: admin, mod, fish, gm, epic"),
):
    # Cache role-filtered chat queries (initial load only, not paginated)
    if role and not since_id and not before_id and not search and not around_ts:
        return _cached_query(
            f"events-role:{type}:{role}:{limit}",
            database.get_events,
            type, limit, None, None, target, item_id, None, None, role,
        )
    return database.get_events(event_type=type, limit=limit, since_id=since_id, before_id=before_id,
                               target=target, item_id=item_id, search=search, around_ts=around_ts, role=role)


@app.get("/api/stats")
def api_stats(since: str = Query(None, description="ISO timestamp to filter from")):
    since = _normalize_since(since)
    return _cached_query(f"stats:{since}", database.get_stats, since)


@app.get("/api/status")
def api_status():
    state = shared_state.read_state(_SHARED_STATE_PATH)
    return {
        "connected": state.get("socket_connected_at") is not None,
        "browser_clients": len(browser_clients),
        "fishtank_online": state.get("fishtank_online", 0),
        "auth_mode": state.get("auth_mode", "unknown"),
        "auth_configured": state.get("auth_configured", False),
    }


@app.api_route("/api/health", methods=["GET", "HEAD"])
def api_health():
    """Health check for monitoring. Sensitive details omitted."""
    now = datetime.now(timezone.utc)
    state = shared_state.read_state(_SHARED_STATE_PATH)

    # Socket health (derived from shared state)
    socket_connected_at_str = state.get("socket_connected_at")
    socket_connected = socket_connected_at_str is not None
    socket_uptime = None
    if socket_connected_at_str:
        try:
            socket_connected_at = datetime.fromisoformat(socket_connected_at_str)
            socket_uptime = int((now - socket_connected_at).total_seconds())
        except (ValueError, TypeError):
            pass

    # Poller health (derived from shared state)
    fishtoy_age = None
    last_fishtoy_str = state.get("last_fishtoy_poll")
    if last_fishtoy_str:
        try:
            fishtoy_age = int((now - datetime.fromisoformat(last_fishtoy_str)).total_seconds())
        except (ValueError, TypeError):
            pass
    stock_age = None
    last_stock_str = state.get("last_stock_poll")
    if last_stock_str:
        try:
            stock_age = int((now - datetime.fromisoformat(last_stock_str)).total_seconds())
        except (ValueError, TypeError):
            pass

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

    # Backup health (derived from shared state)
    last_backup_str = state.get("last_backup")

    # Shared state freshness
    updated_at_str = state.get("updated_at")
    state_age = None
    if updated_at_str:
        try:
            state_age = int((now - datetime.fromisoformat(updated_at_str)).total_seconds())
        except (ValueError, TypeError):
            pass

    # Overall status
    auth_configured = state.get("auth_configured", False)
    issues = []
    if state_age is not None and state_age > 120:
        issues.append(f"shared state stale ({state_age}s)")
    if not socket_connected:
        issues.append("socket disconnected")
    if fishtoy_age is not None and fishtoy_age > 30:
        issues.append(f"fishtoy poller stale ({fishtoy_age}s)")
    elif fishtoy_age is None and auth_configured:
        issues.append("fishtoy poller not started")
    if stock_age is not None and stock_age > 90:
        issues.append(f"stock poller stale ({stock_age}s)")
    elif stock_age is None and auth_configured:
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
            "last_backup": last_backup_str,
        },
        "browser_clients": len(browser_clients),
        "checked_at": now.isoformat(),
    }


@app.get("/api/items")
def api_items():
    """Return the item catalog (itemId -> name/description/icon)."""
    state = shared_state.read_state(_SHARED_STATE_PATH)
    return state.get("item_catalog", {})


@app.get("/api/contestants")
def api_contestants():
    """Return the contestant list."""
    state = shared_state.read_state(_SHARED_STATE_PATH)
    return state.get("contestants", [])


@app.get("/api/rooms")
def api_rooms():
    """Return room code -> name mapping."""
    state = shared_state.read_state(_SHARED_STATE_PATH)
    return state.get("room_map", {})


@app.get("/api/stocks")
def api_stocks():
    """Return current stock data (updated by stock_poller every 60s)."""
    state = shared_state.read_state(_SHARED_STATE_PATH)
    return state.get("stocks", [])


@app.get("/api/feature-toggles")
def api_feature_toggles():
    """Return current feature toggle states."""
    state = shared_state.read_state(_SHARED_STATE_PATH)
    return state.get("feature_toggles", {})


@app.get("/api/stocks/history")
def api_stock_history(
    ticker: str = Query(None, description="Filter by ticker symbol"),
    limit: int = Query(500, le=5000),
    since: str = Query(None, description="ISO timestamp to filter history from"),
):
    """Return stock price history."""
    return database.get_stock_history(ticker=ticker, limit=limit, since=since)


def _normalize_since(since):
    """Truncate since to minute precision for cache key dedup."""
    if since and len(since) > 16:
        return since[:16] + ":00"
    return since


@app.get("/api/analytics/tts-sfx")
def api_tts_sfx_analytics(since: str = Query(None), until: str = Query(None)):
    """TTS and SFX analytics: top rooms, top senders, hourly activity."""
    since = _normalize_since(since)
    until = _normalize_since(until)
    return _cached_query(f"tts-sfx:{since}:{until}", database.get_tts_sfx_analytics, since, until)


@app.get("/api/analytics/chat")
def api_chat_analytics(since: str = Query(None), until: str = Query(None)):
    """Chat analytics: top chatters, hourly volume."""
    since = _normalize_since(since)
    until = _normalize_since(until)
    return _cached_query(f"chat:{since}:{until}", database.get_chat_analytics, since, until, ttl=CACHE_TTL_CHAT)


@app.get("/api/analytics/chat-sentiment")
def api_chat_sentiment(since: str = Query(None), until: str = Query(None)):
    """Chat sentiment analytics: overall mood, hourly breakdown."""
    since = _normalize_since(since)
    until = _normalize_since(until)
    return _cached_query(f"chat-sentiment:{since}:{until}", database.get_chat_sentiment, since, until, ttl=CACHE_TTL_CHAT)


@app.get("/api/analytics/tts-sentiment")
def api_tts_sentiment(since: str = Query(None), until: str = Query(None)):
    """TTS sentiment analytics: overall mood, hourly breakdown, mood by contestant."""
    since = _normalize_since(since)
    until = _normalize_since(until)
    return _cached_query(f"tts-sentiment:{since}:{until}", database.get_tts_sentiment, since, until)


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
    state = shared_state.read_state(_SHARED_STATE_PATH)
    item_catalog = state.get("item_catalog", {})
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
        for v in item_catalog.values()
        if v.get("type") in CAPTURE_TYPES
    ]


@app.get("/api/superchats")
def api_superchats(
    limit: int = Query(50, le=500),
    since: str = Query(None, description="ISO timestamp to filter from"),
):
    """Get superchat events with deletion status."""
    since = _normalize_since(since)
    return _cached_query(f"superchats:{since}", database.get_superchats, limit, since)


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
    return _cached_query(f"notifications:{limit}", database.get_notifications, limit)


@app.get("/api/price-changes")
def api_price_changes(limit: int = Query(100, le=500)):
    """Get TTS/SFX price change history."""
    return database.get_price_changes(limit=limit)


@app.get("/api/user/{username}")
def api_user_search(username: str, limit: int = Query(500, le=2000), before_id: int = Query(None)):
    """Search all event types for a specific user."""
    return database.search_user(username=username, limit=limit, before_id=before_id)


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


@app.get("/api/stocks/sparklines")
def api_stock_sparklines(range: str = Query('today')):
    """Return price arrays per ticker for sparkline rendering."""
    if range not in {'1h', '3h', '12h', 'today', '3d', '1w', 'ipo'}:
        range = 'today'
    return _cached_query(f"stock-sparklines:{range}", database.get_stock_sparklines, range)


@app.get("/api/charts/stocks")
def api_charts_stocks(range: str = Query('24h'), anchor: str = Query(None)):
    if range not in {'30m', '1h', '2h', '6h', '12h', '24h', '3d', '7d', 'all'}:
        range = '24h'
    anchor = _normalize_since(anchor)
    return _cached_query(f"charts-stocks:{range}:{anchor}", database.get_stock_history_chart, range, anchor)


@app.get("/api/charts/spend")
def api_charts_spend(range: str = Query('24h'), anchor: str = Query(None)):
    if range not in {'30m', '1h', '2h', '6h', '12h', '24h', '3d', '7d', 'all'}:
        range = '24h'
    anchor = _normalize_since(anchor)
    return _cached_query(f"charts-spend:{range}:{anchor}", database.get_spend_trends, range, anchor)


@app.get("/api/charts/chatters")
def api_charts_chatters(range: str = Query('24h'), anchor: str = Query(None)):
    if range not in {'30m', '1h', '2h', '6h', '12h', '24h', '3d', '7d', 'all'}:
        range = '24h'
    anchor = _normalize_since(anchor)
    return _cached_query(f"charts-chatters:{range}:{anchor}", database.get_chat_chart, range, anchor, ttl=CACHE_TTL_CHAT)


# --- Serve frontend static files ---

FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"

if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    # Cache hashed assets for 1 year; they're content-addressed so safe to cache forever
    @app.middleware("http")
    async def cache_static_assets(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    @app.get("/{full_path:path}")
    def serve_frontend(full_path: str):
        file_path = (FRONTEND_DIST / full_path).resolve()
        # Prevent path traversal outside dist directory
        if not str(file_path).startswith(str(FRONTEND_DIST.resolve())):
            return FileResponse(FRONTEND_DIST / "index.html",
                                headers={"Cache-Control": "no-cache"})
        if file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(FRONTEND_DIST / "index.html",
                            headers={"Cache-Control": "no-cache"})


# ============================================================
# ENTRYPOINT
# ============================================================

if __name__ == "__main__":
    import uvicorn

    ssl_keyfile = os.environ.get("SSL_KEYFILE")
    ssl_certfile = os.environ.get("SSL_CERTFILE")
    scheme = "https" if ssl_certfile else "http"
    print(f"Starting Fishtank Dashboard API on {scheme}://localhost:8000")
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="warning",
        ssl_keyfile=ssl_keyfile or None,
        ssl_certfile=ssl_certfile or None,
        # Behind Cloudflare: UFW restricts port 443 ingress to CF IP ranges, so
        # trusting X-Forwarded-For from any peer is safe. Without this, the
        # per-IP rate limiter sees every request as coming from a CF edge IP.
        proxy_headers=True,
        forwarded_allow_ips="*",
        # Browsers only send tiny keepalive frames on /ws (the endpoint just
        # awaits receive_text() to detect disconnect). Cap inbound frame size
        # to 4 KB to bound memory under a connection-level flood.
        ws_max_size=4096,
    )
