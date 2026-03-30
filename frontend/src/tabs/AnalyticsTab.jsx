import { useState, useEffect } from 'react'
import { TrendingUp, Volume2, MessageSquare, Users, Bell, Vote, Zap } from 'lucide-react'

function formatTime(ts) {
  if (!ts) return ''
  const ms = typeof ts === 'number' ? (ts > 1e12 ? ts : ts * 1000) : Date.parse(ts)
  return new Date(ms).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
}

function formatDateTime(ts) {
  if (!ts) return ''
  const d = new Date(typeof ts === 'string' ? ts : (ts > 1e12 ? ts : ts * 1000))
  return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' }) + ' ' +
    d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export default function AnalyticsTab({ contestants, roomMap, itemCatalog, notifications = [], systemEvents = [] }) {
  const [stockHistory, setStockHistory] = useState([])
  const [stocks, setStocks] = useState([])
  const [ttsAnalytics, setTtsAnalytics] = useState(null)
  const [chatAnalytics, setChatAnalytics] = useState(null)
  const [fishtoyStatus, setFishtoyStatus] = useState([])
  const [polls, setPolls] = useState([])
  const [priceChanges, setPriceChanges] = useState([])

  useEffect(() => {
    fetch('/api/stocks').then(r => r.json()).then(setStocks).catch(() => {})
    fetch('/api/stocks/history?limit=2000').then(r => r.json()).then(setStockHistory).catch(() => {})
    fetch('/api/analytics/tts-sfx').then(r => r.json()).then(setTtsAnalytics).catch(() => {})
    fetch('/api/analytics/chat').then(r => r.json()).then(setChatAnalytics).catch(() => {})
    fetch('/api/fishtoy-availability').then(r => r.json()).then(setFishtoyStatus).catch(() => {})
    fetch('/api/polls').then(r => r.json()).then(setPolls).catch(() => {})
    fetch('/api/price-changes').then(r => r.json()).then(setPriceChanges).catch(() => {})

    const interval = setInterval(() => {
      fetch('/api/stocks').then(r => r.json()).then(setStocks).catch(() => {})
    }, 30000)
    return () => clearInterval(interval)
  }, [])

  return (
    <div className="flex-1 overflow-y-auto p-3 space-y-3">
      {/* Stock Market */}
      <Section title="Stock Market" icon={TrendingUp}>
        <div className="grid grid-cols-5 gap-2">
          {stocks.sort((a, b) => b.currentPrice - a.currentPrice).map(s => {
            const change = s.currentPrice - s.ipoPrice
            const changePct = s.ipoPrice > 0 ? ((change / s.ipoPrice) * 100).toFixed(0) : 0
            const dayChange = s.currentPrice - s.today
            const isUp = dayChange > 0
            const isDown = dayChange < 0
            return (
              <div key={s.tickerSymbol} className="bg-tank-bg border border-tank-border rounded-lg p-3">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-sm font-bold text-tank-bright">{s.tickerSymbol}</span>
                  <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded ${
                    isUp ? 'bg-green-500/10 text-green-400' : isDown ? 'bg-red-500/10 text-red-400' : 'bg-tank-highlight text-tank-muted'
                  }`}>
                    {isUp ? '+' : ''}{dayChange} today
                  </span>
                </div>
                <div className="text-2xl font-mono font-bold text-tank-bright mb-1">{s.currentPrice}</div>
                <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 text-[10px]">
                  <span className="text-tank-muted">IPO</span><span className="text-tank-text text-right">{s.ipoPrice}</span>
                  <span className="text-tank-muted">Avg</span><span className="text-tank-text text-right">{s.averagePrice}</span>
                  <span className="text-tank-muted">Last hour</span><span className="text-tank-text text-right">{s.lastHour}</span>
                  <span className="text-tank-muted">Last week</span><span className="text-tank-text text-right">{s.lastWeek}</span>
                  <span className="text-tank-muted">Since IPO</span>
                  <span className={`text-right ${change > 0 ? 'text-green-400' : change < 0 ? 'text-red-400' : 'text-tank-muted'}`}>
                    {change > 0 ? '+' : ''}{changePct}%
                  </span>
                  <span className="text-tank-muted">Bid/Ask</span>
                  <span className="text-tank-text text-right">{s.highestBuyOrder}/{s.lowestSellOrder}</span>
                </div>
              </div>
            )
          })}
        </div>
        {stockHistory.length > 0 && (
          <div className="mt-2 text-[10px] font-mono text-tank-muted">
            {stockHistory.length} price snapshots recorded
          </div>
        )}
      </Section>

      {/* Contestant Timeline */}
      <Section title="Contestants" icon={Users}>
        <div className="grid grid-cols-5 gap-2">
          {contestants.map(c => {
            const stock = stocks.find(s => s.tickerSymbol === c.name?.toUpperCase() ||
              s.tickerSymbol === c.name?.substring(0, 4).toUpperCase())
            return (
              <div key={c.id} className={`bg-tank-bg border rounded-lg p-2.5 ${
                c.eliminatedAt ? 'border-red-500/30 opacity-60' : 'border-tank-border'
              }`}>
                <div className="flex items-center gap-2 mb-1.5">
                  {c.photo && (
                    <img src={c.photo} alt={c.name} className="w-8 h-8 rounded-full object-cover border border-tank-border" />
                  )}
                  <div>
                    <div className="text-xs font-semibold" style={{ color: c.color || '#c8cdd4' }}>{c.name}</div>
                    {c.freeloader && <span className="text-[9px] text-tank-muted">Freeloader</span>}
                    {c.eliminatedAt && <span className="text-[9px] text-red-400">Eliminated</span>}
                  </div>
                </div>
                {stock && (
                  <div className="text-[10px] font-mono text-tank-muted">
                    Stock: <span className="text-tank-bright">{stock.currentPrice}</span>
                    <span className={`ml-1 ${stock.currentPrice > stock.today ? 'text-green-400' : stock.currentPrice < stock.today ? 'text-red-400' : ''}`}>
                      ({stock.currentPrice > stock.today ? '+' : ''}{stock.currentPrice - stock.today})
                    </span>
                  </div>
                )}
                {c.endorsements > 0 && (
                  <div className="text-[10px] text-tank-muted">{c.endorsements} endorsements</div>
                )}
              </div>
            )
          })}
        </div>
      </Section>

      <div className="grid grid-cols-2 gap-3">
        {/* TTS/SFX Analytics */}
        <Section title="TTS / SFX Analytics" icon={Volume2}>
          {ttsAnalytics ? (
            <div className="space-y-3">
              {ttsAnalytics.top_rooms.length > 0 && (
                <div>
                  <h4 className="text-[10px] font-mono text-tank-muted uppercase mb-1">Most Active Rooms</h4>
                  <div className="space-y-1">
                    {ttsAnalytics.top_rooms.map((r, i) => (
                      <div key={r.room} className="flex items-center justify-between text-xs">
                        <span className="text-tank-bright">{roomMap[r.room] || r.room}</span>
                        <span className="font-mono text-cyan-400">{r.count}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {ttsAnalytics.top_tts_senders.length > 0 && (
                <div>
                  <h4 className="text-[10px] font-mono text-tank-muted uppercase mb-1">Top TTS Spenders</h4>
                  <div className="space-y-1">
                    {ttsAnalytics.top_tts_senders.map(s => (
                      <div key={s.name} className="flex items-center justify-between text-xs">
                        <span className="text-tank-bright">{s.name}</span>
                        <div className="flex gap-2">
                          <span className="font-mono text-tank-muted">{s.count}x</span>
                          <span className="font-mono text-tank-warn">{s.spend.toLocaleString()}t</span>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {ttsAnalytics.hourly.length > 0 && (
                <div>
                  <h4 className="text-[10px] font-mono text-tank-muted uppercase mb-1">Hourly Activity (UTC)</h4>
                  <HourlyBar data={ttsAnalytics.hourly} color="bg-purple-400" />
                </div>
              )}
            </div>
          ) : (
            <div className="text-xs text-tank-muted font-mono">Loading analytics...</div>
          )}
        </Section>

        {/* Chat Analytics */}
        <Section title="Chat Analytics" icon={MessageSquare}>
          {chatAnalytics ? (
            <div className="space-y-3">
              <div className="text-xs text-tank-muted">
                Total messages: <span className="text-tank-bright font-mono">{chatAnalytics.total.toLocaleString()}</span>
              </div>
              {chatAnalytics.top_chatters.length > 0 && (
                <div>
                  <h4 className="text-[10px] font-mono text-tank-muted uppercase mb-1">Most Active Chatters</h4>
                  <div className="space-y-1">
                    {chatAnalytics.top_chatters.map((c, i) => (
                      <div key={c.name} className="flex items-center justify-between text-xs">
                        <div className="flex items-center gap-1.5">
                          <span className="text-[10px] font-mono text-tank-muted w-4">{i + 1}.</span>
                          <span className="text-tank-bright">{c.name}</span>
                        </div>
                        <span className="font-mono text-blue-400">{c.count}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {chatAnalytics.hourly.length > 0 && (
                <div>
                  <h4 className="text-[10px] font-mono text-tank-muted uppercase mb-1">Hourly Volume (UTC)</h4>
                  <HourlyBar data={chatAnalytics.hourly} color="bg-blue-400" />
                </div>
              )}
            </div>
          ) : (
            <div className="text-xs text-tank-muted font-mono">Loading analytics...</div>
          )}
        </Section>
      </div>

      {/* Fishtoy Availability */}
      <Section title="Fishtoy Availability" icon={TrendingUp}>
        <div className="grid grid-cols-6 gap-1.5">
          {fishtoyStatus.sort((a, b) => (a.name || '').localeCompare(b.name || '')).map(f => (
            <div key={f.id} className={`px-2 py-1.5 rounded border text-xs ${
              f.enabled
                ? 'border-tank-accent/30 bg-tank-accent/5'
                : 'border-tank-border opacity-40'
            }`}>
              <div className="font-medium text-tank-bright truncate">{f.name}</div>
              <div className="flex items-center justify-between mt-0.5">
                <span className="text-[10px] text-tank-warn font-mono">{f.cost}t</span>
                <span className={`text-[9px] font-mono ${f.enabled ? 'text-tank-accent' : 'text-red-400'}`}>
                  {f.enabled ? 'ON' : 'OFF'}
                </span>
              </div>
              {f.type === 'BIGTOY' && (
                <span className="text-[9px] text-purple-400 font-mono">BIGTOY</span>
              )}
            </div>
          ))}
        </div>
      </Section>

      <div className="grid grid-cols-2 gap-3">
        {/* Director Messages / Notifications Timeline */}
        <Section title="Director Messages" icon={Bell}>
          {notifications.length > 0 ? (
            <div className="space-y-1.5 max-h-[300px] overflow-y-auto">
              {notifications.map(n => (
                <div key={n.id} className="flex items-start gap-2 p-2 bg-yellow-500/5 border border-yellow-500/20 rounded">
                  <Bell className="w-3.5 h-3.5 text-yellow-400 shrink-0 mt-0.5" />
                  <div className="min-w-0 flex-1">
                    <p className="text-sm text-tank-bright break-words">{n.message}</p>
                    <span className="text-[10px] font-mono text-tank-muted">{formatDateTime(n.timestamp)}</span>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-xs text-tank-muted font-mono">No director messages yet</div>
          )}
        </Section>

        {/* Poll History */}
        <Section title="Poll History" icon={Vote}>
          {polls.length > 0 ? (
            <div className="space-y-2 max-h-[300px] overflow-y-auto">
              {polls.filter(p => p.event_type === 'poll:start' || p.event_type === 'poll:stop').map(p => {
                const d = p.data || {}
                return (
                  <div key={p.id} className={`p-2 rounded border ${
                    p.event_type === 'poll:stop'
                      ? 'border-purple-500/30 bg-purple-500/5'
                      : 'border-tank-border bg-tank-bg'
                  }`}>
                    <div className="flex items-center justify-between mb-1">
                      <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded ${
                        p.event_type === 'poll:stop'
                          ? 'bg-purple-500/10 text-purple-400'
                          : 'bg-tank-highlight text-tank-muted'
                      }`}>
                        {p.event_type === 'poll:stop' ? 'RESULT' : 'STARTED'}
                      </span>
                      <span className="text-[10px] font-mono text-tank-muted">{formatDateTime(p.timestamp_local)}</span>
                    </div>
                    {d.question && <p className="text-xs text-tank-bright mb-1">{d.question}</p>}
                    {d.winner && (
                      <div className="text-xs">
                        Winner: <span className="font-semibold text-purple-400">{d.winner}</span>
                      </div>
                    )}
                    {d.answers && !d.winner && (
                      <div className="text-[10px] text-tank-muted">
                        Options: {d.answers.join(', ')}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          ) : (
            <div className="text-xs text-tank-muted font-mono">No polls recorded yet</div>
          )}
        </Section>
      </div>

      <div className="grid grid-cols-2 gap-3">
        {/* TTS/SFX Price Changes */}
        <Section title="Price Changes" icon={Zap}>
          {priceChanges.length > 0 ? (
            <div className="space-y-1 max-h-[200px] overflow-y-auto">
              {priceChanges.map(p => (
                <div key={p.id} className="flex items-center justify-between text-xs p-1.5 bg-tank-bg rounded">
                  <span className={`font-mono px-1.5 py-0.5 rounded ${
                    p.event_type === 'tts:price' ? 'bg-purple-500/10 text-purple-400' : 'bg-indigo-500/10 text-indigo-400'
                  }`}>
                    {p.event_type === 'tts:price' ? 'TTS' : 'SFX'}
                  </span>
                  <span className="text-tank-bright font-mono">{typeof p.data === 'number' ? `${p.data}t` : JSON.stringify(p.data)}</span>
                  <span className="text-[10px] text-tank-muted font-mono">{formatDateTime(p.timestamp_local)}</span>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-xs text-tank-muted font-mono">No price changes recorded</div>
          )}
        </Section>

        {/* System Events */}
        <Section title="System Events" icon={Zap}>
          {systemEvents.length > 0 ? (
            <div className="space-y-1 max-h-[200px] overflow-y-auto">
              {systemEvents.map(e => (
                <div key={e.dbId} className="flex items-center gap-2 text-xs p-1.5 bg-tank-bg rounded">
                  <span className="font-mono text-[10px] px-1.5 py-0.5 rounded bg-tank-highlight text-tank-muted shrink-0">
                    {e.event}
                  </span>
                  <span className="text-tank-text truncate flex-1">
                    {typeof e.data === 'string' ? e.data : JSON.stringify(e.data).substring(0, 100)}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-xs text-tank-muted font-mono">No system events yet</div>
          )}
        </Section>
      </div>
    </div>
  )
}

function Section({ title, icon: Icon, children }) {
  return (
    <div className="bg-tank-surface border border-tank-border rounded-lg p-3">
      <div className="flex items-center gap-2 mb-2.5">
        {Icon && <Icon className="w-4 h-4 text-tank-muted" />}
        <h3 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider">{title}</h3>
      </div>
      {children}
    </div>
  )
}

function HourlyBar({ data, color }) {
  const max = Math.max(...data.map(d => d.count), 1)
  return (
    <div className="flex items-end gap-px h-12">
      {Array.from({ length: 24 }, (_, i) => {
        const hour = String(i).padStart(2, '0')
        const entry = data.find(d => d.hour === hour)
        const count = entry?.count || 0
        const pct = (count / max) * 100
        return (
          <div
            key={hour}
            className="flex-1 flex flex-col items-center"
            title={`${hour}:00 UTC - ${count} events`}
          >
            <div className="w-full relative" style={{ height: '40px' }}>
              <div
                className={`absolute bottom-0 w-full rounded-t-sm ${color} opacity-70`}
                style={{ height: `${Math.max(pct, 2)}%` }}
              />
            </div>
            {i % 6 === 0 && (
              <span className="text-[8px] text-tank-muted mt-0.5">{hour}</span>
            )}
          </div>
        )
      })}
    </div>
  )
}
