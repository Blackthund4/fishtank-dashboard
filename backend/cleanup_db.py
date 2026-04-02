"""
One-time database cleanup script.

Removes:
  - Duplicate TTS/SFX events (keeps first per event_id)
  - System chat echoes (tts, sfx, emote usernames)
  - Season pass gift notifications

Run this once to clean historical data. The server's ingestion
filters prevent new dirty data from entering.

Usage:
    cd backend
    python cleanup_db.py
"""

import database

database.init_db()

print("=" * 60)
print("  Fishtank Database Cleanup")
print("=" * 60)
print()

# 1. Dedup TTS/SFX
print("[1/3] Deduplicating TTS/SFX events...")
tts_deleted = database.dedup_tts_sfx()
print(f"      Deleted {tts_deleted} duplicate TTS/SFX rows")

# 2. Purge system chat echoes
print("[2/3] Purging system chat echoes (tts, sfx, emote)...")
chat_deleted = database.purge_system_chat()
print(f"      Deleted {chat_deleted} system chat rows")

# 3. Purge season pass gift notifications
print("[3/3] Purging season pass gift notifications...")
notif_deleted = database.purge_gift_notifications()
print(f"      Deleted {notif_deleted} gift notification rows")

print()
print(f"Total: {tts_deleted + chat_deleted + notif_deleted} rows removed")
print("Done.")
