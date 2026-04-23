import { useState, useEffect, useMemo, useRef, useCallback, memo, lazy, Suspense } from 'react'
import { Fish, MessageSquare, Radio, Search, X, BarChart3, FileText, Bell, Vote, User, Zap, TrendingUp, Crosshair, Box, Users, Clock, Trophy, Crown } from 'lucide-react'
import { Virtuoso } from 'react-virtuoso'
import { useWebSocket } from './useWebSocket'
import { formatDateTime } from './utils/formatTime'
import StatusBar from './components/StatusBar'
import Panel from './components/Panel'
import FishtoyCard from './components/FishtoyCard'
import ChatMessage from './components/ChatMessage'
import ActivityCard from './components/ActivityCard'
import { okJson, apiFetch } from './utils/fetchUtils'
import { tokenize } from './utils/tokenizer'
const AnalyticsTab = lazy(() => import('./tabs/AnalyticsTab'))
const ChartsTab = lazy(() => import('./tabs/ChartsTab'))
const HiddenContentTab = lazy(() => import('./tabs/HiddenContentTab'))
const UserSearchTab = lazy(() => import('./tabs/UserSearchTab'))

const MAX_EVENTS = 500

const FISHTOY_TYPES = new Set(['fishtoy:used'])
const CHAT_TYPES = new Set(['chat:message'])
const CHAT_FILTER_KEYS = { admin: 'isAdmin', mod: 'isMod', fish: 'isFish', gm: 'isGrandMarshall', epic: 'isEpic' }
const CHAT_FILTERS = [
  { id: 'all', label: 'All', color: 'bg-gray-500/20 text-gray-400 border border-gray-500/40' },
  { id: 'admin', label: 'Admin', color: 'bg-green-500/20 text-green-400 border border-green-500/40' },
  { id: 'mod', label: 'Mod', color: 'bg-sky-500/20 text-sky-400 border border-sky-500/40' },
  { id: 'fish', label: 'Fish', color: 'bg-pink-500/20 text-pink-400 border border-pink-500/40' },
  { id: 'gm', label: 'GM', color: 'bg-red-500/20 text-red-400 border border-red-500/40' },
  { id: 'epic', label: 'Epic', color: 'bg-amber-500/20 text-amber-400 border border-amber-500/40' },
]
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

function Sparkline({ data, color, width = 64, height = 20 }) {
  if (!data || data.length < 2) return null
  const min = Math.min(...data)
  const max = Math.max(...data)
  const range = max - min || 1
  const points = data.map((v, i) =>
    `${(i / (data.length - 1)) * width},${height - ((v - min) / range) * height}`
  ).join(' ')
  return (
    <svg width={width} height={height} className="shrink-0">
      <polyline points={points} fill="none" stroke={color} strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  )
}

