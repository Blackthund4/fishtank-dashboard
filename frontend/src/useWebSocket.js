import { useEffect, useRef, useState, useCallback } from 'react'

const STALE_THRESHOLD = 120000 // 2 minutes — reconnects after this gap trigger a full data re-fetch

export function useWebSocket(url) {
  const [isConnected, setIsConnected] = useState(false)
  const [staleReconnect, setStaleReconnect] = useState(0)
  const wsRef = useRef(null)
  const reconnectTimer = useRef(null)
  const listeners = useRef(new Set())

  const addListener = useCallback((fn) => {
    listeners.current.add(fn)
    return () => listeners.current.delete(fn)
  }, [])

  useEffect(() => {
    let alive = true
    let delay = 1000
    let wasConnected = false
    const lastMessageAt = { current: Date.now() }

    function connect() {
      if (!alive) return

      const gapMs = Date.now() - lastMessageAt.current

      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const wsUrl = url || `${protocol}//${window.location.host}/ws`
      const ws = new WebSocket(wsUrl)
      wsRef.current = ws

      ws.onopen = () => {
        setIsConnected(true)
        delay = 1000
        // If we previously had a connection and the gap since last message exceeds
        // the stale threshold, signal App.jsx to re-fetch all data
        if (wasConnected && gapMs > STALE_THRESHOLD) {
          setStaleReconnect(n => n + 1)
        }
        wasConnected = true
        lastMessageAt.current = Date.now()
        if (reconnectTimer.current) {
          clearTimeout(reconnectTimer.current)
          reconnectTimer.current = null
        }
      }

      ws.onmessage = (e) => {
        lastMessageAt.current = Date.now()
        try {
          const msg = JSON.parse(e.data)
          listeners.current.forEach((fn) => fn(msg))
        } catch (err) {
          // ignore malformed messages
        }
      }

      ws.onclose = () => {
        setIsConnected(false)
        wsRef.current = null
        if (alive) {
          reconnectTimer.current = setTimeout(connect, delay)
          delay = Math.min(delay * 2, 30000)
        }
      }

      ws.onerror = () => {
        ws.close()
      }
    }

    connect()

    // Heartbeat: detect silently dead connections (server pings every 60s)
    const heartbeat = setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN
          && Date.now() - lastMessageAt.current > 90000) {
        wsRef.current.close()
      }
    }, 15000)

    // Visibility: reconnect immediately when tab regains focus if stale
    const onVisible = () => {
      if (document.visibilityState !== 'visible') return
      if (!wsRef.current
          || wsRef.current.readyState !== WebSocket.OPEN
          || Date.now() - lastMessageAt.current > 90000) {
        if (wsRef.current) {
          wsRef.current.close()
        } else if (alive) {
          delay = 1000
          connect()
        }
      }
    }
    document.addEventListener('visibilitychange', onVisible)

    return () => {
      alive = false
      clearInterval(heartbeat)
      document.removeEventListener('visibilitychange', onVisible)
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current)
      if (wsRef.current) wsRef.current.close()
    }
  }, [url])

  return { isConnected, addListener, staleReconnect }
}
