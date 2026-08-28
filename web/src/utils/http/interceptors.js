import { getToken } from '@/utils'
import { resolveResError } from './helpers'
import { useUserStore } from '@/store'

export function reqResolve(config) {
  // 处理不需要token的请求
  if (config.noNeedToken) {
    return config
  }

  const token = getToken()
  if (token) {
    config.headers.token = config.headers.token || token
  }

  return config
}

export function reqReject(error) {
  return Promise.reject(error)
}

export function resResolve(response) {
  const { data, status, statusText } = response
  if (data?.code !== 200) {
    const code = data?.code ?? status
    /** 根据code处理对应的操作，并返回处理后的message */
    const message = resolveResError(code, data?.msg ?? statusText)
    /** noErrorMessage：调用方自行处理错误展示（如进度轮询），拦截器不弹全局提示 */
    if (!response?.config?.noErrorMessage) {
      window.$message?.error(message, { keepAliveOnHover: true })
    }
    return Promise.reject({ code, message, error: data || response })
  }
  return Promise.resolve(data)
}

export async function resReject(error) {
  const silent = Boolean(error?.config?.noErrorMessage)
  if (!error || !error.response) {
    const code = error?.code
    /** 根据code处理对应的操作，并返回处理后的message */
    const message = resolveResError(code, error.message)
    if (!silent) {
      window.$message?.error(message)
    }
    return Promise.reject({ code, message, error })
  }
  const { data, status } = error.response

  if (data?.code === 401) {
    try {
      const userStore = useUserStore()
      userStore.logout()
    } catch (error) {
      console.log('resReject error', error)
      return
    }
  }
  // 后端返回的response数据
  const code = data?.code ?? status
  const message = resolveResError(code, data?.msg ?? error.message)
  if (!silent) {
    window.$message?.error(message, { keepAliveOnHover: true })
  }
  return Promise.reject({ code, message, error: error.response?.data || error.response })
}
