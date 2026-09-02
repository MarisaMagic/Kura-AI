import { getToken, isNullOrWhitespace, tryRefreshToken } from '@/utils'

const WHITE_LIST = ['/login', '/register', '/404']
export function createAuthGuard(router) {
  router.beforeEach(async (to) => {
    let token = getToken()
    if (isNullOrWhitespace(token) && !WHITE_LIST.includes(to.path)) {
      token = await tryRefreshToken()
    }

    /** 没有token的情况 */
    if (isNullOrWhitespace(token)) {
      if (WHITE_LIST.includes(to.path)) return true
      return { path: 'login', query: { ...to.query, redirect: to.path } }
    }

    /** 有token的情况 */
    if (to.path === '/login') return { path: '/' }
    if (to.path === '/register') return { path: '/' }
    return true
  })
}
