# Reverse Engineering a Live Reality Show's Real-Time API

## How I Built a Hidden Data Capture System for fishtank.live

### Context

fishtank.live is a 24/7 interactive reality show where viewers spend tokens to trigger real-world events in the house (called "fishtoys"), send text-to-speech messages, play sound effects, and chat. Some fishtoys, like "Love Letter," allow viewers to write custom messages that are printed and delivered to contestants. The contents of these messages are hidden from other viewers unless the contestant reads them aloud on camera.

I wanted to capture these hidden messages and other fishtoy data programmatically, build a logging system, and eventually create a real-time dashboard. The site has no public API documentation, so this required reverse engineering the entire data pipeline from scratch.

### The Starting Point

The only lead was a screenshot from the fishtank community showing raw event data with fields like `itemId`, `target`, `displayName`, `cost`, and crucially, a `metadata` field containing the hidden love letter text. The data format looked like it came from a WebSocket frame, but the exact source was unknown.

The screenshot also revealed the site uses Supabase for authentication (visible from cookie names like `sb-wcsaaupukpdmqdjcgaoo-auth-token`).

### Phase 1: Understanding the Protocol

Research turned up [fishclient](https://pypi.org/project/fishclient/), a third-party Python library for connecting to fishtank.live's Socket.IO server. The library's PyPI page listed event names like `fishtoy:queued`, `fishtoy:update`, `chat:message`, `tts:queued`, and others.

The first challenge was authentication. The fishclient library expects a cookie value, but which one? The site's Supabase integration stores session data across multiple cookies and JWTs. After trial and error:

**What didn't work:** Copying values from Chrome's Application > Cookies tab. Those are Supabase session JWTs (long base64 strings) that the library couldn't use.

**What worked:** Copying the raw cookie value from the Network tab > Request Headers > Cookie header. The actual auth token is a short (~33 character) string, not a JWT.

This distinction cost significant debugging time and became a key documentation point for deployment guides.

### Phase 2: Patching Library Bugs

With authentication working, the fishclient library connected successfully, but events were being dropped. Systematic debugging revealed 5 bugs in fishclient 0.1.4:

1. **Packet filtering bug:** The library only processes msgpack packets with exactly 3 keys (0x83 fixmap header byte). Server responses with 4+ keys are silently dropped. Fix: process all binary frames through the msgpack unpacker regardless of header byte.

2. **Crash on malformed frames:** Any unparseable binary frame crashes the listener thread and triggers an unnecessary reconnection. Fix: wrap `handle_packed()` in try/except.

3. **Shutdown deadlock:** `disconnect()` calls `thread.join()` before `websocket.close()`, causing the thread to block forever waiting for data on an open socket. Fix: close the socket first, then join.

4. **Reconnect on clean shutdown:** The `_patched_listen` method checks `is_connected` after exceptions but the original doesn't, causing spurious reconnection attempts during intentional shutdown.

5. **Disconnect handler signature mismatch:** The library auto-registers a disconnect handler that expects zero arguments, but Socket.IO passes one. The handler silently fails on every server disconnect.

All five were fixed via monkey-patching (replacing instance methods with `types.MethodType`) rather than forking the library, keeping the fix self-contained in a single script.

### Phase 3: The Initial-Data Delay Problem

With bugs patched, chat, TTS, and SFX events streamed in successfully. But a performance issue emerged: after connecting, all events were delayed by roughly 60 seconds.

Diagnosis revealed the `initial-data` event was the culprit. This event fires once on connect and contains the entire site state as a massive JSON blob. When the logger tried to serialize this payload to disk, it blocked the listener thread, causing all subsequent events to queue up behind it.

Solution: exclude `initial-data` from the default event list. The delay disappeared immediately, and events arrived with sub-second latency (confirmed by comparing event timestamps against wall clock time using a dedicated delay measurement script).

### Phase 4: The Fishtoy Mystery

Here's where it got interesting. Chat, TTS, and SFX events all worked perfectly. But fishtoy events (`fishtoy:queued`, `fishtoy:update`) never appeared. Despite the fishclient documentation listing them as valid event names, and despite users actively redeeming fishtoys on the live site, the logger captured nothing.

This required methodical elimination:

**Hypothesis 1: Wrong event names.** Built a raw WebSocket frame logger that intercepts at the `websocket.recv()` level, before any library parsing. This logged every single frame the server sent, both text and binary, with full event name extraction. Ran it during active fishtoy usage. Result: zero fishtoy events in any form.

**Hypothesis 2: Text-encoded events.** The fishclient library only processes binary (msgpack) frames. Socket.IO can also send events as text frames (`42["event_name", {...}]`). Extended the catch-all logger to decode both transport types. Result: still nothing. All Socket.IO events came as binary msgpack, and none were fishtoy-related.

**Conclusion: Fishtoy data is not broadcast over Socket.IO at all.** The event names on the fishclient PyPI page are either outdated (from earlier seasons) or only fire for the user who redeemed the fishtoy, not for all connected clients.

### Phase 5: Finding the Real Source

If the data isn't pushed via WebSocket, it must be available via REST. Systematic API endpoint probing with authenticated GET requests against `api.fishtank.live` tested 50+ URL patterns based on naming conventions observed in the existing endpoints.

Results from probing:

| Endpoint | Status | Notes |
|---|---|---|
| `/v1/items/recent` | **200** | The fishtoy data source |
| `/v1/items` | 200 | Item catalog (names, descriptions, icons) |
| `/v1/tts` | 200 | TTS message queue |
| `/v1/contestants` | 200 | Current season contestants |
| `/v1/auth` | 200 | Session validation |
| `/v1/stocks` | 200 | Stock market feature |
| `/v1/live-streams` | 200 | Camera stream URLs |
| `/v1/items/fishtoy` | 500 | Exists but errors (needs params?) |
| `/v1/items/used` | 500 | Exists but errors |
| `/v1/items/history` | 500 | Exists but errors |

**`/v1/items/recent`** returned exactly the data structure from the original screenshot: `id`, `createdAt`, `updatedAt`, `status`, `userId`, `displayName`, `clanTag`, `itemId`, `cost`, `target`, `secondaryTarget`, `metadata`. The last 10 fishtoy redemptions, including love letter contents in the `metadata` field.

This endpoint has a hard cap of 10 results per request (the `limit` parameter is accepted but ignored). Polling every 2 seconds provides reliable capture with a 10-item buffer per poll cycle.

### Phase 6: The Final Architecture

The working system uses a dual data source approach:

```
Socket.IO (via fishclient)     REST Polling (/v1/items/recent)
        |                                |
   chat, TTS, SFX                   fishtoys
        |                                |
        +----------- SQLite DB ----------+
                        |
              FastAPI WebSocket Bridge
                        |
              React Dashboard (browser)
```

**Why two data sources?** Because the fishtank server uses different delivery mechanisms for different event types. Chat, TTS, and SFX are pushed in real-time over Socket.IO using msgpack binary serialization. Fishtoy redemptions are only available via REST polling. There's no single unified event stream.

The item catalog (`/v1/items`) provides human-readable names for item IDs, so the dashboard shows "Love Letter" instead of "Item #470". The contestant list (`/v1/contestants`) provides metadata for the target filtering UI.

### What I Built

**Standalone fishtoy poller:** Single-file Python script. Polls `/v1/items/recent` every 5 seconds, deduplicates using a rolling ID window, resolves item names from the catalog, filters by item type (FISHTOY/BIGTOY only), writes to session-timestamped JSONL log files and terminal. ~145 lines.

**Combined event logger:** Runs both the REST poller and Socket.IO connection in parallel threads. Thread-safe JSONL logging with `threading.Lock()`. Captures fishtoys, chat, TTS, SFX, polls, director messages, stock events, and system events in a single log. ~380 lines.

**Real-time dashboard:** FastAPI backend + React/Tailwind frontend with five tabs:
- **Dashboard tab:** Three-column layout with unified fishtoy/activity feed (virtual scrolling, keyset pagination, time-travel navigation), chat panel with superchat pinned banners and countdown timers, STO-X ticker with inline SVG sparklines, target filtering with drill-down stats, metadata search, and Last 24h sidebar with leaderboards. Director message banner and live poll bar with animated vote percentages and per-option colors appear at the top when active.
- **Analytics tab:** STO-X cards with 7 range buttons (1h–IPO) and sort options, contestant grid with stock prices, TTS/SFX analytics with sentiment mood badges and per-section time filters, chat analytics with sentiment analysis, poll history with colored vote bars and crown icons, director message timeline, price change log, fishtoy availability status board, and system events. Supports anchor-based time-travel with drag-to-pan.
- **Charts tab:** STO-X price history (LineChart, auto-downsampled), token spend trends (stacked BarChart + LineChart with TTS/SFX/Fishtoy/Poll/Superchat series and toggles), and chat volume (BarChart + top chatters). Nine time ranges from 30m to all-time.
- **Hidden Content tab:** Dedicated searchable archive of fishtoy metadata (love letters, custom messages) with target filtering sidebar. Virtual scrolling with keyset pagination.
- **User Search tab:** Cross-event-type username search with autocomplete, unified activity timeline, and type filters. Virtual scrolling.

SQLite persistence with extracted columns for performance-critical fields (avoiding `json_extract` in aggregates). Stock price history polled every 60 seconds. Browser WebSocket bridge for live updates. Single-process deployment (backend serves built frontend as static files). 35 REST API endpoints. 21 captured socket event types.

**Research/diagnostic scripts:** API endpoint probers, raw WebSocket frame loggers, filtered catchall with auto-reconnect, cookie validation tool. These documented the investigation process and remain useful for discovering new events.

**Open source contribution:** Submitted a PR to the fishclient library with all 5 bug fixes, each with a detailed explanation of the bug, the root cause, and the fix.

### Phase 7: Discovering Unknown Events

With the dashboard running, I needed to identify additional event types that the fishclient documentation didn't cover. Director messages (announcements from the show's producers to all viewers) appeared on the website but weren't being captured.

