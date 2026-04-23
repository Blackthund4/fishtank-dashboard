import { useEffect, useState, memo } from 'react'
import { Fish, MessageSquare, Volume2, Clock, RefreshCw, Wifi, WifiOff, Coins } from 'lucide-react'
import { okJson, apiFetch } from '../utils/fetchUtils'

const fmt = (n) => Number(n || 0).toLocaleString()
const fmtUSD = (n) => (n * 0.10).toLocaleString('en-US', { style: 'currency', currency: 'USD' })

// Isolated timer component — re-renders every second without affecting parent
function TimeDisplay() {
  const [tankTime, setTankTime] = useState('')
  const [localTime, setLocalTime] = useState('')

  useEffect(() => {
    const localTz = Intl.DateTimeFormat().resolvedOptions().timeZone
    const localShort = new Date().toLocaleTimeString('en-US', {
      timeZone: localTz, timeZoneName: 'short',
    }).split(' ').pop()
    const tick = () => {
      const now = new Date()
      setTankTime(now.toLocaleTimeString('en-US', {
        timeZone: 'America/New_York',
        hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true,
      }))
      setLocalTime(now.toLocaleTimeString('en-GB', {
        hour: '2-digit', minute: '2-digit', second: '2-digit',
      }) + ' ' + localShort)
    }
    tick()
    const interval = setInterval(tick, 1000)
    return () => clearInterval(interval)
  }, [])

  return (
    <div className="flex items-center gap-1.5">
      <Clock className="w-3.5 h-3.5 text-tank-amber" />
      <span className="text-[11px] font-mono text-tank-muted hidden sm:inline">Tank</span>
      <span className="text-[11px] font-mono text-tank-bright tabular-nums">{tankTime}</span>
      <span className="text-[11px] font-mono text-tank-muted hidden lg:inline">| {localTime}</span>
    </div>
  )
}

export default function StatusBar({ isConnected, stats, updateAvailable }) {
  const [health, setHealth] = useState(null)

  useEffect(() => {
    const check = () => {
      apiFetch('/api/status')
        .then(okJson)
        .then(setHealth)
        .catch(() => setHealth(null))
    }
    check()
    const interval = setInterval(check, 15000)
    return () => clearInterval(interval)
  }, [])

  return (
    <header className="bg-tank-surface/80 backdrop-blur-sm border-b border-tank-border flex flex-wrap items-center justify-between px-2 sm:px-4 gap-y-1 shrink-0 py-1">
      {/* Row 1 left: Brand + connection + health */}
      <div className="flex items-center gap-1.5 sm:gap-3">
        <div className="flex items-center gap-2">
          <div className="relative">
            <Fish className="w-5 h-5 text-tank-accent" />
            {isConnected && <span className="absolute -top-0.5 -right-0.5 w-1.5 h-1.5 rounded-full bg-tank-accent animate-live-glow" />}
          </div>
          <span className="font-display font-bold text-tank-bright text-sm tracking-wide uppercase hidden sm:inline">
            Fishtank Dashboard
          </span>
          <span className="font-display font-bold text-tank-bright text-sm tracking-wide uppercase sm:hidden">
            FTD
          </span>
        </div>
        <div className="w-px h-5 bg-tank-border" />
        <div className={`flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-mono uppercase tracking-wider transition-colors ${
          isConnected
            ? 'bg-tank-accent/10 text-tank-accent'
            : 'bg-tank-danger/10 text-tank-danger'
        }`}>
          {isConnected ? (
            <>
              <Wifi className="w-3 h-3" />
              <span>Live</span>
            </>
          ) : (
            <>
              <WifiOff className="w-3 h-3" />
              <span>Offline</span>
            </>
          )}
        </div>
        {health && (
          <>
            <div className="w-px h-5 bg-tank-border hidden sm:block" />
            <span className="text-[11px] text-tank-muted font-mono hidden sm:inline">
              {health.fishtank_online > 0 && <><span className="text-tank-bright tabular-nums">{fmt(health.fishtank_online)}</span> watching<span className="hidden md:inline"> | </span><span className="hidden md:inline">{fmt(health.browser_clients)} dasher{health.browser_clients !== 1 ? 's' : ''}</span></>}
              {!health.fishtank_online && <>{fmt(health.browser_clients)} dasher{health.browser_clients !== 1 ? 's' : ''}</>}
            </span>
          </>
        )}
        {updateAvailable && (
          <>
            <div className="w-px h-5 bg-tank-border" />
            <button
              onClick={() => window.location.reload()}
              className="text-[11px] font-mono text-green-400 hover:text-green-300 transition-colors cursor-pointer flex items-center gap-1 bg-green-500/10 px-2 py-0.5 rounded-full hover:bg-green-500/15"
            >
              <RefreshCw className="w-3 h-3" />
              <span className="hidden sm:inline">Update available</span>
              <span className="sm:hidden">Update</span>
            </button>
          </>
        )}
      </div>

      {/* Row 1 right: Time + stats */}
      <div className="flex items-center gap-2 sm:gap-3">
        <TimeDisplay />
        <div className="w-px h-4 bg-tank-border" />
        <div className="flex items-center gap-2 sm:gap-3">
          <StatChip icon={Fish} label="Fishtoys" value={fmt(stats.fishtoys)} color="text-tank-accent" />
          <StatChip icon={MessageSquare} label="Chat" value={fmt(stats.chats)} color="text-blue-400" />
          <StatChip icon={Volume2} label="TTS/SFX" value={fmt(stats.tts + stats.sfx)} color="text-purple-400" />
        </div>
        {/* Token spend: visible inline on md+, wraps to its own row below md */}
        {stats.total_spend > 0 && (
          <>
            <div className="w-px h-4 bg-tank-border hidden md:block" />
            <div className="text-[11px] font-mono hidden md:flex items-center gap-1.5">
              <span className="text-tank-amber tabular-nums">{fmt(stats.total_spend)}t</span>
              <span className="text-green-400/80">({fmtUSD(stats.total_spend)})</span>
            </div>
          </>
        )}
      </div>

      {/* Row 3 (narrow only): Token spend - full width below md */}
      {stats.total_spend > 0 && (
        <div className="flex md:hidden items-center justify-center gap-2 w-full py-0.5 border-t border-tank-border/50 mt-0.5">
          <Coins className="w-3 h-3 text-tank-amber opacity-70" />
          <span className="text-[11px] font-mono text-tank-amber tabular-nums">{fmt(stats.total_spend)} tokens</span>
          <span className="text-[11px] font-mono text-green-400/80">({fmtUSD(stats.total_spend)})</span>
        </div>
      )}
    </header>
  )
}

function StatChip({ icon: Icon, label, value, color }) {
  return (
    <div className="flex items-center gap-1">
      <Icon className={`w-3 h-3 ${color} opacity-70`} />
      <span className="text-[10px] font-mono text-tank-muted hidden lg:inline">{label}</span>
      <span className="text-[11px] font-mono text-tank-bright tabular-nums">{value}</span>
    </div>
  )
}
