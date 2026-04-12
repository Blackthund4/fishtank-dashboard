"""
Unit tests for fishtank dashboard backend.

Tests database operations against an in-memory SQLite database
and filter/dedup functions from server.py.

Run:
    cd backend
    python -m pytest test_backend.py -v
"""

import json
import os
import sqlite3
import sys
import pytest

# Override DB path BEFORE importing database module
os.environ["FISHTANK_DB_PATH"] = ":memory:"

import database

# Import functions from server and ingest (without starting either app)
sys.path.insert(0, os.path.dirname(__file__))
from ingest import _should_filter_chat, _should_filter_notification, _is_duplicate, _seen_tts_sfx_ids
from ingest import _score_sentiment
from server import (
    _check_rate_limit, _prune_rate_limits, _rate_limits, RATE_LIMIT_MAX, _get_client_ip,
    _is_origin_allowed, _try_reserve_ws_slot, _release_ws_slot,
    _ws_ip_counts, browser_clients, MAX_WS_PER_IP, MAX_WS_CLIENTS,
)
import server as _server


# ============================================================
# FIXTURES
# ============================================================


@pytest.fixture(autouse=True)
def fresh_db():
    """Reset the database for each test."""
    # Clear read-only mode from a previous test so the DROP TABLE below can
    # run. Must happen BEFORE getting the connection.
    database.disable_readonly()
    # Get a fresh connection for this thread
    conn = database._get_conn()
    # Drop and recreate tables
    conn.executescript("""
        DROP TABLE IF EXISTS events;
        DROP TABLE IF EXISTS stock_history;
        DROP TABLE IF EXISTS _notify;
        DROP TABLE IF EXISTS keyword_counts;
        DROP TABLE IF EXISTS _kv;
    """)
    database.init_db()
    yield
    # Clear dedup and rate limit state between tests
    _seen_tts_sfx_ids.clear()
    _rate_limits.clear()
    _ws_ip_counts.clear()
    browser_clients.clear()
    database.disable_readonly()


# ============================================================
# DATABASE: store_event
# ============================================================


def test_store_event_returns_id():
    db_id = database.store_event("chat:message", {"message": "hello"})
    assert isinstance(db_id, int)
    assert db_id > 0


def test_store_event_extracts_event_id():
    database.store_event("tts:update", {"id": "12345", "message": "test"})
    events = database.get_events(event_type="tts:update")
    assert events[0]["event_id"] == "12345"


def test_store_event_extracts_timestamp():
    database.store_event("fishtoy:used", {"id": "1", "createdAt": 1700000000000})
    events = database.get_events(event_type="fishtoy:used")
    assert events[0]["timestamp_server"] == 1700000000000


def test_store_event_handles_non_dict():
    db_id = database.store_event("poll:vote", [{"value": "A", "score": 10}])
    assert db_id > 0


# ============================================================
# DATABASE: _extract_columns
# ============================================================


def test_extract_columns_tts():
    ext = database._extract_columns("tts:update", {
        "displayName": "Alice", "cost": 200, "room": "room-1", "message": "hi"
    })
    assert ext["cost"] == 200
    assert ext["display_name"] == "Alice"
    assert ext["room"] == "room-1"
    assert ext["metadata"] is None
    assert ext["item_id"] is None
    assert ext["feature"] is None


def test_extract_columns_fishtoy():
    ext = database._extract_columns("fishtoy:used", {
        "displayName": "Bob", "target": "Contestant", "cost": 50,
        "itemId": 42, "metadata": "secret message"
    })
    assert ext["display_name"] == "Bob"
    assert ext["target"] == "Contestant"
    assert ext["cost"] == 50
    assert ext["item_id"] == "42"
    assert ext["metadata"] == "secret message"


def test_extract_columns_feature_toggle():
    ext = database._extract_columns("feature-toggles:update", {
        "feature": "tts", "enabled": True
    })
    assert ext["feature"] == "tts"


def test_extract_columns_metadata_normalization():
    assert database._extract_columns("fishtoy:used", {"metadata": None})["metadata"] is None
    assert database._extract_columns("fishtoy:used", {"metadata": "null"})["metadata"] is None
    assert database._extract_columns("fishtoy:used", {"metadata": ""})["metadata"] is None
    assert database._extract_columns("fishtoy:used", {"metadata": "hello"})["metadata"] == "hello"


def test_extract_columns_non_dict():
    ext = database._extract_columns("poll:vote", [1, 2, 3])
    assert all(v is None for v in ext.values())


def test_extract_columns_chat_user_fallback():
    ext = database._extract_columns("chat:message", {"user": {"displayName": "Charlie"}})
    assert ext["display_name"] == "Charlie"


# ============================================================
# DATABASE: get_events
# ============================================================


def test_get_events_returns_stored():
    database.store_event("chat:message", {"message": "one"})
    database.store_event("chat:message", {"message": "two"})
    events = database.get_events()
    assert len(events) == 2


def test_get_events_filters_by_type():
    database.store_event("chat:message", {"message": "chat"})
    database.store_event("tts:update", {"message": "tts"})
    events = database.get_events(event_type="chat:message")
    assert len(events) == 1
    assert events[0]["event_type"] == "chat:message"


def test_get_events_comma_separated_types():
    database.store_event("chat:message", {"message": "chat"})
    database.store_event("tts:update", {"message": "tts"})
    database.store_event("sfx:update", {"message": "sfx"})
    events = database.get_events(event_type="tts:update,sfx:update")
    assert len(events) == 2


def test_get_events_since_id():
    id1 = database.store_event("chat:message", {"message": "old"})
    id2 = database.store_event("chat:message", {"message": "new"})
    events = database.get_events(since_id=id1)
    assert len(events) == 1
    assert events[0]["id"] == id2


def test_get_events_limit():
    for i in range(10):
        database.store_event("chat:message", {"message": f"msg {i}"})
    events = database.get_events(limit=3)
    assert len(events) == 3


# ============================================================
# DATABASE: get_stats
# ============================================================


def test_get_stats_empty():
    stats = database.get_stats()
    assert stats["total_events"] == 0
    assert stats["total_spend"] == 0


def test_get_stats_counts():
    database.store_event("chat:message", {"message": "hi"})
    database.store_event("tts:update", {"message": "tts", "cost": 248})
    database.store_event("fishtoy:used", {"target": "LAND", "cost": 1000, "displayName": "user1"})
    stats = database.get_stats()
    assert stats["total_events"] == 3
    assert stats["by_type"]["chat:message"] == 1
    assert stats["fishtoys"]["total"] == 1
    assert stats["fishtoys"]["total_cost"] == 1000
    assert stats["total_spend"] == 1248


def test_get_stats_with_since():
    database.store_event("chat:message", {"message": "old"})
    # since far in the future should return 0
    stats = database.get_stats(since="2099-01-01T00:00:00Z")
    assert stats["total_events"] == 0


# ============================================================
# DATABASE: search_user
# ============================================================


def test_search_user_finds_chat():
    database.store_event("chat:message", {"user": {"displayName": "TestUser"}, "message": "hello"})
    results = database.search_user("TestUser")
    assert results["totals"]["chat"] == 1


def test_search_user_case_insensitive():
    database.store_event("chat:message", {"user": {"displayName": "TestUser"}, "message": "hello"})
    results = database.search_user("testuser")
    assert results["totals"]["chat"] == 1


def test_search_user_finds_tts():
    database.store_event("tts:update", {"displayName": "Sender1", "message": "hello", "cost": 248})
    results = database.search_user("Sender1")
    assert results["totals"]["tts"] == 1


def test_search_user_finds_fishtoys():
    database.store_event("fishtoy:used", {"displayName": "Sender1", "target": "LAND", "cost": 1000})
    results = database.search_user("Sender1")
    assert results["totals"]["fishtoys"] == 1


# ============================================================
# DATABASE: suggest_users
# ============================================================


def test_suggest_users_prefix_match():
    database.store_event("chat:message", {"user": {"displayName": "AlphaUser"}, "message": "hi"})
    database.store_event("chat:message", {"user": {"displayName": "AlphaBeta"}, "message": "hi"})
    database.store_event("chat:message", {"user": {"displayName": "GammaUser"}, "message": "hi"})
    suggestions = database.suggest_users("alpha")
    assert len(suggestions) == 2
    assert all("Alpha" in s or "alpha" in s.lower() for s in suggestions)


def test_suggest_users_no_match():
    database.store_event("chat:message", {"user": {"displayName": "TestUser"}, "message": "hi"})
    suggestions = database.suggest_users("zzz")
    assert len(suggestions) == 0


def test_suggest_users_cross_event_types():
    database.store_event("chat:message", {"user": {"displayName": "SharedUser"}, "message": "hi"})
    database.store_event("tts:update", {"displayName": "SharedUser", "message": "tts"})
    suggestions = database.suggest_users("shared")
    # Should deduplicate
    assert len(suggestions) == 1


# ============================================================
# DATABASE: get_polls
# ============================================================