I built a filtered catchall script that logged every socket event except the high-volume ones (chat, TTS, SFX). This made rare events immediately visible. Running it during active show hours revealed three new events:

- `poll:vote` with live vote tallies as `[{value, score}, ...]`
- `live-stream:status` with camera online/offline status per room
- `notification:global` with director announcement text

But I still didn't have the poll creation and close events.

### Phase 8: Decompiling the Production JavaScript

Rather than waiting for every event type to fire naturally, I took a different approach. The fishtank.live website's JavaScript must contain listeners for every socket event the server can send. Those event name strings are in the bundled code.

Using the browser's Debugger tab, I searched across all loaded JavaScript files for `poll:`. This led to the complete socket event registry, a single object mapping human-readable names to event strings:

```javascript
POLL_START: "poll:start",
POLL_STOP: "poll:stop",
POLL_VOTE: "poll:vote",
NOTIFICATION_GLOBAL: "notification:global",
STOCK_PRICES: "stock:prices",
// ... 60+ events total
```

This gave us the definitive, complete map of every event the server can send, organized into categories: chat (11 events), TTS/SFX (8 events), items (7 events), polls (3 events), stocks (7 events), notifications (3 events), trading (9 events), challenges (4 events), streams (5 events), and system events.

From this registry, I selected the high-value events to capture: the ones that represent show-critical moments (polls, director messages, stock changes, price adjustments, feature toggles, superchats) without adding noise from per-user events like trading or DMs. The event list has grown to 21 as new features were added (superchats, chat room tracking, viewer presence).

