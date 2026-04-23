let _token = document.querySelector('meta[name="api-token"]')?.content || ''
let _tokenIssuedAt = _token ? parseInt(_token.split('.')[0], 10) || 0 : 0

const TOKEN_LIFETIME = 1800 // 30 minutes (must match server TOKEN_LIFETIME)

function _isTokenExpiringSoon() {
  if (!_tokenIssuedAt) return false
  return (Date.now() / 1000) - _tokenIssuedAt > TOKEN_LIFETIME - 300
}

let _refreshPromise = null

async function _refreshToken() {
  if (_refreshPromise) return _refreshPromise
  _refreshPromise = fetch('/api/token/refresh', {
    headers: { 'x-dashboard-token': _token },
  })
    .then(r => {
      if (!r.ok) throw new Error(r.status)
      return r.json()
    })
    .then(data => {
      _token = data.token
      _tokenIssuedAt = parseInt(_token.split('.')[0], 10) || 0
    })
    .catch(() => {
      // Keep old token — next page load will get a fresh one
    })
    .finally(() => {
      _refreshPromise = null
    })
  return _refreshPromise
}

export function getToken() {
  return _token
}

export async function refreshToken() {
  return _refreshToken()
}

export function okJson(r) { if (!r.ok) throw new Error(r.status); return r.json() }

export async function apiFetch(path, opts = {}) {
  if (_isTokenExpiringSoon()) await _refreshToken()

  const headers = { ...opts.headers, 'x-dashboard-token': _token }
  let res = await fetch(path, { ...opts, headers })

  if (res.status === 403) {
    await _refreshToken()
    headers['x-dashboard-token'] = _token
    res = await fetch(path, { ...opts, headers })
  }

  return res
}