def test_get_polls_excludes_votes():
    database.store_event("poll:start", {"poll": {"pid": "t1", "question": "Test?", "answers": ["A", "B"]}})
    database.store_event("poll:vote", [{"value": "A", "score": 5}])
    database.store_event("poll:stop", {"pid": "t1", "question": "Test?", "winner": "A"})
    polls = database.get_polls()
    # poll:start filtered out (matched by pid), only poll:stop remains
    assert len(polls) == 1
    assert polls[0]["event_type"] == "poll:stop"
    assert "poll:vote" not in {p["event_type"] for p in polls}


def test_get_polls_attaches_vote_scores():
    """poll:stop results should include full vote breakdown, not just winner."""
    database.store_event("poll:start", {"poll": {"pid": "v1", "question": "Pick?", "answers": ["A", "B"]}})
    database.store_event("poll:vote", [{"value": "A", "score": 30}, {"value": "B", "score": 20}])
    database.store_event("poll:stop", {"pid": "v1", "question": "Pick?", "winner": "A"})
    polls = database.get_polls()
    stop = [p for p in polls if p["event_type"] == "poll:stop"][0]
    votes = stop["data"].get("votes", [])
    assert len(votes) == 2
    scores = {v["value"]: v["score"] for v in votes}
    assert scores["A"] == 30
    assert scores["B"] == 20


def test_get_polls_scoped_votes():
    """Each poll:stop should get votes from its own poll, not a different one."""
    database.store_event("poll:start", {"poll": {"pid": "p1", "question": "Q1?", "answers": ["X", "Y"]}})
    database.store_event("poll:vote", [{"value": "X", "score": 100}, {"value": "Y", "score": 50}])
    database.store_event("poll:stop", {"pid": "p1", "question": "Q1?", "winner": "X"})
    database.store_event("poll:start", {"poll": {"pid": "p2", "question": "Q2?", "answers": ["M", "N"]}})
    database.store_event("poll:vote", [{"value": "M", "score": 10}, {"value": "N", "score": 5}])
    database.store_event("poll:stop", {"pid": "p2", "question": "Q2?", "winner": "M"})
    polls = database.get_polls()
    stops = [p for p in polls if p["event_type"] == "poll:stop"]
    assert len(stops) == 2
    # Results are newest-first
    stop_q2 = stops[0]
    stop_q1 = stops[1]
    q2_scores = {v["value"]: v["score"] for v in stop_q2["data"]["votes"]}
    q1_scores = {v["value"]: v["score"] for v in stop_q1["data"]["votes"]}
    assert q2_scores == {"M": 10, "N": 5}
    assert q1_scores == {"X": 100, "Y": 50}


def test_get_polls_stale_start_suppressed():
    """Only the newest poll:start without a stop should appear (older ones are implicitly closed)."""
    database.store_event("poll:start", {"poll": {"pid": "old", "question": "Old?", "answers": ["A", "B"]}})
    database.store_event("poll:start", {"poll": {"pid": "new", "question": "New?", "answers": ["C", "D"]}})
    polls = database.get_polls()
    starts = [p for p in polls if p["event_type"] == "poll:start"]
    assert len(starts) == 1
    assert starts[0]["data"]["poll"]["pid"] == "new"


def test_get_polls_no_votes_fallback():
    """When no poll:vote events exist, fall back to initial scores from poll:start."""
    database.store_event("poll:start", {"poll": {
        "pid": "fb1", "question": "Fallback?", "answers": ["A", "B"],
        "scores": [{"value": "A", "score": 0}, {"value": "B", "score": 0}],
    }})
    database.store_event("poll:stop", {"pid": "fb1", "question": "Fallback?", "winner": "A"})
    polls = database.get_polls()
    stop = [p for p in polls if p["event_type"] == "poll:stop"][0]
    votes = stop["data"].get("votes", [])
    assert len(votes) == 2


# ============================================================
# DATABASE: get_latest_poll_state
# ============================================================


def test_poll_state_complete():
    database.store_event("poll:start", {"poll": {"pid": "1", "question": "Who?", "answers": ["A", "B"]}})
    database.store_event("poll:vote", [{"value": "A", "score": 10}, {"value": "B", "score": 5}])
    database.store_event("poll:stop", {"pid": "1", "question": "Who?", "winner": "A"})
    state = database.get_latest_poll_state()
    assert state["question"] == "Who?"
    assert state["winner"] == "A"
    assert state["active"] is False


def test_poll_state_missing_stop():
    database.store_event("poll:start", {"poll": {"pid": "2", "question": "Pick?", "answers": ["X", "Y"]}})
    database.store_event("poll:vote", [{"value": "X", "score": 100}, {"value": "Y", "score": 50}])
    state = database.get_latest_poll_state()
    assert state["question"] == "Pick?"
    assert state["active"] is True
    assert state.get("winner") is None
    assert len(state["votes"]) == 2


def test_poll_state_empty():
    state = database.get_latest_poll_state()
    assert state is None


# ============================================================
# DATABASE: dedup_tts_sfx
# ============================================================


def test_dedup_removes_duplicates():
    database.store_event("tts:update", {"id": "100", "message": "hello", "status": "approved"})
    database.store_event("tts:update", {"id": "100", "message": "hello", "status": "played"})
    database.store_event("tts:update", {"id": "101", "message": "other"})
    deleted = database.dedup_tts_sfx()
    assert deleted == 1
    events = database.get_events(event_type="tts:update")
    assert len(events) == 2
    # First entry (approved) kept, second (played) removed
    ids = {e["event_id"] for e in events}
    assert "100" in ids and "101" in ids


def test_dedup_no_duplicates():
    database.store_event("tts:update", {"id": "200", "message": "one"})
    database.store_event("tts:update", {"id": "201", "message": "two"})
    deleted = database.dedup_tts_sfx()
    assert deleted == 0


# ============================================================
# DATABASE: purge functions
# ============================================================


def test_purge_system_chat():
    database.store_event("chat:message", {"user": {"displayName": "tts"}, "message": "echo"})
    database.store_event("chat:message", {"user": {"displayName": "sfx"}, "message": "echo"})
    database.store_event("chat:message", {"user": {"displayName": "emote"}, "message": "echo"})
    database.store_event("chat:message", {"user": {"displayName": "RealUser"}, "message": "real"})
    deleted = database.purge_system_chat()
    assert deleted == 3
    events = database.get_events(event_type="chat:message")
    assert len(events) == 1
    assert events[0]["data"]["user"]["displayName"] == "RealUser"


def test_purge_gift_notifications():
    database.store_event("notification:global", {"message": "JohnDoe gifted 5 season passes!"})
    database.store_event("notification:global", {"message": "The director has spoken."})
    deleted = database.purge_gift_notifications()
    assert deleted == 1
    events = database.get_events(event_type="notification:global")
    assert len(events) == 1


# ============================================================
# DATABASE: health queries
# ============================================================


def test_get_last_event_per_type():
    database.store_event("chat:message", {"message": "hi"})
    database.store_event("tts:update", {"message": "tts"})
    database.store_event("chat:message", {"message": "hi again"})
    result = database.get_last_event_per_type()
    assert "chat:message" in result
    assert result["chat:message"]["total"] == 2
    assert "tts:update" in result
    assert result["tts:update"]["total"] == 1


def test_get_event_count():
    assert database.get_event_count() == 0
    database.store_event("chat:message", {"message": "one"})
    database.store_event("chat:message", {"message": "two"})
    assert database.get_event_count() == 2


# ============================================================
# FILTERS: _should_filter_chat
# ============================================================


def test_filter_chat_tts():
    assert _should_filter_chat({"user": {"displayName": "tts"}, "message": "echo"}) is True


def test_filter_chat_sfx():
    assert _should_filter_chat({"user": {"displayName": "sfx"}, "message": "echo"}) is True


def test_filter_chat_emote():
    assert _should_filter_chat({"user": {"displayName": "emote"}, "message": "echo"}) is True


def test_filter_chat_case_insensitive():
    assert _should_filter_chat({"user": {"displayName": "TTS"}, "message": "echo"}) is True
    assert _should_filter_chat({"user": {"displayName": "Emote"}, "message": "echo"}) is True


def test_filter_chat_allows_normal_user():
    assert _should_filter_chat({"user": {"displayName": "RealPerson"}, "message": "hi"}) is False


def test_filter_chat_handles_bad_data():
    assert _should_filter_chat(None) is False
    assert _should_filter_chat("not a dict") is False
    assert _should_filter_chat({"user": None}) is False


# ============================================================
# FILTERS: _should_filter_notification
# ============================================================


def test_filter_notification_gift():
    assert _should_filter_notification({"message": "JohnDoe gifted 5 season passes!"}) is True


def test_filter_notification_case_insensitive():
    assert _should_filter_notification({"message": "User GIFTED 10 Season Passes!"}) is True


def test_filter_notification_allows_director():
    assert _should_filter_notification({"message": "Everyone to the living room NOW"}) is False


def test_filter_notification_handles_string():
    assert _should_filter_notification("JohnDoe gifted 3 season passes!") is True


def test_filter_notification_handles_none():
    assert _should_filter_notification(None) is False


# ============================================================
# FILTERS: _is_duplicate
# ============================================================


def test_dedup_catches_same_id():
    data = {"id": "500", "displayName": "Test", "message": "hello"}
    assert _is_duplicate("tts:update", data) is False  # first time
    assert _is_duplicate("tts:update", data) is True   # duplicate