### Phase 9: Solving the Duplicate Event Problem

With expanded event capture, a subtle bug appeared: TTS messages were being logged twice. Investigation revealed that the server fires two events per TTS message: `tts:queued` when it enters the queue, and `tts:update` when it plays. Both contain the same data including cost.

This caused three problems: the activity feed showed duplicates, the analytics counted each TTS twice, and the token spend counter was inflated. The same issue affected SFX events.

The fix was to capture only the `:update` events and drop `:queued` entirely. The update event contains the complete data. Database analytics queries also needed updating since they used `LIKE 'tts%'` which matched both `tts:update` and the newly captured `tts:price` events. Changing to exact `event_type = 'tts:update'` queries fixed the analytics without affecting the price change tracking.

### Phase 10: Data Quality and Filtering

Several data quality issues emerged during live testing:

**Contestant bleed:** The `/v1/contestants` endpoint returns contestants from all seasons, not just the current one. The analytics page showed dozens of irrelevant people from previous seasons. Fixed by filtering for `season == "5"` at load time, with a fallback to all contestants if the season field doesn't exist.

**Item type pollution:** The `/v1/items/recent` endpoint returns all recently used items, including wartoys (user-vs-user effects like "Shrink Ray") and normal items. These aren't relevant to show events. The item catalog's `type` field distinguishes five categories: FISHTOY (45 items), BIGTOY (2), WARTOY (16), NORMAL_ITEM (440), and SPECIAL (3). The poller now filters to only FISHTOY and BIGTOY, with a safe default of capturing unknown items.

**Room code resolution:** TTS/SFX events contain room codes like `hwdn-5` instead of human-readable names. The `/v1/live-streams` endpoint provides the mapping (25 rooms in Season 5). The backend loads this on startup and serves it to the frontend, so the dashboard shows "Hallway" instead of "hwdn-5".

### Technical Decisions Worth Discussing

**Monkey-patching vs. forking:** Initially chose to patch fishclient at runtime via monkey-patching to keep the fix in one file. Later vendored a full fork (`backend/vendor/fishclient/`) installed via `file:` reference in `requirements.txt`. The vendored approach is more maintainable: patches are visible in the codebase, and we can make deeper fixes (like the `_patched_listen` reconnection control) that monkey-patching can't reach. A PR with all fixes was submitted to the original project.

**REST polling vs. WebSocket for fishtoys:** The polling approach has inherent latency (up to 2 seconds) and a theoretical event loss window (if 10+ fishtoys are redeemed within 2 seconds). In practice, fishtoys cost tokens, so redemption rate is low enough that 2-second polling with a 10-item buffer has zero observed loss.

**SQLite with JSON + extracted columns:** Chose to store complete event payloads as JSON for forward compatibility (new API fields captured without schema changes), but performance-critical fields (`sentiment`, `cost`, `display_name`, `target`, `room`, `metadata`, `item_id`, `feature`) are extracted into real indexed columns on insert. All aggregate queries use these extracted columns instead of `json_extract()`, which was causing OOM kills on the 600k+ row table. The hybrid approach preserves the full raw payload while giving indexed query performance where it matters.

**seen_ids pruning:** The deduplication set only keeps IDs from the last 3 polls (max 30 IDs). Without this, a 30-day season would accumulate ~690 MB of set memory. The rolling window is safe because the API returns items in reverse chronological order, so an item that's fallen off the 10-item response will never reappear.

**JS decompilation over passive observation:** Waiting for every socket event to fire naturally could take weeks (some events like `stock:split` or `stock:remove` only happen during eliminations). Decompiling the production JavaScript to extract the event registry took 5 minutes and gave us the complete picture immediately. This is a technique that transfers directly to SE work: when documentation is incomplete, the source code is the ground truth.

**Selective event capture:** The full registry has 60+ events, but most are per-user (trading, DMs, inventory changes) and would add noise without value for a broadcast dashboard. Selecting the 18 show-critical events required understanding the domain well enough to distinguish signal from noise.

### Phase 11: Reverse Engineering the Auth Flow

The dashboard required a browser cookie that expired periodically. Running 24/7 on a VPS meant manually copying cookies was not viable. The Supabase-style cookie (`sb-wcsaaupukpdmqdjcgaoo-auth-token`) contained two JWTs: an access token (15-minute lifetime) and a refresh token (30-day lifetime).

Initial attempts to refresh tokens directly through the Supabase GoTrue endpoint (`/auth/v1/token?grant_type=refresh_token`) failed with 401. The anon API key was needed but couldn't be found. Searching the production JavaScript, localStorage, and request headers all came up empty.

The breakthrough came from logging out and back in with DevTools open. This revealed that fishtank.live wraps Supabase behind their own API at `/v1/auth/log-in`. A simple POST with `{email, password}` returns a full session object containing `access_token`, `refresh_token`, and `live_stream_token`. No API key required.

This meant the entire auth flow could be automated:
1. Store email/password in a `.env` file
2. POST to `/v1/auth/log-in` on startup
3. Construct the cookie from the returned tokens
4. Cache tokens to disk for reuse across restarts
5. Re-authenticate automatically when any REST poller gets a 401

The token cache file uses `chmod 600` on Linux for owner-only access. Email is masked in console logs. Credentials are never exposed through any API endpoint.

### Phase 12: Resilient Socket Reconnection

