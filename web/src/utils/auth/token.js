import { lStorage, sStorage } from '@/utils/storage'

const TOKEN_CODE = 'access_token'

function migrateLegacyToken() {
  if (sStorage.get(TOKEN_CODE)) {
    lStorage.remove(TOKEN_CODE)
    return
  }
  const old = lStorage.get(TOKEN_CODE)
  if (old) {
    sStorage.set(TOKEN_CODE, old)
    lStorage.remove(TOKEN_CODE)
  }
}

export function getToken() {
  migrateLegacyToken()
  return sStorage.get(TOKEN_CODE)
}

export function setToken(token) {
  sStorage.set(TOKEN_CODE, token)
  lStorage.remove(TOKEN_CODE)
}

export function removeToken() {
  sStorage.remove(TOKEN_CODE)
  lStorage.remove(TOKEN_CODE)
}

/** access token 到期前提前刷新的时间窗（含网络/时钟偏差） */
const REFRESH_AHEAD_MS = 60 * 1000

function decodeJwtPayload(token) {
  try {
    const part = String(token || '').split('.')[1]
    if (!part) return null
    const base64 = part.replace(/-/g, '+').replace(/_/g, '/')
    const padded = base64 + '='.repeat((4 - (base64.length % 4)) % 4)
    const json = decodeURIComponent(
      atob(padded)
        .split('')
        .map((c) => `%${`00${c.charCodeAt(0).toString(16)}`.slice(-2)}`)
        .join('')
    )
    return JSON.parse(json)
  } catch {
    return null
  }
}

export function isTokenExpiring(token, aheadMs = REFRESH_AHEAD_MS) {
  const payload = decodeJwtPayload(token)
  if (!payload || typeof payload.exp !== 'number') return false
  return payload.exp * 1000 - Date.now() <= aheadMs
}

/** 返回可用的 access token：缺失或临近过期时静默刷新，刷新失败则回退当前值。 */
export async function ensureFreshToken() {
  const token = getToken()
  if (!token) return await tryRefreshToken()
  if (isTokenExpiring(token)) {
    return (await tryRefreshToken()) || getToken() || token
  }
  return token
}

/**
 * 带鉴权的 fetch（供 SSE / 二进制流等无法走 axios 的场景使用）：
 * 请求前主动刷新临近过期的 token；遇 401 再刷新并重试一次。
 */
export async function authFetch(input, init = {}) {
  const buildInit = (tk) => {
    const headers = new Headers(init.headers || {})
    if (tk) headers.set('token', tk)
    return { credentials: 'include', ...init, headers }
  }
  const token = await ensureFreshToken()
  let res = await fetch(input, buildInit(token))
  if (res.status === 401) {
    const refreshed = await tryRefreshToken()
    if (refreshed) {
      res = await fetch(input, buildInit(refreshed))
    }
  }
  return res
}

let refreshing = null

export async function tryRefreshToken() {
  if (refreshing) return refreshing
  refreshing = (async () => {
    try {
      const base = import.meta.env.VITE_BASE_API || '/api/v1'
      const res = await fetch(`${base}/base/refresh`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
      })
      const data = await res.json().catch(() => null)
      const tok = data?.data?.access_token
      if (data?.code === 200 && tok) {
        setToken(tok)
        return tok
      }
      return null
    } catch {
      return null
    } finally {
      refreshing = null
    }
  })()
  return refreshing
}
