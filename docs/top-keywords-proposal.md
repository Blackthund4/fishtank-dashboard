# Top Keywords Feature Proposal

**Status:** Deferred — performance concerns with Python-side tokenization approach.

## Goal
Show the most frequently used words in chat messages across:
1. Last 24h sidebar (top 10)
2. AnalyticsTab chat section (top 20, tag-style pills)
3. Dedicated `/api/analytics/keywords` endpoint with `since` parameter

## Proposed Approach
Fetch raw `data` blobs from `events` table (filtered by `since`), tokenize in Python with `Counter`, filter stopwords.

### Why this approach
- Can't use `json_extract` in SQL aggregates (OOM on 600k+ rows)
- No schema migration needed
- `since` parameter bounds the result set

### Performance Concerns
- 7d worst case: ~100k messages fetched + `json.loads` per row + tokenization
- Estimated ~1-2s processing + ~50-100 MB memory spike per uncached request
- Even with 60s cache TTL, first request per time window is expensive
- Stats endpoint (24h sidebar) would add keyword extraction to every stats call

## Alternative Approaches to Explore

### A. Pre-computed keyword table
- New `chat_keywords` table populated at ingestion time
- `INSERT INTO chat_keywords (word, timestamp) ...` when processing `chat:message`
- Aggregate queries become simple `GROUP BY word ORDER BY COUNT(*) DESC`
- Pros: Fast reads, no Python-side processing
- Cons: Schema migration, backfill needed, storage overhead

### B. Extracted `message_text` column
- Add real column like `display_name`, populated by `_extract_columns()`
- Still can't easily tokenize in SQL, but avoids `json_extract`
- Would need a SQL tokenization strategy (e.g., recursive CTE or FTS5)

### C. SQLite FTS5 virtual table
- Create FTS5 index on chat message text
- Native full-text search with term frequency ranking
- Pros: Built-in tokenization, fast, low memory
- Cons: FTS5 module availability, separate index maintenance, backfill

### D. Periodic background job
- Background thread computes keyword counts every N minutes
- Stores result in memory or a cache table
- Endpoint serves pre-computed data instantly
- Pros: Zero request-time cost, predictable resource usage
- Cons: Data staleness (acceptable for keywords)

## Files That Would Be Modified
- `backend/database.py` — keyword extraction function + stats integration
- `backend/server.py` — `/api/analytics/keywords` endpoint
- `frontend/src/App.jsx` — `normalizeStats`, initial state, `Last24hSidebar`
- `frontend/src/tabs/AnalyticsTab.jsx` — state, fetch, display section

## Stopword Strategy
- Hardcoded `frozenset` of common English words + chat noise (lol, lmao, bro, etc.)
- Regex tokenizer: `r"[a-z]{3,}"` — only alpha tokens of 3+ chars
- Filter before counting, not after
