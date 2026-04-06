# Fishtank Dashboard

**Live: [https://fish-dash.com](https://fish-dash.com)**

Real-time event monitoring dashboard for [fishtank.live](https://www.fishtank.live), an interactive 24/7 reality show. Captures fishtoy redemptions (including hidden metadata like love letter contents), chat messages, TTS, SFX, superchat pinned messages, polls, director messages, STO-X (stock market) data, and system events via a dual data source architecture built on reverse-engineered APIs.

## Architecture

```
Browser (React)  <--WebSocket-->  Backend (FastAPI)  <--Socket.IO-->  fishtank.live
                 <--REST API--->        |            <--REST poll-->  /v1/items/recent (5s)
                                        |            <--REST poll-->  /v1/stocks (60s)
                                        |            <--REST poll-->  /v1/items + /v1/contestants (10min)
                                    SQLite DB
```

**Backend** captures events from fishtank.live using two methods:
- **REST polling** (`/v1/items/recent` every 5s): Fishtoy and bigtoy redemptions including hidden metadata. The fishtank API does not broadcast fishtoy events over Socket.IO, so polling is the only way to capture them. A separate stock price poller runs every 60s to build price history. A catalog refresh poller updates contestants and item catalog every 10 minutes to pick up new freeloaders and items mid-season.
- **Socket.IO** (via vendored [fishclient](https://github.com/Blackthund4/fishclient) fork with 5 bug fixes): 21 real-time event types including chat, TTS, SFX, polls, director messages, superchats, stock changes, and price updates.

On startup, the backend loads item catalog (`/v1/items`), contestant data (`/v1/contestants`, filtered to current season), room name mappings (`/v1/live-streams`), stock prices (`/v1/stocks`), and active superchats (`/v1/super-chat`).

**Frontend** is a mobile-responsive React + Tailwind CSS dashboard with five tabs. The StatusBar shows a live Tank Time clock (America/New_York) and the viewer's local time with timezone abbreviation.
- **Dashboard**: Three-column layout with fishtoy/activity feed (virtual scrolling with keyset pagination), chat panel with superchat pinned banners and countdown timers, STO-X ticker with inline SVG sparklines and big mover highlights, target filtering with drill-down stats, metadata search, and Last 24h sidebar with leaderboards. Director message banner and live poll bar with animated vote percentages and per-option colors appear at the top when active. Activity panel supports time-travel navigation (jump to 1d/3d/7d/10d/30d ago and paginate from there).
- **Analytics**: STO-X cards with 7 range buttons (1h, 3h, 12h, Today, 3d, 1w, IPO) and sort options (highest value, movers up/down), contestant grid sortable by endorsements or STO-X price, peak activity hours (stacked bar chart by event type with busiest/quietest callouts), TTS/SFX analytics with sentiment mood badges and per-section time filters, chat analytics with sentiment analysis and time filters, poll history with colored vote bars and crown icons, director message timeline, price change log, fishtoy availability with category-level enable/disable status, and system events. Analytics support anchor-based time-travel with drag-to-pan.
- **Charts**: STO-X price history (LineChart, auto-downsampled), token spend trends (stacked BarChart + LineChart with TTS/SFX/Fishtoy/Poll/Superchat series and toggles), and chat volume (BarChart + top chatters). Nine time ranges from 30m to all-time. 5-minute auto-refresh.
- **Hidden Content**: Searchable archive of fishtoy metadata (love letters, custom messages) with target filtering and virtual scrolling.
- **User Search**: Search any username with autocomplete suggestions to see their unified activity timeline across chat, TTS, SFX, superchats, and fishtoys with type filters. Case-insensitive. Virtual scrolling.

### Key Technical Details

- **Dual data source discovery**: Through systematic API probing (`test_api_probe.py`, `test_api_probe2.py`) and raw WebSocket frame analysis (`test_catchall.py`), we determined that fishtoy data is served exclusively via REST (`/v1/items/recent`), not Socket.IO. The fishclient library lists `fishtoy:queued` and `fishtoy:update` as socket events, but these do not fire in Season 5.
- **Complete event registry**: By decompiling the fishtank.live production JavaScript, we mapped the full socket event registry (60+ events across chat, TTS, SFX, polls, stocks, items, trading, challenges, and notifications). 21 high-value events are actively captured.
- **Vendored fishclient fork** ([Blackthund4/fishclient](https://github.com/Blackthund4/fishclient)): 5 bugs fixed, installed via `file:` reference in `requirements.txt`:
  - Only processes 3-key msgpack packets (0x83 fixmap), silently dropping 4+ key packets
  - Malformed binary frames crash the listener and trigger unnecessary reconnection
  - Shutdown causes deadlock (thread.join() before websocket.close())
  - Spurious reconnection on clean shutdown (missing is_connected check)
  - Auto-registered disconnect handler has wrong signature, silently fails
- **Extracted columns**: Performance-critical fields (`sentiment`, `cost`, `display_name`, `target`, `room`, `metadata`, `item_id`, `feature`) are extracted from JSON into real SQLite columns on insert. All aggregate queries use these instead of `json_extract`, avoiding OOM on the 600k+ row table. Backfilled on first startup via background thread.
- **Virtual scrolling and keyset pagination**: Activity, chat, fishtoy, hidden content, and user search panels use `react-virtuoso` for virtual scrolling. Server-side keyset pagination via `before_id` for efficient "load more" without offset scans.
- **Superchat system**: `super-chat:new`/`super-chat:delete` captured via Socket.IO, seeded from REST on startup. Pinned banners with countdown timers above chat. Profile API fallback for empty `displayName` fields (cached 1h). Activity panel has superchat type filter.
- **Sentiment analysis**: VADER-based sentiment scoring on TTS and chat messages at ingestion. Hourly breakdown charts, mood badges, and per-contestant sentiment in Analytics.
- **Item type filtering**: Only FISHTOY and BIGTOY types are captured. WARTOY (user-vs-user effects), NORMAL_ITEM, and SPECIAL types are filtered out via the item catalog's `type` field.
- **Hidden metadata capture**: Fishtoy items like "Love Letter" include user-written content in a `metadata` field not displayed on the website UI.
- **Room name resolution**: TTS/SFX events contain room codes (e.g. `hwdn-5`). The backend resolves these to human-readable names (e.g. "Hallway") via the `/v1/live-streams` endpoint.
- **Stock price history**: Prices are polled every 60 seconds and stored in SQLite for historical tracking. Old snapshots downsampled (not deleted) to maintain long-term trends.
- **Automatic authentication**: The dashboard logs in via `/v1/auth/log-in` using email/password from a `.env` file, caches tokens to disk, and automatically re-authenticates on 401 responses. No manual cookie copying required.
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
- **Security hardening**: CORS restricted to configured origins (GET/HEAD only), per-IP rate limiting (120 req/60s, returns 429), WebSocket connection cap (50 max), endpoint sanitization (no auth tokens or internal state in public responses), generic exception handler (no stack traces), non-root Docker container with gosu privilege drop, and automatic SQLite backup every 6 hours using the online backup API.
- **Error boundary**: React ErrorBoundary catches render errors and displays a recovery UI instead of crashing to a black screen.
- **Service worker**: Retries navigation requests during deploys. Update banner appears when WebSocket reconnects and detects a new `BUILD_VERSION`.
- **GZip compression**: FastAPI GZip middleware compresses API responses for reduced bandwidth.
- **Peak hours**: Combined hourly activity across TTS, SFX, and fishtoys (excluding chat to maintain readable scale) with stacked bar chart and busiest/quietest hour callouts.

## Setup

### Prerequisites

- Python 3.10+
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

The dashboard logs in automatically on startup, caches tokens to disk, and re-authenticates if they expire. No manual cookie copying.

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

### Single process (recommended)

```bash
cd frontend
npm install
npm run build

cd ../backend
python server.py
```

Open http://localhost:8000

### Development mode (two terminals)

Terminal 1 (backend):
```bash
cd backend
python server.py
```

Terminal 2 (frontend dev server):
```bash
cd frontend
npm run dev
```

Open http://localhost:3000 (Vite proxies API/WebSocket to the backend). Dev mode swaps to a red fish favicon and "[DEV]" title.

### Docker (recommended for deployment)

```bash
# Create .env file with your credentials
cat > .env << EOF
FISHTANK_EMAIL=your_email@example.com
FISHTANK_PASSWORD=your_password
ALLOWED_ORIGINS=https://your-domain.com
EOF

# Build and run (GIT_COMMIT enables the update banner)
GIT_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build
```

Open https://localhost (port 443 with Cloudflare Origin Certificate) or http://localhost:8000 (without SSL certs).

The database and token cache persist in a Docker volume (`fishtank-data`). Container runs as a non-root user (via `gosu`), is limited to 1.5GB RAM with no swap, and has a healthcheck that auto-restarts on failure. To stop: `docker compose down`. To rebuild after code changes: `docker compose up -d --build`.

## Testing

```bash
cd backend
pip install pytest
python -m pytest test_backend.py -v
```

110 tests covering database operations (store, query, analytics, dedup, purge, extracted columns), filter functions (chat echo, notification, TTS dedup), rate limiting, poll state reconstruction, user search, sentiment analysis, superchat handling, and chart data queries. Tests run against an in-memory SQLite database.

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
| `chat:message` | Chat messages (with role badges, sentiment scoring) |
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
        server.py             FastAPI server + fishclient bridge + REST pollers + WS fan-out
        database.py           SQLite storage, analytics queries, chart data, extracted columns
        auth.py               Supabase auto-login, token cache, 401 re-auth
        test_backend.py       110 unit tests (in-memory SQLite)
        cleanup_db.py         One-time DB cleanup (dedup TTS, purge system chat/gifts)
        import_logs.py        Backfill JSONL logs into SQLite
        vendor/fishclient/    Vendored patched fork (Blackthund4/fishclient)
        requirements.txt
        .env.example          Template for credentials
    frontend/
        public/
            sw.js             Service worker: retry page during deploys
            favicon.svg       Lucide Fish icon (cyan, matches StatusBar)
            dev-favicon.svg   Lucide Fish icon (red, dev environment only)
        src/
            App.jsx           Main app: all tabs, state, WebSocket client, fetch logic
            useWebSocket.js   WebSocket hook: /ws connection, reconnect, dispatch
            utils/
                formatTime.js     Shared timestamp formatting (formatTime, formatDateTime)
            components/
                StatusBar.jsx     Connection status, Tank Time clock, live stats
                Panel.jsx         Reusable scrollable/virtualized panel
                FishtoyCard.jsx   Collapsible fishtoy event cards (React.memo)
                ChatMessage.jsx   Chat message display with role badges (React.memo)
                ActivityCard.jsx  TTS/SFX/superchat display with room names (React.memo)
                AnchorRow.jsx     Time-travel anchor indicator for panels
                ErrorBoundary.jsx Catches render errors, shows recovery UI
            tabs/
                AnalyticsTab.jsx      STO-X, contestants, analytics, sentiment, polls, system events
                ChartsTab.jsx         STO-X price history, spend trends, chat volume charts
                HiddenContentTab.jsx  Searchable hidden content archive (virtualized)
                UserSearchTab.jsx     User activity search with autocomplete (virtualized)
        index.html
        package.json
        vite.config.js        Dev proxy, dev favicon/title swap, recharts vendor chunk
        tailwind.config.js    Custom "tank" color palette, JetBrains Mono / DM Sans fonts
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
        architecture-split-proposal.md  Ingestion/API separation design doc
        top-keywords-proposal.md        Chat keyword analysis design doc
    .gitignore
    .dockerignore
    entrypoint.sh             Docker entrypoint (chown + gosu privilege drop)
    Dockerfile                Multi-stage build (Node frontend + Python backend)
    docker-compose.yml        Single-command deployment with persistent volume
    TECHNICAL_WRITEUP.md      Reverse engineering process documentation
    DEPLOY_WINDOWS.md         Standalone logger setup (Windows)
    DEPLOY_FEDORA.md          Standalone logger setup (Fedora)
    DEPLOY_DASHBOARD_WINDOWS.md  Dashboard setup (Windows)
    README.md
```

## Deployment

Currently hosted on a Vultr Cloud Compute VPS (2 vCPU, 2GB RAM, Ubuntu 24.04 with Docker) for 24/7 event capture behind Cloudflare DNS/SSL (Full Strict mode). Security hardening includes:

- Cloudflare proxied DNS with Full Strict SSL (Origin Certificate on server)
- UFW firewall (ports 22 and 443, port 443 restricted to Cloudflare IPs only)
- SSH key-based auth with password login disabled
- CORS restricted to `fish-dash.com` origins (GET/HEAD only)
- Per-IP rate limiting (120 req/60s)
- WebSocket connection cap (50)
- Non-root container user (gosu privilege drop)
- Docker memory limit (1.5GB, no swap)
- Docker healthcheck with auto-restart
- Automatic SQLite backup every 6 hours; hourly WAL checkpoint
- Docker log rotation (50MB max, 3 files)
- Ubuntu unattended security upgrades
- UptimeRobot monitoring on `/api/health`

To update after pushing code changes:
```bash
cd /opt/fishtank-dashboard
git fetch origin && git reset --hard origin/main
GIT_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build
```
