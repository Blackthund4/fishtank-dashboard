import { useState, useEffect, useMemo, useRef, useCallback } from 'react'
import { Fish, MessageSquare, Radio, Search, X, BarChart3, FileText, Bell, Vote, User, Zap, Package, Star, TrendingUp } from 'lucide-react'
import { Virtuoso } from 'react-virtuoso'
import { useWebSocket } from './useWebSocket'
import { formatDateTime } from './utils/formatTime'
import StatusBar from './components/StatusBar'
import Panel from './components/Panel'
import FishtoyCard from './components/FishtoyCard'
import ChatMessage from './components/ChatMessage'
import ActivityCard from './components/ActivityCard'
import AnalyticsTab from './tabs/AnalyticsTab'
import ChartsTab from './tabs/ChartsTab'
import HiddenContentTab from './tabs/HiddenContentTab'
import UserSearchTab from './tabs/UserSearchTab'

const MAX_EVENTS = 500

const FISHTOY_TYPES = new Set(['fishtoy:used'])
const CHAT_TYPES = new Set(['chat:message'])
const ACTIVITY_TYPES = new Set([
  'tts:update', 'sfx:update',
  'happening', 'item:new', 'item:update',
  'item-details:new', 'item-details:update',
])
// super-chat:new handled by dedicated handler before ACTIVITY_TYPES check
const NOTIFICATION_TYPES = new Set(['notification:global', 'announcement'])
const SYSTEM_TYPES = new Set([
  'tts:price', 'sfx:price', 'stock:update', 'stock:new',
  'stock:remove', 'stock:split', 'feature-toggles:update',
])

// Extract a sortable timestamp from event data
function getEventTimestamp(item) {
  const d = item.data
  if (!d) return 0
  const raw = d.createdAt || d.updatedAt || d.timestamp
  if (!raw) return 0
  if (typeof raw === 'number') return raw > 1e12 ? raw : raw * 1000
  const ms = Date.parse(raw)
  return isNaN(ms) ? 0 : ms
}

// Sort events newest first by actual timestamp
function sortByTimestamp(arr) {
  return [...arr].sort((a, b) => getEventTimestamp(b) - getEventTimestamp(a))
}

function normalizeStats(raw) {
  const byType = raw.by_type || {}
  return {
    fishtoys: raw.fishtoys?.total || 0,
    chats: byType['chat:message'] || 0,
    tts: byType['tts:update'] || 0,
    sfx: byType['sfx:update'] || 0,
    total_spend: raw.total_spend || raw.fishtoys?.total_cost || 0,
    poll_tokens: raw.poll_tokens || 0,
    superchat_tokens: raw.superchat_tokens || 0,
    top_targets: (raw.top_targets || []).map(t => ({ target: t.name, count: t.count })),
    top_senders: (raw.top_senders || []).map(s => ({ name: s.name, count: s.count, spend: s.spend || 0 })),
    top_tts_senders: (raw.top_tts_senders || []).map(s => ({ name: s.name, count: s.count, spend: s.spend || 0 })),
    top_sfx_senders: (raw.top_sfx_senders || []).map(s => ({ name: s.name, count: s.count, spend: s.spend || 0 })),
    top_chat_senders: (raw.top_chat_senders || []).map(s => ({ name: s.name, count: s.count })),
    top_fishtoy_senders: (raw.top_fishtoy_senders || []).map(s => ({ name: s.name, count: s.count, spend: s.spend || 0 })),
    total_events: raw.total_events || 0,
  }
}

function formatSystemEvent(e) {
  const d = e.data || {}
  const time = formatDateTime(e.timestamp || d.updatedAt || d.createdAt)

  if (e.event === 'feature-toggles:update') {
    const name = (d.feature || '').toUpperCase() || 'Unknown'
    const state = d.enabled ? 'enabled' : 'disabled'
    const price = d.metadata ? ` (${d.metadata} tokens)` : ''
    return { badge: 'TOGGLE', badgeClass: d.enabled ? 'bg-green-500/10 text-green-400' : 'bg-red-500/10 text-red-400', message: `${name} ${state}${price}`, time }
  }
  if (e.event === 'stock:update') {
    if (d.oldTickerSymbol && d.newTickerSymbol) {
      return { badge: 'STO-X', badgeClass: 'bg-blue-500/10 text-blue-400', message: `${d.oldTickerSymbol} renamed to ${d.newTickerSymbol}`, time }
    }
    const ticker = d.tickerSymbol || '?'
    const price = d.currentPrice ?? '?'
    return { badge: 'STO-X', badgeClass: 'bg-blue-500/10 text-blue-400', message: `${ticker} price updated to ${price}`, time }
  }
  if (e.event === 'stock:new') {
    return { badge: 'STO-X', badgeClass: 'bg-green-500/10 text-green-400', message: `${d.tickerSymbol || '?'} added to market`, time }
  }
  if (e.event === 'stock:remove') {
    return { badge: 'STO-X', badgeClass: 'bg-red-500/10 text-red-400', message: `${d.tickerSymbol || '?'} removed from market`, time }
  }
  if (e.event === 'stock:split') {
    return { badge: 'STO-X', badgeClass: 'bg-yellow-500/10 text-yellow-400', message: `${d.tickerSymbol || '?'} stock split`, time }
  }
  if (e.event === 'tts:price') {
    const price = typeof d === 'number' ? d : d.price || d.cost || JSON.stringify(d)
    return { badge: 'TTS', badgeClass: 'bg-purple-500/10 text-purple-400', message: `TTS price changed to ${price} tokens`, time }
  }
  if (e.event === 'sfx:price') {
    const price = typeof d === 'number' ? d : d.price || d.cost || JSON.stringify(d)
    return { badge: 'SFX', badgeClass: 'bg-indigo-500/10 text-indigo-400', message: `SFX price changed to ${price} tokens`, time }
  }
  return { badge: e.event.split(':')[0].toUpperCase(), badgeClass: 'bg-tank-highlight text-tank-muted', message: typeof d === 'string' ? d : JSON.stringify(d).substring(0, 100), time }
}

// $10 = 100 tokens -> 1 token = $0.10
function tokensToUSD(tokens) {
  return (tokens * 0.10).toLocaleString('en-US', { style: 'currency', currency: 'USD' })
}

