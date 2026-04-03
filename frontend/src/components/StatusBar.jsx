import { useEffect, useState } from 'react'
import { Fish, MessageSquare, Volume2, Clock } from 'lucide-react'

export default function StatusBar({ isConnected, stats }) {
  const [health, setHealth] = useState(null)
  const [tankTime, setTankTime] = useState('')

  useEffect(() => {
    const check = () => {
      fetch('/api/status')
        .then((r) => r.json())
        .then(setHealth)
        .catch(() => setHealth(null))
    }
    check()
    const interval = setInterval(check, 15000)
    return () => clearInterval(interval)
  }, [])

  useEffect(() => {
    const tick = () => {
      setTankTime(new Date().toLocaleTimeString('en-US', {
        timeZone: 'America/New_York',
        hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true,
      }))
    }
    tick()
    const interval = setInterval(tick, 1000)
    return () => clearInterval(interval)
  }, [])

  return (
    <header className="bg-tank-surface border-b border-tank-border flex flex-wrap items-center justify-between px-2 sm:px-4 gap-y-1 shrink-0">
      <div className="flex items-center gap-1 sm:gap-3">
        <div className="flex items-center gap-2">
          <Fish className="w-5 h-5 text-tank-accent" />
          <span className="font-sans font-bold text-tank-bright text-sm tracking-wide uppercase">
            Fishtank Dashboard
          </span>
        </div>
        <div className="w-px h-5 bg-tank-border mx-1" />
        <div className="flex items-center gap-1.5">
          {isConnected ? (
            <>
              <span className="w-1.5 h-1.5 rounded-full bg-tank-accent animate-pulse-dot" />
              <span className="text-xs text-tank-accent font-mono">LIVE</span>
            </>
          ) : (
            <>
              <span className="w-1.5 h-1.5 rounded-full bg-tank-danger" />
              <span className="text-xs text-tank-danger font-mono">OFFLINE</span>
            </>
          )}
        </div>
        {health && (
          <>
            <div className="w-px h-5 bg-tank-border mx-1" />
            <span className="text-xs text-tank-muted font-mono hidden sm:inline">
              {health.fishtank_online > 0 && <><span className="text-tank-bright">{health.fishtank_online.toLocaleString()}</span> watching | </>}{health.browser_clients} fish-dasher{health.browser_clients !== 1 ? 's' : ''}
            </span>
          </>
        )}
      </div>
      <div className="flex items-center gap-1 sm:gap-4">
        <div className="flex items-center gap-1.5">
          <Clock className="w-3.5 h-3.5 text-tank-warn" />
          <span className="text-xs font-mono text-tank-muted hidden sm:inline">Tank Time</span>
          <span className="text-xs font-mono text-tank-bright">{tankTime}</span>
        </div>
        <div className="w-px h-5 bg-tank-border" />
        <StatChip icon={Fish} label="Fishtoys" value={stats.fishtoys} color="text-tank-accent" />
        <StatChip icon={MessageSquare} label="Chat" value={stats.chats} color="text-blue-400" />
        <StatChip icon={Volume2} label="TTS/SFX" value={stats.tts + stats.sfx} color="text-purple-400" />
        {stats.total_spend > 0 && (
          <div className="text-xs font-mono text-tank-warn hidden md:block">
            {stats.total_spend.toLocaleString()} tokens spent
            <span className="text-green-400 ml-2">({(stats.total_spend * 0.10).toLocaleString('en-US', {style:'currency',currency:'USD'})})</span>
          </div>
        )}
      </div>
    </header>
  )
}

function StatChip({ icon: Icon, label, value, color }) {
  return (
    <div className="flex items-center gap-1.5">
      <Icon className={`w-3.5 h-3.5 ${color}`} />
      <span className="text-xs font-mono text-tank-muted hidden sm:inline">{label}</span>
      <span className="text-xs font-mono text-tank-bright">{value}</span>
    </div>
  )
}
