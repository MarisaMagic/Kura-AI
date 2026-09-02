import axios from 'axios'
import { resReject, resResolve, reqReject, reqResolve } from './interceptors'

export function createAxios(options = {}) {
  const defaultOptions = {
    timeout: 12000,
    withCredentials: true,
  }
  const service = axios.create({
    ...defaultOptions,
    ...options,
  })
  service.interceptors.request.use(reqResolve, reqReject)
  service.interceptors.response.use(resResolve, async (error) => {
    const config = error?.config
    const status = error?.response?.status
    const code = error?.response?.data?.code
    const isAuthFail = status === 401 || code === 401
    if (isAuthFail && config && !config._retry && !config.noNeedToken) {
      config._retry = true
      const { tryRefreshToken } = await import('@/utils/auth/token')
      const tok = await tryRefreshToken()
      if (tok) {
        config.headers = config.headers || {}
        config.headers.token = tok
        return service(config)
      }
    }
    return resReject(error)
  })
  return service
}

export const request = createAxios({
  baseURL: import.meta.env.VITE_BASE_API,
})
