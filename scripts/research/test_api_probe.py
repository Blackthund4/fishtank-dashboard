"""
Probe the fishtank.live REST API for fishtoy-related endpoints.
"""
import os, sys, requests

COOKIE = os.environ.get("FISHTANK_COOKIE", "")
if not COOKIE:
    print("Set FISHTANK_COOKIE env var first")
    sys.exit(1)

cookie = COOKIE.strip().strip("'\"").replace("\n", "").replace("\r", "")

# Use the same auth flow as fishclient
session = requests.Session()
session.cookies.set("sb-wcsaaupukpdmqdjcgaoo-auth-token", cookie, domain="api.fishtank.live")

# Get access token
auth = session.get("https://api.fishtank.live/v1/auth")
if auth.status_code != 200:
    print(f"Auth failed: {auth.status_code}")
    sys.exit(1)

auth_data = auth.json()
if not auth_data.get("session"):
    print("No session. Cookie invalid.")
    sys.exit(1)

token = auth_data["session"]["access_token"]
print(f"[OK] Authenticated. Token: ...{token[-20:]}\n")

# Endpoints to try
candidates = [
    "/v1/fishtoys",
    "/v1/fishtoy",
    "/v1/fishtoy/queue",
    "/v1/fishtoys/queue",
    "/v1/fishtoys/recent",
    "/v1/fishtoys/history",
    "/v1/toys",
    "/v1/toys/queue",
    "/v1/items",
    "/v1/items/queue",
    "/v1/queue",
    "/v1/queue/fishtoy",
    "/v1/activity",
    "/v1/activity/fishtoy",
    "/v1/events",
    "/v1/events/fishtoy",
    "/v1/happening",
    "/v1/happenings",
    "/v1/tts",
    "/v1/tts/queue",
    "/v1/sfx",
    "/v1/sfx/queue",
    "/v1/redemptions",
    "/v1/transactions",
    "/v1/fishtoy-queue",
    "/v1/toy-queue",
]

print("Probing REST endpoints...\n")

found = []
for path in candidates:
    url = f"https://api.fishtank.live{path}"
    try:
        r = session.get(url, timeout=5)
        status = r.status_code
        body = r.text[:200] if r.text else ""

        if status == 200:
            print(f"  [200 OK]    {path}: {body}")
            found.append((path, body))
        elif status == 404:
            print(f"  [404]       {path}")
        elif status == 401:
            print(f"  [401 UNAUTH] {path}")
        elif status == 403:
            print(f"  [403 FORBID] {path}")
        else:
            print(f"  [{status}]       {path}: {body[:100]}")
    except Exception as e:
        print(f"  [ERROR]     {path}: {e}")

print(f"\n{'=' * 60}")
if found:
    print(f"FOUND {len(found)} working endpoint(s):")
    for path, body in found:
        print(f"\n  {path}:")
        print(f"  {body}")
else:
    print("No fishtoy endpoints found with these paths.")
    print("The data might be behind a different URL pattern.")
print(f"{'=' * 60}")
