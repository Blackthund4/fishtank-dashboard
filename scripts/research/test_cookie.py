import requests

# Paste the cookie value from the Network tab (NOT the Application tab)
# DevTools > Network > click a request to api.fishtank.live > Request Headers > Cookie
# Copy everything AFTER "sb-wcsaaupukpdmqdjcgaoo-auth-token="
COOKIE = """PASTE_VALUE_FROM_NETWORK_TAB_HERE"""

r = requests.get(
    "https://api.fishtank.live/v1/auth",
    headers={
        "Cookie": f"sb-wcsaaupukpdmqdjcgaoo-auth-token={COOKIE}"
    },
)
print(f"Status: {r.status_code}")
print(f"Cookie length: {len(COOKIE)}")
print(r.text[:500])
