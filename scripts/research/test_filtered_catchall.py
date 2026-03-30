"""
Filtered catchall - shows all socket events EXCEPT chat, TTS, and SFX.
Use this to discover new event types like polls, director messages, etc.
"""
import os, sys, time, types, msgpack
from fishclient import FishClient

SKIP = {'chat:message', 'tts:queued', 'tts:update', 'sfx:queued', 'sfx:update'}

cookie = os.environ.get("FISHTANK_COOKIE", "")
if not cookie:
    print("Set FISHTANK_COOKIE env var first"); sys.exit(1)
cookie = cookie.strip().strip("'\"").replace("\n", "").replace("\r", "")


def _handle(self, msg):
    if isinstance(msg, str):
        if msg.startswith("2"):
            self.websocket.send("3")
        else:
            print(f"[TEXT] {msg[:200]}")
            sys.stdout.flush()
    elif isinstance(msg, bytes):
        try:
            u = msgpack.unpackb(msg, raw=False)
            d = u.get("data")
            if u.get("type") == 2 and isinstance(d, list) and len(d) > 0:
                name = d[0]
                payload = d[1] if len(d) > 1 else d
                if name not in SKIP:
                    print(f"\n[EVENT] {name}")
                    if isinstance(payload, dict):
                        for k, v in payload.items():
                            print(f"  {k}: {str(v)[:200]}")
                    else:
                        print(f"  {str(payload)[:500]}")
                    sys.stdout.flush()
            elif u.get("type") != 2:
                print(f"\n[TYPE={u.get('type')}] {str(u)[:300]}")
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


client = FishClient(cookie=cookie)
client.handle_message = types.MethodType(_handle, client)
client.listen = types.MethodType(_listen, client)

print("Listening (chat/tts/sfx filtered out)...")
print("Watching for: polls, director messages, happenings, unknown events")
print("Auto-reconnects on disconnect.")
print("Ctrl+C to stop.\n")

try:
    while True:
        try:
            client.connect()
            print("[OK] Connected.\n")
            while client.is_connected:
                time.sleep(1)
            print("\n[!] Disconnected. Reconnecting in 5s...")
            time.sleep(5)
            # Reset for reconnection
            client = FishClient(cookie=cookie)
            client.handle_message = types.MethodType(_handle, client)
            client.listen = types.MethodType(_listen, client)
        except Exception as e:
            print(f"\n[!] Connection error: {e}. Retrying in 5s...")
            time.sleep(5)
            client = FishClient(cookie=cookie)
            client.handle_message = types.MethodType(_handle, client)
            client.listen = types.MethodType(_listen, client)
except KeyboardInterrupt:
    print("\nDone.")
    os._exit(0)
