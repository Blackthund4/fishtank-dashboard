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
    # Clear dedup state between tests
    _seen_tts_sfx_ids.clear()


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
