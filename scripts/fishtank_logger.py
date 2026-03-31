"""
Fishtank.live Event Logger v5.2

Captures events from fishtank.live using two methods:
  - REST polling: /v1/items/recent for fishtoy redemptions (including
    hidden metadata like love letter contents)
  - Socket.IO: real-time push for chat messages, TTS, and SFX

Resolves item IDs to names via /v1/items catalog on startup.

SETUP:
    1.  pip install fishclient requests
    2.  Get your cookie (see below)
    3.  python fishtank_logger.py

GETTING YOUR COOKIE:
    IMPORTANT: Copy from the NETWORK tab, NOT the Application/Cookies tab.

    1. Open DevTools (F12) > Network tab
    2. Filter by "api.fishtank.live"
    3. Click any request > Request Headers > Cookie header
    4. Copy the value AFTER "sb-wcsaaupukpdmqdjcgaoo-auth-token="
       (~33 chars, NOT a long JWT)
    5. Set the env var:
           export FISHTANK_COOKIE='paste_value_here'       (Linux/Mac)
           $env:FISHTANK_COOKIE = 'paste_value_here'       (PowerShell)
"""

import json
import logging
import os
import sys
import time
import types
import threading
from datetime import datetime, timezone

import requests
from fishclient import FishClient

# ============================================================
# CONFIGURATION
# ============================================================

COOKIE = os.environ.get("FISHTANK_COOKIE", "YOUR_COOKIE_HERE")

LOG_FILE = "fishtank_log.jsonl"  # set to None to disable

SOCKET_EVENTS = [
    "tts:update",
    "tts:price",
    "sfx:update",
    "sfx:price",
    "chat:message",
    "happening",
    "poll:start",
    "poll:stop",
    "poll:vote",
    "notification:global",
    "announcement",
    "stock:update",
    "stock:new",
    "stock:remove",
    "stock:split",
    "feature-toggles:update",
]

FISHTOY_POLL_INTERVAL = 2

VERBOSE = False


# ============================================================
# FISHCLIENT PATCHES
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
            _logger.error(f"Error receiving: {e}")
            _logger.info("Reconnecting...")
            try:
                self.connect()
            except Exception as err:
                _logger.error(f"Reconnect failed: {err}")
            break


# ============================================================
# HELPERS
# ============================================================

_log_lock = threading.Lock()


def ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def epoch_to_str(val):
    try:
        n = int(val)
        if n > 1_000_000_000_000:
            n = n / 1000
        return datetime.fromtimestamp(n, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    except Exception:
        return str(val)


def write_log(event_name, data):
    if not LOG_FILE:
        return
    entry = {"timestamp": ts(), "event": event_name, "data": data}
    try:
        with _log_lock:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        print(f"  [log write error: {e}]")


def clean_cookie(raw):
    return raw.strip().strip("'\"").strip().replace("\n", "").replace("\r", "")


# ============================================================
# ITEM CATALOG
# ============================================================

item_catalog = {}
CAPTURE_TYPES = {"FISHTOY", "BIGTOY"}


def load_item_catalog(session):
    """Fetch /v1/items and build itemId -> full entry lookup."""
    global item_catalog
    try:
        r = session.get("https://api.fishtank.live/v1/items", timeout=10)
        if r.status_code == 200:
            raw = r.json()
            for key, val in raw.items():
                if isinstance(val, dict) and "id" in val:
                    item_catalog[str(val["id"])] = val
            ft = sum(1 for v in item_catalog.values() if v.get("type") in CAPTURE_TYPES)
            print(f"[{ts()}] Loaded {len(item_catalog)} items ({ft} fishtoys/bigtoys)")
        else:
            print(f"[{ts()}] Could not load item catalog (HTTP {r.status_code})")
    except Exception as e:
        print(f"[{ts()}] Could not load item catalog: {e}")


def get_item_name(item_id):
    entry = item_catalog.get(str(item_id))
    return entry.get("name", f"Item #{item_id}") if entry else f"Item #{item_id}"


def should_capture(item_id):
    """Return True if item is a FISHTOY or BIGTOY (or if catalog unavailable)."""
    entry = item_catalog.get(str(item_id))
    if not entry:
        return True
    return entry.get("type") in CAPTURE_TYPES


# ============================================================
# FISHTOY REST POLLER
# ============================================================


def fishtoy_poller(session, stop_event):
    """Poll /v1/items/recent for fishtoy redemptions."""
    seen_ids = set()
    prev_poll_ids = []
    first_poll = True

    while not stop_event.is_set():
        try:
            r = session.get("https://api.fishtank.live/v1/items/recent", timeout=10)

            if r.status_code in (401, 403):
                print(f"\n[{ts()}] Fishtoy poller: auth expired (HTTP {r.status_code}). Restart with fresh cookie.")
                stop_event.wait(FISHTOY_POLL_INTERVAL)
                continue
            if r.status_code != 200:
                print(f"\n[{ts()}] Fishtoy poller: HTTP {r.status_code}")
                stop_event.wait(FISHTOY_POLL_INTERVAL)
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

                if not should_capture(item.get("itemId")):
                    continue

                name = get_item_name(item.get("itemId"))
                itype = item_catalog.get(str(item.get("itemId")), {}).get("type", "?")
                print(f"\n[{ts()}] === {name} ({itype}) ===")
                print(f"  User:        {item.get('displayName', '?')}")
                print(f"  Cost:        {item.get('cost', '?')}")
                print(f"  Target:      {item.get('target', '?')}")
                print(f"  Status:      {item.get('status', '?')}")
                if item.get("secondaryTarget"):
                    print(f"  Secondary:   {item['secondaryTarget']}")
                if item.get("metadata"):
                    print(f"  METADATA:    {item['metadata']}")
                if item.get("clanTag"):
                    print(f"  ClanTag:     {item['clanTag']}")
                if item.get("createdAt"):
                    print(f"  Created:     {epoch_to_str(item['createdAt'])}")

                if VERBOSE:
                    print(f"  RAW: {json.dumps(item, ensure_ascii=False, default=str, indent=2)}")

                write_log("fishtoy:used", item)
                sys.stdout.flush()

            prev_poll_ids.append(this_poll_ids)
            if len(prev_poll_ids) > 3:
                prev_poll_ids.pop(0)
            seen_ids = set().union(*prev_poll_ids)

            if first_poll:
                first_poll = False
                print(f"[{ts()}] Fishtoy poller: {len(items)} items in initial snapshot. Watching for new ones.")

        except requests.RequestException as e:
            print(f"[{ts()}] Fishtoy poll error: {e}")

        stop_event.wait(FISHTOY_POLL_INTERVAL)


# ============================================================
# SOCKET.IO EVENT FORMATTER
# ============================================================


def fmt_generic(data):
    if isinstance(data, dict):
        for k, v in data.items():
            s = str(v)
            if len(s) > 200:
                s = s[:200] + "..."
            print(f"  {k}: {s}")
    elif isinstance(data, list):
        for i, item in enumerate(data[:10]):
            print(f"  [{i}] {item}")
        if len(data) > 10:
            print(f"  ... and {len(data) - 10} more")
    else:
        print(f"  {data}")


# ============================================================
# MAIN
# ============================================================


def main():
    cookie = COOKIE
    if cookie == "YOUR_COOKIE_HERE":
        print("ERROR: No auth cookie set.\n")
        print("Copy from DevTools > Network tab > Request Headers > Cookie")
        print("The value after sb-wcsaaupukpdmqdjcgaoo-auth-token= (~33 chars)\n")
        print("  export FISHTANK_COOKIE='value'       (Linux/Mac)")
        print("  $env:FISHTANK_COOKIE = 'value'       (PowerShell)")
        sys.exit(1)

    cookie = clean_cookie(cookie)

    # ---- REST session ----
    rest_session = requests.Session()
    rest_session.cookies.set(
        "sb-wcsaaupukpdmqdjcgaoo-auth-token", cookie, domain="api.fishtank.live"
    )

    try:
        auth = rest_session.get("https://api.fishtank.live/v1/auth", timeout=10)
        if not auth.json().get("session"):
            print(f"[{ts()}] Auth failed: no session. Check cookie.")
            sys.exit(1)
    except Exception as e:
        print(f"[{ts()}] Auth failed: {e}")
        sys.exit(1)

    # ---- Load item catalog ----
    load_item_catalog(rest_session)

    # ---- Socket.IO ----
    client = FishClient(cookie=cookie)
    client.handle_message = types.MethodType(_patched_handle_message, client)
    client.listen = types.MethodType(_patched_listen, client)

    for event_name in SOCKET_EVENTS:
        def make_handler(evt):
            def handler(data):
                print(f"\n[{ts()}] === {evt} ===")
                fmt_generic(data)
                if VERBOSE:
                    print(f"  RAW: {json.dumps(data, ensure_ascii=False, default=str, indent=2)}")
                write_log(evt, data)
                sys.stdout.flush()
            return handler
        client.dispatcher.on(event_name)(make_handler(event_name))

    @client.dispatcher.on("disconnect")
    def on_disconnect(data):
        print(f"\n[{ts()}] SERVER DISCONNECT: {data}")
        sys.stdout.flush()

    @client.dispatcher.on("connect_error")
    def on_connect_error(data):
        print(f"\n[{ts()}] CONNECTION ERROR: {data}")
        sys.stdout.flush()

    # ---- Connect ----
    print(f"[{ts()}] Connecting to fishtank.live...")
    print(f"Cookie:        ...{cookie[-20:]}")
    print(f"Socket events: {', '.join(SOCKET_EVENTS)}")
    print(f"Fishtoy poll:  /v1/items/recent every {FISHTOY_POLL_INTERVAL}s")
    ft = sum(1 for v in item_catalog.values() if v.get("type") in CAPTURE_TYPES)
    print(f"Item catalog:  {len(item_catalog)} items ({ft} fishtoys/bigtoys captured)")
    print(f"Log file:      {LOG_FILE or 'disabled'}")
    print(f"Verbose:       {VERBOSE}")
    print("-" * 60)

    try:
        client.connect()
        print(f"[{ts()}] Socket.IO connected.")
    except Exception as e:
        print(f"[{ts()}] Socket.IO connection failed: {e}")
        print("Chat/TTS/SFX won't work, but fishtoy polling will still run.")

    # ---- Start fishtoy poller ----
    stop_event = threading.Event()
    poller_thread = threading.Thread(
        target=fishtoy_poller, args=(rest_session, stop_event), daemon=True
    )
    poller_thread.start()

    print(f"[{ts()}] Fishtoy poller started.")
    print(f"[{ts()}] Listening... (Ctrl+C to stop)\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"\n[{ts()}] Shutting down...")
        stop_event.set()
        client.is_connected = False
        if client.websocket is not None:
            try:
                client.websocket.close()
            except Exception:
                pass
        os._exit(0)


if __name__ == "__main__":
    main()
