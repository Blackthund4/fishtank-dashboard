import { useEffect, useRef } from 'react'

export default function Panel({ title, icon: Icon, count, children, className = '' }) {
  const scrollRef = useRef(null)
  const isScrolledToTop = useRef(true)

  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    if (isScrolledToTop.current) {
      el.scrollTop = 0
    }
  }, [count])  // only scroll when item count changes

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
        {count !== undefined && (
          <span className="text-[10px] font-mono text-tank-muted bg-tank-highlight px-1.5 py-0.5 rounded">
            {count}
          </span>
        )}
      </div>
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto p-2 space-y-1.5"
      >
        {children}
      </div>
    </div>
  )
}
