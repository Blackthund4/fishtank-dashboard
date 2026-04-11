"""
Notification Structure Probe
=============================
Probes fishtank REST API for notification endpoints to find the structured
title/body format seen in the site's toast notifications.

USAGE:
    cd backend
    python ../scripts/research/test_notification_probe.py
"""

import json, os, sys
from pathlib import Path

# Add backend to path for auth
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backend"))
from auth import AuthManager, load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / "backend" / ".env")

auth = AuthManager()
session = auth.get_session()

BASE = "https://api.fishtank.live"

# Candidate endpoints
ENDPOINTS = [
    "/v1/notifications",
    "/v1/notifications/recent",
    "/v1/notifications/global",
    "/v1/notifications/all",
    "/v1/notification",
    "/v1/notification/global",
    "/v1/notification/recent",
    "/v1/global-notifications",
    "/v1/director",
    "/v1/director/messages",
    "/v1/announcements",
    "/v1/feed",
    "/v1/feed/notifications",
]

print("=" * 60)
print("Probing fishtank notification REST endpoints")
print("=" * 60)

for ep in ENDPOINTS:
    url = BASE + ep
    try:
        resp = session.get(url, timeout=10)
        status = resp.status_code
        if status == 404:
            print(f"  404  {ep}")
            continue
        if status == 401:
            print(f"  401  {ep} (auth rejected)")
            continue

        print(f"\n  {status}  {ep}")
        try:
            data = resp.json()
            # Pretty print, but truncate if huge
            text = json.dumps(data, indent=2)
            if len(text) > 3000:
                print(text[:3000])
                print(f"  ... (truncated, total {len(text)} chars)")
            else:
                print(text)
        except:
            print(f"  (not JSON) {resp.text[:500]}")
        print()
    except Exception as e:
        print(f"  ERR  {ep}: {e}")

print("\nDone.")
