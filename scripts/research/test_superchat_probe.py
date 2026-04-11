"""
Superchat Discovery Script
==========================
Two-pronged probe to find the socket/REST event name for the new superchat
(pinned chat message) feature on fishtank.live.

Part 1 – REST API probe
    Tries ~30 URL patterns that might serve superchat/pinned-message data.

Part 2 – Socket.IO catchall
    Connects and logs ALL events whose name contains "super", "pin", "highlight",
    OR that look like a new chat variant.  Also logs the full payload of any
    unknown event type not already handled by the dashboard.

USAGE:
    # cookie or email/password auth
    export FISHTANK_COOKIE='your_short_token'
    # OR
    export FISHTANK_EMAIL='you@example.com'
    export FISHTANK_PASSWORD='yourpassword'

    python test_superchat_probe.py

Then trigger a superchat on the site (buy one yourself, or wait for one to
appear) and watch the output.  Copy the full payload block for the new event
and paste it into Tom for backend integration.
"""

import json, os, sys, time, types, threading
import msgpack, requests

# ── Auth ────────────────────────────────────────────────────────────────────

COOKIE   = os.environ.get("FISHTANK_COOKIE", "")
EMAIL    = os.environ.get("FISHTANK_EMAIL", "")
PASSWORD = os.environ.get("FISHTANK_PASSWORD", "")

# Known events already captured by the dashboard – skip these in the "unknown" filter
KNOWN_EVENTS = {
    "chat:message", "tts:update", "tts:price", "sfx:update", "sfx:price",
    "poll:start", "poll:stop", "poll:vote",
    "notification:global", "announcement",
    "stock:update", "stock:new", "stock:remove", "stock:split",
    "happening", "feature-toggles:update", "chat:presence",
    "initial-data", "connect", "disconnect", "connect_error",
    # high-volume – filter to reduce noise
    "tts:queued", "sfx:queued",
}

# Keywords that flag an event as potentially superchat-related
KEYWORDS = ["super", "pin", "highlight", "boost", "feature", "spotlight", "sticky"]


def ts():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


