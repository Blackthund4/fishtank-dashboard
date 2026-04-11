# Proposal: Separate Ingestion from API Server

*Written 2026-04-06. Revised 2026-04-09. For future reference -- not yet implemented.*

## Problem

The dashboard runs as a single FastAPI process (`server.py`) handling both event ingestion (Socket.IO + REST polling) and serving (REST API + WebSocket + static files). This means:

- **Any deploy kills ingestion** -- rebuilding the container stops Socket.IO and REST pollers, causing unrecoverable event gaps
- **No fault isolation** -- a bad analytics query or stuck fishclient callback crashes everything
- **OOM risk** -- ingestion, API, npm build, and index creation all compete for 1.5GB

**Current status:** proactive improvement. The single-process model works at current scale but this is insurance against the failure modes that hurt most (lost events during deploys, cascading crashes).

## Proposed Architecture

Split into two containers sharing one SQLite database via a Docker named volume:

```
                  +--------------------+
fishtank.live --> |   ingestion        | --> SQLite (sole writer)
                  |   (ingest.py)      | --> _shared_state.json
                  +--------------------+     token_cache.json
                           |
                      _notify table
                           |
                  +--------------------+
browsers <------> |   api              | <-- SQLite (reader only)
                  |   (server.py)      | <-- _shared_state.json
                  +--------------------+
```

- **`ingest.py`** -- headless Python process running all 5 daemon threads (Socket.IO reconnect loop, fishtoy poller, stock poller, catalog refresh, DB backup) + auth. Sole DB writer. Sole owner of fishtank.live authentication.
- **`server.py`** (slimmed) -- FastAPI serving REST, WebSocket fan-out, static files. DB reader only. No auth.py import, no fishtank.live credentials. If API-side auth is needed in the future, re-add it then.

## The Hard Problem: WebSocket Fan-out

Currently `broadcast_to_browsers()` is called in-process by ingestion threads. With separate processes, the API server holds the WS connections but the ingestion worker has the events.

**Chosen solution: SQLite notify table + polling.**

The ingestion worker inserts a row into a lightweight `_notify` table after each `store_event()`. The API server polls this table every 200ms (`SELECT id, event_id, event_type FROM _notify WHERE id > ?`). When new rows appear, it fetches the full event and broadcasts to browser WS clients.

Why this over alternatives:
- **Redis pub/sub** -- adds a dependency and memory overhead. Overkill for this scale.
- **Unix socket / TCP** -- requires custom protocol and reconnection logic.
- **Filesystem inotify** -- platform-dependent, unreliable on Docker overlayfs.
- **SQLite notify table** -- zero new dependencies, naturally durable, ~200ms latency (imperceptible for a dashboard).

### Notify Table Schema

```sql
CREATE TABLE IF NOT EXISTS _notify (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
```

### Notify Table Pruning

**Owner: ingestion process.** After each insert, ingestion deletes rows older than 60 seconds:

```sql
DELETE FROM _notify WHERE created_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-60 seconds');
```

This keeps the table small (at peak ~300 events/min = ~300 rows max) without requiring coordination with the API server. If the API server is down or behind, it loses events older than 60s, which is acceptable since it would trigger a full data re-fetch on reconnect anyway.

## Shared State

The API currently serves catalog/stock/toggle endpoints from in-memory caches populated by ingestion threads. With separate processes, the ingestion worker writes an atomic JSON file (`_shared_state.json`) after each catalog refresh or stock update.

```json
{
  "updated_at": "2026-04-06T12:00:00Z",
  "item_catalog": {},
  "contestants": [],
  "room_map": {},
  "stocks": [],
  "feature_toggles": {},
  "socket_connected_at": "2026-04-06T11:55:00Z",
  "fishtank_online": 42,
  "auth_mode": "auto",
  "auth_configured": true,
  "last_fishtoy_poll": "2026-04-06T11:59:55Z",
  "last_stock_poll": "2026-04-06T11:59:30Z",
  "last_backup": "2026-04-06T06:00:00Z",
  "chat_room": "hwdn-5"
}
```

Field notes:
- `socket_connected_at` -- ISO timestamp or `null`. Replaces the boolean `socket_connected` from the original proposal. The API derives both the connected boolean (`!= null`) and uptime (`now - socket_connected_at`) from this single field. Set to `null` when Socket.IO disconnects.
- `auth_configured` -- needed by `/api/status` since the API no longer imports `auth.py`.
- `chat_room` -- current chat room code. The WebSocket hello message sends this to browsers on connect. Updated by ingestion when `chat:room` socket events arrive. Note: `chat:room` and `chat:presence` events do NOT go through the `_notify` table because they never hit the database. They flow exclusively through shared state. This means room changes have ~5s latency to browsers (vs 200ms for DB-backed events), which is acceptable since room changes are rare.
- `last_backup` -- needed by `/api/health` to report backup recency. Previously read from the in-process `_last_backup` global.
- `last_fishtoy_poll`, `last_stock_poll` -- ISO timestamps or `null`. The API health endpoint computes staleness ages from these.

