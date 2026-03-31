import { useState } from 'react'
import { Fish, ArrowRight, FileText, ChevronDown } from 'lucide-react'

function formatTime(ts) {
  if (!ts) return ''
  const ms = typeof ts === 'number' ? (ts > 1e12 ? ts : ts * 1000) : Date.parse(ts)
  if (isNaN(ms)) return ''
  const d = new Date(ms)
  const now = new Date()
  const isToday = d.toDateString() === now.toDateString()
  const time = d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  return isToday ? time : d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' }) + ' ' + time
}

export default function FishtoyCard({ data, eventType, itemCatalog = {}, onTargetClick }) {
  if (!data || typeof data !== 'object') return null
  const [expanded, setExpanded] = useState(false)

  const isQueued = eventType === 'fishtoy:queued'
  const hasMetadata = data.metadata && data.metadata !== 'null'

  const iid = String(data.itemId || '')
  const catalogEntry = itemCatalog[iid]
  const itemName = catalogEntry?.name || `Item #${iid}`
  const itemType = catalogEntry?.type || ''
  const isBigtoy = itemType === 'BIGTOY'

  const hasExpandableContent = hasMetadata || data.secondaryTarget || data.clanTag

  return (
    <div
      className={`animate-slide-in bg-tank-surface border rounded-lg transition-colors ${
        expanded ? 'border-tank-accent/40' : 'border-tank-border hover:border-tank-accent/20'
      }`}
    >
      {/* Compact header - always visible, clickable */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between gap-2 p-2.5 text-left"
      >
        <div className="flex items-center gap-2 min-w-0">
          <div className={`w-6 h-6 rounded flex items-center justify-center shrink-0 ${
            isBigtoy ? 'bg-purple-500/10' : 'bg-tank-accent/10'
          }`}>
            <Fish className={`w-3.5 h-3.5 ${isBigtoy ? 'text-purple-400' : 'text-tank-accent'}`} />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-1.5">
              <span className="text-xs font-semibold text-tank-bright truncate">{data.displayName || '?'}</span>
              <ArrowRight className="w-3 h-3 text-tank-muted shrink-0" />
              <span
                className="text-xs text-tank-warn font-medium cursor-pointer hover:underline shrink-0"
                onClick={(e) => { e.stopPropagation(); onTargetClick?.(data.target) }}
              >
                {data.target || '?'}
              </span>
            </div>
            <div className="flex items-center gap-1.5 text-[10px] text-tank-muted">
              <span className="truncate">{itemName}</span>
              {hasMetadata && (
                <FileText className="w-3 h-3 text-tank-accent shrink-0" />
              )}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {data.cost > 0 && (
            <span className="text-[10px] font-mono text-tank-warn">{data.cost}t</span>
          )}
          <span className="text-[10px] font-mono text-tank-muted">
            {formatTime(data.createdAt || data.updatedAt)}
          </span>
          {hasExpandableContent && (
            <ChevronDown className={`w-3 h-3 text-tank-muted transition-transform ${expanded ? 'rotate-180' : ''}`} />
          )}
        </div>
      </button>

      {/* Expanded detail */}
      {expanded && (
        <div className="px-2.5 pb-2.5 space-y-2 border-t border-tank-border/30 pt-2">
          <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-[11px]">
            <Detail label="Sender" value={data.displayName} />
            <Detail label="Target" value={data.target} />
            <Detail label="Item" value={itemName} />
            <Detail label="Type" value={itemType} color={isBigtoy ? 'text-purple-400' : 'text-tank-accent'} />
            <Detail label="Cost" value={data.cost ? `${data.cost} tokens` : '0'} color="text-tank-warn" />
            <Detail label="Status" value={data.status} />
            {data.secondaryTarget && <Detail label="Secondary" value={data.secondaryTarget} />}
            {data.clanTag && <Detail label="Clan" value={data.clanTag} />}
            <Detail label="ID" value={data.id} mono />
          </div>

          {hasMetadata && (
            <div className="bg-tank-bg border border-tank-accent/20 rounded px-3 py-2">
              <div className="flex items-center gap-1.5 mb-1">
                <FileText className="w-3 h-3 text-tank-accent" />
                <span className="text-[10px] font-mono text-tank-accent uppercase tracking-wider">Hidden Content</span>
              </div>
              <p className="text-sm text-tank-bright leading-relaxed font-sans break-words whitespace-pre-wrap">
                {typeof data.metadata === 'object' ? JSON.stringify(data.metadata) : data.metadata}
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function Detail({ label, value, color = 'text-tank-bright', mono = false }) {
  if (!value) return null
  return (
    <div className="flex items-baseline justify-between">
      <span className="text-tank-muted">{label}</span>
      <span className={`${color} ${mono ? 'font-mono text-[10px]' : ''}`}>{value}</span>
    </div>
  )
}
