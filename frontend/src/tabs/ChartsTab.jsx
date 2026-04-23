import { useState, useEffect, useMemo, useCallback, useRef } from 'react'
import { TrendingUp, DollarSign, MessageSquare, Type } from 'lucide-react'
import AnchorRow, { RANGE_MS } from '../components/AnchorRow'
import { okJson, apiFetch } from '../utils/fetchUtils'
import {
  LineChart, Line, BarChart, Bar, AreaChart, Area,
  XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer,
} from 'recharts'

const RANGES = ['30m', '1h', '2h', '6h', '12h', '24h', '3d', '7d', 'all']
const KEYWORD_RANGES = ['6h', '12h', '24h', '3d', '7d']

const TICKER_COLORS = [
  '#60a5fa', '#34d399', '#f59e0b', '#f87171', '#a78bfa',
  '#fb7185', '#38bdf8', '#4ade80', '#facc15', '#c084fc',
  '#fb923c', '#2dd4bf', '#e879f9', '#86efac', '#fca5a5',
]

const SPEND_COLORS = {
  tts: '#a78bfa', sfx: '#818cf8', fishtoy: '#34d399', poll: '#f59e0b', superchat: '#f472b6',
}
const SPEND_LABELS = {
  tts: 'TTS', sfx: 'SFX', fishtoy: 'Fishtoy', poll: 'Poll Votes', superchat: 'Superchat',
}

const KEYWORD_COLORS = ['#06b6d4', '#a855f7', '#f59e0b', '#10b981', '#f43f5e', '#3b82f6', '#f97316', '#84cc16']

function tokensToUSD(t) { return `$${(t * 0.10).toFixed(2)}` }

function parseTs(ts) {
  if (!ts) return null
  let s = ts.replace(' ', 'T')
  // Handle hourly bucket format "2026-04-12T15" — append :00:00 for valid ISO
  if (/^\d{4}-\d{2}-\d{2}T\d{1,2}$/.test(s)) s += ':00:00'
  if (!s.includes('+') && !s.endsWith('Z')) s += 'Z'
  const d = new Date(s)
  return isNaN(d.getTime()) ? null : d
}

function formatTsLabel(ts, range) {
  const d = parseTs(ts)
  if (!d) return ts ?? ''
  if (range === 'all' || range === '7d')
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
  if (range === '3d')
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' +
      d.toLocaleTimeString([], { hour: 'numeric', hour12: true })
  return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', hour12: true })
}

function formatKeywordTsLabel(ts, range) {
  const d = parseTs(ts)
  if (!d) return ts ?? ''
  const hh = String(d.getUTCHours()).padStart(2, '0')
  if (range === '6h' || range === '12h' || range === '24h')
    return `${hh}:00`
  const mm = String(d.getUTCMonth() + 1).padStart(2, '0')
  const dd = String(d.getUTCDate()).padStart(2, '0')
  return `${mm}/${dd} ${hh}:00`
}

function tickInterval(len, target = 10) {
  return len <= target ? 0 : Math.ceil(len / target) - 1
}

function RangeButtons({ value, onChange, ranges = RANGES }) {
  return (
    <div className="flex gap-1">
      {ranges.map(r => (
        <button
          key={r}
          onClick={() => onChange(r)}
          className={`text-[10px] font-mono px-2 py-0.5 rounded border transition-colors ${
            value === r
              ? 'border-tank-accent text-tank-accent bg-tank-accent/10'
              : 'border-tank-border text-tank-muted hover:text-tank-text'
          }`}
        >{r}</button>
      ))}
    </div>
  )
}


