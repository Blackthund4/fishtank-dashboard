import { memo } from 'react'
import { Volume2, Music, Zap, Info, Star } from 'lucide-react'
import { formatTime } from '../utils/formatTime'

const EVENT_CONFIG = {
  'tts:update': { icon: Volume2, color: 'text-purple-400', bg: 'bg-purple-500/10', label: 'TTS' },
  'sfx:update': { icon: Music, color: 'text-indigo-400', bg: 'bg-indigo-500/10', label: 'SFX' },
  'happening': { icon: Zap, color: 'text-tank-warn', bg: 'bg-orange-500/10', label: 'Happening' },
  'item:new': { icon: Info, color: 'text-cyan-400', bg: 'bg-cyan-500/10', label: 'New Item' },
  'item:update': { icon: Info, color: 'text-cyan-400', bg: 'bg-cyan-500/10', label: 'Item Update' },
  'super-chat:new': { icon: Star, color: 'text-amber-400', bg: 'bg-amber-500/10', label: 'SC' },
}

export default memo(function ActivityCard({ data, eventType, roomMap = {} }) {
  if (!data || typeof data !== 'object') return null

  const config = EVENT_CONFIG[eventType] || {
    icon: Info, color: 'text-tank-muted', bg: 'bg-tank-highlight', label: eventType,
  }
  const Icon = config.icon

  const name = data.displayName || data.user?.displayName || ''
  const message = data.message || data.text || ''
  const ts = data.timestamp || data.createdAt || data.updatedAt
  const roomCode = data.room || ''
  const roomName = roomMap[roomCode] || roomCode

  return (
    <div className="animate-slide-in flex items-start gap-2 p-2 hover:bg-tank-highlight/50 rounded">
      <div className={`w-6 h-6 rounded flex items-center justify-center shrink-0 ${config.bg}`}>
        <Icon className={`w-3.5 h-3.5 ${config.color}`} />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1 sm:gap-2">
          <span className={`text-[10px] font-mono px-1 rounded ${config.bg} ${config.color}`}>
            {config.label}
          </span>
          {name && <span className="text-xs font-medium text-tank-bright">{name}</span>}
          {data.cost > 0 && (
            <span className="text-[10px] font-mono text-tank-warn">{data.cost}t</span>
          )}
          <span className="text-[10px] font-mono text-tank-muted ml-auto shrink-0">
            {formatTime(ts)}
          </span>
        </div>
        {message && (
          <p className="text-xs text-tank-text mt-0.5 break-words leading-relaxed">{message}</p>
        )}
        <div className="flex flex-wrap items-center gap-1 sm:gap-2 mt-0.5">
          {data.target && (
            <span className="text-[10px] text-tank-muted">
              Target: <span className="text-tank-warn">{data.target}</span>
            </span>
          )}
          {data.duration && (
            <span className="text-[10px] text-tank-muted">
              Duration: <span className="text-amber-400">{data.duration}min</span>
            </span>
          )}
          {data.room && (
            <span className="text-[10px] text-tank-muted">
              Room: <span className="text-cyan-400">{roomName}</span>
            </span>
          )}
        </div>
      </div>
    </div>
  )
})
