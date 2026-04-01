function formatTime(ts) {
  if (!ts) return ''
  const ms = typeof ts === 'number' ? (ts > 1e12 ? ts : ts * 1000) : Date.parse(ts)
  if (isNaN(ms)) return ''
  const d = new Date(ms)
  const now = new Date()
  // Compare dates in UTC to avoid timezone mismatch
  const isToday = d.getUTCFullYear() === now.getUTCFullYear() &&
    d.getUTCMonth() === now.getUTCMonth() && d.getUTCDate() === now.getUTCDate()
  const time = d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  return isToday ? time : d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' }) + ' ' + time
}

export default function ChatMessage({ data }) {
  if (!data || typeof data !== 'object') return null

  const user = data.user || {}
  const meta = data.metadata || {}
  const nameColor = user.customUsernameColor || '#c8cdd4'

  const badges = []
  if (meta.isAdmin) badges.push({ label: 'ADMIN', color: 'bg-red-500/20 text-red-400' })
  if (meta.isMod) badges.push({ label: 'MOD', color: 'bg-purple-500/20 text-purple-400' })
  if (meta.isFish) badges.push({ label: 'FISH', color: 'bg-tank-accent/20 text-tank-accent' })

  return (
    <div className="animate-slide-in flex gap-2 py-1 px-2 hover:bg-tank-highlight/50 rounded text-sm group">
      <span className="text-[10px] font-mono text-tank-muted shrink-0 pt-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
        {formatTime(data.timestamp)}
      </span>
      <div className="min-w-0">
        <span className="inline-flex items-center gap-1">
          {badges.map((b) => (
            <span key={b.label} className={`text-[9px] font-mono px-1 rounded ${b.color}`}>
              {b.label}
            </span>
          ))}
          <span className="font-semibold text-sm" style={{ color: nameColor }}>
            {user.displayName || '?'}
          </span>
        </span>
        <span className="text-tank-text ml-1.5 break-words">{data.message || ''}</span>
      </div>
    </div>
  )
}