export default function App() {
  const { isConnected, addListener } = useWebSocket()
  const [serverVersion, setServerVersion] = useState(null)
  const [knownVersion, setKnownVersion] = useState(null)
  const [fishtoys, setFishtoys] = useState([])
  const [fishtoyHasMore, setFishtoyHasMore] = useState(true)
  const [fishtoyLoading, setFishtoyLoading] = useState(false)
  const [chats, setChats] = useState([])
  const [activity, setActivity] = useState([])
  const [stats, setStats] = useState({
    fishtoys: 0, chats: 0, tts: 0, sfx: 0, total_spend: 0, poll_tokens: 0, superchat_tokens: 0,
    top_targets: [], top_senders: [], top_tts_senders: [], top_sfx_senders: [], top_chat_senders: [], top_fishtoy_senders: [], total_events: 0,
  })
  const [sessionStats, setSessionStats] = useState({
    fishtoys: 0, chats: 0, tts: 0, sfx: 0, total_spend: 0, poll_tokens: 0, superchat_tokens: 0,
    top_targets: [], top_senders: [], top_tts_senders: [], top_sfx_senders: [], top_chat_senders: [], top_fishtoy_senders: [], total_events: 0,
  })

  // Catalog data
  const [itemCatalog, setItemCatalog] = useState({})
  const [contestants, setContestants] = useState([])
  const [roomMap, setRoomMap] = useState({})
  const [stocks, setStocks] = useState([])

  // Filters
  const [filterTarget, setFilterTarget] = useState(null)
  const [filterItemId, setFilterItemId] = useState(null)
  const [filterCategory, setFilterCategory] = useState(null)
  const [searchText, setSearchText] = useState('')
  const [activeTab, setActiveTab] = useState('dashboard')

  // Polls and notifications
  const [activePoll, setActivePoll] = useState(null)
  const [pollVotes, setPollVotes] = useState([])
  const [pollElapsed, setPollElapsed] = useState(null)
  const [notifications, setNotifications] = useState([])
  const [systemEvents, setSystemEvents] = useState([])
  const [featureToggles, setFeatureToggles] = useState({})
  const [polls, setPolls] = useState([])
  const [fishtoyStatus, setFishtoyStatus] = useState([])
  const [fishtoyFilter, setFishtoyFilter] = useState('enabled')
  const [allTargets, setAllTargets] = useState([])
  const [activeSuperchats, setActiveSuperchats] = useState([])
  const [scCountdowns, setScCountdowns] = useState({})
  const [activityFilter, setActivityFilter] = useState('all')
  const [systemFilter, setSystemFilter] = useState('all')
  const [directorTimeRange, setDirectorTimeRange] = useState('all')
  const [activityTimeRange, setActivityTimeRange] = useState('all')
  const [activityHasMore, setActivityHasMore] = useState(true)
  const [activityLoading, setActivityLoading] = useState(false)
  const directorRef = useRef(null)
  // Load catalog + historical data on mount
  useEffect(() => {
    fetch('/api/items').then(r => r.json()).then(setItemCatalog).catch(() => {})
    fetch('/api/contestants').then(r => r.json()).then(setContestants).catch(() => {})
    fetch('/api/rooms').then(r => r.json()).then(setRoomMap).catch(() => {})
    fetch('/api/stocks').then(r => r.json()).then(setStocks).catch(() => {})
    fetch('/api/stats').then(r => r.json()).then(raw => setStats(normalizeStats(raw))).catch(() => {})
    const since24h = new Date(Date.now() - 24 * 3600000).toISOString()
    fetch(`/api/stats?since=${encodeURIComponent(since24h)}`).then(r => r.json()).then(raw => setSessionStats(normalizeStats(raw))).catch(() => {})
    fetch('/api/feature-toggles').then(r => r.json()).then(setFeatureToggles).catch(() => {})
    fetch('/api/targets').then(r => r.json()).then(setAllTargets).catch(() => {})
    fetch('/api/fishtoy-availability').then(r => r.json()).then(setFishtoyStatus).catch(() => {})
    fetch('/api/polls').then(r => r.json()).then(setPolls).catch(() => {})
    fetch('/api/notifications?limit=500').then(r => r.json()).then(data => {
      setNotifications(data.map(n => ({
        id: n.id,
        type: n.event_type,
        message: typeof n.data === 'string' ? n.data : n.data?.message || JSON.stringify(n.data),
        timestamp: n.timestamp_local,
      })))
    }).catch(() => {})

    // Fishtoys fetched by fishtoyApiParams effect (server-side filters + pagination)

    // Fetch chat messages
    fetch('/api/events?type=chat:message&limit=500')
      .then(r => r.json())
      .then(events => {
        setChats(events.map(e => ({ event: e.event_type, data: e.data, dbId: e.id })))
      })
      .catch(() => {})

    // Fetch active superchats for pinned banners
    fetch('/api/superchats?limit=50')
      .then(r => r.json())
      .then(data => setActiveSuperchats(data.filter(sc => !sc.deleted)))
      .catch(() => {})

    // Fetch activity (TTS/SFX/happening/superchat) separately so chat doesn't crowd them out
    fetch('/api/events?type=tts:update,sfx:update,happening,super-chat:new&limit=500')
      .then(r => r.json())
      .then(events => {
        setActivity(events.map(e => ({ event: e.event_type, data: e.data, dbId: e.id })))
      })
      .catch(() => {})

    // Fetch system events separately so one type doesn't crowd out others
    Promise.all([
      fetch('/api/events?type=tts:price,sfx:price&limit=200').then(r => r.json()).catch(() => []),
      fetch('/api/events?type=stock:update,stock:new,stock:remove,stock:split&limit=200').then(r => r.json()).catch(() => []),
      fetch('/api/events?type=feature-toggles:update&limit=200').then(r => r.json()).catch(() => []),
    ]).then(([prices, stocks, toggles]) => {
      const all = [...prices, ...stocks, ...toggles]
        .map(e => ({ event: e.event_type, data: e.data, dbId: e.id, timestamp: e.timestamp_local }))
        .sort((a, b) => (b.dbId || 0) - (a.dbId || 0))
      setSystemEvents(all)
    })

    // Reconstruct poll state from database
    fetch('/api/polls/latest')
      .then(r => r.json())
      .then(poll => {
        if (poll && poll.question) {
          setActivePoll({
            question: poll.question,
            answers: poll.answers,
            pid: poll.pid,
            ended: !poll.active,
            winner: poll.winner || null,
            startedAt: poll.started_at ? Date.parse(poll.started_at) : null,
          })
          if (poll.votes) {
            setPollVotes(poll.votes)
          }
        }
      })
      .catch(() => {})
  }, [])

  // Live events
  useEffect(() => {
    const remove = addListener((msg) => {
      if (msg.event_type === 'server:hello') {
        const v = msg.data?.version
        if (v && v !== 'dev') {
          setServerVersion(prev => {
            if (!prev) { setKnownVersion(v); return v }
            return v
          })
        }
        return
      }

      const item = { event: msg.event_type, data: msg.data, dbId: msg.db_id }

      if (FISHTOY_TYPES.has(msg.event_type)) {
        setFishtoys(prev => [item, ...prev])
        const cost = msg.data?.cost || 0
        setStats(s => ({ ...s, fishtoys: s.fishtoys + 1, total_spend: s.total_spend + cost }))
        setSessionStats(s => ({ ...s, fishtoys: s.fishtoys + 1, total_spend: s.total_spend + cost }))
        // Incrementally update allTargets
        const target = msg.data?.target
        if (target) {
          setAllTargets(prev => {
            const idx = prev.findIndex(t => t.target === target)
            if (idx >= 0) {
              const updated = [...prev]
              updated[idx] = { ...updated[idx], count: updated[idx].count + 1, spend: updated[idx].spend + cost }
              return updated.sort((a, b) => b.count - a.count)
            }
            return [{ target, count: 1, spend: cost }, ...prev]
          })
        }
      } else if (CHAT_TYPES.has(msg.event_type)) {
        setChats(prev => [item, ...prev].slice(0, MAX_EVENTS))
        setStats(s => ({ ...s, chats: s.chats + 1 }))
        setSessionStats(s => ({ ...s, chats: s.chats + 1 }))
      } else if (ACTIVITY_TYPES.has(msg.event_type)) {
        setActivity(prev => [item, ...prev])
        if (msg.event_type.startsWith('tts')) {
          const cost = msg.data?.cost || 0
          setStats(s => ({ ...s, tts: s.tts + 1, total_spend: s.total_spend + cost }))
          setSessionStats(s => ({ ...s, tts: s.tts + 1, total_spend: s.total_spend + cost }))
        }
        if (msg.event_type.startsWith('sfx')) {
          const cost = msg.data?.cost || 0
          setStats(s => ({ ...s, sfx: s.sfx + 1, total_spend: s.total_spend + cost }))
          setSessionStats(s => ({ ...s, sfx: s.sfx + 1, total_spend: s.total_spend + cost }))
        }
      } else if (msg.event_type === 'super-chat:new') {
        const cost = msg.data?.cost || 0
        setSessionStats(s => ({ ...s, total_spend: s.total_spend + cost, superchat_tokens: s.superchat_tokens + cost }))
        setStats(s => ({ ...s, total_spend: s.total_spend + cost, superchat_tokens: s.superchat_tokens + cost }))
        // Add to activity feed
        setActivity(prev => [item, ...prev])
        // Add to pinned superchats (dedup by id)
        const scId = String(msg.data?.id || msg.db_id)
        setActiveSuperchats(prev => {
          if (prev.some(sc => String(sc.data?.id || sc.id) === scId)) return prev
          return [{ id: msg.db_id, data: msg.data, deleted: false }, ...prev]
        })
      } else if (msg.event_type === 'super-chat:delete') {
        const deleteId = String(msg.data?.id || '')
        setActiveSuperchats(prev => prev.filter(sc => String(sc.data?.id || sc.id) !== deleteId))
      } else if (msg.event_type === 'poll:start') {
        const pollData = msg.data?.poll || msg.data
        setActivePoll({
          question: pollData.question,
          answers: pollData.answers,
          pid: pollData.pid,
          startedAt: Date.now(),
        })
        setPollVotes(msg.data?.scores || [])
      } else if (msg.event_type === 'poll:vote') {
        setPollVotes(Array.isArray(msg.data) ? msg.data : [])
      } else if (msg.event_type === 'poll:stop') {
        setActivePoll(prev => prev ? { ...prev, ended: true, winner: msg.data?.winner } : null)
        fetch('/api/polls').then(r => r.json()).then(setPolls).catch(() => {})
      } else if (NOTIFICATION_TYPES.has(msg.event_type)) {
        const notif = {
          id: msg.db_id,
          type: msg.event_type,
          message: typeof msg.data === 'string' ? msg.data : msg.data?.message || JSON.stringify(msg.data),
          timestamp: new Date().toISOString(),
        }
        setNotifications(prev => [notif, ...prev].slice(0, 50))
      } else if (SYSTEM_TYPES.has(msg.event_type)) {
        setSystemEvents(prev => [item, ...prev].slice(0, 100))
        // Update feature toggle state in real-time
        if (msg.event_type === 'feature-toggles:update' && msg.data?.feature) {
          setFeatureToggles(prev => ({
            ...prev,
            [msg.data.feature]: {
              enabled: msg.data.enabled,
              metadata: msg.data.metadata,
              updated_at: new Date().toISOString(),
            }
          }))
        }
      }
    })
    return remove
  }, [addListener])

  // Refresh stats periodically
  useEffect(() => {
    const interval = setInterval(() => {
      fetch('/api/stats').then(r => r.json()).then(raw => setStats(normalizeStats(raw))).catch(() => {})
      const since24h = new Date(Date.now() - 24 * 3600000).toISOString()
      fetch(`/api/stats?since=${encodeURIComponent(since24h)}`).then(r => r.json()).then(raw => setSessionStats(normalizeStats(raw))).catch(() => {})
      fetch('/api/stocks').then(r => r.json()).then(setStocks).catch(() => {})
      fetch('/api/feature-toggles').then(r => r.json()).then(setFeatureToggles).catch(() => {})
      fetch('/api/fishtoy-availability').then(r => r.json()).then(setFishtoyStatus).catch(() => {})
    }, 30000)
    return () => clearInterval(interval)
  }, [])

  // Poll duration counter
  useEffect(() => {
    if (!activePoll || activePoll.ended || !activePoll.startedAt) {
      setPollElapsed(null)
      return
    }
    const tick = () => setPollElapsed(Math.floor((Date.now() - activePoll.startedAt) / 1000))
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [activePoll?.startedAt, activePoll?.ended])

  // Superchat countdown + auto-expiry
  useEffect(() => {
    if (activeSuperchats.length === 0) {
      setScCountdowns({})
      return
    }
    const tick = () => {
      const now = Date.now()
      const counts = {}
      const expired = []
      for (const sc of activeSuperchats) {
        const d = sc.data || {}
        const dur = d.duration // minutes
        if (!dur) continue
        const created = d.createdAt || d.updatedAt || sc.timestamp_local
        const startMs = typeof created === 'number'
          ? (created > 1e12 ? created : created * 1000)
          : Date.parse(created)
        if (!startMs || isNaN(startMs)) continue
        const expiresAt = startMs + dur * 60000
        const remaining = Math.floor((expiresAt - now) / 1000)
        const key = String(sc.id || d.id)
        if (remaining <= 0) {
          expired.push(key)
        } else {
          counts[key] = remaining
        }
      }
      setScCountdowns(counts)
      if (expired.length > 0) {
        setActiveSuperchats(prev => prev.filter(sc => {
          const key = String(sc.id || sc.data?.id)
          return !expired.includes(key)
        }))
      }
    }
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [activeSuperchats])

  // Debounce search text for server fetches (300ms)
  const [debouncedSearch, setDebouncedSearch] = useState('')
  useEffect(() => {
    const id = setTimeout(() => setDebouncedSearch(searchText.trim()), 300)
    return () => clearTimeout(id)
  }, [searchText])

  // Build fishtoy API URL from current server-side filters
  const fishtoyApiParams = useMemo(() => {
    const p = new URLSearchParams()
    p.set('limit', '500')
    if (filterTarget) p.set('target', filterTarget)
    if (filterItemId) p.set('item_id', filterItemId)
    if (debouncedSearch) p.set('search', debouncedSearch)
    return p.toString()
  }, [filterTarget, filterItemId, debouncedSearch])

  // Re-fetch fishtoys when server-side filters change
  useEffect(() => {
    setFishtoyHasMore(true)
    fetch(`/api/fishtoys?${fishtoyApiParams}`)
      .then(r => r.json())
      .then(events => {
        setFishtoys(events.map(e => ({ event: e.event_type, data: e.data, dbId: e.id })))
        if (events.length < 500) setFishtoyHasMore(false)
      })
      .catch(() => {})
  }, [fishtoyApiParams])

  // Client-side filters: category (not a server param) + WS event guard for target/itemId/search
  const filteredFishtoys = useMemo(() => {
    let result = fishtoys
    if (filterTarget) result = result.filter(f => f.data?.target === filterTarget)
    if (filterItemId) result = result.filter(f => String(f.data?.itemId) === String(filterItemId))
    if (filterCategory) {
      result = result.filter(f => {
        const cat = itemCatalog[String(f.data?.itemId || '')]
        return cat?.type === filterCategory
      })
    }
    if (searchText.trim()) {
      const q = searchText.toLowerCase()
      result = result.filter(f => {
        const meta = f.data?.metadata
        const name = f.data?.displayName
        return (meta && String(meta).toLowerCase().includes(q)) ||
               (name && name.toLowerCase().includes(q))
      })
    }
    return sortByTimestamp(result)
  }, [fishtoys, filterTarget, filterCategory, filterItemId, searchText, itemCatalog])

  const loadMoreFishtoys = useCallback(() => {
    if (fishtoyLoading || !fishtoyHasMore) return
    const minId = fishtoys.reduce((min, f) => {
      const id = f.dbId
      return id && (min === null || id < min) ? id : min
    }, null)
    if (minId === null) return
    setFishtoyLoading(true)
    fetch(`/api/fishtoys?${fishtoyApiParams}&before_id=${minId}`)
      .then(r => r.json())
      .then(events => {
        if (events.length === 0) {
          setFishtoyHasMore(false)
        } else {
          const newItems = events.map(e => ({ event: e.event_type, data: e.data, dbId: e.id }))
          setFishtoys(prev => {
            const existingIds = new Set(prev.map(f => f.dbId).filter(Boolean))
            const unique = newItems.filter(f => !existingIds.has(f.dbId))
            return [...prev, ...unique]
          })
          if (events.length < 500) setFishtoyHasMore(false)
        }
      })
      .catch(() => {})
      .finally(() => setFishtoyLoading(false))
  }, [fishtoys, fishtoyLoading, fishtoyHasMore, fishtoyApiParams])

  // Sorted chat and activity arrays
  const sortedChats = useMemo(() => sortByTimestamp(chats), [chats])
  const sortedActivity = useMemo(() => {
    let filtered = activity
    if (activityFilter === 'tts') filtered = filtered.filter(a => a.event === 'tts:update')
    else if (activityFilter === 'sfx') filtered = filtered.filter(a => a.event === 'sfx:update')
    else if (activityFilter === 'sc') filtered = filtered.filter(a => a.event === 'super-chat:new')
    if (activityTimeRange !== 'all') {
      const hours = { '1h': 1, '6h': 6, '24h': 24, '7d': 168 }[activityTimeRange]
      if (hours) {
        const cutoff = Date.now() - hours * 3600000
        filtered = filtered.filter(a => getEventTimestamp(a) >= cutoff)
      }
    }
    return sortByTimestamp(filtered)
  }, [activity, activityFilter, activityTimeRange])

  const loadMoreActivity = useCallback(() => {
    if (activityLoading || !activityHasMore) return
    const minId = activity.reduce((min, a) => {
      const id = a.dbId
      return id && (min === null || id < min) ? id : min
    }, null)
    if (minId === null) return
    setActivityLoading(true)
    fetch(`/api/events?type=tts:update,sfx:update,happening,super-chat:new&limit=200&before_id=${minId}`)
      .then(r => r.json())
      .then(events => {
        if (events.length === 0) {
          setActivityHasMore(false)
        } else {
          const newItems = events.map(e => ({ event: e.event_type, data: e.data, dbId: e.id }))
          setActivity(prev => {
            const existingIds = new Set(prev.map(a => a.dbId).filter(Boolean))
            const unique = newItems.filter(a => !existingIds.has(a.dbId))
            return [...prev, ...unique]
          })
          if (events.length < 200) setActivityHasMore(false)
        }
      })
      .catch(() => {})
      .finally(() => setActivityLoading(false))
  }, [activity, activityLoading, activityHasMore])

  // Unique item types seen in fishtoys for filter dropdown
  const seenItemTypes = useMemo(() => {
    const map = new Map()
    fishtoys.forEach(f => {
      const iid = String(f.data?.itemId || '')
      if (iid && !map.has(iid)) {
        const cat = itemCatalog[iid]
        map.set(iid, cat?.name || `Item #${iid}`)
      }
    })
    return Array.from(map.entries()).sort((a, b) => a[1].localeCompare(b[1]))
  }, [fishtoys, itemCatalog])

  // Targets sourced from DB via /api/targets, incrementally updated from WS
  const seenTargets = allTargets

  // Target-specific stats when a target is selected
  const targetStats = useMemo(() => {
    if (!filterTarget) return null

    const targetEvents = fishtoys.filter(f => f.data?.target === filterTarget)
    const totalSpend = targetEvents.reduce((sum, f) => sum + (f.data?.cost || 0), 0)

    // Item types used on this target
    const itemCounts = new Map()
    targetEvents.forEach(f => {
      const iid = String(f.data?.itemId || '')
      const cat = itemCatalog[iid]
      const name = cat?.name || `Item #${iid}`
      const key = iid
      if (!itemCounts.has(key)) {
        itemCounts.set(key, { id: iid, name, count: 0, spend: 0 })
      }
      const entry = itemCounts.get(key)
      entry.count++
      entry.spend += f.data?.cost || 0
    })
    const topItems = Array.from(itemCounts.values()).sort((a, b) => b.count - a.count)

    // Top senders to this target
    const senderCounts = new Map()
    targetEvents.forEach(f => {
      const name = f.data?.displayName
      if (name) senderCounts.set(name, (senderCounts.get(name) || 0) + 1)
    })
    const topSenders = Array.from(senderCounts.entries())
      .map(([name, count]) => ({ name, count }))
      .sort((a, b) => b.count - a.count)
      .slice(0, 10)

    // Events with metadata
    const withMeta = targetEvents.filter(f => f.data?.metadata && f.data.metadata !== 'null').length

    return { total: targetEvents.length, totalSpend, topItems, topSenders, withMeta }
  }, [filterTarget, fishtoys, itemCatalog])

  const hasActiveFilters = filterTarget || filterCategory || filterItemId || searchText.trim()

  function clearFilters() {
    setFilterTarget(null)
    setFilterCategory(null)
    setFilterItemId(null)
    setSearchText('')
  }

  return (
    <div className="h-screen flex flex-col">
      <StatusBar isConnected={isConnected} stats={stats} />

      {serverVersion && knownVersion && serverVersion !== knownVersion && (
        <div className="bg-tank-accent/10 border-b border-tank-accent/30 px-3 py-1.5 flex items-center justify-center gap-2 shrink-0">
          <span className="text-xs text-tank-accent">A new version is available.</span>
          <button
            onClick={() => window.location.reload()}
            className="text-xs font-medium text-tank-accent underline hover:text-tank-bright"
          >
            Refresh
          </button>
        </div>
      )}

      {/* Tab navigation */}
      <div className="bg-tank-surface border-b border-tank-border px-3 flex items-center gap-1 shrink-0">
        {[
          { id: 'dashboard', label: 'Dashboard', icon: Fish },
          { id: 'analytics', label: 'Analytics', icon: BarChart3 },
          { id: 'charts', label: 'Charts', icon: TrendingUp },
          { id: 'hidden', label: 'Hidden Content', icon: FileText },
          { id: 'users', label: 'User Search', icon: User },
        ].map(tab => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-mono border-b-2 transition-colors ${
              activeTab === tab.id
                ? 'border-tank-accent text-tank-accent'
                : 'border-transparent text-tank-muted hover:text-tank-text'
            }`}
          >
            <tab.icon className="w-3.5 h-3.5" />
            <span className="hidden sm:inline">{tab.label}</span>
          </button>
        ))}
      </div>

      {/* Notification banner (director messages) */}
      {notifications.length > 0 && (
        <div className="bg-yellow-500/10 border-b border-yellow-500/30 px-3 py-1.5 flex flex-wrap items-center gap-2 shrink-0">
          <Bell className="w-4 h-4 text-yellow-400 shrink-0" />
          <span className="text-xs font-semibold text-yellow-400 uppercase shrink-0">Director</span>
          <span className="text-sm text-tank-bright flex-1">{notifications[0].message}</span>
          <span className="text-[10px] font-mono text-tank-muted shrink-0">
            {new Date(notifications[0].timestamp).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
          </span>
          {notifications.length > 1 && (
            <button
              onClick={() => {
                setActiveTab('dashboard')
                setTimeout(() => directorRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 100)
              }}
              className="text-[10px] font-mono text-yellow-400/60 hover:text-yellow-400 underline cursor-pointer"
            >
              +{notifications.length - 1} more
            </button>
          )}
        </div>
      )}

      {/* Live poll bar */}
      {activePoll && !activePoll.ended && (
        <div className="bg-purple-950/80 border-b border-purple-500/40 px-3 py-2 shrink-0">
          <div className="flex flex-wrap items-center gap-2 mb-1.5">
            <Vote className="w-4 h-4 text-purple-400 shrink-0" />
            <span className="text-xs font-semibold text-purple-400 uppercase shrink-0">Live Poll</span>
            {pollElapsed !== null && (
              <span className="text-[10px] font-mono text-purple-300/70 shrink-0">
                {Math.floor(pollElapsed / 3600)}:{String(Math.floor((pollElapsed % 3600) / 60)).padStart(2, '0')}:{String(pollElapsed % 60).padStart(2, '0')}
              </span>
            )}
            <span className="text-sm font-medium text-white">{activePoll.question || 'Poll'}</span>
            {pollVotes.length > 0 && (
              <span className="text-[10px] font-mono text-purple-300/60 shrink-0 ml-auto">
                {pollVotes.reduce((s, v) => s + (v.score || 0), 0).toLocaleString()}t total
              </span>
            )}
          </div>
          {pollVotes.length > 0 && (
            <div className="flex gap-2">
              {(() => {
                const total = pollVotes.reduce((s, v) => s + (v.score || 0), 0) || 1
                const maxScore = Math.max(...pollVotes.map(v => v.score || 0))
                return pollVotes.map((v, i) => {
                  const pct = Math.round((v.score || 0) / total * 100)
                  const isLeading = (v.score || 0) === maxScore && maxScore > 0
                  return (
                    <div key={v.value} className="flex-1">
                      <div className="flex items-center justify-between text-[10px] mb-0.5">
                        <span className={`font-medium truncate ${isLeading ? 'text-white' : 'text-purple-200/70'}`}>{v.value}</span>
                        <span className={`font-mono ml-1 ${isLeading ? 'text-purple-300' : 'text-purple-400/60'}`}>
                          {(v.score || 0).toLocaleString()}t ({pct}%)
                        </span>
                      </div>
                      <div className="h-2 bg-purple-900/50 rounded-full overflow-hidden">
                        <div
                          className={`h-full rounded-full transition-all ${isLeading ? 'bg-purple-400' : 'bg-purple-500/50'}`}
                          style={{ width: `${pct}%` }}
                        />
                      </div>
                    </div>
                  )
                })
              })()}
            </div>
          )}
        </div>
      )}

      {/* Poll result (briefly shown after close) */}
      {activePoll && activePoll.ended && (
        <div className="bg-purple-500/5 border-b border-purple-500/20 px-3 py-1.5 flex items-center gap-2 shrink-0">
          <Vote className="w-4 h-4 text-purple-400/60 shrink-0" />
          <span className="text-xs font-semibold text-purple-400/60 uppercase shrink-0">Poll Ended</span>
          <span className="text-sm text-tank-muted">{activePoll.question}</span>
          {activePoll.winner && (
            <span className="text-sm font-semibold text-purple-400">Winner: {activePoll.winner}</span>
          )}
        </div>
      )}

      {activeTab === 'dashboard' && (
      <main className="flex-1 flex flex-col md:flex-row gap-2 p-2 min-h-0">
        {/* LEFT: Fishtoys panel */}
        <div className="w-full md:w-[420px] md:shrink-0 flex flex-col bg-tank-surface border border-tank-border rounded-lg overflow-hidden">
          {/* Filter bar */}
          <div className="border-b border-tank-border p-2 space-y-1.5 shrink-0">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Fish className="w-4 h-4 text-tank-accent" />
                <span className="text-xs font-semibold text-tank-bright uppercase tracking-wider">Fishtoys</span>
                <span className="text-[10px] font-mono text-tank-muted bg-tank-highlight px-1.5 py-0.5 rounded">
                  {hasActiveFilters ? `${filteredFishtoys.length} / ${stats.fishtoys}` : stats.fishtoys}
                </span>
              </div>
              {hasActiveFilters && (
                <button onClick={clearFilters} className="flex items-center gap-1 text-[10px] text-tank-danger hover:text-red-400 font-mono">
                  <X className="w-3 h-3" /> Clear filters
                </button>
              )}
            </div>
            <div className="flex gap-1">
              {[null, 'FISHTOY', 'BIGTOY'].map(cat => (
                <button
                  key={cat || 'all'}
                  onClick={() => setFilterCategory(cat)}
                  className={`text-[10px] font-mono px-2 py-0.5 rounded border transition-colors ${
                    filterCategory === cat
                      ? cat === 'BIGTOY'
                        ? 'border-purple-400 bg-purple-500/10 text-purple-400'
                        : 'border-tank-accent bg-tank-accent/10 text-tank-accent'
                      : 'border-tank-border text-tank-muted hover:border-tank-muted'
                  }`}
                >
                  {cat || 'All'}
                </button>
              ))}
            </div>
            <div className="flex gap-1.5">
              <div className="relative flex-1">
                <Search className="w-3 h-3 text-tank-muted absolute left-2 top-1/2 -translate-y-1/2" />
                <input
                  type="text"
                  placeholder="Search metadata / sender..."
                  value={searchText}
                  onChange={e => setSearchText(e.target.value)}
                  className="w-full bg-tank-bg border border-tank-border rounded text-xs text-tank-text pl-7 pr-2 py-1 font-mono placeholder:text-tank-muted/50 focus:border-tank-accent/50 focus:outline-none"
                />
              </div>
              <select
                value={filterItemId || ''}
                onChange={e => setFilterItemId(e.target.value || null)}
                className="bg-tank-bg border border-tank-border rounded text-xs text-tank-text px-1.5 py-1 font-mono focus:border-tank-accent/50 focus:outline-none max-w-[140px]"
              >
                <option value="">All types</option>
                {seenItemTypes.map(([id, name]) => (
                  <option key={id} value={id}>{name}</option>
                ))}
              </select>
            </div>
            {filterTarget && (
              <div className="flex items-center gap-1.5">
                <span className="text-[10px] font-mono text-tank-muted">Target:</span>
                <span className="text-[10px] font-mono text-tank-warn bg-tank-warn/10 px-1.5 py-0.5 rounded flex items-center gap-1">
                  {filterTarget}
                  <button onClick={() => setFilterTarget(null)} className="hover:text-tank-danger">
                    <X className="w-2.5 h-2.5" />
                  </button>
                </span>
              </div>
            )}
          </div>
          {/* Fishtoy list */}
          <div className="flex-1 min-h-0">
            {filteredFishtoys.length === 0 ? (
              <div className="p-2">
                <EmptyState text={hasActiveFilters ? "No fishtoys match filters" : "Waiting for fishtoy events..."} />
              </div>
            ) : (
              <Virtuoso
                style={{ height: '100%' }}
                data={filteredFishtoys}
                endReached={loadMoreFishtoys}
                overscan={200}
                itemContent={(index, item) => (
                  <div className="px-2 py-0.5">
                    <FishtoyCard
                      data={item.data}
                      eventType={item.event}
                      itemCatalog={itemCatalog}
                      onTargetClick={setFilterTarget}
                    />
                  </div>
                )}
                components={{
                  Footer: () => fishtoyLoading ? (
                    <div className="text-center text-[10px] text-tank-muted py-2">Loading...</div>
                  ) : !fishtoyHasMore ? (
                    <div className="text-center text-[10px] text-tank-muted py-2">No more fishtoys</div>
                  ) : null
                }}
              />
            )}
          </div>
        </div>

        {/* CENTER: Everything else */}
        <div className="flex-1 flex flex-col gap-2 min-w-0">

          {/* Top row: Targets + Target detail */}
          <div className="shrink-0 flex flex-col gap-2">
            {/* Target pills */}
            {seenTargets.length > 0 && (
              <div className="bg-tank-surface border border-tank-border rounded-lg p-2.5">
                <div className="flex items-center gap-2 mb-1.5">
                  <h3 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider">Targets</h3>
                </div>
                <div className="flex flex-wrap gap-1">
                  {seenTargets.map(({ target, count, spend }) => {
                    const isActive = filterTarget === target
                    const contestant = contestants.find(c => c.name === target)
                    return (
                      <button
                        key={target}
                        onClick={() => setFilterTarget(isActive ? null : target)}
                        className={`text-[11px] font-medium px-2 py-0.5 rounded-full border transition-colors ${
                          isActive
                            ? 'border-tank-accent bg-tank-accent/10 text-tank-accent'
                            : 'border-tank-border hover:border-tank-muted text-tank-text'
                        }`}
                        style={contestant?.color && !isActive ? { borderColor: contestant.color + '40' } : undefined}
                        title={`${count} fishtoys, ${spend.toLocaleString()} tokens (${tokensToUSD(spend)})`}
                      >
                        {target}
                        <span className="text-[9px] text-tank-muted ml-1">{count}</span>
                      </button>
                    )
                  })}
                </div>
              </div>
            )}

            {/* Target detail (when selected) */}
            {filterTarget && targetStats && (
              <div className="bg-tank-surface border border-tank-accent/30 rounded-lg p-2.5">
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-2">
                    <button onClick={() => setFilterTarget(null)} className="text-tank-muted hover:text-tank-bright">
                      <X className="w-3.5 h-3.5" />
                    </button>
                    <span className="text-sm font-bold text-tank-accent">{filterTarget}</span>
                  </div>
                  <div className="flex flex-wrap items-center gap-1 sm:gap-3 text-[10px] font-mono">
                    <span className="text-tank-accent">{targetStats.total} fishtoys</span>
                    <span className="text-tank-warn">{targetStats.totalSpend.toLocaleString()} tokens ({tokensToUSD(targetStats.totalSpend)})</span>
                    {targetStats.withMeta > 0 && (
                      <span className="text-tank-accent">{targetStats.withMeta} with content</span>
                    )}
                  </div>
                </div>
                <div className="flex flex-col md:flex-row gap-4">
                  {/* Items used */}
                  {targetStats.topItems.length > 0 && (
                    <div className="flex-1 min-w-0">
                      <h4 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider mb-1">Items used</h4>
                      <div className="space-y-0.5">
                        {targetStats.topItems.map(item => {
                          const isActive = filterItemId === item.id
                          return (
                            <button
                              key={item.id}
                              onClick={() => setFilterItemId(isActive ? null : item.id)}
                              className={`w-full flex items-center justify-between text-left px-1.5 py-0.5 rounded text-xs transition-colors ${
                                isActive
                                  ? 'bg-tank-accent/10 text-tank-accent'
                                  : 'hover:bg-tank-highlight text-tank-text'
                              }`}
                            >
                              <span className="truncate">{item.name}</span>
                              <div className="flex items-center gap-2 shrink-0 ml-2">
                                <span className="font-mono text-tank-muted">{item.count}</span>
                                <span className="font-mono text-tank-warn text-[10px]">{item.spend.toLocaleString()}t ({tokensToUSD(item.spend)})</span>
                              </div>
                            </button>
                          )
                        })}
                      </div>
                    </div>
                  )}
                  {/* Top senders to this target */}
                  {targetStats.topSenders.length > 0 && (
                    <div className="w-full md:w-[180px] md:shrink-0">
                      <h4 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider mb-1">Top senders</h4>
                      <div className="space-y-0.5">
                        {targetStats.topSenders.slice(0, 5).map((s, i) => (
                          <div key={s.name} className="flex items-center justify-between text-xs">
                            <div className="flex items-center gap-1">
                              <span className="text-[10px] font-mono text-tank-muted w-3">{i + 1}.</span>
                              <span className="text-tank-bright truncate">{s.name}</span>
                            </div>
                            <span className="font-mono text-tank-accent shrink-0 ml-1">{s.count}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>

          {/* STO-X ticker */}
          {stocks.length > 0 && (
            <div className="bg-tank-surface border border-tank-border rounded-lg p-2 shrink-0">
              <div className="flex items-center gap-2 mb-1.5">
                <h3 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider">STO-X</h3>
              </div>
              <div className="flex gap-2 overflow-x-auto">
                {[...stocks].sort((a, b) => b.currentPrice - a.currentPrice).map(s => {
                  const change = s.currentPrice - s.today
                  const changePct = s.today > 0 ? ((change / s.today) * 100).toFixed(1) : 0
                  const isUp = change > 0
                  const isDown = change < 0
                  return (
                    <div
                      key={s.tickerSymbol}
                      className={`flex flex-col items-center px-3 py-1.5 rounded border min-w-[80px] ${
                        filterTarget === s.tickerSymbol
                          ? 'border-tank-accent bg-tank-accent/5'
                          : 'border-tank-border'
                      }`}
                      role="button"
                      onClick={() => setFilterTarget(filterTarget === s.tickerSymbol ? null : s.tickerSymbol)}
                      title={`IPO: ${s.ipoPrice} | Avg: ${s.averagePrice} | Last week: ${s.lastWeek}`}
                    >
                      <span className="text-[11px] font-bold text-tank-bright">{s.tickerSymbol}</span>
                      <span className="text-sm font-mono text-tank-bright">{s.currentPrice}</span>
                      <span className={`text-[10px] font-mono ${isUp ? 'text-green-400' : isDown ? 'text-red-400' : 'text-tank-muted'}`}>
                        {isUp ? '+' : ''}{change} ({isUp ? '+' : ''}{changePct}%)
                      </span>
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {/* Fishtoy Availability pills */}
          {fishtoyStatus.length > 0 && (
            <div className="bg-tank-surface border border-tank-border rounded-lg p-2.5 shrink-0">
              <div className="flex items-center justify-between mb-1.5">
                <div className="flex items-center gap-2">
                  <Package className="w-4 h-4 text-tank-muted" />
                  <h3 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider">Fishtoy Availability</h3>
                  <span className="text-[10px] font-mono text-tank-muted bg-tank-highlight px-1.5 py-0.5 rounded">
                    {fishtoyStatus.filter(f => f.enabled).length}/{fishtoyStatus.length}
                  </span>
                </div>
                <div className="flex gap-1">
                  {['enabled', 'all', 'disabled'].map(f => (
                    <button
                      key={f}
                      onClick={() => setFishtoyFilter(f)}
                      className={`text-[9px] font-mono px-1.5 py-0.5 rounded transition-colors ${
                        fishtoyFilter === f
                          ? 'bg-tank-accent/20 text-tank-accent border border-tank-accent/40'
                          : 'text-tank-muted hover:text-tank-text'
                      }`}
                    >
                      {f.charAt(0).toUpperCase() + f.slice(1)}
                    </button>
                  ))}
                </div>
              </div>
              {featureToggles.fishtoys && !featureToggles.fishtoys.enabled && (
                <div className="text-[10px] text-red-400 font-mono mb-1.5 p-1.5 bg-red-500/5 border border-red-500/20 rounded">
                  Fishtoys globally disabled by production
                </div>
              )}
              <div className="flex flex-wrap gap-1">
                {[...fishtoyStatus]
                  .filter(item => fishtoyFilter === 'all' || (fishtoyFilter === 'enabled' ? item.enabled : !item.enabled))
                  .sort((a, b) => (a.name || '').localeCompare(b.name || ''))
                  .map(item => {
                    const isActive = filterItemId === String(item.id)
                    return (
                      <button
                        key={item.id}
                        onClick={() => setFilterItemId(isActive ? null : String(item.id))}
                        className={`text-[10px] font-medium px-2 py-1 rounded border transition-colors ${
                          isActive
                            ? 'border-tank-accent bg-tank-accent/10 text-tank-accent'
                            : item.enabled
                              ? 'border-green-500/30 hover:border-green-400/60 text-tank-text'
                              : 'border-red-500/30 hover:border-red-400/40 text-tank-muted'
                        }`}
                      >
                        {item.name}
                        <span className="text-[9px] text-tank-warn ml-1">{item.cost}t</span>
                        {item.type === 'BIGTOY' && (
                          <Star className={`w-2.5 h-2.5 inline ml-0.5 ${isActive ? 'text-tank-accent' : 'text-purple-400'}`} />
                        )}
                      </button>
                    )
                  })}
              </div>
            </div>
          )}

          {/* Info grid: Director Messages, Poll History, System Events */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-2 shrink-0">
            {/* Director Messages */}
            <div ref={directorRef} className="bg-tank-surface border border-tank-border rounded-lg p-2.5">
              <div className="flex items-center justify-between mb-1.5">
                <div className="flex items-center gap-2">
                  <Bell className="w-3.5 h-3.5 text-yellow-400" />
                  <h3 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider">Director Messages</h3>
                  <span className="text-[10px] font-mono text-tank-muted bg-tank-highlight px-1.5 py-0.5 rounded">
                    {notifications.length}
                  </span>
                </div>
                <div className="flex gap-0.5">
                  {[
                    { id: '1h', label: '1h' },
                    { id: '6h', label: '6h' },
                    { id: '24h', label: '24h' },
                    { id: 'all', label: '\u221E' },
                  ].map(t => (
                    <button
                      key={t.id}
                      onClick={() => setDirectorTimeRange(t.id)}
                      className={`text-[9px] font-mono px-1 py-0.5 rounded transition-colors ${
                        directorTimeRange === t.id
                          ? 'bg-yellow-500/20 text-yellow-400 border border-yellow-500/40'
                          : 'text-tank-muted hover:text-tank-text'
                      }`}
                    >
                      {t.label}
                    </button>
                  ))}
                </div>
              </div>
              {(() => {
                let filtered = notifications
                if (directorTimeRange !== 'all') {
                  const hours = { '1h': 1, '6h': 6, '24h': 24 }[directorTimeRange]
                  if (hours) {
                    const cutoff = Date.now() - hours * 3600000
                    filtered = filtered.filter(n => Date.parse(n.timestamp) >= cutoff)
                  }
                }
                return filtered.length > 0 ? (
                  <div className="space-y-1 max-h-[150px] overflow-y-auto">
                    {filtered.map(n => (
                      <div key={n.id} className="flex items-start gap-1.5 p-1.5 bg-yellow-500/5 border border-yellow-500/20 rounded">
                        <div className="min-w-0 flex-1">
                          <p className="text-xs text-tank-bright break-words">{n.message}</p>
                          <span className="text-[9px] font-mono text-tank-muted">{formatDateTime(n.timestamp)}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="text-[10px] text-tank-muted font-mono">
                    {directorTimeRange !== 'all' ? `No messages in last ${directorTimeRange}` : 'No director messages yet'}
                  </div>
                )
              })()}
            </div>

            {/* Poll History */}
            <div className="bg-tank-surface border border-tank-border rounded-lg p-2.5">
              <div className="flex items-center gap-2 mb-1.5">
                <Vote className="w-3.5 h-3.5 text-purple-400" />
                <h3 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider">Poll History</h3>
              </div>
              {polls.length > 0 ? (
                <div className="space-y-1.5 max-h-[150px] overflow-y-auto">
                  {polls.map(p => {
                    const d = p.data || {}
                    const pollInfo = d.poll || d
                    const question = pollInfo.question
                    const pid = pollInfo.pid
                    // Use live vote tallies for the active poll (#46)
                    const isActivePoll = p.event_type === 'poll:start' && activePoll && !activePoll.ended && pid && pid === activePoll.pid
                    const votes = isActivePoll ? pollVotes : (d.votes || d.scores || [])
                    const total = votes.reduce((s, v) => s + (v.score || 0), 0) || 1
                    return (
                      <div key={p.id} className={`p-1.5 rounded border ${
                        p.event_type === 'poll:stop'
                          ? 'border-purple-500/30 bg-purple-500/5'
                          : isActivePoll
                            ? 'border-purple-400/50 bg-purple-500/10'
                            : 'border-tank-border bg-tank-bg'
                      }`}>
                        <div className="flex items-center justify-between mb-0.5">
                          <span className={`text-[9px] font-mono px-1 py-0.5 rounded ${
                            p.event_type === 'poll:stop'
                              ? 'bg-purple-500/10 text-purple-400'
                              : isActivePoll
                                ? 'bg-purple-500/20 text-purple-300'
                                : 'bg-tank-highlight text-tank-muted'
                          }`}>
                            {p.event_type === 'poll:stop' ? 'RESULT' : isActivePoll ? 'LIVE' : 'STARTED'}
                          </span>
                          <div className="flex items-center gap-1.5">
                            {isActivePoll && pollElapsed !== null && (
                              <span className="text-[9px] font-mono text-purple-300/70">
                                {Math.floor(pollElapsed / 3600)}:{String(Math.floor((pollElapsed % 3600) / 60)).padStart(2, '0')}:{String(pollElapsed % 60).padStart(2, '0')}
                              </span>
                            )}
                            <span className="text-[9px] font-mono text-tank-muted">{formatDateTime(p.timestamp_local)}</span>
                          </div>
                        </div>
                        {question && <p className="text-[11px] text-tank-bright mb-0.5">{question}</p>}
                        {votes.length > 0 && (
                          <div className="space-y-0.5">
                            {[...votes].sort((a, b) => (b.score || 0) - (a.score || 0)).map((v, i) => (
                              <div key={v.value} className="flex items-center gap-1">
                                <span className="text-[9px] w-3 shrink-0">{i === 0 ? '🥇' : i === 1 ? '🥈' : i === 2 ? '🥉' : `${i+1}.`}</span>
                                <div className="flex-1 min-w-0">
                                  <div className="flex items-center justify-between text-[9px]">
                                    <span className="text-tank-bright truncate">{v.value}</span>
                                    <span className="text-purple-400 font-mono ml-1">{v.score?.toLocaleString()}t ({Math.round(v.score / total * 100)}%)</span>
                                  </div>
                                  <div className="h-1 bg-tank-bg rounded-full overflow-hidden">
                                    <div className="h-full rounded-full bg-purple-400/70 transition-all" style={{ width: `${Math.round(v.score / total * 100)}%` }} />
                                  </div>
                                </div>
                              </div>
                            ))}
                          </div>
                        )}
                        {d.winner && (
                          <div className="text-[10px] mt-0.5">
                            Winner: <span className="font-semibold text-purple-400">{d.winner}</span>
                            {votes.length > 0 && <span className="text-tank-muted ml-1">({total.toLocaleString()} tokens)</span>}
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              ) : (
                <div className="text-[10px] text-tank-muted font-mono">No polls recorded yet</div>
              )}
            </div>

            {/* System Events */}
            <div className="bg-tank-surface border border-tank-border rounded-lg p-2.5">
              <div className="flex items-center justify-between mb-1.5">
                <div className="flex items-center gap-2">
                  <Zap className="w-3.5 h-3.5 text-tank-muted" />
                  <h3 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider">System Events</h3>
                </div>
                <div className="flex gap-0.5">
                  {[
                    { id: 'all', label: 'All' },
                    { id: 'toggle', label: 'Toggle' },
                    { id: 'stox', label: 'STO-X' },
                    { id: 'price', label: 'Price' },
                  ].map(f => (
                    <button
                      key={f.id}
                      onClick={() => setSystemFilter(f.id)}
                      className={`text-[9px] font-mono px-1 py-0.5 rounded transition-colors ${
                        systemFilter === f.id
                          ? 'bg-tank-accent/20 text-tank-accent border border-tank-accent/40'
                          : 'text-tank-muted hover:text-tank-text'
                      }`}
                    >
                      {f.label}
                    </button>
                  ))}
                </div>
              </div>
              {(() => {
                const filtered = systemFilter === 'all' ? systemEvents
                  : systemFilter === 'toggle' ? systemEvents.filter(e => e.event === 'feature-toggles:update')
                  : systemFilter === 'stox' ? systemEvents.filter(e => e.event?.startsWith('stock:'))
                  : systemEvents.filter(e => e.event === 'tts:price' || e.event === 'sfx:price')
                return filtered.length > 0 ? (
                  <div className="space-y-0.5 max-h-[150px] overflow-y-auto">
                    {filtered.map(e => {
                      const fmt = formatSystemEvent(e)
                      return (
                        <div key={e.dbId} className="flex items-center gap-1.5 text-[10px] p-1 bg-tank-bg rounded">
                          <span className={`font-mono text-[9px] px-1 py-0.5 rounded shrink-0 ${fmt.badgeClass}`}>
                            {fmt.badge}
                          </span>
                          <span className="text-tank-text flex-1 truncate">{fmt.message}</span>
                          {fmt.time && <span className="text-[9px] text-tank-muted font-mono shrink-0">{fmt.time}</span>}
                        </div>
                      )
                    })}
                  </div>
                ) : (
                  <div className="text-[10px] text-tank-muted font-mono">
                    {systemFilter !== 'all' ? `No ${systemFilter} events` : 'No system events yet'}
                  </div>
                )
              })()}
            </div>
          </div>

          {/* Bottom: Chat + Activity side by side */}
          <div className="flex-1 flex flex-col md:flex-row gap-2 min-h-0">
            <Panel
              title="Chat (Season Pass)"
              icon={MessageSquare}
              count={stats.chats}
              className="flex-1"
              extra={activeSuperchats.length > 0 ? (
                <span className="text-[9px] font-mono px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-400">
                  {activeSuperchats.length} pinned
                </span>
              ) : null}
            >
              {/* Pinned superchat banners */}
              {activeSuperchats.length > 0 && (
                <div className="space-y-1 mb-2">
                  {activeSuperchats.map(sc => {
                    const d = sc.data || {}
                    return (
                      <div key={sc.id || d.id} className="p-2 rounded border border-amber-500/30 bg-amber-500/5">
                        <div className="flex items-center gap-1.5 mb-0.5">
                          <span className="text-[9px] font-mono px-1 py-0.5 rounded bg-amber-500/10 text-amber-400">SC</span>
                          <span className="text-xs font-medium text-amber-300">{d.displayName || d.user?.displayName || d.username || d.userId || '?'}</span>
                          {d.cost > 0 && (
                            <span className="text-[10px] font-mono text-tank-warn">{d.cost}t</span>
                          )}
                          {(() => {
                            const key = String(sc.id || d.id)
                            const secs = scCountdowns[key]
                            if (secs != null) {
                              const m = Math.floor(secs / 60)
                              const s = String(secs % 60).padStart(2, '0')
                              return (
                                <span className={`text-[9px] font-mono ml-auto ${secs < 60 ? 'text-red-400' : 'text-amber-400/70'}`}>
                                  {m}:{s}
                                </span>
                              )
                            }
                            return d.duration ? (
                              <span className="text-[9px] font-mono text-tank-muted ml-auto">{d.duration}min</span>
                            ) : null
                          })()}
                        </div>
                        {d.message && (
                          <p className="text-xs text-tank-bright break-words">{d.message}</p>
                        )}
                      </div>
                    )
                  })}
                </div>
              )}
              {sortedChats.length === 0 ? (
                <EmptyState text="Waiting for chat messages..." />
              ) : (
                sortedChats.map((item) => (
                  <ChatMessage key={item.dbId || item.data?.id} data={item.data} />
                ))
              )}
            </Panel>

            <Panel
              title="Activity"
              icon={Radio}
              count={stats.tts + stats.sfx}
              className="w-full md:w-[340px] md:shrink-0"
              virtualized
              extra={
                <div className="flex items-center gap-2">
                  <div className="flex gap-0.5">
                    {[
                      { id: 'all', label: 'All' },
                      { id: 'tts', label: 'TTS' },
                      { id: 'sfx', label: 'SFX' },
                      { id: 'sc', label: 'SC' },
                    ].map(f => (
                      <button
                        key={f.id}
                        onClick={() => setActivityFilter(f.id)}
                        className={`text-[9px] font-mono px-1.5 py-0.5 rounded transition-colors ${
                          activityFilter === f.id
                            ? f.id === 'sc'
                              ? 'bg-amber-500/20 text-amber-400 border border-amber-500/40'
                              : 'bg-tank-accent/20 text-tank-accent border border-tank-accent/40'
                            : 'text-tank-muted hover:text-tank-text'
                        }`}
                      >
                        {f.label}
                      </button>
                    ))}
                  </div>
                  <div className="w-px h-3 bg-tank-border" />
                  <div className="flex gap-0.5">
                    {[
                      { id: '1h', label: '1h' },
                      { id: '6h', label: '6h' },
                      { id: '24h', label: '24h' },
                      { id: '7d', label: '7d' },
                      { id: 'all', label: '\u221E' },
                    ].map(t => (
                      <button
                        key={t.id}
                        onClick={() => setActivityTimeRange(t.id)}
                        className={`text-[9px] font-mono px-1 py-0.5 rounded transition-colors ${
                          activityTimeRange === t.id
                            ? 'bg-cyan-500/20 text-cyan-400 border border-cyan-500/40'
                            : 'text-tank-muted hover:text-tank-text'
                        }`}
                      >
                        {t.label}
                      </button>
                    ))}
                  </div>
                </div>
              }
            >
              {sortedActivity.length === 0 ? (
                <EmptyState text={activityFilter !== 'all' ? `No ${activityFilter.toUpperCase()} events yet` : "Waiting for TTS / SFX / events..."} />
              ) : (
                <Virtuoso
                  style={{ height: '100%' }}
                  data={sortedActivity}
                  endReached={loadMoreActivity}
                  overscan={200}
                  itemContent={(index, item) => (
                    <div className="px-2 py-0.5">
                      <ActivityCard data={item.data} eventType={item.event} roomMap={roomMap} />
                    </div>
                  )}
                  components={{
                    Footer: () => activityLoading ? (
                      <div className="text-center text-[10px] text-tank-muted py-2">Loading...</div>
                    ) : !activityHasMore ? (
                      <div className="text-center text-[10px] text-tank-muted py-2">No more events</div>
                    ) : null
                  }}
                />
              )}
            </Panel>
          </div>

        </div>

        {/* RIGHT: 24h sidebar */}
        <div className="w-full md:w-[280px] md:shrink-0 overflow-y-auto bg-tank-surface border border-tank-border rounded-lg p-2.5">
          <h3 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider mb-2">
            Last 24 Hours
          </h3>
          <div className="space-y-1.5">
            <StatRow label="Fishtoys" value={sessionStats.fishtoys} color="text-tank-accent" />
            <StatRow label="Chat" value={sessionStats.chats} color="text-blue-400" />
            <StatRow label="TTS" value={sessionStats.tts} color="text-purple-400" />
            <StatRow label="SFX" value={sessionStats.sfx} color="text-indigo-400" />
            {sessionStats.poll_tokens > 0 && (
              <StatRow label="Poll Votes" value={sessionStats.poll_tokens.toLocaleString()} color="text-cyan-400" />
            )}
            {sessionStats.superchat_tokens > 0 && (
              <StatRow label="Superchats" value={sessionStats.superchat_tokens.toLocaleString()} color="text-yellow-400" />
            )}
            <div className="w-full h-px bg-tank-border my-0.5" />
            <StatRow label="Tokens" value={sessionStats.total_spend.toLocaleString()} color="text-tank-warn" />
            <StatRow label="Est. Revenue" value={tokensToUSD(sessionStats.total_spend)} color="text-green-400" />
          </div>

          {/* Top Spenders (TTS, SFX & Fishtoys) */}
          {sessionStats.top_senders && sessionStats.top_senders.length > 0 && (
            <div className="border-t border-tank-border/50 pt-2 mt-2">
              <h4 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider mb-1.5">Top Spenders (TTS, SFX & Fishtoys)</h4>
              <div className="space-y-1">
                {sessionStats.top_senders.slice(0, 5).map((s, i) => (
                  <div key={s.name} className="flex items-center justify-between text-[11px]">
                    <div className="flex items-center gap-1">
                      <span className="text-[10px] font-mono text-tank-muted">{i + 1}.</span>
                      <span className="text-tank-bright truncate">{s.name}</span>
                    </div>
                    <div className="flex flex-col items-end shrink-0">
                      <span className="font-mono text-tank-warn text-[10px]">{s.spend.toLocaleString()}t</span>
                      <span className="font-mono text-green-400 text-[9px]">{tokensToUSD(s.spend)}</span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Top TTS */}
          {sessionStats.top_tts_senders && sessionStats.top_tts_senders.length > 0 && (
            <div className="border-t border-tank-border/50 pt-2 mt-2">
              <h4 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider mb-1.5">Top TTS</h4>
              <div className="space-y-1">
                {sessionStats.top_tts_senders.slice(0, 5).map((s, i) => (
                  <div key={s.name} className="flex items-center justify-between text-[11px]">
                    <div className="flex items-center gap-1">
                      <span className="text-[10px] font-mono text-tank-muted">{i + 1}.</span>
                      <span className="text-tank-bright truncate">{s.name}</span>
                    </div>
                    <div className="flex flex-col items-end shrink-0">
                      <span className="font-mono text-tank-muted text-[10px]">{s.count}x / {s.spend.toLocaleString()}t</span>
                      <span className="font-mono text-green-400 text-[9px]">{tokensToUSD(s.spend)}</span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Top SFX */}
          {sessionStats.top_sfx_senders && sessionStats.top_sfx_senders.length > 0 && (
            <div className="border-t border-tank-border/50 pt-2 mt-2">
              <h4 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider mb-1.5">Top SFX</h4>
              <div className="space-y-1">
                {sessionStats.top_sfx_senders.slice(0, 5).map((s, i) => (
                  <div key={s.name} className="flex items-center justify-between text-[11px]">
                    <div className="flex items-center gap-1">
                      <span className="text-[10px] font-mono text-tank-muted">{i + 1}.</span>
                      <span className="text-tank-bright truncate">{s.name}</span>
                    </div>
                    <div className="flex flex-col items-end shrink-0">
                      <span className="font-mono text-tank-muted text-[10px]">{s.count}x / {s.spend.toLocaleString()}t</span>
                      <span className="font-mono text-green-400 text-[9px]">{tokensToUSD(s.spend)}</span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Top Chat */}
          {sessionStats.top_chat_senders && sessionStats.top_chat_senders.length > 0 && (
            <div className="border-t border-tank-border/50 pt-2 mt-2">
              <h4 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider mb-1.5">Top Chat</h4>
              <div className="space-y-1">
                {sessionStats.top_chat_senders.slice(0, 5).map((s, i) => (
                  <div key={s.name} className="flex items-center justify-between text-[11px]">
                    <div className="flex items-center gap-1">
                      <span className="text-[10px] font-mono text-tank-muted">{i + 1}.</span>
                      <span className="text-tank-bright truncate">{s.name}</span>
                    </div>
                    <span className="font-mono text-blue-400 shrink-0 text-[10px]">{s.count} msg</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Top Fishtoy */}
          {sessionStats.top_fishtoy_senders && sessionStats.top_fishtoy_senders.length > 0 && (
            <div className="border-t border-tank-border/50 pt-2 mt-2">
              <h4 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider mb-1.5">Top Fishtoy</h4>
              <div className="space-y-1">
                {sessionStats.top_fishtoy_senders.slice(0, 5).map((s, i) => (
                  <div key={s.name} className="flex items-center justify-between text-[11px]">
                    <div className="flex items-center gap-1">
                      <span className="text-[10px] font-mono text-tank-muted">{i + 1}.</span>
                      <span className="text-tank-bright truncate">{s.name}</span>
                    </div>
                    <div className="flex flex-col items-end shrink-0">
                      <span className="font-mono text-tank-muted text-[10px]">{s.count}x / {s.spend.toLocaleString()}t</span>
                      <span className="font-mono text-green-400 text-[9px]">{tokensToUSD(s.spend)}</span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </main>
      )}

      {activeTab === 'analytics' && (
        <AnalyticsTab
          contestants={contestants}
          roomMap={roomMap}
          itemCatalog={itemCatalog}
          featureToggles={featureToggles}
        />
      )}

      {activeTab === 'charts' && (
        <ChartsTab stocks={stocks} />
      )}

      {activeTab === 'hidden' && (
        <HiddenContentTab itemCatalog={itemCatalog} />
      )}

      {activeTab === 'users' && (
        <UserSearchTab itemCatalog={itemCatalog} roomMap={roomMap} />
      )}
    </div>
  )
}

function StatRow({ label, value, color = 'text-tank-bright' }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-xs text-tank-muted">{label}</span>
      <span className={`text-sm font-mono font-semibold ${color}`}>{value}</span>
    </div>
  )
}

function EmptyState({ text }) {
  return (
    <div className="flex items-center justify-center h-full">
      <span className="text-xs text-tank-muted font-mono">{text}</span>
    </div>
  )
}
