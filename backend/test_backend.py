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
import sys
import pytest

# Override DB path BEFORE importing database module
os.environ["FISHTANK_DB_PATH"] = ":memory:"

import database

# Import filter functions from server (without starting the app)
sys.path.insert(0, os.path.dirname(__file__))
from server import _should_filter_chat, _should_filter_notification, _is_duplicate, _seen_tts_sfx_ids
from server import _check_rate_limit, _prune_rate_limits, _rate_limits, RATE_LIMIT_MAX
from server import _score_sentiment


# ============================================================
# FIXTURES
# ============================================================


@pytest.fixture(autouse=True)
def fresh_db():
    """Reset the database for each test."""
    # Get a fresh connection for this thread
    conn = database._get_conn()
    # Drop and recreate tables
    conn.executescript("""
        DROP TABLE IF EXISTS events;
        DROP TABLE IF EXISTS stock_history;
    """)
    database.init_db()
    yield
    # Clear dedup and rate limit state between tests
    _seen_tts_sfx_ids.clear()
    _rate_limits.clear()


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
    database.store_event("poll:start", {"poll": {"question": "Test?", "answers": ["A", "B"]}})
    database.store_event("poll:vote", [{"value": "A", "score": 5}])
    database.store_event("poll:stop", {"question": "Test?", "winner": "A"})
    polls = database.get_polls()
    assert len(polls) == 2
    types = {p["event_type"] for p in polls}
    assert "poll:vote" not in types


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


def test_get_fishtoys_pagination():
    for i in range(5):
        database.store_event("fishtoy:used", {"displayName": f"U{i}", "target": "T", "cost": 100})
    result = database.get_fishtoys(limit=2, offset=0)
    assert len(result) == 2
    result2 = database.get_fishtoys(limit=2, offset=2)
    assert len(result2) == 2
    # Different events
    assert result[0]["id"] != result2[0]["id"]


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
