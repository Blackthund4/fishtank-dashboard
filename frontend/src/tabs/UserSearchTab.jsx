import { useState, useRef, useMemo, useCallback } from 'react'
import { Virtuoso } from 'react-virtuoso'
import { Search, MessageSquare, Volume2, Music, Fish, User } from 'lucide-react'
import { formatDateTime } from '../utils/formatTime'

export default function UserSearchTab({ itemCatalog, roomMap }) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState(null)
  const [loading, setLoading] = useState(false)
  const [activeFilter, setActiveFilter] = useState('all')
  const [suggestions, setSuggestions] = useState([])
  const [showSuggestions, setShowSuggestions] = useState(false)
  const suggestTimer = useRef(null)

  function handleQueryChange(val) {
    setQuery(val)
    if (val.trim().length < 2) {
      setSuggestions([])
      setShowSuggestions(false)
      return
    }
    // Debounce autocomplete
    if (suggestTimer.current) clearTimeout(suggestTimer.current)
    suggestTimer.current = setTimeout(() => {
      fetch(`/api/users/suggest?q=${encodeURIComponent(val.trim())}`)
        .then(r => r.json())
        .then(data => { setSuggestions(data || []); setShowSuggestions(true) })
        .catch(() => setSuggestions([]))
    }, 250)
  }

  function selectSuggestion(name) {
    setQuery(name)
    setSuggestions([])
    setShowSuggestions(false)
    // Auto-search
    setLoading(true)
    fetch(`/api/user/${encodeURIComponent(name)}`)
      .then(r => r.json())
      .then(data => { setResults(data); setLoading(false); setActiveFilter('all') })
      .catch(() => { setResults(null); setLoading(false) })
  }

  function handleSearch(e) {
    e.preventDefault()
    setShowSuggestions(false)
    const q = query.trim()
    if (!q) return
    setLoading(true)
    fetch(`/api/user/${encodeURIComponent(q)}`)
      .then(r => r.json())
      .then(data => { setResults(data); setLoading(false); setActiveFilter('all') })
      .catch(() => { setResults(null); setLoading(false) })
  }

  const filters = [
    { id: 'all', label: 'All', icon: User },
    { id: 'chat', label: 'Chat', icon: MessageSquare },
    { id: 'tts', label: 'TTS', icon: Volume2 },
    { id: 'sfx', label: 'SFX', icon: Music },
    { id: 'fishtoys', label: 'Fishtoys', icon: Fish },
  ]

  // Build unified timeline
  const timeline = useMemo(() =>
    results ? buildTimeline(results, activeFilter, itemCatalog, roomMap) : [],
    [results, activeFilter, itemCatalog, roomMap]
  )

  return (
    <div className="flex-1 flex flex-col p-3 min-h-0">
      <div className="flex items-center gap-3 mb-3 shrink-0">
        <User className="w-5 h-5 text-tank-accent" />
        <h2 className="text-sm font-bold text-tank-bright uppercase tracking-wider">User Search</h2>
      </div>

      <form onSubmit={handleSearch} className="flex gap-2 mb-3 shrink-0">
        <div className="relative flex-1 max-w-md">
          <Search className="w-4 h-4 text-tank-muted absolute left-3 top-1/2 -translate-y-1/2" />
          <input
            type="text"
            placeholder="Enter username..."
            value={query}
            onChange={e => handleQueryChange(e.target.value)}
            onFocus={() => suggestions.length > 0 && setShowSuggestions(true)}
            onBlur={() => setTimeout(() => setShowSuggestions(false), 200)}
            className="w-full bg-tank-bg border border-tank-border rounded-lg text-sm text-tank-text pl-10 pr-3 py-2 font-mono placeholder:text-tank-muted/50 focus:border-tank-accent/50 focus:outline-none"
          />
          {showSuggestions && suggestions.length > 0 && (
            <div className="absolute z-10 top-full left-0 right-0 mt-1 bg-tank-surface border border-tank-border rounded-lg shadow-lg overflow-hidden">
              {suggestions.map(name => (
                <button
                  key={name}
                  type="button"
                  onMouseDown={() => selectSuggestion(name)}
                  className="w-full text-left px-3 py-1.5 text-sm font-mono text-tank-text hover:bg-tank-highlight transition-colors"
                >
                  {name}
                </button>
              ))}
            </div>
          )}
        </div>
        <button type="submit" className="bg-tank-accent/10 border border-tank-accent/30 text-tank-accent text-sm font-mono px-4 py-2 rounded-lg hover:bg-tank-accent/20">
          Search
        </button>
      </form>

      {loading && (
        <div className="text-sm text-tank-muted font-mono py-8 text-center">Searching...</div>
      )}

      {results && !loading && (
        <>
          {/* Stats bar */}
          <div className="flex items-center gap-3 mb-3 shrink-0">
            <span className="text-sm text-tank-bright font-semibold">{results.username}</span>
            <div className="flex gap-2">
              {filters.map(f => {
                const count = f.id === 'all'
                  ? Object.values(results.totals).reduce((s, v) => s + v, 0)
                  : results.totals[f.id] || 0
                return (
                  <button
                    key={f.id}
                    onClick={() => setActiveFilter(f.id)}
                    className={`flex items-center gap-1 px-2.5 py-1 rounded-lg text-xs font-mono border transition-colors ${
                      activeFilter === f.id
                        ? 'border-tank-accent bg-tank-accent/10 text-tank-accent'
                        : 'border-tank-border text-tank-muted hover:border-tank-muted'
                    }`}
                  >
                    <f.icon className="w-3 h-3" />
                    {f.label}
                    <span className="text-[10px] ml-0.5">{count}</span>
                  </button>
                )
              })}
            </div>
          </div>

          {/* Results */}
          <div className="flex-1 min-h-0">
            {timeline.length === 0 ? (
              <div className="text-sm text-tank-muted font-mono py-8 text-center">
                No {activeFilter === 'all' ? 'activity' : activeFilter} found for "{results.username}"
              </div>
            ) : (
              <Virtuoso
                style={{ height: '100%' }}
                data={timeline}
                overscan={100}
                itemContent={(index, item) => (
                  <div className="flex items-start gap-2 p-2 mb-1 bg-tank-surface border border-tank-border rounded hover:border-tank-accent/20 transition-colors">
                    <div className={`w-6 h-6 rounded flex items-center justify-center shrink-0 mt-0.5 ${item.iconBg}`}>
                      <item.icon className={`w-3.5 h-3.5 ${item.iconColor}`} />
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 mb-0.5">
                        <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded ${item.badgeBg} ${item.badgeColor}`}>
                          {item.badge}
                        </span>
                        <span className="text-[10px] font-mono text-tank-muted">{item.time}</span>
                        {item.cost > 0 && (
                          <span className="text-[10px] font-mono text-tank-warn">{item.cost}t</span>
                        )}
                        {item.room && (
                          <span className="text-[10px] text-cyan-400">{item.room}</span>
                        )}
                        {item.target && (
                          <span className="text-[10px] text-tank-warn">{item.target}</span>
                        )}
                      </div>
                      <p className="text-sm text-tank-text break-words whitespace-pre-wrap">{item.content}</p>
                      {item.metadata && (
                        <div className="mt-1 bg-tank-bg border border-tank-accent/20 rounded px-2 py-1.5">
                          <span className="text-[9px] font-mono text-tank-accent uppercase">Hidden Content</span>
                          <p className="text-sm text-tank-bright break-words whitespace-pre-wrap">{item.metadata}</p>
                        </div>
                      )}
                    </div>
                  </div>
                )}
              />
            )}
          </div>
        </>
      )}

      {!results && !loading && (
        <div className="text-sm text-tank-muted font-mono py-8 text-center">
          Search for a username to see their chat history, TTS messages, SFX, and fishtoy activity
        </div>
      )}
    </div>
  )
}

function buildTimeline(results, filter, itemCatalog = {}, roomMap = {}) {
  const items = []

  if (filter === 'all' || filter === 'chat') {
    results.chat.forEach(r => {
      items.push({
        id: r.id,
        type: 'chat',
        time: formatDateTime(r.timestamp),
        sortKey: r.timestamp,
        content: r.data?.message || '',
        icon: MessageSquare,
        iconBg: 'bg-blue-500/10',
        iconColor: 'text-blue-400',
        badge: 'CHAT',
        badgeBg: 'bg-blue-500/10',
        badgeColor: 'text-blue-400',
        cost: 0,
        room: null,
        target: null,
        metadata: null,
      })
    })
  }

  if (filter === 'all' || filter === 'tts') {
    results.tts.forEach(r => {
      const roomCode = r.data?.room || ''
      items.push({
        id: r.id,
        type: 'tts',
        time: formatDateTime(r.timestamp),
        sortKey: r.timestamp,
        content: r.data?.message || r.data?.text || '',
        icon: Volume2,
        iconBg: 'bg-purple-500/10',
        iconColor: 'text-purple-400',
        badge: 'TTS',
        badgeBg: 'bg-purple-500/10',
        badgeColor: 'text-purple-400',
        cost: r.data?.cost || 0,
        room: roomMap[roomCode] || roomCode || null,
        target: null,
        metadata: null,
      })
    })
  }

  if (filter === 'all' || filter === 'sfx') {
    results.sfx.forEach(r => {
      const roomCode = r.data?.room || ''
      items.push({
        id: r.id,
        type: 'sfx',
        time: formatDateTime(r.timestamp),
        sortKey: r.timestamp,
        content: r.data?.message || r.data?.text || r.data?.sfxName || 'SFX',
        icon: Music,
        iconBg: 'bg-indigo-500/10',
        iconColor: 'text-indigo-400',
        badge: 'SFX',
        badgeBg: 'bg-indigo-500/10',
        badgeColor: 'text-indigo-400',
        cost: r.data?.cost || 0,
        room: roomMap[roomCode] || roomCode || null,
        target: null,
        metadata: null,
      })
    })
  }

  if (filter === 'all' || filter === 'fishtoys') {
    results.fishtoys.forEach(r => {
      const iid = String(r.data?.itemId || '')
      const cat = itemCatalog[iid]
      const itemName = cat?.name || `Item #${iid}`
      const meta = r.data?.metadata
      items.push({
        id: r.id,
        type: 'fishtoy',
        time: formatDateTime(r.timestamp),
        sortKey: r.timestamp,
        content: `${itemName} -> ${r.data?.target || '?'} (${r.data?.cost || 0} tokens)`,
        icon: Fish,
        iconBg: 'bg-tank-accent/10',
        iconColor: 'text-tank-accent',
        badge: cat?.type || 'FISHTOY',
        badgeBg: cat?.type === 'BIGTOY' ? 'bg-purple-500/10' : 'bg-tank-accent/10',
        badgeColor: cat?.type === 'BIGTOY' ? 'text-purple-400' : 'text-tank-accent',
        cost: r.data?.cost || 0,
        room: null,
        target: r.data?.target || null,
        metadata: meta && meta !== 'null' ? (typeof meta === 'object' ? JSON.stringify(meta) : meta) : null,
      })
    })
  }

  // Sort by timestamp descending (newest first)
  items.sort((a, b) => (b.sortKey || '').localeCompare(a.sortKey || ''))
  return items
}