Atomic write: write to `.tmp`, then `os.replace()`.

### State Refresh Strategy

The API server uses **mtime-based refresh**: on each access, `os.stat()` the file and only re-read + parse if mtime has changed since last load. This avoids unnecessary I/O while keeping latency low for fast-changing fields like `socket_connected`. In practice, ingestion writes this file every 5-60s depending on which poller triggers, so mtime checks are cheap and the data stays reasonably fresh.

## Auth Ownership

**Ingestion is the sole owner of fishtank.live authentication.** It runs `auth.py`, manages `token_cache.json`, and handles 401 re-auth flows. The API server never contacts fishtank.live and has no auth credentials.

This means:
- `auth.py` is imported only by `ingest.py`
- `FISHTANK_EMAIL`, `FISHTANK_PASSWORD`, `FISHTANK_TOKEN_CACHE` are only in the ingestion container's environment
- No risk of two processes racing to refresh the same token
- `server.py` can drop the `auth` import entirely

## WAL Safety

SQLite runs in WAL mode with two processes accessing the same database file via a Docker named volume on ext4/xfs.

**Risk:** long-running analytics queries on the API side hold open read transactions, which prevent WAL checkpointing by the ingestion side. This causes the WAL file to grow unbounded.

**Mitigation:** set `PRAGMA busy_timeout = 5000` on the API server's database connection. This gives the reader 5 seconds to retry if the writer is holding a lock, and prevents immediate SQLITE_BUSY errors. Combined with the existing hourly `PRAGMA wal_checkpoint(PASSIVE)` on the ingestion side, this should keep WAL size manageable.

If WAL growth becomes a problem in practice, the next step would be enforcing short read transactions on the API side (e.g., ensuring all queries use short-lived cursors and no connection holds a transaction open across async awaits).

## Docker Compose

```yaml
services:
  ingestion:
    build:
      context: .
      dockerfile: Dockerfile.ingest     # Python-only, no Node/frontend
    volumes:
      - fishtank-data:/app/data
    environment:
      - FISHTANK_DB_PATH=/app/data/fishtank.db
      - FISHTANK_TOKEN_CACHE=/app/data/token_cache.json
      - FISHTANK_SHARED_STATE=/app/data/_shared_state.json
      - FISHTANK_EMAIL=${FISHTANK_EMAIL}
      - FISHTANK_PASSWORD=${FISHTANK_PASSWORD}
    mem_limit: 1g
    cpus: 1.0
    restart: unless-stopped

  api:
    build:
      context: .
      dockerfile: Dockerfile.api        # Full image with frontend build
      args:
        GIT_COMMIT: ${GIT_COMMIT:-unknown}
    ports:
      - "443:8000"
    volumes:
      - fishtank-data:/app/data
      - certs:/app/certs:ro
    environment:
      - FISHTANK_DB_PATH=/app/data/fishtank.db
      - FISHTANK_SHARED_STATE=/app/data/_shared_state.json
      - ALLOWED_ORIGINS=${ALLOWED_ORIGINS}
      - SSL_CERTFILE=${SSL_CERTFILE}
      - SSL_KEYFILE=${SSL_KEYFILE}
    mem_limit: 2g
    cpus: 1.5
    healthcheck:
      test: ["CMD", "curl", "-f", "https://localhost:8000/api/health", "--insecure"]
      interval: 30s
      timeout: 5s
      retries: 3
    restart: unless-stopped

volumes:
  fishtank-data:
  certs:
```

Memory budget: 1GB ingestion + 2GB API = 3GB total. Comfortable on an 8GB VPS with headroom for OS, Docker overhead, and npm build spikes.

## Deploy Workflow Changes

```bash
# API-only deploy (frontend/API changes -- ZERO ingestion gap):
GIT_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build api

# Full deploy (ingestion changes -- brief event gap):
GIT_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build

# Ingestion-only (rare):
docker compose up -d --build ingestion
```

This is the primary win -- most deploys are frontend/API changes that no longer interrupt ingestion.

### Log Aggregation

With two containers, debugging changes slightly:

```bash
# Combined (interleaved, harder to read):
docker compose logs -f

# Per-service (preferred):
docker compose logs -f ingestion
docker compose logs -f api
```

Both containers should prefix log lines with a service identifier (e.g., `[ingest]` / `[api]`) so combined logs remain parseable.

## Implementation Plan (Phased)

### Phase 1: Shared State Module

Extract `shared_state.py` (~40 lines). Atomic JSON write/read with mtime-based caching. Can be tested independently. Merge to `main` -- the module is inert until `ingest.py` calls it.

