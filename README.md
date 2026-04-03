# Fishtank Dashboard

**Live: [https://fish-dash.com](https://fish-dash.com)**

Real-time event monitoring dashboard for [fishtank.live](https://www.fishtank.live), an interactive 24/7 reality show. Captures fishtoy redemptions (including hidden metadata like love letter contents), chat messages, TTS, SFX, polls, director messages, STO-X (stock market) data, and system events via a dual data source architecture built on reverse-engineered APIs.

## Architecture

```
Browser (React)  <--WebSocket-->  Backend (FastAPI)  <--Socket.IO-->  fishtank.live
                 <--REST API--->        |            <--REST poll-->  /v1/items/recent (2s)
                                        |            <--REST poll-->  /v1/stocks (60s)
                                        |            <--REST poll-->  /v1/items + /v1/contestants (10min)
                                    SQLite DB
```

**Backend** captures events from fishtank.live using two methods:
- **REST polling** (`/v1/items/recent` every 2s): Fishtoy and bigtoy redemptions including hidden metadata. The fishtank API does not broadcast fishtoy events over Socket.IO, so polling is the only way to capture them. A separate stock price poller runs every 60s to build price history. A catalog refresh poller updates contestants and item catalog every 10 minutes to pick up new freeloaders and items mid-season.
- **Socket.IO** (via [fishclient](https://pypi.org/project/fishclient/) with 5 bug patches): 18 real-time event types including chat, TTS, SFX, polls, director messages, stock changes, and price updates.

On startup, the backend loads item catalog (`/v1/items`), contestant data (`/v1/contestants`, filtered to current season), room name mappings (`/v1/live-streams`), and stock prices (`/v1/stocks`).

**Frontend** is a React + Tailwind CSS dashboard with four tabs:
- **Dashboard**: Live fishtoy feed with collapsible cards, chat panel, TTS/SFX activity with room names, STO-X ticker, target filtering, metadata search, and session stats. Director message banner and live poll bar with animated vote percentages appear at the top when active. All event panels sorted chronologically.
- **Analytics**: STO-X cards with sort options (highest value, movers up/down), contestant grid sortable by endorsements or STO-X price, peak activity hours (stacked bar chart by event type with busiest/quietest callouts), TTS/SFX analytics with toggle status badges and per-section time filters (All/7d/3d/24h), chat analytics with time filters, poll history, director message timeline, price change log, fishtoy availability with category-level enable/disable status, and system events. All data auto-refreshes every 30 seconds.
- **Hidden Content**: Searchable archive of fishtoy metadata (love letters, custom messages) with target filtering.
- **User Search**: Search any username with autocomplete suggestions to see their unified activity timeline across chat, TTS, SFX, and fishtoys with type filters. Case-insensitive.

### Key Technical Details

- **Dual data source discovery**: Through systematic API probing (`test_api_probe.py`, `test_api_probe2.py`) and raw WebSocket frame analysis (`test_catchall.py`), we determined that fishtoy data is served exclusively via REST (`/v1/items/recent`), not Socket.IO. The fishclient library lists `fishtoy:queued` and `fishtoy:update` as socket events, but these do not fire in Season 5.
- **Complete event registry**: By decompiling the fishtank.live production JavaScript, we mapped the full socket event registry (60+ events across chat, TTS, SFX, polls, stocks, items, trading, challenges, and notifications). 18 high-value events are actively captured.
- **fishclient library patches** ([PR submitted](https://github.com/pluhian/fishclient)): 5 bugs fixed via monkey-patching:
  - Only processes 3-key msgpack packets (0x83 fixmap), silently dropping 4+ key packets
  - Malformed binary frames crash the listener and trigger unnecessary reconnection
  - Shutdown causes deadlock (thread.join() before websocket.close())
  - Spurious reconnection on clean shutdown (missing is_connected check)
  - Auto-registered disconnect handler has wrong signature, silently fails
- **Item type filtering**: Only FISHTOY and BIGTOY types are captured. WARTOY (user-vs-user effects), NORMAL_ITEM, and SPECIAL types are filtered out via the item catalog's `type` field.
- **Hidden metadata capture**: Fishtoy items like "Love Letter" include user-written content in a `metadata` field not displayed on the website UI.
- **Room name resolution**: TTS/SFX events contain room codes (e.g. `hwdn-5`). The backend resolves these to human-readable names (e.g. "Hallway") via the `/v1/live-streams` endpoint.
- **Stock price history**: Prices are polled every 60 seconds and stored in SQLite for historical tracking.
- **Automatic authentication**: The dashboard logs in via `/v1/auth/log-in` using email/password from a `.env` file, caches tokens to disk, and automatically re-authenticates on 401 responses. No manual cookie copying required.
- **Resilient socket reconnection**: If the Socket.IO connection drops, the reconnect loop gets fresh tokens from the auth manager and reconnects with exponential backoff (5s to 60s). Stale token reconnections are eliminated.
- **Event deduplication**: The fishtank server fires `tts:update` and `sfx:update` twice per message (once as "approved", again as "played"). Events are deduplicated by their unique event ID at the handler level before storage.
- **Chat noise filtering**: The server echoes TTS, SFX, and emote actions as `chat:message` events with system usernames ("tts", "sfx", "emote"). These are filtered at ingestion to prevent polluting chat data and analytics.
- **Notification filtering**: Season pass gift notifications (`"[user] gifted X season passes!"`) are filtered from director messages at ingestion.
- **Poll state reconstruction**: Since `poll:stop` is a single-fire event easily missed during reconnections, the dashboard reconstructs poll state from the database on mount, inferring results from the last `poll:vote` entry when `poll:stop` is missing.
- **Feature toggle monitoring**: Tracks real-time fishtoy/TTS/SFX category enable/disable state from `feature-toggles:update` events. Displays status badges with pricing on Analytics panels and shows category-level warnings on individual items.
- **Per-section time filters**: Analytics panels have independent All/7d/3d/24h filters so TTS and chat analytics can be viewed over different time ranges without affecting each other.
- **Health endpoint**: `/api/health` reports socket uptime, poller staleness, database status, and overall healthy/degraded assessment. Sensitive details (auth tokens, internal timestamps, event type breakdown) are excluded from the public response.
- **Backfill detection**: On startup, the fishtoy poller loads known event IDs from the database and compares them against the API response. Items that occurred during downtime are backfilled automatically instead of being silently skipped.
- **Catalog refresh**: Contestants and item catalog are refreshed every 10 minutes to pick up new freeloaders and fishtoys added mid-season without requiring a server restart.
- **Query separation**: Each event type category (chat, TTS/SFX, system events, fishtoys, polls) is fetched independently to prevent high-volume types from crowding low-volume types out of shared query limits.
- **Security hardening**: CORS restricted to configured origins (GET/HEAD only), per-IP rate limiting (120 req/60s, returns 429), WebSocket connection cap (50 max), endpoint sanitization (no auth tokens or internal state in public responses), generic exception handler (no stack traces), and automatic SQLite backup every 6 hours using the online backup API.
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

**Important**: Copy from the Network tab, not Application/Cookies tab.

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

Open http://localhost:3000 (Vite proxies API/WebSocket to the backend)

### Docker (recommended for deployment)

```bash
# Create .env file with your credentials
cat > .env << EOF
FISHTANK_EMAIL=your_email@example.com
FISHTANK_PASSWORD=your_password
ALLOWED_ORIGINS=http://your-server-ip:8000
EOF

# Build and run
docker compose up -d --build
```

Open http://localhost:8000

The database and token cache persist in a Docker volume (`fishtank-data`). Container is limited to 512MB RAM with a healthcheck that auto-restarts on failure. To stop: `docker compose down`. To rebuild after code changes: `docker compose up -d --build`.

## Testing

```bash
cd backend
pip install pytest
python -m pytest test_backend.py -v
```

50 tests covering database operations (store, query, analytics, dedup, purge), filter functions (chat echo, notification, TTS dedup), rate limiting, poll state reconstruction, and user search. Tests run against an in-memory SQLite database.

## API

Interactive API documentation is available at `/docs` (Swagger UI) and `/redoc` (ReDoc) when the server is running.

| Endpoint | Description |
|---|---|
| `GET /api/events` | List events. Query params: `type` (comma-separated), `limit`, `since_id` |
| `GET /api/fishtoys` | Fishtoy events with filters. Query params: `target`, `item_id`, `search`, `limit`, `offset` |
| `GET /api/stats` | Summary statistics. Query params: `since` (ISO timestamp) |
| `GET /api/health` | Comprehensive health check: socket uptime, poller status, last event per type, DB stats |
| `GET /api/status` | Connection status, browser client count, and auth status |
| `GET /api/items` | Item catalog (itemId to name/description/icon mapping) |
| `GET /api/contestants` | Current season contestant list (filtered to active season) |
| `GET /api/rooms` | Room code to name mapping (e.g. `hwdn-5` to `Hallway`) |
| `GET /api/stocks` | Current STO-X data (updated by poller every 60s) |
| `GET /api/stocks/history` | STO-X price history from SQLite. Query params: `ticker`, `limit` |
| `GET /api/stocks/count` | Actual count of stock history snapshots in database |
| `GET /api/analytics/tts-sfx` | TTS/SFX analytics: top rooms, top senders, hourly activity |
| `GET /api/analytics/chat` | Chat analytics: top chatters, hourly volume |
| `GET /api/analytics/peak-hours` | Combined hourly activity by type with peak/quietest hours |
| `GET /api/hidden-content` | Fishtoy events with metadata only. Query params: `target`, `search`, `limit`, `offset` |
| `GET /api/fishtoy-availability` | Fishtoy/bigtoy items with enabled/cooldown/cost status |
| `GET /api/polls` | Poll start and stop events. Query params: `limit` |
| `GET /api/polls/latest` | Reconstructed state of the most recent poll |
| `GET /api/notifications` | Director messages and announcements. Query params: `limit` |
| `GET /api/price-changes` | TTS/SFX price change history. Query params: `limit` |
| `GET /api/feature-toggles` | Current feature toggle states (fishtoys, TTS, SFX enable/disable + pricing) |
| `GET /api/user/{username}` | Search all event types for a specific user's activity (case-insensitive) |
| `GET /api/users/suggest` | Username autocomplete suggestions. Query params: `q` (min 2 chars) |
| `WS /ws` | Live event stream via WebSocket |

## Socket Events Captured

| Event | Description |
|---|---|
| `chat:message` | Chat messages |
| `tts:update` | Text-to-speech events (with room, sender, cost) |
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

## Project Structure

```
fishtank-dashboard/
    backend/
        server.py             FastAPI server + fishclient bridge + REST pollers
        database.py           SQLite storage layer with analytics queries
        auth.py               Automatic login, token caching, 401 re-auth
        cleanup_db.py         One-time DB cleanup (dedup TTS, purge system chat/gifts)
        test_backend.py       50 unit tests for database, filters, and rate limiting
        import_logs.py        Backfill JSONL logs into SQLite
        requirements.txt
        .env.example          Template for credentials
    frontend/
        src/
            App.jsx           Main layout with tab navigation, polls, notifications
            useWebSocket.js   WebSocket hook with auto-reconnect
            components/
                StatusBar.jsx     Connection status + live stats
                Panel.jsx         Reusable scrollable panel
                FishtoyCard.jsx   Collapsible fishtoy event cards
                ChatMessage.jsx   Chat message display
                ActivityCard.jsx  TTS/SFX display with room names
            tabs/
                AnalyticsTab.jsx      STO-X, contestants, analytics, polls, system events
                HiddenContentTab.jsx  Searchable hidden content archive
                UserSearchTab.jsx     User activity search with autocomplete
        index.html
        package.json
        vite.config.js
        tailwind.config.js
    scripts/
        fishtoy_poller.py     Standalone fishtoy-only REST poller
        fishtank_logger.py    Combined logger (REST + Socket.IO)
        import_logs.py        JSONL to SQLite backfill tool
        research/
            test_catchall.py          Raw WebSocket frame logger (all packet types)
            test_filtered_catchall.py Filtered catchall (skips chat/TTS/SFX noise)
            test_api_probe.py         REST endpoint discovery (round 1)
            test_api_probe2.py        REST endpoint discovery (round 2)
            test_cookie.py            Auth cookie validation
            test_initial_data.py      Initial-data event analysis
    .gitignore
    .dockerignore
    Dockerfile                Multi-stage build (Node frontend + Python backend)
    docker-compose.yml        Single-command deployment with persistent volume
    TECHNICAL_WRITEUP.md      Reverse engineering process documentation
    DEPLOY_WINDOWS.md         Standalone logger setup (Windows)
    DEPLOY_FEDORA.md          Standalone logger setup (Fedora)
    DEPLOY_DASHBOARD_WINDOWS.md  Dashboard setup (Windows)
    README.md
```

## Deployment

Currently hosted on a Vultr Cloud Compute VPS (Ubuntu 24.04 with Docker) for 24/7 event capture. Security hardening includes:

- UFW firewall (ports 22 and 80, port 80 restricted to Cloudflare IPs only)
- SSH key-based auth with password login disabled
- CORS restricted to server origin
- Per-IP rate limiting (120 req/60s)
- WebSocket connection cap (50)
- Docker memory limit (512MB, no swap)
- Docker healthcheck with auto-restart
- Automatic SQLite backup every 6 hours (first after 5 min)
- Docker log rotation (50MB max, 3 files)
- Ubuntu unattended security upgrades
- UptimeRobot monitoring on `/api/health`

To update after pushing code changes:
```bash
cd /opt/fishtank-dashboard
git pull
docker compose up -d --build
```