During extended testing, a subtle failure mode emerged. The Socket.IO connection would drop (keepalive timeout, server restart, network blip), the listen thread would break out of its loop, but `is_connected` was never set to `False`. The reconnect loop polled this flag every 2 seconds to check if the connection was alive. Since it was never cleared, the loop assumed the connection was still active and never attempted to reconnect. The dashboard silently stopped capturing socket events while the web UI and REST pollers continued working normally.

The fix had two parts: set `is_connected = False` in the listen thread on error, and rebuild the connection from scratch on each reconnect attempt rather than reusing the old client. Each reconnect gets fresh tokens from the auth manager, so stale token reconnections are eliminated. Exponential backoff (5s to 60s) prevents hammering the server during extended outages.

### Phase 13: User Activity Search

Added a cross-event-type search that queries chat messages, TTS, SFX, and fishtoy events by username. Each event type stores the username in a different JSON path (`$.user.displayName` for chat, `$.displayName` for TTS/SFX/fishtoys), so the search runs four separate parameterized queries and merges the results into a unified timeline. The frontend provides type filter buttons and displays room names, costs, and hidden fishtoy content inline. Search is case-insensitive using SQLite's `LOWER()` function. An autocomplete system queries distinct displayNames across all event types with a prefix match, debounced at 250ms to avoid hammering the API.

### Phase 14: Data Quality Filtering

Three data quality problems emerged during extended testing that were corrupting analytics:

**TTS/SFX system echoes in chat.** The server echoes every TTS and SFX message as a `chat:message` event with the displayName "tts" or "sfx". This meant every TTS appeared twice in the dashboard (once correctly as a TTS event, once as a fake chat message), and "tts" ranked as the most active chatter in analytics. The fix filters these at the event handler level before database storage, checking if the chat message's displayName matches a known system username.

**TTS/SFX duplicate events.** The server fires `tts:update` twice per TTS message with identical content, timestamp, and cost. A content-hash deduplication system tracks a rolling window of recent event hashes (displayName + message + room + cost) and drops duplicates within a 5-second window. The hash map is pruned at 200 entries to prevent memory growth.

**Season pass gift notification noise.** `notification:global` events include season pass gift announcements ("[user] gifted X season passes!") alongside actual director messages. These are filtered at ingestion by checking for "gifted" and "season pass" in the message text (case-insensitive).

All three filters operate at the ingestion layer rather than the query or display layer. This prevents polluted data from entering the database, keeping analytics accurate and storage clean.

### Phase 15: Poll Resilience and UI Polish

The poll system had a fundamental fragility: `poll:start` and `poll:stop` are single-fire events. If the socket connection is briefly down at the exact moment either fires, that event is lost permanently. `poll:vote` events fire continuously (every time someone votes) so they're nearly impossible to miss, but without `poll:start` there's no question text, and without `poll:stop` there's no winner announcement.

The fix reconstructs poll state from the database. A `/api/polls/latest` endpoint finds the most recent `poll:start`, checks for a matching `poll:stop`, and retrieves the last `poll:vote` entry. If the poll has no `poll:stop`, it's shown as active with the latest vote tallies. The frontend loads this on mount, so poll state survives page refreshes and reconnections.

Additional UI improvements in this phase: events sorted by actual timestamp (not database insertion order) to fix chronological display, UTC-consistent date comparison in timestamp formatting to fix timezone mismatches, contestants sorted by endorsement count, "Stock Market" renamed to "STO-X" to match the product's actual branding, and the director notification banner's "+X more" text linked to the Analytics tab for viewing full history.

### Phase 16: TTS Dedup Revisited

The initial content-hash dedup (Phase 14) used a 5-second window matching displayName, message, room, and cost. It failed because the server sends the same TTS as two separate `tts:update` events: first with `status: "approved"`, then 30-60 seconds later with `status: "played"`. Same event ID, same content, but different `updatedAt` and `status` fields, arriving far outside the 5-second window.

The fix was much simpler than the original approach. Every TTS/SFX event has a unique `id` field. Deduplication by event ID with a 5-minute rolling window catches both status transitions regardless of timing. The old content-hash system was replaced entirely. A database cleanup script (`cleanup_db.py`) was also created to retroactively deduplicate historical data and purge system chat echoes and gift notifications in one pass.

A related issue surfaced in the poll history: `get_polls` queried `poll:start`, `poll:stop`, AND `poll:vote` with a limit of 50. A single poll generates hundreds of vote events, so the 50 most recent rows were all votes, pushing the start/stop entries out of results entirely. The fix was to query only `poll:start` and `poll:stop` in the SQL, since vote data is handled separately by the poll reconstruction endpoint.

### Phase 17: Feature Toggle Monitoring

The `feature-toggles:update` socket events contain `{feature, enabled, metadata}` payloads. The `feature` field identifies the category (fishtoys, tts, sfx, ai-sfx), `enabled` is a boolean, and `metadata` sometimes contains the current price (e.g. "425" for SFX cost). These fire in bursts when production toggles categories on and off.

The backend tracks the latest state per feature name in a dict, loads initial state from the database on startup, and exposes it via `/api/feature-toggles`. The frontend displays status badges on the TTS/SFX and Fishtoy Availability panels, showing ON/OFF with pricing where available. When fishtoys are globally disabled, individual items marked ON show "(fishtoys are currently disabled)" and a banner explains the items can't be used until production re-enables them.

### Phase 18: Analytics Refinement

Two usability issues in the Analytics tab: all analytics showed all-time data with no way to focus on recent activity, and STO-X cards had a fixed sort order.

Per-section time filters (All/7d/3d/24h) were added as independent controls on the TTS/SFX and Chat Analytics panels. Each filter has its own React state and useEffect, so changing one doesn't trigger a refetch for the other. The backend's `get_stats`, `get_tts_sfx_analytics`, and `get_chat_analytics` functions accept an optional `since` ISO timestamp parameter, appending `AND timestamp_local >= ?` to the WHERE clause.

