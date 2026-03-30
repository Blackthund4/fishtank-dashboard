import { useState, useEffect, useMemo } from 'react'
import { Fish, MessageSquare, Radio, Search, X, BarChart3, FileText } from 'lucide-react'
import { useWebSocket } from './useWebSocket'
import StatusBar from './components/StatusBar'
import Panel from './components/Panel'
import FishtoyCard from './components/FishtoyCard'
import ChatMessage from './components/ChatMessage'
import ActivityCard from './components/ActivityCard'
import AnalyticsTab from './tabs/AnalyticsTab'
import HiddenContentTab from './tabs/HiddenContentTab'

const MAX_EVENTS = 500

const FISHTOY_TYPES = new Set(['fishtoy:used', 'fishtoy:queued', 'fishtoy:update'])
const CHAT_TYPES = new Set(['chat:message'])
const ACTIVITY_TYPES = new Set([
  'tts:queued', 'tts:update', 'sfx:queued', 'sfx:update',
  'happening', 'item:new', 'item:update',
  'item-details:new', 'item-details:update',
])

function normalizeStats(raw) {
  const byType = raw.by_type || {}
  return {
    fishtoys: raw.fishtoys?.total || 0,
    chats: byType['chat:message'] || 0,
    tts: (byType['tts:queued'] || 0) + (byType['tts:update'] || 0),
    sfx: (byType['sfx:queued'] || 0) + (byType['sfx:update'] || 0),
    total_spend: raw.total_spend || raw.fishtoys?.total_cost || 0,
    top_targets: (raw.top_targets || []).map(t => ({ target: t.name, count: t.count })),
    top_senders: (raw.top_senders || []).map(s => ({ name: s.name, count: s.count })),
    total_events: raw.total_events || 0,
  }
}