def test_dedup_allows_different_ids():
    data1 = {"id": "501", "displayName": "Test", "message": "hello"}
    data2 = {"id": "502", "displayName": "Test", "message": "hello"}
    assert _is_duplicate("tts:update", data1) is False
    assert _is_duplicate("tts:update", data2) is False


def test_dedup_ignores_non_tts():
    data = {"id": "503", "message": "hello"}
    assert _is_duplicate("chat:message", data) is False
    assert _is_duplicate("chat:message", data) is False  # still not duplicate


def test_dedup_handles_no_id():
    assert _is_duplicate("tts:update", {"message": "no id"}) is False
    assert _is_duplicate("tts:update", {"message": "no id"}) is False


def test_dedup_handles_bad_data():
    assert _is_duplicate("tts:update", None) is False
    assert _is_duplicate("tts:update", "string") is False


# ============================================================
# RATE LIMITING
# ============================================================


def test_rate_limit_allows_normal_traffic():
    for _ in range(10):
        assert _check_rate_limit("192.168.1.1") is False


def test_rate_limit_rejects_over_limit():
    for _ in range(RATE_LIMIT_MAX):
        _check_rate_limit("10.0.0.1")
    assert _check_rate_limit("10.0.0.1") is True


def test_rate_limit_per_ip():
    for _ in range(RATE_LIMIT_MAX):
        _check_rate_limit("10.0.0.2")
    # Different IP should still be allowed
    assert _check_rate_limit("10.0.0.3") is False


def test_rate_limit_prune_cleans_stale():
    _check_rate_limit("10.0.0.4")
    assert "10.0.0.4" in _rate_limits
    # Manually expire the entry
    _rate_limits["10.0.0.4"] = [0]  # timestamp 0 = long ago
    _prune_rate_limits()
    assert "10.0.0.4" not in _rate_limits


def test_rate_limit_prune_keeps_active():
    _check_rate_limit("10.0.0.5")
    _prune_rate_limits()
    assert "10.0.0.5" in _rate_limits


# ============================================================
# CLIENT IP RESOLUTION (XFF via uvicorn proxy_headers)
# ============================================================


class _MockClient:
    def __init__(self, host):
        self.host = host


class _MockRequest:
    def __init__(self, host):
        self.client = _MockClient(host) if host is not None else None


def test_get_client_ip_returns_host():
    # With uvicorn proxy_headers=True, .client.host already reflects XFF
    assert _get_client_ip(_MockRequest("203.0.113.5")) == "203.0.113.5"


def test_get_client_ip_falls_back_on_missing_client():
    assert _get_client_ip(_MockRequest(None)) == "unknown"


def test_get_client_ip_falls_back_on_empty_host():
    assert _get_client_ip(_MockRequest("")) == "unknown"


# ============================================================
# SECURITY HEADERS MIDDLEWARE
# ============================================================


import asyncio
from starlette.responses import Response as _StarletteResponse
from server import security_headers_middleware, _CSP_POLICY, _HAS_SSL, _parse_allowed_origins


def _run_security_headers(status_code=200):
    async def call_next(_req):
        return _StarletteResponse("ok", status_code=status_code)
    return asyncio.run(security_headers_middleware(None, call_next))


def test_security_headers_sets_x_content_type_options():
    resp = _run_security_headers()
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


def test_security_headers_sets_x_frame_options_deny():
    resp = _run_security_headers()
    assert resp.headers["X-Frame-Options"] == "DENY"


def test_security_headers_sets_referrer_policy():
    resp = _run_security_headers()
    assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_security_headers_sets_permissions_policy():
    resp = _run_security_headers()
    perm = resp.headers["Permissions-Policy"]
    assert "camera=()" in perm
    assert "microphone=()" in perm
    assert "geolocation=()" in perm


def test_security_headers_sets_coop():
    resp = _run_security_headers()
    assert resp.headers["Cross-Origin-Opener-Policy"] == "same-origin"


def test_security_headers_sets_csp_report_only():
    resp = _run_security_headers()
    csp = resp.headers["Content-Security-Policy-Report-Only"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert "base-uri 'none'" in csp
    # No enforcing CSP header — still in report-only phase
    assert "Content-Security-Policy" not in resp.headers or \
           resp.headers.get("Content-Security-Policy") is None


def test_security_headers_csp_allows_image_cdn_hosts():
    # Avatars and contestant photos come from three different hosts.
    # Regression guard: if this assertion fails after a CSP edit,
    # avatars will break on fish-dash.com the moment CSP is flipped
    # from report-only to enforcing.
    #   - cdn.fishtank.live  — official contestant photos + some user avatars
    #   - fishtank.b-cdn.net — BunnyCDN pull-zone serving some user avatars
    #   - cdn2.mondomegabits.com — fallback avatar for contestants with no photo
    resp = _run_security_headers()
    csp = resp.headers["Content-Security-Policy-Report-Only"]
    # Extract the img-src directive specifically
    directives = {d.strip().split(" ", 1)[0]: d.strip() for d in csp.split(";") if d.strip()}
    assert "img-src" in directives, "img-src directive missing from CSP"
    img_src = directives["img-src"]
    assert "'self'" in img_src
    assert "data:" in img_src
    assert "https://cdn.fishtank.live" in img_src
    assert "https://fishtank.b-cdn.net" in img_src
    assert "https://cdn2.mondomegabits.com" in img_src


def test_security_headers_stamps_non_200_responses():
    # Headers must be present on rate-limit 429 and exception 500 responses.
    resp = _run_security_headers(status_code=429)
    assert resp.headers["X-Frame-Options"] == "DENY"
    resp = _run_security_headers(status_code=500)
    assert resp.headers["X-Frame-Options"] == "DENY"


def test_security_headers_hsts_only_with_ssl():
    resp = _run_security_headers()
    # Locally SSL_CERTFILE is unset → no HSTS (would poison browser cache)
    if _HAS_SSL:
        assert "max-age=31536000" in resp.headers["Strict-Transport-Security"]
    else:
        assert "Strict-Transport-Security" not in resp.headers


# ============================================================
# CORS ALLOWED ORIGINS (fail-closed parsing)
# ============================================================


def test_parse_allowed_origins_unset_is_empty():
    assert _parse_allowed_origins("") == []
    assert _parse_allowed_origins("   ") == []
    assert _parse_allowed_origins(None) == []


def test_parse_allowed_origins_single():
    assert _parse_allowed_origins("https://fish-dash.com") == ["https://fish-dash.com"]


def test_parse_allowed_origins_multiple():
    result = _parse_allowed_origins("https://fish-dash.com,https://www.fish-dash.com")
    assert result == ["https://fish-dash.com", "https://www.fish-dash.com"]


def test_parse_allowed_origins_strips_whitespace():
    result = _parse_allowed_origins("  https://a.com , https://b.com  ")
    assert result == ["https://a.com", "https://b.com"]


def test_parse_allowed_origins_drops_empty_entries():
    # Trailing comma, double comma, etc. should not yield "" entries
    assert _parse_allowed_origins("https://a.com,,,") == ["https://a.com"]
    assert _parse_allowed_origins(",,https://a.com,,") == ["https://a.com"]


def test_parse_allowed_origins_does_not_default_to_wildcard():
    # Regression guard: the old default was "*", which silently allowed
    # cross-origin requests from anywhere if the env var was unset.
    assert "*" not in _parse_allowed_origins("")
    assert _parse_allowed_origins("") == []


# ============================================================
# WEBSOCKET: ORIGIN CHECK + PER-IP CAP
# ============================================================


def test_is_origin_allowed_empty_rejected():
    # Browsers always send Origin on WS; missing → reject (CSWSH defense).
    assert _is_origin_allowed(None) is False
    assert _is_origin_allowed("") is False


def test_is_origin_allowed_matches_list(monkeypatch):
    monkeypatch.setattr(_server, "_allowed_origins", ["https://fish-dash.com"])
    assert _is_origin_allowed("https://fish-dash.com") is True
    assert _is_origin_allowed("https://evil.com") is False


def test_is_origin_allowed_wildcard_accepts_any(monkeypatch):
    monkeypatch.setattr(_server, "_allowed_origins", ["*"])
    assert _is_origin_allowed("https://fish-dash.com") is True
    assert _is_origin_allowed("https://evil.com") is True
    # Still reject missing Origin even with wildcard
    assert _is_origin_allowed("") is False


def test_is_origin_allowed_empty_allowlist_rejects_all(monkeypatch):
    monkeypatch.setattr(_server, "_allowed_origins", [])
    assert _is_origin_allowed("https://fish-dash.com") is False
    assert _is_origin_allowed("https://evil.com") is False


def test_ws_reserve_slot_succeeds_under_cap():
    ok, reason = _try_reserve_ws_slot("1.2.3.4")
    assert ok is True
    assert reason == "ok"
    assert _ws_ip_counts["1.2.3.4"] == 1


def test_ws_reserve_slot_per_ip_cap():
    for _ in range(MAX_WS_PER_IP):
        ok, _r = _try_reserve_ws_slot("5.6.7.8")
        assert ok is True
    # Fourth connection from same IP is rejected
    ok, reason = _try_reserve_ws_slot("5.6.7.8")
    assert ok is False
    assert reason == "per-ip"


def test_ws_reserve_slot_different_ips_independent():
    for _ in range(MAX_WS_PER_IP):
        _try_reserve_ws_slot("10.0.0.1")
    # Different IP should still be allowed
    ok, _r = _try_reserve_ws_slot("10.0.0.2")
    assert ok is True


def test_ws_release_slot_frees_capacity():
    for _ in range(MAX_WS_PER_IP):
        _try_reserve_ws_slot("20.0.0.1")
    _release_ws_slot("20.0.0.1")
    ok, _r = _try_reserve_ws_slot("20.0.0.1")
    assert ok is True


def test_ws_release_slot_removes_empty_ip():
    _try_reserve_ws_slot("30.0.0.1")
    _release_ws_slot("30.0.0.1")
    assert "30.0.0.1" not in _ws_ip_counts


def test_ws_reserve_slot_global_cap(monkeypatch):
    # Fill the global pool with fake clients
    class _FakeWS:
        pass
    for _ in range(MAX_WS_CLIENTS):
        browser_clients.add(_FakeWS())
    ok, reason = _try_reserve_ws_slot("40.0.0.1")
    assert ok is False
    assert reason == "global"


# ============================================================
# LOGGING: exception handler + access log middleware
# ============================================================

import logging as _logging
from server import generic_exception_handler, access_log_middleware, logger as _api_logger


class _MockURL:
    def __init__(self, path):
        self.path = path


class _MockLoggingRequest:
    def __init__(self, method="GET", path="/api/test", host="1.2.3.4"):
        self.method = method
        self.url = _MockURL(path)
        self.client = _MockClient(host) if host else None


def test_exception_handler_logs_and_returns_500(caplog):
    req = _MockLoggingRequest(method="GET", path="/api/boom")
    exc = ValueError("kaboom")
    with caplog.at_level(_logging.ERROR, logger="fishtank.api"):
        resp = asyncio.run(generic_exception_handler(req, exc))
    assert resp.status_code == 500
    # Generic body, not the exception message
    assert b"Internal server error" in resp.body
    # But the logger saw the full context
    assert "GET" in caplog.text
    assert "/api/boom" in caplog.text
    assert "kaboom" in caplog.text
    assert "ValueError" in caplog.text


def test_access_log_middleware_logs_request(caplog):
    req = _MockLoggingRequest(method="GET", path="/api/stats", host="9.9.9.9")

    async def call_next(_r):
        return _StarletteResponse("ok", status_code=200)

    with caplog.at_level(_logging.INFO, logger="fishtank.api"):
        resp = asyncio.run(access_log_middleware(req, call_next))

    assert resp.status_code == 200
    assert "GET" in caplog.text
    assert "/api/stats" in caplog.text
    assert "200" in caplog.text
    assert "9.9.9.9" in caplog.text


def test_access_log_middleware_logs_non_200(caplog):
    req = _MockLoggingRequest(method="POST", path="/api/bad", host="8.8.8.8")

    async def call_next(_r):
        return _StarletteResponse("rate limited", status_code=429)

    with caplog.at_level(_logging.INFO, logger="fishtank.api"):
        asyncio.run(access_log_middleware(req, call_next))

    assert "POST" in caplog.text
    assert "/api/bad" in caplog.text
    assert "429" in caplog.text


def test_access_log_middleware_logs_exception_then_reraises(caplog):
    req = _MockLoggingRequest(method="GET", path="/api/crash", host="7.7.7.7")

    async def call_next(_r):
        raise RuntimeError("boom")

    with caplog.at_level(_logging.INFO, logger="fishtank.api"):
        with pytest.raises(RuntimeError):
            asyncio.run(access_log_middleware(req, call_next))

    # Access line still emitted with status=500 before the raise propagates
    assert "/api/crash" in caplog.text
    assert "500" in caplog.text


# ============================================================
# DATABASE: read-only mode (PRAGMA query_only)
# ============================================================


def test_enable_readonly_blocks_inserts():
    # Baseline: write works
    database.store_event("chat:message", {"message": "before"})
    # Flip to read-only
    database.enable_readonly()
    # Subsequent INSERT must be refused by SQLite
    with pytest.raises(sqlite3.OperationalError) as exc_info:
        database.store_event("chat:message", {"message": "after"})
    msg = str(exc_info.value).lower()
    assert "readonly" in msg or "read-only" in msg or "query_only" in msg


def test_enable_readonly_allows_reads():
    database.store_event("chat:message", {"message": "hello"})
    database.enable_readonly()
    # Reads still work
    events = database.get_events(event_type="chat:message")
    assert len(events) == 1
    assert events[0]["data"]["message"] == "hello"


def test_disable_readonly_restores_writes():
    database.enable_readonly()
    with pytest.raises(sqlite3.OperationalError):
        database.store_event("chat:message", {"message": "blocked"})
    database.disable_readonly()
    # Writes work again after disabling
    db_id = database.store_event("chat:message", {"message": "ok"})
    assert db_id > 0


def test_enable_readonly_blocks_ddl():
    database.enable_readonly()
    conn = database._get_conn()
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("CREATE TABLE foo (id INTEGER)")


def test_enable_readonly_persists_across_get_conn_calls():
    database.enable_readonly()
    # Same thread, multiple _get_conn() calls — pragma must still apply
    for _ in range(3):
        conn = database._get_conn()
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO events (event_type, timestamp_local, data) VALUES (?, ?, ?)",
                     ("test", "2026-01-01T00:00:00+00:00", "{}"))


