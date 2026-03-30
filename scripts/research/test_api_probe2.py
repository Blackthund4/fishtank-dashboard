"""
Second round API probe - based on /v1/tts and /v1/items patterns.
"""
import os, sys, json, requests

COOKIE = os.environ.get("FISHTANK_COOKIE", "")
cookie = COOKIE.strip().strip("'\"").replace("\n", "").replace("\r", "")

session = requests.Session()
session.cookies.set("sb-wcsaaupukpdmqdjcgaoo-auth-token", cookie, domain="api.fishtank.live")

auth = session.get("https://api.fishtank.live/v1/auth").json()
if not auth.get("session"):
    print("Auth failed"); sys.exit(1)
print("[OK] Authenticated\n")

# Patterns based on what we found:
# /v1/tts returns {"ttsMessages": [...]}
# /v1/items returns item definitions
# /v1/items/queue returned 500 (exists but needs params?)
candidates = [
    # Fishtoy variations following /v1/tts pattern
    "/v1/fishtoy/queue",
    "/v1/fishtoy/messages",
    "/v1/fishtoy/history",
    "/v1/fishtoy/recent",
    "/v1/fishtoy/list",
    "/v1/fishtoy/all",
    # SFX (same pattern as TTS?)
    "/v1/sfx/queue",
    "/v1/sfx/messages",
    # Items sub-endpoints
    "/v1/items/fishtoy",
    "/v1/items/fishtoys",
    "/v1/items/toys",
    "/v1/items/used",
    "/v1/items/history",
    "/v1/items/recent",
    "/v1/items/redemptions",
    # Plural variations
    "/v1/tts/messages",
    "/v1/tts/history",
    "/v1/tts/recent",
    # Queue with item type params
    "/v1/queue/tts",
    "/v1/queue/sfx",
    "/v1/queue/fishtoy",
    "/v1/queue/toys",
    # Use/redeem endpoints
    "/v1/use",
    "/v1/redeem",
    "/v1/shop",
    "/v1/shop/fishtoys",
    "/v1/shop/toys",
    "/v1/store",
    # User-specific
    "/v1/user",
    "/v1/user/items",
    "/v1/user/fishtoys",
    "/v1/user/inventory",
    "/v1/profile",
    "/v1/me",
    # Season/show endpoints
    "/v1/season",
    "/v1/show",
    "/v1/contestants",
    "/v1/fish",
    # Generic
    "/v1/status",
    "/v1/config",
    "/v1/settings",
    "/v1/features",
]

found = []
for path in candidates:
    url = f"https://api.fishtank.live{path}"
    try:
        r = session.get(url, timeout=5)
        body = r.text[:300] if r.text else ""
        if r.status_code == 200:
            print(f"  [200] {path}: {body[:200]}")
            found.append((path, r.status_code, body))
        elif r.status_code == 500:
            print(f"  [500] {path} (EXISTS but errored): {body[:100]}")
            found.append((path, r.status_code, body))
        elif r.status_code != 404:
            print(f"  [{r.status_code}] {path}: {body[:100]}")
            found.append((path, r.status_code, body))
        # Skip 404s silently
    except Exception as e:
        print(f"  [ERR] {path}: {e}")

print(f"\n{'='*60}")
print(f"Results: {len(found)} non-404 endpoints found")
for path, code, body in found:
    print(f"\n  [{code}] {path}")
    print(f"  {body[:300]}")
print(f"{'='*60}")
