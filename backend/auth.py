"""
Fishtank Auth Manager

Handles automatic login, token caching, and re-authentication.
Reads credentials from .env file or environment variables.

Usage:
    from auth import AuthManager
    auth = AuthManager()
    cookie = auth.get_cookie()       # Always returns a valid cookie
    session = auth.get_session()     # Returns requests.Session with cookie set
    auth.handle_401()                # Call when a 401 is detected, triggers re-login
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from base64 import b64decode
from urllib.parse import quote

import requests as http_requests

# ============================================================
# .env FILE LOADER
# ============================================================

def load_dotenv(path=None):
    """Load .env file into os.environ. Does not override existing vars."""
    if path is None:
        path = Path(__file__).parent / ".env"
    else:
        path = Path(path)

    if not path.exists():
        return

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value


# ============================================================
# TOKEN UTILITIES
# ============================================================

def decode_jwt_payload(token):
    """Decode JWT payload without verification."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        # Add padding
        payload += "=" * (4 - len(payload) % 4)
        decoded = b64decode(payload)
        return json.loads(decoded)
    except Exception:
        return {}


def token_expires_at(token):
    """Return expiry timestamp (epoch seconds) of a JWT, or 0 if unknown."""
    payload = decode_jwt_payload(token)
    return payload.get("exp", 0)


def token_is_expired(token, buffer_seconds=60):
    """Check if a JWT is expired or will expire within buffer_seconds."""
    exp = token_expires_at(token)
    if exp == 0:
        return True
    return time.time() >= (exp - buffer_seconds)


# ============================================================
# AUTH MANAGER
# ============================================================

AUTH_URL = "https://api.fishtank.live/v1/auth/log-in"
COOKIE_NAME = "sb-wcsaaupukpdmqdjcgaoo-auth-token"
TOKEN_CACHE_FILE = Path(__file__).parent / "token_cache.json"