# ============================================================
# DATABASE: store_stock_snapshot / get_stock_history / count
# ============================================================


def _make_stock(ticker, price, today=100, last_hour=100, last_week=100, avg=100):
    return {"tickerSymbol": ticker, "currentPrice": price, "today": today,
            "lastHour": last_hour, "lastWeek": last_week, "averagePrice": avg}


def test_store_stock_snapshot():
    database.store_stock_snapshot([_make_stock("AAA", 200), _make_stock("BBB", 300)])
    history = database.get_stock_history()
    assert len(history) == 2
    tickers = {h["ticker"] for h in history}
    assert tickers == {"AAA", "BBB"}


def test_store_stock_snapshot_empty():
    database.store_stock_snapshot([])
    assert database.get_stock_snapshot_count() == 0


def test_get_stock_history_no_filter():
    database.store_stock_snapshot([_make_stock("AAA", 100)])
    database.store_stock_snapshot([_make_stock("AAA", 110)])
    history = database.get_stock_history()
    assert len(history) == 2


def test_get_stock_history_filter_by_ticker():
    database.store_stock_snapshot([_make_stock("AAA", 100), _make_stock("BBB", 200)])
    history = database.get_stock_history(ticker="AAA")
    assert len(history) == 1
    assert history[0]["ticker"] == "AAA"


def test_get_stock_history_filter_by_since():
    database.store_stock_snapshot([_make_stock("AAA", 100)])
    # Future timestamp should exclude everything
    history = database.get_stock_history(since="2099-01-01T00:00:00Z")
    assert len(history) == 0
    # Past timestamp should include everything
    history = database.get_stock_history(since="2000-01-01T00:00:00Z")
    assert len(history) == 1


def test_get_stock_history_combined_filters():
    database.store_stock_snapshot([_make_stock("AAA", 100), _make_stock("BBB", 200)])
    history = database.get_stock_history(ticker="AAA", since="2000-01-01T00:00:00Z")
    assert len(history) == 1
    assert history[0]["ticker"] == "AAA"


def test_get_stock_history_limit():
    for _ in range(5):
        database.store_stock_snapshot([_make_stock("AAA", 100)])
    history = database.get_stock_history(limit=3)
    assert len(history) == 3


def test_get_stock_history_ordered_desc():
    database.store_stock_snapshot([_make_stock("AAA", 100)])
    database.store_stock_snapshot([_make_stock("AAA", 200)])
    history = database.get_stock_history()
    # Most recent first
    assert history[0]["price"] == 200
    assert history[1]["price"] == 100


def test_get_stock_snapshot_count():
    assert database.get_stock_snapshot_count() == 0
    database.store_stock_snapshot([_make_stock("AAA", 100), _make_stock("BBB", 200)])
    assert database.get_stock_snapshot_count() == 2


# ============================================================
# DATABASE: get_tts_sfx_analytics
# ============================================================


def test_tts_sfx_analytics_empty():
    result = database.get_tts_sfx_analytics()
    assert result["top_rooms"] == []
    assert result["top_tts_senders"] == []
    assert result["top_sfx_senders"] == []


def test_tts_sfx_analytics_counts():
    database.store_event("tts:update", {"displayName": "User1", "room": "room-1", "cost": 248, "message": "hi"})
    database.store_event("tts:update", {"displayName": "User1", "room": "room-1", "cost": 248, "message": "hi2"})
    database.store_event("sfx:update", {"displayName": "User2", "room": "room-2", "cost": 100, "message": "sfx"})
    result = database.get_tts_sfx_analytics()
    assert len(result["top_tts_senders"]) >= 1
    assert result["top_tts_senders"][0]["name"] == "User1"
    assert result["top_tts_senders"][0]["count"] == 2
    assert len(result["top_sfx_senders"]) >= 1
    assert result["top_sfx_senders"][0]["name"] == "User2"


