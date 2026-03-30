"""
Fishtoy REST poller.
Polls /v1/items/recent and logs new fishtoy redemptions including metadata.
Resolves item IDs to human-readable names via /v1/items catalog.
Writes to both terminal and fishtoy_log.jsonl.
"""
import os, sys, json, time, requests
from datetime import datetime, timezone

COOKIE = os.environ.get("FISHTANK_COOKIE", "")
if not COOKIE:
    print("Set FISHTANK_COOKIE env var first")
    sys.exit(1)

cookie = COOKIE.strip().strip("'\"").replace("\n", "").replace("\r", "")

POLL_INTERVAL = 2
LOG_FILE = f"fishtoy_log_{datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M%S')}.jsonl"

session = requests.Session()
session.cookies.set("sb-wcsaaupukpdmqdjcgaoo-auth-token", cookie, domain="api.fishtank.live")

auth = session.get("https://api.fishtank.live/v1/auth")
if auth.status_code != 200 or not auth.json().get("session"):
    print("Auth failed. Check cookie.")
    sys.exit(1)
print("[OK] Authenticated")


def ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ---- Load item catalog for name lookup ----
item_catalog = {}
CAPTURE_TYPES = {"FISHTOY", "BIGTOY"}

try:
    r = session.get("https://api.fishtank.live/v1/items", timeout=10)
    if r.status_code == 200:
        raw = r.json()
        for key, val in raw.items():
            if isinstance(val, dict) and "id" in val:
                item_catalog[str(val["id"])] = val
        ft = sum(1 for v in item_catalog.values() if v.get("type") in CAPTURE_TYPES)
        print(f"[OK] Loaded {len(item_catalog)} items ({ft} fishtoys/bigtoys, skipping wartoys)")
    else:
        print(f"[WARN] Could not load item catalog (HTTP {r.status_code}). Will capture all items.")
except Exception as e:
    print(f"[WARN] Could not load item catalog: {e}. Will capture all items.")


def get_item_name(item_id):
    entry = item_catalog.get(str(item_id))
    return entry.get("name", f"Item #{item_id}") if entry else f"Item #{item_id}"


def should_capture(item_id):
    """Return True if item is a FISHTOY or BIGTOY (or if catalog unavailable)."""
    entry = item_catalog.get(str(item_id))
    if not entry:
        return True  # unknown item, capture it to be safe
    return entry.get("type") in CAPTURE_TYPES


def write_log(item):
    try:
        entry = {"timestamp": ts(), "event": "fishtoy:used", "data": item}
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        print(f"  [log write error: {e}]")


seen_ids = set()
prev_poll_ids = []
first_poll = True

print(f"Polling /v1/items/recent every {POLL_INTERVAL}s...")
print(f"Logging to {LOG_FILE}")
print("=" * 60)

try:
    while True:
        try:
            r = session.get("https://api.fishtank.live/v1/items/recent", timeout=10)
            if r.status_code in (401, 403):
                print(f"[{ts()}] Auth expired (HTTP {r.status_code}). Restart with a fresh cookie.")
                time.sleep(POLL_INTERVAL)
                continue
            if r.status_code != 200:
                print(f"[{ts()}] WARN: HTTP {r.status_code}")
                time.sleep(POLL_INTERVAL)
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
                print(f"  User:      {item.get('displayName', '?')}")
                print(f"  Cost:      {item.get('cost', '?')}")
                print(f"  Target:    {item.get('target', '?')}")
                print(f"  Status:    {item.get('status', '?')}")
                if item.get("secondaryTarget"):
                    print(f"  Secondary: {item['secondaryTarget']}")
                if item.get("metadata"):
                    print(f"  METADATA:  {item['metadata']}")
                if item.get("clanTag"):
                    print(f"  ClanTag:   {item['clanTag']}")
                sys.stdout.flush()
                write_log(item)

            prev_poll_ids.append(this_poll_ids)
            if len(prev_poll_ids) > 3:
                prev_poll_ids.pop(0)
            seen_ids = set().union(*prev_poll_ids)

            if first_poll:
                first_poll = False
                print(f"[OK] Initial snapshot: {len(items)} items. Watching for new ones...\n")

        except requests.RequestException as e:
            print(f"[{ts()}] Request failed: {e}")

        time.sleep(POLL_INTERVAL)

except KeyboardInterrupt:
    print("\nStopped.")
    os._exit(0)
