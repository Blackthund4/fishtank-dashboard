# Fishtank Dashboard

Real-time event monitoring dashboard for [fishtank.live](https://www.fishtank.live), an interactive 24/7 reality show. Captures and displays fishtoy redemptions (including hidden metadata like love letter contents), chat messages, TTS, SFX, and other events via a dual data source architecture.

## Architecture

```
Browser (React)  <--WebSocket-->  Backend (FastAPI)  <--Socket.IO-->  fishtank.live
                 <--REST API--->        |            <--REST poll-->  /v1/items/recent
                                    SQLite DB
```

**Backend** captures events from fishtank.live using two methods:
- **REST polling** (`/v1/items/recent` every 2s): Fishtoy redemptions including hidden metadata. The fishtank API does not broadcast fishtoy events over Socket.IO, so polling this endpoint is the only way to capture them.
- **Socket.IO** (via [fishclient](https://pypi.org/project/fishclient/) with patches): Real-time push for chat messages, TTS, and SFX events.

Item names are resolved from the `/v1/items` catalog and contestant data is loaded from `/v1/contestants` on startup.

**Frontend** is a React + Tailwind CSS dashboard with contestant/target filtering, item type filtering, metadata search, and clickable fishtoy cards. Historical data loads from the REST API on page load, then live events stream in via WebSocket.

### Key Technical Details

- **Dual data source discovery**: Through systematic API probing and raw WebSocket frame analysis, we determined that fishtoy data is served exclusively via REST (`/v1/items/recent`), not Socket.IO. The fishclient library lists `fishtoy:queued` and `fishtoy:update` as socket events, but these do not fire in Season 5. Chat, TTS, and SFX still arrive via Socket.IO using msgpack binary serialization.
- **fishclient library patches**: The library has several bugs that required monkey-patching:
  - Only processes 3-key msgpack packets (0x83 fixmap), silently dropping 4+ key packets
  - Malformed binary frames crash the listener and trigger unnecessary reconnection
  - Shutdown causes deadlock (thread.join() before websocket.close())
  - Auto-registered disconnect handler has wrong signature, silently fails
- **Hidden metadata capture**: Fishtoy items like "Love Letter" include user-written content in a `metadata` field that isn't displayed on the website UI. The dashboard surfaces this content prominently.
- **SQLite persistence**: All events are stored as JSON with indexed fields for efficient querying, filtering, and full-text search across metadata.

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

### Get Your Auth Cookie

1. Log into fishtank.live in your browser
2. Open DevTools (F12) > **Network** tab
3. Filter by `api.fishtank.live`
4. Click any request, find the `Cookie:` header in Request Headers
5. Copy the value after `sb-wcsaaupukpdmqdjcgaoo-auth-token=` (short ~33 char string)

**Important**: Copy from the Network tab, not Application/Cookies tab. The Application tab shows Supabase JWTs which will not work.

## Running

### Single process (recommended)

Build the frontend first, then run just the backend. It serves the built frontend as static files.

```bash
cd frontend
npm install
npm run build

cd ../backend
export FISHTANK_COOKIE='your_cookie_value'    # Linux/Mac
$env:FISHTANK_COOKIE = 'your_cookie_value'    # PowerShell
python server.py
```

Open http://localhost:8000

### Development mode (two terminals)

If you're making changes to the frontend and want live reload:

Terminal 1 (backend):
```bash
cd backend
export FISHTANK_COOKIE='your_cookie_value'    # Linux/Mac
$env:FISHTANK_COOKIE = 'your_cookie_value'    # PowerShell
python server.py
```

Terminal 2 (frontend dev server):
```bash
cd frontend
npm run dev
```

Open http://localhost:3000 (Vite proxies API/WebSocket to the backend)

## API

| Endpoint | Description |
|---|---|
| `GET /api/events` | List events. Query params: `type` (comma-separated), `limit`, `since_id` |
| `GET /api/fishtoys` | Fishtoy events with filters. Query params: `target`, `item_id`, `search`, `limit`, `offset` |
| `GET /api/stats` | Summary statistics (counts, top targets, top senders, total spend) |
| `GET /api/items` | Item catalog (itemId to name/description/icon mapping) |
| `GET /api/contestants` | Current season contestant list |
| `GET /api/rooms` | Room code to name mapping (e.g. `hwdn-5` to `Hallway`) |
| `GET /api/stocks` | Live stock market data (refreshes from fishtank API on each call) |
| `GET /api/stocks/history` | Stock price history from SQLite. Query params: `ticker`, `limit` |
| `GET /api/analytics/tts-sfx` | TTS/SFX analytics: top rooms, top senders, hourly activity |
| `GET /api/analytics/chat` | Chat analytics: top chatters, hourly volume |
| `GET /api/hidden-content` | Fishtoy events with metadata only. Query params: `target`, `search`, `limit`, `offset` |
| `GET /api/fishtoy-availability` | Fishtoy/bigtoy items with enabled/cooldown/cost status |
| `GET /api/polls` | Poll events (start, stop, vote). Query params: `limit` |
| `GET /api/notifications` | Director messages and announcements. Query params: `limit` |
| `GET /api/price-changes` | TTS/SFX price change history. Query params: `limit` |
| `GET /api/status` | Connection status and browser client count |
| `WS /ws` | Live event stream via WebSocket |

## Project Structure

```
fishtank-dashboard/
    backend/
        server.py           FastAPI server + fishclient bridge + REST pollers
        database.py         SQLite storage layer with analytics queries
        import_logs.py      Backfill JSONL logs into SQLite
        requirements.txt
    frontend/
        src/
            App.jsx          Main layout with tab navigation
            useWebSocket.js  WebSocket hook with auto-reconnect
            components/
                StatusBar.jsx    Connection status + live stats
                Panel.jsx        Reusable scrollable panel
                FishtoyCard.jsx  Collapsible fishtoy event cards
                ChatMessage.jsx  Chat message display
                ActivityCard.jsx TTS/SFX display with room names
            tabs/
                AnalyticsTab.jsx    Stock market, contestants, TTS/SFX + chat analytics
                HiddenContentTab.jsx  Searchable hidden content archive
        index.html
        package.json
        vite.config.js
        tailwind.config.js
    README.md
```

## License

MIT