def test_tts_sfx_analytics_rooms():
    database.store_event("tts:update", {"displayName": "U", "room": "room-1", "message": "a"})
    database.store_event("sfx:update", {"displayName": "U", "room": "room-1", "message": "b"})
    database.store_event("tts:update", {"displayName": "U", "room": "room-2", "message": "c"})
    result = database.get_tts_sfx_analytics()
    assert len(result["top_rooms"]) == 2
    # room-1 has 2 events, room-2 has 1
    assert result["top_rooms"][0]["room"] == "room-1"
    assert result["top_rooms"][0]["count"] == 2


def test_tts_sfx_analytics_with_since():
    database.store_event("tts:update", {"displayName": "U", "room": "r", "cost": 100, "message": "hi"})
    result = database.get_tts_sfx_analytics(since="2099-01-01T00:00:00Z")
    assert result["top_tts_senders"] == []


# ============================================================
# DATABASE: get_chat_analytics
# ============================================================


def test_chat_analytics_empty():
    result = database.get_chat_analytics()
    assert result["total"] == 0
    assert result["top_chatters"] == []


def test_chat_analytics_counts():
    database.store_event("chat:message", {"user": {"displayName": "Alice"}, "message": "hi"})
    database.store_event("chat:message", {"user": {"displayName": "Alice"}, "message": "hello"})
    database.store_event("chat:message", {"user": {"displayName": "Bob"}, "message": "hey"})
    result = database.get_chat_analytics()
    assert result["total"] == 3
    assert result["top_chatters"][0]["name"] == "Alice"
    assert result["top_chatters"][0]["count"] == 2


def test_chat_analytics_with_since():
    database.store_event("chat:message", {"user": {"displayName": "Alice"}, "message": "hi"})
    result = database.get_chat_analytics(since="2099-01-01T00:00:00Z")
    assert result["total"] == 0


# ============================================================
# DATABASE: get_peak_hours
# ============================================================


def test_peak_hours_empty():
    result = database.get_peak_hours()
    assert result["hourly"] == []
    assert result["peak"] == []
    assert result["quietest"] == []


def test_peak_hours_counts():
    # Insert events that will group by hour
    database.store_event("tts:update", {"displayName": "U", "message": "a"})
    database.store_event("sfx:update", {"displayName": "U", "message": "b"})
    database.store_event("fishtoy:used", {"displayName": "U", "target": "T", "cost": 100})
    result = database.get_peak_hours()
    assert len(result["hourly"]) >= 1
    # All in same hour
    hour = result["hourly"][0]
    assert hour["tts"] >= 1
    assert hour["sfx"] >= 1
    assert hour["fishtoys"] >= 1
    assert hour["total"] == hour["tts"] + hour["sfx"] + hour["fishtoys"]


def test_peak_hours_excludes_chat():
    database.store_event("chat:message", {"user": {"displayName": "U"}, "message": "hi"})
    result = database.get_peak_hours()
    # Chat should not appear in peak hours
    assert result["hourly"] == []


# ============================================================
# DATABASE: get_hidden_content
# ============================================================


def test_hidden_content_empty():
    result = database.get_hidden_content()
    assert result == []


def test_hidden_content_returns_metadata():
    database.store_event("fishtoy:used", {"displayName": "U", "target": "T", "metadata": "love letter text"})
    database.store_event("fishtoy:used", {"displayName": "U", "target": "T"})  # no metadata
    result = database.get_hidden_content()
    assert len(result) == 1
    assert result[0]["data"]["metadata"] == "love letter text"


def test_hidden_content_filter_by_target():
    database.store_event("fishtoy:used", {"displayName": "U", "target": "Alice", "metadata": "msg1"})
    database.store_event("fishtoy:used", {"displayName": "U", "target": "Bob", "metadata": "msg2"})
    result = database.get_hidden_content(target="Alice")
    assert len(result) == 1
    assert result[0]["data"]["target"] == "Alice"


def test_hidden_content_search():
    database.store_event("fishtoy:used", {"displayName": "U", "target": "T", "metadata": "I love pizza"})
    database.store_event("fishtoy:used", {"displayName": "U", "target": "T", "metadata": "hello world"})
    result = database.get_hidden_content(search="pizza")
    assert len(result) == 1


def test_hidden_content_excludes_null_metadata():
    database.store_event("fishtoy:used", {"displayName": "U", "target": "T", "metadata": "null"})
    result = database.get_hidden_content()
    assert len(result) == 0


# ============================================================
# DATABASE: get_fishtoys
# ============================================================


def test_get_fishtoys_basic():
    database.store_event("fishtoy:used", {"displayName": "U", "target": "T", "cost": 100, "itemId": 1})
    result = database.get_fishtoys()
    assert len(result) == 1


def test_get_fishtoys_filter_by_target():
    database.store_event("fishtoy:used", {"displayName": "U", "target": "Alice", "cost": 100})
    database.store_event("fishtoy:used", {"displayName": "U", "target": "Bob", "cost": 200})
    result = database.get_fishtoys(target="Alice")
    assert len(result) == 1
    assert result[0]["data"]["target"] == "Alice"


def test_get_fishtoys_filter_by_item_id():
    # itemId stored as string in real fishtank data after JSON serialization
    database.store_event("fishtoy:used", {"displayName": "U", "target": "T", "itemId": "42"})
    database.store_event("fishtoy:used", {"displayName": "U", "target": "T", "itemId": "99"})
    result = database.get_fishtoys(item_id="42")
    assert len(result) == 1


def test_get_fishtoys_search():
    database.store_event("fishtoy:used", {"displayName": "Sender1", "target": "T", "metadata": "secret message"})
    database.store_event("fishtoy:used", {"displayName": "Sender2", "target": "T"})
    result = database.get_fishtoys(search="secret")
    assert len(result) == 1


def test_get_fishtoys_pagination_offset():
    for i in range(5):
        database.store_event("fishtoy:used", {"displayName": f"U{i}", "target": "T", "cost": 100})
    page1 = database.get_fishtoys(limit=2, offset=0)
    assert len(page1) == 2
    page2 = database.get_fishtoys(limit=2, offset=2)
    assert len(page2) == 2
    # Pages must not overlap
    page1_ids = {e["id"] for e in page1}
    page2_ids = {e["id"] for e in page2}
    assert page1_ids.isdisjoint(page2_ids)


def test_get_fishtoys_pagination_before_id():
    for i in range(5):
        database.store_event("fishtoy:used", {"displayName": f"U{i}", "target": "T", "cost": 100})
    page1 = database.get_fishtoys(limit=2)
    assert len(page1) == 2
    # Keyset: next page starts before the last id of page1
    last_id = page1[-1]["id"]
    page2 = database.get_fishtoys(limit=2, before_id=last_id)
    assert len(page2) == 2
    # All page2 ids must be less than the cursor
    assert all(e["id"] < last_id for e in page2)
    # No overlap
    page1_ids = {e["id"] for e in page1}
    page2_ids = {e["id"] for e in page2}
    assert page1_ids.isdisjoint(page2_ids)


# ============================================================
# DATABASE: get_latest_feature_toggles
# ============================================================


def test_feature_toggles_empty():
    result = database.get_latest_feature_toggles()
    assert result == {}


def test_feature_toggles_returns_latest():
    database.store_event("feature-toggles:update", {"feature": "tts", "enabled": True, "metadata": "248"})
    database.store_event("feature-toggles:update", {"feature": "tts", "enabled": False, "metadata": "0"})
    result = database.get_latest_feature_toggles()
    assert "tts" in result
    # Should be the latest state (disabled)
    assert result["tts"]["enabled"] is False


def test_feature_toggles_multiple_features():
    database.store_event("feature-toggles:update", {"feature": "tts", "enabled": True})
    database.store_event("feature-toggles:update", {"feature": "sfx", "enabled": False})
    result = database.get_latest_feature_toggles()
    assert len(result) == 2
    assert result["tts"]["enabled"] is True
    assert result["sfx"]["enabled"] is False


# ============================================================
# DATABASE: get_known_fishtoy_ids
# ============================================================


def test_known_fishtoy_ids_empty():
    result = database.get_known_fishtoy_ids()
    assert result == set()


def test_known_fishtoy_ids_returns_set():
    database.store_event("fishtoy:used", {"id": "abc123", "displayName": "U", "target": "T"})
    database.store_event("fishtoy:used", {"id": "def456", "displayName": "U", "target": "T"})
    result = database.get_known_fishtoy_ids()
    assert isinstance(result, set)
    assert "abc123" in result
    assert "def456" in result


def test_known_fishtoy_ids_excludes_non_fishtoy():
    database.store_event("fishtoy:used", {"id": "fish1", "displayName": "U", "target": "T"})
    database.store_event("chat:message", {"id": "chat1", "user": {"displayName": "U"}, "message": "hi"})
    result = database.get_known_fishtoy_ids()
    assert "fish1" in result
    assert "chat1" not in result


# ============================================================
# DATABASE: get_notifications / get_price_changes
# ============================================================


def test_get_notifications():
    database.store_event("notification:global", {"message": "Director says hello"})
    database.store_event("announcement", {"message": "System announcement"})
    result = database.get_notifications()
    assert len(result) == 2


