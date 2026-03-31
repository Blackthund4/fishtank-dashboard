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

**Standalone fishtoy poller:** Single-file Python script. Polls `/v1/items/recent` every 2 seconds, deduplicates using a rolling ID window, resolves item names from the catalog, filters by item type (FISHTOY/BIGTOY only), writes to session-timestamped JSONL log files and terminal. ~145 lines.

**Combined event logger:** Runs both the REST poller and Socket.IO connection in parallel threads. Thread-safe JSONL logging with `threading.Lock()`. Captures fishtoys, chat, TTS, SFX, polls, director messages, stock events, and system events in a single log. ~380 lines.

**Real-time dashboard:** FastAPI backend + React/Tailwind frontend with three tabs:
- **Dashboard tab:** Live fishtoy feed with collapsible cards, chat panel, TTS/SFX activity with room name resolution, stock market ticker, target filtering with drill-down (click target to see stats, items used, top senders), metadata search. Director message banner and live poll bar with animated vote percentages appear at the top when active.
- **Analytics tab:** Stock market cards with IPO/avg/bid-ask data, contestant grid with photos and stock prices, TTS/SFX analytics (most active rooms, top spenders, hourly bar charts), chat analytics (top chatters, hourly volume), poll history with results, director message timeline, TTS/SFX price change log, fishtoy availability status board.
- **Hidden Content tab:** Dedicated searchable archive of fishtoy metadata (love letters, custom messages) with target filtering sidebar.

SQLite persistence with JSON storage and `json_extract` queries. Stock price history polled every 60 seconds. Browser WebSocket bridge for live updates. Single-process deployment (backend serves built frontend as static files). 17 REST API endpoints. 18 captured socket event types.

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

From this registry, I selected 18 high-value events to capture: the ones that represent show-critical moments (polls, director messages, stock changes, price adjustments, feature toggles) without adding noise from per-user events like trading or DMs.

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

**Monkey-patching vs. forking:** Chose to patch fishclient at runtime rather than maintain a fork. This keeps the fix in one file and doesn't require managing a custom package. The tradeoff is fragility if the library updates, but the library hasn't been updated in months and the patches are well-documented. I submitted a PR with all fixes to give back to the project.

**REST polling vs. WebSocket for fishtoys:** The polling approach has inherent latency (up to 2 seconds) and a theoretical event loss window (if 10+ fishtoys are redeemed within 2 seconds). In practice, fishtoys cost tokens, so redemption rate is low enough that 2-second polling with a 10-item buffer has zero observed loss.

**SQLite with JSON storage:** Chose to store complete event payloads as JSON rather than normalizing into columns. This means queries use `json_extract()` which is slower than indexed column scans, but it provides forward compatibility: if the API adds new fields, they're automatically captured without schema changes. For the expected data volume (~38 MB/day with active chat), SQLite handles this fine.

**seen_ids pruning:** The deduplication set only keeps IDs from the last 3 polls (max 30 IDs). Without this, a 30-day season would accumulate ~690 MB of set memory. The rolling window is safe because the API returns items in reverse chronological order, so an item that's fallen off the 10-item response will never reappear.

**JS decompilation over passive observation:** Waiting for every socket event to fire naturally could take weeks (some events like `stock:split` or `stock:remove` only happen during eliminations). Decompiling the production JavaScript to extract the event registry took 5 minutes and gave us the complete picture immediately. This is a technique that transfers directly to SE work: when documentation is incomplete, the source code is the ground truth.

**Selective event capture:** The full registry has 60+ events, but most are per-user (trading, DMs, inventory changes) and would add noise without value for a broadcast dashboard. Selecting the 18 show-critical events required understanding the domain well enough to distinguish signal from noise.

### Key Takeaways

1. **Documentation lies (sometimes).** The fishclient library documents `fishtoy:queued` and `fishtoy:update` as valid event names. They exist in the code but don't fire in Season 5. Trusting the docs without verification would have led to a system that silently captured nothing.

2. **Systematic elimination over guessing.** When fishtoy events didn't appear, it was tempting to assume the event name was wrong and start guessing alternatives. Instead, building a raw frame logger that captured everything the server sent proved definitively that no fishtoy data comes over Socket.IO at all. This saved hours of trial-and-error on the wrong path.

3. **The answer was in a different protocol entirely.** The original screenshot looked like WebSocket data, which sent me down the Socket.IO path first. The actual source was a REST endpoint. Being willing to abandon the initial hypothesis and probe a completely different interface was the turning point.

4. **API probing is a legitimate discovery technique.** With no documentation, testing 50 URL patterns against the server based on naming conventions found the working endpoint in minutes. The 500-status responses were also informative: they confirmed the endpoints exist but need specific parameters.

5. **Source code is the ultimate documentation.** When passive observation couldn't reveal all event types, decompiling the client JavaScript gave us the complete registry in minutes. This applies broadly: when APIs are undocumented, the clients that consume them contain the answers.

6. **Data quality requires domain knowledge.** Filtering contestants to the current season, excluding wartoys, resolving room codes, and removing duplicate events all required understanding the show's mechanics. Technical skill gets you the data; domain knowledge makes it useful.

### Relevance to Sales Engineering

This project exercises several skills that directly transfer to SE work:

**API integration and debugging.** Most SE roles involve integrating customer systems with your product's API. Understanding authentication flows (Supabase cookie-based auth), debugging connection failures, and working with undocumented or partially documented APIs is daily work.

**Technical architecture communication.** Being able to explain why a system uses dual data sources, why polling is preferred over push for certain data types, and the tradeoffs involved (latency vs. reliability, normalization vs. flexibility) is core to SE conversations with technical buyers.

**Proof of concept development.** Building a working prototype that demonstrates a technical capability (in this case, hidden data capture + real-time dashboard) is exactly what SEs do when running POCs for prospects. The dashboard went from concept to working prototype in a single session, then iterated through multiple rounds of testing and refinement.

**Problem decomposition under uncertainty.** The fishtoy mystery required breaking an ambiguous problem ("why isn't this working?") into testable hypotheses, building diagnostic tools, and following the evidence to an unexpected conclusion. This mirrors the troubleshooting and solution design work SEs do with customer integrations.

**Open source contribution.** Identifying bugs in a third-party library, writing clean fixes with detailed descriptions, and submitting a PR demonstrates the collaborative technical communication that SEs practice daily when working with engineering teams, partners, and customers.

**Full-stack prototyping.** The dashboard spans Python backend, React frontend, SQLite storage, WebSocket communication, REST API design, and deployment automation. SEs regularly need to build demos and POCs that touch multiple layers of a stack to prove out an integration or show product value.