def get_cookie():
    """Return a clean cookie string, auto-logging in if needed."""
    if COOKIE:
        return COOKIE.strip().strip("'\"").replace("\n", "")

    if EMAIL and PASSWORD:
        masked = EMAIL[:3] + "***" + EMAIL[EMAIL.index("@"):] if "@" in EMAIL else "***"
        print(f"[{ts()}] Logging in as {masked}...")
        resp = requests.post(
            "https://api.fishtank.live/v1/auth/log-in",
            json={"email": EMAIL, "password": PASSWORD},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            session = data.get("session", {})
            access  = session.get("access_token")
            refresh = session.get("refresh_token")
            if not access or not refresh:
                print(f"[!] Login response missing tokens: {data}")
                sys.exit(1)
            from urllib.parse import quote
            raw = json.dumps([access, refresh])
            return quote(raw, safe="")
        print(f"[!] Login failed: {resp.status_code} {resp.text[:300]}")
        sys.exit(1)

    print("ERROR: Set FISHTANK_COOKIE or FISHTANK_EMAIL + FISHTANK_PASSWORD")
    sys.exit(1)


# ── Part 1: REST probe ───────────────────────────────────────────────────────

REST_CANDIDATES = [
    # superchat / pinned chat patterns
    "/v1/chat/super",
    "/v1/chat/superchats",
    "/v1/chat/pinned",
    "/v1/chat/pinned-messages",
    "/v1/chat/highlight",
    "/v1/chat/highlights",
    "/v1/chat/featured",
    "/v1/chat/boost",
    "/v1/chat/boosted",
    "/v1/chat/sticky",
    "/v1/superchats",
    "/v1/superchat",
    "/v1/pinned",
    "/v1/pinned-messages",
    "/v1/featured-messages",
    "/v1/highlights",
    # general recent/activity patterns (may include superchat items)
    "/v1/activity",
    "/v1/activity/recent",
    "/v1/chat/recent",
    "/v1/chat",
    "/v1/messages",
    "/v1/messages/pinned",
    "/v1/messages/recent",
    # items-style patterns (fishtoys were here)
    "/v1/items/super",
    "/v1/items/chat",
    "/v1/items/pinned",
    # misc
    "/v1/chat/queue",
    "/v1/chat/top",
]


def run_rest_probe(session):
    print("\n" + "=" * 60)
    print("PART 1: REST API PROBE")
    print("=" * 60)
    found = []
    for path in REST_CANDIDATES:
        url = f"https://api.fishtank.live{path}"
        try:
            r = session.get(url, timeout=5)
            snippet = r.text[:300].replace("\n", " ")
            status = r.status_code
            tag = {200: "[200 OK ✓]", 404: "[404     ]", 401: "[401 AUTH]",
                   403: "[403 FORB]"}.get(status, f"[{status}    ]")
            print(f"  {tag}  {path}" + (f"\n           → {snippet}" if status == 200 else ""))
            if status == 200:
                found.append((path, r.text))
        except Exception as e:
            print(f"  [ERROR   ]  {path}: {e}")

    print()
    if found:
        print(f">>> {len(found)} REST endpoint(s) returned 200:")
        for path, body in found:
            print(f"\n  {path}:")
            print(f"  {body[:600]}")
    else:
        print(">>> No superchat REST endpoints found with these patterns.")
        print("    Superchat is likely socket-only or under a different prefix.")
    print("=" * 60 + "\n")


# ── Part 2: Socket.IO catchall ───────────────────────────────────────────────

def run_socket_probe(cookie):
    try:
        from fishclient import FishClient
    except ImportError:
        print("[!] fishclient not installed. Run: pip install fishclient")
        print("    Socket probe skipped.")
        return

    print("=" * 60)
    print("PART 2: SOCKET.IO CATCHALL")
    print("Watching for:")
    print(f"  • Any event containing: {', '.join(KEYWORDS)}")
    print("  • Any UNKNOWN event not already in the dashboard")
    print("  • Full payload printed for anything that matches")
    print("Trigger a superchat on the site now...")
    print("=" * 60 + "\n")

    def _handle(self, msg):
        if isinstance(msg, str):
            if msg.startswith("2"):
                self.websocket.send("3")
            elif not msg.startswith("3"):  # skip pong echoes
                print(f"[TEXT] {msg[:200]}")
                sys.stdout.flush()
        elif isinstance(msg, bytes):
            try:
                u = msgpack.unpackb(msg, raw=False)
                d = u.get("data")
                if u.get("type") == 2 and isinstance(d, list) and len(d) > 0:
                    name = d[0]
                    payload = d[1] if len(d) > 1 else {}

                    # Check if it's superchat-related or unknown
                    name_lower = name.lower()
                    is_keyword_match = any(k in name_lower for k in KEYWORDS)
                    is_unknown = name not in KNOWN_EVENTS

                    if is_keyword_match or is_unknown:
                        tag = ">>> KEYWORD MATCH" if is_keyword_match else ">>> UNKNOWN EVENT"
                        print(f"\n{'=' * 50}")
                        print(f"{tag}: {name}")
                        print(f"{'=' * 50}")
                        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
                        sys.stdout.flush()

                    # Also check if any known event suddenly has superchat fields
                    elif name in ("chat:message", "notification:global", "announcement"):
                        payload_str = json.dumps(payload, default=str).lower()
                        if any(k in payload_str for k in KEYWORDS):
                            print(f"\n>>> EXISTING EVENT with keyword in payload: {name}")
                            print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
                            sys.stdout.flush()

            except Exception:
                pass

    def _listen(self):
        if not self.websocket:
            return
        while self.is_connected:
            try:
                self.handle_message(self.websocket.recv())
            except Exception:
                if not self.is_connected:
                    break
                break

    while True:
        try:
            client = FishClient(cookie=cookie)
            client.handle_message = types.MethodType(_handle, client)
            client.listen = types.MethodType(_listen, client)
            client.connect()
            print(f"[{ts()}] Connected. Listening...\n")
            while client.is_connected:
                time.sleep(1)
            print(f"\n[{ts()}] Disconnected. Reconnecting in 5s...")
            time.sleep(5)
        except KeyboardInterrupt:
            print("\nDone.")
            os._exit(0)
        except Exception as e:
            print(f"[{ts()}] Error: {e}. Retrying in 5s...")
            time.sleep(5)


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cookie = get_cookie()

    # REST session
    session = requests.Session()
    session.cookies.set("sb-wcsaaupukpdmqdjcgaoo-auth-token", cookie, domain="api.fishtank.live")

    # Verify auth
    auth = session.get("https://api.fishtank.live/v1/auth", timeout=10)
    if auth.status_code != 200 or not auth.json().get("session"):
        print(f"[!] Auth failed ({auth.status_code}). Check your cookie/credentials.")
        sys.exit(1)
    print(f"[{ts()}] Authenticated OK.\n")

    # Part 1 runs first (quick)
    run_rest_probe(session)

    # Part 2 runs indefinitely – Ctrl+C to stop
    run_socket_probe(cookie)
