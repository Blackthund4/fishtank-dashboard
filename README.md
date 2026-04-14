# Fishtank Dashboard

**Live: [https://fish-dash.com](https://fish-dash.com)**

Real-time event monitoring dashboard for [fishtank.live](https://www.fishtank.live), an interactive 24/7 reality show. Captures fishtoy redemptions (including hidden metadata like love letter contents), chat messages, TTS, SFX, superchat pinned messages, polls, director messages, STO-X (stock market) data, chat keyword trends, and system events via a dual data source architecture built on reverse-engineered APIs.

## Architecture

Two-process split: ingestion writes, API reads. They share a SQLite database and communicate via a `_notify` table and an atomic JSON file for catalog/status IPC.

```
                  +--------------------+
fishtank.live --> |   ingestion        | --> SQLite (sole writer)
                  |   (ingest.py)      | --> _shared_state.json
                  +--------------------+
                           |
                      _notify table (200ms poll)
                           |
                  +--------------------+
browsers <------> |   api              | <-- SQLite (reader only, query_only=1)
                  |   (server.py)      | <-- _shared_state.json
                  +--------------------+
```

**Ingestion process (`ingest.py`)** captures events from fishtank.live using two methods:
- **Socket.IO** (via vendored [fishclient](https://github.com/Blackthund4/fishclient) fork with 5 bug fixes): 21 real-time event types including chat, TTS, SFX, polls, director messages, superchats, stock changes, and price updates. Reconnects with exponential backoff (5s to 60s) and automatic token refresh.
- **REST polling**: Fishtoy/bigtoy redemptions every 5s (`/v1/items/recent`), stock prices every 60s (`/v1/stocks`), catalog refresh every 10 min (contestants, items, rooms, stocks, feature toggles).
- **Background threads**: Keyword aggregation (120s cycle, hourly buckets), SQLite online backup (6h), WAL checkpoint (1h), stock history downsampling (30d+), chat pruning (30d), notify table pruning (<60s of rows).

On startup, the ingestion process loads item catalog, contestant data (filtered to current season), room name mappings, stock prices, active superchats, and backfills any missing extracted columns and keyword data.

**API process (`server.py`)** is a read-only FastAPI server with `PRAGMA query_only=1` on every connection:
- REST endpoints (37 routes) with analytics caching (60s TTL), rate limiting (120 req/60s per IP), and security headers (HSTS, X-Frame-Options, COOP, Referrer-Policy, Permissions-Policy, CSP-Report-Only).
- Notify poller (200ms) picks up new events from the `_notify` table and broadcasts to browser WebSockets.
- WebSocket fan-out (200 clients max, 3 per IP) with origin validation, 60s ping for Cloudflare idle timeout, and `server:hello` version detection for update banners.
- Serves the React SPA as static files with immutable cache on hashed `/assets/*` and no-cache on `index.html`.

**Frontend** is a React 18 + Tailwind CSS dashboard with five tabs and a live WebSocket connection. StatusBar shows Tank Time (America/New_York), local time, and live aggregate stats.

- **Dashboard**: Three-column layout with fishtoy/activity feed (virtual scrolling with keyset pagination), chat panel with superchat pinned banners and countdown timers, STO-X ticker with inline SVG sparklines and big mover highlights, target filtering with drill-down stats, metadata search, chat keyword trending pills, and Last 24h sidebar with leaderboards and top keywords. Director message banner and live poll bar with animated vote percentages appear at the top when active. Activity panel supports time-travel navigation (jump to 1d/3d/7d/10d/30d ago and paginate from there).
- **Analytics**: STO-X cards with 7 range buttons and sort options, contestant grid sortable by endorsements or STO-X price, peak activity hours (stacked bar chart by event type), TTS/SFX analytics with sentiment mood badges and per-section time filters, chat analytics with sentiment analysis and keyword frequency breakdowns, poll history with colored vote bars and crown icons, director message timeline, price change log, fishtoy availability with category-level enable/disable status, and system events. Analytics support anchor-based time-travel with drag-to-pan.
- **Charts**: STO-X price history (LineChart, auto-downsampled), token spend trends (stacked BarChart + LineChart with TTS/SFX/Fishtoy/Poll/Superchat series and toggles), chat volume (BarChart + top chatters), and keyword frequency over time (hourly resolution, 6h+ ranges). Nine time ranges from 30m to all-time. 5-minute auto-refresh.
- **Hidden Content**: Searchable archive of fishtoy metadata (love letters, custom messages) with target filtering and virtual scrolling.
- **User Search**: Search any username with autocomplete suggestions to see their unified activity timeline across chat, TTS, SFX, superchats, and fishtoys with type filters. Case-insensitive. Virtual scrolling.

### Key Technical Details

- **Dual data source discovery**: Through systematic API probing (`test_api_probe.py`, `test_api_probe2.py`) and raw WebSocket frame analysis (`test_catchall.py`), we determined that fishtoy data is served exclusively via REST (`/v1/items/recent`), not Socket.IO. The fishclient library lists `fishtoy:queued` and `fishtoy:update` as socket events, but these do not fire in Season 5.
- **Complete event registry**: By decompiling the fishtank.live production JavaScript, we mapped the full socket event registry (60+ events across chat, TTS, SFX, polls, stocks, items, trading, challenges, and notifications). 21 high-value events are actively captured.
- **Two-process split**: `ingest.py` is the sole SQLite writer and owns all fishtank.live credentials. `server.py` is a read-only API with `PRAGMA query_only=1` enforced on every connection. They share a Docker volume and communicate via a `_notify` table (ingestion inserts after `store_event()`, API polls at 200ms) and an atomic JSON file (`_shared_state.json`) for catalog/status/presence data. Primary benefit: API-only redeploys cause zero ingestion gaps.
- **Vendored fishclient fork** ([Blackthund4/fishclient](https://github.com/Blackthund4/fishclient)): 5 bugs fixed, installed via `file:` reference in `requirements.txt`:
  - Only processes 3-key msgpack packets (0x83 fixmap), silently dropping 4+ key packets
  - Malformed binary frames crash the listener and trigger unnecessary reconnection
  - Shutdown causes deadlock (thread.join() before websocket.close())
  - Spurious reconnection on clean shutdown (missing is_connected check)
  - Auto-registered disconnect handler has wrong signature, silently fails
- **Extracted columns**: 10 performance-critical fields (`sentiment`, `cost`, `display_name`, `target`, `room`, `metadata`, `item_id`, `feature`, `chat_role`, `message_text`) are extracted from JSON into real SQLite columns on insert. All aggregate queries use these instead of `json_extract`, avoiding OOM on the 600k+ row table. Backfilled on first startup via background thread.
- **Keyword analysis**: Hybrid architecture with inline tokenization at ingestion, an in-memory rolling buffer for real-time trending (5 min window), and a background aggregation thread writing hourly buckets to a `keyword_counts` table. Regex tokenizer with a calibrated stopword list (standard English + chat noise + bot commands + platform noise). JavaScript port in the frontend computes chat panel trending pills client-side from messages in state. Three API endpoints serve trending keywords, session-window top keywords, and historical time-series data at hourly resolution.
- **Virtual scrolling and keyset pagination**: Activity, chat, fishtoy, hidden content, and user search panels use `react-virtuoso` for virtual scrolling. Server-side keyset pagination via `before_id` for efficient "load more" without offset scans.
- **Superchat system**: `super-chat:new`/`super-chat:delete` captured via Socket.IO, seeded from REST on startup. Pinned banners with countdown timers above chat. Profile API fallback for empty `displayName` fields (cached 1h, max 500 entries). Activity panel has superchat type filter.
- **Sentiment analysis**: VADER-based sentiment scoring on TTS and chat messages at ingestion. Hourly breakdown charts, mood badges, and per-contestant sentiment in Analytics.
- **Item type filtering**: Only FISHTOY and BIGTOY types are captured. WARTOY (user-vs-user effects), NORMAL_ITEM, and SPECIAL types are filtered out via the item catalog's `type` field.
- **Hidden metadata capture**: Fishtoy items like "Love Letter" include user-written content in a `metadata` field not displayed on the website UI.
- **Room name resolution**: TTS/SFX events contain room codes (e.g. `hwdn-5`). The backend resolves these to human-readable names (e.g. "Hallway") via the `/v1/live-streams` endpoint.
- **Stock price history**: Prices are polled every 60 seconds and stored in SQLite for historical tracking. Old snapshots downsampled to daily averages after 30 days.
- **Automatic authentication**: The ingestion process logs in via `/v1/auth/log-in` using email/password from a `.env` file, caches tokens to disk, and automatically re-authenticates on 401 responses. No manual cookie copying required. The API process does not authenticate with fishtank.live at all.
- **Resilient socket reconnection**: If the Socket.IO connection drops, the reconnect loop gets fresh tokens from the auth manager and reconnects with exponential backoff (5s to 60s). Stale token reconnections are eliminated.
- **Event deduplication**: The fishtank server fires `tts:update` and `sfx:update` twice per message (once as "approved", again as "played"). Events are deduplicated by their unique event ID at the handler level before storage.
- **Chat noise filtering**: The server echoes TTS, SFX, and emote actions as `chat:message` events with system usernames ("tts", "sfx", "emote"). These are filtered at ingestion to prevent polluting chat data and analytics.
- **Notification filtering**: Season pass gift notifications (`"[user] gifted X season passes!"`) are filtered from director messages at ingestion.
- **Poll state reconstruction**: Since `poll:stop` is a single-fire event easily missed during reconnections, the dashboard reconstructs poll state from the database on mount, inferring results from the last `poll:vote` entry when `poll:stop` is missing. Poll history panel shows colored vote bars with crown icons on winners.
- **Feature toggle monitoring**: Tracks real-time fishtoy/TTS/SFX category enable/disable state from `feature-toggles:update` events. Displays status badges with pricing on Analytics panels and shows category-level warnings on individual items.
- **Per-section time filters**: Analytics panels have independent time filters so TTS and chat analytics can be viewed over different time ranges without affecting each other.
- **Health endpoint**: `/api/health` reports socket uptime, poller staleness, database status, and overall healthy/degraded assessment. Sensitive details (auth tokens, internal timestamps, event type breakdown) are excluded from the public response.
- **Backfill detection**: On startup, the fishtoy poller loads known event IDs from the database and compares them against the API response. Items that occurred during downtime are backfilled automatically instead of being silently skipped.
- **Catalog refresh**: Contestants and item catalog are refreshed every 10 minutes to pick up new freeloaders and fishtoys added mid-season without requiring a server restart.
- **Query separation**: Each event type category (chat, TTS/SFX, system events, fishtoys, polls) is fetched independently to prevent high-volume types from crowding low-volume types out of shared query limits. Multi-type queries use `UNION ALL` so each branch uses indexes independently.
- **Thread safety**: All shared backend state (catalogs, rate limit counters, WebSocket clients, dedup window) is protected by dedicated `threading.Lock()` instances. API endpoints return shallow copies under lock.
- **Error boundary**: React ErrorBoundary catches render errors and displays a recovery UI instead of crashing to a black screen.
- **Service worker**: Retries navigation requests during deploys. Update banner appears when WebSocket reconnects and detects a new `BUILD_VERSION`.
- **GZip compression**: FastAPI GZip middleware compresses API responses for reduced bandwidth.
- **Peak hours**: Combined hourly activity across TTS, SFX, and fishtoys (excluding chat to maintain readable scale) with stacked bar chart and busiest/quietest hour callouts.

## Setup

### Prerequisites

- Python 3.12+
- Node.js 18+
- A fishtank.live account

### Backend

```bash
cd backend
pip install -r requirements.txt
```

### Frontend

```bash
cd frontend
npm install
```

### Authentication

**Option 1: Automatic (recommended)**

```bash
cd backend
cp .env.example .env
```

Edit `.env` with your fishtank.live credentials:
```
FISHTANK_EMAIL=your_email@example.com
FISHTANK_PASSWORD=your_password
```

The ingestion process logs in automatically on startup, caches tokens to disk, and re-authenticates if they expire. No manual cookie copying.

**Option 2: Manual cookie (legacy)**
Copy from the Network tab, not Application/Cookies tab.

If you prefer not to store credentials, set the cookie manually:

1. Log into fishtank.live in your browser
2. Open DevTools (F12) > **Network** tab
3. Filter by `api.fishtank.live`
4. Click any request, find the `Cookie:` header in Request Headers
5. Copy the value after `sb-wcsaaupukpdmqdjcgaoo-auth-token=`

Then set it as an environment variable:
```bash
export FISHTANK_COOKIE='your_cookie_value'    # Linux/Mac
$env:FISHTANK_COOKIE = 'your_cookie_value'    # PowerShell
```

## Running

### Two-process development (two terminals)

Terminal 1 (ingestion):
```bash
cd backend
python ingest.py
```

Terminal 2 (API + frontend dev server):
```bash
cd backend
python server.py
```

Terminal 3 (optional, frontend hot reload):
```bash
cd frontend
npm run dev
```

Open http://localhost:3000 (Vite proxies API/WebSocket to the backend). Dev mode swaps to a red fish favicon and "[DEV]" title.

Without the Vite dev server, `server.py` serves the production frontend build at http://localhost:8000 (run `cd frontend && npm run build` first).

### Docker (recommended for deployment)

```bash
# Create .env file with your credentials
cat > .env << EOF
FISHTANK_EMAIL=your_email@example.com
FISHTANK_PASSWORD=your_password
ALLOWED_ORIGINS=https://your-domain.com
EOF

# Build and run both services (GIT_COMMIT enables the update banner)
GIT_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build
```

Two containers start: `ingestion` (headless event capture, no exposed ports) and `api` (HTTPS on port 443 with Cloudflare Origin Certificate). The database and shared state persist in a Docker volume (`fishtank-data`). Both containers run as non-root users (via `gosu`), drop all Linux capabilities except the minimum required, and have `no-new-privileges` set. The API container runs with a read-only root filesystem.

Selective deploys (primary benefit of the two-process split):
```bash
# API-only redeploy (zero ingestion gap)
GIT_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build api

# Ingestion-only redeploy
docker compose up -d --build ingestion

# Per-service logs
docker compose logs -f api
docker compose logs -f ingestion
```

To stop: `docker compose down`. To rebuild after code changes: `GIT_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build`.

## Testing

```bash
cd backend
pip install pytest
python -m pytest test_backend.py -v
```

210 tests covering database operations (store, query, analytics, dedup, purge, extracted columns, keyword aggregation), filter functions (chat echo, notification, TTS dedup), rate limiting, poll state reconstruction, user search, sentiment analysis, superchat handling, chart data queries, security hardening (CORS, CSP, read-only mode, WebSocket origin validation), tokenizer accuracy, and keyword analytics endpoints. Tests run against an in-memory SQLite database.

## API

Interactive API documentation is available at `/docs` (Swagger UI) and `/redoc` (ReDoc) when the server is running.

| Endpoint | Description |
|---|---|
| `GET /api/events` | List events. Query params: `type` (comma-separated), `limit`, `since_id`, `before_id`, `around_ts`, `target`, `item_id`, `search` |
| `GET /api/fishtoys` | Fishtoy events with server-side filters + keyset pagination. Query params: `target`, `item_id`, `search`, `limit`, `before_id` |
| `GET /api/stats` | Summary statistics. Query params: `since` (ISO timestamp) |
| `GET /api/health` | Comprehensive health check: socket uptime, poller status, last event per type, DB stats |
| `GET /api/status` | Connection status, browser client count, and auth status |
| `GET /api/items` | Item catalog (itemId to name/description/icon mapping) |
| `GET /api/contestants` | Current season contestant list (filtered to active season) |
| `GET /api/rooms` | Room code to name mapping (e.g. `hwdn-5` to `Hallway`) |
| `GET /api/stocks` | Current STO-X data (updated by poller every 60s) |
| `GET /api/stocks/history` | STO-X price history from SQLite. Query params: `ticker`, `limit` |
| `GET /api/stocks/count` | Actual count of stock history snapshots in database |
| `GET /api/stocks/delta` | Price deltas for custom time ranges (3h, 12h, 3d) |
| `GET /api/stocks/sparklines` | Price arrays per ticker for sparkline rendering. Query params: `range` |
| `GET /api/superchats` | Active and recent superchats. Query params: `limit`, `since` |
| `GET /api/targets` | All fishtoy targets with total count and spend |
| `GET /api/target-stats` | Detailed stats for a specific target. Query params: `target` |
| `GET /api/analytics/tts-sfx` | TTS/SFX analytics: top rooms, top senders, hourly activity, sentiment |
| `GET /api/analytics/chat` | Chat analytics: top chatters, hourly volume |
| `GET /api/analytics/chat-sentiment` | Chat sentiment: overall mood, hourly breakdown |
| `GET /api/analytics/tts-sentiment` | TTS sentiment: overall mood, hourly breakdown, mood by contestant |
| `GET /api/analytics/peak-hours` | Combined hourly activity by type with peak/quietest hours |
| `GET /api/analytics/keywords` | Keyword frequency time-series data (hourly resolution). Query params: `range`, `anchor` |
| `GET /api/keywords/trending` | Real-time trending keywords (5 min rolling window) |
| `GET /api/keywords/top` | Top keywords over a session window. Query params: `limit` |
| `GET /api/hidden-content` | Fishtoy events with metadata only. Query params: `target`, `search`, `limit`, `offset` |
| `GET /api/hidden-content/targets` | Target list with counts for hidden content sidebar |
| `GET /api/fishtoy-availability` | Fishtoy/bigtoy items with enabled/cooldown/cost status |
| `GET /api/polls` | Poll start and stop events. Query params: `limit` |
| `GET /api/polls/latest` | Reconstructed state of the most recent poll |
| `GET /api/notifications` | Director messages and announcements. Query params: `limit`, `since` |
| `GET /api/price-changes` | TTS/SFX price change history. Query params: `limit` |
| `GET /api/feature-toggles` | Current feature toggle states (fishtoys, TTS, SFX enable/disable + pricing) |
| `GET /api/user/{username}` | Search all event types for a specific user's activity (case-insensitive) |
| `GET /api/users/suggest` | Username autocomplete suggestions. Query params: `q` (min 2 chars) |
| `GET /api/charts/stocks` | STO-X price history chart data. Query params: `range`, `anchor` |
| `GET /api/charts/spend` | Token spend trends chart data. Query params: `range`, `anchor` |
| `GET /api/charts/chatters` | Chat volume chart data. Query params: `range`, `anchor` |
| `WS /ws` | Live event stream via WebSocket. Sends `server:hello` with `BUILD_VERSION` on connect |

## Socket Events Captured

| Event | Description |
|---|---|
| `chat:message` | Chat messages (with role badges, sentiment scoring, keyword tokenization) |
| `chat:room` | Chat room changes (tracked in memory, not stored) |
| `chat:presence` | Live viewer count (tracked in memory, not stored) |
| `tts:update` | Text-to-speech events (with room, sender, cost, sentiment) |
| `tts:price` | TTS price changes |
| `sfx:update` | Sound effect events (with room, sender, cost) |
| `sfx:price` | SFX price changes |
| `poll:start` | Poll created (question + options) |
| `poll:stop` | Poll closed (winner) |
| `poll:vote` | Live vote tallies |
| `notification:global` | Director messages / system announcements |
| `announcement` | System announcements |
| `stock:update` | Individual stock price changes |
| `stock:new` | New stock added |
| `stock:remove` | Stock removed (elimination) |
| `stock:split` | Stock split event |
| `happening` | System happenings |
| `feature-toggles:update` | Feature enable/disable changes |
| `super-chat:new` | Superchat pinned message created |
| `super-chat:delete` | Superchat pinned message removed |

## Project Structure

```
fishtank-dashboard/
    backend/
        ingest.py             Ingestion process: fishclient Socket.IO + REST pollers + sole DB writer
        server.py             API process: read-only SQLite, notify poller, WebSocket fan-out, static files
        shared_state.py       Atomic JSON read/write with mtime caching for IPC between ingest + api
        database.py           SQLite storage, analytics queries, chart data, dedup, keyword aggregation
        auth.py               Supabase auto-login, token cache, 401 re-auth (ingestion-only)
        tokenizer.py          Regex tokenizer with calibrated stopword list for keyword analysis
        test_backend.py       210 unit tests (in-memory SQLite)
        cleanup_db.py         One-time DB cleanup (dedup TTS, purge system chat/gifts)
        import_logs.py        Backfill JSONL logs into SQLite
        backfill_sentiment.py VADER sentiment backfill for historical events
        backfill_keywords.py  One-time keyword_counts backfill from historical message_text data
        vendor/fishclient/    Vendored patched fork (Blackthund4/fishclient)
        requirements.txt      Full dependencies (ingestion: fishclient, requests, vaderSentiment, orjson)
        requirements-api.txt  Lean dependencies (API only: fastapi, uvicorn, orjson)
        .env.example          Template for credentials
    frontend/
        public/
            sw.js             Service worker: retry page during deploys
            sw-register.js    SW registration shim (extracted from index.html for CSP script-src 'self')
            favicon.svg       Lucide Fish icon (cyan, matches StatusBar)
            dev-favicon.svg   Lucide Fish icon (red, dev environment only)
        src/
            App.jsx           Main app: all tabs, state, WebSocket client, fetch logic
            useWebSocket.js   WebSocket hook: /ws connection, reconnect, dispatch
            utils/
                formatTime.js     Shared timestamp formatting (formatTime, formatDateTime)
                fetchUtils.js     okJson helper (throws on non-OK, parses JSON)
                tokenizer.js      JavaScript port of backend tokenizer for client-side keyword pills
            components/
                StatusBar.jsx     Connection status, Tank Time clock, live stats
                Panel.jsx         Reusable scrollable/virtualized panel
                FishtoyCard.jsx   Collapsible fishtoy event cards (React.memo)
                ChatMessage.jsx   Chat message display with role badges and safe color validation (React.memo)
                ActivityCard.jsx  TTS/SFX/superchat display with room names (React.memo)
                AnchorRow.jsx     Time-travel anchor indicator for panels
                ErrorBoundary.jsx Catches render errors, shows recovery UI
            tabs/
                AnalyticsTab.jsx      STO-X, contestants, analytics, sentiment, keywords, polls, system events
                ChartsTab.jsx         STO-X price history, spend trends, chat volume, keyword frequency charts
                HiddenContentTab.jsx  Searchable hidden content archive (virtualized)
                UserSearchTab.jsx     User activity search with autocomplete (virtualized)
        index.html
        package.json
        vite.config.js        Dev proxy, dev favicon/title swap, recharts vendor chunk
        tailwind.config.js    Custom "tank" color palette, JetBrains Mono / Outfit fonts
    scripts/
        fishtoy_poller.py     Standalone fishtoy-only REST poller
        fishtank_logger.py    Combined logger (REST + Socket.IO)
        research/
            test_catchall.py          Raw WebSocket frame logger (all packet types)
            test_filtered_catchall.py Filtered catchall (skips chat/TTS/SFX noise)
            test_api_probe.py         REST endpoint discovery (round 1)
            test_api_probe2.py        REST endpoint discovery (round 2)
            test_cookie.py            Auth cookie validation
            test_initial_data.py      Initial-data event analysis
    docs/
        architecture-split-proposal.md    Ingestion/API separation design doc
        keyword-analysis-architecture.md  Keyword analysis design doc (all 5 phases)
        top-keywords-proposal.md          Original keyword proposal (superseded)
        session-handoff-2026-04-11.md     Deploy session notes and staging wake-up procedure
        adr-secrets-management.md         Secrets management architecture decision record
    .gitignore
    .dockerignore
    entrypoint.sh             Docker entrypoint for API container (chown + gosu privilege drop)
    entrypoint-ingest.sh      Docker entrypoint for ingestion container
    Dockerfile                Multi-stage build: Node frontend + Python API (uses requirements-api.txt)
    Dockerfile.ingest         Python-only build for ingestion (uses full requirements.txt)
    docker-compose.yml        Two-service deployment with shared volume
    TECHNICAL_WRITEUP.md      Reverse engineering process documentation
    DEPLOY_WINDOWS.md         Standalone logger setup (Windows)
    DEPLOY_FEDORA.md          Standalone logger setup (Fedora)
    DEPLOY_DASHBOARD_WINDOWS.md  Dashboard setup (Windows)
    README.md
```

## Deployment

Currently hosted on a Vultr Dedicated CPU VPS (2 vCPU, 8GB RAM, Ubuntu 24.04) for 24/7 event capture behind Cloudflare DNS/SSL (Full Strict mode) with UptimeRobot monitoring on `/api/health`. Security hardening includes:

- **Network**: Cloudflare proxied DNS with Full Strict SSL (Origin Certificate on server). UFW firewall restricts port 443 to Cloudflare IP ranges only. SSH key-based auth with password login disabled.
- **HTTP**: CORS fail-closed (rejects all cross-origin requests when `ALLOWED_ORIGINS` is unset). Per-IP rate limiting (120 req/60s, XFF-corrected behind Cloudflare, returns 429). Security headers on every response: HSTS, X-Frame-Options DENY, X-Content-Type-Options, COOP same-origin, Referrer-Policy strict-origin-when-cross-origin, Permissions-Policy (camera/mic/geolocation disabled), CSP in report-only mode.
- **WebSocket**: Origin header validated against `ALLOWED_ORIGINS` (close 1008 on mismatch). Per-IP connection cap of 3 (close 1013 on reject). Global cap of 200 clients. Inbound frame size capped at 4 KB. 60s ping interval.
- **Docker**: Two containers. Both drop all Linux capabilities (`cap_drop: ALL`) and add back only the 5 required (CHOWN, DAC_OVERRIDE, FOWNER, SETUID, SETGID). `no-new-privileges: true` on both. API container has `read_only: true` root filesystem with a 64 MB tmpfs at `/tmp`. Non-root container user via gosu privilege drop. Ingestion limited to 1GB/1CPU, API limited to 2GB/1.5CPU. Docker log rotation (50MB max, 3 files).
- **Database**: `PRAGMA query_only=1` enforced on every API-side SQLite connection (defense-in-depth). Automatic online backup every 6 hours. Hourly WAL checkpoint. Stock history downsampled after 30 days. Non-staff chat pruned after 30 days.
- **Dependencies**: All runtime Python packages pinned to exact versions (fastapi 0.135.2, uvicorn 0.42.0, requests 2.33.0, vaderSentiment 3.3.2, orjson 3.11.8).
- **OS**: Ubuntu unattended security upgrades enabled.

To update after pushing code changes:
```bash
cd /opt/fishtank-dashboard
git fetch origin && git reset --hard origin/main
GIT_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build --remove-orphans
```
