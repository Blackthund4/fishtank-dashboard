import { useEffect, useRef } from 'react'

export default function Panel({ title, icon: Icon, count, extra, children, className = '', virtualized = false }) {
  const scrollRef = useRef(null)
  const isScrolledToTop = useRef(true)

  useEffect(() => {
    if (virtualized) return
    const el = scrollRef.current
    if (!el) return
    if (isScrolledToTop.current) {
      el.scrollTop = 0
    }
  }, [count, virtualized])

  const handleScroll = (e) => {
    isScrolledToTop.current = e.target.scrollTop < 10
  }

  return (
    <div className={`flex flex-col bg-tank-surface border border-tank-border rounded-lg overflow-hidden ${className}`}>
      <div className="flex items-center justify-between px-3 py-2 border-b border-tank-border shrink-0">
        <div className="flex items-center gap-2">
          {Icon && <Icon className="w-4 h-4 text-tank-muted" />}
          <span className="text-xs font-semibold text-tank-bright uppercase tracking-wider">
            {title}
          </span>
        </div>
        <div className="flex items-center gap-2">
          {extra}
          {count !== undefined && (
            <span className="text-[10px] font-mono text-tank-muted bg-tank-highlight px-1.5 py-0.5 rounded">
              {count}
            </span>
          )}
        </div>
      </div>
      {virtualized ? (
        <div className="flex-1 min-h-0">
          {children}
        </div>
      ) : (
        <div
          ref={scrollRef}
          onScroll={handleScroll}
          className="flex-1 overflow-y-auto p-2 space-y-1.5"
        >
          {children}
        </div>
      )}
    </div>
  )
}
