import { memo } from 'react'
import { formatTime } from '../utils/formatTime'

function formatXP(xp) {
  if (xp >= 1000) return `${(xp / 1000).toFixed(1).replace(/\.0$/, '')}k`
  return String(xp)
}

// Defensive validators for user-controlled CSS colors. customUsernameColor
// and endorsementColor come straight from the fishtank API; React's CSS
// parser already rejects code execution, but a malformed value can still
// produce broken layout or be used to smuggle URLs. Accept only hex and
// rgb/hsl functional notations, fall back to the tank palette default.
const CSS_HEX_RE = /^#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i
const CSS_FUNC_RE = /^(?:rgba?|hsla?)\([\s\d.,%/]+\)$/i
function safeColor(value, fallback) {
  if (typeof value !== 'string') return fallback
  if (value.length > 64) return fallback
  if (CSS_HEX_RE.test(value) || CSS_FUNC_RE.test(value)) return value
  return fallback
}

export default memo(function ChatMessage({ data }) {
  if (!data || typeof data !== 'object') return null

  const user = data.user || {}
  const meta = data.metadata || {}
  const nameColor = safeColor(user.customUsernameColor, '#c8cdd4')

  const badges = []
  if (meta.isAdmin) badges.push({ label: 'ADMIN', color: 'bg-green-500/20 text-green-400' })
  if (meta.isMod) badges.push({ label: 'MOD', color: 'bg-sky-500/20 text-sky-400' })
  if (meta.isFish) badges.push({ label: 'FISH', color: 'bg-pink-500/20 text-pink-400' })
  if (meta.isGrandMarshall) badges.push({ label: 'GM', color: 'bg-red-500/20 text-red-400' })
  if (meta.isEpic) badges.push({ label: 'EPIC', color: 'bg-amber-500/20 text-amber-400' })

  const rowColor = meta.isAdmin ? 'bg-green-500/10 border-l-2 border-green-500/40'
    : meta.isGrandMarshall ? 'bg-red-500/10 border-l-2 border-red-500/40'
    : meta.isEpic ? 'bg-amber-500/10 border-l-2 border-amber-500/40'
    : meta.isMod ? 'bg-sky-500/10 border-l-2 border-sky-500/40'
    : meta.isFish ? 'bg-pink-500/10 border-l-2 border-pink-500/40'
    : ''

  return (
    <div className={`animate-slide-in flex gap-2 py-1 px-2 hover:bg-tank-highlight/50 rounded text-sm group ${rowColor}`}>
      <span className="text-[10px] font-mono text-tank-muted shrink-0 pt-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
        {formatTime(data.timestamp)}
      </span>
      {user.photoURL && (
        <img
          src={user.photoURL}
          alt=""
          loading="lazy"
          className="w-5 h-5 rounded-full shrink-0 mt-0.5"
          onError={e => { e.target.style.display = 'none' }}
        />
      )}
      <div className="min-w-0">
        <span className="inline-flex items-center gap-1 flex-wrap">
          {badges.map((b) => (
            <span key={b.label} className={`text-[9px] font-mono px-1 rounded ${b.color}`}>
              {b.label}
            </span>
          ))}
          <span className="font-semibold text-sm" style={{ color: nameColor }}>
            {user.displayName || '?'}
          </span>
          {user.endorsement && (
            <span
              className="text-[10px] font-mono px-1 rounded bg-tank-highlight/50"
              style={{ color: safeColor(user.endorsementColor, '#a78bfa') }}
            >
              {user.endorsement}
            </span>
          )}
          {user.xp > 0 && (
            <span className="text-[10px] font-mono text-tank-muted">
              {formatXP(user.xp)}xp
            </span>
          )}
        </span>
        <span className="text-tank-text ml-1.5 break-words">{data.message || ''}</span>
      </div>
    </div>
  )
})