function ToggleButton({ active, color, label, onClick }) {
  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-1.5 text-[10px] font-mono px-2 py-0.5 rounded border transition-colors ${
        active ? 'border-current' : 'border-tank-border opacity-40'
      }`}
      style={active ? { color, borderColor: color, background: color + '15' } : {}}
    >
      <span className="w-2 h-2 rounded-full shrink-0" style={{ background: active ? color : '#555566' }} />
      {label}
    </button>
  )
}

function ChartPanel({ title, icon: Icon, children, controls }) {
  return (
    <div className="bg-tank-surface border border-tank-border rounded-lg p-4 space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="flex items-center gap-1.5 text-xs font-mono font-semibold text-tank-bright uppercase tracking-wider">
          {Icon && <Icon size={14} className="text-tank-accent shrink-0" />}
          {title}
        </h2>
        {controls}
      </div>
      {children}
    </div>
  )
}

function EmptyChart({ text }) {
  return <div className="flex items-center justify-center h-40 text-xs font-mono text-tank-muted">{text}</div>
}

function DarkTooltip({ active, payload, label, range, unit, labelMap }) {
  if (!active || !payload?.length) return null
  const visible = payload.filter(e => e.value != null)
  const total = visible.reduce((sum, e) => sum + e.value, 0)
  return (
    <div className="bg-[#111115] border border-[#1e1e24] rounded px-3 py-2 text-xs font-mono shadow-xl">
      <div className="text-[#555566] mb-1.5">{formatTsLabel(label, range)}</div>
      {visible.map(entry => {
        const displayKey = labelMap?.[entry.dataKey] ?? entry.dataKey
        const val = unit === 'usd' ? tokensToUSD(entry.value) : entry.value.toLocaleString() + 't'
        return (
          <div key={entry.dataKey} className="flex items-center gap-2 leading-5">
            <span style={{ color: entry.color ?? entry.fill }} className="shrink-0">{displayKey}:</span>
            <span className="text-[#c8c8d0]">{val}</span>
          </div>
        )
      })}
      {visible.length > 1 && (
        <div className="flex items-center gap-2 leading-5 border-t border-[#1e1e24] mt-1 pt-1">
          <span className="text-[#555566] shrink-0">Total:</span>
          <span className="text-tank-bright">{unit === 'usd' ? tokensToUSD(total) : total.toLocaleString() + 't'}</span>
        </div>
      )}
    </div>
  )
}

function StockTooltip({ active, payload, label, range, tickerColors }) {
  if (!active || !payload?.length) return null
  return (
    <div className="bg-[#111115] border border-[#1e1e24] rounded px-3 py-2 text-xs font-mono shadow-xl">
      <div className="text-[#555566] mb-1.5">{formatTsLabel(label, range)}</div>
      {payload.filter(e => e.value != null).map(entry => (
        <div key={entry.dataKey} className="flex items-center gap-2 leading-5">
          <span style={{ color: tickerColors[entry.dataKey] }} className="shrink-0">{entry.dataKey}:</span>
          <span className="text-[#c8c8d0]">{entry.value?.toLocaleString()}</span>
        </div>
      ))}
    </div>
  )
}

function ChatVolumeTooltip({ active, payload, label, range }) {
  if (!active || !payload?.length) return null
  return (
    <div className="bg-[#111115] border border-[#1e1e24] rounded px-3 py-2 text-xs font-mono shadow-xl">
      <div className="text-[#555566] mb-1">{formatTsLabel(label, range)}</div>
      <div className="flex items-center gap-2">
        <span style={{ color: '#60a5fa' }}>messages:</span>
        <span className="text-[#c8c8d0]">{payload[0].value?.toLocaleString()}</span>
      </div>
    </div>
  )
}

function KeywordTooltip({ active, payload, label, range }) {
  if (!active || !payload?.length) return null
  const visible = payload.filter(e => e.value != null && e.value > 0)
  const total = visible.reduce((sum, e) => sum + e.value, 0)
  return (
    <div className="bg-[#111115] border border-[#1e1e24] rounded px-3 py-2 text-xs font-mono shadow-xl">
      <div className="text-[#555566] mb-1.5">{formatKeywordTsLabel(label, range)}</div>
      {visible.map(entry => (
        <div key={entry.dataKey} className="flex items-center gap-2 leading-5">
          <span className="w-2 h-2 rounded-full shrink-0" style={{ background: entry.color ?? entry.fill }} />
          <span style={{ color: entry.color ?? entry.fill }} className="shrink-0">{entry.dataKey}:</span>
          <span className="text-[#c8c8d0]">{entry.value.toLocaleString()}</span>
        </div>
      ))}
      {visible.length > 1 && (
        <div className="flex items-center gap-2 leading-5 border-t border-[#1e1e24] mt-1 pt-1">
          <span className="text-[#555566] shrink-0">Total:</span>
          <span className="text-tank-bright">{total.toLocaleString()}</span>
        </div>
      )}
    </div>
  )
}

const axisStyle = { fill: '#555566', fontSize: 10, fontFamily: 'JetBrains Mono, monospace' }

export default function ChartsTab({ stocks }) {
  const [stockRange, setStockRange] = useState('24h')
  const [stockData, setStockData] = useState({})
  const [stockToggles, setStockToggles] = useState({})
  const [showEliminated, setShowEliminated] = useState(false)
  const [stockLoading, setStockLoading] = useState(false)

  const [spendRange, setSpendRange] = useState('24h')
  const [spendData, setSpendData] = useState({ granularity: 'hourly', data: [] })
  const [spendToggles, setSpendToggles] = useState({ tts: true, sfx: true, fishtoy: true, poll: true, superchat: true })
  const [spendUnit, setSpendUnit] = useState('tokens')
  const [spendLoading, setSpendLoading] = useState(false)

  const [chatRange, setChatRange] = useState('24h')
  const [chatData, setChatData] = useState({ data: [], top_chatters: [] })
  const [chatLoading, setChatLoading] = useState(false)

  const [stockAnchor, setStockAnchor] = useState(null)
  const [stockAnchorLabel, setStockAnchorLabel] = useState('now')
  const [spendAnchor, setSpendAnchor] = useState(null)
  const [spendAnchorLabel, setSpendAnchorLabel] = useState('now')
  const [chatAnchor, setChatAnchor] = useState(null)
  const [chatAnchorLabel, setChatAnchorLabel] = useState('now')

  const handleStockAnchor = useCallback((a, label) => {
    setStockAnchor(a); if (label) setStockAnchorLabel(label)
    else setStockAnchorLabel(a ? 'custom' : 'now')
  }, [])
  const handleSpendAnchor = useCallback((a, label) => {
    setSpendAnchor(a); if (label) setSpendAnchorLabel(label)
    else setSpendAnchorLabel(a ? 'custom' : 'now')
  }, [])
  const handleChatAnchor = useCallback((a, label) => {
    setChatAnchor(a); if (label) setChatAnchorLabel(label)
    else setChatAnchorLabel(a ? 'custom' : 'now')
  }, [])

  const [keywordChartData, setKeywordChartData] = useState([])
  const [keywordTopWords, setKeywordTopWords] = useState([])
  const [keywordRange, setKeywordRange] = useState('24h')
  const [keywordAnchor, setKeywordAnchor] = useState(null)
  const [keywordAnchorLabel, setKeywordAnchorLabel] = useState('now')
  const handleKeywordAnchor = useCallback((a, label) => {
    setKeywordAnchor(a); if (label) setKeywordAnchorLabel(label)
    else setKeywordAnchorLabel(a ? 'custom' : 'now')
  }, [])

  // Drag-to-pan state
  const dragRef = useRef(null)

  const handleDragStart = useCallback((e, chartId, anchor, range) => {
    dragRef.current = { startX: e.clientX, chartId, anchor, range, width: e.currentTarget.offsetWidth }
  }, [])

  const handleDragEnd = useCallback((e) => {
    if (!dragRef.current) return
    const { startX, chartId, anchor, range, width } = dragRef.current
    dragRef.current = null
    const dx = startX - e.clientX
    if (Math.abs(dx) < 5) return
    e.preventDefault()
    const rangeMs = RANGE_MS[range] || RANGE_MS['24h']
    const timeDelta = (dx / width) * rangeMs
    const ref = anchor ? new Date(anchor).getTime() : Date.now()
    const next = new Date(ref + timeDelta)
    const handler = chartId === 'stock' ? handleStockAnchor : chartId === 'spend' ? handleSpendAnchor : chartId === 'keywords' ? handleKeywordAnchor : handleChatAnchor
    if (next >= new Date()) handler(null, 'now')
    else handler(next.toISOString(), null)
  }, [handleStockAnchor, handleSpendAnchor, handleChatAnchor, handleKeywordAnchor])

  // Keyboard pan — arrows pan all charts together
  useEffect(() => {
    function onKey(e) {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return
      e.preventDefault()
      const dir = e.key === 'ArrowLeft' ? -1 : 1
      ;[
        [stockAnchor, stockRange, handleStockAnchor],
        [spendAnchor, spendRange, handleSpendAnchor],
        [chatAnchor, chatRange, handleChatAnchor],
        [keywordAnchor, keywordRange, handleKeywordAnchor],
      ].forEach(([anchor, range, handler]) => {
        const step = RANGE_MS[range] || RANGE_MS['24h']
        const ref = anchor ? new Date(anchor).getTime() : Date.now()
        const next = new Date(ref + dir * step)
        if (next >= new Date()) handler(null, 'now')
        else handler(next.toISOString(), null)
      })
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [stockAnchor, stockRange, spendAnchor, spendRange, chatAnchor, chatRange,
      keywordAnchor, keywordRange,
      handleStockAnchor, handleSpendAnchor, handleChatAnchor, handleKeywordAnchor])

  const activeTickers = useMemo(
    () => new Set(stocks.map(s => s.tickerSymbol).filter(Boolean)),
    [stocks]
  )

  const [allSeenTickers, setAllSeenTickers] = useState([])

  useEffect(() => {
    function fetchStocks() {
      if (document.hidden) return
      setStockLoading(true)
      const params = new URLSearchParams({ range: stockRange })
      if (stockAnchor) params.set('anchor', stockAnchor)
      apiFetch(`/api/charts/stocks?${params}`)
        .then(okJson).then(setStockData).catch(() => {})
        .finally(() => setStockLoading(false))
    }
    fetchStocks()
    if (!stockAnchor) {
      const id = setInterval(fetchStocks, 5 * 60 * 1000)
      return () => clearInterval(id)
    }
  }, [stockRange, stockAnchor])

  useEffect(() => {
    const incoming = Object.keys(stockData)
    if (incoming.length === 0) return
    setAllSeenTickers(prev => {
      const prevSet = new Set(prev)
      const newOnes = incoming.filter(t => !prevSet.has(t))
      return newOnes.length > 0 ? [...prev, ...newOnes].sort() : prev
    })
    setStockToggles(prev => {
      const next = { ...prev }
      let changed = false
      incoming.forEach(ticker => {
        if (!(ticker in next)) { next[ticker] = activeTickers.has(ticker); changed = true }
      })
      return changed ? next : prev
    })
  }, [stockData, activeTickers])

  useEffect(() => {
    function fetchSpend() {
      if (document.hidden) return
      setSpendLoading(true)
      const params = new URLSearchParams({ range: spendRange })
      if (spendAnchor) params.set('anchor', spendAnchor)
      apiFetch(`/api/charts/spend?${params}`)
        .then(okJson).then(setSpendData).catch(() => {})
        .finally(() => setSpendLoading(false))
    }
    fetchSpend()
    if (!spendAnchor) {
      const id = setInterval(fetchSpend, 5 * 60 * 1000)
      return () => clearInterval(id)
    }
  }, [spendRange, spendAnchor])

  useEffect(() => {
    function fetchChat() {
      if (document.hidden) return
      setChatLoading(true)
      const params = new URLSearchParams({ range: chatRange })
      if (chatAnchor) params.set('anchor', chatAnchor)
      apiFetch(`/api/charts/chatters?${params}`)
        .then(okJson).then(setChatData).catch(() => {})
        .finally(() => setChatLoading(false))
    }
    fetchChat()
    if (!chatAnchor) {
      const id = setInterval(fetchChat, 5 * 60 * 1000)
      return () => clearInterval(id)
    }
  }, [chatRange, chatAnchor])

  useEffect(() => {
    function fetchKeywords() {
      if (document.hidden) return
      const rangeMs = RANGE_MS[keywordRange]
      const ref = keywordAnchor ? new Date(keywordAnchor).getTime() : Date.now()
      const since = rangeMs ? new Date(ref - rangeMs).toISOString() : undefined
      const params = new URLSearchParams()
      if (since) params.set('since', since)
      if (keywordAnchor) params.set('until', keywordAnchor)
      apiFetch(`/api/analytics/keywords?${params}`)
        .then(okJson)
        .then(d => {
          const top6 = (d.top_keywords || []).slice(0, 6)
          setKeywordTopWords(top6)
          const words = top6.map(k => k.word)
          const points = (d.hourly || []).map(h => {
            const point = { time: h.bucket }
            words.forEach(w => { point[w] = 0 })
            h.top.forEach(k => { if (words.includes(k.word)) point[k.word] = k.count })
            return point
          })
          setKeywordChartData(points)
        })
        .catch(() => {})
    }
    fetchKeywords()
    if (!keywordAnchor) {
      const id = setInterval(fetchKeywords, 5 * 60 * 1000)
      return () => clearInterval(id)
    }
  }, [keywordRange, keywordAnchor])

  const tickerColors = useMemo(() => {
    const map = {}
    allSeenTickers.forEach((t, i) => { map[t] = TICKER_COLORS[i % TICKER_COLORS.length] })
    return map
  }, [allSeenTickers])

  const eliminatedTickers = useMemo(
    () => Object.keys(stockData).filter(t => !activeTickers.has(t)),
    [stockData, activeTickers]
  )

  const stockChartData = useMemo(() => {
    const tickers = Object.keys(stockData)
    if (tickers.length === 0) return []
    const indexed = {}
    tickers.forEach(t => { indexed[t] = {}; stockData[t].forEach(p => { indexed[t][p.ts] = p.price }) })
    const tsSet = new Set()
    tickers.forEach(t => stockData[t].forEach(p => tsSet.add(p.ts)))
    return [...tsSet].sort().map(ts => {
      const point = { ts }
      tickers.forEach(t => { point[t] = indexed[t][ts] ?? null })
      return point
    })
  }, [stockData])

  const visibleTickers = useMemo(() =>
    new Set(Object.keys(stockToggles).filter(t => stockToggles[t])),
    [stockToggles]
  )

  function toggleEliminated() {
    const next = !showEliminated
    setShowEliminated(next)
    setStockToggles(prev => {
      const updated = { ...prev }
      eliminatedTickers.forEach(t => { updated[t] = next })
      return updated
    })
  }

  return (
    <div className="flex-1 overflow-y-auto p-3 space-y-3 min-h-0">

      {/* STO-X Price History */}
      <ChartPanel title="STO-X Price History" icon={TrendingUp} controls={
        <div className="flex flex-col items-end gap-1">
          <div className="flex flex-wrap items-center gap-2">
            <RangeButtons value={stockRange} onChange={setStockRange} />
            {eliminatedTickers.length > 0 && (
              <button onClick={toggleEliminated} className={`text-[10px] font-mono px-2 py-0.5 rounded border transition-colors ${
                showEliminated ? 'border-amber-400 text-amber-400 bg-amber-400/10' : 'border-tank-border text-tank-muted hover:text-tank-text'
              }`}>{showEliminated ? 'Hide Eliminated' : 'Show Eliminated'}</button>
            )}
          </div>
          <AnchorRow anchor={stockAnchor} anchorLabel={stockAnchorLabel} onAnchorChange={handleStockAnchor} range={stockRange} />
        </div>
      }>
        {Object.keys(stockData).length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {Object.keys(stockData).sort().map(ticker => {
              const isElim = !activeTickers.has(ticker)
              if (isElim && !showEliminated) return null
              return (
                <ToggleButton key={ticker} active={stockToggles[ticker] ?? true} color={tickerColors[ticker]}
                  label={isElim ? `${ticker} \u2715` : ticker}
                  onClick={() => setStockToggles(prev => ({ ...prev, [ticker]: !prev[ticker] }))} />
              )
            })}
          </div>
        )}
        {stockLoading ? <EmptyChart text="Loading..." /> : stockChartData.length === 0 ? <EmptyChart text="No stock price history collected yet." /> : (
          <div style={{ cursor: 'grab' }} onMouseDown={e => handleDragStart(e, 'stock', stockAnchor, stockRange)}
            onMouseUp={handleDragEnd} onMouseLeave={handleDragEnd}>
            <ResponsiveContainer width="100%" height={280}>
              <LineChart data={stockChartData} margin={{ top: 4, right: 8, left: 0, bottom: 4 }}>
                <CartesianGrid stroke="#1e1e24" strokeDasharray="3 3" vertical={false} />
                <XAxis dataKey="ts" tick={axisStyle} tickLine={false} axisLine={{ stroke: '#1e1e24' }}
                  interval={tickInterval(stockChartData.length)} tickFormatter={ts => formatTsLabel(ts, stockRange)} />
                <YAxis tick={axisStyle} tickLine={false} axisLine={false} width={40} domain={['auto', 'auto']}
                  tickFormatter={v => v.toLocaleString()} />
                <Tooltip content={<StockTooltip range={stockRange} tickerColors={tickerColors} />} />
                {Object.keys(stockData).sort().map(ticker => {
                  if (!visibleTickers.has(ticker)) return null
                  return <Line key={ticker} type="monotone" dataKey={ticker} stroke={tickerColors[ticker]}
                    strokeWidth={1.5} dot={false} connectNulls isAnimationActive={false} />
                })}
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}
      </ChartPanel>

      {/* Token Spend Trends */}
      <ChartPanel title="Token Spend Trends" icon={DollarSign} controls={
        <div className="flex flex-col items-end gap-1">
          <div className="flex flex-wrap items-center gap-2">
            <RangeButtons value={spendRange} onChange={setSpendRange} />
            <div className="flex gap-1">
              {['tokens', 'usd'].map(u => (
                <button key={u} onClick={() => setSpendUnit(u)} className={`text-[10px] font-mono px-2 py-0.5 rounded border transition-colors ${
                  spendUnit === u ? 'border-tank-accent text-tank-accent bg-tank-accent/10' : 'border-tank-border text-tank-muted hover:text-tank-text'
                }`}>{u === 'usd' ? 'USD' : 'Tokens'}</button>
              ))}
            </div>
            <div className="flex gap-1">
              {['tts', 'sfx', 'fishtoy', 'poll', 'superchat'].map(type => (
                <ToggleButton key={type} active={spendToggles[type]} color={SPEND_COLORS[type]}
                  label={SPEND_LABELS[type]} onClick={() => setSpendToggles(prev => ({ ...prev, [type]: !prev[type] }))} />
              ))}
            </div>
          </div>
          <AnchorRow anchor={spendAnchor} anchorLabel={spendAnchorLabel} onAnchorChange={handleSpendAnchor} range={spendRange} />
        </div>
      }>
        {spendLoading ? <EmptyChart text="Loading..." /> : spendData.data.length === 0 ? <EmptyChart text="No spend data for this range." /> : (
          <div style={{ cursor: 'grab' }} onMouseDown={e => handleDragStart(e, 'spend', spendAnchor, spendRange)}
            onMouseUp={handleDragEnd} onMouseLeave={handleDragEnd}>
            <div>
              <h3 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider mb-2 border-l-2 border-tank-accent/30 pl-1.5">Spend by Period</h3>
              <ResponsiveContainer width="100%" height={200}>
                <BarChart data={spendData.data} margin={{ top: 4, right: 8, left: 0, bottom: 4 }}>
                  <CartesianGrid stroke="#1e1e24" strokeDasharray="3 3" vertical={false} />
                  <XAxis dataKey="ts" tick={axisStyle} tickLine={false} axisLine={{ stroke: '#1e1e24' }}
                    interval={tickInterval(spendData.data.length)} tickFormatter={ts => formatTsLabel(ts, spendRange)} />
                  <YAxis tick={axisStyle} tickLine={false} axisLine={false} width={50} domain={[0, 'auto']}
                    tickFormatter={v => spendUnit === 'usd' ? `$${(v * 0.10).toFixed(0)}` : v.toLocaleString()} />
                  <Tooltip content={<DarkTooltip range={spendRange} unit={spendUnit} labelMap={SPEND_LABELS} />} />
                  {['tts', 'sfx', 'fishtoy', 'poll', 'superchat'].map(type => {
                    if (!spendToggles[type]) return null
                    return <Bar key={type} dataKey={type} stackId="spend" fill={SPEND_COLORS[type]} fillOpacity={0.75} isAnimationActive={false} />
                  })}
                </BarChart>
              </ResponsiveContainer>
            </div>
            <div>
              <h3 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider mb-2 border-l-2 border-tank-accent/30 pl-1.5">Spend Trend</h3>
              <ResponsiveContainer width="100%" height={200}>
                <LineChart data={spendData.data} margin={{ top: 4, right: 8, left: 0, bottom: 4 }}>
                  <CartesianGrid stroke="#1e1e24" strokeDasharray="3 3" vertical={false} />
                  <XAxis dataKey="ts" tick={axisStyle} tickLine={false} axisLine={{ stroke: '#1e1e24' }}
                    interval={tickInterval(spendData.data.length)} tickFormatter={ts => formatTsLabel(ts, spendRange)} />
                  <YAxis tick={axisStyle} tickLine={false} axisLine={false} width={50} domain={['auto', 'auto']}
                    tickFormatter={v => spendUnit === 'usd' ? `$${(v * 0.10).toFixed(0)}` : v.toLocaleString()} />
                  <Tooltip content={<DarkTooltip range={spendRange} unit={spendUnit} labelMap={SPEND_LABELS} />} />
                  {['tts', 'sfx', 'fishtoy', 'poll', 'superchat'].map(type => {
                    if (!spendToggles[type]) return null
                    return <Line key={type} type="monotone" dataKey={type} stroke={SPEND_COLORS[type]}
                      strokeWidth={1.5} dot={false} connectNulls isAnimationActive={false} />
                  })}
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>
        )}
      </ChartPanel>

      {/* Chat Volume */}
      <ChartPanel title="Chat Volume" icon={MessageSquare} controls={
        <div className="flex flex-col items-end gap-1">
          <RangeButtons value={chatRange} onChange={setChatRange} />
          <AnchorRow anchor={chatAnchor} anchorLabel={chatAnchorLabel} onAnchorChange={handleChatAnchor} range={chatRange} />
        </div>
      }>
        {chatLoading ? <EmptyChart text="Loading..." /> : chatData.data.length === 0 ? <EmptyChart text="No chat data for this range." /> : (
          <div className="space-y-4">
            <div style={{ cursor: 'grab' }} onMouseDown={e => handleDragStart(e, 'chat', chatAnchor, chatRange)}
              onMouseUp={handleDragEnd} onMouseLeave={handleDragEnd}>
              <ResponsiveContainer width="100%" height={200}>
                <BarChart data={chatData.data} margin={{ top: 4, right: 8, left: 0, bottom: 4 }}>
                  <CartesianGrid stroke="#1e1e24" strokeDasharray="3 3" vertical={false} />
                  <XAxis dataKey="ts" tick={axisStyle} tickLine={false} axisLine={{ stroke: '#1e1e24' }}
                    interval={tickInterval(chatData.data.length)} tickFormatter={ts => formatTsLabel(ts, chatRange)} />
                  <YAxis tick={axisStyle} tickLine={false} axisLine={false} width={55} domain={[0, 'auto']}
                    tickFormatter={v => v.toLocaleString()} />
                  <Tooltip content={<ChatVolumeTooltip range={chatRange} />} />
                  <Bar dataKey="count" fill="#60a5fa" fillOpacity={0.75} isAnimationActive={false} />
                </BarChart>
              </ResponsiveContainer>
            </div>

            {chatData.top_chatters.length > 0 && (
              <div>
                <h3 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider mb-2 border-l-2 border-tank-accent/30 pl-1.5">Top Chatters</h3>
                <div className="space-y-1">
                  {chatData.top_chatters.map((c, i) => {
                    const max = chatData.top_chatters[0].count
                    const pct = (c.count / max) * 100
                    return (
                      <div key={c.name} className="flex items-center gap-2 text-xs font-mono">
                        <span className="text-tank-muted w-4 shrink-0 text-right">{i + 1}</span>
                        <div className="flex-1 relative h-5 flex items-center">
                          <div className="absolute inset-y-0 left-0 rounded-sm bg-blue-500/20" style={{ width: `${pct}%` }} />
                          <span className="relative pl-1.5 text-tank-text truncate">{c.name}</span>
                        </div>
                        <span className="text-tank-muted shrink-0">{c.count.toLocaleString()}</span>
                      </div>
                    )
                  })}
                </div>
              </div>
            )}
          </div>
        )}
      </ChartPanel>

      {/* Chat Keyword Trends */}
      <ChartPanel title="Chat Keyword Trends" icon={Type} controls={
        <div className="flex flex-col items-end gap-1">
          <RangeButtons value={keywordRange} onChange={setKeywordRange} ranges={KEYWORD_RANGES} />
          <AnchorRow anchor={keywordAnchor} anchorLabel={keywordAnchorLabel} onAnchorChange={handleKeywordAnchor} range={keywordRange} />
        </div>
      }>
        {keywordChartData.length === 0 ? <EmptyChart text="No chat keyword data for this range." /> : (
          <div>
            <div style={{ cursor: 'grab' }} onMouseDown={e => handleDragStart(e, 'keywords', keywordAnchor, keywordRange)}
              onMouseUp={handleDragEnd} onMouseLeave={handleDragEnd}>
              <ResponsiveContainer width="100%" height={250}>
                <AreaChart data={keywordChartData} margin={{ top: 4, right: 8, left: 0, bottom: 4 }}>
                  <CartesianGrid stroke="#1e1e24" strokeDasharray="3 3" vertical={false} />
                  <XAxis dataKey="time" tick={axisStyle} tickLine={false} axisLine={{ stroke: '#1e1e24' }}
                    interval={tickInterval(keywordChartData.length)} tickFormatter={v => formatKeywordTsLabel(v, keywordRange)} />
                  <YAxis tick={axisStyle} tickLine={false} axisLine={false} width={40} domain={[0, 'auto']}
                    tickFormatter={v => v.toLocaleString()} />
                  <Tooltip content={<KeywordTooltip range={keywordRange} />} />
                  {keywordTopWords.map((kw, i) => (
                    <Area key={kw.word} type="monotone" dataKey={kw.word} stackId="1"
                      fill={KEYWORD_COLORS[i % KEYWORD_COLORS.length]}
                      stroke={KEYWORD_COLORS[i % KEYWORD_COLORS.length]}
                      fillOpacity={0.6} isAnimationActive={false} />
                  ))}
                </AreaChart>
              </ResponsiveContainer>
            </div>
            <div className="flex flex-wrap gap-2 mt-1 px-2">
              {keywordTopWords.map((kw, i) => (
                <div key={kw.word} className="flex items-center gap-1 text-[10px]">
                  <div className="w-2.5 h-2.5 rounded-sm" style={{ backgroundColor: KEYWORD_COLORS[i % KEYWORD_COLORS.length] }} />
                  <span className="text-tank-muted">{kw.word}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </ChartPanel>
    </div>
  )
}
