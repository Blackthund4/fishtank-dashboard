"""Chat message tokenizer for keyword analysis.

Extracts meaningful words from chat messages, filtering stopwords and noise.
Used by both the inline real-time buffer (one message at a time) and the
background aggregation thread (batched).

Stopword list calibrated from prod audit (2026-04-12, 1.15M msgs / 7 days).
"""

import re
from collections import Counter

# Compile once at import time
_WORD_RE = re.compile(r"[a-z]{3,}")

# Frozen set -- O(1) lookup, immutable, safe across threads
# Calibrated from prod audit (2026-04-12, 1.15M msgs / 7 days / 92k unique words)
STOPWORDS = frozenset({
    # Standard English (confirmed high-frequency in audit: the 2.80%, and 0.91%, etc.)
    "the", "and", "that", "have", "for", "not", "with", "you", "this",
    "but", "his", "from", "they", "been", "one", "had", "has", "her",
    "all", "their", "there", "what", "about", "which", "when", "will",
    "each", "make", "like", "just", "over", "such", "than", "them",
    "very", "some", "can", "would", "could", "into", "other", "more",
    "should", "was", "were", "are", "did", "does", "how", "its",
    "she", "him", "who", "get", "got", "out", "also", "your",
    "only", "then", "too", "any", "don", "now", "way", "may",
    "own", "say", "said", "most", "same", "being", "because",
    "going", "want", "still", "even", "think", "know", "come",
    "really", "right", "off", "back", "need", "ever", "put",
    "good", "why", "time", "bet", "nothing", "without", "day",
    "days", "man",

    # Chat noise (confirmed in audit: lol 0.82%, lmao 0.32%, btw 0.16%)
    "lol", "lmao", "lmfao", "rofl", "haha", "hahaha", "hehe",
    "bro", "bruh", "dude", "guys", "yeah", "yea", "nah",
    "omg", "omfg", "wtf", "idk", "imo", "tbh", "tho",
    "pog", "poggers", "kek", "kekw", "copium", "cope",
    "based", "ratio", "btw", "please",
    "http", "https", "www", "com",

    # Bot/game command spam (confirmed in audit)
    "coinflip", "double", "lexxpoints",

    # Platform noise (event type leakage / generic actions)
    "clip", "tip", "sfx", "tts", "used",
})


def tokenize(text):
    """Extract meaningful words from a chat message.

    Returns a list of lowercase words (3+ alpha chars, not in stopwords).
    Duplicates within a single message are preserved for accurate counting.
    """
    if not text or not isinstance(text, str):
        return []
    return [w for w in _WORD_RE.findall(text.lower()) if w not in STOPWORDS]


def count_tokens(texts):
    """Count word frequencies across multiple messages.

    Args:
        texts: iterable of (message_text,) tuples or plain strings

    Returns:
        Counter of {word: count}
    """
    counts = Counter()
    for item in texts:
        text = item[0] if isinstance(item, (tuple, list)) else item
        if text:
            counts.update(tokenize(text))
    return counts