def test_get_notifications_limit():
    for i in range(5):
        database.store_event("notification:global", {"message": f"msg {i}"})
    result = database.get_notifications(limit=3)
    assert len(result) == 3


def test_get_price_changes():
    database.store_event("tts:price", {"price": 248})
    database.store_event("sfx:price", {"price": 100})
    result = database.get_price_changes()
    assert len(result) == 2


def test_get_price_changes_limit():
    for i in range(5):
        database.store_event("tts:price", {"price": i * 100})
    result = database.get_price_changes(limit=2)
    assert len(result) == 2


# ============================================================
# SENTIMENT: _score_sentiment
# ============================================================


def test_score_sentiment_positive():
    score = _score_sentiment("This is amazing and wonderful!")
    assert score > 0


def test_score_sentiment_negative():
    score = _score_sentiment("This is terrible and awful!")
    assert score < 0


def test_score_sentiment_empty():
    assert _score_sentiment("") == 0.0
    assert _score_sentiment(None) == 0.0


def test_score_sentiment_non_string():
    assert _score_sentiment(123) == 0.0
    assert _score_sentiment(["list"]) == 0.0


# ============================================================
# DATABASE: get_sentiment_analytics
# ============================================================


def test_chat_sentiment_empty():
    result = database.get_chat_sentiment()
    assert result["hourly"] == []
    assert result["overall"]["avg"] == 0
    assert result["label"] == "Neutral"


def test_chat_sentiment_overall():
    database.store_event("chat:message", {"user": {"displayName": "U"}, "message": "great", "sentiment": 0.8})
    database.store_event("chat:message", {"user": {"displayName": "U"}, "message": "ok", "sentiment": 0.0})
    database.store_event("chat:message", {"user": {"displayName": "U"}, "message": "bad", "sentiment": -0.6})
    result = database.get_chat_sentiment()
    assert result["overall"]["positive_pct"] > 0
    assert result["overall"]["neutral_pct"] > 0
    assert result["overall"]["negative_pct"] > 0
    total_pct = result["overall"]["positive_pct"] + result["overall"]["neutral_pct"] + result["overall"]["negative_pct"]
    assert 99.5 <= total_pct <= 100.5
    assert "by_target" not in result


def test_chat_sentiment_hourly():
    database.store_event("chat:message", {"user": {"displayName": "U"}, "message": "hi", "sentiment": 0.5})
    result = database.get_chat_sentiment()
    assert len(result["hourly"]) >= 1
    assert result["hourly"][0]["message_count"] == 1
    assert result["hourly"][0]["avg_sentiment"] == 0.5


def test_chat_sentiment_excludes_tts():
    database.store_event("chat:message", {"user": {"displayName": "U"}, "message": "hello", "sentiment": 0.5})
    database.store_event("tts:update", {"displayName": "U", "target": "T", "message": "tts", "sentiment": -0.5})
    result = database.get_chat_sentiment()
    assert result["overall"]["avg"] == 0.5
    assert result["hourly"][0]["message_count"] == 1


def test_chat_sentiment_with_since():
    database.store_event("chat:message", {"user": {"displayName": "U"}, "message": "hi", "sentiment": 0.5})
    result = database.get_chat_sentiment(since="2099-01-01T00:00:00Z")
    assert result["hourly"] == []
    assert result["overall"]["avg"] == 0


def test_chat_sentiment_excludes_no_sentiment():
    database.store_event("chat:message", {"user": {"displayName": "U"}, "message": "old"})
    database.store_event("chat:message", {"user": {"displayName": "U"}, "message": "new", "sentiment": 0.3})
    result = database.get_chat_sentiment()
    assert result["overall"]["avg"] == 0.3
    assert result["hourly"][0]["message_count"] == 1


def test_chat_sentiment_label():
    database.store_event("chat:message", {"user": {"displayName": "U"}, "message": "amazing", "sentiment": 0.6})
    result = database.get_chat_sentiment()
    assert result["label"] == "Excited"


def test_tts_sentiment_empty():
    result = database.get_tts_sentiment()
    assert result["hourly"] == []
    assert result["by_target"] == []
    assert result["label"] == "Neutral"


def test_tts_sentiment_by_target():
    database.store_event("tts:update", {"displayName": "U", "target": "Alice", "message": "love", "sentiment": 0.9})
    database.store_event("tts:update", {"displayName": "U", "target": "Bob", "message": "hate", "sentiment": -0.8})
    result = database.get_tts_sentiment()
    assert len(result["by_target"]) == 2
    assert result["by_target"][0]["target"] == "Alice"
    assert result["by_target"][0]["avg_sentiment"] > 0
    assert result["by_target"][1]["target"] == "Bob"
    assert result["by_target"][1]["avg_sentiment"] < 0


def test_tts_sentiment_excludes_chat():
    database.store_event("tts:update", {"displayName": "U", "target": "T", "message": "tts", "sentiment": -0.5})
    database.store_event("chat:message", {"user": {"displayName": "U"}, "message": "chat", "sentiment": 0.5})
    result = database.get_tts_sentiment()
    assert result["overall"]["avg"] == -0.5
    assert result["hourly"][0]["message_count"] == 1


def test_tts_sentiment_hourly():
    database.store_event("tts:update", {"displayName": "U", "target": "T", "message": "hi", "sentiment": 0.3})
    result = database.get_tts_sentiment()
    assert len(result["hourly"]) >= 1
    assert result["hourly"][0]["avg_sentiment"] == 0.3


# ============================================================
# SHARED STATE: atomic read/write with mtime caching
# ============================================================

import shared_state


@pytest.fixture(autouse=False)
def reset_shared_state_cache():
    """Reset module-level cache between shared_state tests."""
    shared_state._cached_state = {}
    shared_state._cached_mtime = 0.0
    yield
    shared_state._cached_state = {}
    shared_state._cached_mtime = 0.0


def test_shared_state_round_trip(tmp_path, reset_shared_state_cache):
    path = str(tmp_path / "state.json")
    data = {"updated_at": "2026-04-09T00:00:00Z", "stocks": [1, 2, 3], "item_catalog": {"a": 1}}
    shared_state.write_state(path, data)
    result = shared_state.read_state(path)
    assert result == data


def test_shared_state_missing_file_returns_empty(tmp_path, reset_shared_state_cache):
    path = str(tmp_path / "nonexistent.json")
    result = shared_state.read_state(path)
    assert result == {}


def test_shared_state_mtime_cache_hit(tmp_path, reset_shared_state_cache):
    path = str(tmp_path / "state.json")
    shared_state.write_state(path, {"v": 1})
    shared_state.read_state(path)
    mtime_after_first = shared_state._cached_mtime
    # Second read without file change should reuse cache
    result = shared_state.read_state(path)
    assert result == {"v": 1}
    assert shared_state._cached_mtime == mtime_after_first


def test_shared_state_cache_invalidates_on_write(tmp_path, reset_shared_state_cache):
    path = str(tmp_path / "state.json")
    shared_state.write_state(path, {"v": 1})
    assert shared_state.read_state(path) == {"v": 1}
    shared_state.write_state(path, {"v": 2})
    assert shared_state.read_state(path) == {"v": 2}


