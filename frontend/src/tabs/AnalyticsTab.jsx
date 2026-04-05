import { useState, useEffect, useMemo } from 'react'
import { TrendingUp, Volume2, MessageSquare, Users, Zap, Fish } from 'lucide-react'
import { formatDateTime } from '../utils/formatTime'

function getSinceISO(period) {
  if (!period) return null
  const now = new Date()
  const minutes = period === '1m' ? 1 : period === '1h' ? 60 : period === '6h' ? 360 : period === '24h' ? 1440 : period === '1d' ? 1440 : period === '3d' ? 4320 : period === '7d' ? 10080 : 0
  if (!minutes) return null
  return new Date(now.getTime() - minutes * 60000).toISOString()
}

const TIME_OPTIONS = [
  { id: '7d', label: '7d' },
  { id: '3d', label: '3d' },
  { id: '24h', label: '24h' },
  { id: '1h', label: '1hr' },
]

const STOCK_TIME_OPTIONS = [
  { id: null, label: 'All' },
  { id: '1h', label: '1hr' },
  { id: '6h', label: '6hr' },
  { id: '1d', label: '1day' },
]

const STOCK_SORTS = [
  { id: 'value', label: 'Highest' },
  { id: 'up', label: 'Movers Up' },
  { id: 'down', label: 'Movers Down' },
]

function getMoodLabel(score) {
  if (score >= 0.3)   return { label: 'Excited', bgColor: 'bg-green-500',    textColor: 'text-white' }
  if (score >= 0.08)  return { label: 'Happy',   bgColor: 'bg-lime-400',     textColor: 'text-gray-900' }
  if (score >= -0.08) return { label: 'Neutral', bgColor: 'bg-gray-500/60',  textColor: 'text-gray-100' }
  if (score >= -0.3)  return { label: 'Grumpy',  bgColor: 'bg-orange-500',   textColor: 'text-white' }
  return                { label: 'Hostile', bgColor: 'bg-red-600',      textColor: 'text-white' }
}

function TimeFilter({ value, onChange }) {
  return (
    <div className="flex gap-1">
      {TIME_OPTIONS.map(f => (
        <button
          key={f.id || 'all'}
          onClick={() => onChange(f.id)}
          className={`text-[9px] font-mono px-1.5 py-0.5 rounded transition-colors ${
            value === f.id
              ? 'bg-tank-accent/20 text-tank-accent border border-tank-accent/40'
              : 'text-tank-muted hover:text-tank-text'
          }`}
        >
          {f.label}
        </button>
      ))}
    </div>
  )
}

