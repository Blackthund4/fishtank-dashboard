import { Volume2, Music, Zap, Info } from 'lucide-react'

function formatTime(ts) {
  if (!ts) return ''
  const ms = ts > 1e12 ? ts : ts * 1000
  return new Date(ms).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

const EVENT_CONFIG = {
  'tts:queued': { icon: Volume2, color: 'text-purple-400', bg: 'bg-purple-500/10', label: 'TTS Queued' },
  'tts:update': { icon: Volume2, color: 'text-purple-400', bg: 'bg-purple-500/10', label: 'TTS' },
  'sfx:queued': { icon: Music, color: 'text-indigo-400', bg: 'bg-indigo-500/10', label: 'SFX Queued' },
  'sfx:update': { icon: Music, color: 'text-indigo-400', bg: 'bg-indigo-500/10', label: 'SFX' },
  'happening': { icon: Zap, color: 'text-tank-warn', bg: 'bg-orange-500/10', label: 'Happening' },
  'item:new': { icon: Info, color: 'text-cyan-400', bg: 'bg-cyan-500/10', label: 'New Item' },
  'item:update': { icon: Info, color: 'text-cyan-400', bg: 'bg-cyan-500/10', label: 'Item Update' },
}

export default function ActivityCard({ data, eventType, roomMap = {} }) {
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
        <div className="flex items-center gap-2">
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
        <div className="flex items-center gap-2 mt-0.5">
          {data.target && (
            <span className="text-[10px] text-tank-muted">
              Target: <span className="text-tank-warn">{data.target}</span>
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
}