def test_shared_state_atomic_write_no_corrupt(tmp_path, reset_shared_state_cache):
    """If os.replace fails, the original file stays intact."""
    path = str(tmp_path / "state.json")
    shared_state.write_state(path, {"original": True})
    # Simulate os.replace failure mid-write
    from unittest.mock import patch
    with patch("shared_state.os.replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            shared_state.write_state(path, {"corrupted": True})
    # Original file untouched
    result = shared_state.read_state(path)
    assert result == {"original": True}


# ============================================================
# NOTIFY TABLE: inter-process event notification
# ============================================================


def test_notify_new_event_and_poll():
    database.notify_new_event(42, "chat:message")
    rows = database.poll_notify(0)
    assert len(rows) == 1
    assert rows[0]["event_id"] == 42
    assert rows[0]["event_type"] == "chat:message"


def test_poll_notify_filters_by_last_seen_id():
    database.notify_new_event(1, "chat:message")
    database.notify_new_event(2, "tts:update")
    database.notify_new_event(3, "sfx:update")
    all_rows = database.poll_notify(0)
    second_id = all_rows[1]["id"]
    rows = database.poll_notify(second_id)
    assert len(rows) == 1
    assert rows[0]["event_id"] == 3


def test_poll_notify_empty_when_no_new_rows():
    rows = database.poll_notify(0)
    assert rows == []


def test_prune_notify_deletes_old_rows():
    database.notify_new_event(1, "chat:message")
    # Backdate the row to 2 minutes ago
    conn = database._get_conn()
    conn.execute(
        "UPDATE _notify SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-120 seconds')"
    )
    conn.commit()
    database.prune_notify()
    assert database.poll_notify(0) == []


def test_prune_notify_keeps_recent_rows():
    database.notify_new_event(1, "chat:message")
    database.prune_notify()
    rows = database.poll_notify(0)
    assert len(rows) == 1


# ============================================================
# TOKENIZER
# ============================================================

from tokenizer import tokenize, count_tokens, STOPWORDS


def test_tokenize_basic():
    result = tokenize("drake is causing drama in the house")
    assert "drake" in result
    assert "drama" in result
    assert "house" in result


def test_tokenize_stopwords_filtered():
    result = tokenize("the and that have for not with you this")
    assert result == []


def test_tokenize_chat_noise_filtered():
    result = tokenize("lol lmao bro bruh dude yeah omg pog poggers")
    assert result == []


def test_tokenize_bot_commands_filtered():
    result = tokenize("coinflip double lexxpoints")
    assert result == []


def test_tokenize_platform_noise_filtered():
    """Platform-specific noise words are stopword-filtered."""
    tokens = tokenize("clip this tts sfx tip used for real")
    assert "clip" not in tokens
    assert "tts" not in tokens
    assert "sfx" not in tokens
    assert "tip" not in tokens
    assert "used" not in tokens
    assert "real" in tokens


def test_tokenize_short_words_filtered():
    result = tokenize("I am ok no hi go")
    assert result == []


def test_tokenize_empty():
    assert tokenize("") == []
    assert tokenize(None) == []
    assert tokenize(123) == []


def test_tokenize_urls_stripped():
    result = tokenize("check out https://example.com/page for more info")
    assert "https" not in result
    assert "com" not in result
    assert "example" in result
    assert "page" in result


def test_tokenize_mixed_case():
    result = tokenize("DRAKE is FIGHTING with LAURA")
    assert "drake" in result
    assert "fighting" in result
    assert "laura" in result
    assert all(w == w.lower() for w in result)


def test_tokenize_numbers_stripped():
    result = tokenize("there are 500 tokens and 2 contestants")
    assert "tokens" in result
    assert "contestants" in result
    assert "500" not in result


def test_tokenize_preserves_duplicates():
    result = tokenize("drama drama drama")
    assert result.count("drama") == 3


def test_count_tokens_tuples():
    texts = [("drake is cool",), ("drake fights laura",)]
    counts = count_tokens(texts)
    assert counts["drake"] == 2
    assert counts["cool"] == 1
    assert counts["fights"] == 1
    assert counts["laura"] == 1


def test_count_tokens_strings():
    texts = ["drake is cool", "drake fights laura"]
    counts = count_tokens(texts)
    assert counts["drake"] == 2


def test_count_tokens_empty():
    counts = count_tokens([])
    assert len(counts) == 0


def test_count_tokens_filters_stopwords():
    texts = [("the and this for lol",)]
    counts = count_tokens(texts)
    assert len(counts) == 0


# ============================================================
# DATABASE: _extract_columns - message_text
# ============================================================


def test_extract_message_text_chat():
    ext = database._extract_columns("chat:message", {
        "user": {"displayName": "Alice"}, "message": "hello world"
    })
    assert ext["message_text"] == "hello world"


def test_extract_message_text_non_chat():
    ext = database._extract_columns("tts:update", {
        "displayName": "Alice", "message": "hello", "cost": 248
    })
    assert ext["message_text"] is None


def test_extract_message_text_missing_message():
    ext = database._extract_columns("chat:message", {
        "user": {"displayName": "Alice"}
    })
    assert ext["message_text"] is None


def test_store_event_populates_message_text():
    database.store_event("chat:message", {"user": {"displayName": "Alice"}, "message": "test msg"})
    conn = database._get_conn()
    row = conn.execute("SELECT message_text FROM events WHERE event_type = 'chat:message'").fetchone()
    assert row["message_text"] == "test msg"


# ============================================================
# DATABASE: keyword_counts (upsert, query, prune)
# ============================================================


def test_upsert_keyword_counts_insert():
    database.upsert_keyword_counts([
        ("2026-04-12T15", "drama", 10),
        ("2026-04-12T15", "fight", 5),
    ])
    results = database.get_keyword_top(since="2026-04-12T15", limit=10)
    assert len(results) == 2
    assert results[0]["word"] == "drama"
    assert results[0]["count"] == 10
    assert results[1]["word"] == "fight"
    assert results[1]["count"] == 5


def test_upsert_keyword_counts_increment():
    database.upsert_keyword_counts([("2026-04-12T15", "drama", 10)])
    database.upsert_keyword_counts([("2026-04-12T15", "drama", 7)])
    results = database.get_keyword_top(since="2026-04-12T15", limit=10)
    assert results[0]["word"] == "drama"
    assert results[0]["count"] == 17


def test_upsert_keyword_counts_empty():
    database.upsert_keyword_counts([])
    results = database.get_keyword_top(since="2000-01-01T00", limit=10)
    assert results == []


def test_get_keyword_top_respects_since():
    database.upsert_keyword_counts([
        ("2026-04-11T10", "old", 100),
        ("2026-04-12T15", "new", 50),
    ])
    results = database.get_keyword_top(since="2026-04-12T00:00:00Z", limit=10)
    words = [r["word"] for r in results]
    assert "new" in words
    assert "old" not in words


def test_get_keyword_top_default_limit():
    for i in range(30):
        database.upsert_keyword_counts([("2026-04-12T15", f"word{i:02d}", 30 - i)])
    results = database.get_keyword_top(since="2026-04-12T00:00:00Z")
    assert len(results) == 20


def test_get_keyword_top_sums_across_buckets():
    database.upsert_keyword_counts([
        ("2026-04-12T14", "drama", 20),
        ("2026-04-12T15", "drama", 30),
        ("2026-04-12T16", "drama", 10),
    ])
    results = database.get_keyword_top(since="2026-04-12T14:00:00Z", limit=10)
    assert results[0]["word"] == "drama"
    assert results[0]["count"] == 60


def test_get_keyword_analytics_top_and_hourly():
    database.upsert_keyword_counts([
        ("2026-04-12T14", "drama", 20),
        ("2026-04-12T14", "fight", 10),
        ("2026-04-12T15", "drama", 30),
        ("2026-04-12T15", "laura", 25),
    ])
    result = database.get_keyword_analytics(since="2026-04-12T14:00:00Z")
    top_words = [k["word"] for k in result["top_keywords"]]
    assert top_words[0] == "drama"
    assert len(result["hourly"]) == 2
    assert result["hourly"][0]["bucket"] == "2026-04-12T14"
    assert result["hourly"][1]["bucket"] == "2026-04-12T15"


def test_get_keyword_analytics_with_until():
    database.upsert_keyword_counts([
        ("2026-04-12T14", "drama", 20),
        ("2026-04-12T16", "fight", 10),
    ])
    result = database.get_keyword_analytics(since="2026-04-12T14:00:00Z", until="2026-04-12T15:00:00Z")
    top_words = [k["word"] for k in result["top_keywords"]]
    assert "drama" in top_words
    assert "fight" not in top_words


def test_get_keyword_analytics_hourly_top_10_cap():
    rows = [("2026-04-12T14", f"word{i:02d}", 15 - i) for i in range(15)]
    database.upsert_keyword_counts(rows)
    result = database.get_keyword_analytics(since="2026-04-12T14:00:00Z")
    assert len(result["hourly"]) == 1
    assert len(result["hourly"][0]["top"]) == 10


def test_prune_keyword_counts():
    database.upsert_keyword_counts([
        ("2025-01-01T00", "old", 100),
        ("2026-04-12T15", "new", 50),
    ])
    deleted = database.prune_keyword_counts(retention_days=31)
    assert deleted == 1
    results = database.get_keyword_top(since="2000-01-01T00", limit=10)
    assert len(results) == 1
    assert results[0]["word"] == "new"


def test_prune_keyword_counts_keeps_recent():
    database.upsert_keyword_counts([("2026-04-12T15", "recent", 10)])
    deleted = database.prune_keyword_counts(retention_days=31)
    assert deleted == 0


# ============================================================
# DATABASE: _kv (key-value store)
# ============================================================


def test_kv_get_set():
    assert database.get_kv("test_key") is None
    database.set_kv("test_key", "test_value")
    assert database.get_kv("test_key") == "test_value"


def test_kv_upsert():
    database.set_kv("key", "value1")
    database.set_kv("key", "value2")
    assert database.get_kv("key") == "value2"


def test_kv_stores_as_string():
    database.set_kv("num", 42)
    assert database.get_kv("num") == "42"


# ============================================================
# DATABASE: backfill_message_text
# ============================================================


def test_backfill_message_text():
    conn = database._get_conn()
    conn.execute(
        """INSERT INTO events (event_type, timestamp_local, data, message_text)
           VALUES ('chat:message', '2026-04-12T00:00:00+00:00', ?, NULL)""",
        (json.dumps({"user": {"displayName": "Alice"}, "message": "hello world"}),),
    )
    conn.execute(
        """INSERT INTO events (event_type, timestamp_local, data, message_text)
           VALUES ('chat:message', '2026-04-12T00:01:00+00:00', ?, NULL)""",
        (json.dumps({"user": {"displayName": "Bob"}, "message": "goodbye"}),),
    )
    conn.commit()
    total = database.backfill_message_text(batch_size=10)
    assert total == 2
    rows = conn.execute("SELECT message_text FROM events ORDER BY id").fetchall()
    assert rows[0]["message_text"] == "hello world"
    assert rows[1]["message_text"] == "goodbye"


def test_backfill_message_text_skips_already_populated():
    database.store_event("chat:message", {"user": {"displayName": "Alice"}, "message": "already here"})
    total = database.backfill_message_text()
    assert total == 0


# ============================================================
# Phase 2: Ingestion integration tests
# ============================================================

from datetime import datetime, timezone, timedelta


def test_update_keyword_buffer_adds_entries():
    """_update_keyword_buffer adds tokenized words to the buffer."""
    from ingest import _update_keyword_buffer, _keyword_buffer, _KEYWORD_BUFFER_LOCK
    with _KEYWORD_BUFFER_LOCK:
        _keyword_buffer.clear()
    _update_keyword_buffer("drama fight eviction")
    with _KEYWORD_BUFFER_LOCK:
        assert len(_keyword_buffer) == 1
        ts, words = _keyword_buffer[0]
        assert "drama" in words
        assert "fight" in words
        assert "eviction" in words
    # Cleanup
    with _KEYWORD_BUFFER_LOCK:
        _keyword_buffer.clear()


def test_update_keyword_buffer_ignores_empty():
    """_update_keyword_buffer does nothing for empty/None input."""
    from ingest import _update_keyword_buffer, _keyword_buffer, _KEYWORD_BUFFER_LOCK
    with _KEYWORD_BUFFER_LOCK:
        _keyword_buffer.clear()
    _update_keyword_buffer(None)
    _update_keyword_buffer("")
    _update_keyword_buffer("the and but")  # all stopwords
    with _KEYWORD_BUFFER_LOCK:
        assert len(_keyword_buffer) == 0
    # Cleanup
    with _KEYWORD_BUFFER_LOCK:
        _keyword_buffer.clear()


def test_update_keyword_buffer_prunes_old_entries():
    """_update_keyword_buffer prunes entries older than the window."""
    import time
    from ingest import (
        _update_keyword_buffer, _keyword_buffer,
        _KEYWORD_BUFFER_LOCK, _KEYWORD_BUFFER_WINDOW
    )
    with _KEYWORD_BUFFER_LOCK:
        _keyword_buffer.clear()
        # Manually insert an old entry (6 minutes ago)
        old_ts = time.time() - _KEYWORD_BUFFER_WINDOW - 60
        _keyword_buffer.append((old_ts, ["stale"]))
    # Adding a new entry should prune the old one
    _update_keyword_buffer("fresh content here")
    with _KEYWORD_BUFFER_LOCK:
        assert len(_keyword_buffer) == 1
        _, words = _keyword_buffer[0]
        assert "stale" not in words
        assert "fresh" in words
    # Cleanup
    with _KEYWORD_BUFFER_LOCK:
        _keyword_buffer.clear()


def test_compute_trending_keywords_basic():
    """_compute_trending_keywords returns top keywords sorted by count."""
    import time
    from ingest import (
        _compute_trending_keywords, _keyword_buffer, _KEYWORD_BUFFER_LOCK
    )
    with _KEYWORD_BUFFER_LOCK:
        _keyword_buffer.clear()
        now = time.time()
        # Add multiple entries with known word frequencies
        _keyword_buffer.append((now, ["drama", "fight", "drama"]))
        _keyword_buffer.append((now, ["drama", "eviction"]))
        _keyword_buffer.append((now, ["fight"]))
    result = _compute_trending_keywords(top_n=3)
    assert len(result) == 3
    assert result[0]["word"] == "drama"
    assert result[0]["count"] == 3
    assert result[1]["word"] == "fight"
    assert result[1]["count"] == 2
    # Cleanup
    with _KEYWORD_BUFFER_LOCK:
        _keyword_buffer.clear()


def test_compute_trending_keywords_empty_buffer():
    """_compute_trending_keywords returns empty list when buffer is empty."""
    from ingest import _compute_trending_keywords, _keyword_buffer, _KEYWORD_BUFFER_LOCK
    with _KEYWORD_BUFFER_LOCK:
        _keyword_buffer.clear()
    result = _compute_trending_keywords()
    assert result == []


def test_compute_trending_keywords_respects_top_n():
    """_compute_trending_keywords limits results to top_n."""
    import time
    from ingest import (
        _compute_trending_keywords, _keyword_buffer, _KEYWORD_BUFFER_LOCK
    )
    with _KEYWORD_BUFFER_LOCK:
        _keyword_buffer.clear()
        now = time.time()
        # Add many distinct words
        _keyword_buffer.append((now, ["alpha", "beta", "gamma", "delta", "epsilon"]))
    result = _compute_trending_keywords(top_n=2)
    assert len(result) == 2


def test_keyword_agg_processes_messages(tmp_path, monkeypatch):
    """keyword_agg_thread processes chat messages and upserts keyword counts."""
    import database as db
    # Use in-memory DB
    monkeypatch.setattr(db, "DB_PATH", ":memory:")
    # Reset thread-local connection
    if hasattr(db._local, "conn"):
        db._local.conn = None
    db.init_db()

    # Insert some chat messages with message_text
    conn = db._get_conn()
    now = datetime.now(timezone.utc)
    for i, msg in enumerate(["drama fight", "drama eviction", "fight drama"]):
        ts = (now - timedelta(minutes=i)).isoformat()
        conn.execute(
            "INSERT INTO events (event_type, data, timestamp_local, message_text) VALUES (?, ?, ?, ?)",
            ("chat:message", '{"message": "' + msg + '"}', ts, msg)
        )
    conn.commit()

    # Set checkpoint to 0 so all messages get processed
    db.set_kv("keyword_agg_last_id", "0")

    # Run the aggregation logic manually (extracted from keyword_agg_thread)
    from tokenizer import tokenize
    from collections import defaultdict, Counter

    last_id = int(db.get_kv("keyword_agg_last_id"))
    rows = conn.execute("""
        SELECT id, message_text, timestamp_local FROM events
        WHERE event_type = 'chat:message'
          AND id > ?
          AND message_text IS NOT NULL
        ORDER BY id
        LIMIT 50000
    """, (last_id,)).fetchall()

    bucket_counts = defaultdict(Counter)
    for row in rows:
        words = tokenize(row["message_text"])
        if words:
            bucket = row["timestamp_local"][:13]
            bucket_counts[bucket].update(words)

    upsert_rows = []
    for bucket, counter in bucket_counts.items():
        for word, count in counter.items():
            upsert_rows.append((bucket, word, count))

    db.upsert_keyword_counts(upsert_rows)
    db.set_kv("keyword_agg_last_id", str(rows[-1]["id"]))

    # Verify keyword counts
    top = db.get_keyword_top(limit=10)
    word_map = {k["word"]: k["count"] for k in top}
    assert word_map["drama"] == 3
    assert word_map["fight"] == 2
    assert word_map["eviction"] == 1

    # Verify checkpoint was updated
    assert int(db.get_kv("keyword_agg_last_id")) == rows[-1]["id"]


# ============================================================
# API: keyword endpoints
# ============================================================


def test_api_keywords_trending_returns_shared_state(monkeypatch):
    fake_state = {"trending_keywords": [{"word": "fish", "count": 10}]}
    monkeypatch.setattr(shared_state, "read_state", lambda path: fake_state)
    result = _server.api_keywords_trending()
    assert result["window_seconds"] == 300
    assert len(result["keywords"]) == 1
    assert result["keywords"][0]["word"] == "fish"
    assert result["keywords"][0]["count"] == 10


def test_api_keywords_trending_empty(monkeypatch):
    monkeypatch.setattr(shared_state, "read_state", lambda path: {})
    result = _server.api_keywords_trending()
    assert result["window_seconds"] == 300
    assert result["keywords"] == []


def test_api_keywords_top_returns_data():
    database.upsert_keyword_counts([
        ("2026-04-12T14", "drama", 20),
        ("2026-04-12T14", "fight", 10),
    ])
    result = _server.api_keywords_top(since="2026-04-12T00:00:00Z", limit=20)
    assert len(result) == 2
    assert result[0]["word"] == "drama"
    assert result[0]["count"] == 20


def test_api_keywords_top_respects_limit():
    database.upsert_keyword_counts([
        ("2026-04-12T14", "aaa", 50),
        ("2026-04-12T14", "bbb", 40),
        ("2026-04-12T14", "ccc", 30),
        ("2026-04-12T14", "ddd", 20),
        ("2026-04-12T14", "eee", 10),
    ])
    result = _server.api_keywords_top(since="2026-04-12T00:00:00Z", limit=2)
    assert len(result) == 2
    assert result[0]["word"] == "aaa"
    assert result[1]["word"] == "bbb"


def test_api_keyword_analytics_returns_top_and_hourly():
    database.upsert_keyword_counts([
        ("2026-04-12T14", "drama", 20),
        ("2026-04-12T14", "fight", 10),
        ("2026-04-12T15", "drama", 15),
        ("2026-04-12T15", "chaos", 25),
    ])
    result = _server.api_keyword_analytics(since="2026-04-12T00:00:00Z", until=None)
    assert "top_keywords" in result
    assert "hourly" in result
    assert len(result["hourly"]) == 2
    top_words = [k["word"] for k in result["top_keywords"]]
    assert "drama" in top_words
    assert "chaos" in top_words


def test_api_keywords_top_normalizes_since():
    database.upsert_keyword_counts([("2026-04-12T14", "test", 5)])
    # since with seconds precision should not error
    result = _server.api_keywords_top(since="2026-04-12T14:30:45Z", limit=20)
    assert isinstance(result, list)