STO-X sort options (Highest Value, Movers Up, Movers Down) sort by `currentPrice`, `currentPrice - today` descending, or ascending respectively. Contestant sort toggles between endorsement count and STO-X price. Both use spread copies to avoid React state mutation.

### Phase 19: Production Readiness

Three additions to make the project deployable and maintainable:

**Docker.** A multi-stage Dockerfile uses Node 20 to build the frontend and Python 3.12-slim to run the backend. The final image contains no Node.js runtime. `docker-compose.yml` mounts a persistent volume for the SQLite database and token cache, reads credentials from environment variables, and auto-restarts. Database and token cache paths are configurable via `FISHTANK_DB_PATH` and `FISHTANK_TOKEN_CACHE` environment variables, with defaults that preserve the existing non-Docker behavior.

**Health endpoint.** `/api/health` reports socket connection status and uptime, fishtoy poller staleness (flagged at >30s), stock poller staleness (flagged at >120s), last event timestamp per event type, total event count, database accessibility, and auth status. Returns an overall "healthy" or "degraded" status with a specific issues list. Designed for external monitoring tools or a quick manual check.

**Unit tests.** 110 pytest tests covering database operations (store, query, filter, paginate, analytics with time ranges, dedup, purge, extracted columns), filter functions (chat echo detection for tts/sfx/emote, notification gift detection, TTS event ID dedup), rate limiting (allows traffic, rejects over limit, per-IP isolation, prune lifecycle), poll state reconstruction (complete, missing stop, empty), user search (case-insensitive, cross-event-type, autocomplete), sentiment analysis, superchat handling, and chart data queries. All tests run against an in-memory SQLite database with a fresh schema per test.

### Phase 20: Security Hardening and VPS Deployment

With the show on day 19 of 30, capturing events 24/7 became urgent. The dashboard needed to move from a local Windows machine to a VPS that runs unattended.

**Security hardening (pre-deployment).** Seven code-level changes before exposing the server publicly:

1. CORS: configurable via `ALLOWED_ORIGINS` env var, methods restricted to GET and HEAD only. HEAD was added specifically for UptimeRobot health monitoring, which sends HEAD requests by default.
2. Rate limiting: custom middleware tracking per-IP request counts in a time-windowed dict. 120 requests per 60 seconds per IP, returns 429 when exceeded, skips static file serving, auto-prunes stale entries when the IP count exceeds 1000.
3. Endpoint sanitization: `/api/health` and `/api/status` stripped of auth token expiry timestamps, login counts, event type breakdowns, and socket connection timestamps. Only operational status exposed publicly.
4. WebSocket limit: max 50 concurrent browser connections, rejects with close code 1013 when exceeded.
5. Database backup: periodic SQLite backup using the online backup API (`conn.backup()`) every 6 hours, with the first backup 5 minutes after startup. Initial implementation used `shutil.copy2` which risks corruption during active writes; switched to SQLite's built-in backup API for consistent copies.
6. Docker limits: 1GB memory cap with no swap, healthcheck via `/api/health` every 60 seconds with auto-restart on failure.
7. Generic exception handler: suppresses Python stack traces in production, returning "Internal server error" instead of exposing file paths and library versions.

**Query limit crowding.** A recurring bug class surfaced across three features: polls, activity panel, and system events. When multiple event types share a single database query with a row limit, high-volume types consume the entire limit and push low-volume types out of results entirely. Chat messages (278k) crowded TTS/SFX (5.8k) out of the activity panel. Feature toggle events (108) crowded stock events (2) out of system events. Poll votes (4.7k) crowded poll start/stop (8 total) out of the poll query. The fix in every case was the same: fetch each event type category independently with its own limit, then merge on the frontend if needed. This is now a design rule for the project: never mix event types with different volumes in a single limited query.

**Backfill detection.** The fishtoy poller previously skipped everything on its first poll to establish a baseline. This meant any fishtoys that happened during server downtime were silently lost. The fix loads known fishtoy event IDs from the database before the first poll and compares each API item against them. Items not in the database are stored and logged with a `[BACKFILL]` prefix.

**VPS deployment.** Vultr Cloud Compute (Ubuntu 24.04 with Docker). Server hardening: SSH key-based auth with password login disabled, Docker log rotation (50MB max, 3 files), Ubuntu unattended security upgrades. Historical database (291k events) uploaded via SCP. UptimeRobot configured for external monitoring on `/api/health`.

**Custom domain and Cloudflare.** Registered `fish-dash.com` through Cloudflare Registrar and configured it as a reverse proxy to the VPS. DNS A records point to the VPS IP with Cloudflare's proxy enabled, providing DDoS protection and CDN caching. SSL mode is Full (Strict) with a Cloudflare Origin Certificate installed on the server — the backend runs uvicorn with SSL directly, so encryption is end-to-end from browser to origin. A cache bypass rule prevents Cloudflare from caching `/api/*` and `/ws` paths. Bot Fight Mode is enabled with UptimeRobot IPs allowlisted via IP Access Rules. Email Address Obfuscation had to be disabled because it corrupts API JSON responses containing email-like strings.

The UFW firewall accepts port 443 connections only from Cloudflare's published IP ranges, preventing anyone from bypassing Cloudflare by hitting the VPS IP directly. The only public entry points are port 22 (SSH, key-only) and port 443 (Cloudflare-only).

An unexpected issue surfaced with UptimeRobot: it sends HEAD requests by default, but FastAPI's `@app.get()` decorator only handles GET. HEAD requests returned 405 Method Not Allowed. The fix required two changes: adding HEAD to the CORS allowed methods, and changing the health endpoint's decorator from `@app.get()` to `@app.api_route("/api/health", methods=["GET", "HEAD"])`. Cloudflare's Bot Fight Mode also blocked UptimeRobot's automated requests until it was disabled.