| File | Action |
|------|--------|
| `backend/shared_state.py` | **New** -- `write_state()`, `read_state()` with mtime cache |

### Phase 2: Notify Table + Database Changes

Add the `_notify` table, insert-after-store function, poll function, and prune-on-insert logic to `database.py`. Add `busy_timeout` pragma to reader connections. Merge to `main` -- additive schema, no behavior change yet.

| File | Action |
|------|--------|
| `backend/database.py` | Add `_notify` table, `notify_new_event()`, `poll_notify()`, prune logic, `busy_timeout` pragma |

### Phase 3: Extract Ingestion

Create `ingest.py` by extracting all 5 background threads, Socket.IO handling, auth, and replacing `broadcast_to_browsers()` with `notify_new_event()`. Strip the same code from `server.py`, add the notify poller loop and shared state loader. This is the big change.

#### What moves to `ingest.py`:

**Functions (extracted from server.py):**
- `reconnect_loop()` -- Socket.IO connection manager with backoff
- `make_event_handler(evt)` -- core event processor (121 lines), all socket event handling
- `_patched_handle_message()`, `_patched_listen()` -- fishclient patches
- `fishtoy_poller()` -- REST polling `/v1/items/recent` every 5s
- `stock_poller()` -- REST polling `/v1/stocks` every 60s
- `catalog_refresh_poller()` -- refresh items/contestants every 10 min
- `db_backup_poller()` -- SQLite backup every 6h, WAL checkpoint hourly, prune old data
- `load_catalog()` -- startup catalog/stock/room/toggle fetch
- `seed_superchats_from_rest()` -- startup superchat backfill
- `_backfill_empty_sc_names()` -- startup superchat name resolution
- `_seed_poll_vote_state()` -- startup poll state init
- `stop_fish_client()` -- shutdown handler
- `_is_duplicate()`, `_should_filter_chat()`, `_should_filter_notification()` -- event filters
- `_track_feature_toggle()` -- toggle state tracking
- `_score_sentiment()`, `_get_analyzer()` -- VADER sentiment (lazy-loaded)
- `_normalize_poll_vote()` -- poll vote format normalization
- `_fetch_user_profile()` -- profile API lookup with cache

**Globals that move:**
- `fish_client`, `_socket_connected_at` -- socket state
- `_poller_stop` -- thread stop signal
- `_last_fishtoy_poll`, `_last_stock_poll`, `_last_backup` -- health tracking
- `_fishtank_online`, `_chat_room` -- live state from socket events
- `_feature_toggles` -- toggle state
- `_poll_vote_state` -- poll normalization (internal only)
- `_dedup_lock`, `_seen_tts_sfx_ids` -- TTS/SFX dedup
- `_profile_cache`, `_PROFILE_CACHE_TTL`, `_PROFILE_CACHE_MAX` -- profile cache
- `_sentiment_analyzer` -- VADER instance
- `_catalog_lock`, `_item_catalog`, `_contestants`, `_room_map`, `_stocks` -- catalog data
- `auth` (AuthManager instance) -- sole auth owner
- `EVENTS` list, `FISHTOY_POLL_INTERVAL`, `CAPTURE_TYPES` -- constants

**Key behavior change in `make_event_handler()`:**
- Replace `asyncio.run_coroutine_threadsafe(broadcast_to_browsers(...), _loop)` with `database.notify_new_event(db_id, event_type)` for all events that go to the database.
- `chat:presence` and `chat:room` do NOT go through notify. They only update shared state fields (`fishtank_online`, `chat_room`) which get written to `_shared_state.json`.

**Shared state writes:**
After each catalog refresh, stock update, socket connect/disconnect, or `chat:presence`/`chat:room` event, ingestion calls `shared_state.write_state()` with the full state dict. Frequency: every 5-60s depending on which poller or event triggers it.

**Prune timer:**
`prune_notify()` runs on a 30s timer in a background thread (or piggybacked on an existing poller loop).

**Startup sequence in `ingest.py`:**
1. `database.init_db()` -- ensure tables exist
2. `load_catalog()` -- fetch items, contestants, rooms, stocks, toggles
3. `_seed_poll_vote_state()` -- init poll normalization
4. `seed_superchats_from_rest()` -- backfill superchats
5. Write initial shared state
6. Start `reconnect_loop()` thread
7. Start `fishtoy_poller()` thread
8. Start `stock_poller()` thread
9. Start `catalog_refresh_poller()` thread
10. Start `db_backup_poller()` thread
11. Block on `_poller_stop.wait()` (or `signal.pause()`)

#### What stays in `server.py` (slimmed):

