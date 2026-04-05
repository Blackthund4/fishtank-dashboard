// Service worker: retry navigation requests during deploys
// When the Docker container rebuilds, hashed assets 404 briefly.
// This SW catches those failures and retries after a short delay.

const RETRY_DELAY = 2000
const MAX_RETRIES = 3

self.addEventListener('fetch', (event) => {
  if (event.request.mode !== 'navigate') return

  event.respondWith(
    fetchWithRetry(event.request, MAX_RETRIES)
  )
})

async function fetchWithRetry(request, retries) {
  try {
    const response = await fetch(request)
    if (response.ok || response.type === 'opaqueredirect') return response
    if (retries > 0 && response.status >= 500) {
      await new Promise(r => setTimeout(r, RETRY_DELAY))
      return fetchWithRetry(request, retries - 1)
    }
    return response
  } catch (err) {
    if (retries > 0) {
      await new Promise(r => setTimeout(r, RETRY_DELAY))
      return fetchWithRetry(request, retries - 1)
    }
    throw err
  }
}

// Skip waiting on install so new SW activates immediately
self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim())
})
