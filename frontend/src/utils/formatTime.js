/**
 * Format a timestamp for display. Shows time-only for today, date+time for older.
 * @param {number|string} ts - Unix timestamp (seconds or ms) or ISO string
 * @param {boolean} alwaysShowDate - Always include the date prefix
 */
export function formatTime(ts, alwaysShowDate = false) {
  if (!ts) return ''
  const ms = typeof ts === 'number' ? (ts > 1e12 ? ts : ts * 1000) : Date.parse(ts)
  if (isNaN(ms)) return ''
  const d = new Date(ms)
  const now = new Date()
  const isToday = !alwaysShowDate &&
    d.getUTCFullYear() === now.getUTCFullYear() &&
    d.getUTCMonth() === now.getUTCMonth() && d.getUTCDate() === now.getUTCDate()
  const time = d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  return isToday ? time : d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' }) + ' ' + time
}

/**
 * Format a timestamp always showing date + time.
 */
export function formatDateTime(ts) {
  return formatTime(ts, true)
}
