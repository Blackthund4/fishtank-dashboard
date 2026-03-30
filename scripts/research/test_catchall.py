"""
Fishtank Catchall - logs EVERY packet the server sends.
Intercepts at the lowest level (handle_message) before any filtering.
"""
import os, sys, time, types, json
import msgpack
from fishclient import FishClient

COOKIE = os.environ.get("FISHTANK_COOKIE", "")
if not COOKIE:
    print("Set FISHTANK_COOKIE env var first")
    sys.exit(1)

cookie = COOKIE.strip().strip("'\"").replace("\n", "").replace("\r", "")


def _catchall_handle_message(self, message):
    """Intercept at the lowest level - sees text AND binary frames."""

    if isinstance(message, str):
        # Text frame (Engine.IO)
        if message.startswith("2"):
            self.websocket.send("3")  # pong
            # Don't log pings, too noisy
        else:
            print(f"[TEXT] {message[:100]}")
            sys.stdout.flush()

    elif isinstance(message, bytes):
        # Binary frame - decode msgpack ourselves to see EVERYTHING
        try:
            unpacked = msgpack.unpackb(message, raw=False)
            pkt_type = unpacked.get("type")
            pkt_data = unpacked.get("data")
            pkt_nsp = unpacked.get("nsp", "/")
            pkt_id = unpacked.get("id")

            if pkt_type == 2:
                # Standard EVENT - data is [event_name, payload]
                event_name = pkt_data[0] if isinstance(pkt_data, list) and len(pkt_data) > 0 else "?"
                payload = pkt_data[1] if isinstance(pkt_data, list) and len(pkt_data) > 1 else pkt_data

                preview = ""
                if isinstance(payload, dict):
                    keys = list(payload.keys())[:8]
                    preview = str(keys)
                else:
                    preview = str(payload)[:100]

                print(f"[EVENT type=2] {event_name}: {preview}")

            elif pkt_type == 3:
                # ACK - server acknowledging something
                print(f"[ACK type=3] id={pkt_id} data={str(pkt_data)[:200]}")

            elif pkt_type == 0:
                # CONNECT acknowledgment
                print(f"[CONNECT type=0] nsp={pkt_nsp} data={str(pkt_data)[:200]}")

            elif pkt_type == 1:
                # DISCONNECT
                print(f"[DISCONNECT type=1] nsp={pkt_nsp}")

            elif pkt_type == 4:
                # CONNECT_ERROR
                print(f"[CONNECT_ERROR type=4] data={str(pkt_data)[:200]}")

            else:
                # Anything else
                print(f"[UNKNOWN type={pkt_type}] {str(unpacked)[:300]}")

            sys.stdout.flush()

        except Exception as e:
            print(f"[BINARY] Failed to decode: {e} (first 20 bytes: {message[:20]})")
            sys.stdout.flush()


def _patched_listen(self):
    """Clean listen with proper shutdown handling."""
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
client.handle_message = types.MethodType(_catchall_handle_message, client)
client.listen = types.MethodType(_patched_listen, client)

client.connect()
print("Listening for ALL packets (every type, every event)...")
print("Ctrl+C to stop.\n")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    client.is_connected = False
    try:
        client.websocket.close()
    except Exception:
        pass
    print("\nDone.")