### Phase 21: Hardening and Polish

Several rounds of improvements to make the codebase more robust and the frontend usable across devices.

**Thread safety.** The backend runs 5 daemon threads that read and write shared state (item catalog, contestant list, room map, stock data, feature toggles, rate limit counters, WebSocket client set, dedup window). Each shared resource got a dedicated `threading.Lock()`. API endpoints return shallow copies under lock to prevent mutation during iteration. All three REST pollers were wrapped in `try/finally` with `session.close()` to prevent leaked HTTP connections on exceptions.

**Mobile responsive layout.** The dashboard was originally designed for desktop widths. A pass across all components converted fixed-width layouts to responsive patterns using Tailwind breakpoints: `flex-col md:flex-row` on the main layout, `w-full md:w-[420px] md:shrink-0` for side panels, `hidden sm:inline` on tab labels (icon-only on small screens), `flex-wrap` on the notification banner and poll bar, and responsive grids (`grid-cols-2 sm:grid-cols-3 lg:grid-cols-5`) on stock and contestant cards.

**Tank Time clock and local timezone.** The StatusBar now shows a live clock in the show's timezone (America/New_York, 12-hour format) updated every second. A secondary display shows the viewer's local time with timezone abbreviation (e.g. "14:32:05 BST"), hidden below the `md` breakpoint to save space on smaller screens.

**STO-X time filters.** The Analytics tab's STO-X section gained time filter buttons (All/1hr/6hr/1day). Selecting a period computes reference prices from the filtered stock history, so the change percentages and movers sort reflect the chosen window rather than all-time data.

**Docker security.** The container previously ran as root. A non-root `dashboard` user was added via `groupadd`/`useradd`, with an `entrypoint.sh` script that uses `gosu` to chown the data volume as root, then drops to the unprivileged user before starting the server. Memory limit was raised from 512MB to 1.5GB based on observed usage (including index creation on startup), with swap disabled (`memswap_limit` equal to `mem_limit`).

**Dependency pinning.** Backend `requirements.txt` added upper bound constraints (e.g. `fishclient>=0.1.4,<1.0.0`) to prevent breaking changes from major version bumps. Frontend `package.json` switched from caret ranges (`^18.3.1`) to tilde ranges (`~18.3.1`) to allow patches but not minor version changes.

**CORS default.** The `ALLOWED_ORIGINS` environment variable now defaults to `https://fish-dash.com,https://www.fish-dash.com` instead of a wildcard, so a fresh deployment is locked down without manual configuration.

**Shared utilities.** Timestamp formatting logic (`formatTime` and `formatDateTime`) was duplicated across six frontend components. Extracted to a shared `utils/formatTime.js` module. The functions handle Unix timestamps (seconds or milliseconds), ISO strings, and smart date display (time-only for today, date+time for older events).

### Phase 22: Superchat System

Fishtank added "superchats" — token-purchased pinned chat messages with a chosen duration. Two socket events (`super-chat:new`, `super-chat:delete`) were added to the capture list. On startup, active superchats are seeded from the REST endpoint (`GET /v1/super-chat`).

A subtle data quality issue emerged: some superchat payloads arrive with an empty `displayName` field and no `user` object. The fishtank API exposes a profile endpoint at `api.fishtank.live/v1/profile/{userId}` that can resolve the name. A cached profile fetcher (1h TTL, 500 entry cap with LRU eviction) handles lookups. An important gotcha: the profile URL must use `api.fishtank.live`, not `www.fishtank.live/api` (which returns 404). A startup backfill task patches existing events with empty names.

The frontend shows pinned banners above the chat panel with countdown timers (pre-computed expiry timestamps). Banners auto-expire when their duration elapses. The Activity panel gained a superchat type filter.

### Phase 23: Extracted Columns and Query Performance

As the event table grew past 600,000 rows, `json_extract()` in aggregate queries (GROUP BY, SUM, AVG) started causing OOM kills in the 1.5GB Docker container. The fundamental problem: SQLite can't index into JSON fields efficiently, so every aggregate query required a full table scan with per-row JSON parsing.

The fix was a two-phase migration:
1. Add real columns (`sentiment`, `cost`, `display_name`, `target`, `room`, `metadata`, `item_id`, `feature`) to the events table.
2. Extract values from JSON into these columns on INSERT via `_extract_columns()`. Backfill existing rows on first startup using Python-side parsing in 1,000-row batches (not SQL `UPDATE ... json_extract`, which also OOMs).

All aggregate queries were rewritten to use the extracted columns. `UNION ALL` replaced `event_type IN (...)` with `GROUP BY` so each branch uses `idx_events_type_ts_local` independently. Python merges the small result sets.

Critical lesson learned: `CREATE INDEX IF NOT EXISTS` on 600k+ rows is a one-time cost that stalls startup and can OOM if too many indexes are created at once. `DROP INDEX` + `CREATE INDEX` to replace a legacy index also OOMs. Legacy `json_extract` indexes were left in place rather than risk the DROP.

### Phase 24: Charts Tab

Added a dedicated Charts tab with three `recharts` visualizations:
- **STO-X price history** (LineChart, auto-downsampled to prevent rendering thousands of points)
- **Token spend trends** (stacked BarChart + LineChart showing TTS/SFX/Fishtoy/Poll/Superchat series with toggles). Poll data uses Python-side vote delta calculation to show active spend in real time.
- **Chat volume** (BarChart with top chatters overlay)