class AuthManager:
    def __init__(self):
        # Load .env
        load_dotenv()

        self._email = os.environ.get("FISHTANK_EMAIL", "")
        self._password = os.environ.get("FISHTANK_PASSWORD", "")
        self._cookie_override = os.environ.get("FISHTANK_COOKIE", "")

        self._access_token = None
        self._refresh_token = None
        self._lock = Lock()
        self._last_login = None
        self._login_count = 0

        # Determine auth mode
        if self._email and self._password:
            self._mode = "auto"
            print("[AUTH] Mode: automatic (email/password from .env)")
        elif self._cookie_override:
            self._mode = "manual"
            print("[AUTH] Mode: manual (FISHTANK_COOKIE from env)")
        else:
            self._mode = "none"
            print("[AUTH] Mode: none (no credentials configured)")
            return

        # Try to load cached tokens first (auto mode only)
        if self._mode == "auto":
            if self._load_cached_tokens():
                print(f"[AUTH] Loaded cached tokens (access expires: {self._format_expiry(self._access_token)})")
                # If access token is expired but we have credentials, re-login
                if token_is_expired(self._access_token, buffer_seconds=60):
                    print("[AUTH] Cached access token expired, logging in...")
                    self._login()
            else:
                self._login()

    def _format_expiry(self, token):
        """Format token expiry as human-readable string."""
        exp = token_expires_at(token)
        if exp == 0:
            return "unknown"
        dt = datetime.fromtimestamp(exp, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")

    def _login(self):
        """Authenticate via fishtank.live API and store tokens."""
        if not self._email or not self._password:
            print("[AUTH] Cannot login: no email/password configured")
            return False

        try:
            masked = self._email[:3] + "***" + self._email[self._email.index("@"):] if "@" in self._email else "***"
            print(f"[AUTH] Logging in as {masked}...")
            r = http_requests.post(
                AUTH_URL,
                json={"email": self._email, "password": self._password},
                timeout=15,
            )

            if r.status_code != 200:
                print(f"[AUTH] Login failed: HTTP {r.status_code}")
                try:
                    err = r.json()
                    print(f"[AUTH] Error: {err.get('message', err)}")
                except Exception:
                    print(f"[AUTH] Response: {r.text[:200]}")
                return False

            data = r.json()
            session = data.get("session", {})
            access = session.get("access_token")
            refresh = session.get("refresh_token")

            if not access or not refresh:
                print("[AUTH] Login response missing tokens")
                return False

            with self._lock:
                self._access_token = access
                self._refresh_token = refresh
                self._last_login = datetime.now(timezone.utc)
                self._login_count += 1

            self._save_cached_tokens()

            access_exp = self._format_expiry(access)
            refresh_exp = self._format_expiry(refresh)
            print(f"[AUTH] Login successful (login #{self._login_count})")
            print(f"[AUTH]   Access token expires:  {access_exp}")
            print(f"[AUTH]   Refresh token expires: {refresh_exp}")
            return True

        except http_requests.RequestException as e:
            print(f"[AUTH] Login request failed: {e}")
            return False

    def _save_cached_tokens(self):
        """Save tokens to disk for reuse across restarts."""
        try:
            data = {
                "access_token": self._access_token,
                "refresh_token": self._refresh_token,
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
            TOKEN_CACHE_FILE.write_text(json.dumps(data, indent=2))
            # Restrict file permissions (owner read/write only)
            try:
                TOKEN_CACHE_FILE.chmod(0o600)
            except OSError:
                pass  # Windows doesn't support chmod the same way
        except Exception as e:
            print(f"[AUTH] Could not save token cache: {e}")

    def _load_cached_tokens(self):
        """Load tokens from disk. Returns True if valid tokens were loaded."""
        if not TOKEN_CACHE_FILE.exists():
            return False
        try:
            data = json.loads(TOKEN_CACHE_FILE.read_text())
            access = data.get("access_token", "")
            refresh = data.get("refresh_token", "")
            if not access or not refresh:
                return False
            # Check if refresh token is still valid (30-day lifetime)
            if token_is_expired(refresh, buffer_seconds=3600):
                print("[AUTH] Cached refresh token expired, will re-login")
                return False
            self._access_token = access
            self._refresh_token = refresh
            return True
        except Exception:
            return False

    def get_cookie(self):
        """Return a valid cookie string for use with fishtank API requests.

        In auto mode: returns constructed cookie from tokens.
        In manual mode: returns the raw FISHTANK_COOKIE value.
        """
        if self._mode == "manual":
            return self._cookie_override.strip().strip("'\"").replace("\n", "").replace("\r", "")

        if self._mode == "none":
            return ""

        with self._lock:
            if self._access_token and self._refresh_token:
                return self._build_cookie()
        return ""

    def _build_cookie(self):
        """Build the cookie value from access and refresh tokens.

        The cookie is a JSON array: ["access_token", "refresh_token"]
        URL-encoded when set as a cookie value.
        """
        return json.dumps([self._access_token, self._refresh_token])

    def get_session(self):
        """Return a requests.Session with the auth cookie set."""
        session = http_requests.Session()
        if self._mode == "manual":
            # Manual cookie is already in the correct format from the user
            raw = self._cookie_override.strip().strip("'\"").replace("\n", "").replace("\r", "")
            if raw:
                session.cookies.set(COOKIE_NAME, raw, domain="api.fishtank.live")
        elif self._mode == "auto":
            with self._lock:
                if self._access_token and self._refresh_token:
                    cookie_val = self._build_cookie()
                    encoded = quote(cookie_val, safe="")
                    session.cookies.set(COOKIE_NAME, encoded, domain="api.fishtank.live")
        return session

    def get_fishclient_cookie(self):
        """Return the cookie value in the format fishclient expects.

        fishclient sends this as the auth cookie to the socket server.
        Must be URL-encoded to match what the browser sends in the Cookie header.
        """
        if self._mode == "manual":
            return self._cookie_override.strip().strip("'\"").replace("\n", "").replace("\r", "")

        if self._mode == "none":
            return ""

        with self._lock:
            if self._access_token and self._refresh_token:
                raw = json.dumps([self._access_token, self._refresh_token])
                return quote(raw, safe="")
        return ""

    def handle_401(self):
        """Called when a 401 is detected. Triggers re-authentication."""
        if self._mode != "auto":
            print("[AUTH] 401 detected but not in auto mode. Manual cookie may have expired.")
            return False

        with self._lock:
            # Avoid multiple simultaneous re-logins
            if self._last_login and (datetime.now(timezone.utc) - self._last_login).total_seconds() < 30:
                print("[AUTH] Skipping re-login (last login was <30s ago)")
                return bool(self._access_token)

        print("[AUTH] 401 detected. Re-authenticating...")
        success = self._login()
        if success:
            print("[AUTH] Re-authentication successful. Connections will use new tokens.")
        else:
            print("[AUTH] Re-authentication FAILED. Check credentials in .env file.")
        return success

    @property
    def is_configured(self):
        """Return True if auth credentials are available."""
        return self._mode != "none"

    @property
    def mode(self):
        return self._mode

    def status(self):
        """Return auth status for the /api/status endpoint."""
        status = {
            "mode": self._mode,
            "configured": self.is_configured,
            "login_count": self._login_count,
        }
        if self._access_token:
            status["access_token_expires"] = self._format_expiry(self._access_token)
            status["access_token_expired"] = token_is_expired(self._access_token, buffer_seconds=0)
        if self._refresh_token:
            status["refresh_token_expires"] = self._format_expiry(self._refresh_token)
        if self._last_login:
            status["last_login"] = self._last_login.isoformat()
        return status
