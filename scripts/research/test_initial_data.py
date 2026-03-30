"""
Capture the initial-data payload and search for fishtoy-related data.
Saves the full payload to a file for inspection.
"""
import os, sys, json, time, types
import msgpack
from fishclient import FishClient

COOKIE = os.environ.get("FISHTANK_COOKIE", "")
if not COOKIE:
    print("Set FISHTANK_COOKIE env var first")
    sys.exit(1)

cookie = COOKIE.strip().strip("'\"").replace("\n", "").replace("\r", "")
client = FishClient(cookie=cookie)

def _patched_handle_message(self, message):
    if isinstance(message, str):
        if message.startswith("2"):
            self.websocket.send("3")
    elif isinstance(message, bytes):
        try:
            self.handle_packed(message)
        except Exception:
            pass

client.handle_message = types.MethodType(_patched_handle_message, client)

found_initial = [False]

@client.dispatcher.on("initial-data")
def on_initial(data):
    found_initial[0] = True
    print(f"[OK] Received initial-data. Type: {type(data).__name__}")

    # Save full payload to file
    with open("initial_data_dump.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, default=str, indent=2)
    print(f"[OK] Full payload saved to initial_data_dump.json")

    if isinstance(data, dict):
        print(f"\nTop-level keys: {list(data.keys())}")
        print()

        # Search for anything fishtoy-related
        def search(obj, path=""):
            results = []
            if isinstance(obj, dict):
                for k, v in obj.items():
                    full = f"{path}.{k}" if path else k
                    kl = k.lower()
                    if any(w in kl for w in ["fishtoy", "toy", "queue", "metadata", "love"]):
                        preview = str(v)[:200] if not isinstance(v, (dict, list)) else f"({type(v).__name__}, {len(v)} items)" if isinstance(v, list) else f"(dict, keys: {list(v.keys())[:8]})"
                        results.append((full, preview))
                    if isinstance(v, (dict, list)):
                        results.extend(search(v, full))
            elif isinstance(obj, list):
                for i, item in enumerate(obj[:20]):  # limit to first 20
                    results.extend(search(item, f"{path}[{i}]"))
            return results

        hits = search(data)
        if hits:
            print("FISHTOY-RELATED FIELDS FOUND:")
            for path, preview in hits:
                print(f"  {path}: {preview}")
        else:
            print("No fishtoy-related field names found in top-level scan.")
            print("Searching all string values for 'fishtoy'...")
            
            def deep_search(obj, path=""):
                results = []
                if isinstance(obj, str) and "fishtoy" in obj.lower():
                    results.append((path, obj[:200]))
                elif isinstance(obj, dict):
                    for k, v in obj.items():
                        results.extend(deep_search(v, f"{path}.{k}" if path else k))
                elif isinstance(obj, list):
                    for i, item in enumerate(obj[:50]):
                        results.extend(deep_search(item, f"{path}[{i}]"))
                return results

            deep = deep_search(data)
            if deep:
                print(f"Found {len(deep)} string matches:")
                for path, val in deep[:20]:
                    print(f"  {path}: {val}")
            else:
                print("No 'fishtoy' string found anywhere in the payload.")

    print("\nDone. Check initial_data_dump.json for the full payload.")
    print("Look for arrays that might contain fishtoy queue data.")

# Catch ALL events to see what arrives right after connect
@client.dispatcher.on("fishtoy:queued")
def ft_q(data):
    print(f"\n[FISHTOY:QUEUED] {json.dumps(data, default=str)[:300]}")

@client.dispatcher.on("fishtoy:update")
def ft_u(data):
    print(f"\n[FISHTOY:UPDATE] {json.dumps(data, default=str)[:300]}")

client.connect()
print("Waiting for initial-data and fishtoy events... (Ctrl+C to stop)\n")

try:
    for i in range(30):  # wait 30 seconds
        time.sleep(1)
        if found_initial[0]:
            break
    if not found_initial[0]:
        print("No initial-data received within 30 seconds.")
except KeyboardInterrupt:
    pass
finally:
    client.is_connected = False
    try:
        client.websocket.close()
    except:
        pass
    print("\nDone.")