**Kept as-is:**
- All FastAPI route handlers (`/api/*`)
- `websocket_endpoint()` -- `/ws` handler
- `broadcast_to_browsers()` -- still async, still sends to browser WS clients
- `_ws_ping_loop()` -- WebSocket keepalive
- `browser_clients`, `_clients_lock`, `MAX_WS_CLIENTS` -- WS client management
- `_analytics_cache`, `_cached_query()` -- query caching
- `_rate_limits`, `_rate_limit_lock`, `_check_rate_limit()`, `_prune_rate_limits()` -- rate limiting
- `_normalize_since()` -- cache key normalization
- `_health_event_count` -- health check DB count cache
- Frontend static file serving
- uvicorn entrypoint

**Removed:**
- `import auth`, `from fishclient import FishClient`, `import requests as http_requests`
- All ingestion functions and globals listed above
- `_get_analyzer()`, `_sentiment_analyzer` -- VADER (not needed for serving)

**New additions:**
- `import shared_state`
- Notify poller loop: async task started in `lifespan()`, polls `database.poll_notify()` every 200ms, fetches full event from DB, calls `broadcast_to_browsers()`
- Shared state reader: `shared_state.read_state()` called by endpoints that currently read in-memory globals:
  - `/api/items` reads `state["item_catalog"]` instead of `_item_catalog`
  - `/api/contestants` reads `state["contestants"]` instead of `_contestants`
  - `/api/rooms` reads `state["room_map"]` instead of `_room_map`
  - `/api/stocks` reads `state["stocks"]` instead of `_stocks`
  - `/api/feature-toggles` reads `state["feature_toggles"]` instead of `_feature_toggles`
  - `/api/status` reads `state["fishtank_online"]`, `state["auth_mode"]`, `state["auth_configured"]`
  - `/api/health` reads `state["socket_connected_at"]`, `state["last_fishtoy_poll"]`, `state["last_stock_poll"]`, `state["last_backup"]`, plus derives staleness from `state["updated_at"]`
  - WebSocket hello reads `state["chat_room"]`

**Backfill thread (startup DB maintenance):**
Stays in `server.py` lifespan, but only runs DB schema backfills (extracted columns, poll vote costs, superchat names). `_seed_poll_vote_state()` removed -- it moves to `ingest.py`.

**Lifespan changes:**
```
# Old: starts 5 ingestion threads + backfill + cache warmup + WS ping
# New: starts backfill + cache warmup + WS ping + notify poller
```

| File | Action |
|------|--------|
| `backend/ingest.py` | **New** ~550 lines -- extracted ingestion + auth + shared state writer |
| `backend/server.py` | Remove ~400 lines of ingestion code, add ~60 lines (notify poller + shared state reader) |
| `backend/requirements-api.txt` | **New** -- minimal deps (no fishclient, vaderSentiment, requests) |

### Phase 4: Docker Split

Create `Dockerfile.ingest` (Python-only, no Node/frontend). Adapt existing `Dockerfile` to `Dockerfile.api` (uses `requirements-api.txt`). Update `docker-compose.yml` to two services. Test locally with `docker compose up`.

| File | Action |
|------|--------|
| `Dockerfile.ingest` | **New** -- Python-only image |
| `Dockerfile` | Adapt for API-only (rename or keep as default) |
| `docker-compose.yml` | Two services sharing fishtank-data volume |

### Phase 5: Deploy + Verify

Deploy to VPS. Verify:
- Ingestion runs independently, events flow into DB
- API serves frontend, WS broadcasts work via notify table
- API-only redeploy does not interrupt ingestion
- Memory usage within limits
- WAL file size stable
- Health endpoint reflects ingestion status via shared state

## Risks

- **200ms WS latency** -- notify polling vs in-process broadcast. Imperceptible for a dashboard.
- **SQLite concurrent access** -- works fine on Linux ext4/xfs via Docker named volume. Not safe on NFS. WAL growth mitigated by `busy_timeout` on the reader.
- **Shared state staleness** -- if ingestion crashes, API serves stale catalog data and `socket_connected_at` freezes (showing connected when it isn't). The health endpoint mitigates this: if `updated_at` is older than 2 minutes, report degraded status regardless of other fields.
- **Complexity increase** -- two containers, two Dockerfiles, IPC via DB table + JSON file. More moving parts than single process.
- **Memory overhead** -- two separate Python interpreters don't share memory. Budgeted 3GB total (up from 1.5GB) to account for this.
- **Notify table pruning edge case** -- if ingestion crashes mid-prune, orphaned rows accumulate until restart. Harmless since the table auto-prunes on next insert cycle.

## Rollback

The `_notify` table is additive and harmless. `shared_state.py` is inert without `ingest.py`. To revert: restore the original single `server.py`, `Dockerfile`, and `docker-compose.yml`. No schema migration needed.

The phased implementation means each phase can be individually reverted if problems arise during development.
