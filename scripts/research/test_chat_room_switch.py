"""
Chat Room Switch Probe - figure out how fishtank.live room switching works.

1. Connects and logs the initial chat:room event (if any)
2. Logs all chat:message events to show which room we're in
3. After 10s, attempts to emit chat:room with "global" to switch rooms
4. Continues logging to see if messages change

Usage:
    cd backend
    python -m scripts.research.test_chat_room_switch
  or:
    set FISHTANK_COOKIE=... && python scripts/research/test_chat_room_switch.py

Watch the output for:
  - What the initial chat:room payload looks like
  - Whether chat:message payloads contain a room/channel field
  - Whether emitting chat:room changes the messages we receive
"""
import os, sys, time, types, json
import msgpack
from datetime import datetime

# Allow running from repo root or backend/
backend_dir = os.path.join(os.path.dirname(__file__), "..", "..", "backend")
sys.path.insert(0, backend_dir)
sys.path.insert(0, os.path.join(backend_dir, "vendor", "fishclient"))

from auth import load_dotenv, AuthManager
from fishclient import FishClient

load_dotenv(os.path.join(backend_dir, ".env"))

auth = AuthManager()
cookie = auth.get_cookie()
if not cookie:
    print("No credentials found. Set FISHTANK_EMAIL/FISHTANK_PASSWORD in backend/.env")
    sys.exit(1)

# Track state
room_switch_sent = False
message_count = 0


def ts():
    return datetime.now().strftime("%H:%M:%S")


def _probe_handle_message(self, message):
    global message_count

    if isinstance(message, str):
        if message.startswith("2"):
            self.websocket.send("3")  # pong
        else:
            print(f"[{ts()}] [TEXT] {message[:200]}")
            sys.stdout.flush()
        return

    if not isinstance(message, bytes):
        return

    try:
        unpacked = msgpack.unpackb(message, raw=False)
    except Exception as e:
        print(f"[{ts()}] [BINARY] decode failed: {e}")
        return

    pkt_type = unpacked.get("type")
    pkt_data = unpacked.get("data")

    if pkt_type == 0:
        print(f"[{ts()}] [CONNECT] {pkt_data}")
        sys.stdout.flush()
        return

    if pkt_type != 2 or not isinstance(pkt_data, list) or len(pkt_data) < 2:
        return

    event_name = pkt_data[0]
    payload = pkt_data[1]

    # Log EVERYTHING related to chat and rooms
    if "room" in event_name.lower() or "chat" in event_name.lower() or "channel" in event_name.lower():
        if event_name == "chat:message":
            message_count += 1
            # Show first 5 messages in full, then just count + summary
            if message_count <= 5:
                print(f"[{ts()}] [FULL MESSAGE #{message_count}] {json.dumps(payload, indent=2, default=str)[:1000]}")
            elif message_count % 10 == 0:
                user = payload.get("user", {}) if isinstance(payload, dict) else {}
                name = user.get("displayName", "?") if isinstance(user, dict) else "?"
                msg = str(payload.get("message", ""))[:40] if isinstance(payload, dict) else "?"
                # Check for any room-related fields
                room_fields = {k: v for k, v in (payload.items() if isinstance(payload, dict) else [])
                               if "room" in k.lower() or "channel" in k.lower()}
                room_info = f" room_fields={room_fields}" if room_fields else ""
                print(f"[{ts()}] [chat:message #{message_count}] {name}: {msg}{room_info}")
        else:
            # Non-message chat/room events - log in full
            print(f"[{ts()}] [EVENT] {event_name}: {json.dumps(payload, indent=2, default=str)[:1000]}")

    sys.stdout.flush()


def _patched_listen(self):
    if self.websocket is None:
        return
    while self.is_connected:
        try:
            message = self.websocket.recv()
            self.handle_message(message)
        except Exception as e:
            if not self.is_connected:
                break
            print(f"[ERROR] recv failed: {e}")
            break


client = FishClient(cookie=cookie)
client.handle_message = types.MethodType(_probe_handle_message, client)
client.listen = types.MethodType(_patched_listen, client)

client.connect()
print(f"[{ts()}] Connected. Logging chat:room and chat:message events...")
print(f"[{ts()}] First 5 messages shown in full to inspect payload shape.")
print(f"[{ts()}] Will attempt room switch after 15 seconds.\n")

try:
    # Wait 15s to see initial room, then try switching
    time.sleep(15)

    print(f"\n[{ts()}] === ATTEMPTING ROOM SWITCH ===")
    print(f"[{ts()}] Messages received so far: {message_count}")

    # Try various possible payloads for switching to global chat
    # Attempt 1: simple string
    print(f"\n[{ts()}] Attempt 1: send_event('chat:room', 'global')")
    try:
        client.send_event("chat:room", "global")
        print(f"[{ts()}]   -> sent OK")
    except Exception as e:
        print(f"[{ts()}]   -> error: {e}")

    time.sleep(10)
    print(f"[{ts()}] Messages after attempt 1: {message_count}")

    # Attempt 2: dict with room field
    print(f"\n[{ts()}] Attempt 2: send_event('chat:room', {{'room': 'global'}})")
    try:
        client.send_event("chat:room", {"room": "global"})
        print(f"[{ts()}]   -> sent OK")
    except Exception as e:
        print(f"[{ts()}]   -> error: {e}")

    time.sleep(10)
    print(f"[{ts()}] Messages after attempt 2: {message_count}")

    # Attempt 3: dict with chatRoom field
    print(f"\n[{ts()}] Attempt 3: send_event('chat:room', {{'chatRoom': 'global'}})")
    try:
        client.send_event("chat:room", {"chatRoom": "global"})
        print(f"[{ts()}]   -> sent OK")
    except Exception as e:
        print(f"[{ts()}]   -> error: {e}")

    time.sleep(10)
    print(f"[{ts()}] Messages after attempt 3: {message_count}")

    print(f"\n[{ts()}] Probe complete. Staying connected to observe. Ctrl+C to stop.")
    while True:
        time.sleep(1)

except KeyboardInterrupt:
    pass
finally:
    client.is_connected = False
    try:
        client.websocket.close()
    except Exception:
        pass
    print(f"\n[{ts()}] Done. Total messages: {message_count}")