export default function App() {
  const { isConnected, addListener, staleReconnect } = useWebSocket()
  const [serverVersion, setServerVersion] = useState(null)
  const [knownVersion, setKnownVersion] = useState(null)
  const [chats, setChats] = useState([])
  const [roleChats, setRoleChats] = useState(null)       // null = use chats, or server-fetched role-filtered array
  const [roleChatsLoading, setRoleChatsLoading] = useState(false)
  const [activity, setActivity] = useState([])
  const [stats, setStats] = useState({
    fishtoys: 0, chats: 0, tts: 0, sfx: 0, total_spend: 0, poll_tokens: 0, superchat_tokens: 0,
    top_targets: [], top_senders: [], top_tts_senders: [], top_sfx_senders: [], top_chat_senders: [], top_fishtoy_senders: [], total_events: 0,
  })
  const [sessionStats, setSessionStats] = useState({
    fishtoys: 0, chats: 0, tts: 0, sfx: 0, total_spend: 0, poll_tokens: 0, superchat_tokens: 0,
    top_targets: [], top_senders: [], top_tts_senders: [], top_sfx_senders: [], top_chat_senders: [], top_fishtoy_senders: [], total_events: 0,
  })
  const [topKeywords, setTopKeywords] = useState([])

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
  const [stoxRange, setStoxRange] = useState('today')
  const [stoxDeltas, setStoxDeltas] = useState({})
  const [stoxSparklines, setStoxSparklines] = useState({})
  const [activeTab, setActiveTab] = useState('dashboard')

  // Polls and notifications
  const [activePoll, setActivePoll] = useState(null)
  const [pollVotes, setPollVotes] = useState([])
  const [pollElapsed, setPollElapsed] = useState(null)
  const [notifications, setNotifications] = useState([])
  const [systemEvents, setSystemEvents] = useState([])
  const [featureToggles, setFeatureToggles] = useState({})
  const [polls, setPolls] = useState([])
  const [allTargets, setAllTargets] = useState([])
  const [activeSuperchats, setActiveSuperchats] = useState([])
  const [scCountdowns, setScCountdowns] = useState({})
  const [activityFilter, setActivityFilter] = useState('all')
  const [chatFilter, setChatFilter] = useState('all')
  const [chatRoom, setChatRoom] = useState('')
  const [systemFilter, setSystemFilter] = useState('all')
  const [directorTimeRange, setDirectorTimeRange] = useState('all')
  const [activityAnchor, setActivityAnchor] = useState(null)        // null = live, or ISO string
  const [activityAnchorLabel, setActivityAnchorLabel] = useState('now')
  const [activityHasMore, setActivityHasMore] = useState(true)
  const [activityHasNewer, setActivityHasNewer] = useState(false)
  const [activityLoading, setActivityLoading] = useState(false)
  const activityAnchorRef = useRef(null)
  const chatFilterRef = useRef('all')
  const directorRef = useRef(null)
  // Load catalog + historical data on mount AND after stale WebSocket reconnect
  // (staleReconnect increments when WS reconnects after >2min gap, e.g. overnight idle)
  const loadAllData = useCallback(() => {
    apiFetch('/api/items').then(okJson).then(setItemCatalog).catch(() => {})
    apiFetch('/api/contestants').then(okJson).then(setContestants).catch(() => {})
    apiFetch('/api/rooms').then(okJson).then(setRoomMap).catch(() => {})
    apiFetch('/api/stocks').then(okJson).then(setStocks).catch(() => {})
    apiFetch('/api/stats').then(okJson).then(raw => setStats(normalizeStats(raw))).catch(() => {})
    const since24h = new Date(Date.now() - 24 * 3600000).toISOString()
    apiFetch(`/api/stats?since=${encodeURIComponent(since24h)}`).then(okJson).then(raw => setSessionStats(normalizeStats(raw))).catch(() => {})
    apiFetch('/api/feature-toggles').then(okJson).then(setFeatureToggles).catch(() => {})
    apiFetch('/api/targets').then(okJson).then(setAllTargets).catch(() => {})
    apiFetch('/api/polls').then(okJson).then(setPolls).catch(() => {})
    apiFetch('/api/notifications?limit=500').then(okJson).then(data => {
      setNotifications(data.map(n => ({
        id: n.id,
        type: n.event_type,
        message: typeof n.data === 'string' ? n.data : n.data?.message || JSON.stringify(n.data),
        timestamp: n.timestamp_local,
      })))
    }).catch(() => {})

    // Activity fetched by feedApiParams effect (unified feed with server-side filters + pagination)

    // Fetch chat messages
    apiFetch('/api/events?type=chat:message&limit=500')
      .then(okJson)
      .then(events => {
        setChats(events.map(e => ({ event: e.event_type, data: e.data, dbId: e.id })))
      })
      .catch(() => {})

    // Fetch active superchats for pinned banners
    apiFetch('/api/superchats?limit=50')
      .then(okJson)
      .then(data => setActiveSuperchats(data.filter(sc => !sc.deleted)))
      .catch(() => {})

    // Activity (unified feed) fetched by feedApiParams effect below

    // Fetch system events separately so one type doesn't crowd out others
    Promise.all([
      apiFetch('/api/events?type=tts:price,sfx:price&limit=200').then(okJson).catch(() => []),
      apiFetch('/api/events?type=stock:update,stock:new,stock:remove,stock:split&limit=200').then(okJson).catch(() => []),
      apiFetch('/api/events?type=feature-toggles:update&limit=200').then(okJson).catch(() => []),
    ]).then(([prices, stocks, toggles]) => {
      const all = [...prices, ...stocks, ...toggles]
        .map(e => ({ event: e.event_type, data: e.data, dbId: e.id, timestamp: e.timestamp_local }))
        .sort((a, b) => (b.dbId || 0) - (a.dbId || 0))
      setSystemEvents(all)
    })

    // Reconstruct poll state from database
    apiFetch('/api/polls/latest')
      .then(okJson)
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
          if (Array.isArray(poll.votes)) {
            setPollVotes(poll.votes)
          }
        }
      })
      .catch(() => {})
  }, [])
  useEffect(loadAllData, [loadAllData, staleReconnect])

  // Fetch custom delta base prices and sparkline data when stoxRange changes
  useEffect(() => {
    if (['3h', '12h', '3d'].includes(stoxRange)) {
      apiFetch(`/api/stocks/delta?range=${stoxRange}`)
        .then(okJson).then(setStoxDeltas).catch(() => {})
    }
    apiFetch(`/api/stocks/sparklines?range=${stoxRange}`)
      .then(okJson).then(setStoxSparklines).catch(() => {})
  }, [stoxRange])

  // Sync anchor ref for WS listener (avoids re-registering listener on anchor change)
  useEffect(() => { activityAnchorRef.current = activityAnchor }, [activityAnchor])
  useEffect(() => { chatFilterRef.current = chatFilter }, [chatFilter])

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
        if (msg.data?.chatRoom) setChatRoom(msg.data.chatRoom)
        return
      }

      if (msg.event_type === 'chat:room') {
        setChatRoom(typeof msg.data === 'string' ? msg.data : '')
        return
      }

      const item = { event: msg.event_type, data: msg.data, dbId: msg.db_id }

      if (FISHTOY_TYPES.has(msg.event_type)) {
        if (!activityAnchorRef.current) setActivity(prev => [item, ...prev])
        const cost = msg.data?.cost || 0
        setStats(s => ({ ...s, fishtoys: s.fishtoys + 1, total_spend: s.total_spend + cost }))
        setSessionStats(s => ({ ...s, fishtoys: s.fishtoys + 1, total_spend: s.total_spend + cost }))
        // Incrementally update allTargets — use Map for O(1) lookup
        const target = msg.data?.target
        if (target) {
          setAllTargets(prev => {
            const map = new Map(prev.map(t => [t.target, t]))
            const existing = map.get(target)
            if (existing) {
              map.set(target, { ...existing, count: existing.count + 1, spend: existing.spend + cost })
            } else {
              map.set(target, { target, count: 1, spend: cost })
            }
            return [...map.values()].sort((a, b) => b.count - a.count)
          })
        }
      } else if (CHAT_TYPES.has(msg.event_type)) {
        setChats(prev => [item, ...prev].slice(0, MAX_EVENTS))
        // If a role filter is active, prepend to roleChats if the message matches
        const activeRole = chatFilterRef.current
        if (activeRole !== 'all') {
          const key = CHAT_FILTER_KEYS[activeRole]
          if (msg.data?.metadata?.[key]) {
            setRoleChats(prev => prev ? [item, ...prev] : [item])
          }
        }
        setStats(s => ({ ...s, chats: s.chats + 1 }))
        setSessionStats(s => ({ ...s, chats: s.chats + 1 }))
      } else if (ACTIVITY_TYPES.has(msg.event_type)) {
        if (!activityAnchorRef.current) setActivity(prev => [item, ...prev])
        const cost = msg.data?.cost || 0
        if (msg.event_type === 'tts:update') {
          setStats(s => ({ ...s, tts: s.tts + 1, total_spend: s.total_spend + cost }))
          setSessionStats(s => ({ ...s, tts: s.tts + 1, total_spend: s.total_spend + cost }))
        } else if (msg.event_type === 'sfx:update') {
          setStats(s => ({ ...s, sfx: s.sfx + 1, total_spend: s.total_spend + cost }))
          setSessionStats(s => ({ ...s, sfx: s.sfx + 1, total_spend: s.total_spend + cost }))
        }
      } else if (msg.event_type === 'super-chat:new') {
        const cost = msg.data?.cost || 0
        setSessionStats(s => ({ ...s, total_spend: s.total_spend + cost, superchat_tokens: s.superchat_tokens + cost }))
        setStats(s => ({ ...s, total_spend: s.total_spend + cost, superchat_tokens: s.superchat_tokens + cost }))
        // Add to activity feed
        if (!activityAnchorRef.current) setActivity(prev => [item, ...prev])
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
        apiFetch('/api/polls').then(okJson).then(setPolls).catch(() => {})
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
      apiFetch('/api/stats').then(okJson).then(raw => setStats(normalizeStats(raw))).catch(() => {})
      const since24h = new Date(Date.now() - 24 * 3600000).toISOString()
      apiFetch(`/api/stats?since=${encodeURIComponent(since24h)}`).then(okJson).then(raw => setSessionStats(normalizeStats(raw))).catch(() => {})
      apiFetch('/api/stocks').then(okJson).then(setStocks).catch(() => {})
      apiFetch('/api/feature-toggles').then(okJson).then(setFeatureToggles).catch(() => {})
    }, 60000)
    return () => clearInterval(interval)
  }, [])

  // Refresh top chat keywords periodically (24h window, used by sidebar)
  useEffect(() => {
    const fetchTop = () => {
      if (document.hidden) return
      apiFetch('/api/keywords/top').then(okJson).then(data => setTopKeywords(data || [])).catch(() => setTopKeywords([]))
    }
    fetchTop()
    const interval = setInterval(fetchTop, 60000)
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
  // Pre-compute expiry timestamps once per superchat set change
  const scExpiries = useMemo(() => {
    const map = new Map()
    for (const sc of activeSuperchats) {
      const d = sc.data || {}
      const dur = d.duration
      if (!dur) continue
      const created = d.createdAt || d.updatedAt || sc.timestamp_local
      const startMs = typeof created === 'number'
        ? (created > 1e12 ? created : created * 1000)
        : Date.parse(created)
      if (!startMs || isNaN(startMs)) continue
      map.set(String(sc.id || d.id), startMs + dur * 60000)
    }
    return map
  }, [activeSuperchats])

  useEffect(() => {
    if (activeSuperchats.length === 0) {
      setScCountdowns({})
      return
    }
    const tick = () => {
      const now = Date.now()
      const counts = {}
      const expiredSet = new Set()
      for (const [key, expiresAt] of scExpiries) {
        const remaining = Math.floor((expiresAt - now) / 1000)
        if (remaining <= 0) {
          expiredSet.add(key)
        } else {
          counts[key] = remaining
        }
      }
      setScCountdowns(counts)
      if (expiredSet.size > 0) {
        setActiveSuperchats(prev => prev.filter(sc => {
          const key = String(sc.id || sc.data?.id)
          return !expiredSet.has(key)
        }))
      }
    }
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [scExpiries])

  // Debounce search text for server fetches (300ms)
  const [debouncedSearch, setDebouncedSearch] = useState('')
  useEffect(() => {
    const id = setTimeout(() => setDebouncedSearch(searchText.trim()), 300)
    return () => clearTimeout(id)
  }, [searchText])

  // Build unified feed API URL from current filters
  const feedApiParams = useMemo(() => {
    const p = new URLSearchParams()
    p.set('limit', '500')
    const typeMap = {
      all: 'fishtoy:used,tts:update,sfx:update,happening,super-chat:new',
      fishtoys: 'fishtoy:used',
      tts: 'tts:update',
      sfx: 'sfx:update',
      sc: 'super-chat:new',
    }
    p.set('type', typeMap[activityFilter] || typeMap.all)
    if (filterTarget) p.set('target', filterTarget)
    if (filterItemId) p.set('item_id', filterItemId)
    if (debouncedSearch) p.set('search', debouncedSearch)
    if (activityAnchor) p.set('around_ts', activityAnchor)
    return p.toString()
  }, [activityFilter, filterTarget, filterItemId, debouncedSearch, activityAnchor])

  // Re-fetch unified feed when filters change or after stale reconnect
  useEffect(() => {
    setActivityHasMore(true)
    setActivityHasNewer(!!activityAnchor)
    setFirstActivityIndex(10000)
    apiFetch(`/api/events?${feedApiParams}`)
      .then(okJson)
      .then(events => {
        setActivity(events.map(e => ({ event: e.event_type, data: e.data, dbId: e.id })))
        if (events.length < 500) setActivityHasMore(false)
      })
      .catch(() => {})
  }, [feedApiParams, staleReconnect])

  // Fetch role-filtered chats from server when a role filter is active
  useEffect(() => {
    if (chatFilter === 'all') { setRoleChats(null); setRoleChatsLoading(false); return }
    const ac = new AbortController()
    setRoleChats([])
    setRoleChatsLoading(true)
    apiFetch(`/api/events?type=chat:message&limit=500&role=${chatFilter}`, { signal: ac.signal })
      .then(okJson)
      .then(events => { setRoleChats(events.map(e => ({ event: e.event_type, data: e.data, dbId: e.id }))); setRoleChatsLoading(false) })
      .catch(() => setRoleChatsLoading(false))
    return () => ac.abort()
  }, [chatFilter])

  // Chat array is already newest-first (server ORDER BY id DESC + WS prepend)
  const sortedChats = useMemo(() => roleChats ?? chats, [roleChats, chats])
  const chatKeywordTags = useMemo(() => {
    const counts = new Map()
    for (const chat of sortedChats) {
      const text = typeof chat.data === 'string' ? chat.data : chat.data?.message
      if (!text) continue
      const words = tokenize(text)
      for (const w of words) {
        counts.set(w, (counts.get(w) || 0) + 1)
      }
    }
    return Array.from(counts.entries())
      .sort((a, b) => b[1] - a[1])
      .slice(0, 5)
      .map(([word, count]) => ({ word, count }))
  }, [sortedChats])
  const sortedActivity = useMemo(() => {
    const q = searchText.trim().toLowerCase()

    const filtered = activity.filter(a => {
      // Type filter
      if (activityFilter === 'fishtoys' && a.event !== 'fishtoy:used') return false
      if (activityFilter === 'tts' && a.event !== 'tts:update') return false
      if (activityFilter === 'sfx' && a.event !== 'sfx:update') return false
      if (activityFilter === 'sc' && a.event !== 'super-chat:new') return false
      // Fishtoy-specific filters
      if (a.event === 'fishtoy:used') {
        if (filterCategory) {
          const cat = itemCatalog[String(a.data?.itemId || '')]
          if (cat?.type !== filterCategory) return false
        }
        if (filterTarget && a.data?.target !== filterTarget) return false
        if (filterItemId && String(a.data?.itemId) !== String(filterItemId)) return false
      }
      // Search filter (applies to all event types)
      if (q) {
        const d = a.data || {}
        const name = d.displayName || ''
        const meta = d.metadata ? String(d.metadata) : ''
        const message = d.message || ''
        if (!name.toLowerCase().includes(q) &&
            !meta.toLowerCase().includes(q) &&
            !message.toLowerCase().includes(q)) return false
      }
      return true
    })
    return filtered  // Already newest-first (server ORDER BY id DESC + WS prepend)
  }, [activity, activityFilter, filterCategory, filterTarget, filterItemId, searchText, itemCatalog])

  const loadMoreActivity = useCallback(() => {
    if (activityLoading || !activityHasMore) return
    const minId = activity.reduce((min, a) => {
      const id = a.dbId
      return id && (min === null || id < min) ? id : min
    }, null)
    if (minId === null) return
    setActivityLoading(true)
    apiFetch(`/api/events?${feedApiParams}&before_id=${minId}`)
      .then(okJson)
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
          if (events.length < 500) setActivityHasMore(false)
        }
      })
      .catch(() => {})
      .finally(() => setActivityLoading(false))
  }, [activity, activityLoading, activityHasMore, feedApiParams])

  // Load newer events (scroll up in historical mode)
  const [firstActivityIndex, setFirstActivityIndex] = useState(10000)
  const loadNewerActivity = useCallback(() => {
    if (activityLoading || !activityHasNewer) return
    const maxId = activity.reduce((max, a) => {
      const id = a.dbId
      return id && (max === null || id > max) ? id : max
    }, null)
    if (maxId === null) return
    setActivityLoading(true)
    // Strip around_ts from params for newer pagination (we want events after maxId)
    const p = new URLSearchParams(feedApiParams)
    p.delete('around_ts')
    apiFetch(`/api/events?${p.toString()}&since_id=${maxId}`)
      .then(okJson)
      .then(events => {
        if (events.length === 0) {
          setActivityHasNewer(false)
        } else {
          const newItems = events.map(e => ({ event: e.event_type, data: e.data, dbId: e.id }))
          setActivity(prev => {
            const existingIds = new Set(prev.map(a => a.dbId).filter(Boolean))
            const unique = newItems.filter(a => !existingIds.has(a.dbId))
            setFirstActivityIndex(i => i - unique.length)
            return [...unique, ...prev]
          })
          if (events.length < 500) setActivityHasNewer(false)
        }
      })
      .catch(() => {})
      .finally(() => setActivityLoading(false))
  }, [activity, activityLoading, activityHasNewer, feedApiParams])

  // Unique item types seen in fishtoys for filter dropdown
  const seenItemTypes = useMemo(() => {
    const map = new Map()
    for (const a of activity) {
      if (a.event !== 'fishtoy:used') continue
      const iid = String(a.data?.itemId || '')
      if (iid && !map.has(iid)) {
        const cat = itemCatalog[iid]
        map.set(iid, cat?.name || `Item #${iid}`)
      }
    }
    return Array.from(map.entries()).sort((a, b) => a[1].localeCompare(b[1]))
  }, [activity, itemCatalog])

  // Targets sourced from DB via /api/targets, incrementally updated from WS
  const seenTargets = allTargets

  // Target-specific stats fetched from server (full DB history)
  const [targetStats, setTargetStats] = useState(null)
  useEffect(() => {
    if (!filterTarget) { setTargetStats(null); return }
    apiFetch(`/api/target-stats?target=${encodeURIComponent(filterTarget)}`)
      .then(okJson)
      .then(data => {
        // Enrich item names from catalog
        if (data.topItems) {
          data.topItems = data.topItems.map(item => ({
            ...item,
            name: itemCatalog[String(item.id)]?.name || `Item #${item.id}`,
          }))
        }
        setTargetStats(data)
      })
      .catch(() => setTargetStats(null))
  }, [filterTarget, itemCatalog])

  const hasActiveFilters = filterTarget || filterCategory || filterItemId || searchText.trim()

  function clearFilters() {
    setFilterTarget(null)
    setFilterCategory(null)
    setFilterItemId(null)
    setSearchText('')
    setActivityFilter('all')
    setActivityAnchor(null)
    setActivityAnchorLabel('now')
  }

  // Memoized computed values for render
  const sortedStocks = useMemo(() =>
    [...stocks].sort((a, b) => b.currentPrice - a.currentPrice),
    [stocks]
  )
  const filteredDirectorMessages = useMemo(() => {
    if (directorTimeRange === 'all') return notifications
    const hours = { '1h': 1, '6h': 6, '24h': 24 }[directorTimeRange]
    if (!hours) return notifications
    const cutoff = Date.now() - hours * 3600000
    return notifications.filter(n => Date.parse(n.timestamp) >= cutoff)
  }, [notifications, directorTimeRange])
  const filteredSystemEvents = useMemo(() => {
    if (systemFilter === 'all') return systemEvents
    if (systemFilter === 'toggle') return systemEvents.filter(e => e.event === 'feature-toggles:update')
    if (systemFilter === 'stox') return systemEvents.filter(e => e.event?.startsWith('stock:'))
    return systemEvents.filter(e => e.event === 'tts:price' || e.event === 'sfx:price')
  }, [systemEvents, systemFilter])
  const POLL_COLORS = ['#c084fc', '#a78bfa', '#818cf8', '#7dd3fc', '#67e8f9', '#6ee7b7', '#fcd34d', '#fca5a5']
  const pollVoteBars = useMemo(() => {
    if (!Array.isArray(pollVotes) || pollVotes.length === 0) return null
    const total = pollVotes.reduce((s, v) => s + (v.score || 0), 0) || 1
    const maxScore = Math.max(...pollVotes.map(v => v.score || 0))
    // Pair options off: [0,1], [2,3], etc.
    const pairs = []
    for (let i = 0; i < pollVotes.length; i += 2) {
      pairs.push(pollVotes.slice(i, i + 2))
    }
    return (
      <div className="space-y-1 max-h-[56px] overflow-y-auto">
        {pairs.map((pair, pi) => (
          <div key={pi} className="flex items-stretch gap-2">
            {pair.map((v, vi) => {
              const idx = pi * 2 + vi
              const score = v.score || 0
              const pct = Math.round(score / total * 100)
              const isLeading = score === maxScore && maxScore > 0
              const color = POLL_COLORS[idx % POLL_COLORS.length]
              const isLeft = vi === 0
              const isSolo = pair.length === 1
              return (
                <div key={v.value} className={`flex-1 min-w-0 ${isSolo ? 'max-w-[50%]' : ''}`}>
                  {/* Bar */}
                  <div className="h-2.5 bg-purple-900/30 rounded-full overflow-hidden">
                    <div
                      className={`h-full rounded-full transition-all duration-500 ${isLeading ? 'shadow-[0_0_8px_rgba(192,132,252,0.4)]' : ''} ${!isLeft && !isSolo ? 'ml-auto' : ''}`}
                      style={{
                        width: `${Math.max(pct, 3)}%`,
                        background: isLeading
                          ? `linear-gradient(${isLeft ? '90deg' : '270deg'}, ${color}, ${color}cc)`
                          : `${color}44`,
                      }}
                    />
                  </div>
                  {/* Label row */}
                  <div className={`flex items-center gap-1 mt-0.5 ${!isLeft && !isSolo ? 'justify-end' : ''}`}>
                    {isLeading && <Crown className="w-2.5 h-2.5 text-yellow-400 shrink-0" />}
                    <span className={`text-[10px] truncate ${isLeading ? 'text-white font-medium' : 'text-purple-200/60'}`}>{v.value}</span>
                    <span className={`text-[9px] font-mono shrink-0 ${isLeading ? 'text-purple-200' : 'text-purple-400/50'}`}>
                      {score.toLocaleString()}t ({pct}%)
                    </span>
                  </div>
                </div>
              )
            })}
          </div>
        ))}
      </div>
    )
  }, [pollVotes])
  const handleTargetClick = useCallback((target) => {
    setFilterTarget(target)
    setActivityFilter('fishtoys')
  }, [])

  return (
    <div className="h-screen flex flex-col">
      <StatusBar isConnected={isConnected} stats={stats} updateAvailable={!!(serverVersion && knownVersion && serverVersion !== knownVersion)} />

      {/* Tab navigation */}
      <div className="bg-tank-surface border-b border-tank-border px-2 sm:px-3 flex items-center gap-0.5 sm:gap-1 shrink-0 overflow-x-auto">
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

      {/* Poll banner (live or ended) — fixed height, head-to-head layout */}
      {activePoll && (
        <div className={`border-b px-3 py-1.5 shrink-0 h-[88px] flex flex-col justify-center ${
          activePoll.ended
            ? 'bg-purple-500/5 border-purple-500/20'
            : 'bg-purple-950/80 border-purple-500/40'
        }`}>
          {/* Header row: badge, question, total */}
          <div className="flex items-center gap-2 mb-1 min-h-[20px]">
            <Vote className={`w-3.5 h-3.5 shrink-0 ${activePoll.ended ? 'text-purple-400/60' : 'text-purple-400'}`} />
            <span className={`text-[10px] font-mono uppercase shrink-0 px-1.5 py-0.5 rounded-full ${
              activePoll.ended
                ? 'bg-purple-500/10 text-purple-400/60'
                : 'bg-purple-500/20 text-purple-300 animate-pulse-glow'
            }`}>
              {activePoll.ended ? 'Ended' : 'Live'}
            </span>
            {!activePoll.ended && pollElapsed !== null && (
              <span className="text-[10px] font-mono text-purple-300/60 shrink-0 tabular-nums">
                {Math.floor(pollElapsed / 3600)}:{String(Math.floor((pollElapsed % 3600) / 60)).padStart(2, '0')}:{String(pollElapsed % 60).padStart(2, '0')}
              </span>
            )}
            <span className={`text-xs font-medium truncate ${activePoll.ended ? 'text-tank-muted' : 'text-white'}`}>
              {activePoll.question || 'Poll'}
            </span>
            {activePoll.ended && activePoll.winner && (
              <span className="flex items-center gap-1 shrink-0">
                <Crown className="w-3 h-3 text-yellow-400" />
                <span className="text-xs font-semibold text-purple-400">{activePoll.winner}</span>
              </span>
            )}
            {!activePoll.ended && pollVotes.length > 0 && (
              <span className="text-[9px] font-mono text-purple-300/50 shrink-0 ml-auto tabular-nums">
                {pollVotes.reduce((s, v) => s + (v.score || 0), 0).toLocaleString()}t
              </span>
            )}
          </div>
          {/* Vote bars */}
          {!activePoll.ended && pollVoteBars}
        </div>
      )}

      {activeTab === 'dashboard' && (
      <main className="flex-1 flex flex-col md:flex-row gap-2 p-2 min-h-0 overflow-y-auto md:overflow-hidden">
        {/* LEFT: Unified Activity panel */}
        <div className="w-full md:w-[240px] lg:w-[420px] md:shrink-0 flex flex-col bg-tank-surface border border-tank-border border-t-2 border-t-emerald-500/60 rounded-lg overflow-hidden min-h-[400px] md:min-h-0">
          {/* Filter bar */}
          <div className="border-b border-tank-border p-2 space-y-1.5 shrink-0">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Radio className="w-4 h-4 text-tank-accent" />
                <span className="text-xs font-semibold text-tank-bright uppercase tracking-wider">Activity</span>
                <span className="text-[10px] font-mono text-tank-muted bg-tank-highlight px-1.5 py-0.5 rounded">
                  {activityFilter === 'fishtoys'
                    ? hasActiveFilters ? `${sortedActivity.length} / ${stats.fishtoys}` : stats.fishtoys
                    : activityFilter === 'tts' ? stats.tts
                    : activityFilter === 'sfx' ? stats.sfx
                    : stats.fishtoys + stats.tts + stats.sfx}
                </span>
              </div>
              {hasActiveFilters && (
                <button onClick={clearFilters} className="flex items-center gap-1 text-[10px] text-tank-danger hover:text-red-400 font-mono">
                  <X className="w-3 h-3" /> Clear filters
                </button>
              )}
            </div>
            {/* Type + time range filters */}
            <div className="flex items-center gap-2">
              <div className="flex gap-0.5">
                {[
                  { id: 'all', label: 'All' },
                  { id: 'fishtoys', label: 'Fishtoys' },
                  { id: 'tts', label: 'TTS' },
                  { id: 'sfx', label: 'SFX' },
                  { id: 'sc', label: 'SC' },
                ].map(f => (
                  <button
                    key={f.id}
                    onClick={() => {
                      if (filterTarget && f.id !== 'all' && f.id !== 'fishtoys') {
                        setFilterTarget(null)
                        setFilterCategory(null)
                        setFilterItemId(null)
                        setSearchText('')
                      }
                      setActivityFilter(f.id)
                    }}
                    className={`text-[9px] font-mono px-1.5 py-0.5 rounded transition-colors ${
                      activityFilter === f.id
                        ? f.id === 'sc'
                          ? 'bg-amber-500/20 text-amber-400 border border-amber-500/40'
                          : f.id === 'fishtoys'
                            ? 'bg-cyan-500/20 text-cyan-400 border border-cyan-500/40'
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
                  { id: '30d', label: '30d', days: 30 },
                  { id: '10d', label: '10d', days: 10 },
                  { id: '7d', label: '7d', days: 7 },
                  { id: '3d', label: '3d', days: 3 },
                  { id: '1d', label: '1d', days: 1 },
                  { id: 'now', label: 'Now', days: null },
                ].map(t => (
                  <button
                    key={t.id}
                    onClick={() => {
                      if (t.days === null) {
                        setActivityAnchor(null)
                        setActivityAnchorLabel('now')
                      } else {
                        setActivityAnchor(new Date(Date.now() - t.days * 86400000).toISOString())
                        setActivityAnchorLabel(t.id)
                      }
                    }}
                    className={`text-[9px] font-mono px-1 py-0.5 rounded transition-colors ${
                      activityAnchorLabel === t.id
                        ? 'bg-cyan-500/20 text-cyan-400 border border-cyan-500/40'
                        : 'text-tank-muted hover:text-tank-text'
                    }`}
                  >
                    {t.label}
                  </button>
                ))}
              </div>
            </div>
            {/* Fishtoy sub-filters (shown when fishtoys visible) */}
            {(activityFilter === 'fishtoys' || activityFilter === 'all') && (
              <>
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
                    <option value="">All items</option>
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
              </>
            )}
          </div>
          {/* Unified activity list */}
          <div className="flex-1 min-h-0">
            {sortedActivity.length === 0 ? (
              <div className="p-2">
                <EmptyState text={hasActiveFilters || activityFilter !== 'all' ? "No events match filters" : "Waiting for events..."} />
              </div>
            ) : (
              <Virtuoso
                style={{ height: '100%' }}
                data={sortedActivity}
                firstItemIndex={firstActivityIndex}
                initialTopMostItemIndex={0}
                startReached={activityAnchor ? loadNewerActivity : undefined}
                endReached={loadMoreActivity}
                overscan={200}
                itemContent={(index, item) => (
                  <div className="px-2 py-0.5">
                    {item.event === 'fishtoy:used' ? (
                      <FishtoyCard
                        data={item.data}
                        eventType={item.event}
                        itemCatalog={itemCatalog}
                        onTargetClick={handleTargetClick}
                      />
                    ) : (
                      <ActivityCard data={item.data} eventType={item.event} roomMap={roomMap} />
                    )}
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
          </div>
        </div>

        {/* CENTER: Everything else */}
        <div className="flex-1 flex flex-col gap-2 min-w-0">

          {/* Top row: Targets + Target detail */}
          <div className="shrink-0 flex flex-col gap-2">
            {/* Target pills */}
            {seenTargets.length > 0 && (
              <div className="bg-tank-surface border border-tank-border border-t-2 border-t-red-500/60 rounded-lg p-2.5">
                <div className="flex items-center gap-2 mb-1.5">
                  <Crosshair className="w-3.5 h-3.5 text-tank-muted" />
                  <h3 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider">Targets</h3>
                </div>
                <div className="flex flex-wrap gap-1">
                  {seenTargets.map(({ target, count, spend }) => {
                    const isActive = filterTarget === target
                    const contestant = contestants.find(c => c.name === target)
                    return (
                      <button
                        key={target}
                        onClick={() => {
                          setFilterTarget(isActive ? null : target)
                          if (!isActive) setActivityFilter('fishtoys')
                        }}
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
                      <h4 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider mb-1 flex items-center gap-1.5"><Box className="w-3 h-3" />Items used</h4>
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
                      <h4 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider mb-1 flex items-center gap-1.5"><Users className="w-3 h-3" />Top senders</h4>
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
            <div className="bg-tank-surface border border-tank-border border-t-2 border-t-blue-500/60 rounded-lg p-2 shrink-0">
              <div className="flex items-center gap-2 mb-1.5">
                <TrendingUp className="w-3.5 h-3.5 text-blue-400" />
                <h3 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider">STO-X</h3>
                <div className="flex gap-0.5 ml-auto">
                  {[
                    { id: '1h', label: '1h' },
                    { id: '3h', label: '3h' },
                    { id: '12h', label: '12h' },
                    { id: 'today', label: 'Today' },
                    { id: '3d', label: '3d' },
                    { id: '1w', label: '1w' },
                    { id: 'ipo', label: 'IPO' },
                  ].map(t => (
                    <button
                      key={t.id}
                      onClick={() => setStoxRange(t.id)}
                      className={`text-[9px] font-mono px-1 py-0.5 rounded transition-colors ${
                        stoxRange === t.id
                          ? 'bg-blue-500/20 text-blue-400 border border-blue-500/40'
                          : 'text-tank-muted hover:text-tank-text'
                      }`}
                    >
                      {t.label}
                    </button>
                  ))}
                </div>
              </div>
              <div className="flex gap-2 overflow-x-auto">
                {sortedStocks.map(s => {
                  const apiFields = { '1h': 'lastHour', 'today': 'today', '1w': 'lastWeek', 'ipo': 'ipoPrice' }
                  const apiField = apiFields[stoxRange]
                  const base = apiField ? s[apiField] : stoxDeltas[s.tickerSymbol]
                  const change = base != null ? s.currentPrice - base : 0
                  const changePct = base > 0 ? ((change / base) * 100).toFixed(1) : 0
                  const isUp = change > 0
                  const isDown = change < 0
                  const isBigMover = Math.abs(parseFloat(changePct)) >= 10
                  return (
                    <div
                      key={s.tickerSymbol}
                      className={`flex flex-col items-center px-3 py-1.5 rounded border min-w-[80px] ${
                        filterTarget === s.tickerSymbol
                          ? 'border-tank-accent bg-tank-accent/5'
                          : isBigMover
                            ? isUp
                              ? 'border-green-500/60 bg-green-500/5 shadow-[0_0_8px_rgba(34,197,94,0.15)]'
                              : 'border-red-500/60 bg-red-500/5 shadow-[0_0_8px_rgba(239,68,68,0.15)]'
                            : 'border-tank-border'
                      }`}
                      role="button"
                      onClick={() => setFilterTarget(filterTarget === s.tickerSymbol ? null : s.tickerSymbol)}
                      title={`IPO: ${s.ipoPrice} | Avg: ${s.averagePrice} | Last week: ${s.lastWeek}`}
                    >
                      <span className="text-[11px] font-bold text-tank-bright">{s.tickerSymbol}</span>
                      <span className="text-sm font-mono text-tank-bright">{s.currentPrice}</span>
                      <Sparkline data={stoxSparklines[s.tickerSymbol]} color={isUp ? '#4ade80' : isDown ? '#f87171' : '#555566'} />
                      <span className={`text-[10px] font-mono ${isUp ? 'text-green-400' : isDown ? 'text-red-400' : 'text-tank-muted'}`}>
                        {isUp ? '+' : ''}{change} ({isUp ? '+' : ''}{changePct}%)
                      </span>
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {/* Info grid: Director Messages, Poll History, System Events */}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2 shrink-0">
            {/* Director Messages */}
            <div ref={directorRef} className="group bg-tank-surface border border-tank-border rounded-lg overflow-hidden transition-colors hover:border-yellow-500/30">
              <div className="flex items-center justify-between px-3 py-2 border-b border-tank-border bg-gradient-to-r from-yellow-500/5 to-transparent">
                <div className="flex items-center gap-2">
                  <Bell className="w-3.5 h-3.5 text-yellow-400" />
                  <h3 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider">Director Messages</h3>
                  <span className="text-[10px] font-mono text-yellow-400/70 bg-yellow-500/10 px-1.5 py-0.5 rounded-full min-w-[20px] text-center">
                    {notifications.length}
                  </span>
                </div>
                <div className="flex gap-0.5 bg-tank-bg/50 rounded-md p-0.5">
                  {[
                    { id: '1h', label: '1h' },
                    { id: '6h', label: '6h' },
                    { id: '24h', label: '24h' },
                    { id: 'all', label: '\u221E' },
                  ].map(t => (
                    <button
                      key={t.id}
                      onClick={() => setDirectorTimeRange(t.id)}
                      className={`text-[9px] font-mono px-1.5 py-0.5 rounded transition-all ${
                        directorTimeRange === t.id
                          ? 'bg-yellow-500/20 text-yellow-400 shadow-sm'
                          : 'text-tank-muted hover:text-yellow-400/70'
                      }`}
                    >
                      {t.label}
                    </button>
                  ))}
                </div>
              </div>
              <div className="p-2">
                {filteredDirectorMessages.length > 0 ? (
                  <div className="space-y-1.5 max-h-[160px] overflow-y-auto">
                    {filteredDirectorMessages.map(n => (
                      <div key={n.id} className="flex items-start gap-2 p-2 bg-yellow-500/[0.03] border border-yellow-500/15 rounded-md transition-colors hover:bg-yellow-500/[0.06]">
                        <div className="w-0.5 h-full min-h-[24px] bg-yellow-500/40 rounded-full shrink-0 self-stretch" />
                        <div className="min-w-0 flex-1">
                          <p className="text-xs text-tank-bright break-words leading-relaxed">{n.message}</p>
                          <span className="text-[9px] font-mono text-tank-muted mt-0.5 block">{formatDateTime(n.timestamp)}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="text-[10px] text-tank-muted font-mono py-3 text-center">
                    {directorTimeRange !== 'all' ? `No messages in last ${directorTimeRange}` : 'No director messages yet'}
                  </div>
                )}
              </div>
            </div>

            {/* Poll History */}
            <div className="group bg-tank-surface border border-tank-border rounded-lg overflow-hidden transition-colors hover:border-purple-500/30">
              <div className="flex items-center gap-2 px-3 py-2 border-b border-tank-border bg-gradient-to-r from-purple-500/5 to-transparent">
                <Vote className="w-3.5 h-3.5 text-purple-400" />
                <h3 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider">Poll History</h3>
                {polls.length > 0 && (
                  <span className="text-[10px] font-mono text-purple-400/70 bg-purple-500/10 px-1.5 py-0.5 rounded-full min-w-[20px] text-center">
                    {polls.length}
                  </span>
                )}
              </div>
              <div className="p-2">
                {polls.length > 0 ? (
                  <div className="space-y-1.5 max-h-[160px] overflow-y-auto">
                    {polls.map(p => {
                      const d = p.data || {}
                      const pollInfo = d.poll || d
                      const question = pollInfo.question
                      const pid = pollInfo.pid
                      // Use live vote tallies for the active poll (#46)
                      const isActivePoll = p.event_type === 'poll:start' && activePoll && !activePoll.ended && pid && pid === activePoll.pid
                      const votes = isActivePoll ? pollVotes : (d.votes || d.scores || [])
                      const total = votes.reduce((s, v) => s + (v.score || 0), 0) || 1
                      const sortedVotes = [...votes].sort((a, b) => (b.score || 0) - (a.score || 0))
                      const topScore = sortedVotes[0]?.score || 0
                      return (
                        <div key={p.id} className={`p-2 rounded-md border transition-colors ${
                          p.event_type === 'poll:stop'
                            ? 'border-purple-500/25 bg-purple-500/[0.04]'
                            : isActivePoll
                              ? 'border-purple-400/40 bg-purple-500/[0.08] shadow-[0_0_12px_rgba(168,85,247,0.08)]'
                              : 'border-tank-border bg-tank-bg/50'
                        }`}>
                          <div className="flex items-center justify-between mb-1.5">
                            <span className={`text-[9px] font-mono px-1.5 py-0.5 rounded-full ${
                              p.event_type === 'poll:stop'
                                ? 'bg-purple-500/15 text-purple-400'
                                : isActivePoll
                                  ? 'bg-purple-500/25 text-purple-300 animate-pulse-glow'
                                  : 'bg-tank-highlight text-tank-muted'
                            }`}>
                              {p.event_type === 'poll:stop' ? 'RESULT' : isActivePoll ? 'LIVE' : 'STARTED'}
                            </span>
                            <div className="flex items-center gap-1.5">
                              {isActivePoll && pollElapsed !== null && (
                                <span className="text-[9px] font-mono text-purple-300/70 tabular-nums">
                                  {Math.floor(pollElapsed / 3600)}:{String(Math.floor((pollElapsed % 3600) / 60)).padStart(2, '0')}:{String(pollElapsed % 60).padStart(2, '0')}
                                </span>
                              )}
                              <span className="text-[9px] font-mono text-tank-muted">{formatDateTime(p.timestamp_local)}</span>
                            </div>
                          </div>
                          {question && <p className="text-[11px] text-tank-bright mb-1.5 leading-relaxed">{question}</p>}
                          {sortedVotes.length > 0 && (
                            <div className="space-y-1">
                              {sortedVotes.map((v, i) => {
                                const score = v.score || 0
                                const pct = Math.round(score / total * 100)
                                const isWinner = score === topScore && topScore > 0
                                const color = POLL_COLORS[i % POLL_COLORS.length]
                                return (
                                  <div key={v.value}>
                                    <div className="flex items-center justify-between text-[9px] mb-0.5">
                                      <span className="flex items-center gap-1 min-w-0">
                                        {isWinner && <Crown className="w-2.5 h-2.5 text-yellow-400 shrink-0" />}
                                        <span className={`truncate ${isWinner ? 'text-white font-medium' : 'text-tank-bright'}`}>{v.value}</span>
                                      </span>
                                      <span className={`font-mono ml-2 flex items-center gap-1 shrink-0 tabular-nums ${isWinner ? 'text-purple-300' : 'text-purple-400/60'}`}>
                                        {score.toLocaleString()}t ({pct}%)
                                        {!isWinner && topScore > 0 && <span className="text-red-400/60 text-[8px]">-{(topScore - score).toLocaleString()}t</span>}
                                      </span>
                                    </div>
                                    <div className="h-1.5 bg-purple-900/20 rounded-full overflow-hidden">
                                      <div
                                        className={`h-full rounded-full transition-all duration-700 ease-out ${isWinner ? 'shadow-[0_0_8px_rgba(192,132,252,0.35)]' : ''}`}
                                        style={{
                                          width: `${Math.max(pct, 2)}%`,
                                          background: isWinner
                                            ? `linear-gradient(90deg, ${color}ee, ${color})`
                                            : `${color}33`,
                                        }}
                                      />
                                    </div>
                                  </div>
                                )
                              })}
                            </div>
                          )}
                          {d.winner && sortedVotes.length === 0 && (
                            <div className="text-[10px] mt-1.5 flex items-center gap-1.5">
                              <Crown className="w-3 h-3 text-yellow-400" />
                              <span className="text-tank-muted">Winner:</span>
                              <span className="font-semibold text-purple-400">{d.winner}</span>
                            </div>
                          )}
                        </div>
                      )
                    })}
                  </div>
                ) : (
                  <div className="text-[10px] text-tank-muted font-mono py-3 text-center">No polls recorded yet</div>
                )}
              </div>
            </div>

            {/* System Events */}
            <div className="group bg-tank-surface border border-tank-border rounded-lg overflow-hidden transition-colors hover:border-orange-500/30 sm:col-span-2 lg:col-span-1">
              <div className="flex items-center justify-between px-3 py-2 border-b border-tank-border bg-gradient-to-r from-orange-500/5 to-transparent">
                <div className="flex items-center gap-2">
                  <Zap className="w-3.5 h-3.5 text-orange-400" />
                  <h3 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider">System Events</h3>
                </div>
                <div className="flex gap-0.5 bg-tank-bg/50 rounded-md p-0.5">
                  {[
                    { id: 'all', label: 'All' },
                    { id: 'toggle', label: 'Toggle' },
                    { id: 'stox', label: 'STO-X' },
                    { id: 'price', label: 'Price' },
                  ].map(f => (
                    <button
                      key={f.id}
                      onClick={() => setSystemFilter(f.id)}
                      className={`text-[9px] font-mono px-1.5 py-0.5 rounded transition-all ${
                        systemFilter === f.id
                          ? 'bg-orange-500/20 text-orange-400 shadow-sm'
                          : 'text-tank-muted hover:text-orange-400/70'
                      }`}
                    >
                      {f.label}
                    </button>
                  ))}
                </div>
              </div>
              <div className="p-2">
                {filteredSystemEvents.length > 0 ? (
                  <div className="space-y-1 max-h-[160px] overflow-y-auto">
                    {filteredSystemEvents.map(e => {
                      const fmt = formatSystemEvent(e)
                      return (
                        <div key={e.dbId} className="flex items-center gap-2 text-[10px] px-2 py-1.5 bg-tank-bg/60 rounded-md transition-colors hover:bg-tank-bg">
                          <span className={`font-mono text-[9px] px-1.5 py-0.5 rounded shrink-0 ${fmt.badgeClass}`}>
                            {fmt.badge}
                          </span>
                          <span className="text-tank-text flex-1 truncate">{fmt.message}</span>
                          {fmt.time && <span className="text-[9px] text-tank-muted font-mono shrink-0 tabular-nums">{fmt.time}</span>}
                        </div>
                      )
                    })}
                  </div>
                ) : (
                  <div className="text-[10px] text-tank-muted font-mono py-3 text-center">
                    {systemFilter !== 'all' ? `No ${systemFilter} events` : 'No system events yet'}
                  </div>
                )}
              </div>
            </div>
          </div>

          {/* Bottom: Chat */}
          <div className="flex-1 flex flex-col min-h-[600px] md:min-h-0">
            <Panel
              title={`Chat${chatRoom ? `: ${chatRoom}` : ''}`}
              icon={MessageSquare}
              count={stats.chats}
              className="flex-1 min-h-[300px] md:min-h-0 border-t-2 border-t-rose-500/60"
              virtualized
              extra={<>
                <div className="flex gap-0.5">
                  {CHAT_FILTERS.map(f => (
                    <button
                      key={f.id}
                      onClick={() => setChatFilter(f.id)}
                      className={`text-[9px] font-mono px-1 py-0.5 rounded transition-colors ${
                        chatFilter === f.id
                          ? f.color
                          : 'text-tank-muted hover:text-tank-text'
                      }`}
                    >
                      {f.label}
                    </button>
                  ))}
                </div>
                {activeSuperchats.length > 0 && (
                  <span className="text-[9px] font-mono px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-400">
                    {activeSuperchats.length} pinned
                  </span>
                )}
              </>}
            >
              <div className="flex flex-col h-full">
                {chatKeywordTags.length > 0 && (
                  <div className="flex items-center flex-wrap gap-1 px-2 py-1 border-b border-tank-border bg-tank-surface/50 shrink-0">
                    <span className="text-[8px] text-tank-muted uppercase tracking-wide mr-0.5">trending:</span>
                    {chatKeywordTags.map(({ word, count }) => (
                      <span key={word} className="text-[9px] font-mono bg-cyan-500/10 text-cyan-400 border border-cyan-500/20 rounded-full px-2 py-0.5">
                        {word} <span className="text-cyan-400/60">{count}</span>
                      </span>
                    ))}
                  </div>
                )}
                <div className="flex-1 min-h-0">
              {sortedChats.length === 0 ? (
                <div className="p-2">
                  <EmptyState text={chatFilter !== 'all' ? (roleChatsLoading ? 'Looking for messages...' : 'No messages yet') : 'Waiting for chat messages...'} />
                </div>
              ) : (
                <Virtuoso
                  style={{ height: '100%' }}
                  data={sortedChats}
                  overscan={100}
                  components={{
                    Header: () => activeSuperchats.length > 0 ? (
                      <div className="space-y-1 mb-2 px-2 pt-2">
                        {activeSuperchats.map(sc => {
                          const d = sc.data || {}
                          const key = String(sc.id || d.id)
                          const secs = scCountdowns[key]
                          return (
                            <div key={key} className="p-2 rounded border border-amber-500/30 bg-amber-500/5">
                              <div className="flex items-center gap-1.5 mb-0.5">
                                <span className="text-[9px] font-mono px-1 py-0.5 rounded bg-amber-500/10 text-amber-400">SC</span>
                                <span className="text-xs font-medium text-amber-300">{d.user?.displayName || d.displayName || d.username || (d.userId ? 'Anon' : '?')}</span>
                                {d.cost > 0 && (
                                  <span className="text-[10px] font-mono text-tank-warn">{d.cost}t</span>
                                )}
                                {secs != null ? (
                                  <span className={`text-[9px] font-mono ml-auto ${secs < 60 ? 'text-red-400' : 'text-amber-400/70'}`}>
                                    {Math.floor(secs / 60)}:{String(secs % 60).padStart(2, '0')}
                                  </span>
                                ) : d.duration ? (
                                  <span className="text-[9px] font-mono text-tank-muted ml-auto">{d.duration}min</span>
                                ) : null}
                              </div>
                              {d.message && (
                                <p className="text-xs text-tank-bright break-words">{d.message}</p>
                              )}
                            </div>
                          )
                        })}
                      </div>
                    ) : null,
                  }}
                  itemContent={(index, item) => (
                    <ChatMessage data={item.data} />
                  )}
                />
              )}
                </div>
              </div>
            </Panel>
          </div>

        </div>

        {/* RIGHT: 24h sidebar */}
        <Last24hSidebar sessionStats={sessionStats} topKeywords={topKeywords} />
      </main>
      )}

      <Suspense fallback={<div className="flex-1 flex items-center justify-center text-sm text-tank-muted font-mono">Loading...</div>}>
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
      </Suspense>
    </div>
  )
}

function LeaderboardSection({ title, items, renderItem }) {
  if (!items || items.length === 0) return null
  return (
    <div className="border-t border-tank-border/50 pt-2 mt-2">
      <h4 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider mb-1.5 flex items-center gap-1.5"><Trophy className="w-3 h-3" />{title}</h4>
      <div className="space-y-1">
        {items.map((s, i) => renderItem(s, i))}
      </div>
    </div>
  )
}

const Last24hSidebar = memo(function Last24hSidebar({ sessionStats, topKeywords }) {
  const formattedSpend = useMemo(() => ({
    pollTokens: sessionStats.poll_tokens.toLocaleString(),
    superchatTokens: sessionStats.superchat_tokens.toLocaleString(),
    totalSpend: sessionStats.total_spend.toLocaleString(),
    estRevenue: tokensToUSD(sessionStats.total_spend),
  }), [sessionStats.poll_tokens, sessionStats.superchat_tokens, sessionStats.total_spend])

  const topSenders = useMemo(() =>
    (sessionStats.top_senders || []).slice(0, 5).map(s => ({
      ...s, spendFmt: s.spend.toLocaleString(), usdFmt: tokensToUSD(s.spend)
    })), [sessionStats.top_senders])

  const topTTS = useMemo(() =>
    (sessionStats.top_tts_senders || []).slice(0, 5).map(s => ({
      ...s, spendFmt: s.spend.toLocaleString(), usdFmt: tokensToUSD(s.spend)
    })), [sessionStats.top_tts_senders])

  const topSFX = useMemo(() =>
    (sessionStats.top_sfx_senders || []).slice(0, 5).map(s => ({
      ...s, spendFmt: s.spend.toLocaleString(), usdFmt: tokensToUSD(s.spend)
    })), [sessionStats.top_sfx_senders])

  const topChat = useMemo(() =>
    (sessionStats.top_chat_senders || []).slice(0, 5),
    [sessionStats.top_chat_senders])

  const topFishtoy = useMemo(() =>
    (sessionStats.top_fishtoy_senders || []).slice(0, 5).map(s => ({
      ...s, spendFmt: s.spend.toLocaleString(), usdFmt: tokensToUSD(s.spend)
    })), [sessionStats.top_fishtoy_senders])

  const topWords = useMemo(() => (topKeywords || []).slice(0, 10), [topKeywords])

  return (
    <Panel title="Last 24 Hours" icon={Clock} className="w-full md:w-[240px] lg:w-[280px] md:shrink-0 border-t-2 border-t-cyan-500/60">
      <div className="space-y-1.5">
        <StatRow label="Fishtoys" value={sessionStats.fishtoys} color="text-tank-accent" />
        <StatRow label="Chat" value={sessionStats.chats} color="text-blue-400" />
        <StatRow label="TTS" value={sessionStats.tts} color="text-purple-400" />
        <StatRow label="SFX" value={sessionStats.sfx} color="text-indigo-400" />
        {sessionStats.poll_tokens > 0 && (
          <StatRow label="Poll Votes" value={formattedSpend.pollTokens} color="text-cyan-400" />
        )}
        {sessionStats.superchat_tokens > 0 && (
          <StatRow label="Superchats" value={formattedSpend.superchatTokens} color="text-yellow-400" />
        )}
        <div className="w-full h-px bg-tank-border my-0.5" />
        <StatRow label="Tokens" value={formattedSpend.totalSpend} color="text-tank-warn" />
        <StatRow label="Est. Revenue" value={formattedSpend.estRevenue} color="text-green-400" />
      </div>

      <LeaderboardSection title="24h Top Spenders (TTS, SFX & Fishtoys)" items={topSenders} renderItem={(s, i) => (
        <div key={s.name} className="flex items-center justify-between text-[11px]">
          <div className="flex items-center gap-1">
            <span className="text-[10px] font-mono text-tank-muted">{i + 1}.</span>
            <span className="text-tank-bright truncate">{s.name}</span>
          </div>
          <div className="flex flex-col items-end shrink-0">
            <span className="font-mono text-tank-warn text-[10px]">{s.spendFmt}t</span>
            <span className="font-mono text-green-400 text-[9px]">{s.usdFmt}</span>
          </div>
        </div>
      )} />

      <LeaderboardSection title="24h Top TTS" items={topTTS} renderItem={(s, i) => (
        <div key={s.name} className="flex items-center justify-between text-[11px]">
          <div className="flex items-center gap-1">
            <span className="text-[10px] font-mono text-tank-muted">{i + 1}.</span>
            <span className="text-tank-bright truncate">{s.name}</span>
          </div>
          <div className="flex flex-col items-end shrink-0">
            <span className="font-mono text-tank-muted text-[10px]">{s.count}x / {s.spendFmt}t</span>
            <span className="font-mono text-green-400 text-[9px]">{s.usdFmt}</span>
          </div>
        </div>
      )} />

      <LeaderboardSection title="24h Top SFX" items={topSFX} renderItem={(s, i) => (
        <div key={s.name} className="flex items-center justify-between text-[11px]">
          <div className="flex items-center gap-1">
            <span className="text-[10px] font-mono text-tank-muted">{i + 1}.</span>
            <span className="text-tank-bright truncate">{s.name}</span>
          </div>
          <div className="flex flex-col items-end shrink-0">
            <span className="font-mono text-tank-muted text-[10px]">{s.count}x / {s.spendFmt}t</span>
            <span className="font-mono text-green-400 text-[9px]">{s.usdFmt}</span>
          </div>
        </div>
      )} />

      <LeaderboardSection title="24h Top Chat" items={topChat} renderItem={(s, i) => (
        <div key={s.name} className="flex items-center justify-between text-[11px]">
          <div className="flex items-center gap-1">
            <span className="text-[10px] font-mono text-tank-muted">{i + 1}.</span>
            <span className="text-tank-bright truncate">{s.name}</span>
          </div>
          <span className="font-mono text-blue-400 shrink-0 text-[10px]">{s.count} msg</span>
        </div>
      )} />

      <LeaderboardSection title="24h Top Fishtoy" items={topFishtoy} renderItem={(s, i) => (
        <div key={s.name} className="flex items-center justify-between text-[11px]">
          <div className="flex items-center gap-1">
            <span className="text-[10px] font-mono text-tank-muted">{i + 1}.</span>
            <span className="text-tank-bright truncate">{s.name}</span>
          </div>
          <div className="flex flex-col items-end shrink-0">
            <span className="font-mono text-tank-muted text-[10px]">{s.count}x / {s.spendFmt}t</span>
            <span className="font-mono text-green-400 text-[9px]">{s.usdFmt}</span>
          </div>
        </div>
      )} />

      <LeaderboardSection title="Top Chat Keywords (24h)" items={topWords} renderItem={(w, i) => (
        <div key={w.word} className="flex items-center justify-between text-[11px]">
          <div className="flex items-center gap-1">
            <span className="text-[10px] font-mono text-tank-muted">{i + 1}.</span>
            <span className="text-tank-bright truncate">{w.word}</span>
          </div>
          <span className="font-mono text-cyan-400 shrink-0 text-[10px]">{w.count}</span>
        </div>
      )} />
    </Panel>
  )
})

const StatRow = memo(function StatRow({ label, value, color = 'text-tank-bright' }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-xs text-tank-muted">{label}</span>
      <span className={`text-sm font-mono font-semibold ${color}`}>{value}</span>
    </div>
  )
})

const EmptyState = memo(function EmptyState({ text }) {
  return (
    <div className="flex items-center justify-center h-full">
      <span className="text-xs text-tank-muted font-mono">{text}</span>
    </div>
  )
})