Three cached `/api/charts/*` endpoints serve pre-aggregated data with 9 range options (30m through all-time). `recharts` is code-split into a separate vendor chunk via `vite.config.js` `manualChunks` to keep the main bundle small. Charts auto-refresh every 5 minutes and skip when the tab is hidden.

### Phase 25: Virtual Scrolling and Keyset Pagination

All list panels (Activity, Chat, Fishtoys, Hidden Content, User Search) were converted from simple scrollable divs to `react-virtuoso` virtual scrolling. This reduced DOM node count from thousands to ~50 visible rows per panel, eliminating the render cost of scrolling through large datasets.

Server-side keyset pagination (`before_id` parameter) replaced offset-based pagination. Keyset pagination is O(1) regardless of page depth (compared to `OFFSET N` which scans and discards N rows). Activity loads 500 events initially, then paginates in 200-event batches on scroll.

The Activity panel also gained time-travel navigation: range buttons (1d/3d/7d/10d/30d) send an `around_ts` anchor to the server, which finds the nearest event and returns a page centered on that point. Bi-directional pagination allows scrolling both newer and older from the anchor. "Now" returns to live mode where WebSocket events prepend in real time.

### Phase 26: Sentiment Analysis

VADER-based sentiment scoring was added to TTS and chat messages at ingestion time. The `sentiment` field is stored as an extracted column (`positive`, `negative`, or `neutral`).

Analytics panels gained sentiment breakdowns: hourly bar charts showing positive/negative/neutral counts, overall mood badges with emoji indicators, and per-contestant sentiment in the TTS analytics. A `_sentiment_base()` function runs a single hourly query and computes all stats in Python from the hourly rows, avoiding a second table scan.

### Phase 27: Cloudflare Full Strict SSL

Upgraded from Cloudflare Flexible SSL (Cloudflare terminates HTTPS, connects to origin over HTTP) to Full Strict (end-to-end encryption). A Cloudflare Origin Certificate was generated and installed on the server. The backend now runs uvicorn with `SSL_CERTFILE` and `SSL_KEYFILE` environment variables, serving HTTPS directly. Docker-compose maps port 443:8000. WebSocket keepalive pings (every 60s) prevent Cloudflare's idle timeout from dropping the connection. Bot Fight Mode was re-enabled with UptimeRobot IPs allowlisted via IP Access Rules.

### Phase 28: Frontend Performance

Multiple rounds of React performance optimization to handle the high re-render frequency (WebSocket events arrive multiple times per second during active hours):

**Component memoization.** All list item components (`ChatMessage`, `ActivityCard`, `FishtoyCard`, `StatRow`) wrapped in `React.memo()`. Callbacks passed as props extracted to `useCallback` so memo reference checks pass. Chart bar components also memo'd with internal `useMemo` for sorted data and formatted labels.

**Data memoization.** All filtered/sorted/sliced arrays computed in `useMemo` with minimal dependency arrays. `.toLocaleString()` and `tokensToUSD()` calls wrapped in `useMemo` keyed on the numeric value. Never call formatting functions inline in JSX.

**Panel isolation.** Heavy sections extracted as `React.memo` components receiving only their data prop (`Last24hSidebar` receives `sessionStats`; `TimeDisplay` owns its own 1-second timer). This isolates re-renders to the section whose data actually changed.

**Error boundary.** Added an `ErrorBoundary` component that catches render errors and displays a recovery UI instead of crashing to a black screen.

**Service worker.** `sw.js` retries navigation requests during deploys (up to 3x with 2s delay). An update banner appears when WebSocket reconnects and detects a new `BUILD_VERSION` from `server:hello`.

### Key Takeaways

1. **Documentation lies (sometimes).** The fishclient library documents `fishtoy:queued` and `fishtoy:update` as valid event names. They exist in the code but don't fire in Season 5. Trusting the docs without verification would have led to a system that silently captured nothing.

2. **Systematic elimination over guessing.** When fishtoy events didn't appear, it was tempting to assume the event name was wrong and start guessing alternatives. Instead, building a raw frame logger that captured everything the server sent proved definitively that no fishtoy data comes over Socket.IO at all. This saved hours of trial-and-error on the wrong path.

3. **The answer was in a different protocol entirely.** The original screenshot looked like WebSocket data, which sent me down the Socket.IO path first. The actual source was a REST endpoint. Being willing to abandon the initial hypothesis and probe a completely different interface was the turning point.

4. **API probing is a legitimate discovery technique.** With no documentation, testing 50 URL patterns against the server based on naming conventions found the working endpoint in minutes. The 500-status responses were also informative: they confirmed the endpoints exist but need specific parameters.

5. **Source code is the ultimate documentation.** When passive observation couldn't reveal all event types, decompiling the client JavaScript gave us the complete registry in minutes. This applies broadly: when APIs are undocumented, the clients that consume them contain the answers.

6. **Data quality requires domain knowledge.** Filtering contestants to the current season, excluding wartoys, resolving room codes, and removing duplicate events all required understanding the show's mechanics. Technical skill gets you the data; domain knowledge makes it useful.

7. **Silent failures are the worst bugs.** The socket disconnect issue didn't crash, didn't log errors, and didn't affect the web UI. The only symptom was that new events stopped appearing. Without monitoring or alerting, this would go unnoticed for hours. Building in explicit state transitions and health logging is essential for unattended systems.

8. **Auth flows are discoverable.** When the documented Supabase endpoints didn't work, watching the browser's actual network requests during login revealed the real endpoint in seconds. The browser is always the authoritative client for web API discovery.

9. **Filter at ingestion, not display.** When the server sends polluted data (system echo messages in chat, duplicate TTS events, gift notifications mixed with director messages), filtering at the display layer leaves dirty data in the database that corrupts analytics. Filtering at the ingestion layer keeps the database clean and makes every downstream query accurate without needing per-query workarounds.

