"""Atomic JSON shared state for inter-process communication.

Ingestion writes state; API reads it. Uses mtime-based caching
so the reader only re-parses when the file actually changes.
"""

import os

try:
    import orjson
    def _serialize(obj):
        return orjson.dumps(obj, default=str)
    def _deserialize(raw):
        return orjson.loads(raw)
except ImportError:
    import json
    def _serialize(obj):
        return json.dumps(obj, ensure_ascii=False, default=str).encode()
    def _deserialize(raw):
        return json.loads(raw)

_cached_state = {}
_cached_mtime = 0.0


def write_state(path, state_dict):
    """Atomically write state_dict as JSON to path."""
    global _cached_state, _cached_mtime
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(_serialize(state_dict))
    os.replace(tmp, path)
    # Invalidate cache so same-process reads see the new data even if
    # mtime granularity masks the write (sub-second writes on ext4, etc.)
    _cached_mtime = 0.0


def read_state(path):
    """Read and return parsed state from path, with mtime caching."""
    global _cached_state, _cached_mtime
    try:
        mtime = os.stat(path).st_mtime
    except FileNotFoundError:
        return {}
    if mtime != _cached_mtime:
        with open(path, "rb") as f:
            _cached_state = _deserialize(f.read())
        _cached_mtime = mtime
    return _cached_state