export default function App() {
  const { isConnected, addListener } = useWebSocket()
  const [fishtoys, setFishtoys] = useState([])
  const [chats, setChats] = useState([])
  const [activity, setActivity] = useState([])
  const [stats, setStats] = useState({
    fishtoys: 0, chats: 0, tts: 0, sfx: 0, total_spend: 0,
    top_targets: [], top_senders: [], total_events: 0,
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

  // Load catalog + historical data on mount
  useEffect(() => {
    fetch('/api/items').then(r => r.json()).then(setItemCatalog).catch(() => {})
    fetch('/api/contestants').then(r => r.json()).then(setContestants).catch(() => {})
    fetch('/api/rooms').then(r => r.json()).then(setRoomMap).catch(() => {})
    fetch('/api/stocks').then(r => r.json()).then(setStocks).catch(() => {})
    fetch('/api/stats').then(r => r.json()).then(raw => setStats(normalizeStats(raw))).catch(() => {})

    // Fetch fishtoys separately so they aren't crowded out by chat volume
    fetch('/api/fishtoys?limit=500')
      .then(r => r.json())
      .then(events => {
        setFishtoys(events.map(e => ({ event: e.event_type, data: e.data, dbId: e.id })))
      })
      .catch(() => {})

    // Fetch other events
    fetch('/api/events?type=chat:message,tts:queued,tts:update,sfx:queued,sfx:update,happening&limit=500')
      .then(r => r.json())
      .then(events => {
        const ch = [], act = []
        events.forEach(e => {
          const item = { event: e.event_type, data: e.data, dbId: e.id }
          if (CHAT_TYPES.has(e.event_type)) ch.push(item)
          else if (ACTIVITY_TYPES.has(e.event_type)) act.push(item)
        })
        setChats(ch)
        setActivity(act)
      })
      .catch(() => {})
  }, [])

  // Live events
  useEffect(() => {
    const remove = addListener((msg) => {
      const item = { event: msg.event_type, data: msg.data, dbId: msg.db_id }

      if (FISHTOY_TYPES.has(msg.event_type)) {
        setFishtoys(prev => [item, ...prev].slice(0, MAX_EVENTS))
        setStats(s => ({
          ...s,
          fishtoys: s.fishtoys + 1,
          total_spend: s.total_spend + (msg.data?.cost || 0),
        }))
      } else if (CHAT_TYPES.has(msg.event_type)) {
        setChats(prev => [item, ...prev].slice(0, MAX_EVENTS))
        setStats(s => ({ ...s, chats: s.chats + 1 }))
      } else if (ACTIVITY_TYPES.has(msg.event_type)) {
        setActivity(prev => [item, ...prev].slice(0, MAX_EVENTS))
        if (msg.event_type.startsWith('tts')) {
          setStats(s => ({ ...s, tts: s.tts + 1, total_spend: s.total_spend + (msg.data?.cost || 0) }))
        }
        if (msg.event_type.startsWith('sfx')) {
          setStats(s => ({ ...s, sfx: s.sfx + 1, total_spend: s.total_spend + (msg.data?.cost || 0) }))
        }
      }
    })
    return remove
  }, [addListener])

  // Refresh stats periodically
  useEffect(() => {
    const interval = setInterval(() => {
      fetch('/api/stats').then(r => r.json()).then(raw => setStats(normalizeStats(raw))).catch(() => {})
      fetch('/api/stocks').then(r => r.json()).then(setStocks).catch(() => {})
    }, 30000)
    return () => clearInterval(interval)
  }, [])

  // Client-side filtered fishtoys
  const filteredFishtoys = useMemo(() => {
    let result = fishtoys
    if (filterTarget) {
      result = result.filter(f => f.data?.target === filterTarget)
    }
    if (filterCategory) {
      result = result.filter(f => {
        const cat = itemCatalog[String(f.data?.itemId || '')]
        return cat?.type === filterCategory
      })
    }
    if (filterItemId) {
      result = result.filter(f => String(f.data?.itemId) === String(filterItemId))
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
    return result
  }, [fishtoys, filterTarget, filterCategory, filterItemId, searchText, itemCatalog])

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

  // Unique targets seen in fishtoy data, sorted by count
  const seenTargets = useMemo(() => {
    const counts = new Map()
    const spend = new Map()
    fishtoys.forEach(f => {
      const t = f.data?.target
      if (t) {
        counts.set(t, (counts.get(t) || 0) + 1)
        spend.set(t, (spend.get(t) || 0) + (f.data?.cost || 0))
      }
    })
    return Array.from(counts.entries())
      .map(([target, count]) => ({ target, count, spend: spend.get(target) || 0 }))
      .sort((a, b) => b.count - a.count)
  }, [fishtoys])

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

      {/* Tab navigation */}
      <div className="bg-tank-surface border-b border-tank-border px-3 flex items-center gap-1 shrink-0">
        {[
          { id: 'dashboard', label: 'Dashboard', icon: Fish },
          { id: 'analytics', label: 'Analytics', icon: BarChart3 },
          { id: 'hidden', label: 'Hidden Content', icon: FileText },
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
            {tab.label}
          </button>
        ))}
      </div>

      {activeTab === 'dashboard' && (
      <main className="flex-1 flex gap-2 p-2 min-h-0">
        {/* LEFT: Fishtoys panel */}
        <div className="w-[420px] shrink-0 flex flex-col bg-tank-surface border border-tank-border rounded-lg overflow-hidden">
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
          <div className="flex-1 overflow-y-auto p-2 space-y-1.5">
            {filteredFishtoys.length === 0 ? (
              <EmptyState text={hasActiveFilters ? "No fishtoys match filters" : "Waiting for fishtoy events..."} />
            ) : (
              filteredFishtoys.map((item) => (
                <FishtoyCard
                  key={item.dbId || item.data?.id}
                  data={item.data}
                  eventType={item.event}
                  itemCatalog={itemCatalog}
                  onTargetClick={setFilterTarget}
                />
              ))
            )}
          </div>
        </div>

        {/* RIGHT: Everything else */}
        <div className="flex-1 flex flex-col gap-2 min-w-0">

          {/* Top row: Targets + Stats side by side */}
          <div className="flex gap-2 shrink-0">
            {/* Targets + Target detail */}
            <div className="flex-1 flex flex-col gap-2 min-w-0">
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
                          title={`${count} fishtoys, ${spend.toLocaleString()} tokens`}
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
                    <div className="flex items-center gap-3 text-[10px] font-mono">
                      <span className="text-tank-accent">{targetStats.total} fishtoys</span>
                      <span className="text-tank-warn">{targetStats.totalSpend.toLocaleString()} tokens</span>
                      {targetStats.withMeta > 0 && (
                        <span className="text-tank-accent">{targetStats.withMeta} with content</span>
                      )}
                    </div>
                  </div>
                  <div className="flex gap-4">
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
                                  <span className="font-mono text-tank-warn text-[10px]">{item.spend.toLocaleString()}t</span>
                                </div>
                              </button>
                            )
                          })}
                        </div>
                      </div>
                    )}
                    {/* Top senders to this target */}
                    {targetStats.topSenders.length > 0 && (
                      <div className="w-[180px] shrink-0">
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

            {/* Session stats (always visible, right side) */}
            <div className="w-[200px] shrink-0 bg-tank-surface border border-tank-border rounded-lg p-2.5">
              <h3 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider mb-2">
                Session Stats
              </h3>
              <div className="space-y-1.5">
                <StatRow label="Fishtoys" value={stats.fishtoys} color="text-tank-accent" />
                <StatRow label="Chat" value={stats.chats} color="text-blue-400" />
                <StatRow label="TTS" value={stats.tts} color="text-purple-400" />
                <StatRow label="SFX" value={stats.sfx} color="text-indigo-400" />
                <div className="w-full h-px bg-tank-border my-0.5" />
                <StatRow label="Tokens" value={stats.total_spend.toLocaleString()} color="text-tank-warn" />
              </div>
              {/* Top senders (global, only when no target selected) */}
              {!filterTarget && stats.top_senders && stats.top_senders.length > 0 && (
                <div className="border-t border-tank-border/50 pt-2 mt-2">
                  <h4 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider mb-1.5">Top Senders</h4>
                  <div className="space-y-1">
                    {stats.top_senders.slice(0, 5).map((s, i) => (
                      <div key={s.name} className="flex items-center justify-between text-[11px]">
                        <div className="flex items-center gap-1">
                          <span className="text-[10px] font-mono text-tank-muted">{i + 1}.</span>
                          <span className="text-tank-bright truncate">{s.name}</span>
                        </div>
                        <span className="font-mono text-tank-accent shrink-0">{s.count}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* Stock Market ticker */}
          {stocks.length > 0 && (
            <div className="bg-tank-surface border border-tank-border rounded-lg p-2 shrink-0">
              <div className="flex items-center gap-2 mb-1.5">
                <h3 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider">Stock Market</h3>
              </div>
              <div className="flex gap-2 overflow-x-auto">
                {stocks.sort((a, b) => b.currentPrice - a.currentPrice).map(s => {
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

          {/* Bottom: Chat + Activity side by side */}
          <div className="flex-1 flex gap-2 min-h-0">
            <Panel title="Chat" icon={MessageSquare} count={stats.chats} className="flex-1">
              {chats.length === 0 ? (
                <EmptyState text="Waiting for chat messages..." />
              ) : (
                chats.map((item) => (
                  <ChatMessage key={item.dbId || item.data?.id} data={item.data} />
                ))
              )}
            </Panel>

            <Panel title="Activity" icon={Radio} count={stats.tts + stats.sfx} className="w-[340px] shrink-0">
              {activity.length === 0 ? (
                <EmptyState text="Waiting for TTS / SFX / events..." />
              ) : (
                activity.map((item) => (
                  <ActivityCard key={item.dbId || item.data?.id} data={item.data} eventType={item.event} roomMap={roomMap} />
                ))
              )}
            </Panel>
          </div>

        </div>
      </main>
      )}

      {activeTab === 'analytics' && (
        <AnalyticsTab
          contestants={contestants}
          roomMap={roomMap}
          itemCatalog={itemCatalog}
        />
      )}

      {activeTab === 'hidden' && (
        <HiddenContentTab itemCatalog={itemCatalog} />
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
