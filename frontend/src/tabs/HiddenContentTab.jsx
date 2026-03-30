import { useState, useEffect } from 'react'
import { FileText, Search, X, ArrowRight } from 'lucide-react'

function formatTime(ts) {
  if (!ts) return ''
  const ms = ts > 1e12 ? ts : ts * 1000
  const d = new Date(ms)
  return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' }) + ' ' +
    d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export default function HiddenContentTab({ itemCatalog }) {
  const [items, setItems] = useState([])
  const [search, setSearch] = useState('')
  const [filterTarget, setFilterTarget] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    loadContent()
  }, [filterTarget])

  function loadContent() {
    setLoading(true)
    const params = new URLSearchParams({ limit: '500' })
    if (filterTarget) params.set('target', filterTarget)
    if (search.trim()) params.set('search', search.trim())

    fetch(`/api/hidden-content?${params}`)
      .then(r => r.json())
      .then(data => { setItems(data); setLoading(false) })
      .catch(() => setLoading(false))
  }

  function handleSearch(e) {
    e.preventDefault()
    loadContent()
  }

  // Build target list from loaded items
  const targets = {}
  items.forEach(e => {
    const t = e.data?.target
    if (t) targets[t] = (targets[t] || 0) + 1
  })
  const targetList = Object.entries(targets).sort((a, b) => b[1] - a[1])

  return (
    <div className="flex-1 flex gap-3 p-3 min-h-0">
      {/* Main content list */}
      <div className="flex-1 flex flex-col min-w-0">
        <div className="flex items-center gap-3 mb-3 shrink-0">
          <div className="flex items-center gap-2">
            <FileText className="w-5 h-5 text-tank-accent" />
            <h2 className="text-sm font-bold text-tank-bright uppercase tracking-wider">Hidden Content Archive</h2>
            <span className="text-[10px] font-mono text-tank-muted bg-tank-highlight px-1.5 py-0.5 rounded">
              {items.length}
            </span>
          </div>
          {filterTarget && (
            <span className="text-[10px] font-mono text-tank-warn bg-tank-warn/10 px-2 py-0.5 rounded flex items-center gap-1">
              {filterTarget}
              <button onClick={() => setFilterTarget(null)} className="hover:text-tank-danger"><X className="w-2.5 h-2.5" /></button>
            </span>
          )}
        </div>

        <form onSubmit={handleSearch} className="flex gap-2 mb-3 shrink-0">
          <div className="relative flex-1">
            <Search className="w-3.5 h-3.5 text-tank-muted absolute left-2.5 top-1/2 -translate-y-1/2" />
            <input
              type="text"
              placeholder="Search hidden content..."
              value={search}
              onChange={e => setSearch(e.target.value)}
              className="w-full bg-tank-bg border border-tank-border rounded text-sm text-tank-text pl-8 pr-3 py-1.5 font-mono placeholder:text-tank-muted/50 focus:border-tank-accent/50 focus:outline-none"
            />
          </div>
          <button type="submit" className="bg-tank-accent/10 border border-tank-accent/30 text-tank-accent text-xs font-mono px-3 py-1.5 rounded hover:bg-tank-accent/20">
            Search
          </button>
        </form>

        <div className="flex-1 overflow-y-auto space-y-2">
          {loading ? (
            <div className="text-xs text-tank-muted font-mono text-center py-8">Loading...</div>
          ) : items.length === 0 ? (
            <div className="text-xs text-tank-muted font-mono text-center py-8">No hidden content found</div>
          ) : (
            items.map(e => {
              const d = e.data || {}
              const iid = String(d.itemId || '')
              const cat = itemCatalog[iid]
              const itemName = cat?.name || `Item #${iid}`

              return (
                <div key={e.id} className="bg-tank-surface border border-tank-border rounded-lg p-3">
                  <div className="flex items-center justify-between mb-2">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-semibold text-tank-bright">{d.displayName || '?'}</span>
                      <span className="text-xs text-tank-muted">{itemName}</span>
                      {d.target && (
                        <>
                          <ArrowRight className="w-3 h-3 text-tank-muted" />
                          <button
                            onClick={() => setFilterTarget(d.target)}
                            className="text-xs text-tank-warn font-medium hover:underline"
                          >
                            {d.target}
                          </button>
                        </>
                      )}
                    </div>
                    <div className="flex items-center gap-2 text-[10px] font-mono text-tank-muted">
                      {d.cost > 0 && <span className="text-tank-warn">{d.cost}t</span>}
                      <span>{formatTime(d.createdAt || d.updatedAt)}</span>
                    </div>
                  </div>
                  <div className="bg-tank-bg border border-tank-accent/20 rounded px-3 py-2">
                    <p className="text-sm text-tank-bright leading-relaxed whitespace-pre-wrap break-words">
                      {typeof d.metadata === 'object' ? JSON.stringify(d.metadata) : d.metadata}
                    </p>
                  </div>
                </div>
              )
            })
          )}
        </div>
      </div>

      {/* Sidebar: targets with hidden content */}
      <div className="w-[180px] shrink-0 flex flex-col gap-2">
        <div className="bg-tank-surface border border-tank-border rounded-lg p-2.5">
          <h3 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider mb-2">By Target</h3>
          <div className="space-y-1">
            {targetList.map(([target, count]) => (
              <button
                key={target}
                onClick={() => setFilterTarget(filterTarget === target ? null : target)}
                className={`w-full flex items-center justify-between px-2 py-1 rounded text-xs transition-colors ${
                  filterTarget === target
                    ? 'bg-tank-accent/10 text-tank-accent'
                    : 'hover:bg-tank-highlight text-tank-text'
                }`}
              >
                <span>{target}</span>
                <span className="font-mono text-tank-muted">{count}</span>
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