10. **The obvious dedup strategy isn't always right.** Content hashing seemed like the natural approach for TTS dedup, but it failed because the duplicates had different status fields and arrived minutes apart. Understanding the actual data model (same event ID, different lifecycle stages) led to a simpler and more reliable solution. When a dedup strategy fails, inspect the raw data side by side before building a more complex version of the same approach.

11. **Tests catch assumptions.** Writing unit tests for `get_latest_poll_state` immediately revealed that the function didn't include a `winner` key when no `poll:stop` existed. The test expected `state["winner"]` to be `None`, but the key was absent entirely. This is the kind of bug that works in the UI (optional chaining handles it) but breaks downstream consumers that expect a consistent schema.

12. **Shared queries with limits are a silent data loss vector.** When one event type has 50x the volume of another and they share a query with `LIMIT 500`, the low-volume type effectively doesn't exist in the results. This bug appeared three separate times (polls, activity, system events) before being recognized as a pattern. The rule is simple: never mix event types with different orders of magnitude in a single limited query.

13. **Third-party services make assumptions you didn't plan for.** UptimeRobot sends HEAD requests; FastAPI's `@app.get()` rejects them with 405. Cloudflare's Bot Fight Mode blocks the same monitoring service you're relying on for uptime alerts. Each integration layer adds constraints that only surface in production. Testing locally with `curl` wouldn't have caught either issue because curl defaults to GET and doesn't route through Cloudflare.

14. **`json_extract` doesn't scale.** It works fine for single-row lookups, but in aggregate queries over 600k+ rows it causes full-table scans with per-row JSON parsing — enough to OOM a 1.5GB container. The fix is to extract hot-path fields into real indexed columns at write time. The write-side cost is negligible; the read-side improvement is orders of magnitude.

15. **Index creation is a deployment event.** `CREATE INDEX IF NOT EXISTS` on a large table is a one-time cost that stalls startup for minutes and can OOM if batched. Treat index additions like schema migrations: plan them, warn about the startup delay, and never batch multiple large indexes in one deploy.

16. **Virtual scrolling is table stakes for real-time UIs.** Rendering 500+ DOM nodes per panel, with multiple panels updating from WebSocket events multiple times per second, creates visible jank. Virtual scrolling (react-virtuoso) reduces visible DOM to ~50 nodes regardless of data size. The performance difference is immediate and dramatic.

17. **Memoization is defense in depth.** In a React app where the root component re-renders on every WebSocket event, every un-memoized computation runs on every message. `useMemo` for data, `useCallback` for handlers, `React.memo` for components, and extracted sub-components for independent data sources — each layer prevents unnecessary work from propagating to the next.

### Relevance to Sales Engineering

This project exercises several skills that directly transfer to SE work:

**API integration and debugging.** Most SE roles involve integrating customer systems with your product's API. Understanding authentication flows (Supabase cookie-based auth), debugging connection failures, and working with undocumented or partially documented APIs is daily work.

**Technical architecture communication.** Being able to explain why a system uses dual data sources, why polling is preferred over push for certain data types, and the tradeoffs involved (latency vs. reliability, normalization vs. flexibility) is core to SE conversations with technical buyers.

**Proof of concept development.** Building a working prototype that demonstrates a technical capability (in this case, hidden data capture + real-time dashboard) is exactly what SEs do when running POCs for prospects. The dashboard went from concept to working prototype in a single session, then iterated through multiple rounds of testing and refinement.

**Problem decomposition under uncertainty.** The fishtoy mystery required breaking an ambiguous problem ("why isn't this working?") into testable hypotheses, building diagnostic tools, and following the evidence to an unexpected conclusion. This mirrors the troubleshooting and solution design work SEs do with customer integrations.

**Open source contribution.** Identifying bugs in a third-party library, writing clean fixes with detailed descriptions, and submitting a PR demonstrates the collaborative technical communication that SEs practice daily when working with engineering teams, partners, and customers.

**Full-stack prototyping.** The dashboard spans Python backend, React frontend, SQLite storage, WebSocket communication, REST API design, and deployment automation. SEs regularly need to build demos and POCs that touch multiple layers of a stack to prove out an integration or show product value.

**Authentication and security.** Reverse engineering the auth flow, building automatic token refresh, handling credential storage securely (`.env` files, file permissions, masked logging), and implementing re-authentication on failure are all skills SEs need when helping customers integrate with auth-protected APIs. Understanding OAuth-style token lifecycles is increasingly relevant as more products move to token-based auth.

**Building for unattended operation.** Moving from a "works when I'm watching" prototype to a system that runs 24/7 without intervention required solving a different class of problems: silent failure detection, automatic recovery, credential management, and connection resilience. This mirrors the transition from POC to production that SEs help customers navigate.

**Containerization and deployment.** Packaging the application with Docker (multi-stage build, persistent volumes, environment-based configuration) and deploying to a VPS with firewall rules, SSH hardening, and uptime monitoring demonstrates the deployment skills SEs need when helping customers run integrations in their own infrastructure. A health endpoint that reports component-level status is the kind of operational tooling that separates a demo from a production system.

**Security hardening for production.** Implementing CORS restrictions, rate limiting, endpoint sanitization, connection caps, and exception handling before going public shows awareness of the security considerations involved in exposing any service to the internet. SEs who understand these concerns can have informed conversations with security teams during technical evaluations.

**Testing and quality assurance.** Writing unit tests that exercise the data layer, filter logic, and rate limiting against an in-memory database shows the discipline to verify behavior, not just observe it. SEs who can write and explain tests earn credibility with engineering teams during technical evaluations.
