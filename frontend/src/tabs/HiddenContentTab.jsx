import { useState, useEffect, useCallback } from 'react'
import { Virtuoso } from 'react-virtuoso'
import { FileText, Search, X, ArrowRight, Crosshair } from 'lucide-react'
import { formatDateTime } from '../utils/formatTime'

export default function HiddenContentTab({ itemCatalog }) {
  const [items, setItems] = useState([])
  const [search, setSearch] = useState('')
  const [filterTarget, setFilterTarget] = useState(null)
  const [loading, setLoading] = useState(true)
  const [hasMore, setHasMore] = useState(true)
  const [targetData, setTargetData] = useState({ total: 0, targets: [] })

  // Fetch target counts from server (full DB)
  useEffect(() => {
    fetch('/api/hidden-content/targets')
      .then(r => r.json())
      .then(setTargetData)
      .catch(() => {})
  }, [])

  // Reset and fetch when filter/search changes
  useEffect(() => {
    setItems([])
    setHasMore(true)
    loadPage(null)
  }, [filterTarget])

  function loadPage(beforeId) {
    setLoading(true)
    const params = new URLSearchParams({ limit: '200' })
    if (filterTarget) params.set('target', filterTarget)
    if (search.trim()) params.set('search', search.trim())
    if (beforeId != null) params.set('offset', beforeId)

    fetch(`/api/hidden-content?${params}`)
      .then(r => r.json())
      .then(data => {
        if (beforeId != null) {
          setItems(prev => [...prev, ...data])
        } else {
          setItems(data)
        }
        setHasMore(data.length === 200)
        setLoading(false)
      })
      .catch(() => setLoading(false))
  }

  const loadMore = useCallback(() => {
    if (loading || !hasMore || items.length === 0) return
    loadPage(items.length)
  }, [loading, hasMore, items.length, filterTarget, search])

  function handleSearch(e) {
    e.preventDefault()
    setItems([])
    setHasMore(true)
    loadPage(null)
  }

  return (
    <div className="flex-1 flex flex-col md:flex-row gap-3 p-3 min-h-0">
      {/* Main content list */}
      <div className="flex-1 flex flex-col min-w-0">
        <div className="flex items-center gap-3 mb-3 shrink-0">
          <div className="flex items-center gap-2">
            <FileText className="w-5 h-5 text-tank-accent" />
            <h2 className="text-sm font-bold text-tank-bright uppercase tracking-wider">Hidden Content Archive</h2>
            <span className="text-[10px] font-mono text-tank-muted bg-tank-highlight px-1.5 py-0.5 rounded">
              {filterTarget ? `${items.length} / ${targetData.total}` : targetData.total}
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

        <div className="flex-1 min-h-0">
          {loading && items.length === 0 ? (
            <div className="text-xs text-tank-muted font-mono text-center py-8">Loading...</div>
          ) : items.length === 0 ? (
            <div className="text-xs text-tank-muted font-mono text-center py-8">No hidden content found</div>
          ) : (
            <Virtuoso
              style={{ height: '100%' }}
              data={items}
              endReached={loadMore}
              overscan={100}
              itemContent={(index, e) => {
                const d = e.data || {}
                const iid = String(d.itemId || '')
                const cat = itemCatalog[iid]
                const itemName = cat?.name || `Item #${iid}`

                return (
                  <div className="bg-tank-surface border border-tank-border rounded-lg p-3 mb-2">
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
                        <span>{formatDateTime(d.createdAt || d.updatedAt)}</span>
                      </div>
                    </div>
                    <div className="bg-tank-bg border border-tank-accent/20 rounded px-3 py-2">
                      <p className="text-sm text-tank-bright leading-relaxed whitespace-pre-wrap break-words">
                        {typeof d.metadata === 'object' ? JSON.stringify(d.metadata) : d.metadata}
                      </p>
                    </div>
                  </div>
                )
              }}
              components={{
                Footer: () => loading ? (
                  <div className="text-center text-[10px] text-tank-muted py-2">Loading more...</div>
                ) : !hasMore && items.length > 0 ? (
                  <div className="text-center text-[10px] text-tank-muted py-2">All content loaded</div>
                ) : null,
              }}
            />
          )}
        </div>
      </div>

      {/* Sidebar: targets with hidden content */}
      <div className="w-full md:w-[180px] md:shrink-0 flex flex-col gap-2">
        <div className="bg-tank-surface border border-tank-border rounded-lg p-2.5">
          <h3 className="text-[10px] font-mono text-tank-muted uppercase tracking-wider mb-2 flex items-center gap-1.5"><Crosshair className="w-3 h-3" />By Target</h3>
          <div className="space-y-1">
            {targetData.targets.map(({ target, count }) => (
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
