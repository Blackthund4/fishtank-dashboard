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

**Standalone fishtoy poller:** Single-file Python script. Polls `/v1/items/recent` every 2 seconds, deduplicates using a rolling ID window, resolves item names from the catalog, writes to JSONL log file and terminal. ~130 lines.

**Combined event logger:** Runs both the REST poller and Socket.IO connection in parallel threads. Thread-safe JSONL logging with `threading.Lock()`. Captures fishtoys, chat, TTS, SFX, and system events in a single log. ~370 lines.

**Real-time dashboard:** FastAPI backend + React/Tailwind frontend. SQLite persistence with JSON storage and `json_extract` queries for filtering. Browser WebSocket bridge for live updates. Fishtoy panel with target filtering (clickable contestant pills built from actual target values in the data), item type dropdown, metadata search, and prominently displayed hidden content. Single-process deployment (backend serves built frontend as static files).

### Technical Decisions Worth Discussing

**Monkey-patching vs. forking:** Chose to patch fishclient at runtime rather than maintain a fork. This keeps the fix in one file and doesn't require managing a custom package. The tradeoff is fragility if the library updates, but the library hasn't been updated in months and the patches are well-documented.

**REST polling vs. WebSocket for fishtoys:** The polling approach has inherent latency (up to 2 seconds) and a theoretical event loss window (if 10+ fishtoys are redeemed within 2 seconds). In practice, fishtoys cost tokens, so redemption rate is low enough that 2-second polling with a 10-item buffer has zero observed loss.

**SQLite with JSON storage:** Chose to store complete event payloads as JSON rather than normalizing into columns. This means queries use `json_extract()` which is slower than indexed column scans, but it provides forward compatibility: if the API adds new fields, they're automatically captured without schema changes. For the expected data volume (~38 MB/day with active chat), SQLite handles this fine.

**seen_ids pruning:** The deduplication set only keeps IDs from the last 3 polls (max 30 IDs). Without this, a 30-day season would accumulate ~690 MB of set memory. The rolling window is safe because the API returns items in reverse chronological order, so an item that's fallen off the 10-item response will never reappear.

### Key Takeaways

1. **Documentation lies (sometimes).** The fishclient library documents `fishtoy:queued` and `fishtoy:update` as valid event names. They exist in the code but don't fire in Season 5. Trusting the docs without verification would have led to a system that silently captured nothing.

2. **Systematic elimination over guessing.** When fishtoy events didn't appear, it was tempting to assume the event name was wrong and start guessing alternatives. Instead, building a raw frame logger that captured everything the server sent proved definitively that no fishtoy data comes over Socket.IO at all. This saved hours of trial-and-error on the wrong path.

3. **The answer was in a different protocol entirely.** The original screenshot looked like WebSocket data, which sent me down the Socket.IO path first. The actual source was a REST endpoint. Being willing to abandon the initial hypothesis and probe a completely different interface was the turning point.

4. **API probing is a legitimate discovery technique.** With no documentation, testing 50 URL patterns against the server based on naming conventions (`/v1/items/recent`, `/v1/items/used`, `/v1/tts/history`) found the working endpoint in minutes. The 500-status responses were also informative: they confirmed the endpoints exist but need specific parameters, which narrowed the search.

### Relevance to Sales Engineering

This project exercises several skills that directly transfer to SE work:

**API integration and debugging.** Most SE roles involve integrating customer systems with your product's API. Understanding authentication flows (Supabase cookie-based auth), debugging connection failures, and working with undocumented or partially documented APIs is daily work.

**Technical architecture communication.** Being able to explain why a system uses dual data sources, why polling is preferred over push for certain data types, and the tradeoffs involved (latency vs. reliability, normalization vs. flexibility) is core to SE conversations with technical buyers.

**Proof of concept development.** Building a working prototype that demonstrates a technical capability (in this case, hidden data capture + real-time dashboard) is exactly what SEs do when running POCs for prospects. The dashboard went from concept to working prototype in a single session.

**Problem decomposition under uncertainty.** The fishtoy mystery required breaking an ambiguous problem ("why isn't this working?") into testable hypotheses, building diagnostic tools, and following the evidence to an unexpected conclusion. This mirrors the troubleshooting and solution design work SEs do with customer integrations.