function AnalyticsTab({ contestants, roomMap, itemCatalog, featureToggles = {} }) {
  const [stockHistory, setStockHistory] = useState([])
  const [stocks, setStocks] = useState([])
  const [ttsAnalytics, setTtsAnalytics] = useState(null)
  const [chatAnalytics, setChatAnalytics] = useState(null)
  const [fishtoyStatus, setFishtoyStatus] = useState([])
  const [priceChanges, setPriceChanges] = useState([])
  const [stockCount, setStockCount] = useState(0)
  const [peakHours, setPeakHours] = useState(null)

  // Per-section time filters (default to 24h to avoid full-table scans on 600k+ rows)
  const [ttsPeriod, setTtsPeriod] = useState('24h')
  const [chatPeriod, setChatPeriod] = useState('24h')
  const [stockPeriod, setStockPeriod] = useState(null)
  const [chatSentiment, setChatSentiment] = useState(null)
  const [ttsSentiment, setTtsSentiment] = useState(null)

  const [stockSort, setStockSort] = useState('value')
  const [contestantSort, setContestantSort] = useState('endorsements')

  // Staggered analytics fetches — heavy DB queries must not all fire at once
  // (concurrent json_extract aggregations on 600k+ rows OOM the 1GB container)
  useEffect(() => {
    let cancelled = false
    async function fetchAll() {
      if (document.hidden) return
      // Batch 1: lightweight catalog queries
      fetch('/api/stocks').then(r => r.json()).then(setStocks).catch(() => {})
      fetch('/api/stocks/count').then(r => r.json()).then(d => setStockCount(d.count || 0)).catch(() => {})
      fetch('/api/fishtoy-availability').then(r => r.json()).then(setFishtoyStatus).catch(() => {})
      fetch('/api/price-changes').then(r => r.json()).then(setPriceChanges).catch(() => {})

      // Batch 2: stock history (after a pause)
      await new Promise(r => setTimeout(r, 500))
      if (cancelled) return
      const stockSince = getSinceISO(stockPeriod)
      const stockParams = new URLSearchParams({ limit: '2000' })
      if (stockSince) stockParams.set('since', stockSince)
      await fetch(`/api/stocks/history?${stockParams}`).then(r => r.json()).then(setStockHistory).catch(() => {})

      // Batch 3: TTS/SFX analytics (sequential, not parallel)
      if (cancelled) return
      const ttsSince = getSinceISO(ttsPeriod)
      const ttsParam = ttsSince ? `?since=${encodeURIComponent(ttsSince)}` : ''
      await fetch(`/api/analytics/tts-sfx${ttsParam}`).then(r => r.json()).then(setTtsAnalytics).catch(() => {})
      if (cancelled) return
      await fetch(`/api/analytics/tts-sentiment${ttsParam}`).then(r => r.json()).then(setTtsSentiment).catch(() => {})

      // Batch 4: Chat analytics
      if (cancelled) return
      const chatSince = getSinceISO(chatPeriod)
      const chatParam = chatSince ? `?since=${encodeURIComponent(chatSince)}` : ''
      await fetch(`/api/analytics/chat${chatParam}`).then(r => r.json()).then(setChatAnalytics).catch(() => {})
      if (cancelled) return
      await fetch(`/api/analytics/chat-sentiment${chatParam}`).then(r => r.json()).then(setChatSentiment).catch(() => {})

      // Batch 5: peak hours (heaviest)
      if (cancelled) return
      fetch('/api/analytics/peak-hours').then(r => r.json()).then(setPeakHours).catch(() => {})
    }
    fetchAll()
    const interval = setInterval(fetchAll, 120000)
    return () => { cancelled = true; clearInterval(interval) }
  }, [stockPeriod, ttsPeriod, chatPeriod])

  // Build per-ticker reference prices from filtered stock history
  const stockRefPrices = useMemo(() => {
    if (!stockPeriod || stockHistory.length === 0) return null
    const ref = {}
    // stockHistory is ordered DESC, so the last entry per ticker is the earliest in the window
    for (const snap of stockHistory) {
      ref[snap.ticker] = snap.price
    }
    return ref
  }, [stockPeriod, stockHistory])

  const getStockChange = (s) => {
    if (stockRefPrices && stockRefPrices[s.tickerSymbol] != null) {
      return s.currentPrice - stockRefPrices[s.tickerSymbol]
    }
    return s.currentPrice - s.today
  }

  const stockChangeLabel = stockPeriod
    ? STOCK_TIME_OPTIONS.find(f => f.id === stockPeriod)?.label || stockPeriod
    : 'today'

  const stockMap = useMemo(() => new Map(stocks.map(s => [s.tickerSymbol, s])), [stocks])

  const sortedStocks = useMemo(() => [...stocks].sort((a, b) => {
    if (stockSort === 'up') return getStockChange(b) - getStockChange(a)
    if (stockSort === 'down') return getStockChange(a) - getStockChange(b)
    return b.currentPrice - a.currentPrice
  }), [stocks, stockSort, stockRefPrices])

  const sortedContestants = useMemo(() => [...contestants].sort((a, b) => {
    if (contestantSort === 'stox') {
      const aStock = a.tickerSymbol ? stockMap.get(a.tickerSymbol) : null
      const bStock = b.tickerSymbol ? stockMap.get(b.tickerSymbol) : null
      return (bStock?.currentPrice || 0) - (aStock?.currentPrice || 0)
    }
    return (b.endorsements || 0) - (a.endorsements || 0)
  }), [contestants, contestantSort, stockMap])

  const fishtoyToggle = featureToggles.fishtoys
  const ttsToggle = featureToggles.tts
  const sfxToggle = featureToggles.sfx

  return (
    <div className="flex-1 overflow-y-auto p-3 space-y-3">
      {/* STO-X */}
      <Section title="STO-X" icon={TrendingUp} extra={
        <div className="flex items-center gap-3">
          <div className="flex gap-1">
            {STOCK_TIME_OPTIONS.map(f => (
              <button
                key={f.id || 'all'}
                onClick={() => setStockPeriod(f.id)}
                className={`text-[9px] font-mono px-1.5 py-0.5 rounded transition-colors ${
                  stockPeriod === f.id
                    ? 'bg-tank-accent/20 text-tank-accent border border-tank-accent/40'
                    : 'text-tank-muted hover:text-tank-text'
                }`}
              >
                {f.label}
              </button>
            ))}
          </div>
          <div className="flex gap-1">
            {STOCK_SORTS.map(s => (
              <button
                key={s.id}
                onClick={() => setStockSort(s.id)}
                className={`text-[9px] font-mono px-1.5 py-0.5 rounded ${
                  stockSort === s.id ? 'bg-tank-accent/15 text-tank-accent' : 'text-tank-muted hover:text-tank-text'
                }`}
              >
                {s.label}
              </button>
            ))}
          </div>
        </div>
      }>
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2">
          {sortedStocks.map(s => {
            const change = s.currentPrice - s.ipoPrice
            const changePct = s.ipoPrice > 0 ? ((change / s.ipoPrice) * 100).toFixed(0) : 0
            const periodChange = getStockChange(s)
            const isUp = periodChange > 0
            const isDown = periodChange < 0
            return (
              <div key={s.tickerSymbol} className="bg-tank-bg border border-tank-border rounded-lg p-3">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-sm font-bold text-tank-bright">{s.tickerSymbol}</span>
                  <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded ${
                    isUp ? 'bg-green-500/10 text-green-400' : isDown ? 'bg-red-500/10 text-red-400' : 'bg-tank-highlight text-tank-muted'
                  }`}>
                    {isUp ? '+' : ''}{periodChange} {stockChangeLabel}
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
        {stockCount > 0 && (
          <div className="mt-2 text-[10px] font-mono text-tank-muted">
            {stockCount.toLocaleString()} price snapshots recorded
          </div>
        )}
      </Section>

      {/* Contestants */}
      <Section title="Contestants" icon={Users} extra={
        <div className="flex gap-1">
          {[{ id: 'endorsements', label: 'Endorsements' }, { id: 'stox', label: 'STO-X Price' }].map(s => (
            <button
              key={s.id}
              onClick={() => setContestantSort(s.id)}
              className={`text-[9px] font-mono px-1.5 py-0.5 rounded ${
                contestantSort === s.id ? 'bg-tank-accent/15 text-tank-accent' : 'text-tank-muted hover:text-tank-text'
              }`}
            >
              {s.label}
            </button>
          ))}
        </div>
      }>
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2">
          {sortedContestants.map(c => {
            const stock = c.tickerSymbol ? stocks.find(s => s.tickerSymbol === c.tickerSymbol) : null
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
                    {c.freeloader && <span className="text-[9px] bg-yellow-500/10 text-yellow-400 px-1 rounded">Freeloader</span>}
                    {c.eliminatedAt && <span className="text-[9px] text-red-400">Eliminated {formatDateTime(c.eliminatedAt)}</span>}
                    {c.job && <div className="text-[9px] text-tank-muted">{c.job}</div>}
                    <div className="text-[9px] text-tank-muted">Joined {formatDateTime(c.createdAt)}</div>
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

      {/* Peak Hours */}
      {peakHours && peakHours.hourly.length > 0 && (
        <Section title="Peak Activity Hours" icon={Zap}>
          <div className="flex gap-4 mb-3">
            <div>
              <h4 className="text-[10px] font-mono text-tank-muted uppercase mb-1">Busiest</h4>
              <div className="flex gap-2">
                {peakHours.peak.map(h => (
                  <span key={h.hour} className="text-xs font-mono px-2 py-1 rounded bg-green-500/10 text-green-400 border border-green-500/20">
                    {h.ts ? new Date(h.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : h.hour + ':00'} ({h.total.toLocaleString()})
                  </span>
                ))}
              </div>
            </div>
            <div>
              <h4 className="text-[10px] font-mono text-tank-muted uppercase mb-1">Quietest</h4>
              <div className="flex gap-2">
                {peakHours.quietest.map(h => (
                  <span key={h.hour} className="text-xs font-mono px-2 py-1 rounded bg-tank-highlight text-tank-muted border border-tank-border">
                    {h.ts ? new Date(h.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : h.hour + ':00'} ({h.total.toLocaleString()})
                  </span>
                ))}
              </div>
            </div>
          </div>
          <StackedHourlyBar data={peakHours.hourly} />
        </Section>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {/* TTS/SFX Analytics */}
        <Section title="TTS / SFX Analytics" icon={Volume2}
          badge={ttsSentiment && (() => {
            const { label, bgColor, textColor } = getMoodLabel(ttsSentiment.overall.avg)
            return <span className={`text-[9px] font-mono font-semibold px-1.5 py-0.5 rounded ${bgColor} ${textColor}`}>{label}</span>
          })()}
          extra={
          <div className="flex items-center gap-2">
            {ttsToggle !== undefined && (
              <span className={`text-[9px] font-mono px-1.5 py-0.5 rounded ${ttsToggle?.enabled ? 'bg-green-500/10 text-green-400' : 'bg-red-500/10 text-red-400'}`}>
                TTS: {ttsToggle?.enabled ? 'ON' : 'OFF'}{ttsToggle?.metadata ? ` (${ttsToggle.metadata}t)` : ''}
              </span>
            )}
            {sfxToggle !== undefined && (
              <span className={`text-[9px] font-mono px-1.5 py-0.5 rounded ${sfxToggle?.enabled ? 'bg-green-500/10 text-green-400' : 'bg-red-500/10 text-red-400'}`}>
                SFX: {sfxToggle?.enabled ? 'ON' : 'OFF'}{sfxToggle?.metadata ? ` (${sfxToggle.metadata}t)` : ''}
              </span>
            )}
            <TimeFilter value={ttsPeriod} onChange={setTtsPeriod} />
          </div>
        }>
          {ttsAnalytics ? (
            <div className="space-y-3">
              {ttsAnalytics.top_rooms.length > 0 && (
                <div>
                  <h4 className="text-[10px] font-mono text-tank-muted uppercase mb-1">Most Active Rooms</h4>
                  <div className="space-y-1">
                    {ttsAnalytics.top_rooms.map(r => (
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
<span className="font-mono text-tank-warn">{s.spend.toLocaleString()}t <span className="text-green-400">({(s.spend * 0.10).toLocaleString('en-US', {style:'currency',currency:'USD'})})</span></span>                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {ttsAnalytics.hourly.length > 0 && (
                <div>
                  <h4 className="text-[10px] font-mono text-tank-muted uppercase mb-1">Hourly Activity</h4>
                  <HourlyBar data={ttsAnalytics.hourly} color="bg-purple-400" />
                </div>
              )}
              {ttsSentiment && ttsSentiment.hourly.length > 0 && (
                <div>
                  <h4 className="text-[10px] font-mono text-tank-muted uppercase mb-1">Hourly Sentiment</h4>
                  <SentimentHourlyBar data={ttsSentiment.hourly} />
                </div>
              )}
              {ttsSentiment && ttsSentiment.by_target && ttsSentiment.by_target.length > 0 && (
                <div>
                  <h4 className="text-[10px] font-mono text-tank-muted uppercase mb-1">Mood by Contestant</h4>
                  <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-x-4 gap-y-0.5">
                    {ttsSentiment.by_target.map(t => {
                      const { textColor } = getMoodLabel(t.avg_sentiment)
                      return (
                        <div key={t.target} className="flex items-center justify-between text-xs">
                          <span className="text-tank-bright truncate">{t.target}</span>
                          <span className={`font-mono shrink-0 ml-1 ${textColor}`}>
                            {t.avg_sentiment >= 0 ? '+' : ''}{t.avg_sentiment.toFixed(2)}
                          </span>
                        </div>
                      )
                    })}
                  </div>
                </div>
              )}
            </div>
          ) : (
            <div className="text-xs text-tank-muted font-mono">Loading analytics...</div>
          )}
        </Section>

        {/* Chat Analytics */}
        <Section title="Chat Analytics" icon={MessageSquare}
          badge={chatSentiment && (() => {
            const { label, bgColor, textColor } = getMoodLabel(chatSentiment.overall.avg)
            return <span className={`text-[9px] font-mono font-semibold px-1.5 py-0.5 rounded ${bgColor} ${textColor}`}>{label}</span>
          })()}
          extra={
          <div className="flex items-center gap-2">
            <TimeFilter value={chatPeriod} onChange={setChatPeriod} />
          </div>
        }>
          {chatAnalytics ? (
            <div className="space-y-3">
              <div className="text-xs text-tank-muted">
                Total messages: <span className="text-tank-bright font-mono">{chatAnalytics.total.toLocaleString()}</span>
              </div>
              {chatAnalytics.top_chatters.length > 0 && (
                <div>
                  <h4 className="text-[10px] font-mono text-tank-muted uppercase mb-1">Top Chatters</h4>
                  <div className="space-y-1">
                    {chatAnalytics.top_chatters.map(c => (
                      <div key={c.name} className="flex items-center justify-between text-xs">
                        <span className="text-tank-bright">{c.name}</span>
                        <span className="font-mono text-blue-400">{c.count.toLocaleString()}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {chatAnalytics.hourly.length > 0 && (
                <div>
                  <h4 className="text-[10px] font-mono text-tank-muted uppercase mb-1">Hourly Volume</h4>
                  <HourlyBar data={chatAnalytics.hourly} color="bg-blue-400" />
                </div>
              )}
              {chatSentiment && chatSentiment.hourly.length > 0 && (
                <div>
                  <h4 className="text-[10px] font-mono text-tank-muted uppercase mb-1">Hourly Sentiment</h4>
                  <SentimentHourlyBar data={chatSentiment.hourly} />
                </div>
              )}
            </div>
          ) : (
            <div className="text-xs text-tank-muted font-mono">Loading analytics...</div>
          )}
        </Section>
      </div>

      {/* Fishtoy Availability */}
      <Section title="Fishtoy Availability" icon={Fish} extra={
        fishtoyToggle !== undefined && (
          <span className={`text-[9px] font-mono px-1.5 py-0.5 rounded ${fishtoyToggle?.enabled ? 'bg-green-500/10 text-green-400' : 'bg-red-500/10 text-red-400'}`}>
            Fishtoys: {fishtoyToggle?.enabled ? 'Enabled' : 'Disabled'}
          </span>
        )
      }>
        {fishtoyToggle && !fishtoyToggle.enabled && (
          <div className="text-xs text-red-400 font-mono mb-2 p-2 bg-red-500/5 border border-red-500/20 rounded">
            Fishtoys are currently disabled. Items below may show as ON but cannot be used until Production re-enable the toys.
          </div>
        )}
        <div className="grid grid-cols-3 sm:grid-cols-4 lg:grid-cols-6 gap-1.5">
          {[...fishtoyStatus].sort((a, b) => (a.name || '').localeCompare(b.name || '')).map(f => (
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
              {f.enabled && fishtoyToggle && !fishtoyToggle.enabled && (
                <span className="text-[8px] text-red-400/70 font-mono">(fishtoys are currently disabled)</span>
              )}
              {f.type === 'BIGTOY' && (
                <span className="text-[9px] text-purple-400 font-mono">BIGTOY</span>
              )}
            </div>
          ))}
        </div>
      </Section>

      {/* Price Changes */}
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
    </div>
  )
}

export default AnalyticsTab

function Section({ title, icon: Icon, badge, extra, children }) {
  return (
    <div className="bg-tank-surface border border-tank-border rounded-lg p-3">
      <div className="flex items-center justify-between mb-2.5">
        <div className="flex items-center gap-2">
          {Icon && <Icon className="w-4 h-4 text-tank-muted" />}
          <h3 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider">{title}</h3>
          {badge}
        </div>
        {extra}
      </div>
      {children}
    </div>
  )
}

function SentimentHourlyBar({ data }) {
  const sorted = [...data].sort((a, b) => (a.ts || a.hour) < (b.ts || b.hour) ? -1 : 1)
  const maxAbs = Math.max(...sorted.map(d => Math.abs(d.avg_sentiment)), 0.01)
  const halfHeight = 20
  return (
    <div className="flex gap-px" style={{ height: `${halfHeight * 2}px` }}>
      {sorted.map(d => {
        const label = d.ts
          ? new Date(d.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
          : d.hour + ':00'
        const score = d.avg_sentiment || 0
        const count = d.message_count || 0
        const barH = (Math.abs(score) / maxAbs) * halfHeight
        const isPositive = score >= 0
        return (
          <div
            key={d.ts || d.hour}
            className="flex-1 flex flex-col relative"
            title={`${label}\nSentiment: ${score.toFixed(3)}\nMessages: ${count}`}
          >
            {isPositive ? (
              <div className="absolute w-full bg-green-400/70 rounded-t-sm" style={{ bottom: `${halfHeight}px`, height: `${Math.max(barH, 1)}px` }} />
            ) : (
              <div className="absolute w-full bg-red-400/70 rounded-b-sm" style={{ top: `${halfHeight}px`, height: `${Math.max(barH, 1)}px` }} />
            )}
          </div>
        )
      })}
    </div>
  )
}

function HourlyBar({ data, color }) {
  const sorted = [...data].sort((a, b) => (a.ts || a.hour) < (b.ts || b.hour) ? -1 : 1)
  const max = Math.max(...sorted.map(d => d.count), 1)
  return (
    <div className="flex items-end gap-px h-12">
      {sorted.map((d, i) => {
        const label = d.ts
          ? new Date(d.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
          : d.hour + ':00'
        const pct = (d.count / max) * 100
        return (
          <div
            key={d.ts || d.hour}
            className="flex-1 flex flex-col items-center"
            title={`${label} — ${d.count} events`}
          >
            <div className="w-full relative" style={{ height: '40px' }}>
              <div
                className={`absolute bottom-0 w-full rounded-t-sm ${color} opacity-70`}
                style={{ height: `${Math.max(pct, 2)}%` }}
              />
            </div>
            {(i === 0 || i === sorted.length - 1 || i % 6 === 0) && (
              <span className="text-[8px] text-tank-muted mt-0.5">{label}</span>
            )}
          </div>
        )
      })}
    </div>
  )
}

function StackedHourlyBar({ data }) {
  const sorted = [...data].sort((a, b) => (a.ts || a.hour) < (b.ts || b.hour) ? -1 : 1)
  const max = Math.max(...sorted.map(d => d.total), 1)
  return (
    <div>
      <div className="flex items-end gap-px h-20">
        {sorted.map((d, i) => {
          const label = d.ts
            ? new Date(d.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
            : d.hour + ':00'
          const ttsPct = (d.tts / max) * 100
          const sfxPct = (d.sfx / max) * 100
          const fishPct = (d.fishtoys / max) * 100
          return (
            <div
              key={d.ts || d.hour}
              className="flex-1 flex flex-col items-center"
              title={`${label}\nTTS: ${d.tts}\nSFX: ${d.sfx}\nFishtoys: ${d.fishtoys}\nTotal: ${d.total}`}
            >
              <div className="w-full flex flex-col-reverse" style={{ height: '64px' }}>
                <div className="w-full bg-purple-400/70 rounded-t-sm" style={{ height: `${ttsPct}%` }} />
                <div className="w-full bg-indigo-400/70" style={{ height: `${sfxPct}%` }} />
                <div className="w-full bg-emerald-400/70" style={{ height: `${fishPct}%` }} />
              </div>
              {(i === 0 || i === sorted.length - 1 || i % 4 === 0) && (
                <span className="text-[8px] text-tank-muted mt-0.5">{label}</span>
              )}
            </div>
          )
        })}
      </div>
      <div className="flex items-center gap-3 mt-2">
        <span className="flex items-center gap-1 text-[9px] text-tank-muted"><span className="w-2 h-2 rounded-sm bg-purple-400/70" />TTS</span>
        <span className="flex items-center gap-1 text-[9px] text-tank-muted"><span className="w-2 h-2 rounded-sm bg-indigo-400/70" />SFX</span>
        <span className="flex items-center gap-1 text-[9px] text-tank-muted"><span className="w-2 h-2 rounded-sm bg-emerald-400/70" />Fishtoys</span>
      </div>
    </div>
  )
}
