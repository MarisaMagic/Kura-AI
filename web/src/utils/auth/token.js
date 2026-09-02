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
