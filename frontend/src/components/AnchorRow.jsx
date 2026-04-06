import { useCallback, useMemo } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'

const ANCHOR_BUTTONS = [
  { id: '30d', label: '30d', days: 30 },
  { id: '10d', label: '10d', days: 10 },
  { id: '7d',  label: '7d',  days: 7 },
  { id: '3d',  label: '3d',  days: 3 },
  { id: '1d',  label: '1d',  days: 1 },
  { id: 'now', label: 'Now', days: null },
]

export const RANGE_MS = {
  '30m': 30*60e3, '1h': 3600e3, '2h': 2*3600e3, '3h': 3*3600e3,
  '6h': 6*3600e3, '12h': 12*3600e3, '24h': 86400e3, '1d': 86400e3,
  '3d': 3*86400e3, '7d': 7*86400e3, 'all': 30*86400e3,
}

export default function AnchorRow({ anchor, anchorLabel, onAnchorChange, range, compact }) {
  const panStep = RANGE_MS[range] || RANGE_MS['24h']
  const iconSize = compact ? 12 : 14
  const textSize = compact ? 'text-[9px]' : 'text-[10px]'
  const px = compact ? 'px-1.5' : 'px-2'

  const panLeft = useCallback(() => {
    const ref = anchor ? new Date(anchor).getTime() : Date.now()
    onAnchorChange(new Date(ref - panStep).toISOString(), null)
  }, [anchor, panStep, onAnchorChange])

  const panRight = useCallback(() => {
    if (!anchor) return
    const next = new Date(new Date(anchor).getTime() + panStep)
    if (next >= new Date()) onAnchorChange(null, 'now')
    else onAnchorChange(next.toISOString(), null)
  }, [anchor, panStep, onAnchorChange])

  const anchorDisplay = useMemo(() => {
    if (!anchor) return null
    const d = new Date(anchor)
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' +
      d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', hour12: true })
  }, [anchor])

  return (
    <div className="flex items-center gap-1">
      <button onClick={panLeft} className="text-tank-muted hover:text-tank-accent transition-colors p-0.5 rounded"
        title="Pan earlier"><ChevronLeft size={iconSize} /></button>
      {ANCHOR_BUTTONS.map(b => (
        <button key={b.id} onClick={() => {
          if (b.days === null) onAnchorChange(null, 'now')
          else onAnchorChange(new Date(Date.now() - b.days * 86400e3).toISOString(), b.id)
        }} className={`${textSize} font-mono ${px} py-0.5 rounded transition-colors ${
          anchorLabel === b.id
            ? compact
              ? 'bg-amber-400/20 text-amber-400 border border-amber-400/40'
              : 'border-amber-400 text-amber-400 bg-amber-400/10 border'
            : compact
              ? 'text-tank-muted hover:text-tank-text'
              : 'border-tank-border text-tank-muted hover:text-tank-text border'
        }`}>{b.label}</button>
      ))}
      <button onClick={panRight} disabled={!anchor}
        className={`p-0.5 rounded transition-colors ${anchor ? 'text-tank-muted hover:text-tank-accent' : 'text-tank-border cursor-not-allowed'}`}
        title="Pan later"><ChevronRight size={iconSize} /></button>
      {anchorDisplay && (
        <span className={`${textSize} font-mono text-amber-400/70 ml-1`}>{compact ? '' : 'Viewing: '}{anchorDisplay}</span>
      )}
    </div>
  )
}
